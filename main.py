"""Entry point. Phase 1 = monitor (paper-trade): scan covered markets, confirm health
on-chain, estimate net, log what we WOULD do. No transactions are submitted in monitor.

Two-tier scan (cheap -> precise):
  1) positions_at_risk (Morpho API, USD-approx HF) enumerates borrowers and casts a wide
     net at HF_API_CEILING — one API call for all covered markets.
  2) for the near-risk set: ONE Multicall3 aggregate3 reads market()+oracle.price() per
     market + position() per candidate across ALL markets -> EXACT on-chain HF -> analytic
     net (estimate). Immutable params (oracle, lltv) are cached, so on-chain cost is ~1-2
     eth_calls/cycle regardless of how many markets/positions are in flight.
"""
from __future__ import annotations
import json
import os
import asyncio
import queue
import threading
import logging
import time
from collections import defaultdict

from config import Config
from chain.execute import (try_liquidate, prepare_liquidation, dispatch_liquidations,
                           current_inflight, specfire_gas_today)
from store import Store
from alerts import Alerter
from heartbeat import beat as hb_beat
from strategy.scanner import load_covered_markets
from strategy.guard import KillSwitch, GuardState
from chain.rpc import BaseRpc
from chain import chain_index                      # #110: фоновый индекс цепи (подача блоков)
from chain.morpho import positions_at_risk, MORPHO_BLUE_ADDRESS
from chain.simulate import assess_candidates_batched, estimate

# API USD-approx wide net; the precise on-chain HF makes the real call. Loose on purpose
# (API prices lag the oracle) so nothing near liquidation is missed before on-chain confirm.
# 1.10 -> 1.15 (04.08, ревью cbBTC-полки): за горизонтом 1.10 лежала СТЕНА-2 ($29.1M на
# дист 10-12%) — при BTC ±2% она вплывала в набор «сюрпризом». Откат: HF_API_CEILING=1.10.
HF_API_CEILING = float(os.environ.get("HF_API_CEILING", "1.15"))
# Base gas is tiny; these are monitor placeholders for the paper PnL. Execute computes real
# gas from the live base fee + a competitive sequencer tip.
GAS_USD_EST = 0.10
TIP_USD_EST = 0.05
# Heartbeat "scan:" line is throttled to this cadence to keep logs tiny; ACTIONABLE
# finds and errors are logged unconditionally (never throttled).
HEARTBEAT_SEC = 300


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    # Ключ провайдера живёт в ПУТИ URL, а текст исключения RPC несёт URL целиком — до 07.08
    # журнал отдавал оба drpc-ключа всякому, у кого есть доступ к хосту. Фильтр вешаем на
    # ХЕНДЛЕР, а не на логгер: у logging фильтры логгера не применяются к записям, всплывшим
    # от дочерних логгеров, а «liq.*» пишут все модули бота.
    from chain.rpc import RedactingFilter, note_secrets_from_env
    for h in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
    # Реестр литералов наполняем СРАЗУ из окружения, а не ждём, пока каждый контур построит
    # BaseRpc. 12.08 ключ dRPC (9 символов) уехал в журнал строкой предиктора: его URL до
    # конструктора BaseRpc не доходил, а оба эвристических рубежа (сегмент >=20, литерал >=16)
    # короткий ключ пропускали. Регистрация до первой сетевой строки — единственный порядок,
    # при котором фильтр закрывает и то, что случится в первую секунду работы.
    note_secrets_from_env()


def _empty_summary(n_markets: int) -> dict:
    return {"markets": n_markets, "api_candidates": 0, "confirmed": 0,
            "liquidatable": 0, "profitable": 0, "min_hf": None}


def fetch_candidates(markets):
    """SLOW path (Morpho API): enumerate at-risk borrowers (HF <= HF_API_CEILING), grouped by
    market + debt maps. Candidate SET drifts slowly; refresh periodically (~60s), NOT per block.
    Returns (groups, debt_by, debt_assets_by, hf_by, n)."""
    candidates = positions_at_risk(markets, hf_ceiling=HF_API_CEILING)
    groups = defaultdict(list)
    debt_by = {}
    debt_assets_by = {}
    hf_by = {}
    for c in candidates:
        groups[c.market_id].append(c.borrower)
        debt_by[(c.market_id, c.borrower)] = c.debt_usd
        debt_assets_by[(c.market_id, c.borrower)] = c.debt_assets
        hf_by[(c.market_id, c.borrower)] = c.health_factor
    return dict(groups), debt_by, debt_assets_by, hf_by, len(candidates)


# ---- warm-start (P0-1): снапшот candidate set на диск + загрузка при рестарте ----
# Рестарт (деплой/крэш) обнулял shared.groups до первого fetch_candidates (~1-2 мин слепого окна
# горячего пути на КАЖДЫЙ рестарт). Снапшот пишется refresh-воркером каждые ~60с (tmp+rename,
# атомарно); при старте block_driven_loop грузится, если не старше WARM_START_MAX_AGE_SEC.
# Устаревание безопасно: погашенная позиция даст bs=0 при живом on-chain чтении и отпадёт сама;
# НОВЫЙ заёмщик до первого API-фетча не виден так же, как и без warm-start.
WARM_START_MAX_AGE_SEC = 3600.0


def _save_candidates_snapshot(path, groups, debt_by, debt_assets_by, hf_by, n):
    """Best-effort: ошибка записи не роняет refresh-воркер. Ключи-кортежи -> 'mid|borrower'."""
    try:
        payload = {"ts": time.time(), "n": n, "groups": groups,
                   "debt_by": {f"{m}|{b}": v for (m, b), v in debt_by.items()},
                   "debt_assets_by": {f"{m}|{b}": v for (m, b), v in debt_assets_by.items()},
                   "hf_by": {f"{m}|{b}": v for (m, b), v in hf_by.items()}}
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def _load_candidates_snapshot(path, max_age_sec=WARM_START_MAX_AGE_SEC):
    """(groups, debt_by, debt_assets_by, hf_by, n) либо None (нет файла / стар / битый).
    hf_by в старом снапшоте отсутствует -> {} (гейты по HF fail-open)."""
    try:
        with open(path) as fh:
            d = json.load(fh)
        if time.time() - float(d.get("ts", 0)) > max_age_sec:
            return None
        def _unkey(dd):
            return {tuple(k.split("|", 1)): v for k, v in dd.items()}
        return (dict(d["groups"]), _unkey(d["debt_by"]), _unkey(d["debt_assets_by"]),
                _unkey(d.get("hf_by", {})), int(d.get("n", 0)))
    except Exception:
        return None


