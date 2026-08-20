"""Пейдж квоты — по скользящей БЕЗ бут-прогрева (20.08, 2-й заход по ложным пейджам).

Спан-гейт откладывал пейдж ровно до первого тика с достаточным спаном, но окно в тот момент
всё ещё СОДЕРЖАЛО рестартный прогрев — пейдж стрелял через ~50 мин после КАЖДОГО рестарта
(16:27:53 — оба ключа ложно). Канон Ш23а: насыщенный числитель чинится числителем.
Аптайм-защёлка отвергнута 07.08 (частые рестарты глушили сторожа) — здесь рестарт выкидывает
только СВОЙ прогрев: устойчивый расход виден с 16-й минуты.

Проверяется чистая функция cu_rolling_ex_boot + гард дрейфа (боевой блок hotpath реально
судит пейдж по ней, а не по полной скользящей — иначе фикс стоит этажом выше дефекта).
"""
import sys
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.rpc as rpc  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


H = "test-host#0000"


def _seed(samples):
    with rpc._CU_ROLL_LOCK:
        rpc._CU_ROLL[H] = list(samples)


# точка старта процесса, от которой считается прогрев (burst = 900с по умолчанию)
T0 = rpc._CU_PROC_T0

# ---- 1. Замеры прогрева исключены, пост-прогрев считается ----
# 5 окон по 120с в прогреве (жирные: 60k CU) + 5 окон по 120с после (тонкие: 600 CU)
burst = [(T0 + 100 + i * 120, 60000.0, 120.0) for i in range(5)]
calm = [(T0 + 1000 + i * 120, 600.0, 120.0) for i in range(5)]
_seed(burst + calm)
proj_full, span_full = rpc.cu_rolling_add(H, 0.0, 0.0)     # полная скользящая (добавка нулевая)
proj_pg, span_pg = rpc.cu_rolling_ex_boot(H)
check("полная скользящая заражена прогревом (для контраста)", proj_full > 100, proj_full)
check("вид без прогрева считает только штиль (~13M)", 10 < proj_pg < 16, proj_pg)
check("спан без прогрева = только пост-бут окна (600с)", span_pg == 600.0, span_pg)

# ---- 2. Только прогрев -> (0,0): пейджу не с чего судить, молчание ----
_seed(burst)
proj_pg, span_pg = rpc.cu_rolling_ex_boot(H)
check("одни замеры прогрева -> (0.0, 0.0)", (proj_pg, span_pg) == (0.0, 0.0), (proj_pg, span_pg))

# ---- 3. Пустая скользящая -> (0,0), не падение ----
_seed([])
check("пусто -> (0.0, 0.0)", rpc.cu_rolling_ex_boot(H) == (0.0, 0.0))

# ---- 4. Устойчивый ЖИРНЫЙ расход после прогрева виден (сторож не ослеп) ----
_seed([(T0 + 1000 + i * 120, 60000.0, 120.0) for i in range(5)])
proj_pg, span_pg = rpc.cu_rolling_ex_boot(H)
check("реальный перерасход после прогрева виден (>1000M)", proj_pg > 1000, proj_pg)

# ---- 5. Гард дрейфа: боевой пейдж в hotpath судит по ex_boot, текст — тем же числом ----
import chain.hotpath as hp  # noqa: E402
_SRC = open(hp.__file__.replace(".pyc", ".py")).read()
for _frag in ("cu_rolling_ex_boot", "_roll_pg[0] >= CU_ALERT_PROJ_M",
              "_roll_pg[1] >= CU_ALERT_PAGE_MIN_SPAN_SEC"):
    check("боевой блок содержит %r" % _frag, _frag in _SRC)
check("текст пейджа несёт число БЕЗ прогрева (_roll_pg[0]), не полное",
      "% (_host, _roll_pg[0], CU_QUOTA_M" in _SRC)

with rpc._CU_ROLL_LOCK:
    rpc._CU_ROLL.pop(H, None)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS else "OK: пейдж квоты слеп к бут-прогреву, зряч к устойчивому расходу"))
sys.exit(1 if FAILS else 0)
