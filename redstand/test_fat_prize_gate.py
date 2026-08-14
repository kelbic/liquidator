"""Гейт жира по призу («го» kelbic 07.08, дефект: FAT_DEBT мерял долг, окупаемость определяет
приз = долг × бонус). Инварианты: env unset -> долг-гейт бит-в-бит; рынок без бонуса -> фолбэк."""
import importlib, os, sys


def _fresh():
    sys.modules.pop("chain.spec_fire_live", None)
    return importlib.import_module("chain.spec_fire_live")


os.environ.pop("SPEC_FIRE_FAT_PRIZE_USD", None)
sfl = _fresh()
# 1. дефолт (env unset): приз-гейт выключен, bonus_of игнорируется — долг-гейт прежний
assert sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD)], bonus_of=lambda m: 0.1268)
assert not sfl._tgts_fat([("m", "b", 20000.0)], bonus_of=lambda m: 0.1268)
assert sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD)])   # старый контракт вызова жив
print("UNIT_OK: дефолт = долг-гейт бит-в-бит")

os.environ["SPEC_FIRE_FAT_PRIZE_USD"] = "2000"
sfl = _fresh()
# 2. 12.68%-рынок (cbXRP/AERO/stkWELL): долг $16k = приз $2029 -> жир (при долг-гейте не был)
assert sfl._tgts_fat([("m", "b", 16000.0)], bonus_of=lambda m: 0.1268)
# 3. cbBTC 4.39%: долг $45k = приз $1976 < $2000 -> НЕ жир; $46k = $2019 -> жир
assert not sfl._tgts_fat([("m", "b", 45000.0)], bonus_of=lambda m: 0.0439)
assert sfl._tgts_fat([("m", "b", 46000.0)], bonus_of=lambda m: 0.0439)
print("UNIT_OK: приз-гейт $2000 — 12.68%-жир с $16k, cbBTC с ~$46k")

# 4. рынок без бонуса в снапшоте (bonus=0) -> фолбэк на долг-гейт (fail-open к прежнему)
assert sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD)], bonus_of=lambda m: 0.0)
assert not sfl._tgts_fat([("m", "b", 30000.0)], bonus_of=lambda m: 0.0)
# 5. bonus_of бросил -> фолбэк, не падение окна
def _boom(m):
    raise RuntimeError("snapshot race")
assert sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD)], bonus_of=_boom)
# 6. bonus_of не передан (шоу-путь/старые вызовы) -> долг-гейт
assert sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD)])
assert not sfl._tgts_fat([])
print("UNIT_OK: фолбэки (нет бонуса / исключение / нет bonus_of) = долг-гейт")

os.environ.pop("SPEC_FIRE_FAT_PRIZE_USD", None)
sys.modules.pop("chain.spec_fire_live", None)
print("ПРОШЛИ: гейт жира по призу")
