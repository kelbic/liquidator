"""Явная запись маршрута НЕ затирается обобщающим циклом (#69).

Корень боевого дефекта: `_SWAP_ROUTES` объявлялась литералом, а СЛЕДОМ шёл цикл по фи-карте
UniV3, который ПРИСВАИВАЛ записи по тем же ключам — то есть молча затирал всё явное. Из-за
этого cbXRP не мог иметь осознанного маршрута вовсе: он порождался циклом и всегда получался
UniV3. Когда глубина ушла в Slipstream (как у cbDOGE ts=2000 и cbADA ts=200), выразить это в
фи-карте стало нечем — она умеет только тир UniV3, и правка 24.07 («3000 умер → 1% живой»)
чинила симптом внутри механизма, который по построению не даёт сменить фактори.

Замер на форке 49666337: UniV3 fee=10000 отдавал 0.0176 WETH там, где Slipstream ts=2000 даёт
4.7342 (269x), после чего план молча ревертил по minOut = полный долг.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                                # noqa: E402

CBXRP = "0xcb585250f852c6c6bf90434ab21a00f02833a4af"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# --- 1. ГЛАВНОЕ: явная запись cbXRP пережила цикл ----------------------------------------
r = ex._SWAP_ROUTES[(CBXRP, USDC)]
assert r["router"] == ex.AERO_CL_ROUTER, \
    "цикл фи-карты снова затёр явный маршрут cbXRP (router=%s)" % r["router"]
# кап 5000 -> 3900 (#76, 09.08): прежний коммент считал запас от бонуса ЖИРНОГО рынка пары
# (lltv 0.625 = 12.7%), а связывает тесный (lltv 0.770 = 7.41%) — S_обрыв 7996 cbXRP / 2.
assert r["style"] == "v1" and r.get("max_in") == 3900 * 10**6, r

# --- 2. КОНТРОЛЬ: цикл всё ещё РАБОТАЕТ для пары БЕЗ явной записи ------------------------
# Без него «явное не затирается» неотличимо от «цикл сломан целиком». Проверяем СИНТЕТИЧЕСКОЙ
# парой, а не реальной: в фи-карте осталась ровно одна запись (cbXRP), и у неё теперь ЕСТЬ
# явный маршрут — реальных клиентов у цикла больше нет по построению.
_synth = "0x" + "ab" * 20
_saved_paths = dict(ex.UNIV3_PATHS)
try:
    ex.UNIV3_PATHS[_synth] = 3000
    _t = {}
    for _c, _f in ex.UNIV3_PATHS.items():                  # та же логика, что в модуле
        _t.setdefault((_c, USDC), {"router": ex.UNIV3_SWAP_ROUTER, "style": "02"})
    assert _t[(_synth, USDC)]["router"] == ex.UNIV3_SWAP_ROUTER, \
        "цикл перестал порождать маршрут для пары без явной записи"
    _t2 = {(_synth, USDC): {"router": ex.AERO_CL_ROUTER, "style": "v1"}}
    for _c, _f in ex.UNIV3_PATHS.items():
        _t2.setdefault((_c, USDC), {"router": ex.UNIV3_SWAP_ROUTER, "style": "02"})
    assert _t2[(_synth, USDC)]["router"] == ex.AERO_CL_ROUTER, \
        "setdefault не защищает явную запись — вернулось затирание"
finally:
    ex.UNIV3_PATHS.clear()
    ex.UNIV3_PATHS.update(_saved_paths)

# --- 2б. И сам факт, ради которого фикс делался: у фи-карты остался ОДИН клиент ----------
# Это и есть мера дефекта: единственная пара вне явных маршрутов оказалась тем рынком, где за
# неделю прошло 69 ликвидаций из 78 и $132.5k из $132.9k.
assert list(ex.UNIV3_PATHS) == [CBXRP], \
    "состав фи-карты изменился — перечитать вывод #69, он опирался на «единственного клиента»"

# --- 3. Кап уводит жир в Kyber, а средний размер идёт прямым путём -----------------------
sw = ex.direct_swap(CBXRP, USDC, 3000 * 10**6, "0x" + "11" * 20, "0x" + "11" * 20,
                    repaid_assets=1500 * 10**6)
assert sw and sw["router"] == ex.AERO_CL_ROUTER, "средний размер не пошёл прямым путём"
big = ex.direct_swap(CBXRP, USDC, 18333 * 10**6, "0x" + "11" * 20, "0x" + "11" * 20,
                     repaid_assets=16494 * 10**6)
assert big is None, "жир сверх капа обязан уходить в Kyber, а не в тонкий пул"

# --- 4. СТРУКТУРНО: присваивание в цикле не вернулось ------------------------------------
src = pathlib.Path("chain/execute.py").read_text()
loop = src[src.index("for _coll, _fee in UNIV3_PATHS.items():"):]
loop = loop[:loop.index("ROUTE_COLLATERALS")]
assert "_SWAP_ROUTES.setdefault(" in loop, "цикл снова присваивает — явные записи будут затёрты"
assert "_SWAP_ROUTES[(_coll, _UNIV3_USDC)] =" not in loop, "прямое присваивание вернулось"

print("OK: явный маршрут переживает цикл, цикл жив для остальных, кап уводит жир в Kyber")
