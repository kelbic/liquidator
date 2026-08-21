"""Канарейка выходов НА КРОНЕ: порог = LIF конкретного рынка, адресат = агент (#72).

Зачем регулярно. Роут — не константа, а срез чужой ликвидности: cbXRP протух за недели
(269x мимо), cbDOGE стоял РОВНО НА КРАЮ обрыва (impact 43.5% при бонусе 12.7%). Оба раза
мы узнавали об этом от реверта в бою либо от ручной проверки одной пары. Замер стоит
десятка read-only квот на публичном узле, поэтому дешевле держать его на расписании, чем
ждать следующего молчаливого промаха.

ПОРОГ. «10% на все маршруты» — не порог, а цифра: у cbBTC/USDC весь бонус 4.38% (10% =
гарантированный минус задолго до тревоги), у cbDOGE/USDC бонус 12.68% (10% = тревога на
здоровом маршруте). Порог берём из covered_markets.json — это LIF рынка (bonus = LIF-1),
то есть ровно та величина, которую выход обязан не съесть. Пар, которых в файле нет
(WETH/USDC), касается дефолт 10%.
Пара с НЕСКОЛЬКИМИ рынками (wstETH/WETH, cbADA/USDC, LBTC/cbBTC) берёт МИНИМАЛЬНЫЙ бонус:
маршрут один на всех, а связывающий предел — самый тесный рынок.

ЧЕГО ЭТА КАНАРЕЙКА НЕ ГОВОРИТ (граница, не чинить её видимостью покрытия):
  * impact считается против МАЛОГО референса (1% рабочего объёма), то есть это форма
    кривой пула, а не абсолютная цена выхода. Оракульная цена тут не участвует вовсе.
  * порог сверяется со СТАТИЧЕСКИМ max_in. Живой кап (ROUTE_LIVE_CAP) может увести
    боевой размер и вверх, и вниз — см. задачу #75.
  * карта маршрутов берётся из ЧЕКАУТА АГЕНТСКОЙ МАШИНЫ, а боевой бот живёт своим
    чекаутом на Шарлотте. Пока обе стороны в синке, это одна и та же карта; кап,
    правленный ТОЛЬКО на боевой машине, канарейка не увидит. Сверку с боевым чекаутом
    здесь никто не делает — не считать её сделанной.
  * «НЕ ОПРОШЕН» ничего не утверждает о маршруте: это лимит узла. Он не алертит, он
    вызывает повторный проход. Но полная слепота (ни одного опрошенного маршрута) — это
    уже отказ самого сторожа, и он ОБЯЗАН быть слышен: мёртвый сторож хуже отсутствующего.

Адресат — инбокс агента (hil=False): протухший роут чинит агент (пересчёт капа, смена
венью), человеку тут решать нечего, пока дело не упрётся в подпись/капитал.

    python3 -m analysis.route_canary_watch          # прогон (крон)
    ROUTE_CANARY_MARGIN=0.5 …                       # тревожить на половине бонуса
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import route_canary as rc  # noqa: E402

DEFAULT_PCT = 10.0                 # пар нет в covered_markets.json -> прежний общий порог
WATCH_DIR = os.path.expanduser("~/.fleet-watch")
SOURCE = "route-canary"

# Хвост имени венью в константах chain.execute: _UNIV3_USDC — это USDC, а не отдельный токен.
_VENUE_PREFIXES = ("UNIV3_", "AERO_", "SLIP_")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def load_markets(path=None):
    """covered_markets.json — список рынков с bonus (=LIF-1). Отсутствие файла НЕ фатально:
    порог просто становится дефолтным, а канарейка обязана продолжать мерить."""
    path = path or os.path.join(rc._repo_root(), "covered_markets.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def norm_name(name):
    """Имя константы chain.execute -> символ токена в нижнем регистре ('_UNIV3_USDC' -> 'usdc')."""
    n = str(name).lstrip("_")
    for p in _VENUE_PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
    return n.lower()


def token_symbols(module=None, markets=None):
    """Карта адрес -> символ рынка. Адреса токенов лежат в chain.execute ИМЕНОВАННЫМИ
    константами (_CBBTC, _UNIV3_USDC …); сверяем их с символами covered_markets.json
    без регистра. Спрашивать symbol() у узла не нужно: лишние RPC на том же лимите,
    из-за которого мы и так получаем «НЕ ОПРОШЕН».

    Имена берём только ЗАГЛАВНЫЕ с ведущим подчёркиванием — иначе в карту попадает
    случайная переменная цикла (`_coll` в chain/execute.py указывает на cbXRP)."""
    if module is None:
        _, module = rc.routes()
    known = {}
    for m in (markets if markets is not None else load_markets()):
        for k in ("collateral_token", "loan_token"):
            s = m.get(k)
            if s:
                known[str(s).lower()] = str(s)
    out = {}
    for name, val in vars(module).items():
        if not (isinstance(name, str) and name.startswith("_") and name[1:].isupper()):
            continue
        if not (isinstance(val, str) and val.startswith("0x") and len(val) == 42):
            continue
        sym = known.get(norm_name(name))
        if sym:
            out[val.lower()] = sym       # совпало с рынком — это и есть символ
        else:
            out.setdefault(val.lower(), norm_name(name).upper())
    return out


def onchain_bonus_pct(coll_addr, loan_addr):
    """Бонус пары из таблицы lltv, снятой С ЦЕПИ (chain.route_depth). None — пары там нет.

    Зачем второй источник (#76, 09.08): covered_markets.json на агентской машине НЕ ЗНАЕТ
    рынков, включённых решением владельца через INCLUDE_MARKETS — WETH/USDC (0x8793cf30,
    lltv 0.86, бонус 4.38%) и мега-cbXRP (0xd4a903dc). Для WETH/USDC это означало порог
    ДЕФОЛТА 10% на рынке, весь бонус которого 4.38%: канарейка молчала бы при импакте 9.9%,
    то есть при выходе, съедающем бонус вдвое. Порог, врущий про запас, опаснее его
    отсутствия — по нему принимают решения."""
    try:
        from chain.route_depth import route_bonus_pct  # noqa: PLC0415 — боевой модуль по требованию
        return route_bonus_pct(coll_addr, loan_addr)
    except Exception:  # noqa: BLE001 — канарейка обязана мерить и без этого источника
        return None


def threshold_for(coll_sym, loan_sym, markets, default_pct=DEFAULT_PCT, margin=1.0,
                  pair=None):
    """Порог impact в процентах + откуда он взят.

    Порог = бонус ликвидации (LIF-1) самого тесного рынка этой пары × margin. Выход,
    съедающий бонус целиком, делает ликвидацию убыточной ПО ПОСТРОЕНИЮ — газ и
    заложенный slippage сверху.

    Источников ДВА, и берётся МЕНЬШИЙ: covered_markets.json (обновляется рескaном, знает
    новые рынки) и таблица lltv с цепи в chain.route_depth (знает решения владельца, которых
    в файле нет). Дефолт 10% остаётся только когда молчат оба.
    """
    cands = []
    if coll_sym and loan_sym:
        hits = [m for m in markets
                if str(m.get("collateral_token", "")).lower() == str(coll_sym).lower()
                and str(m.get("loan_token", "")).lower() == str(loan_sym).lower()]
        bonuses = [float(m["bonus"]) for m in hits if isinstance(m.get("bonus"), (int, float))]
        if bonuses:
            cands.append((min(bonuses) * 100.0,
                          "LIF %s/%s (рынков %d, бонус %.2f%%)" % (
                              coll_sym, loan_sym, len(hits), min(bonuses) * 100.0)))
    if pair:
        b = onchain_bonus_pct(pair[0], pair[1])
        if b is not None:
            cands.append((b, "LIF %s/%s с цепи (lltv рынков пары, бонус %.2f%%)" % (
                coll_sym or pair[0][:8], loan_sym or pair[1][:8], b)))
    if cands:
        pct, src = min(cands)
        return pct * margin, src
    return default_pct * margin, "дефолт %.0f%% (пары нет в covered_markets)" % default_pct


def thresholds_for_rows(rows, markets=None, symbols=None, default_pct=DEFAULT_PCT, margin=1.0):
    """(coll, loan) -> (порог, откуда) для готовых строк замера."""
    markets = load_markets() if markets is None else markets
    symbols = token_symbols(markets=markets) if symbols is None else symbols
    out = {}
    for r in rows:
        cs = symbols.get(r["coll"].lower())
        ls = symbols.get(r["loan"].lower())
        out[(r["coll"], r["loan"])] = threshold_for(cs, ls, markets, default_pct, margin,
                                                    pair=(r["coll"], r["loan"]))
    return out


def at_block(row):
    """«, блок замера N» либо пусто. Тревога обязана быть ВОСПРОИЗВОДИМОЙ: разбор начинается
    с реквоты на ТОМ ЖЕ блоке (канон «факт по цепи, а не по тексту тревоги»), а без номера
    блока разбирающий его восстанавливает руками по времени — 19.08 именно так и вышло.
    Пусто, когда замер шёл на «latest» (узел не назвал блок): врать про воспроизводимость
    хуже, чем её не обещать."""
    b = (row or {}).get("block")
    if not isinstance(b, str) or not b.startswith("0x"):
        return ""
    try:
        return ", блок замера %d" % int(b, 16)
    except ValueError:
        return ""


def evaluate(rows, thresholds, symbols=None):
    """Проблемные маршруты. НЕ ОПРОШЕН сюда не попадает НИКОГДА — это лимит узла."""
    symbols = symbols or {}
    probs = []
    for r in rows:
        th, src = thresholds.get((r["coll"], r["loan"]), (DEFAULT_PCT, "дефолт"))
        name = "%s→%s" % (symbols.get(r["coll"].lower(), r["coll"][:10]),
                          symbols.get(r["loan"].lower(), r["loan"][:10]))
        if r["status"] == rc.DEAD:
            probs.append({"kind": "dead", "name": name, "row": r,
                          "th": th, "src": src,
                          "text": "%s %s МЁРТВ: %s%s" % (name, r["router"], r["err"],
                                                              at_block(r))})
        elif r["status"] == rc.ALIVE and r["impact"] >= th:
            probs.append({"kind": "deep", "name": name, "row": r,
                          "th": th, "src": src,
                          "text": "%s %s impact %.2f%% ≥ порога %.2f%% [%s], "
                                  "max_in=%s (ед. token_in)%s" % (
                              name, r["router"], r["impact"], th, src,
                              r["cap"] if r["cap"] is not None else "нет",
                              at_block(r))})
    return probs


def policy_cap(coll, loan, *, call=None, measure=None, **kw):
    """Что БОЕВАЯ политика капа (chain.route_depth) назначит этому роуту на СЕГОДНЯШНЕМ пуле.

    Зачем это в тексте тревоги (разбор 20.08, CBLTC). Канарейка судит СТАТИЧЕСКИЙ max_in —
    это её честно объявленная граница. Но у бота поверх статика живёт слой live-cap, и он
    режет размер сам: 20.08 канарейка кричала «impact 29.03% при бонусе 12.68%», а боевой
    бот в этот момент УЖЕ держал роут на 105 cbLTC вместо 280 (журнал Шарлотт 17:18-17:33) —
    то есть на импакте ~1.9%. Тревога была права про ПУЛ и молчала про то, покрыт ли он.
    Разбирающему пришлось выяснять это ссылкой в чужой журнал; теперь ответ едет в тексте.

    Считает ТА ЖЕ функция, что и в бою (measure_route_cap), а не второй расчёт по мотивам:
    вторая реализация разъехалась бы с боевой ровно тогда, когда это важнее всего. Подменён
    только ТРАНСПОРТ — квоты идут через rc._call на публичный узел канарейки.

    Побочный эффект set_live_cap остаётся ВНУТРИ процесса канарейки (агентская машина,
    отдельный процесс, живёт минуту) — боевого бота он не касается.

    Возвращает (cap, static) либо (None, static/None): незнание не выдаёт себя за диагноз.
    """
    try:
        from eth_abi import encode  # noqa: F401,PLC0415 — нужен самому measure_route_cap
        from chain import route_depth as rd  # noqa: PLC0415
        from chain.execute import _SWAP_ROUTES  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None, None
    static = (_SWAP_ROUTES.get((coll.lower(), loan.lower())) or {}).get("max_in")
    if static is None:
        return None, None

    _call_fn = call or rc._call        # инъекция транспорта — тем же контрактом, что у rc.quote
    # Лестница из 10 ступеней просится у публичного узла ОДНОЙ пачкой, и на дефолтном
    # ретрае (4) один 429 стирает всю справку («НЕ ПОСЧИТАНА» на живом прогоне 20.08).
    # Терпения тут не жалко: путь работает только на пробитом роуте, то есть редко.
    kw.setdefault("tries", 6)
    kw.setdefault("pause", 1.2)

    class _NodeLimit(Exception):
        """Узел не обслужил ступень лестницы."""

    def _batch(_rpc, calls):
        """Пачка квот -> [(ok, raw32)] в форме, которую ждёт measure_route_cap.

        РАЗНИЦА, КОТОРУЮ НЕЛЬЗЯ СТИРАТЬ. Боевая политика читает (False, b"") как ДАННЫЕ —
        «ступень ревертит, дальше обрыв, режем кап». В бою это верно: там aggregate3, и
        транспортный отказ выбрасывает исключение (fail-open, статик остаётся). Здесь
        транспорт другой, и молчание узла (429) пришло бы в ту же ячейку — политика вернула
        бы ЗАНИЖЕННЫЙ кап, а тревога напечатала бы его как факт. Незнание обязано закрывать
        гард, а не выдавать себя за диагноз: любой UNPOLLED рвёт замер, справки просто не
        будет. DEAD (положительное свидетельство реверта) — это данные, он проходит.
        """
        out = []
        for to, data in calls:
            hexdata = "0x" + data.hex() if isinstance(data, (bytes, bytearray)) else data
            res, _err, kind = _call_fn(to, hexdata, **kw)
            if kind == rc.UNPOLLED:
                raise _NodeLimit(str(_err)[:80])
            out.append((True, bytes.fromhex(res[2:])) if (kind == rc.ALIVE and res)
                       else (False, b""))
        return out

    try:
        cap = (measure or rd.measure_route_cap)(None, coll, loan, batch_fn=_batch,
                                                log_fn=lambda _m: None)
    except Exception:  # noqa: BLE001 — справка НЕ ИМЕЕТ ПРАВА гасить тревогу
        return None, static
    return cap, static


def live_cap_caveat():
    """Условие, при котором посчитанный кап ДЕЙСТВИТЕЛЬНО режет боевой размер.

    Зачем оговорка, а не прежнее «боевой live-cap режет размер сам» (разбор 21.08, CBBTC).
    Та формулировка утверждала ПОВЕДЕНИЕ БОТА, которого канарейка не видит, и утверждала его
    БЕЗУСЛОВНО. Механизм же фейл-ОПЕНИТСЯ: chain.execute._effective_cap отдаёт статик, если
    live-кап протух (LIVE_CAP_TTL_SEC) либо не ставился ни разу, а ставит его только
    (а) RouteDepthWatcher — и лишь пока на паре живёт кандидат с долгом >= LIVE_CAP_MIN_DEBT_USD,
    (б) prearm-окно по горячей цели — и там замер под `except: pass`, то есть необязателен.
    Значит текст врал бы РОВНО В ТОТ ПРОГОН, когда покрытие исчезло: пара без крупного
    кандидата даёт ту же строку «режет размер сам» при статике в бою (канон
    own-meter-blind-path — свой счётчик слеп к необслуживаемому пути).

    Проверить условие отсюда нельзя: набор кандидатов с долгами живёт в процессе бота на
    Шарлотте, у канарейки его нет. Поэтому она НАЗЫВАЕТ предел, а не изображает знание —
    канон «тревога несёт основание, по которому судила» (29ae77e).
    """
    try:
        from chain.execute import LIVE_CAP_TTL_SEC, ROUTE_LIVE_CAP  # noqa: PLC0415
        from chain.route_depth import LIVE_CAP_MIN_DEBT_USD  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — справка не имеет права падать в тексте тревоги
        return (". ПОКРЫТИЕ БОЯ НЕ ПРОВЕРЕНО: константы live-слоя не прочитаны")
    if not ROUTE_LIVE_CAP:
        return (". В БОЮ СЛОЙ ВЫКЛЮЧЕН (ROUTE_LIVE_CAP=0 в ЭТОМ чекауте) — размер боя "
                "равен статику, тревога про БОЙ ТОЖЕ")
    return (". ПРО БОЙ НЕ СУДИТ: кап применяется, лишь пока свежий (TTL %ds) — ставит его "
            "вотчер, пока на паре есть кандидат с долгом >=$%.0fk, либо prearm по горячей "
            "цели; иначе _effective_cap молча отдаёт СТАТИК. Набор кандидатов отсюда не "
            "виден" % (int(LIVE_CAP_TTL_SEC), LIVE_CAP_MIN_DEBT_USD / 1e3))


def annotate_policy(probs, *, call=None, measure=None, budget_sec=None, clock=None, **kw):
    """Дописывает к «глубоким» проблемам вердикт боевой политики капа. Только справка:
    состав probs и решение «тревожить» не меняются ни при каком исходе.

    БЮДЖЕТ ВРЕМЕНИ — не украшение. Справка считается ДО отправки тревоги, а весь прогон
    живёт под `timeout 600` в кроне. Лестница из 10 ступеней на терпеливом ретрае (tries=6)
    у деградировавшего узла способна съесть остаток бюджета, и SIGTERM убил бы САМУ ТРЕВОГУ —
    ровно в тот прогон, когда с пулом хуже всего. Справка, стоящая тревоги, дороже своей
    пользы: за порогом бюджета остальные роуты честно получают «НЕ ПОСЧИТАНА».
    """
    budget = _env_float("ROUTE_CANARY_POLICY_BUDGET_SEC", 120.0) if budget_sec is None \
        else float(budget_sec)
    now = clock or time.monotonic
    t0 = now()
    for p in probs:
        if p.get("kind") != "deep":
            continue
        if now() - t0 >= budget:
            p["text"] += ("\n    политика капа: НЕ ПОСЧИТАНА (бюджет времени %ds исчерпан) — "
                          "справка не имеет права стоить самой тревоги" % int(budget))
            continue
        r = p.get("row") or {}
        cap, static = policy_cap(r.get("coll", ""), r.get("loan", ""), call=call,
                                 measure=measure, **kw)
        if cap is None or not static:
            p["text"] += "\n    политика капа: НЕ ПОСЧИТАНА (замер не удался) — размер судить по статику"
        elif cap >= static:
            p["text"] += ("\n    политика капа: %d (=статик %d) — живой слой НЕ режет, "
                          "размер боя равен судимому" % (cap, static))
        else:
            p["text"] += ("\n    политика капа на сегодня: %d вместо статика %d (x%.2f) — "
                          "столько НАЗНАЧИЛ БЫ живой слой на этом пуле; тревога про ПУЛ%s"
                          % (cap, static, cap / static, live_cap_caveat()))
    return probs


def fingerprint(probs, blind=False):
    """Отпечаток СОСТОЯНИЯ (какие маршруты и чем плохи), без числового шума: тревога по
    осциллирующей величине обязана звонить на смену состояния, а не на каждый тик."""
    key = ";".join(sorted("%s:%s" % (p["name"], p["kind"]) for p in probs))
    if blind:
        key = "blind|" + key
    return hashlib.sha1(key.encode()).hexdigest()[:8] if key else "-"


def format_alert(probs, rows, blind=False):
    n = len(rows)
    unpolled = [r for r in rows if r["status"] == rc.UNPOLLED]
    if blind:
        head = ("КАНАРЕЙКА ОСЛЕПЛА: ни один из %d маршрутов не опрошен за все проходы. "
                "Это отказ сторожа, а не деградация роутов — проверить узел %s."
                % (n, rc.RPC))
        return head
    head = "выходы: %d маршрут(ов) из %d под тревогой" % (len(probs), n)
    lines = [head] + ["  " + p["text"] for p in probs]
    if unpolled:
        lines.append("  не опрошено (лимит узла, НЕ деградация): %d" % len(unpolled))
    lines.append("  замер: analysis/route_canary_watch.py, лог %s/route-canary.log"
                 % WATCH_DIR)
    return "\n".join(lines)


def _fleet_notify():
    """Канал тревоги. Недоступный канал — сам по себе отказ: возвращаем None, вызывающий
    обязан крикнуть в лог (тишина неотличима от «всё спокойно»)."""
    sys.path.insert(0, WATCH_DIR)
    try:
        from notify import notify  # noqa: PLC0415
        return notify
    except Exception:  # noqa: BLE001
        return None


def run(*, passes=None, repass_sleep=None, call=None, notify_fn=None, markets=None,
        module=None, items=None, out=print, sleep=time.sleep, default_pct=DEFAULT_PCT,
        margin=None, dedup_sec=None, **kw):
    """Полный цикл: замер -> повторные проходы по неопрошенным -> порог -> алерт -> лог.

    Возвращает сводку (dict) — тестам и вызывающему; печать идёт через `out`.
    """
    passes = int(_env_float("ROUTE_CANARY_PASSES", 3) if passes is None else passes)
    repass_sleep = (_env_float("ROUTE_CANARY_REPASS_SEC", 20.0)
                    if repass_sleep is None else repass_sleep)
    margin = _env_float("ROUTE_CANARY_MARGIN", 1.0) if margin is None else margin
    dedup_sec = _env_float("ROUTE_CANARY_DEDUP_SEC", 86400) if dedup_sec is None else dedup_sec

    if items is None:
        items, module = rc.routes(module)
    by_key = {k: (k, r) for k, r in items}
    rows = rc.scan(items, module=module, call=call, **kw)
    used = 1
    while used < passes:
        pending = [by_key[(r["coll"], r["loan"])] for r in rows
                   if r["status"] == rc.UNPOLLED and (r["coll"], r["loan"]) in by_key]
        if not pending:
            break
        sleep(repass_sleep)
        used += 1
        fresh = {(r["coll"], r["loan"]): r for r in
                 rc.scan(pending, module=module, call=call, **kw)}
        rows = [fresh.get((r["coll"], r["loan"]), r) for r in rows]

    markets = load_markets() if markets is None else markets
    symbols = token_symbols(module=module, markets=markets)
    th = thresholds_for_rows(rows, markets=markets, symbols=symbols,
                             default_pct=default_pct, margin=margin)
    probs = evaluate(rows, th, symbols)
    # Справка о боевом капе — ТОЛЬКО когда есть что разбирать (10 квот на пробитый роут,
    # ноль на спокойном проходе): бюджет узла тратится на разбор, а не на рутину.
    if probs:
        annotate_policy(probs, call=call, **kw)
    polled = sum(1 for r in rows if r["status"] != rc.UNPOLLED)
    blind = bool(rows) and polled == 0

    rc.render(rows, th, out=out)
    fp = fingerprint(probs, blind)
    verdict = "inbox" if (probs or blind) else "нет"
    if probs or blind:
        notify_fn = notify_fn or _fleet_notify()
        text = format_alert(probs, rows, blind)
        if notify_fn is None:
            verdict = "КАНАЛ НЕДОСТУПЕН"
            out("route-canary: КАНАЛ ТРЕВОГИ НЕДОСТУПЕН (~/.fleet-watch/notify.py), текст ниже")
            out(text)
        else:
            verdict = notify_fn(text, source=SOURCE, hil=False,
                                key="route-canary:" + fp, dedup_sec=dedup_sec)
    summary = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "passes": used, "routes": len(rows),
        "alive": sum(1 for r in rows if r["status"] == rc.ALIVE),
        "dead": sum(1 for r in rows if r["status"] == rc.DEAD),
        "unpolled": len(rows) - polled,
        "problems": len(probs), "blind": blind, "fp": fp, "alert": verdict,
        "rows": rows, "thresholds": th,
    }
    out("%s route-canary: проходов=%d маршрутов=%d жив=%d мёртв=%d не_опрошен=%d "
        "проблем=%d слепота=%s alert=%s fp=%s"
        % (summary["ts"], used, summary["routes"], summary["alive"], summary["dead"],
           summary["unpolled"], summary["problems"], "да" if blind else "нет",
           verdict, fp))
    return summary


def main():
    try:
        s = run()
    except Exception as e:  # noqa: BLE001 — падение самой канарейки обязано быть слышно
        import traceback
        traceback.print_exc()
        n = _fleet_notify()
        msg = "канарейка выходов УПАЛА: %s: %s" % (type(e).__name__, e)
        if n:
            n(msg, source=SOURCE, hil=False, key="route-canary:crash", dedup_sec=21600)
        print("%s route-canary: ОШИБКА %s"
              % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg))
        return 2
    return 1 if (s["problems"] or s["blind"]) else 0


if __name__ == "__main__":
    sys.exit(main())
