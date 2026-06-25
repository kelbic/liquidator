"""Hot path (Phase 2, PRECONF-triggered). A SECOND trigger beside the block loop: on a real `transmit`
to one of our aggregators, recompute the affected candidates' HF on the PRE-CONFIRMED price, and for a
position that flips liquidatable + passes the preconf-pending sim + the net floor + the narrow reaction
filter ($2k+), prepare and dispatch a liquidation. Reuses the battle-tested prepare/dispatch building
blocks; ONLY the sourcing (preconf price for sizing + preconf-pending sim) and the trigger differ.

Latency shape: the async recv loop only DETECTS a transmit (precise 94-form match) and spawns a worker
thread; all slow work (preconf read -> flip -> KyberSwap quote -> sim -> dispatch) runs off the recv
loop so detection stays responsive. Cross-path nonce safety (this path + the block loop both send from
one wallet) is handled by the send-lock inside dispatch_liquidations (added in C2).

DORMANT until wired into block_driven_loop behind cfg.hot_path. No behavior change to the existing loop.
"""
from __future__ import annotations
import threading
import time

MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
FB_URL = "wss://mainnet.flashblocks.base.org/ws"
PRECONF_RPC_DEFAULT = "https://mainnet-preconf.base.org"
HOT_THROTTLE_SEC = 0.25   # min seconds between hot-path spawns PER aggregator. The bare match fires on
                          # frequent price-READ txs; this bounds preconf-RPC reads + thread spawns.
                          # Transmit cadence is minutes, so 0.25s loses no real update; raise if the
                          # public preconf endpoint rate-limits (a throttled/None read simply skips).
HOT_STATS_SEC = 120       # emit hot-path counters (spawn + gate outcomes) every N seconds. Lets us see
                          # detection is ALIVE without waiting for a flip (the gate is silent on readers).


# ---- pure helpers (unit-tested) ----

def _price_moved(agg, px, last_price):
    """True if px differs from the last preconf price seen for agg (or first sighting); updates
    last_price. The bare aggregator match also fires on price-READ txs, so this gates the heavy
    multicall to REAL transmits: a reader AFTER a transmit returns the new price (delta -> proceed);
    a reader with no transmit returns the same price (skip). px is None (unreadable) -> skip."""
    if px is None:
        return False
    prev = last_price.get(agg)
    last_price[agg] = px
    return prev is None or px != prev


