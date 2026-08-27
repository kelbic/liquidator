"""27.08 cbXRP -> вторая CL-фабрика (AERO_CL3_*): запись маршрута, калдата direct_swap, квотер
ИЗ ЗАПИСИ у живого капа/§9 (и не течёт на соседей), негативный контроль — без ключа `quoter`
маршрут падает на квотер семейства. Замеры и происхождение адресов — в комментариях execute.py."""
import sys
sys.path.insert(0, ".")
import chain.execute as ex
import chain.route_depth as rd
from chain.execute import _SWAP_ROUTES, direct_swap, _CBXRP, _CBBTC, _UNIV3_USDC, _UNIV3_WETH

R = _SWAP_ROUTES[(_CBXRP, _UNIV3_USDC)]
assert R["router"] == ex.AERO_CL3_ROUTER and R["style"] == "v1", R
assert R["quoter"] == ex.AERO_CL3_QUOTER, R
p = R["path"]
assert p[:20].hex() == _CBXRP[2:] and p[23:43].hex() == _UNIV3_WETH[2:] and p[46:66].hex() == _UNIV3_USDC[2:] and len(p) == 66
assert p[20:23] == (100).to_bytes(3, "big") and p[43:46] == (50).to_bytes(3, "big"), "ноги ts100 -> ts50"
assert R["max_in"] == 64000 * 10**6, R["max_in"]
print("UNIT_OK 1: маршрут cbXRP: роутер второй фабрики, путь ts100->ts50, кап 64k, квотер записи")

ex._LIVE_CAPS.clear()
LIQ = "0x" + "11" * 20
sw = direct_swap(_CBXRP, _UNIV3_USDC, 16000 * 10**6, LIQ, LIQ, repaid_assets=20000 * 10**6)
assert sw is not None and sw["router"] == ex.AERO_CL3_ROUTER, sw
assert sw["calldata"].startswith("0x" + ex._AERO_EXACT_INPUT_SEL), "v1-форма exactInput (c04b8d59)"
assert p.hex() in sw["calldata"], "путь записи в калдате"
assert direct_swap(_CBXRP, _UNIV3_USDC, 64001 * 10**6, LIQ, LIQ, repaid_assets=1) is None, "сверх капа -> Kyber"
print("UNIT_OK 2: direct_swap строит 16k cbXRP на новый роутер той же ABI; сверх 64k -> None")

seen = {}
def _spy(rpc, calls):
    seen.setdefault("quoters", set()).update(q for q, _ in calls)
    return [(True, (10**18).to_bytes(32, "big")) for _ in calls]
rd.measure_route_depth(None, _CBXRP, _UNIV3_USDC, batch_fn=_spy) if hasattr(rd, "measure_route_depth") else None
qf = rd.make_cumulative_quote_fn(None, _CBXRP, _UNIV3_USDC, [10**6, 2 * 10**6], batch_fn=_spy)
assert seen["quoters"] == {ex.AERO_CL3_QUOTER}, seen
seen.clear()
rd.make_cumulative_quote_fn(None, _CBBTC, _UNIV3_USDC, [10**6, 2 * 10**6], batch_fn=_spy)
assert seen["quoters"] == {rd.AERO_QUOTER}, ("квотер второй фабрики НЕ должен течь на соседей", seen)
print("UNIT_OK 3: §9/живой кап cbXRP спрашивают квотер второй фабрики, cbBTC — прежний")

# негативный контроль: без ключа quoter — квотер семейства
saved = R.pop("quoter")
try:
    assert rd._route_quoter(R) == rd.AERO_QUOTER
    seen.clear(); rd.make_cumulative_quote_fn(None, _CBXRP, _UNIV3_USDC, [10**6], batch_fn=_spy)
    assert seen["quoters"] == {rd.AERO_QUOTER}, seen
finally:
    R["quoter"] = saved
print("UNIT_OK 4: негативный контроль — без ключа маршрут падает на квотер семейства (v1 -> 0x254c)")
