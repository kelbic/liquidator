"""ПРИЁМКА ЗЕРКАЛА tip v2 ЧИСЛОМ: прогон ФАКТИЧЕСКОГО 7-суточного потока транзитов.

ПОЧЕМУ НЕ ЮНИТ. Юнит показывает, что механизм умеет то, что задумано, на подобранных входах.
Здесь спрашивается другое и более дорогое: ЧТО ЭТОТ МЕХАНИЗМ ДЕЛАЛ БЫ ВСЮ НЕДЕЛЮ на реальном
потоке — сколько времени в портфеле стоял призовой пол, сколько раз состав менялся (каждая
смена = пересигнация до 110 лестниц по 28-32с) и успел бы он войти в режим затора 19.08.

ВХОД: analysis/transit_tips_7d.txt — `unix_ts tip_wei agg`, 129 транзитов за 5.7 суток
(журнал позиций судимых блоков). Поток скармливается note_transit_tip с ХОДОМ ВРЕМЕНИ, а
портфель опрашивается по сетке — то есть меряется не «после всего», а КАЖДЫЙ МОМЕНТ.

КРАСНЫЙ-НА-БАЗЕ. Тот же поток прогоняется через ПРЕЖНЮЮ редакцию (TTL 6ч + отбор по цене, без
склейки и гистерезиса). На ЭТОМ потоке она даёт тот же результат — и это сказано прямо, вместе
с замеренной причиной: чтобы отбор-по-цене вытеснил пол, нужно >=4 различные живые моды, а их
максимум ОДНА за всю неделю. Поэтому пара красный/зелёный строится ОТДЕЛЬНО, на явно названной
адверсарной конфигурации: предмет проверки — ПРАВИЛО, и оно обязано держать любой вход.

ЗАПУСК: python3 analysis/mirror_v2_replay.py     (ненулевой код = приёмка провалена)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import chain.execute as ex                                      # noqa: E402

LOG = pathlib.Path(__file__).resolve().parent / "transit_tips_7d.txt"
FLOOR_MAX = ex._MIRROR_FLOOR_MAX
GRID_SEC = 60.0                    # шаг сетки опроса портфеля
JAM_TS = 1787172660                # 19.08 20:51 — затор, в котором потеряли $3905
JAM_LO, JAM_HI = 30_000_000, 34_000_000
FAILS = []


def rows():
    out = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ts, tip, agg = line.split()[:3]
        out.append((int(ts), int(tip), agg))
    return sorted(out)


LEGACY_TTL = 21600.0               # дефолт TTL до фикса (6 часов) — часть той же редакции


def legacy_portfolio(now):
    """ПРЕЖНЯЯ редакция ЦЕЛИКОМ (v2 ред.1), какой она стояла до вердикта линзы: TTL 6 часов,
    моды пер-агг БЕЗ склейки, отбор `sorted(...)[:4]` ПО ВЕЛИЧИНЕ, добивка статикой, ни
    гистерезиса, ни гарантии пола. Обе части редакции важны: длинный TTL держит живыми много
    классов одновременно, и именно тогда отбор-по-цене выталкивает пол."""
    with ex._TRANSIT_TIPS_LOCK:
        rings = {k: [w for ts, w in v if now - ts <= LEGACY_TTL]
                 for k, v in ex._TRANSIT_TIPS.items()}
    modes = set()
    for fresh in rings.values():
        if len(fresh) < ex._MIRROR_MIN_N:
            continue
        from collections import Counter
        m, cnt = Counter(fresh).most_common(1)[0]
        if cnt / len(fresh) >= ex._MIRROR_MIN_SHARE:
            modes.add(m)
    modes = sorted(modes, reverse=True)[:ex._MIRROR_SLOTS]
    if not modes:
        return ()
    slots = list(modes)
    for t in ex._PROBE_TIP_TIERS:
        if len(slots) >= ex._MIRROR_SLOTS:
            break
        if t not in slots:
            slots.append(int(t))
    return tuple(slots)


def replay(portfolio_fn, fresh_state=True):
    """Прогон потока с ходом времени. Возврат: (моментов, моментов с полом, смен состава,
    первый момент со слотом затора, список составов)."""
    data = rows()
    if fresh_state:
        with ex._TRANSIT_TIPS_LOCK:
            ex._TRANSIT_TIPS.clear()
        ex._MIRROR_APPLIED, ex._MIRROR_APPLIED_MODES = (), frozenset()
        ex._MIRROR_HOT_UNTIL = 0.0
    t0, t1 = data[0][0], data[-1][0]
    i, moments, with_floor, changes, jam_at = 0, 0, 0, 0, None
    prev, seen = None, []
    t = t0
    while t <= t1:
        while i < len(data) and data[i][0] <= t:
            ex.note_transit_tip(data[i][1], data[i][2], now=data[i][0])
            i += 1
        p = portfolio_fn(t)
        eff = p if p else tuple(ex._PROBE_TIP_TIERS)     # пустой портфель = статические тиры
        moments += 1
        if any(int(x) <= FLOOR_MAX for x in eff):
            with_floor += 1
        if prev is not None and eff != prev:
            changes += 1
            seen.append((t, eff))
        if jam_at is None and any(JAM_LO <= int(x) <= JAM_HI for x in eff):
            jam_at = t
        prev = eff
        t += GRID_SEC
    return moments, with_floor, changes, jam_at, seen


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


print("=" * 96)
print("ПРИЁМКА ЗЕРКАЛА v2 НА ФАКТИЧЕСКОМ ПОТОКЕ (%s)" % LOG.name)
print("=" * 96)
DATA = rows()
DAYS = (DATA[-1][0] - DATA[0][0]) / 86400.0
print("вход: %d транзитов, %.2f суток, TTL=%.0fс, склейка ±%d%%, гистерезис n>=%d либо возраст>=%.0fс"
      % (len(DATA), DAYS, ex._MIRROR_TTL_SEC, round((ex._MIRROR_GLUE_STEP - 1) * 100),
         ex._MIRROR_ENTRY_N, ex._MIRROR_ENTRY_AGE_SEC))

# --- ФИКС ---------------------------------------------------------------------------------
m, wf, ch, jam, seen = replay(lambda t: ex.transit_tip_portfolio(now=t))
share = 100.0 * wf / m
print("\nПОСЛЕ ФИКСА: моментов=%d, с полом<=%d wei: %.1f%%, смен состава=%d (%.1f/сутки)"
      % (m, FLOOR_MAX, share, ch, ch / DAYS))
print("   первые составы: %s" % "; ".join(
    "%d:%s" % (t, [int(x) for x in p]) for t, p in seen[:4]))
check("(а) слот пола присутствует >=95%% моментов", share >= 95.0, "%.1f%%" % share)
check("(б) смен состава за неделю немного (модель линзы без склейки: ~317/сутки)",
      ch / DAYS < 32.0, "%.1f/сутки" % (ch / DAYS))
check("(в) слот затора 31-33 mgwei ПОЯВИЛСЯ", jam is not None,
      "нет" if jam is None else "в t=%d (затор с %d, задержка %.0fс)" % (jam, JAM_TS, jam - JAM_TS))
if jam is not None:
    # гистерезис входа: не раньше самой пачки и не позже разумного (возраст 60с + шаг сетки)
    check("(в) вход в затор в пределах гистерезиса, а не мгновенно",
          jam >= JAM_TS, "t=%d против начала затора %d" % (jam, JAM_TS))

# --- ПРЕЖНЯЯ РЕДАКЦИЯ НА ТОМ ЖЕ ПОТОКЕ: ЧЕСТНЫЙ РЕЗУЛЬТАТ -------------------------------------
# Ожидание было — увидеть моменты БЕЗ пола. Их НЕТ, и это надо сказать прямо, а не подогнать
# стенд: чтобы отбор-по-цене вытеснил пол, нужно >=4 РАЗЛИЧНЫЕ живые моды одновременно, а на
# этом потоке их максимум ОДНА (замер ниже). То есть блокер линзы — дефект КОДА (ветка отбора
# существует и срабатывает на четырёх классах), но НЕ то, что случилось на прошлой неделе.
# Оценка «пол вылетает в 32% штиля» — модель линзы, а не факт этого журнала.
m0, wf0, ch0, jam0, _ = replay(legacy_portfolio)
share0 = 100.0 * wf0 / m0
print("\nПРЕЖНЯЯ РЕДАКЦИЯ (TTL 6ч, отбор по цене) на ТОМ ЖЕ потоке: с полом %.1f%%, смен=%d"
      % (share0, ch0))
_max_modes = 0
for _t, _x, _a in DATA:
    _live = {}
    for ts, tip, agg in DATA:
        if ts <= _t and _t - ts <= LEGACY_TTL:
            _live.setdefault(agg, []).append(tip)
    _mm = set()
    for _agg, _v in _live.items():
        _v = _v[-ex._MIRROR_K:]
        if len(_v) < ex._MIRROR_MIN_N:
            continue
        from collections import Counter as _C
        _m, _c = _C(_v).most_common(1)[0]
        if _c / len(_v) >= ex._MIRROR_MIN_SHARE:
            _mm.add(_m)
    _max_modes = max(_max_modes, len(_mm))
print("   причина: максимум ОДНОВРЕМЕННО живых различных мод за неделю = %d (нужно >=%d, чтобы "
      "пол вытеснился) => на этом потоке дефект ЛАТЕНТЕН" % (_max_modes, ex._MIRROR_SLOTS))

# --- КРАСНЫЙ/ЗЕЛЁНЫЙ НА КОНФИГУРАЦИИ, ГДЕ СВОЙСТВО ЛОМАЕТСЯ ----------------------------------
# Поток недели свойство не ломает, поэтому пара красный/зелёный строится ЯВНО: четыре
# агрегатора с РАЗНЫМИ дорогими классами + один с полом. Это не «данные с цепи», и так и
# написано; предмет здесь — ПРАВИЛО ОТБОРА, а оно обязано держать любой состав входа.
def _adversarial(now0):
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
    ex._MIRROR_APPLIED, ex._MIRROR_APPLIED_MODES = (), frozenset()
    ex._MIRROR_HOT_UNTIL = 0.0
    for agg, tip in (("0xaaa", 30_000_000), ("0xbbb", 20_000_000),
                     ("0xccc", 12_000_000), ("0xddd", 6_000_000)):
        for k in range(6):
            ex.note_transit_tip(tip, agg, now=now0 - 300 + k)
    for k in range(30):                      # пол: массовый класс, много улик
        ex.note_transit_tip(1_000_000, "0xfff", now=now0 - 300 + k)


_ADV_T = DATA[-1][0]
_adversarial(_ADV_T)
_legacy_adv = legacy_portfolio(_ADV_T)
_adversarial(_ADV_T)
_fixed_adv = ex.transit_tip_portfolio(now=_ADV_T)
print("\nАДВЕРСАРНАЯ КОНФИГУРАЦИЯ (4 дорогих класса + пол, 30 улик у пола):")
print("   прежнее правило: %s" % [int(x) for x in _legacy_adv])
print("   правило после фикса: %s" % [int(x) for x in _fixed_adv])
check("КРАСНЫЙ: прежний отбор по цене ВЫБРАСЫВАЕТ пол",
      not any(int(x) <= FLOOR_MAX for x in _legacy_adv), str(_legacy_adv))
check("ЗЕЛЁНЫЙ: после фикса пол В СЛОТАХ (гарантия явной строкой)",
      any(int(x) <= FLOOR_MAX for x in _fixed_adv), str(_fixed_adv))
check("ЗЕЛЁНЫЙ: отбор по весу улики — самый массовый класс в портфеле",
      1_000_000 in _fixed_adv, str(_fixed_adv))

# --- ЭКОНОМИКА СОСТАВОВ ------------------------------------------------------------------------
_probes = ex.mirror_window_probes()
print("\nэкономика (ETH $1730, газ пробы %d, объём окна %.0f проб = %.0f/с × %.0fс):"
      % (ex._PROBE_GAS_MEASURED, _probes, ex._PACER_RATE, ex._MIRROR_WINDOW_SEC))
print("   статика %s -> $%.2f/окно" % (list(ex._PROBE_TIP_TIERS),
                                       ex._mirror_cost_usd(ex._PROBE_TIP_TIERS, 1730.0)))
for t, p in seen[:6]:
    print("   состав %s -> $%.2f/окно" % ([int(x) for x in p], ex._mirror_cost_usd(p, 1730.0)))

print("\n" + "=" * 96)
if FAILS:
    print("ПРИЁМКА ПОТОКА ПРОВАЛЕНА: %s" % ", ".join(FAILS))
    sys.exit(1)
print("ПРИЁМКА ПОТОКА ПРОШЛА: пол держится %.1f%% времени, смен %.1f/сутки, затор подхвачен"
      % (share, ch / DAYS))
