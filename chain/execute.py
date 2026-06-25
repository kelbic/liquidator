"""Execute layer (Phase 2). Builds the on-chain liquidation bundle and gates it with a live
eth_call simulation before any send (no send happens in this module). Aggregator = KyberSwap
(keyless) for the collateral->loan swap; the contract validates the OUTCOME (minProfit gate),
so the route itself is untrusted-but-checked.

stdlib HTTP (urllib) like morpho.py — runs on `python3`; only the eth_call path needs web3 (venv).
"""
from __future__ import annotations
import json
import threading
import urllib.request
from urllib.parse import urlencode

_SEND_LOCK = threading.Lock()   # serialize dispatch nonce-fetch+send across the block loop and hot path

ORACLE_PRICE_SCALE = 10 ** 36
WAD = 10 ** 18
KYBER_HOST = "https://aggregator-api.kyberswap.com"
_CLIENT_ID = "kelbic-liquidator"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"   # KyberSwap CF blocks default urllib UA


def _get(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "x-client-id": _CLIENT_ID, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-client-id": _CLIENT_ID, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def kyber_swap(token_in: str, token_out: str, amount_in: int, sender: str, recipient: str,
               slippage_bps: int = 100, chain: str = "base", timeout: int = 15) -> dict:
    """KyberSwap aggregator (keyless). Two steps: GET /routes -> POST /route/build.
    Returns {router, calldata, amount_in, amount_out, min_out}. The encoded swap pulls token_in
    from `sender` (our contract, which approved the router) and sends token_out to `recipient`
    (our contract). slippage_bps caps the route's own min-out (10000 = 100%); we ALSO gate on
    minProfit in the contract, so this is a secondary guard."""
    api = f"{KYBER_HOST}/{chain}/api/v1"
    q = urlencode({"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(int(amount_in))})
    routes = _get(f"{api}/routes?{q}", timeout)
    if routes.get("code") not in (0, None):
        raise RuntimeError(f"kyber routes error: {routes.get('message')}")
    rd = routes["data"]
    built = _post(f"{api}/route/build",
                  {"routeSummary": rd["routeSummary"], "sender": sender, "recipient": recipient,
                   "slippageTolerance": int(slippage_bps)}, timeout)
    if built.get("code") not in (0, None):
        raise RuntimeError(f"kyber build error: {built.get('message')}")
    bd = built["data"]
    return {
        "router": rd["routerAddress"],
        "calldata": bd["data"],
        "amount_in": int(bd["amountIn"]),
        "amount_out": int(bd["amountOut"]),
        "min_out": int(bd["amountOut"]) * (10000 - int(slippage_bps)) // 10000,  # kyber bakes this into calldata
    }


# ---- UniV3 direct-swap (variant A): precomputed multi-hop coll->WETH->USDC, NO in-race quote ----
UNIV3_SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"  # Uniswap SwapRouter02 (Base)
_UNIV3_WETH = "0x4200000000000000000000000000000000000006"
_UNIV3_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_UNIV3_WETH_USDC_FEE = 500
_UNIV3_EXACT_INPUT_SEL = "b858183f"  # exactInput((bytes,address,uint256,uint256)) verified cast sig
UNIV3_PATHS = {  # collateral(lower) -> fee tier of coll/WETH pool (verified factory.getPool)
    "0xcb585250f852c6c6bf90434ab21a00f02833a4af": 3000,   # cbXRP/WETH
    "0xcbd06e5a2b0c65597161de254aa074e489deb510": 3000,   # cbDOGE/WETH
    "0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c": 3000,   # cbADA/WETH
}


def univ3_swap(token_in, token_out, amount_in, sender, recipient, *, repaid_assets, slippage_bps=100):
    """Drop-in for kyber_swap on cbXRP/cbDOGE/cbADA. Precomputed UniV3 multi-hop coll->WETH->USDC
    (always-multi, as winners do), NO in-race quote. amountOutMinimum=repaid_assets (debt floor);
    profit protected by preconf sim + contract minProfit. Returns same {router, calldata} as
    kyber_swap, or None if token_in has no UniV3 path (caller falls back to kyber_swap)."""
    from eth_abi import encode
    coll = token_in.lower()
    if coll not in UNIV3_PATHS:
        return None
    fee = UNIV3_PATHS[coll]
    path = (bytes.fromhex(token_in[2:]) + int(fee).to_bytes(3, "big")
            + bytes.fromhex(_UNIV3_WETH[2:]) + int(_UNIV3_WETH_USDC_FEE).to_bytes(3, "big")
            + bytes.fromhex(_UNIV3_USDC[2:]))
    params = encode(["(bytes,address,uint256,uint256)"],
                    [(path, recipient, int(amount_in), int(repaid_assets))])
    return {"router": UNIV3_SWAP_ROUTER, "calldata": "0x" + _UNIV3_EXACT_INPUT_SEL + params.hex()}


def expected_seized(repaid_assets: int, lif: float, price: int) -> int:
    """Collateral (wei) Morpho seizes for repaying `repaid_assets` (loan wei), per _liquidate:
    seized = mulDivDown(wMulDown(repaidAssets, LIF), ORACLE_PRICE_SCALE, price)."""
    lif_wad = int(round(lif * WAD))
    incentivized = repaid_assets * lif_wad // WAD
    return incentivized * ORACLE_PRICE_SCALE // price


# --- liquidate() bundle + pre-send eth_call gate ---

_LIQUIDATE_SIG = ("liquidate((address,address,address,address,uint256),"
                  "address,uint256,address,bytes,uint256)")


def encode_liquidate(mp: dict, borrower: str, repaid_shares: int, swap_target: str,
                     swap_calldata: str, min_profit: int) -> str:
    """ABI-encode Liquidator.liquidate(mp, borrower, repaidShares, swapTarget, swapData, minProfit)."""
    from eth_abi import encode
    from eth_utils import keccak
    selector = keccak(text=_LIQUIDATE_SIG)[:4]
    cd = bytes.fromhex(swap_calldata[2:] if swap_calldata.startswith("0x") else swap_calldata)
    args = encode(
        ["(address,address,address,address,uint256)", "address", "uint256", "address", "bytes", "uint256"],
        [(mp["loanToken"], mp["collateralToken"], mp["oracle"], mp["irm"], int(mp["lltv"])),
         borrower, int(repaid_shares), swap_target, cd, int(min_profit)],
    )
    return "0x" + (selector + args).hex()


def simulate_tx(rpc, liquidator_addr: str, from_addr: str, calldata: str,
                state_override: dict | None = None, block: str = "latest") -> dict:
    """eth_call the liquidate bundle (no send). {ok, profit, error}. `block` defaults to 'latest' (the
    confirmed-state gate the block loop uses); the hot path passes a preconf RPC + block='pending' to
    gate on the pre-confirmed price, where a just-transmitted price has already moved the position."""
    tx = {"to": liquidator_addr, "from": from_addr, "data": calldata}
    try:
        ret = bytes(rpc.eth_call(tx, block, state_override))
        profit = int.from_bytes(ret[:32], "big") if len(ret) >= 32 else 0
        return {"ok": True, "profit": profit, "error": None}
    except Exception as e:
        return {"ok": False, "profit": 0, "error": str(e)[:240]}


# --- sign + submit (EIP-1559 on Base) ---

def send_tx(rpc, key: str, tx: dict, wait: bool = True, timeout: int = 120,
            min_tip_wei: int = 0) -> dict:
    """Sign `tx` with `key` and submit to Base. Fills from/chainId/nonce/fees/gas if absent."""
    w3 = rpc._web3()
    acct = w3.eth.account.from_key(key)
    t = dict(tx)
    t.setdefault("from", acct.address)
    t.setdefault("chainId", rpc.chain_id)
    if "nonce" not in t:
        t["nonce"] = w3.eth.get_transaction_count(acct.address, "pending")
    if "maxFeePerGas" not in t and "gasPrice" not in t:
        base = w3.eth.get_block("latest").get("baseFeePerGas") or 0
        tip = max(int(w3.eth.max_priority_fee), int(min_tip_wei))
        t["maxPriorityFeePerGas"] = tip
        t["maxFeePerGas"] = int(base) * 2 + tip
    if "gas" not in t:
        t["gas"] = int(w3.eth.estimate_gas(t) * 12 // 10)
    signed = acct.sign_transaction(t)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    out = {"hash": h.hex(), "status": None, "gas_used": None, "receipt": None}
    if wait:
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
        out["receipt"] = rcpt; out["status"] = rcpt.get("status"); out["gas_used"] = rcpt.get("gasUsed")
    return out


# --- full execute action for one candidate (read fresh -> gate -> send) ---

_MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"


def prepare_liquidation(rpc, cfg, market_id: str, borrower: str, debt_usd: float,
                        debt_assets: int, slippage_bps: int = 100) -> dict:
    """Read fresh on-chain state, size the seize, fetch the swap, simulate, and gate on the NET
    floor — everything UP TO sending. Returns {ok: True, calldata, net_usd, profit_usd, cost_usd}
    ready to sign+send, or {ok: False, reason, net_usd?}. No send, so many candidates can be
    prepared and then dispatched together (parallel). Catches its own errors."""
    try:
        from chain.multicall import (aggregate3, encode_id_to_market_params_call,
            decode_id_to_market_params, encode_market_call, decode_market,
            encode_price_call, decode_price, encode_position_call, decode_position)
        from chain.simulate import to_assets_up
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = w3.eth.account.from_key(cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        r1 = aggregate3(rpc, [(_MORPHO_BLUE, encode_id_to_market_params_call(mid)),
                              (_MORPHO_BLUE, encode_market_call(mid)),
                              (_MORPHO_BLUE, encode_position_call(mid, borrower))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
        m = decode_market(r1[1][1]); tba, tbs = m[2], m[3]
        borrow_shares, _ = decode_position(r1[2][1])
        if borrow_shares == 0:
            return {"ok": False, "reason": "no debt (cleared)"}
        price = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])

        repaid_shares = int(borrow_shares)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
        if seized == 0:
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=slippage_bps)

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0)
        sim = simulate_tx(rpc, liq, bot, cd0)
        if not sim["ok"]:
            return {"ok": False, "reason": f"sim revert: {sim['error']}"}
        profit_wei = sim["profit"]
        profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
        cost_usd = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
        net_usd = profit_usd - cost_usd
        if net_usd < cfg.min_profit_usd:
            return {"ok": False, "net_usd": net_usd,
                    "reason": f"net ${net_usd:.2f} (profit ${profit_usd:.2f} - cost ${cost_usd:.2f}) < min ${cfg.min_profit_usd:.2f}"}

        min_profit_final = profit_wei * 95 // 100   # revert if realized < 95% of simulated
        cd1 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], min_profit_final)
        return {"ok": True, "calldata": cd1, "net_usd": net_usd,
                "profit_usd": profit_usd, "cost_usd": cost_usd}
    except Exception as e:
        return {"ok": False, "reason": f"error: {type(e).__name__}: {str(e)[:120]}"}


