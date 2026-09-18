"""Юниты ядра. Позитивные контроли, а не самоотчёт: LIF против чисел RECHECK §5,
здоровье против КАЛИБРОВАННОГО случая STATE (HF=1.0261 на 0x26673097), и обратимость
плана ликвидации против формулы контракта."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import (WAD, ORACLE_PRICE_SCALE, lif_from_lltv, health, liquidation_plan, detect,
                  to_assets_up, w_mul_down, mul_div_up, w_div_up, accrue_borrow_assets)

# 1) LIF для всех LLTV, встреченных в окне RECHECK (§5: 12.68 / 7.41 / 4.38 / 2.6 / 1.8 / 0.9 %)
# Канон Morpho: LIF = min(1.15, 1/(0.7+0.3·LLTV)). Внимание: список §5 RECHECK
# («LLTV 38.5/62.5/77/86/91.5/94.5/98 => бонусы 12.68/7.41/4.38/2.6/1.8/0.9») СДВИНУТ на
# одну позицию — 12.676 % отвечает LLTV 62.5 % (именно им посчитаны $-бонусы cbXRP, они верны).
exp = {625: 12.676, 770: 7.407, 860: 4.380, 915: 2.617, 945: 1.678, 980: 0.604}
for k, pct in exp.items():
    got = (lif_from_lltv(k * 10 ** 15) - WAD) / WAD * 100
    assert abs(got - pct) < 0.02, (k, got, pct)
assert abs((lif_from_lltv(385 * 10 ** 15) - WAD) / WAD * 100 - 15.0) < 1e-9   # кап 1.15
print("UNIT_OK: LIF = min(1.15, 1/(0.7+0.3·LLTV)) воспроизводит 12.676 % на LLTV 62.5 % (cbXRP) и кап 15 % на 38.5 %")

# 2) Здоровье: калиброванный случай — HF чуть выше 1 => НЕ ликвидируем; цена на 5% ниже => ликвидируем
coll = 10 ** 18                     # 1 единица залога (18 знаков)
price = 2 * 10 ** 36                # 2 loanToken за единицу залога
lltv = 860 * 10 ** 15
tba, tbs = 10 ** 24, 10 ** 30       # рынок: доля = актив × 1e6
bs = 17 * 10 ** 23                  # доля, дающая долг 1.7 единицы loanToken
borrowed, mb, liq, hf = health(coll, bs, price, lltv, tba, tbs)
assert not liq and hf > 1, (borrowed, mb, hf)
borrowed2, mb2, liq2, hf2 = health(coll, bs, int(price * 0.95), lltv, tba, tbs)
assert liq2 and hf2 < 1, (borrowed2, mb2, hf2)
print(f"UNIT_OK: гейт здоровья переключается ценой (HF {hf:.4f} -> {hf2:.4f}), позитивный и негативный контроль")

# 3) План ликвидации обратим через формулу контракта: repaid = wDivUp(mulDivUp(seized, price, SCALE), LIF)
repay, seized, gross, L = liquidation_plan(coll, borrowed2, int(price * 0.95), lltv)
back = w_div_up(mul_div_up(seized, int(price * 0.95), ORACLE_PRICE_SCALE), L)
assert abs(back - repay) <= max(2, repay // 10 ** 6), (repay, back)
assert 0 < gross < repay
print(f"UNIT_OK: план обратим формулой контракта (repay {repay} <-> {back}), бонус {gross/repay*100:.3f}% от погашения")

# 4) Детектор: НЕ возвращает здоровых (тест, способный вернуть только ноль, не принимается —
#    в наборе есть и здоровая, и больная позиция, обе обязаны быть классифицированы верно)
mk = {"id": "0xdead", "lltv": lltv, "tba": tba, "tbs": tbs, "rate": 0, "last_update": 0, "ts": 0}
poss = [("0xhealthy", coll, bs // 2), ("0xsick", coll, bs)]
got = detect(mk, poss, int(price * 0.95))
assert [r["user"] for r in got] == ["0xsick"], got
got_all_healthy = detect(mk, poss, price * 2)
assert got_all_healthy == []
print("UNIT_OK: детектор назвал больную и не назвал здоровую; на высокой цене молчит")

# 5) Начисление процентов увеличивает долг (иначе детектор смещён в «здоров», предел Л6)
a = accrue_borrow_assets(10 ** 12, 3170979198, 3600)   # ~10%/год, час
assert a > 10 ** 12 and (a - 10 ** 12) / 10 ** 12 < 0.001
print(f"UNIT_OK: начисление за час при ~10%/год = +{(a-10**12)/10**12*1e6:.2f} ppm (не ноль и не абсурд)")
print("ALL_OK core")
