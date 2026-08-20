"""Юниты пер-роутного капа + weETH-роут. Без сети."""
import sys
import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from chain.execute import (direct_swap, _SWAP_ROUTES, ROUTE_COLLATERALS,
                           _WEETH, _WSTETH, _UNIV3_WETH)

LIQ = "0x00000000000000000000000000000000000000ff"

# 1) weETH-роут добавлен, попал в ROUTE_COLLATERALS (fast-lane + кэш слоя-2)
# #75 (09.08): кап 160 -> 140. Замер на трёх блоках (49753974/49754165/49754310): обрыв пула
# ts=1 сегодня на 287 weETH (281 -> imp 0.02%, 292 -> 3.21%, 302 -> 6.42%), бонус пары 1.6777%
# (тесный рынок lltv 0.945) достигается на 286.9 -> S_обрыв/2 = 143.5 -> кап 140.
# 19.08 (разбор тревоги канарейки #72): кап 140 -> 55. Обрыв УЕХАЛ ПОД КАП и держится вторые
# сутки: лестница в одном блоке на трёх блоках подряд (50196398/50196414/50196429) — плоско до
# 105 weETH (<=0.05%), колено 110 (1.25/2.90/0.08%), 140 -> 21.06-23.70% при бонусе 1.6777%.
# Архив-квоты за 4.5ч (9 точек, шаг 30 мин): на 140 разорительно 9/9, на 55 — 0/9.
# S_обрыв ~110 -> S_обрыв/2 = 55. Значение ЖЁСТКОЕ: менять только с новым замером.
assert (_WEETH, _UNIV3_WETH) in _SWAP_ROUTES, "weETH-роут отсутствует"
assert _WEETH in ROUTE_COLLATERALS, "weETH не в ROUTE_COLLATERALS"
assert _SWAP_ROUTES[(_WEETH, _UNIV3_WETH)]["max_in"] == 55 * 10**18
print("UNIT_OK: weETH->WETH роут добавлен, в ROUTE_COLLATERALS, кап 55")

# 2) под капом — валидный direct calldata; над капом — None (-> caller Kyber)
under = direct_swap(_WEETH, _UNIV3_WETH, 50 * 10**18, LIQ, LIQ, repaid_assets=1)
assert under is not None and under["calldata"].startswith("0x") and under["router"], under
over = direct_swap(_WEETH, _UNIV3_WETH, 140 * 10**18, LIQ, LIQ, repaid_assets=1)
assert over is None, "над капом должен быть None (Kyber-фолбэк)"
print("UNIT_OK: weETH под капом -> direct; над капом -> None")

# 3) wstETH кап 150; граница включительна
# #75 (09.08): 300 -> 150. Прежний коммент обещал «300 < обрыва», но обрыв приехал ВПЛОТНУЮ:
# 295 -> 0.003%, 305 -> 0.023%, 315 -> 0.29%, 326 -> 3.45%, 340 -> 7.44% (три блока подряд).
# Бонус пары 1.0611% — самый тесный во флоте (lltv 0.965) — достигается на 317.7 wstETH,
# то есть кап стоял в 6% от размера, съедающего ВЕСЬ бонус. S_обрыв/2 = 158.9 -> кап 150.
assert _SWAP_ROUTES[(_WSTETH, _UNIV3_WETH)]["max_in"] == 150 * 10**18
assert direct_swap(_WSTETH, _UNIV3_WETH, 150 * 10**18, LIQ, LIQ, repaid_assets=1) is not None, "ровно кап — ок"
assert direct_swap(_WSTETH, _UNIV3_WETH, 150 * 10**18 + 1, LIQ, LIQ, repaid_assets=1) is None, "кап+1 — None"
print("UNIT_OK: wstETH кап 150 включительно, кап+1 -> None")

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

# 5b) #75/#76: у КАЖДОГО капнутого роута кап согласован с бонусом ТЕСНЕЙШЕГО рынка пары.
# Прежний коммент cbXRP объявлял «impact 4.84% против LIF 12.7%», взяв бонус ЖИРНОГО рынка
# пары (lltv 0.625) при живом втором рынке с lltv 0.770 (бонус 7.41%). Тест держит инвариант:
# бонус роута = минимум по рынкам, и импакт на капе обязан быть ниже его.
import chain.route_depth as _rd
assert abs(_rd.route_bonus_pct(_CBXRP := "0xcb585250f852c6c6bf90434ab21a00f02833a4af",
                               _UNIV3_USDC) - 7.4114) < 1e-3, "cbXRP: бонус тесного рынка 7.41%"
for _k, _r in _SWAP_ROUTES.items():
    _b = _rd.route_bonus_pct(*_k)
    assert _b is not None, "роут %s без бонуса в таблице lltv — незнание должно закрывать гард" % (_k,)
    assert 0 < _rd.impact_budget_pct(*_k) < _b, "бюджет обязан быть строго внутри бонуса"
print("UNIT_OK: у всех 11 роутов бонус с цепи, бюджет = его доля (cbXRP 7.41%%, не 12.7%%)")

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

# 7) 24.07 вечер: cbETH-роут (дыра как у weETH) + кап EURC-ноги cbBTC
from chain.execute import _CBETH, _CBBTC as _CB, _EURC
r = _SWAP_ROUTES[(_CBETH, _UNIV3_WETH)]
assert r["router"] == AERO_CL_ROUTER and r["max_in"] == 300 * 10**18, r
assert _CBETH in RC, "cbETH не попал в fast-lane/кэш слоя-2"
assert direct_swap(_CBETH, _UNIV3_WETH, 250 * 10**18, LIQ, LIQ, repaid_assets=1) is not None
assert direct_swap(_CBETH, _UNIV3_WETH, 301 * 10**18, LIQ, LIQ, repaid_assets=1) is None
assert _SWAP_ROUTES[(_CB, _EURC)]["max_in"] == 15 * 10**7, "EURC-нога: кап 1.5 cbBTC"
assert direct_swap(_CB, _EURC, 2 * 10**8, LIQ, LIQ, repaid_assets=1) is None, "2 cbBTC -> Kyber"
from chain.spec_fire import _label_of as _lo
assert _lo("cbETH") == "ETH", "cbETH — пег (ETH-нога), predict не армит"
print("UNIT_OK: cbETH-роут (кап 300) + EURC-кап 1.5; пег-семантика верна")