def _execute_actionable(rpc, cfg, actionable, debt_by, debt_assets_by, store, guard, alerter, log,
                        prepare_fn=prepare_liquidation, dispatch_fn=dispatch_liquidations):
    """Monitor: record paper. Execute: kill switch ONCE, prepare each (fresh reads+simulate+floor),
    dispatch passers NON-BLOCKING with sequential nonces, record. Parallel so a cascade isn't
    serialized on one blocking send. prepare_fn/dispatch_fn injectable for tests."""
    def _record(mid, borrower, hr, sr, status, tx_hash=None, net_usd=None, gas_usd=None):
        net = sr.net_usd if net_usd is None else net_usd       # realized (receipt) when sent, else expected (sim)
        gas = sr.gas_usd if gas_usd is None else gas_usd
        store.log_action(market_id=mid, borrower=borrower, mode=cfg.mode, tx_hash=tx_hash,
                         net_usd=net, gas_usd=gas, status=status)
        log.info("ACTIONABLE %s/%s HF=%.4f net=$%.2f mode=%s status=%s",
                 mid[:10], borrower[:10], hr.hf, net, cfg.mode, status)
        # 28.07: исход ОБЯЗАН быть в тексте — «💰 execute … net $6.79» без статуса читается
        # с телефона как заработок, хотя это мог быть skip:sim (вопрос kelbic по 0x6e0bdA0b)
        alerter.send_async(f"\U0001F4B0 {cfg.mode} liquidation {borrower[:10]} "
                     f"net ${net:.2f} HF={hr.hf:.4f} → {status[:60]}", key=f"act:{mid}:{borrower}")

    if not actionable:
        return
    if cfg.mode != "execute":
        for mid, borrower, hr, sr in actionable:
            _record(mid, borrower, hr, sr, "paper")
        return

    today = store.realized_today()
    blocked = guard.blocked_reason(GuardState(realized_net_today=today["net"],
                                              gas_spent_today=today["gas"] + specfire_gas_today(), inflight=current_inflight()))
    if blocked:
        for mid, borrower, hr, sr in actionable:
            _record(mid, borrower, hr, sr, f"blocked:{blocked}")
        return

    ready, meta = [], {}
    for mid, borrower, hr, sr in actionable:
        prep = prepare_fn(rpc, cfg, mid, borrower,
                          debt_by.get((mid, borrower), 0.0),
                          debt_assets_by.get((mid, borrower), 0))
        if prep.get("ok"):
            ready.append((mid, borrower, prep)); meta[(mid, borrower)] = (hr, sr)
        else:
            _record(mid, borrower, hr, sr, f"skip:{prep['reason'][:50]}")
    if not ready:
        return

    results = dispatch_fn(rpc, cfg, ready, log=log, max_inflight=cfg.max_inflight)
    done = set()
    for r in results:
        hr, sr = meta[(r["market_id"], r["borrower"])]
        done.add((r["market_id"], r["borrower"]))
        status = (f"submitted:{r.get('status')} net${r.get('net_usd', 0):.2f}" if r["sent"]
                  else f"skip:{r['reason'][:50]}")
        _record(r["market_id"], r["borrower"], hr, sr, status, tx_hash=r.get("hash"),
                net_usd=(r.get("net_usd") if r["sent"] else None),
                gas_usd=(r.get("gas_usd") if r["sent"] else None))
    for mid, borrower, prep in ready:
        if (mid, borrower) not in done:
            hr, sr = meta[(mid, borrower)]
            _record(mid, borrower, hr, sr, "skip:deferred (inflight cap/stop)")


def process_candidates(rpc, by_id, groups, debt_by, debt_assets_by, store, cfg, guard, alerter,
                       log, ctx_cache, n_candidates=0, hf_cache=None, read_rpc=None):
    """FAST path (on-chain): ONE aggregate3 over the candidate set -> exact HF; per
    liquidatable+profitable+unblocked candidate, execute. ~1-2 eth_calls — cheap per block.
    read_rpc: откуда ЧИТАТЬ ассессмент (по умолчанию rpc). Замер CU 25.07: этот батч —
    0.5-0.8 eth_call/с = 37-52M CU/мес, т.е. один он съедал квоту Alchemy; отправки и
    реактивный путь остаются на rpc, читаем с бесплатного bg (при отказе — ретрай на rpc)."""
    summary = _empty_summary(len(by_id))
    summary["api_candidates"] = n_candidates
    if not groups:
        return summary
    # ЦЕПОЧКА ДЕНЕЖНЫХ ЧТЕНИЙ (25.07, замечание ревьювера #2): keyed dRPC -> Alchemy -> публичный.
    # Публичный НЕ годится первым: замер 25.07 — 429 на батче 1262 кандидатов ЦЕЛИКОМ (dRPC 100%
    # за 0.7с, Alchemy за 0.6с). И главное: assess_candidates_batched при ЧАСТИЧНОМ отказе молча
    # ПРОПУСКАЕТ кандидатов (ok=False -> continue, исключения нет) => фолбэк по except не срабатывал
    # бы, а флип в день приза потерялся молча (класс дыры 07-23). Поэтому проверяем ПОКРЫТИЕ.
    _rr = read_rpc or rpc
    _expect = sum(len(v) for mid, v in groups.items() if mid in ctx_cache) or None
    try:
        assessed = assess_candidates_batched(_rr, MORPHO_BLUE_ADDRESS, groups, ctx_cache)
    except Exception:
        if _rr is rpc:
            raise
        assessed = assess_candidates_batched(rpc, MORPHO_BLUE_ADDRESS, groups, ctx_cache)
    else:
        if _expect and _rr is not rpc and len(assessed) < _expect:
            log.warning("assess неполон на bg-RPC (%d из %d) — перечитываю на основном",
                        len(assessed), _expect)
            try:
                _re = assess_candidates_batched(rpc, MORPHO_BLUE_ADDRESS, groups, ctx_cache)
                if len(_re) > len(assessed):
                    assessed = _re
            except Exception:
                pass                          # основной тоже отказал -> работаем с неполным набором
    actionable = []
    _pos_rows = []                                   # 27.07: батч-апсерт после цикла (1 commit, не ~98)
    _sim_rows = []                                   # 27.07: sim-телеметрия тоже после диспатча

    for mid, borrower, hr in assessed:
        mkt = by_id.get(mid)
        if mkt is None:
            continue
        if hf_cache is not None:
            hf_cache[(mid, borrower)] = hr.hf       # remember reading -> drives the hot set
        summary["confirmed"] += 1
        summary["min_hf"] = hr.hf if summary["min_hf"] is None else min(summary["min_hf"], hr.hf)
        _pos_rows.append((mid, borrower, hr.hf))
        if not hr.liquidatable:
            continue
        summary["liquidatable"] += 1
        sr = estimate(hr, debt_by.get((mid, borrower), 0.0), mkt.expected_slippage,
                      GAS_USD_EST, TIP_USD_EST, cfg.min_profit_usd)        # analytic net, pure
        _sim_rows.append(dict(market_id=mid, borrower=borrower,
                              repaid_usd=sr.repaid_usd, seized_usd=sr.seized_usd,
                              bonus_usd=sr.seized_usd - sr.repaid_usd, gas_usd=sr.gas_usd,
                              net_usd=sr.net_usd, would_submit=int(sr.profitable),
                              reverted=int(sr.reverted), note=sr.note))   # 27.07: пишем ПОСЛЕ денег
        if not sr.profitable:
            continue
        summary["profitable"] += 1
        actionable.append((mid, borrower, hr, sr))   # collect -> batch-dispatch after the loop
    _execute_actionable(rpc, cfg, actionable, debt_by, debt_assets_by, store, guard, alerter, log)
    store.upsert_positions(_pos_rows)                # телеметрия одной транзакцией ПОСЛЕ денег (не в их пути)
    for _kw in _sim_rows:
        store.log_simulation(**_kw)
    return summary


def scan_once(rpc, markets, store, cfg, guard, alerter, log, ctx_cache) -> dict:
    """One full scan pass (poll mode): fetch candidate set (API) then process it (on-chain)."""
    by_id = {m.market_id: m for m in markets}
    groups, debt_by, debt_assets_by, _hf_by, ncand = fetch_candidates(markets)
    if ncand and rpc is None:
        log.warning("near-risk positions found but RPC_URL empty — on-chain confirm skipped")
        s = _empty_summary(len(markets)); s["api_candidates"] = ncand; return s
    return process_candidates(rpc, by_id, groups, debt_by, debt_assets_by, store, cfg, guard,
                              alerter, log, ctx_cache, ncand)


