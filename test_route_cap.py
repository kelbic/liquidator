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

# 4) роут БЕЗ капа (cbBTC/USDC) не задет — любой размер отдаёт calldata
from chain.execute import _CBBTC, _UNIV3_USDC
assert "max_in" not in _SWAP_ROUTES[(_CBBTC, _UNIV3_USDC)]
big = direct_swap(_CBBTC, _UNIV3_USDC, 10**12, LIQ, LIQ, repaid_assets=1)
assert big is not None, "роут без капа не должен резаться"
print("UNIT_OK: роуты без max_in не затронуты")

# 5) peg-пары (weETH/wstETH — обе ноги ETH) исключены из predict-окон -> кап там не нужен,
#    он защищает ТОЛЬКО реактивное исполнение
from chain.spec_fire import _label_of
assert _label_of("weETH") == _label_of("WETH") == "ETH", "weETH/WETH обе ETH-ноги"
assert _label_of("wstETH") == "ETH"
print("UNIT_OK: weETH/wstETH — peg (обе ETH-ноги), predict их не армит; кап только для реактива")

print("=== ВСЕ ROUTE-CAP ЮНИТЫ ПРОШЛИ ===")
