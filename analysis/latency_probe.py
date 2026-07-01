"""Read-only: measure OUR post-detection pipeline latency and express it in TX-POSITIONS, to answer
retrospectively — would we have landed BEFORE the actual winner? At equal detection start (preconf
price is public, seen by all in the same ~200ms sub-block), the race is execution latency. A winner's
`gap` (tx-positions between the oracle transmit and their liquidate, from gap_profile) is THEIR
demonstrated latency in tx-units; ours is L_seconds * throughput. If G_ours < gap we'd plausibly be
included first at equal fee -> reachable.

UPPER bound on share, by construction: (a) a winner's shown gap is how late they WERE, not their floor
— under our pressure they may compress it; (b) assumes equal fee priority. But it cheaply gates the
NECESSARY condition (are we even fast enough) with NO waiting, NO volatility, NO send.

Times the REAL prepare path on a current $-large candidate, N runs -> median. Key-free: uses
WALLET_ADDRESS for the sim `from` (no signing, no send; a sim revert still times the eth_call, so the
measurement holds). Compares G_ours to per-market med_gap from gap_profile (30d).
    RUNS=7   python -m analysis.latency_probe
"""
from __future__ import annotations
import os
import sys
import time
import statistics

# med_gap (tx-positions) per market from gap_profile (30d). The bar G_ours must beat to land before
# the MEDIAN winner on that market. Liquid targets only (where we'd actually race).
MED_GAP = {"cbXRP/USDC": 40, "SOL/USDC": 271, "cbDOGE/USDC": 46, "cbADA/USDC": 7,
           "cbLTC/USDC": 114, "cbBTC/EURC": 160, "cbETH/USDC": 20}
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
DISPATCH_RTT_S = 0.05   # measured ~50ms send RTT (STATE); sign overhead negligible
BLOCK_TIME_S = 2.0


def _time_once(rpc, cfg, mid_hex, borrower):
    """One timed pass of the real prepare path. Returns (t_reads, t_quote, t_sim) or None.
    Key-free: sim `from` = WALLET_ADDRESS. No send."""
    from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
        encode_market_call, decode_market, encode_price_call, decode_price,
        encode_position_call, decode_position)
    from chain.simulate import to_assets_up
    from chain.execute import expected_seized, kyber_swap, encode_liquidate, simulate_tx
    from strategy.pnl import lif_from_lltv

    liq = cfg.liquidator_address
    mid = rpc.to_bytes32(mid_hex)
    t0 = time.perf_counter()
    r1 = aggregate3(rpc, [(MORPHO, encode_id_to_market_params_call(mid)),
                          (MORPHO, encode_market_call(mid)),
                          (MORPHO, encode_position_call(mid, borrower))])
    loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
    m = decode_market(r1[1][1]); tba, tbs = m[2], m[3]
    borrow_shares, _ = decode_position(r1[2][1])
    price = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
    t_reads = time.perf_counter() - t0
    if borrow_shares == 0:
        return None
    repaid_shares = int(borrow_shares)
    repaid_assets = to_assets_up(repaid_shares, tba, tbs)
    seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 1e18), price)
    if seized == 0:
        return None
    mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
    t1 = time.perf_counter()
    swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=100)
    t_quote = time.perf_counter() - t1
    cd = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0)
    t2 = time.perf_counter()
    simulate_tx(rpc, liq, cfg.wallet_address or "0x0000000000000000000000000000000000000000", cd)
    t_sim = time.perf_counter() - t2
    return t_reads, t_quote, t_sim


def main():
    sys.path.insert(0, ".")
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import positions_at_risk
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    if not cfg.liquidator_address:
        sys.exit("LIQUIDATOR_ADDRESS unset — needed for the swap/sim path.")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    runs = int(os.environ.get("RUNS", "7"))

    def log(m):
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    markets = load_covered_markets(cfg.covered_markets_path)
    cands = [c for c in positions_at_risk(markets, hf_ceiling=1.10) if c.debt_assets > 0]
    cands.sort(key=lambda c: -c.debt_usd)
    if not cands:
        sys.exit("no candidates with debt right now (calm) — rerun later.")
    tgt = cands[0]
    log(f"latency-probe — target {tgt.market_id[:10]}.. borrower {tgt.borrower[:10]}.. "
        f"debt~${tgt.debt_usd:,.0f} | {runs} runs, READ-ONLY/key-free")

    reads, quotes, sims, totals = [], [], [], []
    for i in range(runs):
        try:
            r = _time_once(rpc, cfg, tgt.market_id, tgt.borrower)
        except Exception as e:
            log(f"  run {i+1}: skipped ({type(e).__name__}: {str(e)[:60]})")
            continue
        if r is None:
            log(f"  run {i+1}: skipped (no debt/seized)")
            continue
        tr, tq, ts = r
        reads.append(tr); quotes.append(tq); sims.append(ts); totals.append(tr + tq + ts)
        log(f"  run {i+1}: reads {tr*1000:.0f}ms  quote {tq*1000:.0f}ms  sim {ts*1000:.0f}ms  "
            f"total {(tr+tq+ts)*1000:.0f}ms")
    if len(totals) < 3:
        sys.exit("too few successful runs to measure (KyberSwap/RPC hiccups) — rerun.")

    # throughput from recent blocks
    head = w3.eth.block_number
    counts = []
    for b in range(head - 20, head):
        try:
            counts.append(len(w3.eth.get_block(b)["transactions"]))
        except Exception:
            pass
    txpb = statistics.median(counts) if counts else 0.0
    R = txpb / BLOCK_TIME_S

    med_reads = statistics.median(reads); med_quote = statistics.median(quotes)
    med_sim = statistics.median(sims); med_total = statistics.median(totals)
    pipeline = med_total + DISPATCH_RTT_S          # detection -> submitted
    g_ours = pipeline * R

    log("---- SUMMARY ----")
    log(f"median: reads {med_reads*1000:.0f}ms  quote {med_quote*1000:.0f}ms  sim {med_sim*1000:.0f}ms  "
        f"-> prepare {med_total*1000:.0f}ms + dispatch {DISPATCH_RTT_S*1000:.0f}ms = pipeline {pipeline*1000:.0f}ms")
    log(f"throughput: {txpb:.0f} tx/block / {BLOCK_TIME_S:.0f}s = {R:.1f} tx/s")
    log(f"G_ours = {pipeline:.3f}s * {R:.1f} tx/s = {g_ours:.0f} tx-positions (where our liquidate would land after detection)")
    log("")
    log("== reachable vs MEDIAN winner per market (gap > G_ours => we'd land first at equal fee; UPPER bound) ==")
    for pair, g in sorted(MED_GAP.items(), key=lambda kv: -kv[1]):
        verdict = "REACHABLE" if g > g_ours else "too slow vs median winner"
        log(f"  {pair:<14} med_gap={g:>4}   {verdict}")
    log("")
    log(f"NOTE: G_ours={g_ours:.0f} is a NECESSARY-condition gate, not win-rate. A winner's gap is how late")
    log("they WERE, not their floor; under our pressure windows compress. Feed G_ours into gap_profile")
    log("(bucket reaction $ by gap>G_ours) for the reachable-$ share, then a live paper run for the real one.")


if __name__ == "__main__":
    main()
