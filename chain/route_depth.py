"""LIVE-CAP замер (04.08, weETH tail): живая ёмкость капнутых прямых роутов.

Ликвидность тонких CL-пулов подвижна (см. коммент у _LIVE_CAPS в chain/execute.py). Один
замер = ОДНА aggregate3-пачка квот у квотера DEX'а роута: базовая квота (1% статик-капа) +
лестница долей статик-капа. Live cap = верхняя ступень с импактом <= LIVE_CAP_MAX_IMPACT_PCT
-> chain.execute.set_live_cap (TTL, рост капнут там же). Семантика ошибок РАЗНАЯ:
реверт ступени = ДАННЫЕ (пул не тянет — режем кап, вплоть до 0 = полный Kyber-фолбэк),
транспортная ошибка RPC = fail-open (live-кап не трогаем, протухнет в статик сам).

Триггеры: (а) RouteDepthWatcher — фоновая петля, меряет капнутый роут пока на его паре живёт
кандидат с долгом >= LIVE_CAP_MIN_DEBT_USD (адреса пары — из idToMarketParams, кэш навсегда:
params рынка иммутабельны); (б) prearm-окно — немедленный замер по горячей цели. Оба через
maybe_refresh (пер-роутный rate-limit LIVE_CAP_REFRESH_SEC). Слой опционален: смерть вотчера
не трогает огневой путь — потребители вернутся к статик max_in по TTL.

Откаты: ROUTE_LIVE_CAP=0 — потребители игнорируют live-кап (execute.py); LIVE_CAP_WATCH=0 —
вотчер не стартует (замер остаётся только из prearm-окон)."""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("liq.route_depth")

AERO_QUOTER = "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"    # Slipstream QuoterV2 (бенч 28.07)
UNIV3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"   # UniV3 QuoterV2 (сан-квота 24.07)
_QUOTE_SEL = bytes.fromhex("cdca1753")                        # quoteExactInput(bytes,uint256)
_MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

LIVE_CAP_LADDER = (0.25, 0.5, 1.0, 1.5, 2.0)                  # доли статик max_in
LIVE_CAP_MAX_IMPACT_PCT = float(os.environ.get("LIVE_CAP_MAX_IMPACT_PCT", "0.5"))
LIVE_CAP_REFRESH_SEC = float(os.environ.get("LIVE_CAP_REFRESH_SEC", "120"))
LIVE_CAP_MIN_DEBT_USD = float(os.environ.get("LIVE_CAP_MIN_DEBT_USD", "25000"))
LIVE_CAP_WATCH = os.environ.get("LIVE_CAP_WATCH", "1").lower() not in ("0", "false", "no")
LIVE_CAP_WATCH_POLL_SEC = float(os.environ.get("LIVE_CAP_WATCH_POLL_SEC", "30"))

_TS_LOCK = threading.Lock()
_LAST_TS: dict = {}      # (tin, tout) -> monotonic последней попытки замера


def _quote_batch(rpc, calls):
    from chain.multicall import aggregate3
    return aggregate3(rpc, calls)


