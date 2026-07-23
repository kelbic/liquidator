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
import os
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

# Ground-truth полного горячего набора (A3): journal получает только счётчик (hotset n=.. src=..),
# сам набор (borrower:bs пары) пишется сюда — сводить по адресу ликвидированного на каскаде разводит
# L (в дампе с bs=0) / B (отсутствует) / A (ok=False в drop-логе) / A' (src=confirmed). Дёшево на диск,
# не топит journal в каскаде (pspawn до 145). Ротацию файла — на владельце (logrotate/ручной rm).
_HOTSET_LOG_PATH = "/root/liquidator/hotset.log"


def _dump_hotset(mid, src, tba, tbs, pos, dropped):
    """Append одной строки: block-набор borrower:bs (+ dropped ok=False адреса). Best-effort, не
    роняет горячий путь при ошибке записи (диск полон/права)."""
    try:
        parts = " ".join("%s:%d" % (b[:10], bs) for (b, bs, _col) in pos)
        drp = (" DROPPED=" + ",".join(d[:10] for d in dropped)) if dropped else ""
        with open(_HOTSET_LOG_PATH, "a") as _f:
            _f.write("%.3f mid=%s src=%s n=%d tba=%d tbs=%d | %s%s\n"
                     % (time.time(), mid[:10], src, len(pos), tba, tbs, parts, drp))
    except Exception:
        pass


# ---- pure helpers (unit-tested) ----

_BF_HIST: dict = {}          # bn -> fb-baseFee (последние ~16 блоков)
_BF_LAST_LOG = 0.0           # троттл лога 60с
_BF_GAPS = 0                 # разрывы bn между fb-записями с последнего лога (= пропуски index-0)
_BF_PREV_BN = None
_BF_LATEST = None            # (bn, fb_wei, monotonic_ts) — последний index-0 baseFee (SEND-2)


def _fresh_basefee(max_age_sec=6.0):
    """SEND-2 гард свежести: fb-baseFee, если последний index-0 кадр не старше 3 блоков (6с),
    иначе None -> prep не несёт fee -> send_tx идёт старым Alchemy-путём. Защита от «фид жив,
    кадры не идут» (gap60s-класс: 0.017% в покое, каскад неизвестен — watch-item). Пишет слот
    фид-петля, читает тред prepare_hot: подмена кортежа атомарна (GIL), гонка максимум на один
    кадр ~200мс — гард переваривает."""
    if _BF_LATEST is None:
        return None
    _bn, fb, ts = _BF_LATEST
    if time.monotonic() - ts > max_age_sec:
        return None
    return fb


def _basefee_probe(*, bn, fb, rpc, log):
    """5a (LOG-ONLY, перед SEND-2): fb-baseFee из flashblock-хедера vs Alchemy get_block(N) по
    НОМЕРУ блока (лаг 3 — блок к этому моменту подтверждён) — тот же аксессор, что в send_tx
    (execute.py get_block().baseFeePerGas). SEND-2 ПЕРЕНОСИТ send-путь на fb-источник; этот замер
    решает: чисто (eq=True, gap~0) -> SEND-2 как спроектирован; иначе -> fallback max(fb,last_alch)
    или fb x1.25. gap60s = пропущенные index-0 кадры (недоступность источника). Вызывается только
    из фид-петли (один тред) -> глобалы без лока; сравнение — в daemon-треде (Alchemy-RTT вне петли).
    После разрыва bn лог откладывается, пока tgt в дыре (счётчик gap не теряется) — юнит."""
    global _BF_LAST_LOG, _BF_GAPS, _BF_PREV_BN, _BF_LATEST
    if _BF_PREV_BN is not None and bn > _BF_PREV_BN + 1:
        _BF_GAPS += bn - _BF_PREV_BN - 1
    _BF_PREV_BN = bn
    _BF_HIST[bn] = fb
    _BF_LATEST = (bn, fb, time.monotonic())
    for k in [k for k in _BF_HIST if k < bn - 16]:
        _BF_HIST.pop(k, None)
    now = time.monotonic()
    if now - _BF_LAST_LOG < 60.0:
        return
    tgt = bn - 3
    fb_t = _BF_HIST.get(tgt)
    if fb_t is None:                 # прогрев (<3 блоков) / пропуск tgt: троттл не жжём, повтор блоком позже
        return
    _BF_LAST_LOG = now
    gaps = _BF_GAPS; _BF_GAPS = 0
    def _cmp():
        alch = None
        try:
            alch = int(rpc._web3().eth.get_block(tgt).get("baseFeePerGas") or 0)
        except Exception:
            pass
        log.info("DIAG basefee bn=%d fb=%s alch=%s eq=%s gap60s=%d",
                 tgt, fb_t, alch, (fb_t is not None and alch is not None and fb_t == alch), gaps)
    threading.Thread(target=_cmp, daemon=True).start()


_SHADOW_SCALE = {"cbXRP": 10**28, "cbDOGE": 10**26, "cbADA": 10**28,   # immutable оракулов (STATE Идея 2)
                 "cbLTC": 10**26,   # P1-8, ИСПРАВЛЕНО 07-08: STATE-строка «цена cbADA/cbLTC ×1e28» была
                                    # НЕВЕРНА для cbLTC — живой shadow-чек дал match=False ровно 100×
                                    # (px_preconf/answer=1e26), decimals с чейна: cbLTC=8 (как cbDOGE→1e26),
                                    # cbADA=6→1e28. Формула: 1e36·10^(loanDec−collDec)/1e8_answer.
                 "cbBTC": 10**26}   # CBBTC_PLAN 07-09: SCALE_FACTOR оракула = 1e26, подтверждён БИТ-В-БИТ
                                    # (answer*1e26 == price(), остаток 0). Гейт: первый live shadow-чек
                                    # match=False -> убрать строку немедленно (poll-путь безопасен без неё).


def _build_shadow_map(feeds, meta, markets, primary=None):
    """{aggregator_lower: (symbol, scale, oracle)} ТОЛЬКО для одно-фид money-маркетов (SCALE известен).
    Чистая (юнит). Ротацию агрегатора закрывает часовой re-resolve, пересобирающий карту вместе с
    feeds/meta; между ротацией и пересборкой shadow просто перестаёт матчить (log-only, безвредно).
    CBBTC_PLAN: ТОЛЬКО loan=USDC — _SHADOW_SCALE выведен для USD-quote (answer*scale==price
    USD-оракула); один агрегатор могут делить рынки с РАЗНЫМ quote (cbBTC/USDC и cbBTC/EURC оба
    резолвятся в cbBTC/USD-фид), и посадить USD-scale на EURC-оракул = неверный px_hint +
    вечный match=False. Не-USDC рынки остаются на poll-детекции (как и было).
    07-23 multi-leg: рынок числится и под extra-ногами (BF2/QF1/QF2) — membership под ЧУЖИМ agg
    не должен получать наш _SHADOW_SCALE (cbBTC-scale на ожившей USDC/USD-ноге в депег = ложный
    px_hint в feed-спавн). primary = feeds.primary_map() ({mid: BF1-agg}); None = до-multi-leg
    поведение (тесты/офлайн)."""
    out = {}
    by_id = {m.market_id: m for m in markets}
    for agg, mids in feeds.items():
        for mid in mids:
            if primary is not None and primary.get(mid) != agg:
                continue                     # extra-нога: scale чужого BF1-фида сюда не сажаем
            m = by_id.get(mid); om = meta.get(mid)
            if not m or not om:
                continue
            if getattr(m, "loan_token", "") != "USDC":
                continue
            scale = _SHADOW_SCALE.get(getattr(m, "collateral_token", None))
            if scale:
                out[agg] = (m.collateral_token, scale, om[0])
                break
    return out


def _shadow_feed_check(*, sym, agg, answer, scale, oracle, preconf_rpc, price_fn, t0, log):
    """SHADOW слоя 1 (план п.4): цена, декодированная ИЗ ФИДА (~0.7мс), против цены оракула на
    preconf (~350мс RTT) на том же транзите. ЛОГ-ONLY — не действует, ничего не спавнит дальше.
    px_preconf изредка может быть СТАРОЙ ценой (чтение обогнало применение pending) — benign-гонка,
    НЕ ошибка декодера; ошибка декодера = px_feed не равен НИ старой, НИ новой цене оракула.
    Правило флипа PRICE_FROM_FEED — в STATE (Идея 2, слой 1)."""
    px_feed = answer * scale
    px_pre = None
    try:
        px_pre = price_fn(preconf_rpc, oracle)
    except Exception:
        px_pre = None
    log.info("DIAG shadow-feed mkt=%s agg=%s answer=%d px_feed=%d px_preconf=%s match=%s dt=%.0fms",
             sym, agg[:10], answer, px_feed, px_pre,
             px_pre is not None and px_pre == px_feed, (time.perf_counter() - t0) * 1000.0)


def _feed_spawn_gate(oracle, bn, feed_handled):
    """PURE (юнит): ровно ОДИН feed-спавн на (oracle, bn). Тот же transmit-tx появляется в
    нескольких флэшблок-суб-кадрах -> shadow-декод раньше спавнил _process_transmit на каждом
    (наблюдалось 2 спавна с dt=3мс, 07-08 20:18). До fast-lane это смягчал sim; без sim каждый
    реальный флип давал бы 2-3 слепые отправки. Возврат True = спавнить (и (oracle,bn) помечен);
    False = дубль, не спавнить. feed_handled чистится на смене блока (как раньше)."""
    if (oracle, bn) in feed_handled:
        return False
    feed_handled.add((oracle, bn))
    return True


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


