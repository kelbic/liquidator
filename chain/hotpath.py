"""Hot path (Phase 2, PRECONF-triggered). A SECOND trigger beside the block loop: on a real `transmit`
to one of our aggregators, recompute the affected candidates' HF on the PRE-CONFIRMED price, and for a
position that flips liquidatable + passes the preconf-pending sim + the net floor + the narrow reaction
filter (`hot_min_repaid_usd`, see config.py for the live value — this was hardcoded as "$2k+" here and
went stale when the floor was lowered to $450), prepare and dispatch a liquidation. Reuses the
battle-tested prepare/dispatch building blocks; ONLY the sourcing (preconf price for sizing +
preconf-pending sim) and the trigger differ.

Latency shape: the async recv loop only DETECTS a transmit (precise 94-form match) and spawns a worker
thread; all slow work (preconf read -> flip -> KyberSwap quote -> sim -> dispatch) runs off the recv
loop so detection stays responsive. Cross-path nonce safety (this path + the block loop both send from
one wallet) is handled by the send-lock inside dispatch_liquidations (added in C2).

DORMANT until wired into block_driven_loop behind cfg.hot_path. No behavior change to the existing loop.
"""
from __future__ import annotations
import logging
import threading
import time

log = logging.getLogger(__name__)

MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
FB_URL = "wss://mainnet.flashblocks.base.org/ws"
PRECONF_RPC_DEFAULT = "https://mainnet-preconf.base.org"
HOT_THROTTLE_SEC = 0.25   # min seconds between hot-path spawns PER aggregator. The bare match fires on
                          # frequent price-READ txs; this bounds preconf-RPC reads + thread spawns.
                          # Transmit cadence is minutes, so 0.25s loses no real update; raise if the
                          # public preconf endpoint rate-limits (a throttled/None read simply skips).
HOT_STATS_SEC = 120       # emit hot-path counters (spawn + gate outcomes) every N seconds. Lets us see
                          # detection is ALIVE without waiting for a flip (the gate is silent on readers).

# 2a: idToMarketParams is immutable per market for its whole life. resolve_meta() already decodes
# it fully but only kept (oracle, lltv); this cache keeps loan/coll/irm too so prepare_hot can skip
# its own confirmed-RPC re-fetch of the same data. Module-level by design (not threaded through
# meta/_process_transmit/kwargs) to avoid touching the existing meta shape used by the gate-check
# and _process_transmit's unpacking — zero blast radius outside resolve_meta/prepare_hot.
_FULL_PARAMS_CACHE: dict = {}


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


