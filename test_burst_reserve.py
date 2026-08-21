"""БЁРСТ-В-ЩЕЛЬ (20.08): резерв ведра пейсера под момент транзита.

ЗАМЕР, породивший пакет (51 блок с вердиктом): чужих впереди в полосе медиана 1 — толпы нет,
а наш ПЕРВЫЙ tx приходит на медиану +12 от транзита. Залп движка (Ш29) выдаёт 24 жетона
мимо СВОЕГО ведра, но ниже стоит пейсер эндпоинта, к транзиту пустой по построению — залп
размазывался капельницей 110/с на весь суб-блок.

Проверяется:
  * обычная отправка НЕ трогает резерв (ведро ниже 1+reserve -> pacer_dry);
  * залповая отправка ИЗ резерва проходит и считается (urgent_sent);
  * мгновенная ёмкость залпа = 3 канала x RESERVE (флаг держится на ВЕСЬ обход каналов —
    первая редакция снимала его на первом канале, ёмкость была бы 1x);
  * квота канала за секунду НЕ превышена (резерв меняет РАСПРЕДЕЛЕНИЕ, не расход);
  * reserve клампится к burst-1 (ручка больше ведра не запирает канал навсегда);
  * пометка одноразовая: следующая проба того же треда — обычная;
  * reserve=0 -> поведение прежнее посимвольно.
"""
import sys
import time
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.execute as ex  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


def reset():
    with ex._PACER_LOCK:
        ex._PACER_BUCKETS.clear()
        ex._PACER_URGENT_SENT.clear()
    ex._urgent_take_flag()          # снять возможную пометку


# ---- 1. Обычная отправка не трогает резерв ----
reset()
CH, RATE, BURST, RES = "chan|A", 110.0, 30.0, 12.0
took = 0
while ex._pacer_take(CH, RATE, BURST, reserve=RES):
    took += 1
    if took > 100:
        break
check("обычный поток выбирает ведро ДО резерва (30-12=18)", took == 18, took)
check("следующая обычная — сухо", not ex._pacer_take(CH, RATE, BURST, reserve=RES))

# ---- 2. Залповая берёт из резерва ----
urg = 0
while ex._pacer_take(CH, RATE, BURST, reserve=RES, urgent=True):
    urg += 1
    if urg > 100:
        break
check("залп добирает резерв (12)", urg == 12, urg)
check("счётчик urgent_sent = 12", ex.pacer_urgent_stats().get(CH) == 12,
      ex.pacer_urgent_stats())
check("после резерва сухо и залпу", not ex._pacer_take(CH, RATE, BURST, reserve=RES, urgent=True))

# ---- 3. Мгновенная ёмкость залпа = 3 канала x резерв (>= залпа движка 24) ----
reset()
chans = ["chan|IP1", "chan|IP2", "chan|IP3"]
for c in chans:                                  # ровный поток осушил ведро до резерва
    while ex._pacer_take(c, RATE, BURST, reserve=RES):
        pass
burst_capacity = 0
for c in chans:
    while ex._pacer_take(c, RATE, BURST, reserve=RES, urgent=True):
        burst_capacity += 1
check("мгновенная ёмкость залпа 3x12=36 >= 24 жетонов движка",
      burst_capacity == 36, burst_capacity)

# ---- 4. Квота за секунду НЕ превышена: резерв меняет распределение, не расход ----
reset()
C = "chan|quota"
with ex._PACER_LOCK:                             # ведро пустое, начало окна
    ex._PACER_BUCKETS[C] = [0.0, time.monotonic() - 1.0]   # накопило ровно секунду
n = 0
while ex._pacer_take(C, RATE, BURST, reserve=RES, urgent=True):
    n += 1
    if n > 500:
        break
check("за секунду накопления канал отдал не больше burst (30)", n <= BURST, n)

# ---- 5. Резерв больше ведра не запирает канал ----
reset()
check("reserve=999 клампится: обычная отправка всё равно проходит",
      ex._pacer_take("chan|clamp", RATE, BURST, reserve=999.0))

# ---- 6. Пометка треда одноразовая ----
ex.pacer_mark_urgent()
check("пометка снята первым чтением", ex._urgent_take_flag() is True)
check("второе чтение — уже обычная отправка", ex._urgent_take_flag() is False)

# ---- 7. reserve=0 -> прежнее поведение посимвольно ----
reset()
C0 = "chan|legacy"
took0 = 0
while ex._pacer_take(C0, RATE, BURST):           # без reserve/urgent — как в HEAD
    took0 += 1
    if took0 > 100:
        break
check("без резерва ведро отдаёт ровно burst (30)", took0 == 30, took0)

# ---- 8. Гард дрейфа: флаг держится на ВЕСЬ обход каналов ----
_SRC = open(ex.__file__.replace(".pyc", ".py")).read()
check("флаг снимается ОДИН раз на пробу (_urg перед обходом)",
      "_urg = _urgent_take_flag()" in _SRC)
for frag in ("source_ip=_ip, urgent=_urg", "urgent=_urg)"):
    check("канал получает флаг: %r" % frag, frag in _SRC)
import chain.fire_engine as fe  # noqa: E402
_SRC_FE = open(fe.__file__.replace(".pyc", ".py")).read()
check("движок метит тред ровно на жетоне залпа",
      "pacer_mark_urgent()" in _SRC_FE and "sent_burst" in _SRC_FE)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: бёрст-в-щель — резерв копится в тишине, тратится в щели, квота цела"))
sys.exit(1 if FAILS else 0)
