"""Г2д-3/#132: немота флота ПО БЮДЖЕТУ тира обязана иметь ЧИСЛО.

Замер линзы 16.08: общий бакет «суточный факт + бронь в полёте» гасит пробу БЕЗ счётчика и без
лога — при базе 0.02 gwei и $50 суточных флот немеет после 117 проб (1.1 обхода из 110), при
$100 — после 59, то есть ПОЛОВИНА флота молчит. Каскад = всплеск базы, значит потолок сжимается
ровно в решающий момент, и до этой правки отказ был неотличим от «склад пуст» (skipped_dry).

Стенд гоняет НАСТОЯЩИЙ `send_presigned_probe` (не реплику гейта) и требует, чтобы:
  * при выбранном бакете проба не ушла И счётчик `reserve_skips` вырос;
  * причина «tx в полёте» считалась ОТДЕЛЬНО (иначе два разных диагноза снова сольются);
  * счётчик рос ровно на число отказов (не «примерно»);
  * проводка в журнал существовала — имя счётчика есть в форматной строке боевого модуля.

Красный-на-базе: до правки `volley_budget_skips` не существует вовсе (ImportError), то есть
числа нет — что и было дефектом.
"""
import sys

sys.path.insert(0, ".")

from chain import execute as ex   # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


def _reset(gas_usd, reserved, inflight):
    ex._VOLLEY_GAS_USD = gas_usd
    ex._VOLLEY_GAS_RESERVED = reserved
    ex._INFLIGHT = inflight
    ex._VOLLEY_RESERVE_SKIPS = 0
    ex._VOLLEY_INFLIGHT_SKIPS = 0


# --- 1. БАКЕТ ВЫБРАН: проба не уходит, и это ВИДНО числом -------------------------------------
_reset(ex.CASCADE_TIER_GAS_USD + 1.0, 0.0, 0)
addr = "0x" + "a1" * 20
res = [ex.send_presigned_probe(None, None, addr) for _ in range(7)]
rs, ifs = ex.volley_budget_skips()
check("бакет выбран: ни одна проба не ушла", all(r is None for r in res), res[:2])
check("счётчик reserve_skips вырос РОВНО на число отказов", rs == 7, "rs=%d" % rs)
check("причина 'в полёте' НЕ смешана с бюджетной", ifs == 0, "ifs=%d" % ifs)

# --- 2. ПРИЧИНА «tx В ПОЛЁТЕ» СЧИТАЕТСЯ ОТДЕЛЬНО ----------------------------------------------
_reset(0.0, 0.0, 1)
if getattr(ex, "_ENGINE_PROBE_IGNORE_INFLIGHT", False):
    ex._ENGINE_PROBE_IGNORE_INFLIGHT = False
res2 = [ex.send_presigned_probe(None, None, addr) for _ in range(3)]
rs2, ifs2 = ex.volley_budget_skips()
check("в полёте: пробы не ушли", all(r is None for r in res2))
check("счётчик inflight_skips вырос, бюджетный НЕ вырос", ifs2 == 3 and rs2 == 0,
      "rs=%d ifs=%d" % (rs2, ifs2))

# --- 3. ЗДОРОВЫЙ СЛУЧАЙ: бакет свободен => бюджетный счётчик НЕ растёт -------------------------
# (проба всё равно не уйдёт — склада нет, — но причина ДРУГАЯ, и это главное: диагнозы разведены)
_reset(0.0, 0.0, 0)
ex.send_presigned_probe(None, None, addr)
rs3, ifs3 = ex.volley_budget_skips()
check("свободный бакет: бюджетный счётчик не растёт (отказ по складу — другой диагноз)",
      rs3 == 0 and ifs3 == 0, "rs=%d ifs=%d" % (rs3, ifs3))

# --- 4. ПРОВОДКА В ЖУРНАЛ: число обязано печататься, иначе прибора для человека нет ------------
src = open("chain/spec_fire_live.py").read()
check("проводка: имя счётчика есть в форматной строке окна",
      "budget_reserve_skips=%d" in src and "volley_budget_skips" in src)
check("проводка: значение подставляется в ту же строку", "_rs, _ifs," in src)

print("=" * 60)
if FAILS:
    print("ПРОВАЛ: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("OK: немота по бюджету тира имеет число и доезжает до журнала")
