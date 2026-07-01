"""Stage 3a (read-only PAPER). On a real `transmit` to one of OUR aggregators, recompute the affected
candidates' HF on the PRE-CONFIRMED price (oracle.price('pending') from the preconf RPC) using the
EXACT prod health math (chain.simulate.health_from + MarketContext), and log/persist a 'PAPER would-take'
whenever a position flips liquidatable. Two purposes:
  1) validate the preconf pipeline END-TO-END (detect transmit -> price('pending') -> HF flip) — the
     load-bearing prerequisite for ANY hot path;
  2) emit the would-take stream (block, sub-block index, timestamp, repaid$) that stage 3b cross-checks
     against the ACTUAL winners (Morpho API + getLogs gap) to get our realistic share at equal start.

Touches NOTHING armed: own flashblock subscription with PRECISE 94-form matching (no over-count), own
preconf RPC read, candidate set from the Morpho API in a daemon thread. No WALLET_KEY, no tx, no bot
state. Flips appended to FLAGS_PATH (JSONL) for 3b.

    PRECONF_RPC=https://mainnet-preconf.base.org  DURATION=  FLAGS_PATH=paper_flags.jsonl
    venv/bin/python -m analysis.paper_share        (DURATION empty = forever, Ctrl-C)
"""
from __future__ import annotations
import os
import sys
import time
import json
import threading
from collections import defaultdict

from config import Config
from chain.rpc import BaseRpc
from chain.feeds import FB_URL, resolve_feeds, extract_txs, block_number_of
from chain.simulate import ORACLE_ABI, MarketContext, health_from
from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
    encode_market_call, decode_market, encode_position_call, decode_position)
from chain.morpho import MORPHO_BLUE_ADDRESS, positions_at_risk
from strategy.scanner import load_covered_markets

PRECONF_RPC = os.environ.get("PRECONF_RPC", "https://mainnet-preconf.base.org")
FLAGS_PATH = os.environ.get("FLAGS_PATH", "paper_flags.jsonl")


def flips_from(price, lltv_wad, tba, tbs, positions):
    """PURE: the prod flip (health_from + MarketContext) with the PRECONF price substituted — nothing
    else differs from the armed assess. positions: [(borrower, borrow_shares, collateral, debt_usd)]
    -> [(borrower, HealthReport, debt_usd)] for the ones that flip liquidatable."""
    ctx = MarketContext(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                        total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
    out = []
    for (b, bs, col, du) in positions:
        hr = health_from(ctx, int(bs), int(col))
        if hr.liquidatable:
            out.append((b, hr, du))
    return out


def resolve_meta(rpc, markets):
    """{mid: (oracle, lltv_wad)} via ONE aggregate3 of idToMarketParams (immutable)."""
    mids = [m.market_id for m in markets if m.market_id]
    calls = [(MORPHO_BLUE_ADDRESS, encode_id_to_market_params_call(rpc.to_bytes32(mid))) for mid in mids]
    out = {}
    for mid, (ok, data) in zip(mids, aggregate3(rpc, calls)):
        if ok and data:
            _loan, _coll, oracle, _irm, lltv = decode_id_to_market_params(data)
            out[mid] = (oracle, lltv)
    return out


def read_preconf_price(preconf_rpc, oracle):
    """oracle.price() at block='pending' on the preconf RPC = the pre-confirmed Morpho price. None on fail
    (public preconf endpoint rate-limits)."""
    try:
        return int(preconf_rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending"))
    except Exception:
        return None


def read_positions(rpc, mid, borrowers):
    """market totals + each position at latest -> (tba, tbs, [(borrower, bs, col)]). ONE aggregate3."""
    b32 = rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE_ADDRESS, encode_market_call(b32))]
    for b in borrowers:
        calls.append((MORPHO_BLUE_ADDRESS, encode_position_call(b32, b)))
    res = aggregate3(rpc, calls)
    if not res or not res[0][0] or not res[0][1]:
        return None
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    pos = []
    for b, (ok, data) in zip(borrowers, res[1:]):
        if ok and data:
            bs, col = decode_position(data)
            pos.append((b, bs, col))
    return tba, tbs, pos