def dynamic_tip_gwei(cfg, debt_usd):
    """PURE (юнит): tip по размеру приза. FCFS-рутина позицией за tip не платится (канарейка
    23.07: +7..+36 in-block на 0.05 gwei), но в каскадную ДАВКУ победители бидят 22-59 gwei
    (фэт-разведка 45д, блоки 47803xxx: один оператор 56.7 gwei -> idx 1154 против своих же
    0.0125 -> idx 1171) — похоже на fee-сортировку ВНУТРИ флешблок-кадра при заторе. Страховка
    стоит ~$1.9/gwei на 500k газа — окупается только на крупном призе, отсюда ступени."""
    du = float(debt_usd or 0.0)
    if du >= getattr(cfg, "tip_big_repaid_usd", 20000.0):
        return float(getattr(cfg, "tip_big_gwei", 30.0))
    if du >= getattr(cfg, "tip_mid_repaid_usd", 2000.0):
        return float(getattr(cfg, "tip_mid_gwei", 2.0))
    return float(cfg.tip_gwei)


def _net_gate(profit_wei, debt_usd, debt_assets, cfg):
    """PURE: profit/cost/net in USD — как floor в prepare_liquidation, но cost по ДИНАМИЧЕСКОМУ
    tip: крупный приз платит страховой tip, и гейт обязан видеть эту цену."""
    profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
    cost_usd = cfg.gas_limit_est * dynamic_tip_gwei(cfg, debt_usd) * cfg.eth_price_usd / 1e9
    return profit_usd, cost_usd, profit_usd - cost_usd


# ---- on-chain reads ----

def read_preconf_price(preconf_rpc, oracle):
    """oracle.price() at block='pending' on the preconf RPC = the pre-confirmed Morpho price. None on fail."""
    from chain.simulate import ORACLE_ABI
    try:
        return int(preconf_rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending"))
    except Exception:
        return None


_PX_HEDGE_SEC = float(os.environ.get("PX_HEDGE_TIMEOUT_SEC", "0.4"))


def read_price_hedged(preconf_rpc, rpc, oracle, price_fn=read_preconf_price):
    """Хедж-чтение цены оракула: preconf-pending (лид ~1.8с) и confirmed стартуют ПАРАЛЛЕЛЬНО,
    preconf ждём не дольше _PX_HEDGE_SEC. Штатно preconf отвечает ~100мс -> берём его значение,
    поведение прежнее. В деградацию (429-trickle ~2.05с; канарейка 23.07 probe dt_spawn=2267мс
    БЕЗ hotset-строк = гейт/префетч висели на цене, все рынки в f_noprice) отдаём confirmed:
    лид теряем, детект/гонку/тред — нет. Старая цена с confirmed безопасна: нет флипа -> skip
    (как раньше при None), флип и по старой -> жертва легитимна. Оба легга через price_fn."""
    if rpc is None:
        return price_fn(preconf_rpc, oracle)
    ev_p, box_p = threading.Event(), []
    ev_c, box_c = threading.Event(), []

    def _leg(r, ev, box):
        try:
            box.append(price_fn(r, oracle))
        except Exception:
            pass
        finally:
            ev.set()

    threading.Thread(target=_leg, args=(preconf_rpc, ev_p, box_p), daemon=True).start()
    threading.Thread(target=_leg, args=(rpc, ev_c, box_c), daemon=True).start()
    ev_p.wait(timeout=_PX_HEDGE_SEC)
    if box_p and box_p[0] is not None:
        return box_p[0]
    ev_c.wait(timeout=2.0)
    if box_c and box_c[0] is not None:
        return box_c[0]
    return box_p[0] if box_p else None      # confirmed молчит -> preconf мог дозреть после капа


def read_positions(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
    """market totals + each position -> (tba, tbs, [(borrower, bs, col)]). ONE aggregate3.
    Reads on preconf-pending (same source as the flip price + sim) when preconf_rpc is given, so the
    reaction flip is visible ~1.8s before confirmed; falls back to rpc-latest on degraded preconf.
    src_out (opt. mutable dict): records which source served the read ('preconf'/'confirmed') so the
    caller can flag A' (silent confirmed-fallback -> stale debt -> false flip). Return shape unchanged."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    b32 = rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))]
    for b in borrowers:
        calls.append((MORPHO_BLUE, encode_position_call(b32, b)))
    res = None; _src = "confirmed"
    if preconf_rpc is not None:
        try:
            r = aggregate3(preconf_rpc, calls, block_identifier="pending")
            if r and r[0][0] and r[0][1]:
                res = r; _src = "preconf"
        except Exception:
            res = None
    if res is None:
        try:
            res = aggregate3(rpc, calls)                      # A': silent confirmed-fallback (src=confirmed below)
        except Exception as e:
            # 07-23: 429-шторм на confirmed убивал ВЕСЬ transmit-воркер необработанным HTTPError
            # (preconf-ветка выше обёрнута, фолбэк — не был) = потерянные рынки транзита в каскад.
            # None -> вызывающий считает f_norp и продолжает соседей (как при пустом ответе).
            log.info("DIAG read_positions fallback-fail market=%s err=%s", mid[:10], str(e)[:60])
            return None
    if src_out is not None:
        src_out["src"] = _src
    if not res or not res[0][0] or not res[0][1]:
        return None
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    pos = []; _dropped = []
    for b, (ok, data) in zip(borrowers, res[1:]):
        if ok and data:
            bs, col = decode_position(data)
            pos.append((b, bs, col))
        else:
            _dropped.append(b)
            log.info("DIAG read_positions drop %s/%s ok=%s data_len=%d",
                     mid[:10], b[:10], ok, len(data) if data else 0)
    _dump_hotset(mid, _src, tba, tbs, pos, _dropped)          # A3: full set to disk, counter to journal
    return tba, tbs, pos


# ---- Layer 2 (Идея 2): continuous background position cache, money-markets only ----
# Scope: только рынки из execute.ROUTE_COLLATERALS (P1-8: +cbLTC/cbBTC; сустейн валидирован на одном рынке,
# 340 вызовов/блок; полный охват всех ~40 рынков и поведение ПОД каскадом НЕ провалидированы,
# см. STATE.md "РАЗБИВКА — симметрия с декодером ЧАСТИЧНАЯ"). Любой рынок вне кэша падает в
# read_positions без изменений — нулевой риск для непокрытых рынков.

_POSITION_CACHE_LOCK = threading.Lock()
_POSITION_CACHE: dict = {}  # {market_id: (tba, tbs, {borrower: (bs, col, last_ok_ts)})}
_POSITION_CACHE_MAX_AGE_SEC = 6.0  # ~3 cycles at the observed dt=1.9-2.4s/5-market cycle


def _cache_coverage_stats(borrowers, cached_positions, now):
    """PURE: for a live-read borrower list, how many would have been found in the position cache
    (in_cache) vs never-seen, how old the found ones are, and WHICH ones were missing (short
    addresses, capped) — so a missing borrower can be cross-referenced by address against the
    drop-log, distinguishing 'cache hasn't caught up yet' from 'this borrower fails in both paths
    for the same underlying reason'. No RTT, no side effects — pure comparison against whatever's
    already in memory. Used to measure never-seen frequency under real transmit load WITHOUT
    switching the hot path to consume the cache (see STATE.md)."""
    ages = [now - cached_positions[b][2] for b in borrowers if b in cached_positions]
    missing = [b[:10] for b in borrowers if b not in cached_positions]
    in_cache = len(ages)
    avg_age = sum(ages) / len(ages) if ages else 0.0
    max_age = max(ages) if ages else 0.0
    return in_cache, len(borrowers), avg_age, max_age, missing[:10]


def refresh_position_cache(cache_rpc, mid, borrowers):
    """Background per-block refresh for ONE money-market. A borrower whose position() call in the
    batch comes back ok=False/empty is logged (refresh-miss) but their PREVIOUS cache entry (if any)
    is left untouched — not wiped, not refreshed. Freshness is enforced downstream by age at read
    time (read_positions_cached), not by dropping on a single missed cycle.
    RPC-split (07-21): positions are slow-moving, so this bulk refresh runs on the general/confirmed
    `cache_rpc` (RPC_URL) at block='latest' — NOT on preconf. The preconf per-IP compute cap only
    tolerates ~1 heavy call/s; keeping the ~5-20/s refresh off preconf leaves that budget for the
    price poll (read_preconf_prices, ~0.5/s) which is the one thing that actually needs the ~1.8s lead."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    if not borrowers:
        return
    b32 = cache_rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))] + [(MORPHO_BLUE, encode_position_call(b32, b)) for b in borrowers]
    try:
        res = aggregate3(cache_rpc, calls, block_identifier="latest")
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