def read_positions(rpc, mid, borrowers, preconf_rpc=None):
    """market totals + each position -> (tba, tbs, [(borrower, bs, col)]). ONE aggregate3.
    Reads on preconf-pending (same source as the flip price + sim) when preconf_rpc is given, so the
    reaction flip is visible ~1.8s before confirmed; falls back to rpc-latest on degraded preconf."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    b32 = rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))]
    for b in borrowers:
        calls.append((MORPHO_BLUE, encode_position_call(b32, b)))
    res = None
    if preconf_rpc is not None:
        try:
            r = aggregate3(preconf_rpc, calls, block_identifier="pending")
            if r and r[0][0] and r[0][1]:
                res = r
        except Exception:
            res = None
    if res is None:
        res = aggregate3(rpc, calls)
    if not res or not res[0][0] or not res[0][1]:
        return None
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    pos = []
    for b, (ok, data) in zip(borrowers, res[1:]):
        if ok and data:
            bs, col = decode_position(data)
            pos.append((b, bs, col))
        else:
            log.info("DIAG read_positions drop %s/%s ok=%s data_len=%d",
                     mid[:10], b[:10], ok, len(data) if data else 0)
    return tba, tbs, pos


# ---- Layer 2 (Идея 2): continuous background position cache, money-markets only ----
# Scope: только рынки из execute.UNIV3_PATHS (валидированный сустейном масштаб — один рынок,
# 340 вызовов/блок; полный охват всех ~40 рынков и поведение ПОД каскадом НЕ провалидированы,
# см. STATE.md "РАЗБИВКА — симметрия с декодером ЧАСТИЧНАЯ"). Любой рынок вне кэша падает в
# read_positions без изменений — нулевой риск для непокрытых рынков.

_POSITION_CACHE_LOCK = threading.Lock()
_POSITION_CACHE: dict = {}  # {market_id: (tba, tbs, {borrower: (bs, col, last_ok_ts)})}
_POSITION_CACHE_MAX_AGE_SEC = 6.0  # ~3 cycles at the observed dt=1.9-2.4s/5-market cycle


def refresh_position_cache(preconf_rpc, mid, borrowers):
    """Background per-block refresh for ONE money-market. A borrower whose position() call in the
    batch comes back ok=False/empty is logged (refresh-miss) but their PREVIOUS cache entry (if any)
    is left untouched — not wiped, not refreshed. Freshness is enforced downstream by age at read
    time (read_positions_cached), not by dropping on a single missed cycle."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    if not borrowers:
        return
    b32 = preconf_rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))] + [(MORPHO_BLUE, encode_position_call(b32, b)) for b in borrowers]
    try:
        res = aggregate3(preconf_rpc, calls, block_identifier="pending")
    except Exception as e:
        log.info("DIAG position-cache refresh-fail market=%s err=%s", mid[:10], str(e)[:80])
        return
    if not res or not res[0][0] or not res[0][1]:
        log.info("DIAG position-cache refresh-fail market=%s reason=market-call-failed", mid[:10])
        return
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    now = time.time()
    with _POSITION_CACHE_LOCK:
        prev = _POSITION_CACHE.get(mid)
        positions = dict(prev[2]) if prev else {}
        for b, (ok, data) in zip(borrowers, res[1:]):
            if ok and data:
                bs, col = decode_position(data)
                positions[b] = (bs, col, now)
            else:
                log.info("DIAG position-cache refresh-miss market=%s borrower=%s ok=%s", mid[:10], b[:10], ok)
        _POSITION_CACHE[mid] = (tba, tbs, positions)


def read_positions_cached(rpc, mid, borrowers, preconf_rpc=None):
    """Drop-in for read_positions (same signature — swaps in via _process_transmit's reads_fn=
    default, no call-site change needed). Market not yet in _POSITION_CACHE (not money-market-
    covered, or not refreshed yet) -> unchanged fallback to the original live read_positions.
    Market IS cached: a borrower's entry younger than _POSITION_CACHE_MAX_AGE_SEC is served from
    cache (zero RTT); a borrower missing entirely, or present but older than the threshold, is
    EXCLUDED from this transmit's candidates + logged (A-with-age-bound: trust recent cache,
    never act on data past the freshness window), everyone else in the same market is served
    normally, zero RTT."""
    with _POSITION_CACHE_LOCK:
        cached = _POSITION_CACHE.get(mid)
    if cached is None:
        return read_positions(rpc, mid, borrowers, preconf_rpc=preconf_rpc)
    tba, tbs, positions = cached
    now = time.time()
    pos = []
    for b in borrowers:
        p = positions.get(b)
        if p is None:
            log.info("DIAG position-cache stale-skip market=%s borrower=%s reason=never-seen", mid[:10], b[:10])
            continue
        bs, col, last_ok_ts = p
        age = now - last_ok_ts
        if age <= _POSITION_CACHE_MAX_AGE_SEC:
            pos.append((b, bs, col))
        else:
            log.info("DIAG position-cache stale-skip market=%s borrower=%s reason=too-old age=%.1fs",
                     mid[:10], b[:10], age)
    return tba, tbs, pos


