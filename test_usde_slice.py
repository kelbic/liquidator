"""Юниты экономики среза USDe (28.07, проверка ревьюверских цифр). Без сети."""
import math
import sys

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from analysis.usde_watch import lif_of, slice_prize, heal_after, heal_days, SLICE_MAX_IMPACT

# 1) LIF из LLTV по формуле Morpho Blue (было зашито 2.5% — на 0.915 реально 2.617%)
lif = lif_of(int(0.915e18))
assert abs(lif - 1.02617) < 1e-5, lif
assert abs(lif_of(int(0.86e18)) - 1.04384) < 1e-5, lif_of(int(0.86e18))
assert lif_of(int(0.0e18)) == 1.15, "кап MAX_LIF"
print("UNIT_OK: LIF из LLTV (0.915 -> 2.617%%, 0.86 -> 4.384%%, кап 1.15)")

# 2) приз среза по ФАКТИЧЕСКОМУ импакту (замер 28.07: 0.22% на капе -> нетто ~$26.8k),
#    и консервативный пол по потолочному 1% (~$17.8k) — обе ветки живые
p = slice_prize(1_120_678, lif, 0.0022)
assert 26_000 < p < 27_500, p
p_floor = slice_prize(1_120_678, lif)
assert 17_000 < p_floor < 19_000, p_floor
assert slice_prize(1_120_678, 1.0, 0.0022) < 0, "нулевой бонус -> минус (импакт платим всегда)"
print("UNIT_OK: приз среза $%.1fk по факт-импакту 0.22%% (пол по потолку 1%% = $%.1fk)"
      % (p / 1e3, p_floor / 1e3))

# 3) ЛЕЧЕНИЕ: LIF*LLTV < 1 => после среза HF > 1 (ключевой факт, ревьювер его упустил)
h = heal_after(1_120_678, 20_000_000.0, lif, 0.915)
assert h > 0, "ликвидация ОБЯЗАНА лечить позицию при LIF*LLTV<1"
assert abs(h - 0.00362) < 5e-4, h
d = heal_days(1_120_678, 20_000_000.0, lif, 0.915, 0.0359)
assert 30 < d < 45, d
print("UNIT_OK: срез $1.12M лечит кита $20M на +%.4f HF = %.0f дней до след. флипа" % (h, d))

# 4) масштаб: чем меньше срез, тем короче пауза (линейно) — основа тактики «дробить»
d_small = heal_days(250_000, 20_000_000.0, lif, 0.915, 0.0359)
assert 6 < d_small < 10, d_small
assert d / d_small > 3.5, "пауза обязана расти с размером среза"
print("UNIT_OK: срез $250k -> пауза %.1f дн (дробление ускоряет повтор, но приз меньше)" % d_small)

# 5) страховка от регрессии: срез больше долга не ломает формулы
assert heal_after(30_000_000, 20_000_000.0, lif, 0.915) == 0.0
assert heal_days(30_000_000, 20_000_000.0, lif, 0.915, 0.0359) == 0.0
print("UNIT_OK: срез >= долга — формулы не падают")

print("=== ВСЕ USDE-SLICE ЮНИТЫ ПРОШЛИ ===")
