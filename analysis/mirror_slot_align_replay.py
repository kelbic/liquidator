"""ПРИБОР ДЕФЕКТА (C): сколько НОГ переодевается на плотном потоке транзитов.

mirror_v2_replay.py считает СМЕНЫ СОСТАВА, но моделирует статику как СЫРОЙ кортеж
_PROBE_TIP_TIERS — то есть меряет не то, что носят ноги, а «портфель или константа».
Здесь спрашивается денежная величина: при каждой смене СКОЛЬКО ИНДЕКСОВ слотов сменило
значение (индекс -> 25% флота, 55 ног, пересборка лестницы 28-32с) и сколько из смен —
ЧИСТАЯ ПЕРЕСТАНОВКА того же множества (переодевание, не покупающее ничего).

Состав берётся ТЕМ ЖЕ вызовом, которым его берёт денежный путь probe_tip_for:
mirror_effective_slots(now=t) — а на базе (функции ещё нет) прежней моделью «портфель либо
статика», как и в приёмочном реплее.

ЗАПУСК: MIRROR_REPLAY_LOG=analysis/transit_tips_7d_dense.txt python3 analysis/mirror_slot_align_replay.py
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import chain.execute as ex                                     # noqa: E402

LOG = pathlib.Path(os.environ.get("MIRROR_REPLAY_LOG")
                   or (pathlib.Path(__file__).resolve().parent / "transit_tips_7d.txt"))
GRID_SEC = float(os.environ.get("MIRROR_REPLAY_GRID", "60"))
FLEET = 220


def rows():
    out = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ts, tip, agg = line.split()[:3]
        out.append((int(ts), int(tip), agg))
    return sorted(out)


_EFF = getattr(ex, "mirror_effective_slots", None)


def eff(t):
    if _EFF is not None:
        s = _EFF(now=t)
        return tuple(int(x) for x in s) if s else tuple(int(x) for x in ex._PROBE_TIP_TIERS)
    p = ex.transit_tip_portfolio(now=t)
    return tuple(int(x) for x in p) if p else tuple(int(x) for x in ex._PROBE_TIP_TIERS)


def reset():
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
    ex._MIRROR_APPLIED, ex._MIRROR_APPLIED_MODES = (), frozenset()
    ex._MIRROR_HOT_UNTIL = 0.0
    if hasattr(ex, "_MIRROR_SLOT_PREV"):
        ex._MIRROR_SLOT_PREV = ()
    ex._mirror_exp_reset()


class _Clock:
    """Подмена часов МОДУЛЯ execute (не глобальных): probe_tip_for берёт время сам, без
    параметра — иначе прибор мерил бы не боевую функцию, а её модель."""

    def __init__(self, real):
        self._real = real
        self.now = 0.0

    def time(self):
        return self.now

    def __getattr__(self, k):
        return getattr(self._real, k)


FLEET_ADDRS = ["0x" + ("%040x" % (i * 7919 + 13)) for i in range(FLEET)]


def _tips_now(clock, t):
    """ЧТО НЕСЁТ КАЖДАЯ ИЗ 220 НОГ — БОЕВОЙ функцией probe_tip_for, а не её моделью."""
    clock.now = t
    return [ex.probe_tip_for(a) for a in FLEET_ADDRS]


def main():
    data = rows()
    reset()
    t0, t1 = data[0][0], data[-1][0]
    days = (t1 - t0) / 86400.0
    i, moments, changes, perm, slots_moved, full = 0, 0, 0, 0, 0, 0
    prev, samples = None, []
    real_time = ex.time
    clock = _Clock(real_time)
    ex.time = clock
    legs_moved, prev_tips = 0, None
    t = t0
    while t <= t1:
        while i < len(data) and data[i][0] <= t:
            ex.note_transit_tip(data[i][1], data[i][2], now=data[i][0])
            i += 1
        cur = eff(t)
        moments += 1
        if prev is not None and cur != prev:
            changes += 1
            k = sum(1 for a, b in zip(prev, cur) if a != b)
            slots_moved += k
            if k == len(cur):
                full += 1
            if set(prev) == set(cur) and len(prev) == len(cur):
                perm += 1
                if len(samples) < 6:
                    samples.append((t, prev, cur))
            tips = _tips_now(clock, t)
            if prev_tips is not None:
                legs_moved += sum(1 for a, b in zip(prev_tips, tips) if a != b)
            prev_tips = tips
        elif prev_tips is None:
            prev_tips = _tips_now(clock, t)
        prev = cur
        t += GRID_SEC
    ex.time = real_time
    print("вход: %d транзитов, %.2f суток, сетка %.0fс, аксессор=%s"
          % (len(data), days, GRID_SEC, "mirror_effective_slots" if _EFF else "портфель-или-статика (база)"))
    print("моментов=%d  СМЕН=%d (%.1f/сутки)" % (moments, changes, changes / days))
    print("слотов/смену=%.2f  ног/смену=%.0f  ног ВСЕГО=%d"
          % (slots_moved / max(1, changes), FLEET * slots_moved / max(1, changes) / len(prev),
             FLEET * slots_moved / len(prev)))
    print("смен, трогающих ВСЕ %d слотов: %d (%.0f%%)"
          % (len(prev), full, 100.0 * full / max(1, changes)))
    print("смен ЧИСТОЙ ПЕРЕСТАНОВКОЙ того же множества: %d (%.0f%%) — переодевание задаром"
          % (perm, 100.0 * perm / max(1, changes)))
    print("НОГ ПЕРЕОДЕТО (probe_tip_for, флот %d): %d всего, %.0f на смену"
          % (FLEET, legs_moved, legs_moved / max(1, changes)))
    for t_, a, b in samples:
        print("   t=%d  %s -> %s" % (t_, list(a), list(b)))


main()