def try_liquidate(rpc, cfg, market_id: str, borrower: str, debt_usd: float,
                  debt_assets: int, log=None, slippage_bps: int = 100) -> dict:
    """Prepare + send ONE liquidation (blocking) — the sequential path. Returns
    {sent, reason?/hash?/status?/net_usd?/profit_usd?}. The batch path prepares many then sends
    non-blocking with sequential nonces; this wrapper keeps the single-shot behavior identical."""
    prep = prepare_liquidation(rpc, cfg, market_id, borrower, debt_usd, debt_assets, slippage_bps)
    if not prep["ok"]:
        out = {"sent": False, "reason": prep["reason"]}
        if "net_usd" in prep:
            out["net_usd"] = prep["net_usd"]
        return out
    try:
        res = send_tx(rpc, cfg.wallet_key, {"to": cfg.liquidator_address, "data": prep["calldata"]},
                      min_tip_wei=int(cfg.tip_gwei * 1e9))
    except Exception as e:
        return {"sent": False, "reason": f"send error: {type(e).__name__}: {str(e)[:120]}"}
    if log:
        log.info("LIQUIDATE sent %s/%s tx=%s status=%s net~$%.2f (profit $%.2f - cost $%.2f)",
                 market_id[:10], borrower[:10], res["hash"], res["status"],
                 prep["net_usd"], prep["profit_usd"], prep["cost_usd"])
    return {"sent": True, "hash": res["hash"], "status": res["status"], "gas_used": res["gas_used"],
            "profit_usd": prep["profit_usd"], "net_usd": prep["net_usd"]}


