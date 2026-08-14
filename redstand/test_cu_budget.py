"""Бюджет боевого ключа на кэш позиций + скользящий ФАКТ расхода вместо спайка одного окна.

Повод (07.08, замер против замысла). В коде записано «~22 вызова/30с ТОЛЬКО в окнах»; журнал
за сутки дал 45 399 вызовов `refresh_position_cache` = 35M CU/мес на боевом ключе Alchemy при
лимите 30M — 53% всего его расхода. Логика была цела, разошлась ЧАСТОТА: prearm-петля зовёт
`cache_boost()` безусловно на каждом проходе и продлевает буст на 90с, а predict-окна идут
почти встык, поэтому буст держался 72% суток.

Второй дефект той же истории: тревога по квоте висела на проекции ОДНОГО окна и на защёлке
«не раньше 6ч аптайма». Проекция одного окна расходится с фактом в 6.7-40 раз, а защёлку
сносил каждый рестарт: 07.08 при 21 рестарте и максимальном за неделю расходе тревога не
сработала ни разу.
"""
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.rpc as rpc                                 # noqa: E402
import chain.hotpath as hp                              # noqa: E402
import chain.failover as fo                             # noqa: E402

# ---------- 1. скользящий факт ----------------------------------------------------------
rpc._CU_ROLL.clear()
# 2600 CU за 120с => 2600/120*86400*30/1e6 = 56.16 M/мес
p, span = rpc.cu_rolling_add("h", 2600, 120)
assert abs(p - 56.16) < 0.01, p
assert span == 120, span

# второе окно вдвое дешевле — накопленное едет к среднему, а не прыгает на последнее
p2, span2 = rpc.cu_rolling_add("h", 1300, 120)
assert span2 == 240, span2
assert abs(p2 - 42.12) < 0.01, "скользящее должно быть средним по накопленному, а не последним: %s" % p2

# КОНТРОЛЬ: проекция ОДНОГО окна на тех же данных дала бы 28.08 — то есть одиночное окно и
# накопленный факт это РАЗНЫЕ числа, и порог обязан стоять на втором.
assert abs(1300 / 120 * 86400 * 30 / 1e6 - 28.08) < 0.01

# хосты не смешиваются
p3, _ = rpc.cu_rolling_add("other", 100, 120)
assert p3 < 5.0, p3
assert abs(rpc.cu_rolling_add("h", 1300, 120)[0] - 37.44) < 0.01

# старые замеры выпадают из окна
rpc._CU_ROLL["h"] = [(time.monotonic() - rpc.CU_ROLL_WINDOW_SEC - 10, 999999.0, 120.0)]
p4, span4 = rpc.cu_rolling_add("h", 2600, 120)
assert span4 == 120 and abs(p4 - 56.16) < 0.01, (p4, span4)

# ---------- 2. бюджет боевого ключа -----------------------------------------------------
COMBAT, BG = object(), object()
used = []
orig_refresh, orig_shed = hp._refresh_position_caches, fo.bg_shed
fo.bg_shed = lambda *a, **k: False
hp._refresh_position_caches = lambda **kw: used.append(kw["cache_rpc"])


class _Shared:
    pass


def spawn():
    hp._CACHE_REFRESH_STARTED = 0.0        # снять интервал-гейт, чтобы гонять циклы подряд
    hp._CACHE_REFRESH_THREAD = None
    hp._spawn_cache_refresh(cache_rpc=BG, shared=_Shared(), bn=1, combat_rpc=COMBAT)
    t = hp._CACHE_REFRESH_THREAD
    if t is not None:
        t.join(5)


try:
    hp._CACHE_COMBAT_SPEND.clear()
    hp._CACHE_BOOST_UNTIL = 0.0
    assert hp._CACHE_COMBAT_BUDGET_SEC_H == 600.0, hp._CACHE_COMBAT_BUDGET_SEC_H
    assert hp._CACHE_REFRESH_MIN_INTERVAL == 30.0, hp._CACHE_REFRESH_MIN_INTERVAL
    allowed = int(hp._CACHE_COMBAT_BUDGET_SEC_H / hp._CACHE_REFRESH_MIN_INTERVAL)   # 20

    # штиль: буста нет -> фоновый ключ, бюджет не тратится
    spawn()
    assert used == [BG], used
    assert not hp._CACHE_COMBAT_SPEND, "штилевой цикл не должен платить бюджетом"

    # окно живо: боевой ключ, пока хватает бюджета часа
    used.clear()
    hp.cache_boost(90.0)
    for _ in range(allowed):
        spawn()
    assert used == [COMBAT] * allowed, "в бюджете все циклы обязаны идти боевым ключом: %d из %d" % (
        used.count(COMBAT), allowed)
    assert len(hp._CACHE_COMBAT_SPEND) == allowed, hp._CACHE_COMBAT_SPEND

    # бюджет выбран -> тот же живой буст уже НЕ пускает на платный ключ
    used.clear()
    hp.cache_boost(90.0)
    spawn()
    assert used == [BG], "бюджет часа выбран, а цикл всё равно ушёл на боевой ключ: %s" % used

    # ...и каденс упал до тихого: цикл 60-секундной давности при MIN=30 прошёл бы, при QUIET=120 нет
    used.clear()
    hp._CACHE_REFRESH_THREAD = None
    hp._CACHE_REFRESH_STARTED = time.monotonic() - 60.0
    hp._spawn_cache_refresh(cache_rpc=BG, shared=_Shared(), bn=2, combat_rpc=COMBAT)
    if hp._CACHE_REFRESH_THREAD is not None:
        hp._CACHE_REFRESH_THREAD.join(5)
    assert used == [], "сверх бюджета каденс обязан упасть до тихого (%.0fс), а цикл пошёл" % (
        hp._CACHE_QUIET_INTERVAL)

    # КОНТРОЛЬ обратного: без выбранного бюджета тот же 60-секундный цикл проходит.
    # Без него «цикл не пошёл» неотличимо от «тест не дошёл до вызова».
    hp._CACHE_COMBAT_SPEND.clear()
    used.clear()
    hp._CACHE_REFRESH_THREAD = None
    hp._CACHE_REFRESH_STARTED = time.monotonic() - 60.0
    hp.cache_boost(90.0)
    hp._spawn_cache_refresh(cache_rpc=BG, shared=_Shared(), bn=3, combat_rpc=COMBAT)
    if hp._CACHE_REFRESH_THREAD is not None:
        hp._CACHE_REFRESH_THREAD.join(5)
    assert used == [COMBAT], "контроль: в бюджете 60-секундный цикл обязан пройти боевым: %s" % used

    # откат без кода: бюджет 0 = прежнее поведение
    hp._CACHE_COMBAT_BUDGET_SEC_H, save = 0.0, hp._CACHE_COMBAT_BUDGET_SEC_H
    hp._CACHE_COMBAT_SPEND.clear()
    used.clear()
    hp.cache_boost(90.0)
    for _ in range(allowed + 5):
        spawn()
    assert used == [COMBAT] * (allowed + 5), "бюджет 0 обязан снимать ограничение целиком"
    hp._CACHE_COMBAT_BUDGET_SEC_H = 600.0
finally:
    hp._refresh_position_caches, fo.bg_shed = orig_refresh, orig_shed
    hp._CACHE_COMBAT_SPEND.clear()
    hp._CACHE_BOOST_UNTIL = 0.0
    hp._CACHE_REFRESH_THREAD = None
    hp._CACHE_REFRESH_STARTED = 0.0
    rpc._CU_ROLL.clear()

print("OK: бюджет боевого ключа держит, скользящий факт не спайкует, откат на 0 работает")
