"""PRE-ARM (Stage A, 24.07): прогрев Kyber-квот в predict-окнах — ноль отправок.

Бэктест каскада 24-25.06: lead med 67с — за окно успеваем прогреть МЕДЛЕННУЮ часть выстрела.
Полный prep до флипа невозможен (sim ревертит на здоровой позиции), поэтому греем ровно то,
что в гонке стоит 0.4-0.9с: Kyber route+build для целей, которые пойдут Kyber-путём —
(а) пара ВНЕ _SWAP_ROUTES (weETH/WETH: пул ts=1 глубок лишь до ~$207k, Kyber-агрегат тянет),
(б) мега-сайз >= FAST_LANE_MAX_REPAID_USD на роутнутой паре (обрыв одиночного пула wstETH
~$1.6M — sim-путь возьмёт через Kyber). Роутнутая мелочь НЕ греется — direct_swap и так 0 RTT.

Потребление — прозрачный кэш в kyber_swap (chain/execute.py): хит, если возраст <= PREARM_TTL
и cached_amount_in в полосе [PREARM_BAND..1.0] от запрошенного. Инвариант сайзинга: цена идёт
В СТОРОНУ флипа (dir=down дешевеет залог, dir=up дорожает долг) => seized на транзите >= seized
при arm => cached amountIn <= фактического seized ВСЕГДА; недобор <= (1-BAND) остаётся в
контракте пылью (окупается снятым RTT), эконом-гейт minProfit/sim НЕ обходится нигде.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("liq.prearm")

PREARM_TTL_SEC = float(os.environ.get("PREARM_TTL_SEC", "45"))
PREARM_BAND = float(os.environ.get("PREARM_BAND", "0.97"))
PREARM_REFRESH_SEC = float(os.environ.get("PREARM_REFRESH_SEC", "15"))
PREARM_TOP_K = int(os.environ.get("PREARM_TOP_K", "3"))
# 28.07: 20000 -> 1000. Пол прогрева НЕ МОЖЕТ быть выше пола spec-fire (mid-tier $1k):
# негретая цель $1-20k без статик-роута = пробы «нет свопа» весь боевой цикл (SOL-окно 01:06).
PREARM_MIN_DEBT_USD = float(os.environ.get("PREARM_MIN_DEBT_USD", "1000"))
PREARM_MAX_WARMS = int(os.environ.get("PREARM_MAX_WARMS", "40"))   # кап Kyber-вызовов на окно
# CAP-AWARE (01.08, «го» kelbic после гонки cbDOGE): «роутнутая» пара считается прикрытой
# прямым пулом ТОЛЬКО если фактический сейз влезает в её max_in. Слепота к капам не грела
# Kyber для полосы weETH $330-500k и для cbDOGE-кита. Откат PREARM_CAP_AWARE=0.
PREARM_CAP_AWARE = os.environ.get("PREARM_CAP_AWARE", "1").lower() not in ("0", "false", "no")

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}    # (token_in_lower, token_out_lower) -> {swap, amount_in, ts, mid, borrower}


def prearm_put(token_in: str, token_out: str, swap: dict, mid: str = "", borrower: str = "",
               now=None) -> None:
    with _CACHE_LOCK:
        _CACHE[(token_in.lower(), token_out.lower())] = {
            "swap": swap, "amount_in": int(swap.get("amount_in") or 0),
            "ts": time.monotonic() if now is None else now, "mid": mid, "borrower": borrower}


def prearm_get(token_in: str, token_out: str, amount_in: int, now=None) -> dict | None:
    """Хит: возраст <= TTL и cached_amount_in в [BAND..1.0]*amount_in (см. инвариант в шапке)."""
    with _CACHE_LOCK:
        e = _CACHE.get((token_in.lower(), token_out.lower()))
    if not e:
        return None
    age = (time.monotonic() if now is None else now) - e["ts"]
    if age > PREARM_TTL_SEC:
        return None
    ca = e["amount_in"]
    if not (int(amount_in) * PREARM_BAND <= ca <= int(amount_in)):
        return None
    return {"swap": dict(e["swap"]), "age_sec": age, "mid": e["mid"], "borrower": e["borrower"]}


def prearm_clear() -> None:      # тестовый/аварийный сброс
    with _CACHE_LOCK:
        _CACHE.clear()


def routed_at_size(coll: str, loan: str, seized: int) -> bool:
    """True = прямой роут ЕСТЬ и фактический сейз влезает в его кап (греть Kyber незачем).
    С PREARM_CAP_AWARE=0 — старая бинарная семантика «роут есть» без учёта max_in."""
    from chain.execute import _SWAP_ROUTES
    r = _SWAP_ROUTES.get((coll.lower(), loan.lower()))
    if r is None:
        return False
    if not PREARM_CAP_AWARE:
        return True
    cap = r.get("max_in")
    return cap is None or int(seized) <= int(cap)


# токен -> predict-лейбл (копия семантики spec_fire: ведома ровно ОДНА нога => рынок feed-driven)
from chain.spec_fire import _label_of  # noqa: E402  (лёгкий импорт, без сети)


class PreArmWorker:
    """Потребитель событий PredictDriver: на arm греет Kyber-квоты top-K целей фида,
    рефреш каждые PREARM_REFRESH_SEC до конца окна. Ноль отправок, sim не обходится."""

    def __init__(self, rpc, cfg, shared, read_fn=None, quote_fn=None, log_fn=None):
        self.rpc, self.cfg, self.shared = rpc, cfg, shared
        self._read_fn = read_fn          # шов: (mid, borrower) -> dict|None
        self._quote_fn = quote_fn        # шов: (coll, loan, seized) -> swap dict
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._lock = threading.Lock()
        self._windows: dict = {}         # feed -> threading.Event (stop)
        self.stats = {"windows": 0, "warms": 0, "hits_logged_elsewhere": 0}

    # ---- боевые реализации швов ----
    def _read_target(self, mid, borrower):
        from chain.multicall import (aggregate3, encode_id_to_market_params_call,
            decode_id_to_market_params, encode_market_call, decode_market,
            encode_position_call, decode_position, encode_price_call, decode_price)
        from chain.simulate import to_assets_up
        from strategy.pnl import lif_from_lltv
        from chain.execute import expected_seized, _SWAP_ROUTES, _MORPHO_BLUE
        m32 = self.rpc.to_bytes32(mid)
        r1 = aggregate3(self.rpc, [(_MORPHO_BLUE, encode_id_to_market_params_call(m32)),
                                   (_MORPHO_BLUE, encode_market_call(m32)),
                                   (_MORPHO_BLUE, encode_position_call(m32, borrower))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
        mk = decode_market(r1[1][1]); tba, tbs = mk[2], mk[3]
        bs, _ = decode_position(r1[2][1])
        if bs == 0:
            return None
        price = decode_price(aggregate3(self.rpc, [(oracle, encode_price_call())])[0][1])
        repaid_assets = to_assets_up(bs, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
        return {"coll": coll, "loan": loan, "seized": int(seized),
                "routed": routed_at_size(coll, loan, int(seized))}

    def _quote(self, coll, loan, seized):
        from chain.execute import kyber_swap
        liq = self.cfg.liquidator_address
        return kyber_swap(coll, loan, seized, liq, liq)

    # ---- выбор целей (та же фид/направление-семантика, что у spec_fire) ----
    def _pick(self, feed: str, direction: str):
        from chain.execute import _SWAP_ROUTES
        fl_max = float(getattr(self.cfg, "fast_lane_max_repaid_usd", 500000.0) or 500000.0)
        rows = []
        with self.shared.lock:
            groups = dict(self.shared.groups)
            debt_by = dict(self.shared.debt_by)
            by_id = dict(self.shared.by_id)
        for mid, borrowers in groups.items():
            m = by_id.get(mid)
            if m is None:
                continue
            cl = _label_of(getattr(m, "collateral_token", ""))
            ll = _label_of(getattr(m, "loan_token", ""))
            if cl == ll or not ((direction == "down" and cl == feed) or (direction == "up" and ll == feed)):
                continue
            # 28.07 (баг слотов, SOL-окно 01:06): роутнутая не-мега в top-K бессмысленна —
            # window_loop её всё равно continue-ит (direct_swap 0 RTT), а слот top-K съеден,
            # и негретая цель (uSOL без статик-роута) до Kyber не доходила: пробы spec-fire
            # скипались «нет свопа» весь боевой цикл. Фильтруем ДО среза top-K; проверка в
            # window_loop остаётся (свежее on-chain чтение — страховка от рассинхрона).
            # NB (01.08, разбор cbDOGE): collateral_token/loan_token у Market — СИМВОЛЫ, а ключи
            # _SWAP_ROUTES — адреса, так что этот фильтр инертен (всегда False) и слоты режет
            # только top-K срез. Боевое решение принимает window_loop по адресам из
            # _read_target (теперь cap-aware). Символьный фильтр не чиним втихую — менять
            # слот-динамику отдельным пакетом, если top-K начнёт голодать.
            pair_routed = ((getattr(m, "collateral_token", "") or "").lower(),
                           (getattr(m, "loan_token", "") or "").lower()) in _SWAP_ROUTES
            for b in borrowers:
                d = float(debt_by.get((mid, b), 0.0) or 0.0)
                if d < PREARM_MIN_DEBT_USD:
                    continue
                if pair_routed and d < fl_max:
                    continue
                rows.append((mid, b, d))
        rows.sort(key=lambda t: -t[2])
        return rows[:PREARM_TOP_K]

    def _window_loop(self, feed: str, direction: str, stop: threading.Event):
        warms = 0
        while not stop.is_set() and warms < PREARM_MAX_WARMS:
            t0 = time.monotonic()
            for mid, b, d in self._pick(feed, direction):
                if stop.is_set() or warms >= PREARM_MAX_WARMS:
                    break
                try:
                    tgt = (self._read_fn or self._read_target)(mid, b)
                    if tgt is None:
                        continue
                    fl_max = float(getattr(self.cfg, "fast_lane_max_repaid_usd", 500000.0) or 500000.0)
                    if tgt["routed"] and d < fl_max:
                        continue                     # direct_swap и так 0 RTT — греть нечего
                    _tq = time.monotonic()
                    swap = (self._quote_fn or self._quote)(tgt["coll"], tgt["loan"], tgt["seized"])
                    prearm_put(tgt["coll"], tgt["loan"], swap, mid=mid, borrower=b)
                    warms += 1
                    self.stats["warms"] += 1
                    self._log("PREARM warm feed=%s %s/%s debt=$%.0fk amt_in=%d kyber=%.0fms%s" % (
                        feed, mid[:10], b[:10], d / 1e3, swap.get("amount_in", 0),
                        (time.monotonic() - _tq) * 1000.0, " (routed-mega)" if tgt["routed"] else ""))
                except Exception as e:
                    self._log("PREARM warm-fail feed=%s %s/%s: %s" % (feed, mid[:10], b[:10],
                                                                      type(e).__name__))
            # рефреш: ждём остаток интервала (просыпаемся сразу по stop)
            stop.wait(max(0.5, PREARM_REFRESH_SEC - (time.monotonic() - t0)))

    def handle_event(self, ev: dict):
        try:
            kind = ev.get("event")
            feed = (ev.get("feed") or "").upper()
            if kind == "arm":
                with self._lock:
                    old = self._windows.pop(feed, None)
                    if old is not None:
                        old.set()
                    stop = threading.Event()
                    self._windows[feed] = stop
                    self.stats["windows"] += 1
                threading.Thread(target=self._window_loop, args=(feed, ev.get("dir") or "down", stop),
                                 daemon=True, name="prearm-%s" % feed).start()
            elif kind in ("confirmed", "push", "disarm", "falsepos", "stale"):
                with self._lock:
                    stop = self._windows.pop(feed, None)
                if stop is not None:
                    stop.set()      # кэш живёт ещё TTL — confirmed-транзит потребит его сейчас
        except Exception:
            log.exception("prearm: событие %s упало (слой опционален)", ev.get("event"))
