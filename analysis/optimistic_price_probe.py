"""Read-only STAGE 2: is there a pre-confirmed (optimistic) price source?
Tests path (b) PENDING tag first (simpler/robuster than decoding OCR transmit). For each oracle
update caught in a sub-block (block B, idx k), reads Morpho wrapper price() at 'pending' vs 'latest'
at that moment, then after B confirms checks if pending predicted the confirmed value:
  pending==confirmed!=latest -> PENDING-LEADS+CORRECT (flashblock-aware) -> build Stage 3 on it
  pending==latest            -> not flashblock-aware -> need path (a) decode fallback
Also captures transmit selector (groundwork for path a; no decode here). Reuses chain.feeds.
Read-only: ws + eth_call, no key, no send.
    DURATION=180 python -m analysis.optimistic_price_probe
"""
from __future__ import annotations
import os
import sys

DURATION = float(os.environ.get("DURATION", "180"))


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
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
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
    print(f"==== STAGE 2: pre-confirmed price? тест PENDING ({DURATION:.0f}s) ====")
    print(f"  слушаем {len(agg_set)} агрегаторов; wrapper.price() pending vs latest на каждом обновлении")

    pending_unsupported = [False]

    def price_at(oracle, block_id):
        return rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier=block_id)

    checks = {}; results = []; cur_block = None
    t_end = time.monotonic() + DURATION

    def finalize(b):
        done = []
        for ev in checks.get(b, []):
            try:
                p_conf = price_at(ev["oracle"], b)
            except Exception:
                continue
            sel = "?"
            try:
                inp = w3.eth.get_transaction(ev["txhash"])["input"]
                h = inp.hex() if hasattr(inp, "hex") else str(inp)
                sel = "0x" + (h[2:] if h.startswith("0x") else h)[:8]
            except Exception:
                pass
            led = ev["p_pending"] != ev["p_latest"]
            correct = ev["p_pending"] == p_conf
            stale_latest = ev["p_latest"] != p_conf
            if pending_unsupported[0]:
                v = "pending-UNSUPPORTED"
            elif led and correct:
                v = "PENDING-LEADS+CORRECT"
            elif (not led) and stale_latest:
                v = "pending-STALE(=latest)"
            elif not stale_latest:
                v = "no-price-change-by-confirm"
            else:
                v = "pending-led-but-wrong"
            results.append(v)
            print(f"  block {b} idx {ev['index']} agg {ev['agg'][:10]}… sel={sel}  "
                  f"latest={ev['p_latest']} pending={ev['p_pending']} confirmed={p_conf}  -> {v}")
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
                    p_latest = price_at(oracle, "latest"); p_pending = price_at(oracle, "pending")
                except Exception:
                    pending_unsupported[0] = True; p_latest = p_pending = None
                txhash = w3.keccak(hexstr=rawtx).hex() if rawtx else None
                checks.setdefault(bn, []).append(
                    {"index": idx, "agg": matched, "oracle": oracle,
                     "p_latest": p_latest, "p_pending": p_pending, "txhash": txhash})

    for b in sorted(list(checks)):
        finalize(b)

    print(f"\n==== РЕЗУЛЬТАТ ({len(results)} обновлений с проверкой) ====")
    tally = Counter(results)
    for v, n in tally.most_common():
        print(f"  {v}: {n}")
    leads = tally.get("PENDING-LEADS+CORRECT", 0)
    if leads and leads >= max(1, sum(tally.values()) // 2):
        print("\n  ВЫВОД: pending flashblock-aware и верен -> стадия 3 строит оптимистичный гейт на")
        print("  wrapper.price(block='pending'), опрашивая под-блоками. Декод transmit НЕ нужен.")
    elif tally.get("pending-STALE(=latest)", 0) or pending_unsupported[0]:
        print("\n  ВЫВОД: pending НЕ отдаёт pre-confirmed -> путь (б) мёртв. Нужен путь (а): декод OCR")
        print("  `transmit`-calldata. Селекторы выше -> сверить с верифицированным ABI на Basescan, строить по факту.")
    else:
        print("\n  ВЫВОД: смешанно/мало данных (возможно лаг ингеста pending). Прогнать дольше/в волатильность.")
    print("  (read-only стадия 2: источник оптимистичной цены выбираем ФАКТОМ.)")


def main():
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
