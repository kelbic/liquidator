"""Read-only: re-test PRE-CONFIRMED price via a FLASHBLOCK-AWARE RPC.
Stage-2 showed pending==latest on the bot's RPC = signature of a STANDARD (non-flashblocks)
endpoint. On a flashblock-aware endpoint eth_call(pending) runs against pre-confirmed sub-block
state (~1.8s before sealing). Re-test price(pending) vs: bot RPC + https://mainnet-preconf.base.org.
Anchored to oracle updates caught in sub-blocks; verify which pending == confirmed != latest.
If a flashblock endpoint leads -> optimistic source (in wrapper format), OCR decoder unneeded.
Read-only: ws + eth_call, no key, no send.
    DURATION=300 python -m analysis.preconf_price_probe
"""
from __future__ import annotations
import os
import sys

DURATION = float(os.environ.get("DURATION", "300"))
PRECONF_URL = os.environ.get("PRECONF_URL", "https://mainnet-preconf.base.org")


async def run():
    import asyncio, json, time
    from collections import Counter
    import websockets, brotli
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI, ORACLE_ABI
    from chain.feeds import resolve_feeds, extract_txs, block_number_of, FB_URL
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    pre = BaseRpc(PRECONF_URL, cfg.chain_id)
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)

    markets = load_covered_markets(cfg.covered_markets_path)
    feeds = resolve_feeds(rpc, markets)
    agg_set = set(feeds)
    agg_market, market_oracle = {}, {}
    for agg, mids in feeds.items():
        mid = mids[0]; agg_market[agg] = mid
        try:
            market_oracle[mid] = morpho.functions.idToMarketParams(rpc.to_bytes32(mid)).call()[2]
        except Exception:
            market_oracle[mid] = None

    print(f"==== PRE-CONFIRMED price via flashblock-aware RPC ({DURATION:.0f}s) ====")
    print(f"  bot RPC pending  vs  preconf {PRECONF_URL} pending  vs  confirmed")
    try:
        print(f"  preconf reachable: block_number={pre.block_number()}")
    except Exception as e:
        print(f"  ! preconf НЕ доступен: {type(e).__name__}: {str(e)[:100]} (тогда только bot RPC)")

    def price_at(client, oracle, block_id):
        return client.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier=block_id)

    checks, results = {}, []
    cur_block = None
    t_end = time.monotonic() + DURATION

    def classify(latest, pending, conf):
        if pending is None:
            return "unavailable"
        led = pending != latest; correct = pending == conf; stale = latest != conf
        if led and correct:
            return "LEADS+CORRECT"
        if (not led) and stale:
            return "STALE(=latest)"
        if not stale:
            return "no-change"
        return "led-but-wrong"

    def finalize(b):
        done = []
        for ev in checks.get(b, []):
            try:
                conf = price_at(rpc, ev["oracle"], b)
            except Exception:
                continue
            vb = classify(ev["bot_latest"], ev["bot_pending"], conf)
            vp = classify(ev["bot_latest"], ev["pre_pending"], conf)
            results.append((vb, vp))
            print(f"  blk {b} idx {ev['index']} agg {ev['agg'][:10]}…  bot[{vb}] preconf[{vp}]  "
                  f"latest={ev['bot_latest']} botPend={ev['bot_pending']} prePend={ev['pre_pending']} conf={conf}")
            done.append(ev)
        if b in checks:
            checks[b] = [e for e in checks[b] if e not in done]
            if not checks[b]:
                del checks[b]

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
            if cur_block is not None and bn != cur_block:
                for b in sorted([b for b in checks if b <= cur_block]):
                    finalize(b)
            cur_block = bn
            idx = d.get("index")
            for rawtx, to in extract_txs(d):
                matched = to if (to and to in agg_set) else next((a for a in agg_set if rawtx and a[2:] in rawtx), None)
                if not matched:
                    continue
                oracle = market_oracle.get(agg_market[matched])
                if not oracle:
                    continue
                try:
                    bot_latest = price_at(rpc, oracle, "latest")
                except Exception:
                    continue
                try:
                    bot_pending = price_at(rpc, oracle, "pending")
                except Exception:
                    bot_pending = None
                try:
                    pre_pending = price_at(pre, oracle, "pending")
                except Exception:
                    pre_pending = None
                checks.setdefault(bn, []).append(
                    {"index": idx, "agg": matched, "oracle": oracle,
                     "bot_latest": bot_latest, "bot_pending": bot_pending, "pre_pending": pre_pending})

    for b in sorted(list(checks)):
        finalize(b)

    print(f"\n==== РЕЗУЛЬТАТ ({len(results)} обновлений) ====")
    bot_tally = Counter(v for v, _ in results); pre_tally = Counter(v for _, v in results)
    print(f"  bot RPC     pending: {dict(bot_tally)}")
    print(f"  preconf RPC pending: {dict(pre_tally)}")
    n = len(results); bot_ok = bot_tally.get("LEADS+CORRECT", 0); pre_ok = pre_tally.get("LEADS+CORRECT", 0)
    if n and bot_ok >= max(1, n // 2):
        print("\n  ВЫВОД: BOT RPC уже flashblock-aware -> источник = wrapper.price(block='pending') на текущем RPC.")
        print("  Один эндпоинт, ноль декода. Стадия 3 строится на нём.")
    elif n and pre_ok >= max(1, n // 2):
        print(f"\n  ВЫВОД: preconf RPC отдаёт pre-confirmed цену (bot — нет). Источник = wrapper.price('pending') на")
        print(f"  {PRECONF_URL} (или flashblocks-вариант Alchemy). OCR-декод НЕ нужен.")
        print("  (Публичный preconf рейт-лимитится — для прода flashblocks-эндпоинт Alchemy/QuickNode/Chainstack.)")
    elif n:
        print("\n  ВЫВОД: ни один эндпоинт не лидирует -> только тогда путь (а) OCR-декод. (Проверь доступность")
        print("  preconf и что окно не тихое; возможно нужен платный flashblocks-RPC.)")
    else:
        print("\n  обновлений не поймано — тихое окно; прогнать дольше/в волатильность.")
    print("  (read-only: источник оптимистичной цены выбираем ФАКТОМ.)")


def main():
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
