"""Live persistent logger over chain.feeds.watch_feeds — HOT-BUILD STAGE 1, DETECT-ONLY.
Logs every oracle update to one of OUR aggregators (block, sub-block index, markets), live-resolved
from covered. Does NOT touch the bot: second read-only subscription, no send/assess/shared state.
    DURATION=   (empty=forever, Ctrl-C)   python -m analysis.feed_watch
"""
from __future__ import annotations
import os
import sys
import time


def main():
    sys.path.insert(0, ".")
    import asyncio
    from config import Config
    from chain.rpc import BaseRpc
    from chain.feeds import watch_feeds
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    dur = os.environ.get("DURATION", "").strip()
    duration = float(dur) if dur else None
    seen = {"n": 0, "idx_gt0": 0}

    def on_update(market_ids, info):
        seen["n"] += 1
        idx = info.get("index")
        miss = ""
        if isinstance(idx, int) and idx > 0:
            seen["idx_gt0"] += 1
            miss = "  (index>0: текущий ассесс пропустил бы в этом блоке)"
        print(f"{time.strftime('%H:%M:%S')} UPDATE agg={info['agg'][:12]}… block={info['block']} "
              f"index={idx} markets={[m[:10] for m in market_ids]}{miss}", flush=True)

    def log(msg):
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    log(f"feed-watch старт (duration={'∞' if duration is None else duration}s) — DETECT-ONLY, бот не трогаем")
    try:
        asyncio.run(watch_feeds(rpc, lambda: load_covered_markets(cfg.covered_markets_path),
                                on_update, duration=duration, log=log))
    except KeyboardInterrupt:
        pass
    log(f"стоп. обновлений наших фидов поймано: {seen['n']}  (на index>0: {seen['idx_gt0']})")


if __name__ == "__main__":
    main()
