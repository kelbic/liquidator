"""Канарейка ВЫХОДОВ: жив ли каждый наш прямой маршрут и какова его глубина (#69).

Урок 24.07: «прямые роуты протухают за недели». 09.08 это подтвердилось ценой: cbXRP держали на
UniV3 fee=10000, где на боевом размере выход в 269 раз хуже Aerodrome — и план молча ревертил.
Проверили одну пару из одиннадцати. Эта канарейка проверяет ВСЕ и делает это дёшево: read-only
квоты на публичном узле, без ключей, без tx, боевой бот не задет.

Метод: для каждой записи _SWAP_ROUTES берём её же path и зовём quoteExactInput у СВОЕГО квотера
(style 02 -> UniV3 QuoterV2, style v1 -> Slipstream QuoterV2 — пулы одного тира в разных фактори
не видны чужому квотеру, урок 24.07). Считаем цену на малом объёме (референс) и на рабочем
(max_in либо дефолт), impact между ними — это и есть «сколько съест наш выход».

    python3 -m analysis.route_canary          # разовый прогон, таблица
    python3 -m analysis.route_canary_watch    # тот же замер на кроне, с алертом агенту (#72)

ТРИ СТАТУСА, А НЕ ДВА (#72). Первая редакция знала только «жив»/«МЁРТВ», и публичный узел,
отшивший нас по ставке, приезжал в таблицу как МЁРТВ — то есть лимит УЗЛА выглядел как
деградация ПУЛА. На кроне это стало бы генератором ложных тревог (замер 09.08: 2 маршрута из
11 не опрошены на первом же проходе). Теперь «НЕ ОПРОШЕН» — отдельный статус: он ничего не
утверждает о маршруте и не алертит, он требует ПОВТОРНОГО ПРОХОДА.

ШОВ ТОЛЬКО НА ТРАНСПОРТЕ: `fetch(url, body) -> dict` подменяется в тестах, вся сборка калдаты,
разбор ошибок узла, ретрай и арифметика impact остаются настоящими.
"""
import json
import os
import sys
import time
import urllib.request

RPC = os.environ.get("CANARY_RPC", "https://mainnet.base.org")
UNIV3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
AERO_QUOTER = "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"
# Селектор СЧИТАЕМ, а не хардкодим: первая редакция несла старый QuoterV1-селектор и
# получала «execution reverted» на всех маршрутах — то есть канарейка врала бы, что
# мертво всё, тогда как мёртв был мой вызов.
from eth_utils import keccak
SEL = keccak(text="quoteExactInput(bytes,uint256)")[:4].hex()

ALIVE = "жив"
DEAD = "МЁРТВ"
UNPOLLED = "НЕ ОПРОШЕН"

# Признаки «узел не ответил / отшил по ставке». Список НАМЕРЕННО узкий: попадание сюда
# гасит тревогу, поэтому лишнее слово (например голое «exceeded») сделало бы канарейку
# слепой к настоящему отказу пула. «execution reverted» ни под один из них не подходит.
_NODE_LIMIT_HINTS = (
    "rate limit", "ratelimit", "too many requests", "429", "over compute",
    "capacity", "quota", "temporarily unavailable", "service unavailable",
    "bad gateway", "502", "503", "504", "timeout", "timed out", "try again",
    "connection reset", "connection refused",
)


