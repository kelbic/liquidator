"""Read-only edge probe over Flashblocks. Measures; touches NOTHING (no send, no prod files, no
WALLET_KEY). Lightweight WORKFLOW path (read-only research, like preliq_inventory). Answers the
three open hot-build questions in ONE VPS run so stage 2 is built on data, not assumptions:

  (1) match_aggs OVER-COUNT — for every frame where one of our aggregator addresses appears, split
      PRECISE (`94`<agg> = the RLP `to`-field form, i.e. a real tx TO the aggregator -> a transmit)
      vs LOOSE-ONLY (agg bytes appear ONLY elsewhere in the raw tx, e.g. ABI calldata referencing
      the feed -> a FALSE positive). The loose-only count is exactly how much the current
      match_aggs/feed_watch over-reports.

  (2) HOT-BUILD SIGNAL — distribution of PRECISE transmit hits by sub-block index. index==0 the
      current once-per-block assess already catches; index>0 it misses WITHIN the block. The
      index>0 share is the real upside hot-build would unlock.

  (3) PENDING CAPABILITY (load-bearing) — on a PRECISE hit, read the aggregator's latestAnswer at
      block=pending vs block=latest. If pending != latest right after a transmit, the unconfirmed
      price is readable -> stage 2 can assess via eth_call(block="pending"). If pending==latest (or
      the tag errors), this RPC is NOT flashblock-aware -> detecting the transmit doesn't help until
      the block confirms, and stage 2 must decode the OCR report locally (or use a preconf RPC).

    DURATION=<sec>   (empty = forever, Ctrl-C)
    MAX_DUMP=<n>     (loose-only raw txs to print for eyeballing; default 5)
    python -m analysis.feed_probe
"""
from __future__ import annotations
import os
import sys
import time
from collections import Counter

# Chainlink OCR aggregator read — latestAnswer() is part of the AggregatorInterface; we read it at
# two block tags to test whether `pending` reflects the just-seen (unconfirmed) transmit.
_AGG_ANSWER_ABI = [{"name": "latestAnswer", "type": "function", "stateMutability": "view",
                    "inputs": [], "outputs": [{"name": "", "type": "int256"}]}]


def _answer_at(rpc, agg, tag):
    """agg.latestAnswer() at a given block tag. Returns int or None (tag unsupported / call failed)."""
    try:
        return int(rpc.contract(agg, _AGG_ANSWER_ABI).functions.latestAnswer().call(block_identifier=tag))
    except Exception as e:
        return ("ERR", type(e).__name__, str(e)[:80])


def main():
    sys.path.insert(0, ".")
    import asyncio
    import json
    import websockets
    import brotli
    from config import Config
    from chain.rpc import BaseRpc
    from chain.feeds import FB_URL, resolve_feeds, extract_txs, block_number_of
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    dur = os.environ.get("DURATION", "").strip()
    duration = float(dur) if dur else None
    max_dump = int(os.environ.get("MAX_DUMP", "5"))

    def log(m):
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    markets = load_covered_markets(cfg.covered_markets_path)
    feeds = resolve_feeds(rpc, markets)          # {agg_lower: [market_id]} from CURRENT covered set
    agg_set = set(feeds)
    if not agg_set:
        sys.exit("no Chainlink aggregators resolved from covered markets (nothing to probe).")
    log(f"probe start (duration={'inf' if duration is None else duration}s) — READ-ONLY, bot untouched")
    log(f"{len(agg_set)} aggregators from {sum(len(v) for v in feeds.values())} markets")

    precise_by_idx = Counter()    # sub-block index -> # PRECISE (real transmit) hits
    loose_only = 0                # frames where agg bytes appear ONLY loosely (false positives)
    n_precise = 0
    n_pending_checks = 0
    pending_differs = 0
    dumped = 0

    async def run():
        nonlocal loose_only, n_precise, n_pending_checks, pending_differs, dumped
        t_end = (time.monotonic() + duration) if duration else None
        async with websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None) as ws:
            while t_end is None or time.monotonic() < t_end:
                to = 5.0 if t_end is None else max(1.0, t_end - time.monotonic())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=to)
                except asyncio.TimeoutError:
                    if t_end is not None:
                        break
                    continue
                try:
                    txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                    d = json.loads(txt)
                except Exception:
                    continue
                bn = block_number_of(d)
                if bn is None:
                    continue
                idx = d.get("index")
                txs = extract_txs(d)
                for agg in agg_set:
                    needle = agg[2:]
                    precise_form = "94" + needle
                    precise = any((to_ == agg) or (raw_ and precise_form in raw_) for raw_, to_ in txs)
                    loose = precise or any(raw_ and needle in raw_ for raw_, _ in txs)
                    if precise:
                        precise_by_idx[idx if isinstance(idx, int) else -1] += 1
                        n_precise += 1
                        miss = "  <-- index>0: current assess MISSES in-block" if isinstance(idx, int) and idx > 0 else ""
                        log(f"PRECISE transmit agg={agg[:12]}.. block={bn} index={idx} markets={[m[:10] for m in feeds[agg]]}{miss}")
                        if n_pending_checks < 40:     # cap the extra RPC; enough to characterize
                            n_pending_checks += 1
                            a_pend = _answer_at(rpc, agg, "pending")
                            a_late = _answer_at(rpc, agg, "latest")
                            same = (a_pend == a_late)
                            if isinstance(a_pend, int) and isinstance(a_late, int) and not same:
                                pending_differs += 1
                            log(f"    pending-check: latest={a_late} pending={a_pend} "
                                f"{'SAME' if same else 'DIFFERS -> pending reflects unconfirmed!'}")
                    elif loose:
                        loose_only += 1
                        if dumped < max_dump:
                            dumped += 1
                            snippet = next((r[:80] for r, _ in txs if r and needle in r), "?")
                            log(f"LOOSE-ONLY (false positive) agg={agg[:12]}.. block={bn} index={idx} raw~{snippet}..")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

    log("---- SUMMARY ----")
    total_precise = sum(precise_by_idx.values())
    idx0 = precise_by_idx.get(0, 0)
    idxgt0 = total_precise - idx0
    log(f"(1) over-count: PRECISE transmit hits={total_precise}  LOOSE-ONLY false positives={loose_only}"
        f"  ({'n/a' if total_precise + loose_only == 0 else f'{100*loose_only/(total_precise+loose_only):.0f}% of current match_aggs hits are noise'})")
    log(f"(2) hot-build signal: PRECISE at index=0={idx0}  index>0={idxgt0}"
        f"  ({'n/a' if total_precise == 0 else f'{100*idxgt0/total_precise:.0f}% land at index>0 -> missed in-block today'})")
    log(f"    index breakdown: {dict(sorted(precise_by_idx.items(), key=lambda kv: (kv[0] is None, kv[0])))}")
    log(f"(3) pending capability: checks={n_pending_checks}  pending!=latest={pending_differs}"
        f"  -> {'PENDING IS FLASHBLOCK-AWARE (eth_call(pending) path viable)' if pending_differs else 'no observed diff (need a transmit during window, or RPC not flashblock-aware -> OCR-decode/preconf RPC)'}")


if __name__ == "__main__":
    main()
