"""Read-only: is this RPC flashblock-aware — does it see PRECONF (unconfirmed) tx state? This is the
load-bearing gate for hot-build stage 2, decoupled from oracle transmits / volatility: it fires on
EVERY flashblock tx (tens/min), so it answers in ~90s with no waiting and no volatility needed. If
the RPC doesn't know an unconfirmed flashblock tx, then eth_call(block="pending") cannot reflect a
just-landed `transmit`, and detecting it early buys nothing -> the clean stage-2 path is dead.

Method: for each flashblock tx (raw RLP), txhash = keccak(raw); query eth_getTransactionByHash, then
read the confirmed head. Split every sample by whether the tx's block is already confirmed:
  CONTROL  (bn <= confirmed head): known-rate MUST be ~100% — it VALIDATES raw->hash. If this is low,
    our hashing is wrong and the whole result is inconclusive (NOT a verdict on the node).
  PRECONF  (bn  > confirmed head): the tx's block is not yet confirmed -> known-rate IS the answer.
    ~0% => node is NOT flashblock-aware => (3) RED (need preconf RPC endpoint or local OCR-decode).
    high => node serves preconf state => (3) GREEN => eth_call(pending) path is viable.
Also prints pending vs latest block numbers + pending tx count as a secondary signal.

    DURATION=90   (seconds; default 90)   python -m analysis.pending_probe
Read-only: no WALLET_KEY, no tx, no bot touch.
"""
from __future__ import annotations
import os
import sys
import time


def main():
    sys.path.insert(0, ".")
    import asyncio
    import json
    import websockets
    import brotli
    from web3 import Web3
    from config import Config
    from chain.rpc import BaseRpc
    from chain.feeds import FB_URL, extract_txs, block_number_of

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    duration = float(os.environ.get("DURATION", "90"))

    def log(m):
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    # one-shot pending vs latest header comparison (secondary signal)
    try:
        pend = w3.eth.get_block("pending"); late = w3.eth.get_block("latest")
        pn = pend.get("number"); ln = late.get("number")
        log(f"pending head={pn} latest head={ln} (pending ahead by {pn - ln if pn and ln else '?'}); "
            f"pending tx count={len(pend.get('transactions') or [])}")
    except Exception as e:
        log(f"pending/latest header read failed: {type(e).__name__}: {str(e)[:80]}")

    ctrl_n = ctrl_known = pre_n = pre_known = 0
    sampled = 0
    log(f"preconf-probe start (duration={duration}s) — READ-ONLY, bot untouched")

    async def run():
        nonlocal ctrl_n, ctrl_known, pre_n, pre_known, sampled
        t_end = time.monotonic() + duration
        async with websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None) as ws:
            while time.monotonic() < t_end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, t_end - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                try:
                    txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                    d = json.loads(txt)
                except Exception:
                    continue
                bn = block_number_of(d)
                if bn is None:
                    continue
                raw_tx = next((r for r, _ in extract_txs(d) if r), None)
                if not raw_tx:
                    continue
                try:
                    txhash = Web3.keccak(hexstr=raw_tx).hex()
                except Exception:
                    continue
                # query known-ness FIRST, then read confirmed head: if tx is known AND its block is
                # still beyond the confirmed head, the node knew an unconfirmed tx (tight, low confound)
                try:
                    w3.eth.get_transaction(txhash)
                    known = True
                except Exception:
                    known = False
                try:
                    confirmed = w3.eth.block_number
                except Exception:
                    continue
                sampled += 1
                if bn > confirmed:
                    pre_n += 1; pre_known += int(known)
                    if pre_n <= 8:
                        log(f"PRECONF sample: tx block={bn} > confirmed={confirmed}  known={known}")
                else:
                    ctrl_n += 1; ctrl_known += int(known)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

    log("---- SUMMARY ----")
    log(f"samples={sampled}")
    cr = (100 * ctrl_known / ctrl_n) if ctrl_n else None
    pr = (100 * pre_known / pre_n) if pre_n else None
    log(f"CONTROL (confirmed-block tx, hashing sanity): {ctrl_known}/{ctrl_n}"
        f"  ({'n/a' if cr is None else f'{cr:.0f}% known — should be ~100%'})")
    log(f"PRECONF (unconfirmed-block tx, THE answer):   {pre_known}/{pre_n}"
        f"  ({'n/a' if pr is None else f'{pr:.0f}% known'})")
    if cr is not None and cr < 80:
        log("VERDICT: INCONCLUSIVE — control known-rate low => raw->hash mismatch, fix before trusting.")
    elif pr is None:
        log("VERDICT: INCONCLUSIVE — no unconfirmed-block samples captured (rerun, maybe longer DURATION).")
    elif pr >= 50:
        log("VERDICT: GREEN — RPC serves preconf flashblock state => eth_call(pending) path VIABLE for stage 2.")
    else:
        log("VERDICT: RED — RPC does NOT know unconfirmed flashblock txs => eth_call(pending) can't see a "
            "just-landed transmit. Stage 2 needs a preconf RPC endpoint or local OCR-report decode.")


if __name__ == "__main__":
    main()