def rescan_markets(cfg, current_markets, ctx_cache, log, alerter=None):
    """Re-fetch + re-select the covered set: catches new tail markets, drops out-of-band ones,
    excludes configured SVR/OEV oracles. Hot-swaps, prunes ctx_cache, persists JSON, reloads.
    Returns the new list, or the current one unchanged on any failure."""
    try:
        from analysis.build_covered_markets import (fetch_markets, filter_phantoms,
                                                    rescan_change_msg, select_markets,
                                                    to_json_records)
        exclude = {a.strip().lower() for a in (cfg.exclude_oracles or "").split(",") if a.strip()}
        items = fetch_markets()
        records = to_json_records(select_markets(items, exclude_oracles=exclude))
        # 30.07 («го» kelbic): отбор по займу геймится скам-рынками (elephanToken-«RSS», $1M
        # фейкового займа) — фантомы (оракул-константа + нет выхода) отсеиваются до записи.
        ph_reasons, ph_fresh, _pre_pairs = {}, set(), {}
        try:
            _pre_pairs = {r["market_id"]: "%s/%s" % (r["collateral_token"], r["loan_token"])
                          for r in records}
            _ph_cache = os.path.join(os.path.dirname(cfg.covered_markets_path) or ".",
                                     "phantom_cache.json")
            records, ph_reasons, ph_fresh = filter_phantoms(
                records, items, cfg.bg_rpc_url or cfg.rpc_url, _ph_cache,
                log_fn=lambda m: log.info("%s", m))
        except Exception as e:  # фильтр не имеет права уронить рескан
            log.warning("phantom filter failed (%s) — пропускаю", type(e).__name__)
        if not records:
            log.warning("market rescan: empty selection — keeping current %d markets", len(current_markets))
            return current_markets
        old_ids = {m.market_id for m in current_markets}
        new_ids = {r["market_id"] for r in records}
        for mid in (old_ids - new_ids):
            ctx_cache.pop(mid, None)
        try:
            with open(cfg.covered_markets_path, "w") as fh:
                json.dump(records, fh, indent=2)
        except OSError as e:
            log.warning("market rescan: persist failed: %s", e)
        new_markets = load_covered_markets(cfg.covered_markets_path)
        added, removed = new_ids - old_ids, old_ids - new_ids
        if added or removed:
            log.info("market rescan: %d markets (+%d/-%d) added=%s removed=%s", len(new_markets),
                     len(added), len(removed), sorted(a[:10] for a in added), sorted(r[:10] for r in removed))
            if alerter is not None:
                # 28.07: рескан молча откатил ручную правку covered (WETH/USDC) — изменение
                # листа обязано быть видимым владельцу; 29.07: списанные пары (RESCAN_ALERT_QUIET)
                # ходят молча — пустое сообщение НЕ шлём (лог-строка выше есть всегда)
                try:
                    old_pairs = {m.market_id: "%s/%s" % (m.collateral_token, m.loan_token)
                                 for m in current_markets}
                    new_pairs = {r["market_id"]: "%s/%s" % (r["collateral_token"], r["loan_token"])
                                 for r in records}
                    _msg = rescan_change_msg(items, old_pairs, new_pairs, added, removed, exclude,
                                             phantom_reasons=ph_reasons)
                    if _msg:
                        alerter.send_async(_msg, key="rescan-cov")
                except Exception:
                    pass
        else:
            log.info("market rescan: %d markets (no change)", len(new_markets))
        # свежий фантом, которого в covered и не было: change-алерта не будет — одна 🕳-строка
        # (кэш вердиктов гарантирует «без спама»: повторные ресканы молчат)
        _ph_new = [m for m in sorted(ph_fresh) if m not in old_ids]
        if _ph_new and alerter is not None:
            try:
                alerter.send_async("🕳 rescan: " + " | ".join(
                    "%s %s… отсеян как фантом (%s)" % (_pre_pairs.get(m, "?/?"), m[:10],
                                                       ph_reasons.get(m, ""))
                    for m in _ph_new), key="rescan-phantom")
            except Exception:
                pass
        return new_markets
    except Exception as e:
        log.warning("market rescan failed (%s) — keeping current %d markets", type(e).__name__, len(current_markets))
        return current_markets


class _Shared:
    """Candidate set shared between the API-refresh thread (writer) and the wss block loop (reader)."""
    def __init__(self, markets):
        self.lock = threading.Lock()
        self.by_id = {m.market_id: m for m in markets}
        self.groups, self.debt_by, self.debt_assets_by, self.n = {}, {}, {}, 0
        self.hf_by = {}                          # (mid, borrower) -> USD-approx HF (для SPEC-FIRE reach-гейта)
        from collections import deque
        self.groups_history = deque(maxlen=64)   # (monotonic_ts, {mid: frozenset(borrowers)}) — closes B
                                                 # retrospectively: was borrower in groups at transmit-time N?

    def snapshot(self):
        with self.lock:
            return self.by_id, self.groups, self.debt_by, self.debt_assets_by, self.n


_WETH_BASE = "0x4200000000000000000000000000000000000006"


def fetch_eth_price_usd(timeout=10):
    """ETH/USD from the Morpho API (assetByAddress(WETH).price.usd) — same API the loop already
    uses, reachable from the VPS. Returns float or None on failure (caller keeps last good value)."""
    import json as _json, urllib.request as _u
    q = ('query($a:String!,$c:Int!){ assetByAddress(address:$a,chainId:$c){ price { usd } } }')
    body = _json.dumps({"query": q, "variables": {"a": _WETH_BASE, "c": 8453}}).encode()
    req = _u.Request("https://api.morpho.org/graphql", data=body,
                     headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with _u.urlopen(req, timeout=timeout) as r:
        d = _json.loads(r.read())
    usd = (((d.get("data") or {}).get("assetByAddress") or {}).get("price") or {}).get("usd")
    return float(usd) if usd else None


def _refresh_worker(shared, markets, cfg, ctx_cache, log, stop, alerter=None):
    """Background thread: periodically re-fetch the at-risk candidate set (slow API) + rescan
    markets (6h). API-only (no web3) -> safe alongside the loop. Updates `shared` atomically."""
    last_rescan = time.time()
    while not stop.is_set():
        try:
            if time.time() - last_rescan >= cfg.rescan_interval_sec:
                markets = rescan_markets(cfg, markets, ctx_cache, log, alerter)
                last_rescan = time.time()
            try:
                px = fetch_eth_price_usd()
                if px and px > 0:
                    cfg.eth_price_usd = px            # live ETH/USD -> honest net-cost floor (no hardcode)
            except Exception:
                pass                                  # keep last good price
            _t0 = time.time()
            g, db, dab, hb, n = fetch_candidates(markets)
            log.info("DIAG candidate refresh n=%d dt=%.1fs", n, time.time() - _t0)
            with shared.lock:
                shared.by_id = {m.market_id: m for m in markets}
                shared.groups, shared.debt_by, shared.debt_assets_by, shared.n = g, db, dab, n
                shared.hf_by = hb
                shared.groups_history.append(                          # snapshot per fetch (monotonic ts)
                    (time.monotonic(), {mid: frozenset(bs) for mid, bs in g.items()}))
            _save_candidates_snapshot(cfg.candidates_snapshot_path, g, db, dab, hb, n)   # P0-1 warm-start
        except Exception:
            log.exception("candidate refresh error")
        stop.wait(cfg.candidate_refresh_sec)


async def _new_heads(ws):
    """Subscribe to newHeads; yield the LATEST header each time, draining any buffered backlog so a
    slow assess never makes us act on stale state (the loop self-aligns to the current block)."""
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
    sub = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
    if "error" in sub:
        raise RuntimeError(f"wss subscribe rejected: {sub['error']}")
    while True:
        msg = json.loads(await ws.recv())                       # block for next head
        head = (msg.get("params") or {}).get("result")
        if not head:
            continue
        while True:                                             # drain backlog -> keep only latest
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 0.01))
            except asyncio.TimeoutError:
                break
            nh = (msg.get("params") or {}).get("result")
            if nh:
                head = nh
        if head.get("number"):
            yield int(head["number"], 16)


async def _flashblock_heads(ws):
    """Subscribe to the public Base Flashblocks feed; yield each block's number ONCE, on the FIRST
    Flashblock seen for it — ~2.1s earlier than newHeads (measured). Messages are brotli-compressed
    JSON; the block number is base/block_number on the initial sub-block (index 0) and
    metadata/block_number on the rest. Flashblocks are ordered, so we yield on a strictly increasing
    number and dedup the ~10 sub-blocks per block. Per-message decode errors are skipped (the feed is
    public/best-effort) so one bad frame can't kill the stream."""
    import brotli  # lazy: keeps `import main` clean without the dep
    last = 0
    while True:
        raw = await ws.recv()
        try:
            txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            d = json.loads(txt)
        except Exception:
            continue
        bn = None
        base = d.get("base")
        if isinstance(base, dict):
            bn = base.get("block_number")
        if bn is None:
            meta = d.get("metadata")
            if isinstance(meta, dict):
                bn = meta.get("block_number")
        if bn is None:
            continue
        bn = int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else int(bn)
        if bn <= last:
            continue                                  # dedup sub-blocks / stale frames (monotonic)
        last = bn
        yield bn


