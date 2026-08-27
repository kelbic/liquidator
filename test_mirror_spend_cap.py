"""ПОРТФЕЛЬ ЗЕРКАЛА: ДВЕ ЗОНЫ, Σ-КАП, ШТОРМ, АНТИФЛАП (слепой дизайн 27.08, STATE 79bd9dd).

Замер: после снятия входного капа зеркало за утро собрало [15.86M,10M,5M,1.0] = $10.94/окно
при 2/10 точных попаданий против 3/10 у пола-4 за $1.56; 520 смен состава/сутки.

Приёмка:
(а) дорогих слотов не больше EXPENSIVE_SLOTS (1) при любом числе дорогих классов;
(б) Σ-кап: дорогой класс, не укладывающийся в SUM_CAP, входит ТОЛЬКО штормом;
(в) шторм-гейт ПО ФЛОТУ: 8 сэмплов 28-34M с ДВУХ агрегаторов по 4 (реплика 19.08) — берётся;
    4 сэмпла у одного — нет (путь «возраст» для дорогого закрыт);
(г) полоса пола — точные значения без склейки;
(д) антифлап: dwell — дорогой держится DWELL после входа; gap — замена не чаще GAP и с маржой;
(е) стабильные индексы: смена одного класса не сдвигает остальные;
(ж) статика по умолчанию = пол-4 бит-в-бит с «только пол» (переход статика↔слоты не переодевает);
(з) КРАСНЫЙ-НА-БАЗЕ: прежнее правило (отбор по весу без зон) пустило бы 3 дорогих из 4.
"""
import sys, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
FAILS = []
def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok: FAILS.append(name)
import chain.execute as ex  # noqa: E402
NOW = 1_787_300_000.0
def reset():
    with ex._TRANSIT_TIPS_LOCK: ex._TRANSIT_TIPS.clear()
    ex._MIRROR_APPLIED, ex._MIRROR_APPLIED_MODES = (), frozenset()
    ex._MIRROR_HOT_UNTIL = 0.0; ex._mirror_exp_reset()
def feed(tip, agg, n=8, t0=NOW, step=1.0):
    for k in range(n): ex.note_transit_tip(tip, agg, now=t0 + k * step)
P = lambda t=NOW + 20: ex.transit_tip_portfolio(now=t)  # noqa: E731

# (а) четыре дорогих класса с уликой -> ровно один дорогой слот, пол занимает три
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9)
for i, w in enumerate((30_000_000, 20_000_000, 12_000_000, 6_000_000)): feed(w, "0xd%d" % i, 8)
g = P()
check("(а) дорогих слотов ровно 1 при четырёх дорогих классах", sum(1 for x in g if x >= ex._MIRROR_EXP_MIN) == 1, g)
check("(а) пол занял три слота точными значениями", sorted(x for x in g if x < ex._MIRROR_EXP_MIN) == [1_000_000, 1_100_000, 1_150_000], g)
check("(а) при равной улике выбран класс В Σ-капе (6M), не шторм (30M)", 6_000_000 in g, g)

# (б) Σ-кап: 10M при поле 3.25M = 13.25 > 12M -> только шторм; шторм есть (8 свежих) -> входит; бюджет выбран -> нет
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9)
feed(10_000_000, "0xa", 8)
g = P()
check("(б) 10M поверх пола 3.25M > Σ-кап 12M входит ШТОРМОМ (свежих 8 >= %d)" % ex._MIRROR_EXP_N, 10_000_000 in g and ex._MIRROR_EXP["storm"], (g, ex._MIRROR_EXP["storm"]))
ex._MIRROR_EXP["storm_used"] = ex._MIRROR_STORM_SEC_PER_DAY + 1; ex._MIRROR_EXP["cls"] = None
ex._MIRROR_EXP["last_change"] = 0.0
g2 = P(NOW + 25)
check("(б) шторм-бюджет суток выбран -> класс выше Σ-капа НЕ входит", 10_000_000 not in g2, g2)
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9); feed(8_000_000, "0xa", 8)
check("(б) 8M поверх пола 3.25M = 11.25 <= 12M входит БЕЗ шторма", 8_000_000 in P() and not ex._MIRROR_EXP["storm"], (P(), ex._MIRROR_EXP["storm"]))

# (в) шторм по флоту: реплика 19.08 — по 4 сэмпла 28-34M с двух агрегаторов за 145с
reset(); feed(1_000_000, "0xf0", 12)
for k, (w, agg) in enumerate(((28_000_000, "0x0e"), (31_241_909, "0x1e"), (33_429_565, "0x0e"), (33_429_565, "0x1e"),
                             (33_429_565, "0x0e"), (34_020_542, "0x1e"), (28_949_119, "0x0e"), (30_521_342, "0x1e"))):
    ex.note_transit_tip(w, agg, now=NOW + (12, 24, 52, 64, 80, 88, 133, 145)[k])