def realized_econ(profit_usd: float, gas_used: int, eff_gas_price: int, status, eth_price_usd: float):
    """Realized (net_usd, gas_usd) from a mined receipt. A revert still burns gas, so gas is ALWAYS a
    cost; net = profit - gas on success (status==1), else just -gas. Pure -> the kill-switch math is
    unit-testable: the loss gate must see a revert as a LOSS, not the simulated expected profit."""
    gas_usd = gas_used * eff_gas_price * eth_price_usd / 1e18
    net_usd = (profit_usd - gas_usd) if status == 1 else -gas_usd
    return net_usd, gas_usd


def _revert_bucket(err_str: str) -> str:
    """Map a revert error string to a short diagnostic bucket. Pure -> unit-testable. The buckets we
    care about: 'position healthy' (lost the race / position recovered -> latency lever) vs structural
    (no-route / slippage / Morpho param error -> a different fix). Falls back to the raw msg/selector."""
    import re
    low = err_str.lower()
    if "healthy" in low:
        return "position healthy (lost race / recovered)"
    if "0x81ceff30" in low:
        return "Morpho 0x81ceff30"
    m = re.search(r"reverted:?\s*(.+)", err_str)        # Error(string): 'execution reverted: <msg>'
    if m:
        return m.group(1).strip()[:80]
    sel = re.search(r"0x[0-9a-fA-F]{8}", err_str)        # custom-error selector
    if sel:
        return "selector " + sel.group(0)
    return err_str[:80]


