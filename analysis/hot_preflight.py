"""Hot-path PREFLIGHT self-test (read-only, key-free, NO send). Validates the ONE load-bearing unknown
before any live fire: does eth_call of our full liquidate bundle (Morpho flashloan -> swap -> liquidate)
EXECUTE against the PRECONF-PENDING state on the preconf RPC? It runs the ENTIRE prepare pipeline on the
current largest candidate — reads -> size on the preconf price -> KyberSwap quote -> encode -> simulate
against BOTH preconf-pending and latest — and reports. NO transaction is sent.

Reading the result:
  * preconf-pending sim returns a profit, OR reverts cleanly with a Morpho reason (e.g. 'position is
    healthy' when the candidate isn't liquidatable yet) -> the MECHANISM WORKS; the hot path is viable
    (a real flip would return profit through the same call). Proceed to step B (simulate_tx block param).
  * preconf-pending sim errors at the RPC level (method/tag unsupported, bundle won't execute) -> the
    preconf RPC can't simulate our bundle at pending; the hot path needs a STATE-OVERRIDE on the normal
    RPC instead. Redesign before any send.
We learn this in seconds, for $0, before touching the armed send path.
    venv/bin/python -m analysis.hot_preflight
"""
from __future__ import annotations
import sys
import time


def sim_at(rpc, liq, frm, cd, block):
    """eth_call our liquidate bundle at `block` (no send). {ok, profit, error}. Mirrors execute.simulate_tx
    but lets us choose the RPC (preconf) and block tag (pending)."""
    try:
        ret = bytes(rpc.eth_call({"to": liq, "from": frm, "data": cd}, block))
        return {"ok": True, "profit": int.from_bytes(ret[:32], "big") if len(ret) >= 32 else 0, "error": None}
    except Exception as e:
        return {"ok": False, "profit": 0, "error": str(e)[:200]}


def main():
    sys.path.insert(0, ".")
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import positions_at_risk, MORPHO_BLUE_ADDRESS
    from chain.simulate import ORACLE_ABI, MarketContext, health_from, to_assets_up
    from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
        encode_market_call, decode_market, encode_price_call, decode_price,
        encode_position_call, decode_position)
    from chain.execute import kyber_swap, encode_liquidate, expected_seized
    from strategy.pnl import lif_from_lltv
    from strategy.scanner import load_covered_markets

    PRECONF_RPC = "https://mainnet-preconf.base.org"
    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    if not cfg.liquidator_address or not cfg.wallet_address:
        sys.exit("LIQUIDATOR_ADDRESS / WALLET_ADDRESS required (sim from=owner).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    preconf_rpc = BaseRpc(PRECONF_RPC, cfg.chain_id)
    liq, frm = cfg.liquidator_address, cfg.wallet_address

    def log(m):
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    markets = load_covered_markets(cfg.covered_markets_path)
    cands = [c for c in positions_at_risk(markets, hf_ceiling=1.10) if c.debt_assets > 0]
    cands.sort(key=lambda c: -c.debt_usd)
    if not cands:
        sys.exit("no candidates with debt right now — rerun later.")
    c = cands[0]
    mid, borrower = c.market_id, c.borrower
    log(f"PREFLIGHT self-test — target {mid[:10]}.. borrower {borrower[:10]}.. debt~${c.debt_usd:,.0f}")
    log("READ-ONLY, key-free (sim from=WALLET_ADDRESS), NO send")

    b32 = rpc.to_bytes32(mid)
    r1 = aggregate3(rpc, [(MORPHO_BLUE_ADDRESS, encode_id_to_market_params_call(b32)),
                          (MORPHO_BLUE_ADDRESS, encode_market_call(b32)),
                          (MORPHO_BLUE_ADDRESS, encode_position_call(b32, borrower))])
    loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
    m = decode_market(r1[1][1]); tba, tbs = m[2], m[3]
    bs, col = decode_position(r1[2][1])
    if bs == 0:
        sys.exit("candidate has no debt (cleared) — rerun.")

    price_latest = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
    price_preconf = None
    try:
        price_preconf = int(preconf_rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending"))
    except Exception as e:
        log(f"!! preconf price('pending') FAILED: {type(e).__name__}: {str(e)[:120]}")

    def hf_at(price):
        return health_from(MarketContext(oracle=oracle, price=int(price), lltv_wad=lltv_wad,
                           total_borrow_assets=tba, total_borrow_shares=tbs), bs, col)

    hr_latest = hf_at(price_latest)
    log(f"price latest ={price_latest}  HF={hr_latest.hf:.4f}  liq={hr_latest.liquidatable}")
    if price_preconf is not None:
        hr_pre = hf_at(price_preconf)
        d = "SAME" if price_preconf == price_latest else f"DIFF ({(price_preconf/price_latest-1)*100:+.3f}%)"
        log(f"price preconf={price_preconf}  HF={hr_pre.hf:.4f}  liq={hr_pre.liquidatable}  [{d} vs latest]")

    # size on the preconf price (what the hot path would do); fall back to latest if preconf read failed
    price_for_size = price_preconf if price_preconf is not None else price_latest
    repaid_assets = to_assets_up(bs, tba, tbs)
    seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 1e18), price_for_size)
    if seized == 0:
        sys.exit("seized=0 — cannot build bundle.")
    mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}

    t = time.perf_counter()
    try:
        swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=100)
    except Exception as e:
        sys.exit(f"!! KyberSwap quote FAILED: {type(e).__name__}: {str(e)[:160]}")
    log(f"KyberSwap quote OK in {(time.perf_counter()-t)*1000:.0f}ms  (seized={seized} -> out={swap['amount_out']})")
    cd = encode_liquidate(mp, borrower, int(bs), swap["router"], swap["calldata"], 0)

    log("---- SIMULATE the full bundle (NO send) ----")
    s_pre = sim_at(preconf_rpc, liq, frm, cd, "pending")
    s_lat = sim_at(rpc, liq, frm, cd, "latest")
    log(f"  preconf-pending: ok={s_pre['ok']} profit_wei={s_pre['profit']} err={s_pre['error']}")
    log(f"  latest        : ok={s_lat['ok']} profit_wei={s_lat['profit']} err={s_lat['error']}")

    if s_pre["profit"] > 0:
        profit_usd = s_pre["profit"] * c.debt_usd / c.debt_assets if c.debt_assets else 0.0
        cost = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
        log(f"  preconf net est: profit ${profit_usd:,.2f} - cost ${cost:,.2f} = ${profit_usd-cost:,.2f} "
            f"(floor ${cfg.min_profit_usd:.0f})")

    log("---- VERDICT ----")
    healthy_revert = (not s_pre["ok"]) and s_pre["error"] and ("healthy" in s_pre["error"].lower() or "0x" in (s_pre["error"] or ""))
    if s_pre["profit"] > 0:
        log("GREEN: preconf-pending eth_call EXECUTED our bundle and returned profit -> hot path mechanism VIABLE. Proceed to step B.")
    elif s_pre["ok"] or healthy_revert:
        log("GREEN (mechanism): preconf-pending eth_call EXECUTED the bundle (candidate not liquidatable now -> clean revert).")
        log("  A real flip returns profit through the SAME call. Mechanism works -> proceed to step B.")
    else:
        log("RED: preconf-pending eth_call did NOT execute our bundle (RPC-level failure). Hot path needs a")
        log("  STATE-OVERRIDE on the normal RPC instead of preconf-pending sim. Redesign before any send.")


if __name__ == "__main__":
    main()