HOT_HF_CEILING = 1.02          # block mode: per block, assess only candidates with last HF < this
HOT_FULL_REFRESH_SEC = 30.0    # block mode: re-assess the FULL candidate set at least this often
HOT_LOOP_START_STAGGER_SEC = 8.0  # разнести старт hot_loop от cold-start залпа block-loop (keyed-RPC rate-limit)


def _hot_subset(groups, hf_cache):
    """Candidates whose last on-chain HF was below HOT_HF_CEILING (unseen -> included). Only these
    can realistically liquidate within a block; assessing just them keeps per-block cost tiny."""
    hot = {}
    for mid, borrowers in groups.items():
        sel = [b for b in borrowers if hf_cache.get((mid, b), 0.0) < HOT_HF_CEILING]
        if sel:
            hot[mid] = sel
    return hot


_FLASHBLOCK_URL = "wss://mainnet.flashblocks.base.org/ws"
_FLASHBLOCK_FALLBACK_AFTER = 3   # consecutive flashblocks failures (no blocks) -> fall back to newHeads
# 05.08: фолбэк был ОДНОСТОРОННИМ до рестарта — после шторма бот жил на newHeads (~2.1с медленнее
# реакции), пока человек не рестартовал в тихое окно (04.08: 2 часа на newHeads). Авто-возврат =
# периодическая проба flashblocks; бэкофф щедрый (урок 07-21: фиксированные 3с реконнекта на
# per-IP лимите flashblocks = самоподдерживающийся 429-шторм на час). Откат FLASHBLOCK_AUTO_RETURN=0.
_FB_AUTO_RETURN = os.environ.get("FLASHBLOCK_AUTO_RETURN", "1").lower() not in ("0", "false", "no")
_FB_RETRY_SEC = float(os.environ.get("FLASHBLOCK_RETRY_SEC", "600"))
_FB_RETRY_MAX_SEC = float(os.environ.get("FLASHBLOCK_RETRY_MAX_SEC", "3600"))
_FB_STABLE_SEC = float(os.environ.get("FLASHBLOCK_STABLE_SEC", "30"))


class FbAutoReturn:
    """Проба возврата на flashblocks из newHeads-фолбэка. Проба = штатный connect-путь петли
    с fails=AFTER-1: одна неудача -> немедленный тихий откат на newHeads (без TG — человек тут
    не действует), следующая проба по x2-бэкоффу 600с..3600с. Гистерезис (ревью 05.08): успех
    объявляется только после stable_sec НЕПРЕРЫВНОГО потока блоков — хлопающий источник не
    спамит TG и не сбрасывает бэкофф. Счётчики fallback/restore — в лог-строки эпизодов."""
    def __init__(self, base=None, cap=None, enabled=None, stable_sec=None):
        self.base = _FB_RETRY_SEC if base is None else base
        self.cap = _FB_RETRY_MAX_SEC if cap is None else cap
        self.enabled = _FB_AUTO_RETURN if enabled is None else enabled
        self.stable_sec = _FB_STABLE_SEC if stable_sec is None else stable_sec
        self.delay = self.base
        self.next_at = None        # None = не в фолбэке, проб нет
        self.probing = False
        self.first_blk_ts = None   # первый блок текущей пробы (отсчёт стабилизации)
        self.n_fallbacks = 0
        self.n_restores = 0

    def on_fallback(self, now):
        """Настоящий фолбэк (не проба): новый эпизод, таймер с базы."""
        self.n_fallbacks += 1
        self.delay = self.base
        self.next_at = now + self.delay
        self.probing = False
        self.first_blk_ts = None

    def due(self, now):
        return bool(self.enabled and self.next_at is not None and now >= self.next_at)

    def on_probe(self, now):
        self.probing = True
        self.first_blk_ts = None
        self.delay = min(self.delay * 2, self.cap)
        self.next_at = now + self.delay

    def on_block(self, now):
        """Блок пришёл во время пробы. True = поток стабилен stable_sec -> возврат состоялся."""
        if not self.probing:
            return False
        if self.first_blk_ts is None:
            self.first_blk_ts = now
            return False
        if now - self.first_blk_ts >= self.stable_sec:
            self.n_restores += 1
            self.on_success()
            return True
        return False

    def on_success(self):
        self.delay = self.base
        self.next_at = None
        self.probing = False
        self.first_blk_ts = None
# TG-гигиена 07-10 (решение владельца): одиночный ws-обрыв block-лупа самолечится за 3с и является
# фоном Base-эндпоинта (~раз в 1-3ч) — БОЛЬШЕ НЕ ПЕЙДЖИТ, только журнал. TG получает СЕРИЮ
# (флаппинг: >=_RECON_SERIES_N за окно — эндпоинт болен, но до fallback не дошло) и сам fallback
# (key=fbfallback, реальная деградация). Философия = hot-path (серии, не блипы).
_RECON_SERIES_N = 3
_RECON_SERIES_WINDOW_SEC = 1800


def _offer_latest(q, blk):
    """Слот «последний блок» (Queue maxsize=1) для воркера-ассессора: занятый воркер не копит
    очередь — устаревший блок вытесняется свежим (ассессмент по старому блоку бесполезен).
    -> True, если старый блок вытеснен (сигнал деградации, лог троттлится у вызывающего)."""
    try:
        q.put_nowait(blk)
        return False
    except queue.Full:
        dropped = False
        try:
            q.get_nowait()
            dropped = True
        except queue.Empty:
            pass
        try:
            q.put_nowait(blk)
        except queue.Full:
            pass
        return dropped


def _source_params(cfg, active):
    """(url, connect_kwargs, generator) for the active block source. flashblocks = the public feed
    (~2.1s earlier, brotli, large frames -> max_size=None); newheads = the RPC wss (fallback)."""
    if active == "flashblocks":
        return (_FLASHBLOCK_URL,
                {"open_timeout": 20, "ping_interval": 20, "max_size": None},
                _flashblock_heads)
    # newheads-fallback WSS: явный BLOCK_WSS_URL (иначе наивная https→wss от rpc_url). НУЖЕН, когда у
    # keyed-RPC WSS на ДРУГОМ пути, чем HTTPS (dRPC: HTTPS .../base/<key>, а WSS .../ogws?...&dkey=<key>
    # → наивная замена даёт битый URL → newheads RuntimeError). Инцидент 07-21: переезд на dRPC сломал
    # fallback; publicnode WSS (кап-фри) или dRPC ogws — рабочие. Пусто = старое поведение (Alchemy-совместимо).
    wss = cfg.block_wss_url or cfg.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
    return (wss, {"open_timeout": 20, "ping_interval": 20}, _new_heads)