def _revert_reason(rpc, tx_hash, block_number):
    """Best-effort revert reason for a mined-but-reverted tx: replay it as eth_call at the mined block
    and surface the revert string/selector via _revert_bucket. DIAGNOSTIC ONLY (block end-state may
    differ from what the tx saw mid-block) -> use as a bucket, not proof. Two RPC calls; reverts are
    infrequent so the cost is negligible. Never raises -> failure to diagnose returns a tag, not an error."""
    try:
        w3 = rpc._web3()
        tx = w3.eth.get_transaction(tx_hash)
        rpc.eth_call({"to": tx["to"], "from": tx["from"], "data": tx["input"]},
                     block=block_number if block_number is not None else "latest")
        return "clean-on-replay (state moved / lost race)"
    except Exception as e:
        return _revert_bucket(str(e))


def dispatch_liquidations(rpc, cfg, prepared: list, log=None, max_inflight: int = 3,
                          send_fn=None, wait_receipts: bool = True, timeout: int = 120) -> list:
    """Send up to `max_inflight` already-prepared liquidations NON-BLOCKING with SEQUENTIAL nonces,
    then collect receipts. This is the parallel path: in a volatility cascade several positions are
    liquidatable at once, and sending them one-at-a-time (blocking on each receipt) would let a
    competitor take the rest.

    `prepared`: list of (market_id, borrower, prep) where prep is a prepare_liquidation() result
    with ok=True (already passed simulate + the net floor). Returns a result dict per attempted
    send: {market_id, borrower, sent, hash?/status?/gas_used?/net_usd/reason?}.

    Nonce safety: we fetch the base nonce ONCE and assign base+0, base+1, ... so concurrent sends
    don't race on get_transaction_count (which would hand them all the same nonce). Only candidates
    that passed simulate get a nonce (definitely-sending). If a send THROWS mid-batch we STOP (a
    missing nonce would strand every later tx behind the gap); the next block re-derives the base
    nonce and retries. Reverts do NOT break the chain — a reverted tx still consumes its nonce.
    `send_fn` is injectable for tests."""
    send = send_fn or send_tx
    results: list = []
    if not prepared:
        return results
    w3 = rpc._web3()
    bot = w3.eth.account.from_key(cfg.wallet_key).address
    with _SEND_LOCK:                                         # serialize nonce-fetch+send across senders (block loop + hot path)
        base_nonce = int(w3.eth.get_transaction_count(bot, "pending"))
        tip = int(cfg.tip_gwei * 1e9)
        batch = prepared[:max_inflight]

        sent = []
        for i, (mid, borrower, prep) in enumerate(batch):
            try:
                res = send(rpc, cfg.wallet_key,
                           {"to": cfg.liquidator_address, "data": prep["calldata"], "nonce": base_nonce + i},
                           wait=False, min_tip_wei=tip)
                sent.append((mid, borrower, prep, res["hash"]))
                if log:
                    log.info("dispatch: sent %s/%s nonce=%d tx=%s net~$%.2f",
                             mid[:10], borrower[:10], base_nonce + i, res["hash"], prep["net_usd"])
            except Exception as e:
                if log:
                    log.warning("dispatch: send FAILED at nonce %d (%s/%s): %s — stopping batch to avoid gap",
                                base_nonce + i, mid[:10], borrower[:10], type(e).__name__)
                results.append({"market_id": mid, "borrower": borrower, "sent": False,
                                "reason": f"send error: {type(e).__name__}: {str(e)[:80]}",
                                "net_usd": prep["net_usd"]})
                break   # nonce gap -> later txs would be stuck; next block re-derives the base nonce

    if not wait_receipts:
        for mid, borrower, prep, h in sent:
            results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                            "status": None, "gas_used": None, "net_usd": prep["net_usd"]})
        return results

    for mid, borrower, prep, h in sent:
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
            status = rcpt.get("status")
            net_usd, gas_usd = realized_econ(prep["profit_usd"], int(rcpt.get("gasUsed") or 0),
                                             int(rcpt.get("effectiveGasPrice") or 0), status, cfg.eth_price_usd)
            row = {"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                   "status": status, "gas_used": rcpt.get("gasUsed"),
                   "net_usd": net_usd, "gas_usd": gas_usd}
            if status != 1:                                  # diagnose the revert (lost race vs structural)
                reason = _revert_reason(rpc, h, rcpt.get("blockNumber"))
                row["revert_reason"] = reason
                if log:
                    log.warning("liquidate revert %s/%s reason=%s tx=%s",
                                mid[:10], borrower[:10], reason, h)
            results.append(row)
        except Exception as e:
            # outcome unknown -> conservatively book estimated cost as a LOSS, never a phantom profit
            results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                            "status": None, "gas_used": None,
                            "net_usd": -prep["cost_usd"], "gas_usd": prep["cost_usd"],
                            "reason": f"receipt timeout: {type(e).__name__}"})
    return results