def read_positions_cached(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
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
        return read_positions(rpc, mid, borrowers, preconf_rpc=preconf_rpc, src_out=src_out)
    tba, tbs, positions = cached
    if src_out is not None:
        src_out["src"] = "cache"
    now = time.time()
    pos = []
    stale = []
    # 07-23: при refresh-гейте 1/30с (см. _CACHE_REFRESH_MIN_INTERVAL) запись старше 6с — НОРМА,
    # а не исключение; исключать заёмщика из transmit-кандидатов = пропущенная жертва в гонке.
    # Свежие отдаём из кэша (~1мс), stale/missing дочитываем ЖИВЬЁМ одним aggregate3 на
    # CONFIRMED (~70-130мс): preconf-компьют-кап на тяжёлый multicall отдаёт 429 за ~2.05с
    # (замер 23.07; канарейка tip=0.05: 6 промахов из 7 = ровно этот столл, dt_spawn 2.2-2.55с
    # при ack 55-69мс -> мимо блока на КАЖДОМ транзите с жертвой). Preconf-лид остаётся у цены
    # (~0-124мс, лёгкий вызов) — флип двигает цена, позиции медленные. Только если живое
    # чтение упало — старое поведение (stale-skip), чтобы не действовать по протухшим данным.
    for b in borrowers:
        p = positions.get(b)
        if p is None:
            stale.append(b)
            continue
        bs, col, last_ok_ts = p
        if now - last_ok_ts <= _POSITION_CACHE_MAX_AGE_SEC:
            pos.append((b, bs, col))
        else:
            stale.append(b)
    if stale:
        try:
            live = read_positions(rpc, mid, stale, preconf_rpc=None, src_out=src_out)
        except Exception as e:
            live = None
            log.info("DIAG position-cache live-subset fail market=%s n=%d err=%s",
                     mid[:10], len(stale), str(e)[:60])
        if live is not None:
            tba, tbs, live_pos = live                        # живые totals свежее кэшевых
            pos.extend(live_pos)
            if src_out is not None:
                src_out["src"] = "cache+" + str(src_out.get("src") or "live")
        else:
            for b in stale:
                log.info("DIAG position-cache stale-skip market=%s borrower=%s reason=live-fail",
                         mid[:10], b[:10])
    return tba, tbs, pos


_CACHE_REFRESH_THREAD = None      # re-entrancy guard (план п.3): один цикл за раз
_CACHE_REFRESH_STARTED = 0.0      # monotonic старт живого цикла (для busy-skip running=)
_CACHE_SKIP_N = 0                 # skip'ов с последнего лога (троттл 60с)
_CACHE_SKIP_LOG_TS = 0.0
# 07-23: refresh каждые ~2с тяжёлыми aggregate3 выжигал burst-бакет RPC_URL -> 429-шторм душил
# block-assess (~2% блоков) и nonce-fetch. 1 цикл/30с возвращает бакет боевым путям; потребитель
# кэша (POSITIONS_FROM_CACHE=1) при stale-записи дочитывает живьём (см. read_positions_cached),
# так что редкий refresh НЕ теряет жертв. 0 = старое поведение (каждый блок).
_CACHE_REFRESH_MIN_INTERVAL = float(os.environ.get("POSITION_CACHE_MIN_INTERVAL_SEC", "30"))


def _spawn_cache_refresh(*, cache_rpc, shared, bn):
    """Re-entrancy guard for the Layer-2 driver (plan #3). The driver used to be spawned as a NEW
    thread EVERY block while one cycle takes 1.9-2.4s ~= block time: any RTT degradation =>
    overlapping cycles => thread pile-up => self-load on the battle preconf exactly at cascade
    peak (the plausible trigger of the A' confirmed-fallback). One cycle at a time: if the
    previous one is still alive, skip this block. busy-skip is throttled to 1 line/60s with an
    accumulated count — n/running are the live signal of preconf strain (correlate with
    src=confirmed). Called only from the single feed-loop thread => no lock around check-then-set."""
    global _CACHE_REFRESH_THREAD, _CACHE_REFRESH_STARTED, _CACHE_SKIP_N, _CACHE_SKIP_LOG_TS
    if (_CACHE_REFRESH_MIN_INTERVAL > 0 and _CACHE_REFRESH_STARTED
            and time.monotonic() - _CACHE_REFRESH_STARTED < _CACHE_REFRESH_MIN_INTERVAL):
        return                                    # интервал-гейт (см. _CACHE_REFRESH_MIN_INTERVAL): тихий skip
    t = _CACHE_REFRESH_THREAD
    if t is not None and t.is_alive():
        _CACHE_SKIP_N += 1
        now = time.monotonic()
        if now - _CACHE_SKIP_LOG_TS >= 60.0:
            log.info("DIAG cache-refresh busy-skip n=%d running=%.1fs bn=%d",
                     _CACHE_SKIP_N, now - _CACHE_REFRESH_STARTED, bn)
            _CACHE_SKIP_N = 0
            _CACHE_SKIP_LOG_TS = now
        return
    _CACHE_REFRESH_STARTED = time.monotonic()
    _CACHE_REFRESH_THREAD = threading.Thread(target=_refresh_position_caches, daemon=True,
                                             kwargs=dict(cache_rpc=cache_rpc, shared=shared, bn=bn))
    _CACHE_REFRESH_THREAD.start()


def _refresh_position_caches(*, cache_rpc, shared, bn):
    """Per-block driver (Layer 2, measurement phase — see STATE.md 'C temporary'). Refreshes
    _POSITION_CACHE for every money-market (UNIV3_PATHS collateral) via refresh_position_cache.
    Writes _POSITION_CACHE and logs refresh-miss/-fail + cycle timing. NOTE 07-23: the cache IS
    consumed when POSITIONS_FROM_CACHE=1 (P1-6, live in prod) — read_positions_cached serves fresh
    entries and live-reads stale ones. One market's decode/network hiccup does not stop the cycle
    for the others."""
    from chain.execute import ROUTE_COLLATERALS
    with shared.lock:
        groups = dict(shared.groups)
    _t0 = time.time()
    n_markets = 0
    for mid, borrowers in groups.items():
        params = _FULL_PARAMS_CACHE.get(mid)
        if not params:
            continue
        _loan, coll, _oracle, _irm, _lltv = params
        if coll.lower() not in ROUTE_COLLATERALS:    # P1-8: +cbLTC/cbBTC (Slipstream-маршруты)
            continue
        try:
            refresh_position_cache(cache_rpc, mid, borrowers)
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


def _poll_changed(prices, poll_seen, oracle2agg, feed_handled=None, bn=None):
    """PURE: given {oracle: price|None}, update poll_seen (baseline on first sighting) and return the
    set of aggregators whose oracle price MOVED. First sighting -> no spawn; None price -> skipped.
    Layer1 dedup: if feed_handled contains (oracle, bn), the feed already spawned this transmit ->
    still update poll_seen baseline (so next block compares correctly) but DON'T re-add to changed."""
    changed = set()
    for o, px in prices.items():
        if px is None:
            continue
        prev = poll_seen.get(o)
        poll_seen[o] = px
        if prev is not None and px != prev:
            if feed_handled is not None and (o, bn) in feed_handled:
                continue                        # feed already spawned this (oracle, block) -> skip poll dup
            changed.add(oracle2agg.get(o))
    changed.discard(None)
    return changed


def _poll_prices(*, preconf_rpc, rpc_fallback, feeds, meta, poll_seen, stats, spawn_kwargs, bn, log, feed_handled=None):
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
    changed = _poll_changed(prices, poll_seen, oracle2agg, feed_handled=feed_handled, bn=bn)
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


def _seized_exact(repaid_shares, lif_wad, price, tba, tbs):
    """P1-7: seizedAssets ТОЧНО по канону Morpho._liquidate (ветка repaidShares>0):
    toAssetsDown -> wMulDown(LIF) -> mulDivDown(SCALE, price), все floor. Прежний expected_seized
    считал от to_assets_up (репэй-сторона) — оценка сверху на wei-округлениях; для amount_in свопа
    нужен ТОЧНЫЙ приход залога (amount_in > баланса = SwapFailed), для капа — точное сравнение
    с collateral (иначе Panic 0x11)."""
    from chain.simulate import to_assets_down
    from chain.execute import ORACLE_PRICE_SCALE, WAD
    assets_down = to_assets_down(repaid_shares, tba, tbs)
    return (assets_down * lif_wad // WAD) * ORACLE_PRICE_SCALE // price


def _cap_repaid_shares(borrow_shares, collateral, price, lif_wad, tba, tbs):
    """P1-7: max repaidShares, при котором seized <= collateral в арифметике канона — глубокая
    позиция (залог не покрывает долг+бонус) раньше давала Panic 0x11 (collateral -= seized) ->
    sim-revert -> skip, и главные призы каскадов терялись. Кап ДО сайзинга свопа (запрет STATE
    «НЕ капаем» относился к пост-хок капу ПОСЛЕ расчёта свопа — там mismatch). При капе забирается
    весь залог -> Morpho сам списывает остаток долга (bad-debt socialization). Это НЕ закрытый
    seizedAssets-режим: вход контракта прежний (repaidShares). Floor-инверсия + верификация вперёд
    точной формулой; чистая (юнит)."""
    from chain.simulate import to_shares_down
    from chain.execute import ORACLE_PRICE_SCALE, WAD
    bs = int(borrow_shares)
    if bs <= 0:
        return 0
    if _seized_exact(bs, lif_wad, price, tba, tbs) <= collateral:
        return bs                                            # весь долг помещается — кап не нужен
    cap_assets = (collateral * price // ORACLE_PRICE_SCALE) * WAD // lif_wad
    rs = min(to_shares_down(cap_assets, tba, tbs), bs)
    for _ in range(4):                                       # floor-инверсия может промахнуться на wei
        if rs <= 0:
            return 0
        if _seized_exact(rs, lif_wad, price, tba, tbs) <= collateral:
            return rs
        rs -= max(1, rs // 10**6)
    return 0


_GAS_CACHE: dict = {}   # market_id -> padded gas от последнего успешного preconf-estimate (P0-4).
                        # Газ-профиль стабилен per-market (тот же UniV3-путь, та же форма tx);
                        # send всегда шлёт max(cached, cfg.gas_limit_est) — лимит с запасом бесплатен.


def prepare_hot(rpc, preconf_rpc, cfg, market_id, borrower, debt_usd, debt_assets, price, slippage_bps=100,
                state=None):
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
        from chain.execute import kyber_swap, direct_swap, encode_liquidate, simulate_tx, account_for
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = account_for(w3, cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        # market-PARAMS (статичные адреса рынка, иммутабельны) — из кэша resolve_meta (2a), без RTT.
        # Fallback на confirmed-RPC fetch, если кэш ещё не наполнен (до первого resolve_meta).
        _cached = _FULL_PARAMS_CACHE.get(market_id)
        if _cached:
            loan, coll, oracle, irm, lltv_wad = _cached
        else:
            rp_params = aggregate3(rpc, [(MORPHO_BLUE, encode_id_to_market_params_call(mid))])
            loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(rp_params[0][1])
        # market(tba/tbs)+position(borrow_shares): P0-4 — штатно приходят от caller'а (state=), который
        # миллисекундами раньше прочитал их тем же read_positions из ТОГО ЖЕ preconf-pending. Повторное
        # чтение здесь было чистым дублем (~350мс RTT). Fallback-чтение остаётся для вызова без state.
        if state is not None:
            tba, tbs, borrow_shares, collateral_dbg = state
            _snap_src = "caller"
        else:
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
        # ДИАГНОСТИКА расхождения preconf vs confirmed — В ТРЕДЕ (P0-4: было синхронно = 150-300мс
        # Alchemy-RTT В горячем пути ради лога; вывод тот же, задержки ноль).
        def _diag_divergence(_bs_pre=int(borrow_shares), _src=_snap_src):
            try:
                _rp_conf = aggregate3(rpc, [(MORPHO_BLUE, encode_position_call(mid, borrower))])
                _bs_conf, _ = decode_position(_rp_conf[0][1])
                if _bs_conf != _bs_pre:
                    log.info("DIAG divergence %s/%s src=%s preconf_bs=%d confirmed_bs=%d ratio=%.2f",
                             market_id[:10], borrower[:10], _src, _bs_pre, _bs_conf,
                             (_bs_conf / _bs_pre if _bs_pre else 0))
            except Exception:
                pass
        threading.Thread(target=_diag_divergence, daemon=True).start()

        _lif = lif_from_lltv(lltv_wad / 10**18)
        _lif_wad = int(round(_lif * 10**18))
        # P1-7: кап на глубокой позиции (seized>collateral был Panic 0x11 -> skip); seized ТОЧНО
        # по канону (toAssetsDown), а не оценкой сверху — amount_in свопа = реальный приход залога.
        repaid_shares = _cap_repaid_shares(int(borrow_shares), int(collateral_dbg), int(price),
                                           _lif_wad, tba, tbs)
        if repaid_shares <= 0:
            return {"ok": False, "reason": "cap: no repayable shares (collateral~0)"}
        if repaid_shares < int(borrow_shares):
            log.info("DIAG cap %s/%s bs=%d -> repaid_shares=%d (deep position: весь залог, остаток долга спишет Morpho)",
                     market_id[:10], borrower[:10], int(borrow_shares), repaid_shares)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = _seized_exact(repaid_shares, _lif_wad, int(price), tba, tbs)   # (1) preconf price, канон-точно
        if seized == 0:
            log.info("DIAG seized=0 %s/%s repaid_shares=%d repaid_assets=%d collateral=%d tba=%d tbs=%d price=%d lltv=%d",
                     market_id[:10], borrower[:10], repaid_shares, repaid_assets, collateral_dbg, tba, tbs, int(price), lltv_wad)
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
        if swap is None:                       # market outside direct routes -> Kyber fallback (unchanged)
            swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=slippage_bps)

        # P0-4: guard-read свежести УДАЛЁН — он дублировал sim: тот же preconf-pending источник, а sim
        # ниже ревертит (Panic 0x11) на том же устаревшем долге и даёт тот же skip. Отдельное чтение
        # стоило ~350мс RTT в горячем пути и ловило только окно «между guard и sim» ~0мс шириной.

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0)
        # P0-4: gas per-market из кэша; при промахе estimate_gas идёт ПАРАЛЛЕЛЬНО sim (оба preconf,
        # cd0/cd1 по газу эквивалентны — отличаются одним сравнением minProfit) -> первый выстрел по
        # рынку платит max(sim, estimate) вместо суммы, последующие — только sim.
        _cached_gas = _GAS_CACHE.get(market_id)
        _gas_holder: dict = {}
        _est_thread = None
        if _cached_gas is None:
            def _do_estimate():
                try:
                    _g = preconf_rpc._web3().eth.estimate_gas({"from": bot, "to": liq, "data": cd0},
                                                              block_identifier="pending")
                    _gas_holder["gas"] = int(_g) * 12 // 10
                except Exception:
                    pass
            _est_thread = threading.Thread(target=_do_estimate, daemon=True)
            _est_thread.start()
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
        # gas: из per-market кэша (0 RTT) либо от параллельного preconf-estimate выше. Явный gas в prep
        # по-прежнему нужен, иначе send_tx оценит на confirmed-Alchemy, где reaction-флип ещё healthy ->
        # estimate_gas ревертит -> tx НЕ уходит. Лимит всегда max(..., cfg.gas_limit_est) — запас бесплатен.
        if _cached_gas is not None:
            _gas_limit = max(_cached_gas, cfg.gas_limit_est)
        else:
            if _est_thread is not None:
                _est_thread.join(timeout=2.0)
            _est = _gas_holder.get("gas")
            if _est is not None:
                _GAS_CACHE[market_id] = _est
                _gas_limit = max(_est, cfg.gas_limit_est)
            else:
                _gas_limit = cfg.gas_limit_est
        _prep = {"ok": True, "calldata": cd1, "gas": _gas_limit, "net_usd": net_usd, "profit_usd": profit_usd, "cost_usd": cost_usd,
                 # сырые компоненты для batch-энкодера + debt для per-item USD в receipt-цикле (prep["calldata"] = одиночный вызов)
                 "mp": mp, "borrower": borrower, "repaid_shares": repaid_shares, "swap_target": swap["router"],
                 "swap_calldata": swap["calldata"], "min_profit": min_profit_final, "debt_usd": debt_usd, "debt_assets": debt_assets}
        _fb = _fresh_basefee()
        if _fb is not None:                                   # SEND-2: fee из fb-хедера (0 Alchemy-RTT в send)
            _tip = int(dynamic_tip_gwei(cfg, debt_usd) * 1e9)   # каскадная страховка по призу
            _prep["max_fee"] = _fb * 2 + _tip                 # та же формула base*2+tip, но base = pending-блок из фида
            _prep["tip"] = _tip
        return _prep
    except Exception as e:
        return {"ok": False, "reason": f"error: {type(e).__name__}: {str(e)[:120]}"}


def prepare_fast(rpc, preconf_rpc, cfg, market_id, borrower, debt_usd, debt_assets, price, state):
    """P1-9 fast-lane (флаг cfg.fast_lane, OFF по умолчанию): подготовка БЕЗ sim и БЕЗ estimate —
    ноль RTT, путь decode->send ~150-250мс. Безопасность НЕ на sim, а на контрактах: Morpho
    HEALTHY_POSITION ревертит до трансферов, наш minProfit/SwapFailed/CannotRepay ревертят своп-
    исход; цена промаха = газ реверта (~центы при Base-fee), учитывается kill-switch'ем как loss.
    Условия (иначе {ok:False} и обычный prepare_hot с sim): state от caller; params в кэше;
    маршрут direct_swap (Kyber в fast-lane запрещён — серверный RTT); fee из fb-хедера свежий
    (SEND-2); gas из per-market кэша либо cfg.gas_limit_est.
    minProfit аналитический: 50% теоретического бонуса repaid*(LIF-1) — запас на слиппаж/старение."""
    try:
        from chain.simulate import to_assets_up
        from chain.execute import direct_swap, encode_liquidate, WAD
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq or state is None:
            return {"ok": False, "reason": "fast: no state/liquidator"}
        _fb = _fresh_basefee()
        if _fb is None:
            return {"ok": False, "reason": "fast: fb-basefee stale"}
        params = _FULL_PARAMS_CACHE.get(market_id)
        if not params:
            return {"ok": False, "reason": "fast: params not cached"}
        loan, coll, oracle, irm, lltv_wad = params
        tba, tbs, borrow_shares, collateral = state
        if borrow_shares <= 0:
            return {"ok": False, "reason": "fast: no debt"}
        _lif = lif_from_lltv(lltv_wad / 10**18)
        _lif_wad = int(round(_lif * 10**18))
        repaid_shares = _cap_repaid_shares(int(borrow_shares), int(collateral), int(price),
                                           _lif_wad, tba, tbs)
        if repaid_shares <= 0:
            return {"ok": False, "reason": "fast: cap=0"}
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = _seized_exact(repaid_shares, _lif_wad, int(price), tba, tbs)
        if seized == 0:
            return {"ok": False, "reason": "fast: seized=0"}
        swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
        if swap is None:
            return {"ok": False, "reason": "fast: no direct route"}
        min_profit = repaid_assets * (_lif_wad - WAD) // WAD // 2
        if min_profit <= 0:
            return {"ok": False, "reason": "fast: zero bonus"}
        profit_usd, cost_usd, net_usd = _net_gate(min_profit, debt_usd, debt_assets, cfg)
        if net_usd < cfg.min_profit_usd:
            return {"ok": False, "reason": f"fast: net ${net_usd:.2f} < min (analytic floor)"}
        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        cd = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], min_profit)
        _tip = int(dynamic_tip_gwei(cfg, debt_usd) * 1e9)     # каскадная страховка по призу
        return {"ok": True, "calldata": cd, "gas": max(_GAS_CACHE.get(market_id, 0), cfg.gas_limit_est),
                "net_usd": net_usd, "profit_usd": profit_usd, "cost_usd": cost_usd,
                "max_fee": _fb * 2 + _tip, "tip": _tip, "fast": True,
                # сырые компоненты для batch-энкодера + debt для per-item USD в receipt-цикле
                "mp": mp, "borrower": borrower, "repaid_shares": repaid_shares, "swap_target": swap["router"],
                "swap_calldata": swap["calldata"], "min_profit": min_profit, "debt_usd": debt_usd, "debt_assets": debt_assets}
    except Exception as e:
        return {"ok": False, "reason": f"fast error: {type(e).__name__}: {str(e)[:80]}"}


# ---- per-transmit worker (reaction filter + kill-switch + dispatch) ----

def hot_min_repaid(cfg):
    return float(getattr(cfg, "hot_min_repaid_usd", 2000.0))


_ANSWER_UPDATED_SIG = None   # keccak(AnswerUpdated(int256,uint256,uint256)); лениво

# Поправка 07-17 п.1: медиана «+46» считалась ТОЛЬКО по попаданиям в блок; 37% MISSED-BLOCK
# (+1 блок = полный проигрыш) в неё не входили. Копим ОБА исхода за кампанию + tx-count блока
# (перевод «позиции -> мс» раньше был не обоснован кодом). Под своим локом (пишут bg-треды).
_CANARY_STATS_LOCK = threading.Lock()
_CANARY_HIT_DELTAS: list = []   # in-block дельты
_CANARY_MISSES = 0              # canary_blk > transmit_blk


def _canary_measure(rpc, canary_hash, agg, transmit_block, cfg, log):
    """⭐ Bg: дождаться receipt канарейки -> учесть газ в СВОЙ бакет -> найти tx-index транслята
    (AnswerUpdated на agg в transmit_block, узкий getLogs) -> ДЕЛЬТА canary_idx - transmit_idx.
    Малая/отриц. дельта = попадаем в блок первыми = выигрываем гонку. Read-only после отправки."""
    global _ANSWER_UPDATED_SIG, _CANARY_MISSES
    from chain.execute import canary_record_gas, canary_invalidate_nonce, canary_stats
    from web3 import Web3
    try:
        w3 = rpc._web3()
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(canary_hash, timeout=45)
        except Exception:
            canary_invalidate_nonce(rpc, cfg)   # дропнута -> _LOCAL_NONCE впереди чейна -> инвалидировать (money-safety)
            log.info("DIAG canary NO-RECEIPT (dropped?) -> nonce invalidated")
            return
        cblk, cidx = rcpt.get("blockNumber"), rcpt.get("transactionIndex")
        gas_usd = int(rcpt.get("gasUsed") or 0) * int(rcpt.get("effectiveGasPrice") or 0) * cfg.eth_price_usd / 1e18
        canary_record_gas(gas_usd)
        if _ANSWER_UPDATED_SIG is None:
            from eth_utils import keccak
            _ANSWER_UPDATED_SIG = "0x" + keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
        from chain.rpc import get_logs_fb
        tidx = None
        for _att in range(4):                  # getLogs отстаёт от receipt (лаг пропагации логов) -> ретрай
            logs = get_logs_fb(w3, {"fromBlock": transmit_block, "toBlock": transmit_block,
                                    "address": Web3.to_checksum_address(agg), "topics": [_ANSWER_UPDATED_SIG]})
            if logs:
                tidx = logs[0].get("transactionIndex"); break
            time.sleep(2.0)
        probes, spent = canary_stats()
        if tidx is not None and cblk == transmit_block:
            try:                                   # tx-count блока: «позиции -> мс» с опорой, не на глаз
                _blk_txs = int(w3.eth.get_block_transaction_count(transmit_block))
            except Exception:
                _blk_txs = -1
            with _CANARY_STATS_LOCK:
                _CANARY_HIT_DELTAS.append(cidx - tidx)
                _hits = sorted(_CANARY_HIT_DELTAS); _miss = _CANARY_MISSES
            log.info("DIAG canary DELTA delta=%d canary_idx=%d transmit_idx=%d block=%d blk_txs=%d gas$=%.3f probe=%d spent$=%.2f | run: hits=%d miss=%d med_hit=%d",
                     cidx - tidx, cidx, tidx, transmit_block, _blk_txs, gas_usd, probes, spent,
                     len(_hits), _miss, _hits[len(_hits) // 2])
        elif tidx is not None:
            with _CANARY_STATS_LOCK:
                _CANARY_MISSES += 1
                _hits_n = len(_CANARY_HIT_DELTAS); _miss = _CANARY_MISSES
            log.info("DIAG canary MISSED-BLOCK canary_blk=%d transmit_blk=%d (+%d поздно) canary_idx=%d gas$=%.3f probe=%d | run: hits=%d miss=%d",
                     cblk, transmit_block, cblk - transmit_block, cidx, gas_usd, probes, _hits_n, _miss)
        else:
            log.info("DIAG canary no-AnswerUpdated agg=%s blk=%d canary_idx=%d gas$=%.3f probe=%d",
                     agg[:10], transmit_block, cidx, gas_usd, probes)
    except Exception as e:
        log.info("DIAG canary measure fail: %s", type(e).__name__)


def _maybe_canary(agg, block, subidx, rpc, cfg, log, t0=None):
    """⭐ Пробить канарейку, ЕСЛИ флаг ON, это РЕАЛЬНЫЙ транслят (не периодический poll — у того нет
    AnswerUpdated в блоке, дельту мерить не против чего) и в цикле НЕТ ликвидации/inflight (ноль помех).
    Гейты кап/интервал/газ — атомарно в send_canary. Замер в фоне -> не тормозит hot-путь.
    Поправка 07-17: канарейка летит БОЕВЫМ send-путём (fee из fb-хедера + SEND-3 raw в секвенсер) —
    пробы 1-100 шли Alchemy-путём с 2 fee-RTT и без секвенсера, т.е. мерили оверхед, которого у
    реальной ликвидации нет. dt_spawn (t0=спавн транзита) + ack в логе = разложение мс по п.1 поправки."""
    if not getattr(cfg, "canary_probe", False) or subidx == "poll":
        return
    from chain.execute import send_canary, current_inflight
    if current_inflight() > 0:                    # боевая tx в полёте -> не соревноваться за nonce
        return
    _fb = _fresh_basefee()
    _tip = int(cfg.tip_gwei * 1e9)
    _seq = str(getattr(cfg, "sequencer_rpc", "") or "")
    _bcast = [_seq] if (_seq and _seq.lower() not in ("off", "none", "0")) else None
    _ss = time.perf_counter()
    h = send_canary(rpc, cfg, log,
                    max_fee=(_fb * 2 + _tip) if _fb is not None else None, tip=_tip,
                    broadcast_raw_urls=_bcast)
    if h:
        # trig=feed (money-путь, price=0ms) vs bare-match idx (в dt_spawn сидит gate-RTT ~110-130мс) —
        # без атрибуции кампания несравнима: медиану надо считать ПО feed-пробам отдельно.
        log.info("DIAG canary sent block=%d trig=%s agg=%s dt_spawn=%s ack=%.0fms fees=%s seq=%s",
                 block, subidx, agg[:10],
                 ("%.0fms" % ((time.time() - t0) * 1000.0)) if t0 is not None else "?",
                 (time.perf_counter() - _ss) * 1000.0,
                 "fb" if _fb is not None else "alchemy", "on" if _bcast else "off")
        threading.Thread(target=_canary_measure, args=(rpc, h, agg, block, cfg, log), daemon=True).start()


def _process_transmit(agg, block, subidx, t, *, rpc, preconf_rpc, cfg, feeds, meta, shared,
                      store, guard, alerter, log, last_price=None, stats=None,
                      price_fn=read_preconf_price, reads_fn=read_positions,
                      prepare_fn=prepare_hot, dispatch_fn=None,
                      px_hint=None, hint_oracle=None):
    """For one detected transmit: per affected market, read the preconf price, recompute flips, keep only
    reaction prizes (repaid >= hot_min_repaid), check the SHARED kill-switch, prepare_hot, and dispatch
    the passers. Injectable seams (price_fn/reads_fn/prepare_fn/dispatch_fn) for unit tests. Returns the
    dispatch results (list) for logging/tests."""
    from strategy.guard import GuardState
    from chain.execute import fast_lane_disabled, pop_fast_lane_trip   # II.6 circuit breaker
    if dispatch_fn is None:
        from chain.execute import dispatch_liquidations as dispatch_fn  # noqa: F811
    if reads_fn is read_positions and getattr(cfg, "positions_from_cache", False):
        # P1-6 потребление слоя 2 (флаг POSITIONS_FROM_CACHE, OFF по умолчанию): только для
        # ДЕФОЛТНОГО seam (инжектированные reads_fn тестов не трогаем). read_positions_cached
        # сам падает в живой read_positions для рынков вне кэша — нулевой риск непокрытым.
        reads_fn = read_positions_cached
    floor = hot_min_repaid(cfg)
    _gate_px = {}                                            # 2b: цена оракула, прочитанная gate -> переиспользовать в цикле
    if px_hint is not None and hint_oracle is not None:
        # P0-3: feed-декод (FORWARD_SEL точной формы) = сам transmit, gate ему не нужен. Раньше gate ниже
        # ЧИТАЛ preconf (~350мс) и ПЕРЕЗАПИСЫВАЛ хинт; при preconf-лаге (~0.5с) старая цена давала
        # moved=False -> feed-спавн умирал, а poll этого блока уже дедупнут через feed_handled -> триггер
        # уезжал на блок позже. Теперь: хинт = цена цикла, baseline обновляем, gate НЕ читаем (0 RTT).
        _gate_px[hint_oracle] = px_hint
        if last_price is not None:
            _price_moved(agg, px_hint, last_price)           # baseline для последующих bare-match gate'ов
        if stats is not None:
            stats["proceed"] += 1
    # price-change gate: the bare match also fires on price-READ txs. Read ONE representative oracle
    # preconf price; if it has not moved since last sighting this was a reader, not a transmit -> skip
    # the 340-call multicall + flips. This is what makes the bare match viable on the hot path.
    elif last_price is not None:
        rep = next((meta[m][0] for m in feeds.get(agg, []) if meta.get(m)), None)
        if rep is None:
            return []
        px = read_price_hedged(preconf_rpc, rpc, rep, price_fn=price_fn)
        if px is not None:
            _gate_px[rep] = px                               # 2b: рынок с этим оракулом не будет читать 2-й раз
        moved = _price_moved(agg, px, last_price)
        if stats is not None:                                # health buckets (none=preconf unreadable)
            stats["none" if px is None else ("proceed" if moved else "skip")] += 1
        if not moved:
            return []
    with shared.lock:
        groups = dict(shared.groups); debt_by = dict(shared.debt_by); debt_assets_by = dict(shared.debt_assets_by)

    # Поправка 07-17 п.2 (остаточный preconf-RTT): оракулы рынков этого agg вне _gate_px читались
    # ПОСЛЕДОВАТЕЛЬНО внутри цикла (~110мс каждый из Charlotte) -> рынок №2/№3 ждал СУММУ чужих RTT
    # (наблюдалось в журнале: price=0ms -> 114ms -> 110ms), и канарейка стреляла после всех. Теперь
    # все недостающие цены стартуют параллельно ЗДЕСЬ; цикл ждёт только СВОЙ оракул (Event).
    # Hint-рынок не ждёт ничего (цена уже в _gate_px).
    _px_prefetch = {}                            # oracle -> (Event, [px|None])
    for _mid_pf in feeds.get(agg, []):
        _om_pf = meta.get(_mid_pf)
        if not _om_pf or not groups.get(_mid_pf):
            continue
        _o_pf = _om_pf[0]
        if _o_pf in _gate_px or _o_pf in _px_prefetch:
            continue
        _ev_pf, _box_pf = threading.Event(), []
        _px_prefetch[_o_pf] = (_ev_pf, _box_pf)

        def _pf_read(_o=_o_pf, _ev=_ev_pf, _box=_box_pf):
            try:
                _box.append(read_price_hedged(preconf_rpc, rpc, _o, price_fn=price_fn))
            except Exception:
                pass
            finally:
                _ev.set()
        threading.Thread(target=_pf_read, daemon=True).start()

    ready, meta_by = [], {}
    _canary_fired = False                        # ранний выстрел на hint-рынке (см. ниже) — один на транзит
    _t_read_total = 0.0; _t_prep_total = 0.0; _t_flips_total = 0.0   # DIAG latency: read=340-call, sim=prepare_hot
    for mid in feeds.get(agg, []):
        om = meta.get(mid)
        borrowers = groups.get(mid, [])
        if not om or not borrowers:
            if stats is not None: stats["f_nocand"] += 1
            continue
        oracle, lltv_wad = om
        _pxs = time.perf_counter()
        if oracle in _gate_px:                               # 2b: gate уже прочитал этот оракул -> без 2-го RTT
            price = _gate_px[oracle]
        elif oracle in _px_prefetch:                         # 07-17: параллельный префетч стартовал до цикла
            _ev_w, _box_w = _px_prefetch[oracle]
            _ev_w.wait(timeout=5.0)
            price = _box_w[0] if _box_w else None
        else:                                                # страховка (префетч покрывает штатно)
            price = read_price_hedged(preconf_rpc, rpc, oracle, price_fn=price_fn)
        _t_price_last = (time.perf_counter() - _pxs) * 1000.0
        if price is None:
            if stats is not None: stats["f_noprice"] += 1
            continue
        _rs = time.perf_counter()
        _src_out = {}
        rp = reads_fn(rpc, mid, borrowers, preconf_rpc=preconf_rpc, src_out=_src_out)
        _rsrc = _src_out.get("src", "?")
        with _POSITION_CACHE_LOCK:
            _cached = _POSITION_CACHE.get(mid)
        if _cached is not None:
            _in_cache, _total, _avg_age, _max_age, _missing = _cache_coverage_stats(borrowers, _cached[2], time.time())
            log.info("DIAG cache-coverage market=%s in_cache=%d/%d avg_age=%.1fs max_age=%.1fs missing=%s",
                     mid[:10], _in_cache, _total, _avg_age, _max_age, ",".join(_missing) if _missing else "-")
        _t_read_last = (time.perf_counter() - _rs) * 1000.0; _t_read_total += _t_read_last
        if not rp:
            if stats is not None: stats["f_norp"] += 1
            continue
        tba, tbs, pos = rp
        if _cached is not None and _rsrc != "cache":
            # P1-6 log-only КАЛИБРОВКА (гейт потребления по чеклисту STATE): значения кэша vs живое
            # чтение на том же транзите. mism=0 на потоке -> кэш можно включать (POSITIONS_FROM_CACHE=1).
            _nw = time.time(); _same = 0; _mm = []
            for (_b2, _bs2, _col2) in pos:
                _c2 = _cached[2].get(_b2)
                if _c2 is None or _nw - _c2[2] > _POSITION_CACHE_MAX_AGE_SEC:
                    continue
                if _c2[0] == _bs2 and _c2[1] == _col2:
                    _same += 1
                else:
                    _mm.append(_b2[:8])
            log.info("DIAG cache-divergence market=%s same=%d mism=%d tba_eq=%s tbs_eq=%s bad=%s",
                     mid[:10], _same, len(_mm), _cached[0] == tba, _cached[1] == tbs,
                     ",".join(_mm[:5]) if _mm else "-")
        log.info("DIAG hotset mid=%s src=%s trig=%s agg=%s n=%d block=%d",   # agg= атрибуция ноги-триггера (multi-leg 07-23)
                 mid[:10], _rsrc, subidx, agg[:10], len(pos), block)  # full set on disk
        positions = [(b, bs, col, debt_by.get((mid, b), 0.0)) for (b, bs, col) in pos]
        pos_by = {b: (bs, col) for (b, bs, col) in pos}      # P0-4: state для prepare_hot без повторного RTT
        _fls = time.perf_counter()                           # 07-17 п.1: CPU _flips по непруненному набору — мерить
        flipped = _flips(price, lltv_wad, tba, tbs, positions)
        _t_flips_last = (time.perf_counter() - _fls) * 1000.0; _t_flips_total += _t_flips_last
        if not flipped:
            if stats is not None: stats["f_noflip"] += 1
            if positions:                                    # DIAG: what state did we read? (preconf-source check)
                from chain.simulate import MarketContext as _MC, health_from as _hf
                _ctx = _MC(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                           total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
                _c = [(bb, bs, _hf(_ctx, bs, col).hf) for (bb, bs, col, _du) in positions]
                _bb, _bs, _hv = min(_c, key=lambda x: x[2])
                log.info("DIAG noflip %s/%s minHF=%.4f bshares=%d px=%d n=%d src=%s trig=%s price=%.0fms read=%.0fms flips=%.1fms e2e=%.0fms",
                         mid[:10], _bb[:10], _hv, _bs, int(price), len(_c), _rsrc, subidx, _t_price_last, _t_read_last,
                         _t_flips_last, (time.time() - t) * 1000.0)
        for (b, hr, du) in flipped:
            if du < floor:                                   # reaction filter: $2k+ only
                if stats is not None: stats["f_floor"] += 1
                continue
            if stats is not None: stats["f_prep"] += 1
            _bc = pos_by.get(b)
            if (getattr(cfg, "fast_lane", False) and not fast_lane_disabled()   # II.6: breaker -> фолбэк sim
                    and _bc and du >= getattr(cfg, "fast_lane_min_repaid_usd", 1000.0)):
                _fs = time.perf_counter()                    # P1-9: fast-lane БЕЗ sim; при ok обычный
                fprep = prepare_fast(rpc, preconf_rpc, cfg, mid, b, du,   # prepare НЕ дублируем (2-я tx
                                     debt_assets_by.get((mid, b), 0), price,   # сожгла бы nonce+газ)
                                     (tba, tbs, _bc[0], _bc[1]))
                _t_prep_total += (time.perf_counter() - _fs) * 1000.0
                if fprep.get("ok"):
                    ready.append((mid, b, fprep)); meta_by[(mid, b)] = (hr, du)
                    log.info("HOT ready FAST %s/%s hf=%.4f repaid~$%.0f net>=$%.2f block=%d subidx=%s",
                             mid[:10], b[:10], hr.hf, du, fprep["net_usd"], block, subidx)
                    continue
                log.info("DIAG fast-lane fallback %s/%s: %s", mid[:10], b[:10], str(fprep.get("reason"))[:60])
            _ps = time.perf_counter()
            # P0-4: (tba,tbs,bs,col) из read_positions этого же
            prep = prepare_fn(rpc, preconf_rpc, cfg, mid, b, du,   # триггера (мс назад, тот же preconf) -> prepare_hot
                              debt_assets_by.get((mid, b), 0), price,   # не перечитывает market/position (~-350мс)
                              state=((tba, tbs, _bc[0], _bc[1]) if _bc else None))
            _t_prep_total += (time.perf_counter() - _ps) * 1000.0
            if prep.get("ok"):
                ready.append((mid, b, prep)); meta_by[(mid, b)] = (hr, du)
                log.info("HOT ready %s/%s hf=%.4f repaid~$%.0f net=$%.2f block=%d subidx=%s",
                         mid[:10], b[:10], hr.hf, du, prep["net_usd"], block, subidx)
            else:
                log.info("HOT skip %s/%s: %s", mid[:10], b[:10], str(prep.get("reason"))[:80])

        if hint_oracle is not None and oracle == hint_oracle and not ready and not _canary_fired:
            # 07-17: канарейка стреляет В ТОЧКЕ, где fast-lane отправил бы hint-рынок, а не после
            # обработки всех соседей agg (пробы 1-100 включали до ~220мс чужих preconf-RTT).
            _canary_fired = True
            _maybe_canary(agg, block, subidx, rpc, cfg, log, t0=t)

    if not ready:
        if not _canary_fired:                              # bare-match транзиты (без hint) — как раньше, после цикла
            _maybe_canary(agg, block, subidx, rpc, cfg, log, t0=t)   # ⭐ нет ликвидации -> пробить гонку
        return []

    from chain.execute import current_inflight
    today = store.realized_today()                           # SHARED kill-switch with the block loop
    blocked = guard.blocked_reason(GuardState(realized_net_today=today["net"],
                                              gas_spent_today=today["gas"],
                                              inflight=current_inflight()))   # P0: был хардкод 0 — inflight-гейт мёртв
    if blocked:
        log.error("HOT kill switch engaged: %s — %d ready NOT dispatched", blocked, len(ready))
        alerter.send_async(f"\U0001F6D1 hot path halted: {blocked}", key="hot-killswitch")
        return []

    _ds = time.perf_counter()
    results = dispatch_fn(rpc, cfg, ready, log=log, max_inflight=cfg.max_inflight)
    _t_send = (time.perf_counter() - _ds) * 1000.0
    _fl_trip = pop_fast_lane_trip()                # II.6: breaker сработал в receipt-цикле dispatch -> TG-пейдж
    if _fl_trip:
        alerter.send_async(_fl_trip, key="fastlane-breaker")
    from chain.execute import pop_batch_empty_trip
    _be_trip = pop_batch_empty_trip()              # мультибатч: серия пустых батчей (nOk=0) -> TG-пейдж
    if _be_trip:
        alerter.send_async(_be_trip, key="batch-empty")
    log.info("DIAG timing read=%.0fms flips=%.0fms sim=%.0fms send=%.0fms e2e=%.0fms n_ready=%d",
             _t_read_total, _t_flips_total, _t_prep_total, _t_send, (time.time() - t) * 1000.0, len(ready))
    for r in results:
        hr, du = meta_by.get((r["market_id"], r["borrower"]), (None, 0.0))
        status = (f"submitted:{r.get('status')} net${r.get('net_usd',0):.2f}" if r["sent"]
                  else f"skip:{str(r.get('reason'))[:50]}")
        store.log_action(market_id=r["market_id"], borrower=r["borrower"], mode=cfg.mode,
                         tx_hash=r.get("hash"), net_usd=r.get("net_usd", 0.0), gas_usd=r.get("gas_usd", 0.0), status=status)
        log.info("HOT ACTIONABLE %s/%s status=%s tx=%s", r["market_id"][:10], r["borrower"][:10], status, r.get("hash"))
        alerter.send_async(f"\U0001F525 HOT {cfg.mode} {r['borrower'][:10]} {status}", key=f"hot:{r['market_id']}:{r['borrower']}")
    return results


# ---- async flashblock subscriber (detect + spawn) ----

def _init_resolve_retry(fn, *args, what="", tries=14, base=0.5, cap=10.0, log=None, empty_ok=True):
    """Init-резолв hot_loop на keyed-RPC с rate-лимитом (напр. dRPC Free = 100 req/s): при старте бот
    выстреливает залп разом (candidate refresh + mega-fetch из block-loop + resolve) — на миг >лимита →
    429. Ретраим с backoff (капнут cap): залп схлынет и повтор пройдёт, поток hot_loop НЕ умирает.
    tries×cap подобраны пережить mega-fetch залп (~8с, но повторяется каждые ~30с) — 14 попыток,
    backoff 0.5→10с ≈ до ~110с суммарно. При живом dRPC резолв проходит с 1-й попытки (нулевая задержка).
    На Alchemy (500 req/s) 429 не приходил, потому хрупкость не вылезала (инцидент 07-21/22)."""
    for i in range(tries):
        try:
            out = fn(*args)
            if out or empty_ok:
                return out
            # empty_ok=False: resolve_feeds глотает пер-рыночные ошибки и в шторм отдаёт {} БЕЗ
            # исключения (07-23: «0 aggregators») — считаем пустоту отказом и ретраим как 429.
            raise RuntimeError("empty resolve result")
        except Exception as e:
            if i == tries - 1:
                if not empty_ok:
                    if log is not None:
                        log.warning("hot_loop init %s: пусто после %d попыток (%s) — стартую слепым, "
                                    "ре-резолв подберёт ускоренным циклом", what, tries, type(e).__name__)
                    return {}
                raise
            wait = min(base * (2 ** i), cap)
            if log is not None:
                log.warning("hot_loop init %s: %s — retry %d/%d через %.1fс (rate-limit burst)",
                            what, type(e).__name__, i + 1, tries, wait)
            time.sleep(wait)


async def hot_loop(rpc, preconf_rpc, cfg, shared, store, guard, alerter, log, stop, markets,
                   ws_factory=None):
    """Subscribe to Flashblocks; on a PRECISE transmit to one of our aggregators, spawn a worker thread
    that runs _process_transmit (the recv loop itself does no slow work). Re-resolves feeds/meta on a
    cadence (rotation). DORMANT until block_driven_loop starts this behind cfg.hot_path."""
    import asyncio
    import json
    import websockets
    import brotli
    from chain.feeds import (resolve_feeds, extract_txs, block_number_of, decode_forward_answer,
                             FORWARD_SEL, basefee_of, primary_map)

    def _default_ws():
        return websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None)
    ws_factory = ws_factory or _default_ws

    from chain.rpc import BaseRpc
    _fb_resolve_rpc = BaseRpc(os.environ.get("FEEDS_FALLBACK_RPC", "https://mainnet.base.org"),
                              getattr(rpc, "chain_id", 8453), request_timeout=8.0)
    feeds = _init_resolve_retry(resolve_feeds, rpc, markets, _fb_resolve_rpc,
                                what="feeds", log=log, empty_ok=False)

    def _resolve_meta_fb():
        # 17:44 23.07: init-meta исчерпала 14 ретраев за ~8.4мин шторма и УРОНИЛА hot_loop
        # (self-heal рестартил его циклом). Симметрия с f96854d: фолбэк-эндпоинт + empty_ok=False
        # ниже = слепой старт вместо смерти; слепая meta ре-резолвится ускоренно (120с, bg).
        try:
            return resolve_meta(rpc, markets)
        except Exception:
            return resolve_meta(_fb_resolve_rpc, markets)
    meta = _init_resolve_retry(_resolve_meta_fb, what="meta", log=log, empty_ok=False)
    agg_set = set(feeds)
    shadow_map = _build_shadow_map(feeds, meta, markets, primary=primary_map())   # SHADOW слоя 1 (log-only спавн-гейт)
    log.info("DIAG shadow-feed map %s", {a[:10]: s for a, (s, _sc, _o) in shadow_map.items()})
    # 07-23: init-резолв конкурирует со стартовым залпом (mega-fetch жжёт оба эндпоинта) и приходит
    # ЧАСТИЧНЫМ (23 ключа против 27 в спокойный замер) — первый ре-резолв назначаем через 180с
    # (залп улёгся, ноги добираются), дальше обычный каденс. Guard «пусто не затирает» стоит ниже.
    last_resolve = time.monotonic() - max(0.0, cfg.rescan_interval_sec - 180.0)
    log.info("hot path: %d aggregators / %d markets (preconf=%s, tip=%.1f gwei, floor repaid $%.0f)",
             len(agg_set), len(meta), preconf_rpc.rpc_url, cfg.tip_gwei, hot_min_repaid(cfg))
    _seq_url = str(getattr(cfg, "sequencer_rpc", "") or "")   # 07-17: тёплый keep-alive к секвенсеру ДО
    if _seq_url and _seq_url.lower() not in ("off", "none", "0"):   # первого выстрела (холодный TLS ~2-3 RTT)
        from chain.execute import warm_broadcast
        warm_broadcast([_seq_url])

    last_spawn: dict = {}        # agg -> last spawn time.time() (throttle; survives ws reconnects)
    last_price: dict = {}        # agg -> last preconf price of representative oracle (change-gate)
    stats = {"spawn": 0, "skip": 0, "proceed": 0, "none": 0,    # hot-path health; emitted every HOT_STATS_SEC
             "poll": 0, "pspawn": 0, "poll_none": 0, "poll_fb": 0,  # poll_fb=oracles filled from latest fallback
             "f_nocand": 0, "f_noprice": 0, "f_norp": 0, "f_noflip": 0, "f_floor": 0, "f_prep": 0}  # funnel proceed->prepare_hot
    last_stats = time.monotonic()
    poll_seen: dict = {}         # oracle -> last preconf price (poll-local; baseline -> spawn only on move)
    feed_handled: set = set()    # Layer1: (oracle, bn) spawned by feed this block -> poll skips dup; cleared on block change
    last_poll_block = None       # one preconf-price poll per block
    fb_windows = 0               # consecutive stats windows with poll_fb>0 (preconf degradation streak)
    _fb_backoff_n = 0            # подряд-неудачные reconnect'ы flashblocks -> экспон. backoff. У flashblocks
                                 # per-IP лимит на ЧАСТОТУ коннектов: фикс-3с реконнект при транзиентном сбое
                                 # Base превращал его в самоподдерживающийся 429-шторм на час (инцидент 07-21).
    last_fb_alert = 0.0          # monotonic of last degradation alert (anti-flood)
    recon_ts = []                # monotonic'ы реконнектов hot-path ws; одиночный разрыв публичного
    last_recon_alert = 0.0       # Flashblocks-WS = НОРМА (роняет соединение раз в час-два) -> TG молчит,
                                 # алерт ТОЛЬКО на серию (3+ за 180с = реальная нестабильность фида/сети)
    _re_thread = None            # 07-23: ре-резолв УШЁЛ ИЗ recv-петли — inline-резолв глушил hot-path
    _re_box = None               # на всю длительность (спокойный замер multi-leg: 184с петля не читает
    _re_started = 0.0            # WS -> триггеры мертвы). Фоновый тред кладёт результат в box,
                                 # петля коммитит готовое; один резолв за раз (как _spawn_cache_refresh).
                                 # _re_started: orphan-гард — Thread-245 висел 7+ мин на чёрнодырном
                                 # эндпоинте (per-recv timeout переживает trickle), блокируя рескан.
    while not stop.is_set():
        try:
            async with ws_factory() as ws:
                _fb_backoff_n = 0            # коннект установлен -> сброс backoff
                while not stop.is_set():
                    # слепой старт (feeds ИЛИ meta пусты) ре-резолвится ускоренно (120с), не через 6ч rescan
                    _rescan_iv = cfg.rescan_interval_sec if (feeds and meta) else min(cfg.rescan_interval_sec, 120.0)
                    if (_re_thread is not None and _re_thread.is_alive()
                            and time.monotonic() - _re_started > 300.0):
                        # висящий резолв (чёрная дыра эндпоинта) не должен блокировать рескан:
                        # бросаем daemon-тред (его box осиротел — коммит его не прочтёт), повтор ~120с
                        log.warning("re-resolve завис >300с — бросаю тред, повтор ~120с (карта прежняя, %d aggs)",
                                    len(agg_set))
                        _re_thread = None; _re_box = None
                        last_resolve = time.monotonic() - max(0.0, _rescan_iv - 120.0)
                    if _re_thread is not None and not _re_thread.is_alive() and _re_box is not None:
                        _res = _re_box.get("res")
                        log.info("DIAG re-resolve commit res=%s dt=%.0fs",
                                 "ok" if _res else "none", time.monotonic() - _re_started)
                        _re_thread = None; _re_box = None
                        if _res:
                            new_feeds, new_meta = _res
                            last_resolve = time.monotonic()
                            if (new_feeds or not feeds) and (new_meta or not meta):
                                feeds = new_feeds; meta = new_meta; agg_set = set(feeds)
                                shadow_map = _build_shadow_map(feeds, meta, markets, primary=primary_map())   # ротация агрегатора
                                log.info("re-resolve: %d watch keys / %d market-legs",
                                         len(agg_set), sum(len(v) for v in feeds.values()))
                            else:
                                # шторм на обоих эндпоинтах: пустой картой ЖИВУЮ не затираем
                                log.warning("re-resolve пуст (feeds=%d meta=%d) — держу старую карту (%d aggs)",
                                            len(new_feeds), len(new_meta), len(agg_set))
                        else:
                            # резолв упал целиком (исключение в bg): повтор через ~120с, не через 6ч
                            last_resolve = time.monotonic() - max(0.0, _rescan_iv - 120.0)
                            log.warning("re-resolve упал (шторм) — повтор ~120с, карта прежняя (%d aggs)", len(agg_set))
                    if (time.monotonic() - last_resolve >= _rescan_iv
                            and not (_re_thread is not None and _re_thread.is_alive())):
                        _re_box = {}
                        _re_started = time.monotonic()
                        log.info("DIAG re-resolve spawn iv=%.0f since_last=%.0fs",
                                 _rescan_iv, time.monotonic() - last_resolve)

                        def _bg_resolve(box=_re_box, cur_meta=meta):
                            try:
                                nf = resolve_feeds(rpc, markets, _fb_resolve_rpc)
                            except Exception:
                                box["res"] = None
                                return
                            try:
                                nm = resolve_meta(rpc, markets)
                            except Exception:
                                # meta иммутабельна per market (idToMarketParams) — 429 на одном
                                # aggregate3 рескана не должен блокировать коммит карты ног
                                # (17:33 23.07: час тихих ретраев именно из-за этого)
                                try:
                                    nm = resolve_meta(_fb_resolve_rpc, markets)
                                except Exception:
                                    nm = cur_meta
                            box["res"] = (nf, nm)
                        _re_thread = threading.Thread(target=_bg_resolve, daemon=True)
                        _re_thread.start()
                    if time.monotonic() - last_stats >= HOT_STATS_SEC:
                        log.info("hot stats %ds: spawn=%d pspawn=%d poll=%d | gate proceed=%d skip=%d none=%d poll_none=%d poll_fb=%d | funnel nocand=%d noprice=%d norp=%d noflip=%d floor=%d prep=%d",
                                 int(time.monotonic() - last_stats), stats["spawn"], stats["pspawn"], stats["poll"],
                                 stats["proceed"], stats["skip"], stats["none"], stats["poll_none"], stats["poll_fb"],
                                 stats["f_nocand"], stats["f_noprice"], stats["f_norp"], stats["f_noflip"], stats["f_floor"], stats["f_prep"])
                        # порог: >3 фолбэка/поллинг ≈ >8% чтений цены потеряли фору = РЕАЛЬНАЯ cap-деградация
                        # (исходный preconf-кап был 1457/окно=67%); микро-остаток спайков фида (≤2%, ~48/окно)
                        # НЕ будит — иначе ложно «мелькает» в норме (07-22, poll_fb калибр. на катастрофу, не на 1-2 оракула)
                        fb_windows = fb_windows + 1 if stats["poll_fb"] > stats["poll"] * 3 else 0
                        if fb_windows >= 3 and time.monotonic() - last_fb_alert > 1800:
                            last_fb_alert = time.monotonic()
                            try:
                                alerter.send_async("preconf degraded %dx windows: polling on LATEST (detection alive, "
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
                        feed_handled.clear()                  # Layer1: dedup set is per-block -> clear on block change
                        _bf = basefee_of(d)                   # 5a: index-0 кадр несёт base-хедер
                        if _bf is not None:
                            _basefee_probe(bn=bn, fb=_bf, rpc=rpc, log=log)
                        threading.Thread(target=_poll_prices, daemon=True,
                                         kwargs=dict(preconf_rpc=preconf_rpc, rpc_fallback=rpc, feeds=feeds, meta=meta,
                                                     poll_seen=poll_seen, stats=stats, bn=bn, log=log,
                                                     feed_handled=feed_handled,
                                                     spawn_kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                                       feeds=feeds, meta=meta, shared=shared,
                                                                       store=store, guard=guard, alerter=alerter,
                                                                       log=log, last_price=last_price, stats=stats))).start()
                        # Layer 2, measurement phase (C temporary — see STATE.md): fills _POSITION_CACHE +
                        # logs refresh-miss/cycle timing. Does NOT touch reads_fn — read_positions_cached
                        # is not wired into _process_transmit yet, so this has zero effect on hot-path behavior.
                        _spawn_cache_refresh(cache_rpc=rpc, shared=shared, bn=bn)   # RPC-split: рефреш на общий rpc (confirmed), НЕ на preconf
                    idx = d.get("index"); txs = extract_txs(d); now = time.time()
                    for _raw, _to in txs:                        # SHADOW слоя 1: декод transmit из фида (log-only)
                        if _raw and FORWARD_SEL in _raw:
                            _dec = decode_forward_answer(_raw)
                            if _dec and _dec[0] in shadow_map:
                                _sym, _scale, _oracle = shadow_map[_dec[0]]
                                if cfg.price_from_feed and _feed_spawn_gate(_oracle, bn, feed_handled):
                                    # Layer1 LIVE: feed price triggers _process_transmit + dedups poll;
                                    # gate = один спавн на (oracle,bn) — transmit виден в неск. суб-кадрах
                                    log.info("DIAG feed-spawn mkt=%s oracle=%s bn=%s px_hint=%d",
                                             _sym, _oracle[:10], bn, _dec[1] * _scale)
                                    threading.Thread(target=_process_transmit,
                                                     args=(_dec[0], bn, "feed", time.time()),
                                                     kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                                 feeds=feeds, meta=meta, shared=shared,
                                                                 store=store, guard=guard, alerter=alerter,
                                                                 log=log, last_price=last_price, stats=stats,
                                                                 px_hint=_dec[1] * _scale, hint_oracle=_oracle),
                                                     daemon=True).start()
                                threading.Thread(target=_shadow_feed_check, daemon=True,
                                                 kwargs=dict(sym=_sym, agg=_dec[0], answer=_dec[1],
                                                             scale=_scale, oracle=_oracle,
                                                             preconf_rpc=preconf_rpc,
                                                             price_fn=read_preconf_price,
                                                             t0=time.perf_counter(), log=log)).start()
                    for agg in agg_set:                          # BARE match: ANY mention of the aggregator
                        if now - last_spawn.get(agg, 0.0) < HOT_THROTTLE_SEC:
                            continue                             # per-agg throttle: bound preconf reads/spawns
                        if any(raw_ and (agg[2:] in raw_) for raw_, _ in txs):
                            last_spawn[agg] = now
                            stats["spawn"] += 1
                            if len(agg) != 42:               # watch-паттерн (не адрес): спавны редкие по построению
                                log.info("DIAG pattern-spawn %s bn=%s", agg, bn)
                            threading.Thread(target=_process_transmit, args=(agg, bn, idx, now),
                                             kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                         feeds=feeds, meta=meta, shared=shared, store=store,
                                                         guard=guard, alerter=alerter, log=log,
                                                         last_price=last_price, stats=stats),
                                             daemon=True).start()
        except Exception as e:
            _fb_backoff_n += 1
            _wait = min(3 * (2 ** (_fb_backoff_n - 1)), 60)      # 3,6,12,24,48,60,60… — не долбим кап на частоту
            log.warning("hot path ws error (%s) — reconnecting in %.0fs (backoff #%d)", type(e).__name__, _wait, _fb_backoff_n)
            _now = time.monotonic()
            recon_ts = [t for t in recon_ts if _now - t <= 180.0] + [_now]   # окно 180с
            if len(recon_ts) >= 3 and _now - last_recon_alert > 600.0:       # серия -> реальная проблема
                last_recon_alert = _now
                alerter.send_async("\U0001F6A8 hot-path %d реконнектов за 3 мин — check journalctl" % len(recon_ts),
                             key="hot-wsserr")
            await asyncio.sleep(_wait)