async def block_driven_loop(rpc, markets, store, cfg, guard, alerter, log, ctx_cache):
    """wss newHeads -> fast on-chain assess+execute every block (~2s). Background thread refreshes
    candidates+markets so the per-block path never blocks on the API. Reconnects on wss drop."""
    import websockets
    _bg_read_rpc = rpc                            # ассессмент читаем с бесплатного bg-RPC (CU 25.07)
    try:
        # 05.08 страйп: массовая нога (ассессмент ×96/окно + route-depth) едет на ключ №2,
        # чтобы её квота-шторм не топил триггерную ногу (preconf/predict/кэш на ключе №1).
        # Пусто => ключ №1 (прежнее поведение). Откат: BG_RPC_URL_2= (пусто).
        _u = str(getattr(cfg, "bg_rpc_url2", "") or "") or str(getattr(cfg, "bg_rpc_url", "") or "")
        if _u and _u != cfg.rpc_url:
            _bg_read_rpc = BaseRpc(_u, cfg.chain_id, request_timeout=10)
            if getattr(cfg, "bg_rpc_url2", ""):
                log.info("bg-страйп: ассессмент+route-depth -> ключ №2 (%s…)",
                         _u.split("//")[-1].split("/")[0])
    except Exception:
        _bg_read_rpc = rpc
    shared = _Shared(markets)
    _snap = _load_candidates_snapshot(cfg.candidates_snapshot_path)      # P0-1: закрыть слепое окно рестарта
    if _snap:
        _g, _db, _dab, _hb, _n = _snap
        with shared.lock:
            shared.groups, shared.debt_by, shared.debt_assets_by, shared.n = _g, _db, _dab, _n
            shared.hf_by = _hb
        log.info("warm-start: %d candidates / %d markets из снапшота (age<=%.0fс) — hot path видит набор с первого блока",
                 _n, len(_g), WARM_START_MAX_AGE_SEC)
    stop = threading.Event()
    threading.Thread(target=_refresh_worker, args=(shared, markets, cfg, ctx_cache, log, stop, alerter),
                     daemon=True).start()
    if getattr(cfg, "hot_path", False):                      # preconf-triggered hot path (OFF until ARM)
        from chain.hotpath import hot_loop
        preconf_rpc = BaseRpc(cfg.preconf_rpc or "https://mainnet-preconf.base.org", cfg.chain_id)

        def _run_hot():
            import asyncio as _a
            time.sleep(HOT_LOOP_START_STAGGER_SEC)     # размазать залп: дать cold-start mega-fetch/refresh block-loop схлынуть
            _heal_n = 0
            while not stop.is_set():                   # self-heal: init-429/крах НЕ должен молча убивать fast-путь (инцидент 07-21)
                _t0 = time.monotonic()
                try:
                    _a.run(hot_loop(rpc, preconf_rpc, cfg, shared, store, guard, alerter, log, stop, markets))
                    break                              # чистый выход (stop выставлен)
                except Exception:
                    # прожил >120с = разовый сбой (сброс); быстрый повтор = крэш-цикл (dRPC 429 на init-залпе)
                    # -> backoff, чтобы рестарты НЕ долбили dRPC init и он восстановился (5→10→20→40→60с)
                    _heal_n = 0 if (time.monotonic() - _t0) > 120 else _heal_n + 1
                    _hw = min(5 * (2 ** _heal_n), 60)
                    log.exception("hot_loop упал — self-heal перезапуск через %.0fс (backoff #%d)" % (_hw, _heal_n))
                    time.sleep(_hw)
        threading.Thread(target=_run_hot, daemon=True).start()
        # Ш28: замер ДОМИНИРУЮЩЕГО КЛАССА tip. Без этого вызова dominant_tip_wei() всегда 0,
        # горячий путь молча откатывается на прежние 0.05/0.5 gwei, и правка мертва — ровно
        # тот шов, на котором мы уже горели (арм доски пять суток жил под заглушкой).
        # Ходит по bg-стрипу, НЕ по боевому ключу: это диагностика, а не деньги.
        try:
            from chain.hotpath import start_tip_sampler
            start_tip_sampler(os.environ.get("COMBAT_TIP_RPC_URL", "")
                              or os.environ.get("BG_RPC_URL_2", "")
                              or os.environ.get("BG_RPC_URL", "")
                              or "https://mainnet.base.org", log=log)
        except Exception as _e:
            log.error("TIP-CLASS: замер класса НЕ поднят (%s) — боевой tip останется прежним",
                      type(_e).__name__)
        log.warning("HOT PATH ENABLED — preconf trigger live (tip=%.2f gwei, daily-loss<=$%.0f, repaid>=$%.0f)",
                    cfg.tip_gwei, cfg.max_daily_loss_usd, getattr(cfg, "hot_min_repaid_usd", 2000.0))
    # LIVE-CAP вотчер (04.08, weETH tail): держит живой кап капнутых роутов, пока на паре
    # есть кандидат >= $25k — реактивный путь стреляет в непредсказуемый момент (peg-пары
    # predict не армит), а протухший статик-кап ошибается в обе стороны (04.08: пул weETH
    # плоский до 260 при статик 160 и seize жертвы 168). Слой опционален, огневой путь не
    # трогает; откаты ROUTE_LIVE_CAP=0 (потребление) / LIVE_CAP_WATCH=0 (вотчер).
    try:
        from chain.route_depth import (RouteDepthWatcher, LIVE_CAP_MIN_DEBT_USD,
                                       LIVE_CAP_REFRESH_SEC)
        _rdw = RouteDepthWatcher(_bg_read_rpc, shared)   # фоновое чтение = bg-RPC (CU 25.07);
        # 429-шторм на bg -> fail-open в статик; горячий перемер в prearm-окне идёт боевым rpc
        if _rdw.start() is not None:
            log.warning("LIVE-CAP WATCHER ENABLED — глубина капнутых роутов "
                        "(долг>=$%.0fk, замер раз в %.0fс на роут)",
                        LIVE_CAP_MIN_DEBT_USD / 1e3, LIVE_CAP_REFRESH_SEC)
    except Exception:
        log.exception("live-cap вотчер не стартовал (опционален, статик-капы в силе)")
    if getattr(cfg, "predict_shadow", False):
        # PREDICT-слой (порт Katana v5), SHADOW: Binance WS + anchor/return-движок меряет lead/FP
        # на живом потоке (PREDICT-строки в лог), НИЧЕГО не стреляет и fire-путь не трогает.
        # Резолв прокси и старт — в фоне: старт бота не ждёт сеть предикта.
        def _run_predict():
            try:
                from chain.pricefeed import PriceFeed
                from chain.predict import PredictEngine, PredictDriver, resolve_feed_proxies, make_poll_fn
                # predict-полл агрегаторов = фоновое чтение (CU 25.07). #33 (09.08): у полла
                # СВОЯ ручка, потому что деление bg-ключа с прочими фоновыми потребителями
                # замерено как потеря оси предвидения: за сутки 226 отказов 429, из них 127 с
                # экспоненциальным backoff — 1725с ≈ 29 МИНУТ слепоты, и слепнет именно тот
                # слой, который даёт нам упреждение (реакция гонки не берёт). Пустая
                # PREDICT_RPC_URL = прежнее поведение (bg-ключ), то есть откат мгновенный.
                _bg, _src = rpc, "боевой"
                try:
                    from chain.predict import pick_poll_endpoint
                    _url, _src = pick_poll_endpoint(
                        os.environ.get("PREDICT_RPC_URL", ""),
                        str(getattr(cfg, "bg_rpc_url", "") or ""), cfg.rpc_url)
                    if _url:
                        _bg = BaseRpc(_url, cfg.chain_id, request_timeout=10)
                except Exception:
                    _bg, _src = rpc, "боевой"
                # КАТЕГОРИЯ, а не URL: в URL живёт ключ, и однажды он уже уехал в журнал
                # через str(e) сетевого слоя. Знать, на чьём ключе сидит полл, надо — но по
                # имени источника, не по адресу.
                log.warning("predict-полл: узел=%s (429 на этом узле = слепота предвидения)", _src)
                proxies = resolve_feed_proxies(rpc, markets)
                if not proxies:
                    # #50: раньше это был RETURN НАВСЕГДА — при RPC-шторме на буте слой не
                    # поднимался до рестарта. Тот же класс, ради которого hot_loop получил
                    # self-heal после инцидента 07-21. Здесь — внешняя петля ретрая.
                    log.warning("predict-shadow: прокси не разрезолвились — повтор через %.0fс",
                                _PREDICT_RETRY_SEC)
                    return "retry"
                # ЗНАМЕНАТЕЛЬ — ФИДЫ, А НЕ РЫНКИ (мой ложный алерт 09.08: «7 из 41», где 41 —
                # число рынков; десятки рынков делят один фид, а у стабл-пар его нет вовсе —
                # тот же класс «не тот объект в знаменателе», что уже стоил разбора).
                # Сравнивать честно можно только с ПРЕДЫДУЩИМ успешным резолвом этого процесса:
                # просадка покрытия = деградация, а первый резолв — просто факт.
                _prev = _predict_feeds_seen.get("n")
                if _prev is not None and len(proxies) < _prev:
                    log.error("predict-shadow: покрытие фидов ПРОСЕЛО %d -> %d "
                              "(детект наследует урезанный список)", _prev, len(proxies))
                _predict_feeds_seen["n"] = max(_prev or 0, len(proxies))
                engine = PredictEngine()
                pf = PriceFeed(symbols=list(proxies), log=lambda m: log.info("%s", m))
                pf.start()
                _handlers = []
                if os.environ.get("PREDICT_SPEC_CANARY", "").lower() in ("1", "true", "yes", "on"):
                    # SPEC-канарейка: замер достижимости предочереди (delta 0..5) спам-пробами
                    # в predict-окнах. Копеечные self-tx, общий канареечный газ-кап, ликвидаций нет.
                    from chain.spec_canary import SpecCanary
                    _sc = SpecCanary(rpc, cfg, proxies)
                    _handlers.append(_sc.handle_event)
                    log.warning("SPEC-CANARY ENABLED — predict-окна пробивают предочередь (кап %s окон)",
                                os.environ.get("SPEC_CANARY_MAX_WINDOWS", "60"))
                if os.environ.get("PREDICT_SPEC_FIRE_SHADOW", "").lower() in ("1", "true", "yes", "on"):
                    # SPEC-FIRE shadow (24.07, гейт снят бэктестом каскада 24-25.06): считает ЦЕЛИ
                    # из нашего candidate set в каждом predict-окне (targets/fat/debt). Ноль отправок.
                    try:
                        from chain.spec_fire import SpecFireShadow
                        _sf = SpecFireShadow(cfg, shared)
                        _handlers.append(_sf.handle_event)
                        log.warning("SPEC-FIRE SHADOW ENABLED — цели в predict-окнах (fat>=$%.0fk), ноль отправок",
                                    _sf.fat_usd / 1e3)
                    except Exception:
                        log.exception("spec-fire shadow не стартовал (опционален, канарейка/predict не задеты)")
                if os.environ.get("PREDICT_PREARM", "").lower() in ("1", "true", "yes", "on"):
                    # PRE-ARM Stage A (24.07): греет Kyber-квоты top-K целей фида в окне (weETH и
                    # мега-сайз). Ноль отправок; потребление — прозрачный кэш в kyber_swap.
                    try:
                        from chain.prearm import PreArmWorker, PREARM_MIN_DEBT_USD, PREARM_TOP_K
                        _pa = PreArmWorker(rpc, cfg, shared)
                        _handlers.append(_pa.handle_event)
                        log.warning("PRE-ARM ENABLED — Kyber-прогрев в окнах (top-%d, debt>=$%.0fk), ноль отправок",
                                    PREARM_TOP_K, PREARM_MIN_DEBT_USD / 1e3)
                    except Exception:
                        log.exception("pre-arm не стартовал (опционален, остальные слои не задеты)")
                if os.environ.get("SPEC_FIRE_LIVE", "").lower() in ("1", "true", "yes", "on"):
                    # SPEC-FIRE LIVE (Stage B, 24.07): боевые пробы по жиру >=$500k в последние
                    # секунды окна (|ret|>=dev). Канареечная nonce-машинерия, свой газ-бакет,
                    # пробы молчат при inflight>0 и НЕ трогают дедуп реактивного пути.
                    try:
                        from chain.spec_fire_live import (SpecFireLive, SPEC_FIRE_MIN_REPAID_USD,
                                                          SPEC_FIRE_MAX_PROBES_WINDOW)
                        from chain.predict import FEEDS_BASE
                        _l2s = {row["label"].upper(): sym for sym, row in FEEDS_BASE.items()}
                        _dev = {row["label"].upper(): float(row["dev"]) for row in FEEDS_BASE.values()}
                        _sfl = SpecFireLive(rpc, cfg, shared,
                                            ret_fn=lambda lbl: engine.ret(_l2s.get(lbl.upper(), lbl)),
                                            dev_by=_dev)
                        # У2 (09.08): немота ног по комиссии требует человека (долив либо
                        # ручка MAXFEE_RESERVE_MULT) — даём окну канал к владельцу
                        _sfl._alert = lambda _t, _k: alerter.send_async(_t, key=_k)
                        from chain.execute import set_combat_win_hook
                        set_combat_win_hook(_sfl.note_combat_win_by_market)   # #58
                        _handlers.append(_sfl.handle_event)
                        from chain.failover import set_windows_probe
                        set_windows_probe(lambda: len(_sfl._windows))   # назад-свитч не посреди окна
                        # WS-handoff (06.08): транзит из recv-петли -> pending_fire армленному окну
                        from chain.hotpath import set_ws_transmit_hook
                        set_ws_transmit_hook(_sfl.note_ws_transmit)
                        # PENDING-DETECT (пакет A, 04.08): детект транзитов по формируемому блоку
                        # на ОТДЕЛЬНОМ бесключевом провайдере; handoff в окно атомарным флагом.
                        try:
                            from chain.pending_detect import PendingDetect, PENDING_DETECT, PENDING_TIER1_HZ
                            if PENDING_DETECT:
                                _pl = {row["label"].upper(): proxies[sym]
                                       for sym, row in FEEDS_BASE.items() if sym in proxies}
                                _pd = PendingDetect(cfg, _sfl, _pl)
                                if _pd.start() is not None:
                                    _sfl.pending_healthy_fn = _pd.healthy   # A3: кипэлайв лестницы
                                    log.warning("PENDING-DETECT ENABLED — ярус-1 %.0f Гц по %d фидам, "
                                                "провайдеры %s (Alchemy запрещён кодом)",
                                                PENDING_TIER1_HZ, len(_pl),
                                                [u.split('//', 1)[-1].split('/', 1)[0] for u in _pd.urls])
                        except Exception:
                            log.exception("pending-detect не стартовал (опционален, окна на полной каденсии)")
                        from chain.execute import _SPECFIRE_MAX_GAS_USD as _sf_bucket
                        log.warning("SPEC-FIRE LIVE ENABLED — пробы по жиру >=$%.0fk при |ret|>=dev "
                                    "(кап %d проб/окно, газ-бакет $%.0f)",
                                    SPEC_FIRE_MIN_REPAID_USD / 1e3, SPEC_FIRE_MAX_PROBES_WINDOW,
                                    _sf_bucket)
                    except Exception:
                        log.exception("spec-fire live не стартовал (опционален, остальные слои не задеты)")
                _on_event = None
                if _handlers:
                    def _on_event(ev, _hs=tuple(_handlers)):
                        for _h in _hs:              # каждый хендлер в своём try — падение одного
                            try:                    # не глушит остальных (канарейка главнее)
                                _h(ev)
                            except Exception:
                                log.exception("predict on_event handler упал (изолирован)")
                # 05.08 bg-CU: полл latestRoundData 2с -> 4с (~11M CU/мес с bg-ключа; confirmed-
                # метка грубеет на ±2с при lead med 67с — незаметно). Откат PREDICT_POLL_SEC=2.
                # #82: запасной узел на время отказа основного. Замер: отказов основного ~1%
                # (226/21600 в сутки), но backoff превращает их в ~29 минут ПОЛНОЙ слепоты;
                # публичный узел даёт 26% отказов, то есть 74% успеха — несравнимо лучше нуля.
                # Пустая PREDICT_FALLBACK_RPC_URL = прежнее поведение (уход в backoff).
                _fb_fn = None
                try:
                    _fbu = str(os.environ.get("PREDICT_FALLBACK_RPC_URL",
                                              "https://mainnet.base.org") or "")
                    if _fbu and _fbu != _url:
                        _fb_fn = make_poll_fn(BaseRpc(_fbu, cfg.chain_id, request_timeout=10),
                                              proxies)
                except Exception:
                    _fb_fn = None
                drv = PredictDriver(engine, mid_fn=pf.mid, poll_fn=make_poll_fn(_bg, proxies),
                                    fallback_poll_fn=_fb_fn,
                                    on_event=_on_event,
                                    poll_interval=float(os.environ.get("PREDICT_POLL_SEC", "4.0")),
                                    log=lambda m: log.info("%s", m))
                drv.start()
                log.warning("PREDICT SHADOW ENABLED — %d feeds (%s), меряет lead/FP, не стреляет",
                            len(proxies), ",".join(sorted(proxies)))
                return "ok"
            except Exception:
                log.exception("predict-shadow не стартовал — повтор через %.0fс", _PREDICT_RETRY_SEC)
                return "retry"

        def _run_predict_supervised():
            """#50: слой стартовал ОДНОКРАТНО — пустой резолв или исключение оставляли предиктор
            мёртвым до рестарта, а он и есть верх сигнального тракта. Петля с бэкоффом: как у
            _run_hot, получившего self-heal после инцидента той же природы."""
            _delay = _PREDICT_RETRY_SEC
            while True:
                try:
                    if _run_predict() == "ok":
                        return                     # слой поднялся и живёт в своих тредах
                except Exception:
                    log.exception("predict-shadow: надзор поймал исключение")
                time.sleep(_delay)
                _delay = min(_delay * 2, _PREDICT_RETRY_MAX_SEC)

        threading.Thread(target=_run_predict_supervised, daemon=True).start()
    def _assess_block(blk, st):
        """Пер-блочный ассессмент — бывшее тело recv-цикла, 1:1. st — состояние ассессора
        (hf_cache/троттлы/пульс), локальное для его потока.
        30.07: под штормом МЕДЛЕННЫХ отказов RPC (408/таймаут ~7.4с; вчерашние быстрые 429 флапа
        не давали) inline-ассессмент держал event loop 7-10с/блок — сервер рвал flashblocks-WS как
        slow-consumer: 83 обрыва block-loop за 3ч при 0 у hot_loop в том же процессе (у того recv
        чистый с 07-23, «inline-резолв глушил hot-path» — тот же класс бага)."""
        if time.time() - st["last_pulse"] >= 30.0:
            hb_beat("bot-block")             # пульс = живой АССЕССМЕНТ, не просто живой WS:
            st["last_pulse"] = time.time()   # зависший воркер будит heartbeat-check, как раньше inline
        by_id, groups, debt_by, debt_assets_by, ncand = shared.snapshot()
        today = store.realized_today()
        gs = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"] + specfire_gas_today(),
                        inflight=current_inflight())
        blocked = guard.blocked_reason(gs)
        if blocked and cfg.mode == "execute":
            if time.time() - st["last_hb"] >= HEARTBEAT_SEC:
                log.error("kill switch engaged: %s", blocked)
                alerter.send_async(f"\U0001F6D1 liquidator halted: {blocked}", key="killswitch")
                st["last_hb"] = time.time()
            return
        if not groups:
            return
        hf_cache = st["hf_cache"]
        do_full = time.time() - st["last_full"] >= HOT_FULL_REFRESH_SEC
        if do_full:
            st["last_full"] = time.time()
            live = {(m, b) for m, bs in groups.items() for b in bs}
            for k in [k for k in hf_cache if k not in live]:
                del hf_cache[k]                       # prune cache to current set
        else:
            groups = _hot_subset(groups, hf_cache)    # per block: only near-threshold
        t0 = time.time()
        try:
            s = process_candidates(rpc, by_id, groups, debt_by, debt_assets_by, store, cfg,
                                   guard, alerter, log, ctx_cache, ncand, hf_cache=hf_cache,
                                   read_rpc=_bg_read_rpc)
        except Exception as e:
            # RPC-split (07-21): per-block process_candidates ходит по общей ноде (сейчас
            # публичная, лимитированная) — её транзиентный HTTPError/429 НЕ должен ронять ассессор.
            # Пропускаем ЭТОТ блок; следующий блок повторит. Троттл-лог 1/30с.
            _bnow = time.monotonic()
            if _bnow - st["last_blockerr"] >= 30.0:
                log.warning("block %s assess skipped (%s) — RPC hiccup, следующий блок повторит",
                            blk, type(e).__name__)
                st["last_blockerr"] = _bnow
            return
        if s["liquidatable"] or s["profitable"] or (time.time() - st["last_hb"] >= HEARTBEAT_SEC):
            st["last_hb"] = time.time()
            tag = "full" if do_full else "hot"
            mh = f"{s['min_hf']:.4f}" if s["min_hf"] is not None else "n/a"
            log.info("block %d: assessed=%d/%d (%s) liq=%d profit=%d min_hf=%s in %.2fs",
                     blk, s["confirmed"], ncand, tag, s["liquidatable"],
                     s["profitable"], mh, time.time() - t0)

    _assess_st = {"last_hb": 0.0, "last_full": 0.0, "last_pulse": 0.0, "last_blockerr": 0.0,
                  "hf_cache": {}}
    # 30.07: ассессмент УХОДИТ ИЗ recv-петли в один воркер (симметрия с hot_loop 07-23) — WS
    # всегда читает кадры, busy-воркер вытесняет устаревший блок. BLOCK_ASSESS_ASYNC=0 = откат inline.
    _assess_async = os.environ.get("BLOCK_ASSESS_ASYNC", "1").lower() not in ("0", "false", "off")
    _assess_q: queue.Queue = queue.Queue(maxsize=1)
    _skip_n = 0
    _skip_log_ts = 0.0
    if _assess_async:
        def _assess_worker():
            while not stop.is_set():
                try:
                    _blk = _assess_q.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    _assess_block(_blk, _assess_st)
                except Exception:                     # баг ассессора не убивает воркер
                    log.exception("assess-воркер: ошибка на блоке %s (воркер жив)", _blk)
        threading.Thread(target=_assess_worker, daemon=True, name="block-assess").start()
        log.info("block-assess: воркер-тред активен (recv-петля не блокируется; BLOCK_ASSESS_ASYNC=0 = откат)")
    active = cfg.block_source if cfg.block_source in ("flashblocks", "newheads") else "newheads"
    fails = 0
    recon_ts: list[float] = []   # моменты ws-обрывов для серия-гейта TG (см. _RECON_SERIES_N)
    fbret = FbAutoReturn()       # авто-возврат flashblocks (05.08)
    while True:
        if (active == "newheads" and cfg.block_source == "flashblocks"
                and fbret.due(time.monotonic())):
            active = "flashblocks"
            fails = _FLASHBLOCK_FALLBACK_AFTER - 1   # одна неудача пробы -> немедленный откат
            fbret.on_probe(time.monotonic())
            log.info("flashblocks auto-return: проба возврата (при неудаче следующая через %.0fс)",
                     fbret.delay)
        url, conn_kw, gen = _source_params(cfg, active)
        try:
            async with websockets.connect(url, **conn_kw) as ws:
                log.info("block source=%s connected — loop (mode=%s)", active, cfg.mode)
                async for blk in gen(ws):
                    # #110: подача блока фоновому индексу. Только присваивание номера и
                    # Event.set — ни сети, ни лока, ни треда: вся работа живёт в СВОЁМ
                    # долгоживущем треде индекса. Индекса нет -> мгновенный выход.
                    chain_index.offer_block(blk)
                    # Гистерезис пробы (05.08): до подтверждённой стабильности fails НЕ сбрасываем —
                    # хлопающий источник (аптайм < stable_sec) валится в тихий probe-fail с бэкоффом,
                    # а не в пинг-понг 3с-реконнектов (урок 07-21: per-IP лимит flashblocks).
                    if not fbret.probing:
                        fails = 0   # blocks are flowing on this source
                    elif active == "flashblocks" and fbret.on_block(time.monotonic()):
                        fails = 0
                        log.info("flashblocks auto-return: источник ВЕРНУЛСЯ, стабилен %.0fс "
                                 "(fallback#%d restore#%d)", fbret.stable_sec,
                                 fbret.n_fallbacks, fbret.n_restores)
                        alerter.send_async("\U00002705 flashblocks восстановлен (авто-возврат, "
                                           "стабилен %.0fс)" % fbret.stable_sec, key="fbreturn")
                    if _assess_async:
                        if _offer_latest(_assess_q, blk):
                            _skip_n += 1
                            _snow = time.monotonic()
                            if _snow - _skip_log_ts >= 60.0:
                                log.info("DIAG assess-lag: %d блоков вытеснено (воркер занят) — WS жив",
                                         _skip_n)
                                _skip_n = 0
                                _skip_log_ts = _snow
                    else:
                        _assess_block(blk, _assess_st)
        except Exception as e:
            log.warning("block source=%s error (%s) — reconnecting in 3s", active, type(e).__name__)
            _now = time.monotonic()
            recon_ts[:] = [t for t in recon_ts if _now - t < _RECON_SERIES_WINDOW_SEC] + [_now]
            if len(recon_ts) >= _RECON_SERIES_N:     # одиночный блип — только журнал (TG-гигиена 07-10)
                alerter.send_async("\U0001F6A8 block-loop: %d reconnects/%dm — flapping, check journalctl"
                                   % (len(recon_ts), _RECON_SERIES_WINDOW_SEC // 60), key="wsserr")
            if active == "flashblocks":
                fails += 1
                if fails >= _FLASHBLOCK_FALLBACK_AFTER:
                    active = "newheads"; fails = 0
                    if fbret.probing:            # неудачная проба возврата — тихо, по бэкоффу
                        fbret.probing = False
                        log.info("flashblocks auto-return: не вернулись (%s) — следующая проба "
                                 "через %.0fс", type(e).__name__,
                                 max(0.0, fbret.next_at - time.monotonic()))
                    else:                        # настоящий фолбэк: TG + старт таймера проб
                        fbret.on_fallback(time.monotonic())
                        log.error("flashblocks feed unstable — FALLING BACK to newHeads "
                                  "(fallback#%d restore#%d)", fbret.n_fallbacks, fbret.n_restores)
                        alerter.send_async("\U0001F504 flashblocks -> newHeads fallback", key="fbfallback")
            await asyncio.sleep(3)


_predict_feeds_seen: dict = {}          # максимум покрытия фидов, виденный процессом
_PREDICT_RETRY_SEC = float(os.environ.get("PREDICT_RETRY_SEC", "30"))
_PREDICT_RETRY_MAX_SEC = float(os.environ.get("PREDICT_RETRY_MAX_SEC", "300"))


def main():
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    log = logging.getLogger("liquidator")

    store = Store(cfg.db_path)
    alerter = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, cfg.alert_antiflood_sec, tag=cfg.bot_tag)
    from chain.execute import check_gas_budget_sane, sign_backend_probe
    check_gas_budget_sane(cfg.max_daily_gas_usd, log)   # #53: несогласованность — громко на старте
    sign_backend_probe(log=log)   # #COIN: чем и почём подписываем — фактом из журнала, не верой
    guard = KillSwitch(cfg.max_daily_loss_usd, cfg.max_daily_gas_usd, cfg.max_inflight)
    markets = load_covered_markets(cfg.covered_markets_path)
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id) if cfg.rpc_url else None
    if rpc is not None:
        from chain.failover import build_failover
        rpc = build_failover(rpc, cfg, alerter)   # без COMBAT_FAILOVER_URLS вернёт rpc как был
    ctx_cache: dict = {}   # immutable per-market params (oracle, lltv_wad), filled on first sight

    # #110 ФОНОВЫЙ ИНДЕКС ЦЕПИ. Поднимается ДО петель, на СВОЁМ (не боевом) узле. Он не в
    # горячем пути и ничего не отправляет: читает по одному блоку и раздаёт из памяти исходы
    # наших проб, их ПОЗИЦИИ, tip транзита и свежих трупов. Без него приёмка любого пакета
    # читается счётчиком, который под 429 лжёт (см. docs/ROLLBACKS.md, CHAIN_INDEX=0).
    chain_index.start_index(cfg=cfg, watch_to=[cfg.liquidator_address])
    # #123: «отказ при чужой победе» — арм ЯВНЫЙ и только здесь (боевая точка входа, канон
    # notify_fn: забытый арм = молчание, не выстрел). Прибор сбоку: осечка арма не роняет старт.
    try:
        from chain import hotpath as _hp123
        _idx123 = chain_index.get_index()
        if _idx123 is not None and _hp123.REFUSAL_LOSS_ALERT:
            _idx123.set_on_liquidate(_hp123.make_refusal_loss_watcher(alerter, cfg))
            log.info("#123 refusal-loss АРМИРОВАН: отказ+чужая победа в TTL-окне -> TG")
        else:
            log.info("#123 refusal-loss ВЫКЛЮЧЕН (%s)",
                     "REFUSAL_LOSS_ALERT=0" if _idx123 is not None else "индекса нет")
    except Exception:
        log.exception("#123 refusal-loss: арм не удался — канал закрыт, огонь не затронут")

    log.info("liquidator start: mode=%s chain=%s markets=%d rpc=%s",
             cfg.mode, cfg.chain, len(markets), "set" if rpc else "MISSING")
    if not cfg.rpc_url:
        log.warning("RPC_URL empty — only API enumeration, no on-chain confirm")
    if cfg.mode == "execute":
        log.warning("EXECUTE mode — real txs. kill switch: loss<=$%.0f gas<=$%.0f",
                    cfg.max_daily_loss_usd, cfg.max_daily_gas_usd)
        if rpc:
            from chain.execute import prewarm_local_nonce, volley_accounts
            prewarm_local_nonce(rpc, cfg.wallet_key)   # 07-23: снять единственный RPC-hop с ПЕРВОГО залпа
            volley_accounts()   # тир 07.08: ростер залповых EOA в старт-лог (позитивное
                                # доказательство загрузки ключей; пусто = тир молчит)

    if cfg.loop_mode == "block" and rpc:
        asyncio.run(block_driven_loop(rpc, markets, store, cfg, guard, alerter, log, ctx_cache))
        return

    last_hb = 0.0
    last_rescan = time.time()
    while True:
        try:
            if rpc and time.time() - last_rescan >= cfg.rescan_interval_sec:
                markets = rescan_markets(cfg, markets, ctx_cache, log, alerter)
                last_rescan = time.time()
            t0 = time.time()
            today = store.realized_today()
            st = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"] + specfire_gas_today(),
                            inflight=current_inflight())
            blocked = guard.blocked_reason(st)
            if blocked and cfg.mode == "execute":
                log.error("kill switch engaged: %s", blocked)
                alerter.send_async(f"\U0001F6D1 liquidator halted: {blocked}", key="killswitch")
            s = scan_once(rpc, markets, store, cfg, guard, alerter, log, ctx_cache) if markets else _empty_summary(0)
            hb_beat("bot-block")
            now = time.time()
            if (s["liquidatable"] or s["profitable"]) or (now - last_hb >= HEARTBEAT_SEC):
                last_hb = now
                mh = f"{s['min_hf']:.4f}" if s["min_hf"] is not None else "n/a"
                log.info("scan: markets=%d api_cand=%d confirmed=%d liq=%d profit=%d min_hf=%s in %.2fs",
                         s["markets"], s["api_candidates"], s["confirmed"], s["liquidatable"],
                         s["profitable"], mh, time.time() - t0)
        except Exception:
            log.exception("loop error")
            alerter.send_async("\U0001F6A8 liquidator loop error — check journalctl", key="looperr")
        time.sleep(cfg.poll_interval_sec)


if __name__ == "__main__":
    main()