# 8) 25.07: капы мем-путей по замеру обрыва (cbDOGE 100k ток., cbADA 60k)
# #71 (09.08): кап понижен 100k -> 50k по ЗАМЕРУ канарейки — на 100k пул отдавал impact
# 43.50% при бонусе 12.7% (обрыв, не деградация: 50% капа = 0.94%). Значение здесь
# ЖЁСТКОЕ намеренно: оно защищает деньги, и менять его можно только с новым замером.
# #71 ЗАКРЫТ (09.08, три блока): 50k подтверждён «до обрыва» (imp 0.69%), но НЕ достаточен —
# сетка 48k..74k показала колено ровно за ним: 52k -> 0.99%, 56k -> 2.28%, 58.4k -> 3.33%,
# 60.8k -> 6.82%, 63.2k -> 10.41%, 65.8k -> 13.87%. Полный бонус 12.676% достигается на 64.9k,
# то есть кап стоял в 1.3x от размера-разорителя. Кап 32k = S_обрыв/2 (делитель — замеренный
# дрейф обрыва: у этого же пула 135k 25.07 -> 65k 09.08).
assert _SWAP_ROUTES[(_CBDOGE, _UNIV3_USDC)]["max_in"] == 32_000 * 10**8
# 19.08 (тревога канарейки imp 16.03% на капе): cbADA 60k -> 15k. У пула ts=200 нет ОДНОГО
# обрыва — он мерцает: 30 архив-выборок за 14.5ч дали разорительный выход на капе 60k в 5/30
# (в здоровом состоянии обрыв на ~190k, в осушенном даже 6k стоит 3.66%). Статик выбран по
# РАСПРЕДЕЛЕНИЮ (15k безопасен в 29/30), быстрая динамика — на живом слое route_depth.
assert _SWAP_ROUTES[(_CBADA, _UNIV3_USDC)]["max_in"] == 15_000 * 10**6
# границы двигаются ВМЕСТЕ с капом: 30k под ним (директ), 40k сверх (Kyber)
assert direct_swap(_CBDOGE, _UNIV3_USDC, 30_000 * 10**8, LIQ, LIQ, repaid_assets=1) is not None
assert direct_swap(_CBDOGE, _UNIV3_USDC, 40_000 * 10**8, LIQ, LIQ, repaid_assets=1) is None
assert direct_swap(_CBADA, _UNIV3_USDC, 15_000 * 10**6, LIQ, LIQ, repaid_assets=1) is not None
assert direct_swap(_CBADA, _UNIV3_USDC, 60_000 * 10**6, LIQ, LIQ, repaid_assets=1) is None
print("UNIT_OK: капы cbDOGE/cbADA — под обрывом директ, сверх — Kyber")

# 8b) #75: роутов БЕЗ капа больше нет. cbLTC/USDC и LBTC/cbBTC жили без max_in, а значит
# и без живого слоя (route_depth меряет только капнутые) — при этом cbLTC-пул отдаёт 55%
# импакта на $50k, а LBTC/cbBTC обрывается на 54.7 LBTC при бонусе всего 1.68%.
from chain.execute import _CBLTC, _LBTC
assert all(r.get("max_in") for r in _SWAP_ROUTES.values()), "бескапный роут = слепая зона"
assert _SWAP_ROUTES[(_CBLTC, _UNIV3_USDC)]["max_in"] == 280 * 10**8      # S_обрыв 565 / 2
assert _SWAP_ROUTES[(_LBTC, _CBBTC)]["max_in"] == 27 * 10**8             # S_обрыв 54.7 / 2
assert direct_swap(_CBLTC, _UNIV3_USDC, 1_000 * 10**8, LIQ, LIQ, repaid_assets=1) is None
assert direct_swap(_LBTC, _CBBTC, 60 * 10**8, LIQ, LIQ, repaid_assets=1) is None
print("UNIT_OK: cbLTC/LBTC капнуты по замеру — бескапных роутов не осталось")

# 9) 27.07 WETH->USDC (новый рынок $70.2M): fee=3000, НЕ 500 — замер глубины опрокинул шаблон
from chain.execute import _UNIV3_WETH as _W
_r = _SWAP_ROUTES[(_W, _UNIV3_USDC)]
assert _r["router"] == UNIV3_SWAP_ROUTER and _r["max_in"] == 8000 * 10**18
assert _r["path"][20:23] == (3000).to_bytes(3, "big"), "должен быть тир 3000 (замер: 500 в 25× тоньше)"
assert _W in RC, "WETH обязан попасть в fast-lane/кэш слоя-2 нового рынка"
assert direct_swap(_W, _UNIV3_USDC, 600 * 10**18, LIQ, LIQ, repaid_assets=1) is not None
assert direct_swap(_W, _UNIV3_USDC, 8001 * 10**18, LIQ, LIQ, repaid_assets=1) is None
print("UNIT_OK: WETH->USDC fee=3000 кап 8000 ETH; крупнейшая позиция рынка ~545 ETH (запас 15×)")

print("=== ВСЕ ROUTE-CAP ЮНИТЫ ПРОШЛИ (вкл. WETH/USDC) ===")
