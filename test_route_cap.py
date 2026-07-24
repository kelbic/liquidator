"""Юниты пер-роутного капа + weETH-роут. Без сети."""
import sys
sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
from chain.execute import (direct_swap, _SWAP_ROUTES, ROUTE_COLLATERALS,
                           _WEETH, _WSTETH, _UNIV3_WETH)

LIQ = "0x00000000000000000000000000000000000000ff"

# 1) weETH-роут добавлен, попал в ROUTE_COLLATERALS (fast-lane + кэш слоя-2)
assert (_WEETH, _UNIV3_WETH) in _SWAP_ROUTES, "weETH-роут отсутствует"
assert _WEETH in ROUTE_COLLATERALS, "weETH не в ROUTE_COLLATERALS"
assert _SWAP_ROUTES[(_WEETH, _UNIV3_WETH)]["max_in"] == 160 * 10**18
print("UNIT_OK: weETH->WETH роут добавлен, в ROUTE_COLLATERALS, кап 160")

# 2) под капом — валидный direct calldata; над капом — None (-> caller Kyber)
under = direct_swap(_WEETH, _UNIV3_WETH, 150 * 10**18, LIQ, LIQ, repaid_assets=1)
assert under is not None and under["calldata"].startswith("0x") and under["router"], under
over = direct_swap(_WEETH, _UNIV3_WETH, 200 * 10**18, LIQ, LIQ, repaid_assets=1)
assert over is None, "над капом должен быть None (Kyber-фолбэк)"
print("UNIT_OK: weETH под капом -> direct; над капом -> None")

# 3) wstETH кап 300; граница включительна
assert _SWAP_ROUTES[(_WSTETH, _UNIV3_WETH)]["max_in"] == 300 * 10**18
assert direct_swap(_WSTETH, _UNIV3_WETH, 300 * 10**18, LIQ, LIQ, repaid_assets=1) is not None, "ровно кап — ок"
assert direct_swap(_WSTETH, _UNIV3_WETH, 300 * 10**18 + 1, LIQ, LIQ, repaid_assets=1) is None, "кап+1 — None"
print("UNIT_OK: wstETH кап 300 включительно, кап+1 -> None")

# 4) cbBTC/USDC кап 30 cbBTC (~$1.9M): кит $1.5M (~25 cbBTC) — директ; сверх — Kyber
from chain.execute import _CBBTC, _UNIV3_USDC
assert _SWAP_ROUTES[(_CBBTC, _UNIV3_USDC)]["max_in"] == 30 * 10**8
whale = direct_swap(_CBBTC, _UNIV3_USDC, 25 * 10**8, LIQ, LIQ, repaid_assets=1)
assert whale is not None, "сайз кита обязан идти директом (скорость, профит жив)"
mega = direct_swap(_CBBTC, _UNIV3_USDC, 31 * 10**8, LIQ, LIQ, repaid_assets=1)
assert mega is None, "сверх капа (imp>бонуса) — None -> Kyber"
print("UNIT_OK: cbBTC кап 30 — кит директом, минусовый мега-сайз в Kyber")

# 5) peg-пары (weETH/wstETH — обе ноги ETH) исключены из predict-окон -> кап там не нужен,
#    он защищает ТОЛЬКО реактивное исполнение
from chain.spec_fire import _label_of
assert _label_of("weETH") == _label_of("WETH") == "ETH", "weETH/WETH обе ETH-ноги"
assert _label_of("wstETH") == "ETH"
print("UNIT_OK: weETH/wstETH — peg (обе ETH-ноги), predict их не армит; кап только для реактива")

# 6) ре-верификация мем-ног 24.07: cbXRP -> UniV3 1%; cbDOGE/cbADA -> Slipstream-пути
from chain.execute import (UNIV3_PATHS, _CBDOGE, _CBADA, AERO_CL_ROUTER,
                           UNIV3_SWAP_ROUTER, ROUTE_COLLATERALS as RC)
assert UNIV3_PATHS["0xcb585250f852c6c6bf90434ab21a00f02833a4af"] == 10000, "cbXRP: 3000 мёртв"
for tok, ts in ((_CBDOGE, 2000), (_CBADA, 200)):
    r = _SWAP_ROUTES[(tok, _UNIV3_USDC)]
    assert r["router"] == AERO_CL_ROUTER and r["style"] == "v1", r
    assert r["path"][20:23] == ts.to_bytes(3, "big"), "первая нога ts=%d" % ts
    assert tok in RC, "выпал из fast-lane/кэша слоя-2"
sw = direct_swap(_CBDOGE, _UNIV3_USDC, 223671650512, LIQ, LIQ, repaid_assets=135680940)
assert sw is not None and sw["router"] == AERO_CL_ROUTER, sw
print("UNIT_OK: мем-ноги пере-верифицированы (cbXRP 1%%, cbDOGE ts2000, cbADA ts200)")

print("=== ВСЕ ROUTE-CAP ЮНИТЫ ПРОШЛИ ===")
