"""Юниты батч-лимита и клампа по балансу (25.07, флаг ревьювера #1 + решение владельца
не доливать ETH): батч-путь был третьим и падал на фолбэк 500k; тонкий кошелёк не должен
отбивать залп 'insufficient funds'. Без сети."""
import sys, types
sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
from chain.execute import batch_gas_limit, clamp_fee_to_balance, gas_floor_of

cfg = types.SimpleNamespace(gas_limit_est=500_000, gas_limit_floor=3_000_000)
GW = 10 ** 9

# 1) батч из 3 КИТОВ с известными лимитами (форк 25.07: 2.47M и 2.17M) — не 500k-фолбэк
preps = [{"gas": 2_470_096}, {"gas": 2_166_534}, {"gas": 2_400_000}]
g = batch_gas_limit(preps, cfg)
assert g > 8_500_000, g
print("UNIT_OK: батч 3 китов -> лимит %.2fM (реальные расходы + запас)" % (g / 1e6))

# 2) prep БЕЗ поля gas (блок-луп prepare_liquidation) -> фолбэк floor, НЕ gas_limit_est
g2 = batch_gas_limit([{}, {}, {}], cfg)
g_old = int((3 * cfg.gas_limit_est + 30_000 * 3) * 1.25)
assert g2 == int((3 * cfg.gas_limit_floor + 90_000) * 1.25) and g2 > 4 * g_old
print("UNIT_OK: prep без gas -> %.2fM по floor (прежний фолбэк дал бы %.2fM = OOG)" % (
    g2 / 1e6, g_old / 1e6))
assert gas_floor_of(types.SimpleNamespace()) == 3_000_000, "дефолт floor при отсутствии поля"

# 3) кламп: резерв влезает -> ничего не меняем
bal = 10 ** 17                                     # 0.1 ETH
mf, tip, ok = clamp_fee_to_balance(3_000_000, 2 * GW, GW, bal, min_tip_wei=10 ** 6)
assert ok and (mf, tip) == (2 * GW, GW)
print("UNIT_OK: резерв влезает — fee не тронут")

# 4) каскадный tip 30 gwei на 3M лимите = 0.09 ETH: при 0.094 ETH и 10% подушке НЕ влезает ->
#    режем fee, лимит НЕ трогаем (иначе вернули бы OOG-дыру)
bal = 94 * 10 ** 15                                # 0.094 ETH (реальный баланс 25.07)
mf0, tip0 = 31 * GW, 30 * GW
mf, tip, ok = clamp_fee_to_balance(3_000_000, mf0, tip0, bal, min_tip_wei=5 * 10 ** 7)
assert ok and mf < mf0 and tip < tip0
assert 3_000_000 * mf <= bal * 9 // 10, "резерв обязан влезть в баланс с подушкой"
print("UNIT_OK: тонкий кошелёк — tip %.1f->%.2f gwei, лимит 3M цел, резерв влез" % (
    tip0 / GW, tip / GW))

# 5) баланса не хватает даже на базовую цену -> ok=False (вызывающий дропает хвост, без nonce-гэпа)
mf, tip, ok = clamp_fee_to_balance(12_000_000, 30 * GW, 29 * GW, 10 ** 15, min_tip_wei=10 ** 6)
assert not ok
print("UNIT_OK: неподъёмный резерв -> ok=False (хвост дропается, гэпа нет)")

# 6) min_tip уважается: если урезание уводит tip ниже минимума — отказ, а не отправка «в пустоту»
mf, tip, ok = clamp_fee_to_balance(3_000_000, 31 * GW, 30 * GW, 10 ** 16, min_tip_wei=5 * GW)
assert not ok, (mf, tip, ok)
print("UNIT_OK: tip ниже min_tip -> отказ, а не бессмысленный выстрел")

# 7) 25.07 патронный резерв: расходники не грызут выстрел (баланс долит до 0.1938)
from chain.execute import probe_allowed_by_reserve, BULLET_RESERVE_WEI
_probe = 4_000_000 * int(0.15 * GW)                 # проба: лимит 4M, maxFee ~0.15 gwei
assert BULLET_RESERVE_WEI == int(0.12 * 10 ** 18), BULLET_RESERVE_WEI
assert probe_allowed_by_reserve(int(0.193838 * 10 ** 18), _probe) is True
assert probe_allowed_by_reserve(int(0.1205 * 10 ** 18), _probe) is False
assert probe_allowed_by_reserve(None, _probe) is True, "слепота -> fail-open"
_bullet = 4_000_000 * 30 * GW                       # патрон 30 gwei влезает в 0.1938 с подушкой
assert _bullet <= int(0.193838 * 10 ** 18) * 9 // 10
print("UNIT_OK: патрон %.2f ETH защищён от расходников; на 0.12 пробы молчат" % (_bullet / 1e18))

print("=== ВСЕ GAS/BALANCE ЮНИТЫ ПРОШЛИ (вкл. патронный резерв) ===")
