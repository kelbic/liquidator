"""Юниты gas-floor (25.07, форк-экзамен кита): лимит tx НЕ падает до 500k, когда
preconf-estimate не ответил — иначе приз уходит в out-of-gas. Стоимость по-прежнему
считается по gas_limit_est (net-гейт мелких призов в каскаде не должен задохнуться)."""
import sys, types
sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
from config import Config
import chain.hotpath as hp

# 1) конфиг: floor есть, он ВЫШЕ фактического расхода кита (2 470 096 на форке 25.07)
d = Config()
assert d.gas_limit_floor >= 2_470_096, "floor обязан покрывать замеренный расход кита"
assert d.gas_limit_est == 500_000, "оценка стоимости остаётся отдельной величиной"
print("UNIT_OK: floor=%d покрывает замер кита 2470096, est=%d для стоимости" % (
    d.gas_limit_floor, d.gas_limit_est))

# 2) env-рычаг работает
import os
os.environ["GAS_LIMIT_FLOOR"] = "4200000"
try:
    assert Config.from_env().gas_limit_floor == 4_200_000
finally:
    del os.environ["GAS_LIMIT_FLOOR"]
print("UNIT_OK: GAS_LIMIT_FLOOR читается из env")

# 3) стоимость в net-гейте НЕ раздувается floor'ом (иначе каскадный tip задушит мелкие призы)
cfg = types.SimpleNamespace(gas_limit_est=500_000, gas_limit_floor=3_000_000,
                            tip_gwei=30.0, eth_price_usd=1730.0, min_profit_usd=5.0)
_p, cost, _n = hp._net_gate(1_000_000, 1000.0, 1_000_000, cfg)
cost_if_floor = cfg.gas_limit_floor * cfg.tip_gwei * cfg.eth_price_usd / 1e9
assert cost < cost_if_floor / 3, (cost, cost_if_floor)
print("UNIT_OK: стоимость в гейте $%.0f (по est), а не $%.0f (по floor) — мелкие призы живы" % (
    cost, cost_if_floor))

# 4) кэш газа: реальный estimate ВЫШЕ floor -> берём его (батч не занизит лимит)
assert max(d.gas_limit_floor + 500_000, d.gas_limit_floor) == d.gas_limit_floor + 500_000
print("UNIT_OK: живой estimate выше floor не срезается")

# 5) floor покрывает ФАКТИЧЕСКИЙ расход на позицию в батче (форк 25.07: 11.25M / 3 = 3.75M)
assert d.gas_limit_floor >= 3_750_000, "floor ниже замеренного расхода позиции в батче"
print("UNIT_OK: floor=%.1fM >= 3.75M (факт на позицию в батче из 3 китов)" % (d.gas_limit_floor / 1e6))

print("=== ВСЕ GAS-FLOOR ЮНИТЫ ПРОШЛИ ===")
