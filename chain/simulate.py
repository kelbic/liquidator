"""On-chain liquidatability gate — the monitor-grade core of the dry-run idea
(docs/WORKFLOW.md). Upgrades morpho.py's USD-approx flag to the EXACT health the
Morpho Blue contract itself enforces, read live via eth_call:

    maxBorrow = wMulDown(mulDivDown(collateral, oraclePrice, 1e36), lltv)
    borrowed  = toAssetsUp(borrowShares, totalBorrowAssets, totalBorrowShares)
    liquidatable  <=>  maxBorrow < borrowed

ABI/formula verified against Morpho docs + BaseScan (IMorpho, SharesMathLib).

Reads are split per-market vs per-borrower: idToMarketParams/market/oracle.price are
PER-MARKET (read once via read_market_context); only position() is per-borrower. So a
market with N borrowers costs 3 + N calls, not 4N. The scanner (loop) should batch the
N position() calls through Multicall3 (Base: 0xcA11...CA11) -> 4 calls/market regardless.

Two honest scope limits for monitor:
  * market() totals are NOT interest-accrued (values at lastUpdate). Borrowed is
    therefore very slightly understated -> HF slightly overstated -> we err toward
    "healthy", never a false "liquidate". Good enough to FLAG; execute will accrue.
  * the real revert/profit check (eth_call of the actual liquidate+swap tx) needs
    the deployed flash-loan liquidator contract -> that's `simulate_tx`, execute
    phase. Here profit is the ANALYTIC estimate from strategy.pnl (LIF-based).
"""
from __future__ import annotations
from dataclasses import dataclass

from chain.rpc import BaseRpc
from chain.multicall import (aggregate3, encode_market_call, decode_market,
    encode_id_to_market_params_call, decode_id_to_market_params,
    encode_price_call, decode_price, encode_position_call, decode_position)
from strategy.pnl import PnlInputs, net_profit, lif_from_lltv

# Morpho Blue exact integer math (SharesMathLib + the health check in _liquidate).
ORACLE_PRICE_SCALE = 10 ** 36
WAD = 10 ** 18
VIRTUAL_SHARES = 10 ** 6
VIRTUAL_ASSETS = 1