def _refresh_position_caches(*, preconf_rpc, shared, bn):
    """Per-block driver (Layer 2, measurement phase — see STATE.md 'C temporary'). Refreshes
    _POSITION_CACHE for every money-market (UNIV3_PATHS collateral) via refresh_position_cache.
    PURE MEASUREMENT: writes _POSITION_CACHE and logs refresh-miss/-fail + cycle timing, but does
    NOT touch reads_fn/_process_transmit — the armed hot path keeps reading via the original live
    read_positions, completely unchanged, until reads_fn is explicitly swapped (deliberately not
    done yet). One market's decode/network hiccup does not stop the cycle for the others."""
    from chain.execute import UNIV3_PATHS
    with shared.lock:
        groups = dict(shared.groups)
    _t0 = time.time()
    n_markets = 0
    for mid, borrowers in groups.items():
        params = _FULL_PARAMS_CACHE.get(mid)
        if not params:
            continue
        _loan, coll, _oracle, _irm, _lltv = params
        if coll.lower() not in UNIV3_PATHS:
            continue
        try:
            refresh_position_cache(preconf_rpc, mid, borrowers)
            n_markets += 1
        except Exception:
            log.exception("position-cache refresh error market=%s", mid[:10])
    if n_markets:
        log.info("DIAG position-cache cycle markets=%d dt=%.1fs block=%s", n_markets, time.time() - _t0, bn)


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
    """{mid: (oracle, lltv_wad)} via ONE aggregate3 of idToMarketParams (immutable). Also fills
    _FULL_PARAMS_CACHE (loan/coll/irm too) so prepare_hot can skip its own params re-fetch (2a)."""
    from chain.multicall import aggregate3, encode_id_to_market_params_call, decode_id_to_market_params
    mids = [m.market_id for m in markets if m.market_id]
    calls = [(MORPHO_BLUE, encode_id_to_market_params_call(rpc.to_bytes32(mid))) for mid in mids]
    out = {}
    for mid, (ok, data) in zip(mids, aggregate3(rpc, calls)):
        if ok and data:
            loan, coll, oracle, irm, lltv = decode_id_to_market_params(data)
            out[mid] = (oracle, lltv)
            _FULL_PARAMS_CACHE[mid] = (loan, coll, oracle, irm, lltv)
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
    """Mirror of execute.prepare_liquidation, PRECONF-sourced — diverges from it in more than the two
    points below; this is illustrative, not a complete list (params sourcing, swap routing, and extra
    diagnostic/guard reads have also diverged — check the function body, not this docstring, for the
    current full set):
      (1) the seize is sized on the PRE-CONFIRMED `price` (passed in, read once by the caller), not a
          fresh latest read — so `expected_seized` matches what the contract seizes at execution;
      (2) the simulate is gated against PRECONF-PENDING (simulate_tx(preconf_rpc, ..., block='pending'))
          where the position is already liquidatable, instead of latest (still healthy pre-confirm).
    Same {ok, ...} shape as the armed prepare."""
    try:
        from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
            encode_market_call, decode_market, encode_position_call, decode_position)
        from chain.simulate import to_assets_up
        from chain.execute import kyber_swap, univ3_swap, encode_liquidate, expected_seized, simulate_tx
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = w3.eth.account.from_key(cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        # market-PARAMS (статичные адреса рынка, иммутабельны) — из кэша resolve_meta (2a), без RTT.
        # Fallback на confirmed-RPC fetch, если кэш ещё не наполнен (до первого resolve_meta).
        _cached = _FULL_PARAMS_CACHE.get(market_id)
        if _cached:
            loan, coll, oracle, irm, lltv_wad = _cached
        else:
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
        swap = univ3_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
        if swap is None:                       # market outside UniV3 paths -> Kyber fallback (unchanged)
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
    _t_read_total = 0.0; _t_prep_total = 0.0   # DIAG latency: read=340-call, sim=prepare_hot
    for mid in feeds.get(agg, []):
        om = meta.get(mid)
        borrowers = groups.get(mid, [])
        if not om or not borrowers:
            if stats is not None: stats["f_nocand"] += 1
            continue
        oracle, lltv_wad = om
        _pxs = time.perf_counter()
        price = price_fn(preconf_rpc, oracle)
        _t_price_last = (time.perf_counter() - _pxs) * 1000.0
        if price is None:
            if stats is not None: stats["f_noprice"] += 1
            continue
        _rs = time.perf_counter()
        rp = reads_fn(rpc, mid, borrowers, preconf_rpc=preconf_rpc)
        _t_read_last = (time.perf_counter() - _rs) * 1000.0; _t_read_total += _t_read_last
        if not rp:
            if stats is not None: stats["f_norp"] += 1
            continue
        tba, tbs, pos = rp
        positions = [(b, bs, col, debt_by.get((mid, b), 0.0)) for (b, bs, col) in pos]
        flipped = _flips(price, lltv_wad, tba, tbs, positions)
        if not flipped:
            if stats is not None: stats["f_noflip"] += 1
            if positions:                                    # DIAG: what state did we read? (preconf-source check)
                from chain.simulate import MarketContext as _MC, health_from as _hf
                _ctx = _MC(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                           total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
                _c = [(bb, bs, _hf(_ctx, bs, col).hf) for (bb, bs, col, _du) in positions]
                _bb, _bs, _hv = min(_c, key=lambda x: x[2])
                log.info("DIAG noflip %s/%s minHF=%.4f bshares=%d px=%d n=%d price=%.0fms read=%.0fms",
                         mid[:10], _bb[:10], _hv, _bs, int(price), len(_c), _t_price_last, _t_read_last)
        for (b, hr, du) in flipped:
            if du < floor:                                   # reaction filter: $2k+ only
                if stats is not None: stats["f_floor"] += 1
                continue
            if stats is not None: stats["f_prep"] += 1
            _ps = time.perf_counter()
            prep = prepare_fn(rpc, preconf_rpc, cfg, mid, b, du,
                              debt_assets_by.get((mid, b), 0), price)
            _t_prep_total += (time.perf_counter() - _ps) * 1000.0
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

    _ds = time.perf_counter()
    results = dispatch_fn(rpc, cfg, ready, log=log, max_inflight=cfg.max_inflight)
    _t_send = (time.perf_counter() - _ds) * 1000.0
    log.info("DIAG timing read=%.0fms sim=%.0fms send=%.0fms n_ready=%d",
             _t_read_total, _t_prep_total, _t_send, len(ready))
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
             "poll": 0, "pspawn": 0, "poll_none": 0, "poll_fb": 0,  # poll_fb=oracles filled from latest fallback
             "f_nocand": 0, "f_noprice": 0, "f_norp": 0, "f_noflip": 0, "f_floor": 0, "f_prep": 0}  # funnel proceed->prepare_hot
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
                        log.info("hot stats %ds: spawn=%d pspawn=%d poll=%d | gate proceed=%d skip=%d none=%d poll_none=%d poll_fb=%d | funnel nocand=%d noprice=%d norp=%d noflip=%d floor=%d prep=%d",
                                 int(time.monotonic() - last_stats), stats["spawn"], stats["pspawn"], stats["poll"],
                                 stats["proceed"], stats["skip"], stats["none"], stats["poll_none"], stats["poll_fb"],
                                 stats["f_nocand"], stats["f_noprice"], stats["f_norp"], stats["f_noflip"], stats["f_floor"], stats["f_prep"])
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
                        # Layer 2, measurement phase (C temporary — see STATE.md): fills _POSITION_CACHE +
                        # logs refresh-miss/cycle timing. Does NOT touch reads_fn — read_positions_cached
                        # is not wired into _process_transmit yet, so this has zero effect on hot-path behavior.
                        threading.Thread(target=_refresh_position_caches, daemon=True,
                                         kwargs=dict(preconf_rpc=preconf_rpc, shared=shared, bn=bn)).start()
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