def _flips(price, lltv_wad, tba, tbs, positions):
    """PURE flip recompute on the preconf price — identical to the armed assess (chain.simulate.health_from).
    positions: [(borrower, borrow_shares, collateral, debt_usd)] -> [(borrower, HealthReport, debt_usd)]."""
    from chain.simulate import MarketContext, health_from
    ctx = MarketContext(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                        total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
    out = []
    for (b, bs, col, du) in positions:
        hr = health_from(ctx, int(bs), int(col))
        if hr.liquidatable:
            out.append((b, hr, du))
    return out


def _net_gate(profit_wei, debt_usd, debt_assets, cfg):
    """PURE: profit/cost/net in USD — same arithmetic as prepare_liquidation's floor."""
    profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
    cost_usd = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
    return profit_usd, cost_usd, profit_usd - cost_usd


# ---- on-chain reads ----

def read_preconf_price(preconf_rpc, oracle):
    """oracle.price() at block='pending' on the preconf RPC = the pre-confirmed Morpho price. None on fail."""
    from chain.simulate import ORACLE_ABI
    try:
        return int(preconf_rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending"))
    except Exception:
        return None


def read_positions(rpc, mid, borrowers):
    """market totals + each position at latest -> (tba, tbs, [(borrower, bs, col)]). ONE aggregate3."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    b32 = rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))]
    for b in borrowers:
        calls.append((MORPHO_BLUE, encode_position_call(b32, b)))
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


def read_preconf_prices(preconf_rpc, oracles, rpc_fallback=None):
    """ONE aggregate3 at block='pending' on the preconf RPC -> ({oracle: price|None}, n_fallback). Reads
    ALL our oracles each block; covers markets whose aggregator never surfaces in the tx stream (the money
    markets cbXRP/cbDOGE/cbADA), which the bare match structurally misses. If rpc_fallback is given, any
    oracle that came back None (preconf degraded/rate-limited) is re-read on `latest` in ONE extra batch
    (latest is as fast and identical between transmits -> fills holes losing only the ~1.8s preconf lead).
    n_fallback = how many oracles were filled from latest (0 = preconf healthy)."""
    from chain.multicall import aggregate3, encode_price_call, decode_price
    if not oracles:
        return {}, 0
    calls = [(o, encode_price_call()) for o in oracles]
    try:
        res = aggregate3(preconf_rpc, calls, block_identifier="pending")
        out = {o: (decode_price(data) if (ok and data) else None) for o, (ok, data) in zip(oracles, res)}
    except Exception:
        out = {o: None for o in oracles}
    n_fb = 0
    if rpc_fallback is not None:
        missing = [o for o, v in out.items() if v is None]
        if missing:
            try:
                fb = aggregate3(rpc_fallback, [(o, encode_price_call()) for o in missing])
                for o, (ok, data) in zip(missing, fb):
                    if ok and data:
                        px = decode_price(data)
                        if px is not None:
                            out[o] = px
                            n_fb += 1
            except Exception:
                pass
    return out, n_fb


def _poll_changed(prices, poll_seen, oracle2agg):
    """PURE: given {oracle: price|None}, update poll_seen (baseline on first sighting) and return the
    set of aggregators whose oracle price MOVED. First sighting -> no spawn; None price -> skipped."""
    changed = set()
    for o, px in prices.items():
        if px is None:
            continue
        prev = poll_seen.get(o)
        poll_seen[o] = px
        if prev is not None and px != prev:
            changed.add(oracle2agg.get(o))
    changed.discard(None)
    return changed


def _poll_prices(*, preconf_rpc, rpc_fallback, feeds, meta, poll_seen, stats, spawn_kwargs, bn, log):
    """Per-block trigger: batch-read all oracle preconf prices; spawn a (gated) _process_transmit for
    each aggregator whose price moved. The agg-level gate dedups vs any bare-match spawn. This is what
    gives cbXRP/cbDOGE/cbADA a detection trigger at all (they never surface in the flashblock stream)."""
    oracle2agg = {}
    for agg, mids in feeds.items():
        for mid in mids:
            om = meta.get(mid)
            if om:
                oracle2agg[om[0]] = agg
    prices, n_fb = read_preconf_prices(preconf_rpc, list(oracle2agg), rpc_fallback=rpc_fallback)
    if stats is not None:
        stats["poll"] += 1
        stats["poll_none"] += sum(1 for v in prices.values() if v is None)
        stats["poll_fb"] += n_fb
    changed = _poll_changed(prices, poll_seen, oracle2agg)
    if stats is not None:
        stats["pspawn"] += len(changed)
    for agg in changed:
        threading.Thread(target=_process_transmit, args=(agg, bn, "poll", time.time()),
                         kwargs=spawn_kwargs, daemon=True).start()


def resolve_meta(rpc, markets):
    """{mid: (oracle, lltv_wad)} via ONE aggregate3 of idToMarketParams (immutable)."""
    from chain.multicall import aggregate3, encode_id_to_market_params_call, decode_id_to_market_params
    mids = [m.market_id for m in markets if m.market_id]
    calls = [(MORPHO_BLUE, encode_id_to_market_params_call(rpc.to_bytes32(mid))) for mid in mids]
    out = {}
    for mid, (ok, data) in zip(mids, aggregate3(rpc, calls)):
        if ok and data:
            _loan, _coll, oracle, _irm, lltv = decode_id_to_market_params(data)
            out[mid] = (oracle, lltv)
    return out


# ---- preconf-sourced prepare (mirror of execute.prepare_liquidation; 2 diffs only) ----

def _diag_revert_bucket(err: str) -> str:
    """Классифицирует sim-revert в бакет для DIAG. Чистая (под юнит)."""
    e = (err or "").lower()
    if "0x11" in e or "panic" in e:
        return "panic-underflow"
    if "0x81ceff30" in e:
        return "swapfailed"
    if "healthy" in e or "position is heal" in e:
        return "healthy-race"
    if "0x08c379a0" in e:
        return "error-string"
    return "other"


def prepare_hot(rpc, preconf_rpc, cfg, market_id, borrower, debt_usd, debt_assets, price, slippage_bps=100):
    """Mirror of execute.prepare_liquidation, PRECONF-sourced. Two and only two differences:
      (1) the seize is sized on the PRE-CONFIRMED `price` (passed in, read once by the caller), not a
          fresh latest read — so `expected_seized` matches what the contract seizes at execution;
      (2) the simulate is gated against PRECONF-PENDING (simulate_tx(preconf_rpc, ..., block='pending'))
          where the position is already liquidatable, instead of latest (still healthy pre-confirm).
    Everything else — fresh market-param/market/position reads, seize math, KyberSwap quote, encode,
    honest net floor, minProfit=95% — is identical to the armed prepare. Same {ok, ...} shape."""
    try:
        from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
            encode_market_call, decode_market, encode_position_call, decode_position)
        from chain.simulate import to_assets_up
        from chain.execute import kyber_swap, encode_liquidate, expected_seized, simulate_tx
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = w3.eth.account.from_key(cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        # market-PARAMS (статичные адреса рынка) — из подтверждённого rpc (не меняются):
        rp_params = aggregate3(rpc, [(MORPHO_BLUE, encode_id_to_market_params_call(mid))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(rp_params[0][1])
        # market(tba/tbs)+position(borrow_shares) — из PRECONF pending (ТОТ ЖЕ источник, что sim) для консистентности.
        # fallback на rpc-latest при degraded preconf (теряем 1.8с lead, но не падаем).
        try:
            rp_state = aggregate3(preconf_rpc, [(MORPHO_BLUE, encode_market_call(mid)),
                                                (MORPHO_BLUE, encode_position_call(mid, borrower))],
                                  block_identifier="pending")
            _preconf_ok = rp_state[0][0] and rp_state[1][0]
        except Exception:
            _preconf_ok = False
        if _preconf_ok:
            m = decode_market(rp_state[0][1]); tba, tbs = m[2], m[3]
            borrow_shares, collateral_dbg = decode_position(rp_state[1][1])
            _snap_src = "preconf"
        else:
            rp_fb = aggregate3(rpc, [(MORPHO_BLUE, encode_market_call(mid)),
                                     (MORPHO_BLUE, encode_position_call(mid, borrower))])
            m = decode_market(rp_fb[0][1]); tba, tbs = m[2], m[3]
            borrow_shares, collateral_dbg = decode_position(rp_fb[1][1])
            _snap_src = "rpc-fallback"
        if borrow_shares == 0:
            return {"ok": False, "reason": "no debt (cleared)"}
        # ДИАГНОСТИКА расхождения: confirmed borrow_shares vs preconf — подтверждает рассогласование на флипе.
        try:
            _rp_conf = aggregate3(rpc, [(MORPHO_BLUE, encode_position_call(mid, borrower))])
            _bs_conf, _ = decode_position(_rp_conf[0][1])
            if _bs_conf != borrow_shares:
                log.info("DIAG divergence %s/%s src=%s preconf_bs=%d confirmed_bs=%d ratio=%.2f",
                         market_id[:10], borrower[:10], _snap_src, borrow_shares, _bs_conf,
                         (_bs_conf / borrow_shares if borrow_shares else 0))
        except Exception:
            pass

        repaid_shares = int(borrow_shares)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), int(price))   # (1) preconf price
        if seized == 0:
            log.info("DIAG seized=0 %s/%s repaid_shares=%d repaid_assets=%d collateral=%d tba=%d tbs=%d price=%d lltv=%d",
                     market_id[:10], borrower[:10], repaid_shares, repaid_assets, collateral_dbg, tba, tbs, int(price), lltv_wad)
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=slippage_bps)

        # GUARD свежести (skip-only): повторный preconf-read borrow_shares макс. близко к send.
        # Если долг УПАЛ ниже planned repaid -> SKIP (underflow неизбежен). НЕ капаем (seized/swap уже
        # посчитаны от repaid_shares -> кап = swap-mismatch -> SwapFailed). Либо planned как есть, либо skip.
        try:
            _rg = aggregate3(preconf_rpc, [(MORPHO_BLUE, encode_position_call(mid, borrower))],
                             block_identifier="pending")
            if _rg[0][0]:
                _fresh_bs, _ = decode_position(_rg[0][1])
                if _fresh_bs <= 0 or repaid_shares > _fresh_bs:
                    log.info("DIAG guard-skip %s/%s repaid=%d fresh_bs=%d (долг упал ниже planned -> underflow неизбежен)",
                             market_id[:10], borrower[:10], repaid_shares, _fresh_bs)
                    return {"ok": False, "reason": f"guard: debt dropped {repaid_shares}->{_fresh_bs}, would underflow"}
        except Exception:
            pass  # guard-read упал -> продолжаем (sim всё равно отловит underflow)

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0)
        sim = simulate_tx(preconf_rpc, liq, bot, cd0, block="pending")        # (2) preconf-pending gate
        if not sim["ok"]:
            # DIAG: логируем ВСЕ sim-revert'ы (не только Panic/seized) с бакетом причины + calldata+блок для форка.
            try:
                _diag_blk = rpc._web3().eth.block_number
            except Exception:
                _diag_blk = 0
            _diag_bucket = _diag_revert_bucket(str(sim.get("error", "")))
            log.info("DIAG sim-revert %s/%s bucket=%s err=%s | repaid_shares=%d repaid_assets=%d seized=%d collateral=%d tba=%d tbs=%d price=%d lltv=%d lif=%.4f block=%d cd=%s",
                     market_id[:10], borrower[:10], _diag_bucket, str(sim["error"])[:40], repaid_shares, repaid_assets, seized,
                     collateral_dbg, tba, tbs, int(price), lltv_wad, lif_from_lltv(lltv_wad / 10**18), _diag_blk, cd0.hex() if isinstance(cd0, (bytes, bytearray)) else str(cd0))
            return {"ok": False, "reason": f"preconf sim revert: {sim['error']}"}
        profit_wei = sim["profit"]
        profit_usd, cost_usd, net_usd = _net_gate(profit_wei, debt_usd, debt_assets, cfg)
        if net_usd < cfg.min_profit_usd:
            return {"ok": False, "net_usd": net_usd,
                    "reason": f"net ${net_usd:.2f} (profit ${profit_usd:.2f} - cost ${cost_usd:.2f}) < min ${cfg.min_profit_usd:.2f}"}

        min_profit_final = profit_wei * 95 // 100
        cd1 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], min_profit_final)
        return {"ok": True, "calldata": cd1, "net_usd": net_usd, "profit_usd": profit_usd, "cost_usd": cost_usd}
    except Exception as e:
        return {"ok": False, "reason": f"error: {type(e).__name__}: {str(e)[:120]}"}


# ---- per-transmit worker (reaction filter + kill-switch + dispatch) ----

def hot_min_repaid(cfg):
    return float(getattr(cfg, "hot_min_repaid_usd", 2000.0))


def _process_transmit(agg, block, subidx, t, *, rpc, preconf_rpc, cfg, feeds, meta, shared,
                      store, guard, alerter, log, last_price=None, stats=None,
                      price_fn=read_preconf_price, reads_fn=read_positions,
                      prepare_fn=prepare_hot, dispatch_fn=None):
    """For one detected transmit: per affected market, read the preconf price, recompute flips, keep only
    reaction prizes (repaid >= hot_min_repaid), check the SHARED kill-switch, prepare_hot, and dispatch
    the passers. Injectable seams (price_fn/reads_fn/prepare_fn/dispatch_fn) for unit tests. Returns the
    dispatch results (list) for logging/tests."""
    from strategy.guard import GuardState
    if dispatch_fn is None:
        from chain.execute import dispatch_liquidations as dispatch_fn  # noqa: F811
    floor = hot_min_repaid(cfg)
    # price-change gate: the bare match also fires on price-READ txs. Read ONE representative oracle
    # preconf price; if it has not moved since last sighting this was a reader, not a transmit -> skip
    # the 340-call multicall + flips. This is what makes the bare match viable on the hot path.
    if last_price is not None:
        rep = next((meta[m][0] for m in feeds.get(agg, []) if meta.get(m)), None)
        if rep is None:
            return []
        px = price_fn(preconf_rpc, rep)
        moved = _price_moved(agg, px, last_price)
        if stats is not None:                                # health buckets (none=preconf unreadable)
            stats["none" if px is None else ("proceed" if moved else "skip")] += 1
        if not moved:
            return []
    with shared.lock:
        groups = dict(shared.groups); debt_by = dict(shared.debt_by); debt_assets_by = dict(shared.debt_assets_by)

    ready, meta_by = [], {}
    for mid in feeds.get(agg, []):
        om = meta.get(mid)
        borrowers = groups.get(mid, [])
        if not om or not borrowers:
            continue
        oracle, lltv_wad = om
        price = price_fn(preconf_rpc, oracle)
        if price is None:
            continue
        rp = reads_fn(rpc, mid, borrowers)
        if not rp:
            continue
        tba, tbs, pos = rp
        positions = [(b, bs, col, debt_by.get((mid, b), 0.0)) for (b, bs, col) in pos]
        for (b, hr, du) in _flips(price, lltv_wad, tba, tbs, positions):
            if du < floor:                                   # reaction filter: $2k+ only
                continue
            prep = prepare_fn(rpc, preconf_rpc, cfg, mid, b, du,
                              debt_assets_by.get((mid, b), 0), price)
            if prep.get("ok"):
                ready.append((mid, b, prep)); meta_by[(mid, b)] = (hr, du)
                log.info("HOT ready %s/%s hf=%.4f repaid~$%.0f net=$%.2f block=%d subidx=%s",
                         mid[:10], b[:10], hr.hf, du, prep["net_usd"], block, subidx)
            else:
                log.info("HOT skip %s/%s: %s", mid[:10], b[:10], str(prep.get("reason"))[:80])

    if not ready:
        return []

    today = store.realized_today()                           # SHARED kill-switch with the block loop
    blocked = guard.blocked_reason(GuardState(realized_net_today=today["net"],
                                              gas_spent_today=today["gas"], inflight=0))
    if blocked:
        log.error("HOT kill switch engaged: %s — %d ready NOT dispatched", blocked, len(ready))
        alerter.send(f"\U0001F6D1 hot path halted: {blocked}", key="hot-killswitch")
        return []

    results = dispatch_fn(rpc, cfg, ready, log=log, max_inflight=cfg.max_inflight)
    for r in results:
        hr, du = meta_by.get((r["market_id"], r["borrower"]), (None, 0.0))
        status = (f"submitted:{r.get('status')} net${r.get('net_usd',0):.2f}" if r["sent"]
                  else f"skip:{str(r.get('reason'))[:50]}")
        store.log_action(market_id=r["market_id"], borrower=r["borrower"], mode=cfg.mode,
                         tx_hash=r.get("hash"), net_usd=r.get("net_usd", 0.0), gas_usd=r.get("gas_usd", 0.0), status=status)
        log.info("HOT ACTIONABLE %s/%s status=%s tx=%s", r["market_id"][:10], r["borrower"][:10], status, r.get("hash"))
        alerter.send(f"\U0001F525 HOT {cfg.mode} {r['borrower'][:10]} {status}", key=f"hot:{r['market_id']}:{r['borrower']}")
    return results


# ---- async flashblock subscriber (detect + spawn) ----

async def hot_loop(rpc, preconf_rpc, cfg, shared, store, guard, alerter, log, stop, markets,
                   ws_factory=None):
    """Subscribe to Flashblocks; on a PRECISE transmit to one of our aggregators, spawn a worker thread
    that runs _process_transmit (the recv loop itself does no slow work). Re-resolves feeds/meta on a
    cadence (rotation). DORMANT until block_driven_loop starts this behind cfg.hot_path."""
    import asyncio
    import json
    import websockets
    import brotli
    from chain.feeds import resolve_feeds, extract_txs, block_number_of

    def _default_ws():
        return websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None)
    ws_factory = ws_factory or _default_ws

    feeds = resolve_feeds(rpc, markets)
    meta = resolve_meta(rpc, markets)
    agg_set = set(feeds)
    last_resolve = time.monotonic()
    log.info("hot path: %d aggregators / %d markets (preconf=%s, tip=%.1f gwei, floor repaid $%.0f)",
             len(agg_set), len(meta), preconf_rpc.rpc_url, cfg.tip_gwei, hot_min_repaid(cfg))

    last_spawn: dict = {}        # agg -> last spawn time.time() (throttle; survives ws reconnects)
    last_price: dict = {}        # agg -> last preconf price of representative oracle (change-gate)
    stats = {"spawn": 0, "skip": 0, "proceed": 0, "none": 0,    # hot-path health; emitted every HOT_STATS_SEC
             "poll": 0, "pspawn": 0, "poll_none": 0, "poll_fb": 0}  # poll_fb=oracles filled from latest fallback
    last_stats = time.monotonic()
    poll_seen: dict = {}         # oracle -> last preconf price (poll-local; baseline -> spawn only on move)
    last_poll_block = None       # one preconf-price poll per block
    fb_windows = 0               # consecutive stats windows with poll_fb>0 (preconf degradation streak)
    last_fb_alert = 0.0          # monotonic of last degradation alert (anti-flood)
    while not stop.is_set():
        try:
            async with ws_factory() as ws:
                while not stop.is_set():
                    if time.monotonic() - last_resolve >= cfg.rescan_interval_sec:
                        try:
                            feeds = resolve_feeds(rpc, markets); meta = resolve_meta(rpc, markets)
                            agg_set = set(feeds); last_resolve = time.monotonic()
                        except Exception:
                            pass
                    if time.monotonic() - last_stats >= HOT_STATS_SEC:
                        log.info("hot stats %ds: spawn=%d pspawn=%d poll=%d | gate proceed=%d skip=%d none=%d poll_none=%d poll_fb=%d",
                                 int(time.monotonic() - last_stats), stats["spawn"], stats["pspawn"], stats["poll"],
                                 stats["proceed"], stats["skip"], stats["none"], stats["poll_none"], stats["poll_fb"])
                        fb_windows = fb_windows + 1 if stats["poll_fb"] > 0 else 0
                        if fb_windows >= 3 and time.monotonic() - last_fb_alert > 1800:
                            last_fb_alert = time.monotonic()
                            try:
                                alerter.send("preconf degraded %dx windows: polling on LATEST (detection alive, "
                                             "but WITHOUT the ~1.8s lead -> contested races lost). check preconf RPC." % fb_windows)
                            except Exception:
                                pass
                        for _k in list(stats):
                            stats[_k] = 0
                        last_stats = time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                        d = json.loads(txt)
                    except Exception:
                        continue
                    bn = block_number_of(d)
                    if bn is None:
                        continue
                    if bn != last_poll_block:                 # one preconf-price poll per block (money markets)
                        last_poll_block = bn
                        threading.Thread(target=_poll_prices, daemon=True,
                                         kwargs=dict(preconf_rpc=preconf_rpc, rpc_fallback=rpc, feeds=feeds, meta=meta,
                                                     poll_seen=poll_seen, stats=stats, bn=bn, log=log,
                                                     spawn_kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                                       feeds=feeds, meta=meta, shared=shared,
                                                                       store=store, guard=guard, alerter=alerter,
                                                                       log=log, last_price=last_price, stats=stats))).start()
                    idx = d.get("index"); txs = extract_txs(d); now = time.time()
                    for agg in agg_set:                          # BARE match: ANY mention of the aggregator
                        if now - last_spawn.get(agg, 0.0) < HOT_THROTTLE_SEC:
                            continue                             # per-agg throttle: bound preconf reads/spawns
                        if any(raw_ and (agg[2:] in raw_) for raw_, _ in txs):
                            last_spawn[agg] = now
                            stats["spawn"] += 1
                            threading.Thread(target=_process_transmit, args=(agg, bn, idx, now),
                                             kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                         feeds=feeds, meta=meta, shared=shared, store=store,
                                                         guard=guard, alerter=alerter, log=log,
                                                         last_price=last_price, stats=stats),
                                             daemon=True).start()
        except Exception as e:
            log.warning("hot path ws error (%s) — reconnecting in 3s", type(e).__name__)
            alerter.send("\U0001F6A8 hot-path reconnect — check journalctl", key="hot-wsserr")
            await asyncio.sleep(3)
