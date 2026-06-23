"""Feed map + sub-block oracle-update reader (rotation-aware). HOT-BUILD STAGE 1: DETECT ONLY.
resolve_feeds(rpc, markets) -> {aggregator_lower: [market_id]} via idToMarketParams(id).oracle ->
oracle.BASE_FEED_1() -> proxy.aggregator(); rebuilt from CURRENT covered (follows rotation).
watch_feeds(...) subscribes to Flashblocks, parses diff/transactions for `transmit` to those
aggregators, calls on_update(market_ids, info) — NOTHING ELSE. No price, no assess, no send.
Import-clean: websockets/brotli/Morpho ABIs imported lazily.
"""
from __future__ import annotations
import time
from collections import defaultdict

FB_URL = "wss://mainnet.flashblocks.base.org/ws"
ZERO = "0x0000000000000000000000000000000000000000"


def _addr_abi(n):
    return [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "address"}]}]


def _try_addr(rpc, addr, getter):
    try:
        v = getattr(rpc.contract(addr, _addr_abi(getter)).functions, getter)().call()
        return v if v and v != ZERO else None
    except Exception:
        return None


def _resolve_one(rpc, market_id):
    """market_id -> OCR aggregator (lower) or None (non-Chainlink wrapper). I/O; patched in tests."""
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)
    try:
        oracle = morpho.functions.idToMarketParams(rpc.to_bytes32(market_id)).call()[2]
    except Exception:
        return None
    proxy = _try_addr(rpc, oracle, "BASE_FEED_1")
    if not proxy:
        return None
    agg = _try_addr(rpc, proxy, "aggregator") or proxy
    return agg.lower() if agg else None


def resolve_feeds(rpc, markets):
    """{aggregator_lower: [market_id,...]} for covered markets (grouping over _resolve_one).
    Non-Chainlink markets (no BASE_FEED_1) omitted — separate reader branch."""
    aggs = defaultdict(list)
    for m in markets:
        agg = _resolve_one(rpc, m.market_id)
        if agg:
            aggs[agg].append(m.market_id)
    return dict(aggs)


def extract_txs(d):
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
            out.append(((raw.lower() if isinstance(raw, str) else None),
                        (t.get("to") or "").lower() or None))
        else:
            out.append((None, None))
    return out


def match_aggs(txs, agg_set):
    hit = set()
    for raw, to in txs:
        if to and to in agg_set:
            hit.add(to)
        elif raw:
            for a in agg_set:
                if a[2:] in raw:
                    hit.add(a)
    return hit


def block_number_of(d):
    base = d.get("base") if isinstance(d.get("base"), dict) else {}
    meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    bn = base.get("block_number") or meta.get("block_number")
    if bn is None:
        return None
    return int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else int(bn)


async def watch_feeds(rpc, markets_loader, on_update, *, duration=None,
                      feed_rescan_sec=3600.0, ws_factory=None, log=None):
    """Subscribe to Flashblocks; on oracle update to one of our aggregators in a sub-block, call
    on_update(market_ids, info). DETECT ONLY. markets_loader re-called every feed_rescan_sec
    (rotation). info={block,index,agg,t}. ws_factory: test seam."""
    import asyncio
    import json
    import websockets
    import brotli

    def _default_ws():
        return websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None)

    ws_factory = ws_factory or _default_ws
    feeds = resolve_feeds(rpc, markets_loader())
    agg_set = set(feeds)
    last_resolve = time.monotonic()
    if log:
        log("feed-watch: %d aggregators from %d markets (live resolve)" % (len(agg_set), sum(len(v) for v in feeds.values())))
    t_end = (time.monotonic() + duration) if duration else None

    async with ws_factory() as ws:
        while t_end is None or time.monotonic() < t_end:
            if time.monotonic() - last_resolve >= feed_rescan_sec:
                try:
                    feeds = resolve_feeds(rpc, markets_loader())
                    agg_set = set(feeds)
                    last_resolve = time.monotonic()
                    if log:
                        log("feed-watch: re-resolved -> %d aggregators" % len(agg_set))
                except Exception as e:
                    if log:
                        log("feed-watch: re-resolve failed: %r" % e)
            timeout = 5.0 if t_end is None else max(1.0, t_end - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
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
            hits = match_aggs(extract_txs(d), agg_set)
            if not hits:
                continue
            idx = d.get("index")
            now = time.monotonic()
            for a in hits:
                on_update(feeds.get(a, []), {"block": bn, "index": idx, "agg": a, "t": now})
