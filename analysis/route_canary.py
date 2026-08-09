"""Канарейка ВЫХОДОВ: жив ли каждый наш прямой маршрут и какова его глубина (#69).

Урок 24.07: «прямые роуты протухают за недели». 09.08 это подтвердилось ценой: cbXRP держали на
UniV3 fee=10000, где на боевом размере выход в 269 раз хуже Aerodrome — и план молча ревертил.
Проверили одну пару из одиннадцати. Эта канарейка проверяет ВСЕ и делает это дёшево: read-only
квоты на публичном узле, без ключей, без tx, боевой бот не задет.

Метод: для каждой записи _SWAP_ROUTES берём её же path и зовём quoteExactInput у СВОЕГО квотера
(style 02 -> UniV3 QuoterV2, style v1 -> Slipstream QuoterV2 — пулы одного тира в разных фактори
не видны чужому квотеру, урок 24.07). Считаем цену на малом объёме (референс) и на рабочем
(max_in либо дефолт), impact между ними — это и есть «сколько съест наш выход».

    python3 -m analysis.route_canary            # RPC=публичный Base
"""
import json
import os
import sys
import urllib.request

RPC = os.environ.get("CANARY_RPC", "https://mainnet.base.org")
UNIV3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
AERO_QUOTER = "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"
# Селектор СЧИТАЕМ, а не хардкодим: первая редакция несла старый QuoterV1-селектор и
# получала «execution reverted» на всех маршрутах — то есть канарейка врала бы, что
# мертво всё, тогда как мёртв был мой вызов.
from eth_utils import keccak
SEL = keccak(text="quoteExactInput(bytes,uint256)")[:4].hex()


def _call(to, data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(RPC, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "route-canary/1.0"})
    import time as _t
    # Публичный узел режет по ставке. Без ретрая канарейка объявляла бы МЁРТВЫМ то, что просто
    # не доспросили — а «не знаю» и «мёртв» это разные вещи (канон: незнание закрывает гард,
    # но не выдаёт себя за диагноз).
    r = None
    for _try in range(4):
        _t.sleep(0.6 * (_try + 1))
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=45).read())
            break
        except Exception as e:
            last = type(e).__name__
    if r is None:
        return None, "НЕ ОПРОШЕН (%s)" % last
    if "error" in r:
        return None, str(r["error"].get("message"))[:40]
    return r.get("result"), None


def quote(quoter, path_bytes, amount_in):
    ph = path_bytes.hex()
    data = ("0x" + SEL
            + "%064x" % 64                     # offset до bytes
            + "%064x" % amount_in
            + "%064x" % len(path_bytes)
            + ph + "0" * ((32 - len(path_bytes) % 32) % 32 * 2))
    res, err = _call(quoter, data)
    if res is None or len(res) < 66:
        return None, err or "пусто"
    return int(res[2:66], 16), None


def main():
    sys.path.insert(0, ".")
    import chain.execute as ex
    rows = []
    for (coll, loan), r in ex._SWAP_ROUTES.items():
        quoter = AERO_QUOTER if r["router"] == ex.AERO_CL_ROUTER else UNIV3_QUOTER
        cap = r.get("max_in")
        work = cap if cap else 10 ** 8            # рабочий объём: кап либо номинал
        ref = max(1, work // 100)                 # референс: 1% от рабочего
        q_ref, e1 = quote(quoter, r["path"], ref)
        q_work, e2 = quote(quoter, r["path"], work)
        if q_ref is None or q_work is None:
            rows.append((coll, loan, r, "МЁРТВ", e1 or e2, None, None))
            continue
        px_ref = q_ref / ref if ref else 0
        px_work = q_work / work if work else 0
        imp = (1 - px_work / px_ref) * 100 if px_ref else 0
        rows.append((coll, loan, r, "жив", None, q_work, imp))

    print("%-14s %-14s %-6s %-8s %10s %8s" % ("collateral", "loan", "роутер", "статус",
                                              "выход", "impact"))
    bad = 0
    for coll, loan, r, st, err, out, imp in rows:
        who = "AERO" if r["router"] == ex.AERO_CL_ROUTER else "UNIV3"
        if st == "МЁРТВ":
            bad += 1
            print("%-14s %-14s %-6s %-8s %10s %8s  <- %s" % (
                coll[:12], loan[:12], who, st, "—", "—", err))
        else:
            flag = "  <- impact выше 10%" if imp > 10 else ""
            bad += 1 if imp > 10 else 0
            print("%-14s %-14s %-6s %-8s %10d %7.2f%%%s" % (
                coll[:12], loan[:12], who, st, out, imp, flag))
    print("\nмаршрутов: %d, проблемных: %d" % (len(rows), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