g = P(NOW + 150)
check("(в) реплика затора 19.08 (4+4 с двух агрегаторов за 145с) ВЗЯТА штормом", any(x >= 28_000_000 for x in g), g)
reset(); feed(1_000_000, "0xf0", 12); feed(33_400_000, "0x1e", 4)
g = P(NOW + ex._MIRROR_ENTRY_AGE_SEC + 10)
check("(в) 4 сэмпла у одного агрегатора даже после выдержки возраста — НЕ берутся", not any(x >= 28_000_000 for x in g), g)

# (г) пол без склейки
reset()
for i, w in enumerate((1_000_000, 1_050_000, 1_100_000, 1_150_000)): feed(w, "0xf%d" % i, 8)
check("(г) полоса пола — точные значения, 4 слота = 4 разных значения", sorted(P()) == [1_000_000, 1_050_000, 1_100_000, 1_150_000], P())

# (д) антифлап: dwell и gap/маржа
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9); feed(6_000_000, "0xa", 8)
g1 = P()
feed(7_000_000, "0xb", 9, t0=NOW + 30)                      # конкурент дорогого с +1 уликой
g2 = P(NOW + 60)
check("(д) dwell: дорогой 6M держится, 7M с +1 уликой его не сменяет", 6_000_000 in g2 and 7_000_000 not in g2, g2)
# после dwell: 6M ещё жив (>=4 свежих за 600с), 7M набрал улику с маржой +2 в свежем окне
feed(6_000_000, "0xa", 4, t0=NOW + ex._MIRROR_EXP_DWELL_SEC - 200)
feed(7_000_000, "0xb", 12, t0=NOW + ex._MIRROR_EXP_DWELL_SEC - 100)
g3 = P(NOW + ex._MIRROR_EXP_DWELL_SEC + 100)
check("(д) после dwell и с маржой +2 замена дорогого дорогим проходит", 7_000_000 in g3 and 6_000_000 not in g3, g3)
# выход: улика дорогого протухла (>600с) -> слот отдан полу
g4 = P(NOW + ex._MIRROR_EXP_DWELL_SEC + 2 * ex._MIRROR_EXP_FRESH_SEC + 200)
check("(д) выход: без свежей улики дорогой слот уходит, портфель = пол", all(x < ex._MIRROR_EXP_MIN for x in g4), g4)

# (е) стабильные индексы
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9); feed(6_000_000, "0xa", 8)
g1 = P(); idx_floor = {v: i for i, v in enumerate(g1) if v < ex._MIRROR_EXP_MIN}
# улика пола и нового дорогого — ЖИВАЯ у момента проверки (TTL 900с), 6M к тому времени протух
_t = NOW + ex._MIRROR_EXP_DWELL_SEC + 100
feed(1_000_000, "0xf0", 12, t0=_t); feed(1_100_000, "0xf1", 10, t0=_t); feed(1_150_000, "0xf2", 9, t0=_t)
feed(7_000_000, "0xb", 12, t0=_t)
g2 = P(_t + 50)
check("(е) смена дорогого класса НЕ сдвигает индексы пола", all(g2[i] == v for v, i in idx_floor.items()), (g1, g2))

# (ж) статика = пол-4 и равна составу «только пол»
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9); feed(1_300_000, "0xf3", 9)
check("(ж) статика по умолчанию = пол-4", tuple(ex._PROBE_TIP_TIERS) == (1_000_000, 1_100_000, 1_150_000, 1_300_000), ex._PROBE_TIP_TIERS)
check("(ж) состав «только пол» бит-в-бит равен статике (переход не переодевает ноги)", sorted(P()) == sorted(ex._PROBE_TIP_TIERS), P())

# (з) КРАСНЫЙ-НА-БАЗЕ: прежний отбор (топ-4 по весу без зон) на фикстуре (а)
reset(); feed(1_000_000, "0xf0", 12); feed(1_100_000, "0xf1", 10); feed(1_150_000, "0xf2", 9)
for i, w in enumerate((30_000_000, 20_000_000, 12_000_000, 6_000_000)): feed(w, "0xd%d" % i, 8)
with ex._TRANSIT_TIPS_LOCK:
    rings = {k: [(ts, w) for ts, w in v] for k, v in ex._TRANSIT_TIPS.items()}
cands = [c for c in (ex._agg_mode(f) for f in rings.values()) if c is not None]
legacy = [g[0] for g in sorted(ex._glue_modes(cands), key=lambda g: (-g[1], -g[0]))[:4]]
check("(з) КРАСНЫЙ-НА-БАЗЕ: прежний отбор пустил бы >= 2 дорогих из четырёх", sum(1 for x in legacy if x >= ex._MIRROR_EXP_MIN) >= 2, legacy)

_S = open("chain/execute.py").read()
check("гард: правила записаны в коде (две зоны, шторм по флоту, стабильные индексы)",
      "_MIRROR_EXP_N" in _S and "СТАБИЛЬНЫЕ ИНДЕКСЫ" in _S and "ПО ФЛОТУ" in _S)
print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS else "OK: портфель — пол точными значениями, дорогой ≤1 под Σ-капом/штормом, без флапа"))
sys.exit(1 if FAILS else 0)
