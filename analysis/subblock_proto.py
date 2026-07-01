"""Read-only PROTOTYPE: sub-block reader — does flashblock pre-confirmation give us the oracle
update earlier than our current once-per-block confirmed assess? (Step #3, proves the win first.)
Reuses the bot's flashblock pattern but ALSO parses diff/transactions (bot ignores) to catch
`transmit` txs to OUR OCR aggregators. Aggregators resolved LIVE from covered (rotation-aware).
Measures sub-block INDEX of each update + LEAD vs our once-per-block assess. PHASE A dumps the real
diff.transactions shape. Read-only: ws subscribe + eth_call only, no key, no tx.
    DURATION=90 python -m analysis.subblock_proto
"""
from __future__ import annotations
import os
import sys

DURATION = float(os.environ.get("DURATION", "90"))
FB_URL = "wss://mainnet.flashblocks.base.org/ws"
ZERO = "0x0000000000000000000000000000000000000000"


def _addr_abi(n):
    return [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "address"}]}]


def _try_addr(rpc, addr, g):
    try:
        v = getattr(rpc.contract(addr, _addr_abi(g)).functions, g)().call()
        return v if v and v != ZERO else None
    except Exception:
        return None


def resolve_aggregators(rpc, morpho, markets):
    from collections import defaultdict
    aggs = defaultdict(list); ocache = {}
    for m in markets:
        try:
            oracle = morpho.functions.idToMarketParams(rpc.to_bytes32(m.market_id)).call()[2]
        except Exception:
            continue
        if oracle not in ocache:
            proxy = _try_addr(rpc, oracle, "BASE_FEED_1")
            ocache[oracle] = (_try_addr(rpc, proxy, "aggregator") or proxy) if proxy else None
        agg = ocache[oracle]
        if agg:
            aggs[agg.lower()].append(m.market_id)
    return dict(aggs)


def _extract_txs(d):
    diff = d.get("diff") or {}
    txs = diff.get("transactions")
    if txs is None:
        txs = d.get("transactions") or []
    out = []
    for t in txs:
        if isinstance(t, str):
            out.append((t.lower(), None))
        elif isinstance(t, dict):
            raw = t.get("raw") or t.get("rlp") or t.get("input")
            out.append(((raw.lower() if isinstance(raw, str) else None), (t.get("to") or "").lower() or None))
        else:
            out.append((None, None))
    return out


def _match(txs, agg_set):
    hit = set()
    for raw, to in txs:
        if to and to in agg_set:
            hit.add(to)
        elif raw:
            for a in agg_set:
                if a[2:] in raw:
                    hit.add(a)
    return hit


async def run():
    import asyncio, json, time, statistics
    from collections import Counter
    import websockets, brotli
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)
    markets = load_covered_markets(cfg.covered_markets_path)
    aggs = resolve_aggregators(rpc, morpho, markets)
    agg_set = set(aggs)
    print(f"==== под-блок-ридер ПРОТОТИП ({DURATION:.0f}s) ====")
    print(f"  covered={len(markets)}  слушаем {len(agg_set)} OCR-агрегаторов (живой резолв)")

    events, pending, cur_block = [], [], None
    sub_count = Counter(); dumped = 0
    t_end = time.monotonic() + DURATION

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
            idx = d.get("index")
            base = d.get("base") if isinstance(d.get("base"), dict) else {}
            meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
            bn = base.get("block_number") or meta.get("block_number")
            if bn is None:
                continue
            bn = int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else int(bn)
            now = time.monotonic()
            if dumped < 2:
                diff = d.get("diff") or {}
                txs = diff.get("transactions") or d.get("transactions") or []
                sample = txs[0] if txs else None
                print(f"\n  [discover] keys={list(d.keys())} index={idx} block={bn} "
                      f"diff.keys={list(diff.keys())[:8]} n_txs={len(txs)}")
                print(f"             tx[0] type={type(sample).__name__} "
                      f"sample={(sample[:80]+'…') if isinstance(sample,str) else sample}")
                print(f"             metadata.keys={list(meta.keys())[:10]}")
                dumped += 1
            if cur_block is not None and bn != cur_block:
                for ev in pending:
                    ev["lead_ms"] = (now - ev["t"]) * 1000.0
                    events.append(ev)
                pending = []
            cur_block = bn
            if idx is not None:
                sub_count[idx] += 1
            for a in _match(_extract_txs(d), agg_set):
                pending.append({"block": bn, "index": idx if idx is not None else -1, "agg": a, "t": now})

    leads = [e["lead_ms"] for e in events if "lead_ms" in e]
    print(f"\n==== РЕЗУЛЬТАТ ====")
    print(f"  под-блоков по index: {dict(sorted(sub_count.items(), key=lambda kv:(kv[0] is None, kv[0])))}")
    print(f"  оракловых обновлений НАШИХ агрегаторов поймано: {len(events)}")
    if not events:
        print("  (за окно ни одного обновления наших фидов — цены стояли; увеличь DURATION/окно волатильности)")
        return
    idxc = Counter(e["index"] for e in events)
    print(f"  на каком под-блоке (index) появлялось обновление: {dict(sorted(idxc.items()))}")
    for a, n in Counter(e["agg"] for e in events).most_common():
        print(f"    {a}  обновлений={n}  рынков={len(aggs.get(a, []))}")
    if leads:
        leads.sort(); med = statistics.median(leads)
        print(f"\n  ЛИД vs наш ассесс (раз-в-блок): min={min(leads):.0f}ms median={med:.0f}ms max={max(leads):.0f}ms (n={len(leads)})")
        early = sum(1 for e in events if isinstance(e['index'], int) and e['index'] > 0)
        print(f"  обновлений на index>0 (наш index-0 ассесс их пропускает до след. блока): {early}/{len(events)}")
        print(f"\n  Вывод: median lead ~{med:.0f}ms = фора под-блок-ридера над текущим путём.")
        print("  Стабильные сотни мс–секунды + обновления на index>0 -> это и есть разрыв, который")
        print("  под-блок-ассесс (оптимистичный гейт на pre-confirmed цене) закрывает. Тогда -> билд по контракту.")
    print("\n  (read-only прототип: доказывает выигрыш ДО горячего пути.)")


def main():
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