def main():
    sys.path.insert(0, ".")
    import asyncio
    import websockets
    import brotli

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    preconf_rpc = BaseRpc(PRECONF_RPC, cfg.chain_id)
    dur = os.environ.get("DURATION", "").strip()
    duration = float(dur) if dur else None

    def log(m):
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    markets = load_covered_markets(cfg.covered_markets_path)
    feeds = resolve_feeds(rpc, markets)          # {agg_lower: [mids]}
    meta = resolve_meta(rpc, markets)            # {mid: (oracle, lltv_wad)}
    agg_set = set(feeds)
    if not agg_set:
        sys.exit("no Chainlink aggregators resolved from covered markets.")
    log(f"paper-share start (preconf={PRECONF_RPC}, dur={'inf' if duration is None else duration}s) — READ-ONLY, bot untouched")
    log(f"{len(agg_set)} aggregators / {len(meta)} markets resolved; flags -> {FLAGS_PATH}")

    shared = {"cands": {}}                       # {mid: [(borrower, debt_assets, debt_usd)]}
    stop = threading.Event()

    def refresher():
        while not stop.is_set():
            try:
                cs = defaultdict(list)
                for c in positions_at_risk(markets, hf_ceiling=1.10):
                    cs[c.market_id].append((c.borrower, c.debt_assets, c.debt_usd))
                shared["cands"] = dict(cs)
            except Exception as e:
                log(f"candidate refresh failed: {type(e).__name__}")
            stop.wait(60)

    threading.Thread(target=refresher, daemon=True).start()
    for _ in range(30):                          # wait for the first candidate load
        if shared["cands"]:
            break
        time.sleep(1)
    log(f"candidate set: {sum(len(v) for v in shared['cands'].values())} at-risk positions across {len(shared['cands'])} markets")

    ctr = {"transmits": 0, "preconf_ok": 0, "preconf_fail": 0, "flips": 0}
    fh = open(FLAGS_PATH, "a")

    def on_transmit(agg, block, subidx, t):
        ctr["transmits"] += 1
        cands = shared["cands"]
        for mid in feeds.get(agg, []):
            om = meta.get(mid)
            if not om:
                continue
            oracle, lltv_wad = om
            rows = cands.get(mid, [])
            if not rows:
                continue
            price = read_preconf_price(preconf_rpc, oracle)
            if price is None:
                ctr["preconf_fail"] += 1
                continue
            ctr["preconf_ok"] += 1
            rp = read_positions(rpc, mid, [b for (b, da, du) in rows])
            if not rp:
                continue
            tba, tbs, pos = rp
            du_by = {b: du for (b, da, du) in rows}
            positions = [(b, bs, col, du_by.get(b, 0.0)) for (b, bs, col) in pos]
            for (b, hr, du) in flips_from(price, lltv_wad, tba, tbs, positions):
                ctr["flips"] += 1
                rec = {"t": t, "block": block, "subidx": subidx, "agg": agg, "market": mid,
                       "borrower": b, "hf": round(hr.hf, 6), "repaid_usd": round(du, 2),
                       "preconf_price": str(price)}
                fh.write(json.dumps(rec) + "\n"); fh.flush()
                log(f"PAPER would-take {mid[:10]}/{b[:10]} hf={hr.hf:.4f} repaid~${du:,.0f} "
                    f"block={block} subidx={subidx}  <-- preconf flip")

    async def run():
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
                now = time.time()
                for agg in agg_set:                          # PRECISE 94-form match (no over-count)
                    pf = "94" + agg[2:]
                    if any((to_ == agg) or (raw_ and pf in raw_) for raw_, to_ in txs):
                        on_transmit(agg, bn, idx, now)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    stop.set(); fh.close()
    log("---- SUMMARY ----")
    log(f"precise transmits={ctr['transmits']}  preconf reads ok/fail={ctr['preconf_ok']}/{ctr['preconf_fail']}  "
        f"would-take flips={ctr['flips']}  (flags -> {FLAGS_PATH})")
    log("next (3b): match flags to ACTUAL liquidations (Morpho API + getLogs gap); compare our")
    log("detect(subidx)+G_ours vs the winner's gap -> realistic share at EQUAL detection start.")


if __name__ == "__main__":
    main()