def _repo_root():
    """Корень репозитория (родитель analysis/) — чтобы `-m analysis.…` работал из любого cwd:
    крон запускает нас не из рабочей директории разработчика."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_node_limit(msg) -> bool:
    """True — сообщение говорит о лимите/недоступности УЗЛА, а не о состоянии пула."""
    m = str(msg or "").lower()
    return any(h in m for h in _NODE_LIMIT_HINTS)


def _fetch(url, body, timeout=45):
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "route-canary/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _call(to, data, *, rpc=None, fetch=None, tries=4, pause=0.6, block="latest"):
    """eth_call с ретраем. Возвращает (result, err, kind), kind ∈ {ALIVE, DEAD, UNPOLLED}.

    Публичный узел режет по ставке. Без ретрая канарейка объявляла бы МЁРТВЫМ то, что просто
    не доспросили — а «не знаю» и «мёртв» это разные вещи (канон: незнание закрывает гард,
    но не выдаёт себя за диагноз).

    block — номер блока (hex) либо "latest". Пин нужен затем, что impact сравнивает ДВЕ
    котировки: на «latest» они уезжают в РАЗНЫЕ блоки, и любой чужой своп между ними
    приезжает в наш impact как «глубина пула».
    """
    fetch = fetch or _fetch
    url = rpc or RPC
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, block]}).encode()
    last = "нет ответа"
    for i in range(max(1, int(tries))):
        if pause:
            time.sleep(pause * (i + 1))
        try:
            r = fetch(url, body)
        except Exception as e:  # noqa: BLE001 — тип ошибки транспорта тут и есть диагноз
            last = type(e).__name__
            continue
        if not isinstance(r, dict):
            last = "не-JSON ответ"
            continue
        if "error" in r:
            msg = str((r.get("error") or {}).get("message"))[:120]
            if is_node_limit(msg):
                last = msg          # ретраим: это лимит узла, а не приговор маршруту
                continue
            return None, msg, DEAD  # положительное свидетельство отказа — это диагноз
        res = r.get("result")
        if res is None:
            last = "пустой result"
            continue
        return res, None, ALIVE
    return None, "%s (%s)" % (UNPOLLED, last), UNPOLLED


def quote(quoter, path_bytes, amount_in, *, call=None, **kw):
    """quoteExactInput у нашего квотера. Возвращает (amount_out, err, kind)."""
    call = call or _call
    ph = path_bytes.hex()
    data = ("0x" + SEL
            + "%064x" % 64                     # offset до bytes
            + "%064x" % amount_in
            + "%064x" % len(path_bytes)
            + ph + "0" * ((32 - len(path_bytes) % 32) % 32 * 2))
    res, err, kind = call(quoter, data, **kw)
    if res is None:
        return None, err or "пусто", kind or DEAD
    if len(res) < 66:
        return None, "короткий ответ (%d симв.)" % len(res), DEAD
    return int(res[2:66], 16), None, ALIVE


def block_number(*, rpc=None, fetch=None, timeout=45):
    """Текущий блок узла (hex-строка) либо None. None — НЕ отказ: замер продолжится на
    «latest», просто без пина (см. _call). Своё исключение канарейка тут глотает намеренно:
    неумение узла отдать номер блока не является фактом о маршрутах."""
    fetch = fetch or _fetch
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "eth_blockNumber", "params": []}).encode()
    try:
        r = fetch(rpc or RPC, body)
        res = r.get("result") if isinstance(r, dict) else None
        return res if isinstance(res, str) and res.startswith("0x") else None
    except Exception:  # noqa: BLE001 — транспорт/чужая заглушка: пин необязателен
        return None


def _route_quoter(r):
    """Квотер маршрута: явный `quoter` записи, иначе квотер семейства по `style`.
    Ровно то же правило, что в боевом chain/route_depth._route_quoter — прибор обязан
    спрашивать пул тем же квотером, каким его спрашивает бой.

    Почему НЕ по адресу роутера (так было до 27.08): у Aerodrome не один CL-роутер.
    Коммит d34f6d6 увёл cbXRP на вторую CL-фабрику (AERO_CL3_ROUTER 0x698Cb2b6 +
    свой QuoterV2 0x514c8B5f), сравнение `router == AERO_CL_ROUTER` дало False, и
    аэродромный путь поехал в UniV3-квотер. Тот ревертит по построению — канарейка
    объявила ЖИВОЙ маршрут МЁРТВЫМ (ложная тревога 27.08 17:06, ключ 5d198dc0).
    Признак семейства взят из ЗАПИСИ маршрута, а не из адреса: адресов у семейства много,
    запись одна."""
    q = r.get("quoter")
    if isinstance(q, str) and q.startswith("0x"):
        return q
    return AERO_QUOTER if r.get("style") == "v1" else UNIV3_QUOTER


def _venue_name(quoter):
    """Ярлык венью в строке лога. Ложный ярлык дороже отсутствующего: 27.08 строка
    сказала «UNIV3» про аэродромный маршрут, и разбор тревоги начинался бы не с той
    фабрики. `AERO*` — аэродромное семейство на НЕ дефолтной фабрике (свой квотер)."""
    if quoter == UNIV3_QUOTER:
        return "UNIV3"
    return "AERO" if quoter == AERO_QUOTER else "AERO*"


def routes(module=None):
    """Боевая карта маршрутов (chain.execute._SWAP_ROUTES) как список пар."""
    if module is None:
        sys.path.insert(0, _repo_root())
        import chain.execute as module  # noqa: PLC0415 — импорт боевого модуля по требованию
    return list(module._SWAP_ROUTES.items()), module


def scan(items=None, *, module=None, call=None, **kw):
    """Замер списка маршрутов. Возвращает список словарей — по одному на маршрут.

    Статус маршрута: DEAD (узел дал положительное свидетельство отказа) > UNPOLLED
    (узел нас не обслужил) > ALIVE. Порядок именно такой: реверт квотера — это факт о
    маршруте, а 429 — факт о нас.
    """
    if items is None:
        items, module = routes(module)
    elif module is None:
        sys.path.insert(0, _repo_root())
        import chain.execute as module  # noqa: PLC0415
    # ПИН БЛОКА (19.08). impact = отношение двух котировок, и на «latest» они попадают в
    # РАЗНЫЕ блоки: у мерцающего пула (cbADA/WETH ts=200 сегодня: L 8.4e16 -> 1.1e16 -> 5.9e16
    # за 40 минут) чужой своп между ними приезжает в наш impact как «форма пула», а по этому
    # числу режут капы. Один eth_blockNumber на проход; узел без архива честно уронит квоты
    # на пине — тогда пара перезапрашивается на «latest» (ниже), то есть хуже прежнего не
    # становится. Отсутствие номера блока = просто «latest», это не отказ сторожа.
    blk = kw.pop("block", None)
    if blk is None:
        blk = block_number(rpc=kw.get("rpc"), fetch=kw.get("fetch")) or "latest"
    out = []
    for (coll, loan), r in items:
        quoter = _route_quoter(r)
        who = _venue_name(quoter)
        cap = r.get("max_in")
        work = cap if cap else 10 ** 8            # рабочий объём: кап либо номинал
        ref = max(1, work // 100)                 # референс: 1% от рабочего
        q_ref, e1, k1 = quote(quoter, r["path"], ref, call=call, block=blk, **kw)
        q_work, e2, k2 = quote(quoter, r["path"], work, call=call, block=blk, **kw)
        if blk != "latest" and ALIVE not in (k1, k2):
            # на пине не ответили ОБА — вероятнее всего у узла нет состояния этого блока
            # (не архив). Это факт о УЗЛЕ, а не о маршруте: перезапрашиваем без пина.
            q_ref, e1, k1 = quote(quoter, r["path"], ref, call=call, block="latest", **kw)
            q_work, e2, k2 = quote(quoter, r["path"], work, call=call, block="latest", **kw)
        row = {"coll": coll, "loan": loan, "router": who, "cap": cap, "block": blk,
               "work": work, "ref": ref, "out": None, "impact": None, "err": None}
        if DEAD in (k1, k2):
            row["status"], row["err"] = DEAD, (e1 if k1 == DEAD else e2)
        elif UNPOLLED in (k1, k2):
            row["status"], row["err"] = UNPOLLED, (e1 if k1 == UNPOLLED else e2)
        else:
            px_ref = q_ref / ref if ref else 0
            px_work = q_work / work if work else 0
            row["status"] = ALIVE
            row["out"] = q_work
            row["impact"] = (1 - px_work / px_ref) * 100 if px_ref else 0.0
        out.append(row)
    return out


def render(rows, thresholds=None, out=print):
    """Таблица прогона. thresholds: (coll, loan) -> (порог %, откуда) либо None (тогда 10%)."""
    out("%-14s %-14s %-6s %-12s %14s %8s %8s" % (
        "collateral", "loan", "роутер", "статус", "выход", "impact", "порог"))
    for r in rows:
        th, src = (thresholds or {}).get((r["coll"], r["loan"]), (10.0, "дефолт"))
        if r["status"] != ALIVE:
            out("%-14s %-14s %-6s %-12s %14s %8s %7.2f%%  <- %s" % (
                r["coll"][:12], r["loan"][:12], r["router"], r["status"],
                "—", "—", th, r["err"]))
        else:
            flag = "  <- impact выше порога (%s)" % src if r["impact"] >= th else ""
            out("%-14s %-14s %-6s %-12s %14d %7.2f%% %7.2f%%%s" % (
                r["coll"][:12], r["loan"][:12], r["router"], r["status"],
                r["out"], r["impact"], th, flag))


def main():
    rows = scan()
    try:                                   # пороги считает вотчер (LIF конкретного рынка)
        from analysis.route_canary_watch import thresholds_for_rows
        th = thresholds_for_rows(rows)
    except Exception as e:                 # noqa: BLE001 — таблица обязана печататься всегда
        print("порог по умолчанию 10%% (LIF не подгружен: %s)" % e)
        th = None
    render(rows, th)
    bad = sum(1 for r in rows if r["status"] == DEAD
              or (r["status"] == ALIVE
                  and r["impact"] >= (th or {}).get((r["coll"], r["loan"]), (10.0, ""))[0]))
    unpolled = sum(1 for r in rows if r["status"] == UNPOLLED)
    print("\nмаршрутов: %d, проблемных: %d, не опрошено: %d" % (len(rows), bad, unpolled))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