def measure_route_cap(rpc, token_in, token_out, batch_fn=None, log_fn=None):
    """Один замер глубины роута -> set_live_cap. Возвращает применённый кап (wei token_in)
    или None (роут не капнутый / транспортная ошибка — live-кап не трогаем)."""
    from eth_abi import encode
    from chain.execute import _SWAP_ROUTES, set_live_cap
    _log = log_fn or (lambda m: log.info("%s", m))
    key = (token_in.lower(), token_out.lower())
    r = _SWAP_ROUTES.get(key)
    if r is None or r.get("max_in") is None:
        return None
    static = int(r["max_in"])
    quoter = AERO_QUOTER if r["style"] == "v1" else UNIV3_QUOTER
    sizes = [max(static // 100, 1)] + [int(static * f) for f in LIVE_CAP_LADDER]
    calls = [(quoter, _QUOTE_SEL + encode(["bytes", "uint256"], [r["path"], s]))
             for s in sizes]
    try:
        res = (batch_fn or _quote_batch)(rpc, calls)
    except Exception as e:
        _log("LIVECAP transport-fail %s->%s: %s (статик остаётся)"
             % (token_in[:8], token_out[:8], type(e).__name__))
        return None
    ok0, d0 = res[0]
    if not ok0 or len(d0) < 32:
        cap = 0          # базовая квота ревертнула: пул мёртв -> прямой роут закрыт, Kyber
    else:
        base = int.from_bytes(d0[:32], "big") / sizes[0]
        cap = 0
        for (ok, d), s in zip(res[1:], sizes[1:]):
            if not ok or len(d) < 32:
                break                                  # реверт ступени = данные: дальше хуже
            ratio = int.from_bytes(d[:32], "big") / s
            if base <= 0 or (1.0 - ratio / base) * 100.0 > LIVE_CAP_MAX_IMPACT_PCT:
                break                                  # AMM-импакт монотонен по размеру
            cap = s
        if cap == 0:
            cap = static // 10   # даже 0.25x не тянет: крошечный прямой сайз, остальное Kyber
    eff = set_live_cap(token_in, token_out, cap)
    _log("LIVECAP %s->%s: static=%d live=%s (x%.2f)"
         % (token_in[:8], token_out[:8], static, eff, (eff or 0) / static))
    return eff


def maybe_refresh(rpc, token_in, token_out, now=None, force=False, **kw):
    """Rate-limited обёртка measure_route_cap: не чаще LIVE_CAP_REFRESH_SEC на роут.
    Быстрый no-op для роутов без статик-капа. None = замер не проводился/не удался."""
    from chain.execute import _SWAP_ROUTES
    key = (token_in.lower(), token_out.lower())
    r = _SWAP_ROUTES.get(key)
    if r is None or r.get("max_in") is None:
        return None
    n = time.monotonic() if now is None else now
    with _TS_LOCK:
        if not force and n - _LAST_TS.get(key, -1e18) < LIVE_CAP_REFRESH_SEC:
            return None
        _LAST_TS[key] = n           # штампуем ПОПЫТКУ (и провал держит темп — не молотим RPC)
    return measure_route_cap(rpc, token_in, token_out, **kw)


class RouteDepthWatcher:
    """Фоновая петля: пока на капнутой паре живёт кандидат с долгом >= LIVE_CAP_MIN_DEBT_USD,
    держим live-кап свежим (реактивный путь стреляет в непредсказуемый момент — прогрев из
    prearm-окон его не покрывает: peg-пары predict не армит). Адреса пары рынка — один
    aggregate3 idToMarketParams на неизвестные mid, кэш навсегда (params иммутабельны)."""

    def __init__(self, rpc, shared, log_fn=None, resolve_fn=None, refresh_fn=None):
        self.rpc, self.shared = rpc, shared
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._resolve_fn = resolve_fn      # шов: [mid] -> {mid: (coll, loan)}
        self._refresh_fn = refresh_fn      # шов: (tin, tout) -> cap|None
        self._pair_by_mid: dict = {}       # mid -> (coll_lower, loan_lower)
        self._stop = threading.Event()
        self.stats = {"cycles": 0, "refreshes": 0, "errors": 0}

    def _resolve(self, mids):
        from chain.multicall import (aggregate3, encode_id_to_market_params_call,
                                     decode_id_to_market_params)
        out = {}
        calls = [( _MORPHO, encode_id_to_market_params_call(self.rpc.to_bytes32(m))) for m in mids]
        res = aggregate3(self.rpc, calls)
        for m, (ok, data) in zip(mids, res):
            if not ok or len(data) < 160:
                continue
            loan, coll, _o, _i, _l = decode_id_to_market_params(data)
            out[m] = (coll.lower(), loan.lower())
        return out

    def _cycle(self):
        from chain.execute import _SWAP_ROUTES
        capped = {k for k, r in _SWAP_ROUTES.items() if r.get("max_in") is not None}
        with self.shared.lock:
            debt_by = dict(self.shared.debt_by)
        # max долг по mid (только достойные замера)
        top_by_mid: dict = {}
        for (mid, _b), d in debt_by.items():
            d = float(d or 0.0)
            if d >= LIVE_CAP_MIN_DEBT_USD and d > top_by_mid.get(mid, 0.0):
                top_by_mid[mid] = d
        unknown = [m for m in top_by_mid if m not in self._pair_by_mid]
        if unknown:
            self._pair_by_mid.update((self._resolve_fn or self._resolve)(unknown))
        due = {self._pair_by_mid[m] for m in top_by_mid
               if self._pair_by_mid.get(m) in capped}
        for tin, tout in due:
            got = (self._refresh_fn or (lambda a, b: maybe_refresh(self.rpc, a, b,
                                                                   log_fn=self._log)))(tin, tout)
            if got is not None:
                self.stats["refreshes"] += 1
        self.stats["cycles"] += 1

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                self.stats["errors"] += 1
                self._log("LIVECAP watcher-fail: %s (слой опционален, статик остаётся)"
                          % type(e).__name__)
            self._stop.wait(LIVE_CAP_WATCH_POLL_SEC)

    def start(self):
        if not LIVE_CAP_WATCH:
            return None
        t = threading.Thread(target=self._loop, daemon=True, name="route-depth")
        t.start()
        return t

    def stop(self):
        self._stop.set()
