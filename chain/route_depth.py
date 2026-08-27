"""LIVE-CAP замер (04.08, weETH tail): живая ёмкость капнутых прямых роутов.

Ликвидность тонких CL-пулов подвижна (см. коммент у _LIVE_CAPS в chain/execute.py). Один
замер = ОДНА aggregate3-пачка квот у квотера DEX'а роута: базовая квота (1% статик-капа) +
лестница долей статик-капа. Live cap -> chain.execute.set_live_cap (TTL, рост капнут там же).
Семантика ошибок РАЗНАЯ: реверт ступени = ДАННЫЕ (пул не тянет — режем кап, вплоть до 0 =
полный Kyber-фолбэк), транспортная ошибка RPC = fail-open (live-кап не трогаем, протухнет
в статик сам).

ПОРОГ ИМПАКТА — ОТ БОНУСА РЫНКА, НЕ ПЛОСКИЙ (#75, замер 09.08 блоки 49753974/49754165/
49754310). До этого стоял LIVE_CAP_MAX_IMPACT_PCT=0.5% на все 11 роутов, а бонус у них
разный (1.06%…12.68%): те же 0.5% означали «отдать 4% бонуса» на cbDOGE и «отдать 47%»
на wstETH/WETH. Одна константа = одиннадцать разных политик, ни одна не выбрана.

Правило из ДВУХ членов с разной силой:
  * МЯГКИЙ (политика маржи): impact <= LIVE_CAP_BONUS_SHARE * бонус. Половина бонуса —
    нам, половина — пулу; остаток бонуса покрывает газ, зазор «оракул != цена выхода» и
    дрейф между котировкой и включением.
  * ЖЁСТКИЙ (не разориться): cap <= S_обрыв / LIVE_CAP_CLIFF_SAFETY, где S_обрыв — размер,
    на котором импакт достигает ПОЛНОГО бонуса (с него ликвидация убыточна по построению).
    Делитель 2 не выдуман, а ЗАМЕРЕН как дрейф самого обрыва: weETH 210 -> ~100 за день
    (24.07), cbDOGE 135k -> ~65k за две недели (25.07->09.08), cbADA 116k ток. дал 5.84%
    и 8.14% на блоках в пяти минутах друг от друга (09.08).
На пологом пуле связывает мягкий член (S_budget < S_обрыв/2 при share<0.5), на CL-пуле
«плоско->обрыв» — жёсткий: там любой порог импакта даёт ОДИН и тот же размер (последний
перед исчерпанием диапазона), то есть порог вообще не является связывающим пределом.

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


def _route_quoter(r) -> str:
    """Квотер маршрута: явный `quoter` записи (27.08: вторая CL-фабрика, cbXRP) либо квотер
    семейства по стилю. Квотер ОБЯЗАН быть той же фабрики, что роутер: QuoterV2 считает адрес
    пула через свою factory(), и чужой квотер на живом пути отдаёт ноль — живой кап схлопнулся
    бы в 0 и роут молча выпал бы из строек. Чистая."""
    q = r.get("quoter")
    if q:
        return q
    return AERO_QUOTER if r["style"] == "v1" else UNIV3_QUOTER
_MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

LIVE_CAP_LADDER = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)   # доли статик max_in
# Лестница уплотнена в середине и ДОТЯНУТА ДО 4x (#75). Два разных требования:
#  * плотность у рабочего размера — чтобы кап не прыгал ступенями по 2x;
#  * ДЛИНА за пределы того, что мы готовы исполнить (потолок роста всё равно 2x статика) —
#    иначе «обрыв не найден» неотличимо от «обрыва нет». Первая редакция этого фикса мерила
#    до 2x, обрыва не видела и на живом узле поднимала кап РОВНО на обрыв: wstETH статик 150
#    -> live 300 при разорительном размере 318. Тест, который может вернуть только ноль.
LIVE_CAP_BONUS_SHARE = float(os.environ.get("LIVE_CAP_BONUS_SHARE", "0.5"))
LIVE_CAP_CLIFF_SAFETY = float(os.environ.get("LIVE_CAP_CLIFF_SAFETY", "2.0"))
# Плоский порог остаётся ТОЛЬКО как аварийный ручной оверрайд (откат к поведению до #75):
# пусто/0 = порог считается от бонуса рынка. Число -> оно и есть порог на все роуты.
LIVE_CAP_MAX_IMPACT_PCT = float(os.environ.get("LIVE_CAP_MAX_IMPACT_PCT", "0") or 0)
LIVE_CAP_REFRESH_SEC = float(os.environ.get("LIVE_CAP_REFRESH_SEC", "120"))
LIVE_CAP_MIN_DEBT_USD = float(os.environ.get("LIVE_CAP_MIN_DEBT_USD", "25000"))
LIVE_CAP_WATCH = os.environ.get("LIVE_CAP_WATCH", "1").lower() not in ("0", "false", "no")
LIVE_CAP_WATCH_POLL_SEC = float(os.environ.get("LIVE_CAP_WATCH_POLL_SEC", "30"))

_TS_LOCK = threading.Lock()
_LAST_TS: dict = {}      # (tin, tout) -> monotonic последней попытки замера

# --- БОНУС РЫНКА = потолок, который выход не имеет права съесть (#75/#76) ------------------
# LIF Morpho: min(MAX_LIF, 1/(CURSOR*lltv + 1-CURSOR)); бонус = LIF-1. lltv рынка ИММУТАБЕЛЕН,
# поэтому таблица — константа, как и сама карта роутов; измениться может только состав рынков
# на паре (новый рынок с другим lltv). Значения сняты С ЦЕПИ (Morpho.idToMarketParams, блок
# 49753919, публичный узел), НЕ из covered_markets.json: тот файл на агентской машине отстаёт
# от решений владельца (WETH/USDC и мега-cbXRP живут в INCLUDE_MARKETS, а в JSON их нет).
# Расхождение файла с этой таблицей ловит канарейка выходов (route_canary_watch).
MORPHO_LIQUIDATION_CURSOR = 0.3
MORPHO_MAX_LIF = 1.15


def lif_bonus_pct(lltv: float) -> float:
    """Бонус ликвидации рынка в ПРОЦЕНТАХ по его lltv. Чистая (юнит)."""
    lif = min(MORPHO_MAX_LIF,
              1.0 / (MORPHO_LIQUIDATION_CURSOR * float(lltv) + 1.0 - MORPHO_LIQUIDATION_CURSOR))
    return (lif - 1.0) * 100.0


def _lltv_table():
    """(coll, loan) -> [lltv, …] всех НАШИХ рынков пары. Ленивая: адреса живут в execute."""
    from chain.execute import (_CBBTC, _CBLTC, _CBXRP, _CBDOGE, _CBADA, _CBETH, _LBTC,
                               _WSTETH, _WEETH, _EURC, _SOL, _UNIV3_USDC, _UNIV3_WETH)
    return {
        (_CBBTC, _EURC): [0.860],                    # 0x67ebd84b… -> 4.3841%
        (_CBLTC, _UNIV3_USDC): [0.625],              # 0x9125d0fa… -> 12.6761%
        # ДВА рынка (#76): мега 0xd4a903dc… lltv 0.625 (бонус 12.68%) И 0xfdfecf85… lltv 0.770
        # (7.41%). Связывает ТЕСНЕЙШИЙ — маршрут один на оба, кап тоже один.
        (_CBXRP, _UNIV3_USDC): [0.625, 0.770],       # -> 7.4114%
        (_CBBTC, _UNIV3_USDC): [0.860],              # 0x9103c3b4… -> 4.3841%
        # SOL/USDC 27.08 (роут заведён после вскрытия: рынок третий по деньгам, а маршрута не
        # было вовсе). Рынков пары РОВНО ДВА, оба lltv 0.625 — перебором CreateMarket-логов
        # Morpho за 9M блоков: 0x7dc02ff6… и 0x4aa5debe…. Без этой строки test_route_cap ловит
        # незнание бонуса как незакрытый гард — и правильно делает.
        (_SOL, _UNIV3_USDC): [0.625],                # -> 12.6761%
        # cbBTC/WETH 19.08 (роут заведён после потери 50191667): рынок пары РОВНО ОДИН —
        # 0x5dffffc7… lltv 0.915 (проверено обходом всех 33 рынков конфигов на форке 50191666).
        # Без этой строки канарейка выходов судила бы новый роут дефолтом (класс #76), а
        # test_route_cap ловит незнание бонуса как незакрытый гард — и правильно делает.
        (_CBBTC, _UNIV3_WETH): [0.915],              # 0x5dffffc7… -> 2.6247%
        (_LBTC, _CBBTC): [0.915, 0.945],             # 0x4944a116…, 0x30767836… -> 1.6777%
        (_WSTETH, _UNIV3_WETH): [0.945, 0.965],      # 0x3a4048c6…, 0x6aa81f51… -> 1.0611%
        (_WEETH, _UNIV3_WETH): [0.915, 0.945],       # 0x78d11c03…, 0xfd0895ba… -> 1.6777%
        (_CBETH, _UNIV3_WETH): [0.945],              # 0x84662b4f… -> 1.6777%
        # WETH/USDC 0x8793cf30… — в INCLUDE_MARKETS, но НЕ в covered_markets.json: без этой
        # строки канарейка судила рынок по дефолту 10% при настоящем бонусе 4.38%.
        (_UNIV3_WETH, _UNIV3_USDC): [0.860],         # -> 4.3841%
        (_CBDOGE, _UNIV3_USDC): [0.625],             # 0x73527ddd… -> 12.6761%
        (_CBADA, _UNIV3_USDC): [0.625, 0.770],       # 0xd7520ad1…, 0xf2a59ad1… -> 7.4114%
    }


_ROUTE_BONUS: dict = {}


def route_bonus_pct(token_in, token_out):
    """Бонус ТЕСНЕЙШЕГО рынка пары в процентах либо None (пары нет в таблице).

    Минимум, а не максимум: маршрут обслуживает все рынки пары, кап у него один, и связывает
    самый тесный. Комментарий #76 брал у cbXRP бонус ЖИРНОГО рынка (12.7%) и объявлял запас
    2.6x там, где по тесному (7.41%) он был 1.85x."""
    if not _ROUTE_BONUS:
        for (c, l), lltvs in _lltv_table().items():
            _ROUTE_BONUS[(c.lower(), l.lower())] = min(lif_bonus_pct(x) for x in lltvs)
    return _ROUTE_BONUS.get((token_in.lower(), token_out.lower()))


def impact_budget_pct(token_in, token_out):
    """Порог импакта для роута: доля бонуса рынка (либо ручной плоский оверрайд).

    Роут без бонуса в таблице = НЕЗНАНИЕ, и оно закрывает гард: берём самый тесный бонус
    таблицы, а не щедрый дефолт (канон «незнание обязано закрывать гард»)."""
    if LIVE_CAP_MAX_IMPACT_PCT > 0:
        return LIVE_CAP_MAX_IMPACT_PCT
    b = route_bonus_pct(token_in, token_out)          # он же прогревает _ROUTE_BONUS
    if b is None:
        b = min(_ROUTE_BONUS.values()) if _ROUTE_BONUS else 1.0
    return b * LIVE_CAP_BONUS_SHARE


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
    quoter = _route_quoter(r)
    sizes = [max(static // 100, 1)] + [int(static * f) for f in LIVE_CAP_LADDER]
    calls = [(quoter, _QUOTE_SEL + encode(["bytes", "uint256"], [r["path"], s]))
             for s in sizes]
    try:
        res = (batch_fn or _quote_batch)(rpc, calls)
    except Exception as e:
        _log("LIVECAP transport-fail %s->%s: %s (статик остаётся)"
             % (token_in[:8], token_out[:8], type(e).__name__))
        return None
    bonus = route_bonus_pct(token_in, token_out)
    budget = impact_budget_pct(token_in, token_out)
    ruin = bonus if bonus is not None else budget / max(LIVE_CAP_BONUS_SHARE, 1e-9)
    ok0, d0 = res[0]
    rungs = []           # сырая лестница для SHRINK-телеметрии (ревью 05.08: спор «80 vs 300»
    if not ok0 or len(d0) < 32:      # решается в секунду сырыми ступенями). Ступени — В ДОЛЯХ
        cap = 0          # СТАТИКА: раньше делились на 1e18 при любых decimals, и cbDOGE (8
        rungs.append("base:REV")     # знаков) приезжал в журнал как 5e-14 — диагност, врущий
    else:                            # о величине, которую сам же и печатает
        base = int.from_bytes(d0[:32], "big") / sizes[0]
        cap = 0                      # верхняя ступень, укладывающаяся в бюджет
        safe = 0                     # верхняя ступень, про которую ЗАМЕРОМ известно, что она
        cliff_seen = False           # не разорительна — нижняя граница обрыва
        over_budget = False
        for (ok, d), s in zip(res[1:], sizes[1:]):
            if not ok or len(d) < 32:
                rungs.append("%.4gx:REV" % (s / static))
                cliff_seen = True     # реверт ступени = данные: дальше хуже, ниже — не знаем
                break
            ratio = int.from_bytes(d[:32], "big") / s
            imp = (1.0 - ratio / base) * 100.0 if base > 0 else 100.0
            rungs.append("%.4gx:%.2f%%" % (s / static, imp))
            if base <= 0 or imp >= ruin:
                cliff_seen = True     # с этого размера выход съедает ВЕСЬ бонус
                break                 # обрыв ЗАЖАТ между safe и s; ниже по построению не идём
            safe = s
            if base <= 0 or imp > budget:
                over_budget = True    # мягкий член исчерпан, но лестницу НЕ бросаем: жёсткому
            elif not over_budget:     # члену нужно ещё найти разорительную ступень
                cap = s
        if cliff_seen:
            # Обрыв зажат в (safe, s]; фейл-клоуз берёт НИЖНЮЮ границу: мы знаем лишь, что до
            # safe пул цел. Держимся вдвое ниже неё — сам обрыв ездит в 2x за сутки.
            cap = min(cap, int(safe / LIVE_CAP_CLIFF_SAFETY))
        if cap == 0:
            cap = static // 10   # даже 0.25x не тянет: крошечный прямой сайз, остальное Kyber
    from chain.execute import _effective_cap
    prev = _effective_cap((token_in.lower(), token_out.lower()))
    eff = set_live_cap(token_in, token_out, cap)
    _log("LIVECAP %s->%s: static=%d live=%s (x%.2f) бюджет=%.2f%% (бонус %s)"
         % (token_in[:8], token_out[:8], static, eff, (eff or 0) / static, budget,
            ("%.2f%%" % bonus) if bonus is not None else "НЕИЗВЕСТЕН"))
    if prev and eff is not None and eff * 2 <= prev:   # сжатие >=x2 — сырые ступени в журнал
        _log("LIVECAP SHRINK %s->%s: %d -> %d, ступени=[%s]"
             % (token_in[:8], token_out[:8], prev, eff, " ".join(rungs)))
    return eff


# --- §9 (#133): КУМУЛЯТИВНЫЕ КОТИРОВКИ ОДНОГО РОУТА -----------------------------------------
# Зачем отдельная дверь, если рядом уже есть measure_route_cap. Тот отвечает на вопрос «какой
# размер роут ТЯНЕТ» и лестницу квот выбрасывает (rungs живут только в строке журнала). §9
# спрашивает другое: «сколько отдаст пул НА КОНКРЕТНОМ кумулятивном входе» — и ответ нужен
# ЧИСЛОМ, в точках, которые задаёт состав группы. Общего у них ровно транспорт.
def cumulative_quotes(rpc, token_in, token_out, sizes, batch_fn=None):
    """{размер: выход} у квотера ПРЯМОГО роута, ОДНОЙ пачкой aggregate3.

    None вместо словаря = роута нет (Kyber-агрегат: путь размазан по нескольким пулам и
    серверный minOut нам не принадлежит) либо транспорт отказал. Значение None у отдельного
    размера = ступень РЕВЕРТНУЛА, и это ДАННЫЕ: пул такого кумулятивного входа не тянет.

    Семантика ошибок повторяет measure_route_cap намеренно: реверт ступени — факт о пуле,
    транспортная ошибка — незнание.
    """
    from eth_abi import encode
    from chain.execute import _SWAP_ROUTES
    key = (token_in.lower(), token_out.lower())
    r = _SWAP_ROUTES.get(key)
    if r is None:
        return None
    quoter = _route_quoter(r)
    sizes = [int(s) for s in sizes if int(s) > 0]
    if not sizes:
        return {}
    calls = [(quoter, _QUOTE_SEL + encode(["bytes", "uint256"], [r["path"], s])) for s in sizes]
    try:
        res = (batch_fn or _quote_batch)(rpc, calls)
    except Exception as e:
        log.info("§9 quote transport-fail %s->%s: %s (котировок нет — §9 обязан отказать, "
                 "а не выдумать запас)", token_in[:8], token_out[:8], type(e).__name__)
        return None
    out = {}
    for s, (ok, d) in zip(sizes, res):
        out[s] = int.from_bytes(d[:32], "big") if (ok and len(d) >= 32) else None
    return out


def make_cumulative_quote_fn(rpc, token_in, token_out, amounts_in, batch_fn=None):
    """Квотер для `board_v6.same_pool_min_outs`: x -> выход за КУМУЛЯТИВНЫЙ вход x.

    Спрашиваем РОВНО те точки, которые нужны худшему случаю (§9): суммарный вход группы T,
    T−aᵢ по каждой цели (я исполняюсь ПОСЛЕДНИМ) и aᵢ (одиночный выход — для отчёта «сколько
    стоит группировка»). Это <=2n+1 квот в ОДНОЙ пачке.

    Возврат None = котировок для этого роута не существует. Вызывающий обязан прочитать это
    как «данных нет» и отказать (или уйти в строгий disjoint-режим), а НЕ как «запас нулевой».
    """
    a = [int(x) for x in amounts_in if int(x) > 0]
    if not a:
        return None
    total = sum(a)
    want = sorted({total} | {total - x for x in a} | set(a))
    table = cumulative_quotes(rpc, token_in, token_out, want, batch_fn=batch_fn)
    if table is None:
        return None

    def _q(x):
        x = int(x)
        if x == 0:
            return 0
        if x not in table:
            raise KeyError("§9: котировка на кумулятивный вход %d не запрашивалась — состав "
                           "группы изменился после снятия квот" % x)
        return table[x]

    return _q


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