# Minimal read-only ABI (function signatures verified against IMorpho on BaseScan).
MORPHO_READ_ABI = [
    {"name": "idToMarketParams", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "id", "type": "bytes32"}],
     "outputs": [{"name": "loanToken", "type": "address"},
                 {"name": "collateralToken", "type": "address"},
                 {"name": "oracle", "type": "address"},
                 {"name": "irm", "type": "address"},
                 {"name": "lltv", "type": "uint256"}]},
    {"name": "position", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "id", "type": "bytes32"}, {"name": "user", "type": "address"}],
     "outputs": [{"name": "supplyShares", "type": "uint256"},
                 {"name": "borrowShares", "type": "uint128"},
                 {"name": "collateral", "type": "uint128"}]},
    {"name": "market", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "id", "type": "bytes32"}],
     "outputs": [{"name": "totalSupplyAssets", "type": "uint128"},
                 {"name": "totalSupplyShares", "type": "uint128"},
                 {"name": "totalBorrowAssets", "type": "uint128"},
                 {"name": "totalBorrowShares", "type": "uint128"},
                 {"name": "lastUpdate", "type": "uint128"},
                 {"name": "fee", "type": "uint128"}]},
]
ORACLE_ABI = [
    {"name": "price", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]


@dataclass
class SimResult:
    profitable: bool
    reverted: bool
    net_usd: float
    repaid_usd: float
    seized_usd: float
    gas_usd: float
    note: str = ""


@dataclass
class MarketContext:
    """Per-market state — read once, reused for every borrower in that market."""
    oracle: str
    price: int
    lltv_wad: int
    total_borrow_assets: int     # interest-stale (see module docstring)
    total_borrow_shares: int


@dataclass
class HealthReport:
    liquidatable: bool
    hf: float
    collateral: int          # collateral token wei
    borrowed_assets: int     # loan token wei
    max_borrow: int          # loan token wei
    lltv_wad: int


# ---- pure integer math (unit-tested; mirrors the contract exactly) ----

def _mul_div_up(x: int, y: int, d: int) -> int:
    return (x * y + (d - 1)) // d


def to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    """SharesMathLib.toAssetsUp with virtual shares/assets."""
    return _mul_div_up(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def max_borrow(collateral: int, price: int, lltv_wad: int) -> int:
    """wMulDown(mulDivDown(collateral, price, 1e36), lltv) — the contract's health cap."""
    mb = (collateral * price) // ORACLE_PRICE_SCALE   # mulDivDown
    return (mb * lltv_wad) // WAD                       # wMulDown


def health_factor(collateral: int, price: int, lltv_wad: int, borrowed_assets: int) -> float:
    if borrowed_assets <= 0:
        return float("inf")
    return max_borrow(collateral, price, lltv_wad) / borrowed_assets


def is_liquidatable(collateral: int, price: int, lltv_wad: int, borrowed_assets: int) -> bool:
    if borrowed_assets <= 0:
        return False
    return max_borrow(collateral, price, lltv_wad) < borrowed_assets


def health_from(ctx: MarketContext, borrow_shares: int, collateral: int) -> HealthReport:
    """Pure: build a HealthReport from a market context + one position's raw numbers."""
    borrowed = to_assets_up(borrow_shares, ctx.total_borrow_assets, ctx.total_borrow_shares)
    mb = max_borrow(collateral, ctx.price, ctx.lltv_wad)
    return HealthReport(
        liquidatable=(borrowed > 0 and mb < borrowed),
        hf=(mb / borrowed if borrowed > 0 else float("inf")),
        collateral=collateral, borrowed_assets=borrowed, max_borrow=mb, lltv_wad=ctx.lltv_wad,
    )


def estimate(hr: HealthReport, debt_usd: float, slippage: float, gas_usd: float,
             tip_usd: float = 0.0, min_profit_usd: float = 0.0) -> SimResult:
    """Pure PnL gate: healthy -> not profitable; else analytic net via strategy.pnl.
    LIF from the on-chain lltv. Real tx simulation -> simulate_tx (execute phase)."""
    if not hr.liquidatable:
        return SimResult(False, False, 0.0, 0.0, 0.0, gas_usd, f"healthy on-chain HF={hr.hf:.4f}")
    bonus = lif_from_lltv(hr.lltv_wad / WAD) - 1.0
    net = net_profit(PnlInputs(debt_usd=debt_usd, bonus=bonus, slippage=slippage,
                               gas_usd=gas_usd, tip_usd=tip_usd))
    return SimResult(net >= min_profit_usd, False, net, debt_usd, debt_usd * (1.0 + bonus),
                     gas_usd, f"on-chain HF={hr.hf:.4f}, est net (analytic; tx-sim=execute phase)")


# ---- on-chain reads (I/O — dry-run on the VPS; sandbox can't reach Base RPC) ----

def _checksum(addr: str) -> str:
    from web3 import Web3
    return Web3.to_checksum_address(addr)


def read_market_context(rpc: BaseRpc, morpho_addr: str, market_id: str) -> MarketContext:
    """3 eth_calls, ONCE per market: idToMarketParams + market + oracle.price."""
    mid = rpc.to_bytes32(market_id)
    morpho = rpc.contract(morpho_addr, MORPHO_READ_ABI)
    _, _, oracle, _, lltv_wad = morpho.functions.idToMarketParams(mid).call()
    m = morpho.functions.market(mid).call()
    price = rpc.contract(oracle, ORACLE_ABI).functions.price().call()
    return MarketContext(oracle=oracle, price=price, lltv_wad=lltv_wad,
                         total_borrow_assets=m[2], total_borrow_shares=m[3])


def read_position(rpc: BaseRpc, morpho_addr: str, market_id: str, borrower: str):
    """1 eth_call per borrower -> (borrow_shares, collateral). Batch via Multicall3 at scale."""
    mid = rpc.to_bytes32(market_id)
    morpho = rpc.contract(morpho_addr, MORPHO_READ_ABI)
    _, borrow_shares, collateral = morpho.functions.position(mid, _checksum(borrower)).call()
    return borrow_shares, collateral


def read_health(rpc: BaseRpc, morpho_addr: str, market_id: str, borrower: str) -> HealthReport:
    """Single-position convenience (4 calls). Loop should reuse read_market_context."""
    ctx = read_market_context(rpc, morpho_addr, market_id)
    bs, col = read_position(rpc, morpho_addr, market_id, borrower)
    return health_from(ctx, bs, col)


def assess_position(rpc: BaseRpc, morpho_addr: str, market_id: str, borrower: str,
                    debt_usd: float, slippage: float, gas_usd: float,
                    tip_usd: float = 0.0, min_profit_usd: float = 0.0) -> SimResult:
    """Single-shot convenience: read_health + estimate. Loop uses the granular pieces."""
    return estimate(read_health(rpc, morpho_addr, market_id, borrower),
                    debt_usd, slippage, gas_usd, tip_usd, min_profit_usd)


def refresh_market_params(rpc: BaseRpc, morpho_addr: str, market_ids: list, ctx_cache: dict) -> None:
    """Cache IMMUTABLE per-market params (oracle, lltv_wad) for any uncached markets via one
    aggregate3 of idToMarketParams. ctx_cache: {market_id_hex: (oracle_addr, lltv_wad)}."""
    missing = [mid for mid in market_ids if mid not in ctx_cache]
    if not missing:
        return
    calls = [(morpho_addr, encode_id_to_market_params_call(rpc.to_bytes32(mid))) for mid in missing]
    for mid, (ok, data) in zip(missing, aggregate3(rpc, calls)):
        if ok and data:
            _loan, _coll, oracle, _irm, lltv_wad = decode_id_to_market_params(data)
            ctx_cache[mid] = (oracle, lltv_wad)


def assess_candidates_batched(rpc: BaseRpc, morpho_addr: str, groups: dict, ctx_cache: dict) -> list:
    """groups: {market_id_hex: [borrower_addr]}. ONE aggregate3 reads market()+oracle.price()
    for every market + position() for every candidate, across ALL markets at once. Immutable
    oracle/lltv come from ctx_cache (refreshed first). -> [(market_id, borrower, HealthReport)].
    This is what makes the scan ~1-2 eth_calls/cycle regardless of market/position count."""
    refresh_market_params(rpc, morpho_addr, list(groups.keys()), ctx_cache)
    mids = [mid for mid in groups if mid in ctx_cache]
    if not mids:
        return []
    calls = [(morpho_addr, encode_market_call(rpc.to_bytes32(mid))) for mid in mids]
    calls += [(ctx_cache[mid][0], encode_price_call()) for mid in mids]   # price() on the oracle addr
    pos_index = []
    for mid in mids:
        b32 = rpc.to_bytes32(mid)
        for b in groups[mid]:
            calls.append((morpho_addr, encode_position_call(b32, b)))
            pos_index.append((mid, b))
    res = aggregate3(rpc, calls)
    n = len(mids)
    ctx_by = {}
    for i, mid in enumerate(mids):
        ok_m, d_m = res[i]
        ok_p, d_p = res[n + i]
        if ok_m and d_m and ok_p and d_p:
            m = decode_market(d_m)
            oracle, lltv_wad = ctx_cache[mid]
            ctx_by[mid] = MarketContext(oracle=oracle, price=decode_price(d_p), lltv_wad=lltv_wad,
                                        total_borrow_assets=m[2], total_borrow_shares=m[3])
    out = []
    for (mid, b), (ok, data) in zip(pos_index, res[2 * n:]):
        ctx = ctx_by.get(mid)
        if ctx is not None and ok and data:
            bs, col = decode_position(data)
            out.append((mid, b, health_from(ctx, bs, col)))
    return out


def simulate_tx(*args, **kwargs):
    """Execute-phase: eth_call the real liquidate+flashloan+swap bundle against latest
    state, decode seized/repaid, price to USD, subtract gas+tip; reverting => skip.
    Needs the deployed flash-loan liquidator contract (not built in monitor)."""
    raise NotImplementedError("execute phase: real-tx simulation needs the liquidator contract")
