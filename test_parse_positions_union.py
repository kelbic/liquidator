"""Юниты унии HF-оценок в parse_positions (инцидент 31.07: API ночью сменил USD-прайсинг
msETH -> деривация collUsd/borrowUsd*lltv прыгнула 1.04->1.25 через потолок 1.10, кластер
wstETH/msETH с on-chain HF 1.006-1.015 выпал из кандидатов — реактивный путь ослеп).
Проверяет: (A) кандидат берётся, если ЛЮБАЯ из оценок <= потолка; (B) хранится минимум
(ближайшая к флипу); (C) без поля healthFactor — прежнее поведение; (D) обе высоко — режется;
(E) bad-debt фильтр (coll<borrow) главнее унии. Без сети."""
import sys

sys.path.insert(0, ".")
from chain.morpho import parse_positions

MID = "0x" + "aa" * 32
LLTV = {MID: 0.945}


def item(coll_usd, borrow_usd, hf_api=None, user="0x" + "11" * 20):
    it = {"user": {"address": user}, "market": {"marketId": MID},
          "state": {"collateral": "1000000", "collateralUsd": coll_usd,
                    "borrowAssets": "500000", "borrowAssetsUsd": borrow_usd,
                    "borrowShares": "1"}}
    if hf_api is not None:
        it["healthFactor"] = hf_api
    return it


def run(*items, ceiling=1.10):
    return parse_positions({"data": {"marketPositions": {"items": list(items)}}},
                           LLTV, hf_ceiling=ceiling, min_debt_usd=200.0)


# A) msETH-сценарий 31.07: USD-деривация 1.259 (>1.10), поле 1.0063 (<=1.10) -> ВКЛЮЧЁН
got = run(item(468146.0, 351232.0, hf_api=1.0063))
assert len(got) == 1, "уния обязана спасать кандидата при вранье USD-марок"
# B) хранится минимум из оценок
assert abs(got[0].health_factor - 1.0063) < 1e-9, got[0].health_factor
print("UNIT_OK: уния включает по полю healthFactor, хранится ближайшая к флипу")

# обратная сторона: деривация низкая, поле высокое (LBTC-класс вранья) — тоже включён, hf=деривация
got = run(item(120000.0, 110000.0, hf_api=1.4))          # деривация 1.031
assert len(got) == 1 and abs(got[0].health_factor - (120000.0 / 110000.0 * 0.945)) < 1e-9
print("UNIT_OK: включение по деривации при высоком поле (симметрия)")

# C) поля нет — прежнее поведение (только деривация)
assert len(run(item(120000.0, 110000.0))) == 1
assert len(run(item(200000.0, 110000.0))) == 0           # деривация 1.718 > 1.10
print("UNIT_OK: без поля healthFactor поведение прежнее")

# D) обе оценки выше потолка — режется
assert len(run(item(200000.0, 110000.0, hf_api=1.5))) == 0
print("UNIT_OK: обе высоко — кандидата нет")

# E) bad-debt (коллатерал < долга) режется ДО унии — защита от реверта 0x81ceff30
assert len(run(item(90000.0, 110000.0, hf_api=0.9))) == 0
print("UNIT_OK: bad-debt фильтр главнее унии")

print("=== ВСЕ UNION-ЮНИТЫ ПРОШЛИ ===")
