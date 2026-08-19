"""Юнит-приёмка прямого роута cbBTC->WETH (задача А, после потери 50191667 на $3905).

ПРЕДМЕТ. Рынок 0x5dffffc7 = cbBTC(залог)/WETH(долг). Пары (cbBTC,WETH) в карте роутов НЕ БЫЛО:
план цели жил на Kyber-агрегате, fast-lane невозможен, а для §9 (#133) такая цель НЕДОСТУПНА
ГРУППЕ по построению — пул Kyber-плана неизвестен, и запас худшего порядка считать не из чего.

КАНОНЫ:
 * КРАСНЫЙ-НА-БАЗЕ НА ТОМ ЖЕ ОБЪЕКТЕ: запись временно вынимается из карты, и ровно те же вызовы
   обязаны вернуть None. «Роут работает» без этого неотличимо от «тест не умеет ловить разницу».
 * КАП ПРОВЕРЯЕТСЯ ОБЕИМИ СТОРОНАМИ: под капом калдата есть, на wei выше — None (уход в Kyber).
 * ЧИСЛА РАЗВЕДКИ НЕ ДУБЛИРУЮТСЯ: тест читает их ИЗ КАРТЫ, иначе он ловил бы расхождение с
   собственной копией, а не с боевым значением.

ЗАПУСК: python3 test_route_cbbtc_weth.py
"""
import sys

import chain.execute as ex
import chain.route_depth as rd

FAILS = []
KEY = (ex._CBBTC.lower(), ex._UNIV3_WETH.lower())
LIQ = "0x" + "11" * 20


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


R = ex._SWAP_ROUTES.get(KEY)
check("роут cbBTC->WETH ЕСТЬ в карте", R is not None)
if R is None:
    sys.exit(1)
CAP = int(R["max_in"])
print("   запись: router=%s style=%s кап=%d (%.3f cbBTC)" % (R["router"][:12], R["style"], CAP,
                                                             CAP / 1e8))

# --- 1. ПРЯМОЙ РОУТ ОТДАЁТ КАЛДАТУ на размере целей рынка (0.06-0.35 cbBTC) --------------------
SEIZED = 6_027_321                      # сейз победителя эпизода: 0.06027321 cbBTC
sw = ex.direct_swap(ex._CBBTC, ex._UNIV3_WETH, SEIZED, LIQ, LIQ,
                    repaid_assets=1_839_273_459_101_730_693)
check("на сейзе эпизода роут собирается", sw is not None and sw["calldata"].startswith("0x"))
check("роутер записи = роутер калдаты", sw and sw["router"] == R["router"])

# --- 2. КРАСНЫЙ-НА-БАЗЕ: без записи те же вызовы дают None ------------------------------------
_saved = ex._SWAP_ROUTES.pop(KEY)
try:
    check("БЕЗ записи: direct_swap = None (состояние до задачи А — план уходил на Kyber)",
          ex.direct_swap(ex._CBBTC, ex._UNIV3_WETH, SEIZED, LIQ, LIQ,
                         repaid_assets=1) is None)
    check("БЕЗ записи: route_max_in = None", ex.route_max_in(ex._CBBTC, ex._UNIV3_WETH) is None)
    check("БЕЗ записи: §9-котировщик = None (цель ГРУППЕ недоступна)",
          rd.make_cumulative_quote_fn(None, ex._CBBTC, ex._UNIV3_WETH, [SEIZED],
                                      batch_fn=lambda *a: []) is None)
finally:
    ex._SWAP_ROUTES[KEY] = _saved

# --- 3. КАП РЕЖЕТ СВЕРХУ ------------------------------------------------------------------------
check("кап: ровно на капе роут есть",
      ex.direct_swap(ex._CBBTC, ex._UNIV3_WETH, CAP, LIQ, LIQ, repaid_assets=1) is not None)
check("кап: на wei выше — None (уход в Kyber)",
      ex.direct_swap(ex._CBBTC, ex._UNIV3_WETH, CAP + 1, LIQ, LIQ, repaid_assets=1) is None)
check("route_max_in отдаёт кап карты", ex.route_max_in(ex._CBBTC, ex._UNIV3_WETH) == CAP)

# --- 4. ПУТЬ ОДНОШАГОВЫЙ И В ПРАВИЛЬНУЮ СТОРОНУ ------------------------------------------------
path = R["path"]
check("путь однохоповый (20+3+20 байт)", len(path) == 43, len(path))
check("путь начинается cbBTC", path[:20].hex() == ex._CBBTC[2:].lower())
check("путь кончается WETH", path[23:].hex() == ex._UNIV3_WETH[2:].lower())
check("стиль и роутер согласованы",
      (R["style"] == "02" and R["router"] == ex.UNIV3_SWAP_ROUTER)
      or (R["style"] == "v1" and R["router"] == ex.AERO_CL_ROUTER), R["style"])

# --- 5. СИНЕРГИЯ С §9: пара стала виден котировщику группы --------------------------------------
_seen = {}


def _fake_batch(rpc, calls):
    """Транспорт подменён, всё остальное боевое: кодирование пути, выбор квотера, точки состава."""
    _seen["quoter"] = calls[0][0]
    _seen["n"] = len(calls)
    return [(True, (10 ** 18).to_bytes(32, "big")) for _ in calls]


qf = rd.make_cumulative_quote_fn(None, ex._CBBTC, ex._UNIV3_WETH,
                                 [SEIZED, 2 * SEIZED, 3 * SEIZED], batch_fn=_fake_batch)
check("§9: котировщик пары строится (цель стала ГОДНА в same-pool группу)", qf is not None)
check("§9: квоты ушли на квотер ТОЙ венью, что записана в карте",
      _seen.get("quoter") == (rd.UNIV3_QUOTER if R["style"] == "02" else rd.AERO_QUOTER),
      _seen.get("quoter"))
check("§9: спрошены точки состава (<=2n+1 одной пачкой)", 1 <= _seen.get("n", 0) <= 7, _seen)

# --- 6. ОСТАЛЬНАЯ КАРТА НЕ ЗАДЕТА ---------------------------------------------------------------
check("cbBTC->USDC жив (прямой Slipstream ts=100, с 28.07)",
      ex._SWAP_ROUTES.get((ex._CBBTC.lower(), ex._UNIV3_USDC.lower())) is not None)
check("обратной пары (WETH->cbBTC) не заведено — выход ликвидатора односторонний",
      ex._SWAP_ROUTES.get((ex._UNIV3_WETH.lower(), ex._CBBTC.lower())) is None)

if FAILS:
    print("=== РОУТ cbBTC->WETH: %d ПРОВАЛОВ ===" % len(FAILS))
    sys.exit(1)
print("=== РОУТ cbBTC->WETH ПРИНЯТ: fast-lane открыт, пара видна §9, кап режет сверху ===")
