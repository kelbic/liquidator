"""Ш24b: тиры tip живут от ИЗМЕРЕННОГО класса транзита, а не от вбитых констант.

ЧТО БЫЛО (Ш24, 11.08). Флот делился на четыре тира — 1.0e6 / 1.1e6 / 1.775e6 / 3.0e6 — взятых
из частот доминирующего класса за 3 часа и одного наблюдения каскада.

ЧТО ПОКАЗАЛО ВСКРЫТИЕ 12.08. Решает не «класс блока вообще», а tip САМОГО ТРАНЗИТА — той
транзакции, что двигает цену. Он виден поимённо в шести блоках: 1.0e6 в 49834406 / 49887645 /
49887648 / 49887661 и 1.01e6 в 49880361 / 49887640. Тир 1.775e6 совпадает с классом в 0.016%
блоков, 3.0e6 — в 0.007%: ~55 ног из 110 стояли там, где приза не бывает.

Мода полного блока для этой цели НЕ ГОДИТСЯ — она спамерозависима: в 49880361 мода равна 1.01e6
только потому, что один контракт конкурента положил туда 1561 пробу.

ПРИЁМКА ЗДЕСЬ — ПРО ЭФФЕКТ, А НЕ ПРО СТРОКУ: считается ДОЛЯ НОГ, чей подписанный tip РАВЕН tip
транзита в измеренных блоках. Плюс дрели гистерезиса ВО ВРЕМЕНИ и позитивный контроль: без
гистерезиса тот же поток замеров даёт сотни смен набора в сутки.
"""
import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from collections import Counter                            # noqa: E402

import chain.execute as ex                                 # noqa: E402
import chain.fire_engine as fe                             # noqa: E402

FLEET = ["0x" + hashlib.sha256(str(i).encode()).hexdigest()[:40] for i in range(110)]
# tip ТРАНЗИТА, снятый поблочно (объект, который решает)
TRANSIT = {49834406: 1_000_000, 49887645: 1_000_000, 49887648: 1_000_000, 49887661: 1_000_000,
           49880361: 1_010_000, 49887640: 1_010_000}
# МЁРТВАЯ полоса — только 1.775e6. Полосу 3.0e6 ревью вернуло как КАСКАДНУЮ СТРАХОВКУ, и
# вернуло как множитель класса (x3), а не как константу: частота 0.007% считана по ВСЕМ блокам,
# а стреляем мы в каскадах, где знаменателя у нас нет. Замер того, чем я предлагал её заменить
# (динамика набора), — в разделе 9: на боевом каденсе аудита каскад её не двигает вовсе.
DEAD_TIERS = (1_775_000,)                                  # 0.016% блоков


def in_class(tiers, cls):
    """Сколько ног флота встанут РОВНО в класс `cls` при наборе `tiers`."""
    if not tiers:
        return 0
    return sum(1 for a in FLEET if tiers[int(str(a)[-8:], 16) % len(tiers)] == cls)


def reset_class_state():
    ex._TIP_CLASS_WIN.clear()
    ex._TIP_CLASS_LATCHED = 0
    ex._TIP_CLASS_SWITCH_TS = 0.0
    ex._TIP_TIERS_LIVE = tuple(ex._PROBE_TIP_TIERS)


# --- 1. ЭФФЕКТ: ноги стоят в классе транзита, а не в мёртвых полосах ---------------------
OLD = (1_000_000, 1_100_000, 1_775_000, 3_000_000)         # набор Ш24
new = ex.probe_tip_tiers()
old_hits = sum(in_class(OLD, c) for c in TRANSIT.values()) / len(TRANSIT)
new_hits = sum(in_class(new, c) for c in TRANSIT.values()) / len(TRANSIT)
assert new_hits > old_hits, "новый набор не поднял долю ног в классе транзита: %.1f -> %.1f" % (
    old_hits, new_hits)
assert in_class(OLD, 1_010_000) == 0, "контроль сломан: старый набор якобы покрывал 1.01e6"
assert in_class(new, 1_010_000) > 0, "класс 1.01e6 (2 из 6 блоков) снова не покрыт"
dead_old = sum(1 for a in FLEET if OLD[int(str(a)[-8:], 16) % len(OLD)] in DEAD_TIERS)
dead_new = sum(1 for a in FLEET if ex.probe_tip_for(a) in DEAD_TIERS)
assert dead_new == 0, "%d ног всё ещё стоят в мёртвой полосе %s" % (dead_new, DEAD_TIERS)
casc = sum(1 for a in FLEET if ex.probe_tip_for(a) == 3_000_000)
assert casc > 0, "каскадная полоса пуста — страховка снята, а замера в её знаменателе нет"
print("OK 1/8: в классе транзита %.0f ног из 110 против %.0f у прежнего набора; в мёртвой "
      "полосе 1.775e6 — 0 против %d; на каскадной страховке %d ног"
      % (new_hits, old_hits, dead_old, casc))

# --- 2. Флот делится ровно и ДЕТЕРМИНИРОВАННО -------------------------------------------
c = Counter(ex.probe_tip_for(a) for a in FLEET)
assert set(c) == set(new), "не все тиры населены: %s" % dict(c)
ideal = len(FLEET) / len(new)
assert min(c.values()) >= ideal * 0.6, "тир голодает (%d при идеале %.0f)" % (min(c.values()), ideal)
for a in FLEET[:20]:
    assert ex.probe_tip_for(a) == ex.probe_tip_for(a) == ex.probe_tip_for(a.lower())
print("OK 2/8: 110 ног -> %s (идеал %.0f на тир), назначение детерминировано" % (dict(sorted(c.items())), ideal))

# --- 3. НАБОР ЕДЕТ ЗА ИЗМЕРЕНИЕМ (то, ради чего Ш24b) -----------------------------------
# Класс уехал 1.0e6 -> 1.5e6 и держится час. Набор обязан переехать, и переехать ЭФФЕКТИВНО:
# ноги обязаны получить новый tip, а не «в логах появилась строка».
reset_class_state()
t = 0.0
for _ in range(400):                                       # час замеров с шагом 10с
    ex.refresh_tip_tiers(now=t, dom_wei=1_500_000)
    t += 10.0
tiers3 = ex.probe_tip_tiers()
assert 1_500_000 in tiers3, "набор не переехал на измеренный класс: %s" % (tiers3,)
assert in_class(tiers3, 1_500_000) >= len(FLEET) / len(tiers3) * 0.6, \
    "класс покрыт, но ногами не населён: %s" % (tiers3,)
assert any(ex.probe_tip_for(a) == 1_500_000 for a in FLEET), "ни одна нога не встала в новый класс"
print("OK 3/8: класс 1.0e6 -> 1.5e6 за час замеров -> набор %s, в классе %d ног"
      % (list(tiers3), in_class(tiers3, 1_500_000)))

# --- 4. ГИСТЕРЕЗИС: полоса, кулдаун, окно ------------------------------------------------
# 4a. полоса 2 п.п.: дрожание внутри полосы набор НЕ двигает
reset_class_state()
t = 0.0
for _ in range(400):
    ex.refresh_tip_tiers(now=t, dom_wei=1_010_000)         # +1% к опорному 1.0e6
    t += 10.0
assert ex.probe_tip_tiers() == tuple(ex._PROBE_TIP_TIERS), \
    "смена внутри полосы 2 п.п. перетряхнула флот: %s" % (ex.probe_tip_tiers(),)
# 4b. кулдаун: сразу после переезда второй переезд запрещён
reset_class_state()
t = 0.0
for _ in range(400):
    ex.refresh_tip_tiers(now=t, dom_wei=1_500_000)
    t += 10.0
first = ex.probe_tip_tiers()
for _ in range(200):                                       # ещё 33 минуты, класс уехал снова
    ex.refresh_tip_tiers(now=t, dom_wei=3_000_000)
    t += 10.0
assert ex.probe_tip_tiers() == first, "кулдаун 60 мин не удержал второй переезд подряд"
for _ in range(200):                                       # добираем час -> переезд разрешён
    ex.refresh_tip_tiers(now=t, dom_wei=3_000_000)
    t += 10.0
assert ex.probe_tip_tiers() != first, "после кулдауна набор обязан ехать за классом"
print("OK 4/8: полоса 2 п.п. держит дрожание, кулдаун 60 мин держит второй переезд подряд")

# --- 5. ВО ВРЕМЕНИ, А НЕ В МОМЕНТЕ: одиночный замер и «неделя штиля» ---------------------
# 5a. один свежий замер (даже дикий) набор не двигает — улик нет
reset_class_state()
assert ex.refresh_tip_tiers(now=0.0, dom_wei=9_000_000)[1] is False, "переезд по ОДНОМУ замеру"
assert ex.probe_tip_tiers() == tuple(ex._PROBE_TIP_TIERS)
# 5b. дрель «неделя штиля -> старт окна»: окно накоплено, потом семплер молчит неделю, потом
# один свежий замер. Он НЕ имеет права переключить набор: улики протухли вместе с окном.
reset_class_state()
t = 0.0
for _ in range(400):
    ex.refresh_tip_tiers(now=t, dom_wei=1_000_000)
    t += 10.0
t += 7 * 86400.0                                           # неделя тишины (семплер молчит)
sw = ex.refresh_tip_tiers(now=t, dom_wei=5_000_000)[1]
assert sw is False, "первый замер после недели штиля переключил набор — окно не протухло"
assert len(ex._TIP_CLASS_WIN) == 1, "окно не обнулилось молчанием: %d" % len(ex._TIP_CLASS_WIN)
print("OK 5/8: одиночный замер набор не двигает; после недели штиля окно копится заново")

# --- 6. ПОЗИТИВНЫЙ КОНТРОЛЬ: без гистерезиса тот же поток даёт сотни смен в сутки --------
# Иначе разделы 4-5 не доказывают ничего: они могли бы «держать набор» просто потому, что стенд
# не умеет его двигать.
# Сутки замеров с шагом 10с. Класс дрожит между соседними блоками (замер: смена в 33% блоков),
# а В СЕРЕДИНЕ СУТОК действительно уезжает — набор обязан переехать РОВНО один раз, а не ходить
# за каждым дрожанием.
FEED = []
for i in range(8640):
    base = 1_000_000 if i < 4320 else 1_500_000
    FEED.append(base if (i // 7) % 3 else int(base * (1.1 if (i // 11) % 2 else 1.01)))


def switches(band, cooldown, window, span=0.9, min_samples=6):
    reset_class_state()
    sv = (ex._TIP_CLASS_BAND, ex._TIP_CLASS_COOLDOWN_SEC, ex._TIP_CLASS_WINDOW_SEC,
          ex._TIP_CLASS_SPAN_FRAC, ex._TIP_CLASS_MIN_SAMPLES)
    ex._TIP_CLASS_BAND, ex._TIP_CLASS_COOLDOWN_SEC = band, cooldown
    ex._TIP_CLASS_WINDOW_SEC, ex._TIP_CLASS_SPAN_FRAC = window, span
    ex._TIP_CLASS_MIN_SAMPLES = min_samples
    n = 0
    try:
        for i, w in enumerate(FEED):
            if ex.refresh_tip_tiers(now=i * 10.0, dom_wei=w)[1]:
                n += 1
    finally:
        (ex._TIP_CLASS_BAND, ex._TIP_CLASS_COOLDOWN_SEC, ex._TIP_CLASS_WINDOW_SEC,
         ex._TIP_CLASS_SPAN_FRAC, ex._TIP_CLASS_MIN_SAMPLES) = sv
    return n


with_hyst = switches(ex._TIP_CLASS_BAND, ex._TIP_CLASS_COOLDOWN_SEC, ex._TIP_CLASS_WINDOW_SEC)
without = switches(0.0, 0.0, 10.0, span=0.0, min_samples=1)
assert 1 <= with_hyst <= 2, ("с гистерезисом %d смен набора в сутки: он обязан ПРОПУСТИТЬ "
                             "настоящий переезд (>=1) и погасить дрожание (<=2 на переходе, "
                             "где окно ещё смешано из старого и нового класса)" % with_hyst)
assert without > 50, ("КОНТРОЛЬ ПРОВАЛЕН: без гистерезиса стенд даёт %d смен — он не умеет их "
                      "считать, значит цифра выше ничего не доказывает" % without)
print("OK 6/8: КОНТРОЛЬ — на сутках дрожащего класса %d смен набора против %d без гистерезиса"
      % (with_hyst, without))

# --- 7. ПОТОЛОК ПЕРЕСБОРКИ: настоящая plan_ladder_resigns --------------------------------
# Ноги «уехали по тиру» все разом (так и будет при смене набора), но за проход разрешено не
# больше потолка. «price» потолком НЕ ограничен: такая нога уже нема.
reset_class_state()
ex._VOLLEY_LADDER.clear()
BASE = 5_000_000
for a in FLEET[:30]:
    ex._VOLLEY_LADDER[a] = {"q": [(0, "0x", None)], "gas": 3_000_000,
                            "max_fee": BASE * ex._VOLLEY_PRESIGN_MAXFEE_MULT + 1_000_000,
                            "tip": 1_000_000, "ts": 0.0}
stale, price, moved, deferred = ex.plan_ladder_resigns(FLEET[:30], BASE, lambda _a: 1_777_000)
assert price == [], "цена объявлена протухшей на ровном месте: %d" % len(price)
assert len(moved) == ex.tip_resign_cap() and deferred == 30 - ex.tip_resign_cap(), \
    "потолок переезда не сработал: %d за проход, отложено %d" % (len(moved), deferred)
# та же лестница, но невключаемая по цене -> идёт БЕЗ потолка
for a in FLEET[:30]:
    ex._VOLLEY_LADDER[a]["max_fee"] = 1                     # ниже пола включаемости
stale2, price2, moved2, _d2 = ex.plan_ladder_resigns(FLEET[:30], BASE, lambda _a: 1_000_000)
assert len(price2) == 30 and moved2 == [], "немая по цене нога попала под потолок переезда"
ex._VOLLEY_LADDER.clear()
print("OK 7/8: переезд по тиру порциями по %d за проход (отложено %d), пересборка по цене — без потолка"
      % (ex.tip_resign_cap(), deferred))

# --- 8. ОТКАТ СНИМАЕТ МЕХАНИЗМ ------------------------------------------------------------
# 8a. SPEC_FIRE_TIP_CLASS_DYNAMIC=0 -> набор снова статический ДАЖЕ ПОСЛЕ состоявшегося переезда
reset_class_state()
t = 0.0
for _ in range(400):
    ex.refresh_tip_tiers(now=t, dom_wei=1_500_000)
    t += 10.0
assert 1_500_000 in ex.probe_tip_tiers()
_sv = ex._TIP_CLASS_DYNAMIC
try:
    ex._TIP_CLASS_DYNAMIC = False
    assert ex.probe_tip_tiers() == tuple(ex._PROBE_TIP_TIERS), "откат не вернул статический набор"
    assert ex.refresh_tip_tiers(now=t + 10_000, dom_wei=9_000_000) == (
        tuple(ex._PROBE_TIP_TIERS), False), "с выключенным механизмом набор всё ещё считается"
    assert all(ex.probe_tip_for(a) in ex._PROBE_TIP_TIERS for a in FLEET), \
        "ноги остались на динамическом наборе — откат снял параметр, а не механизм"
finally:
    ex._TIP_CLASS_DYNAMIC = _sv
# 8b. пустой список тиров -> прежняя одна константа (откат одной переменной окружения)
_saved = ex._PROBE_TIP_TIERS
try:
    ex._PROBE_TIP_TIERS = ()
    reset_class_state()
    assert ex.probe_tip_for(FLEET[0]) == ex._PROBE_TIP_WEI, "пустой список не вернул прежнюю константу"
    assert ex.refresh_tip_tiers(now=0.0, dom_wei=1_500_000) == ((), False), \
        "без тиров механизм всё равно считает набор"
finally:
    ex._PROBE_TIP_TIERS = _saved
    reset_class_state()
print("OK 8/8: откат SPEC_FIRE_TIP_CLASS_DYNAMIC=0 снимает МЕХАНИЗМ (набор статичен даже после "
      "переезда); пустой список тиров -> прежняя константа")

# --- 9. ЗАМЕР: ДИНАМИКА НАБОРА КАСКАД НЕ ПОКРЫВАЕТ (блокер ревью 12.08) -----------------
# Я снял статический тир 3.0e6 по частоте, считанной ПО ВСЕМ БЛОКАМ, и предложил взамен
# динамику. Здесь она измеряется на БОЕВОМ каденсе аудита — тем самым, с которым живёт бот.
def switch_after(cascade_wei, hold_sec, cadence=fe.ENGINE_AUDIT_SEC, calm=1_000_000):
    """Через сколько секунд НЕПРЕРЫВНОГО удержания класса набор переедет. None = не переедет."""
    reset_class_state()
    t = 0.0
    while t < 4 * 3600:                                    # сутки-эквивалент спокойного класса
        ex.refresh_tip_tiers(now=t, dom_wei=calm)
        t += cadence
    t0, at = t, None
    while t - t0 <= hold_sec:
        _tiers, sw = ex.refresh_tip_tiers(now=t, dom_wei=cascade_wei)
        if sw and at is None:
            at = t - t0
        t += cadence
    return at


CASC = 3_000_000
assert switch_after(CASC, 240) is None, \
    "стенд не воспроизводит: 4-минутный каскад якобы двигает набор"
_at = switch_after(CASC, 4 * 3600)
assert _at is not None and _at >= 1500, "набор переезжает подозрительно быстро: %.0fс" % _at
reset_class_state()
print("OK 9/9: ЗАМЕР на боевом каденсе (аудит %.0fс): каскад 4 мин НЕ двигает набор вовсе, "
      "непрерывное удержание класса %d wei двигает через %.0f мин (+ до %.0f мин кулдауна). "
      "Каскад живёт 2-4с ⇒ динамика каскадную полосу НЕ ПОКРЫВАЕТ — поэтому она вернулась "
      "множителем x3" % (fe.ENGINE_AUDIT_SEC, CASC, _at / 60.0,
                         ex._TIP_CLASS_COOLDOWN_SEC / 60.0))

# --- ПРОВОДКА: набор действительно спрашивают из аудита, а лестница подписывается тиром ---
src = pathlib.Path(__file__).resolve().parent
blk = (src / "chain/execute.py").read_text()
i = blk.index("def presign_ladder(")
assert "probe_tip_for(addr)" in blk[i:blk.index("\ndef ", i + 10)], \
    "лестница подписывается общей константой — тиры мертвы"
ablk = (src / "chain/spec_fire_live.py").read_text()
j = ablk.index("def _audit(addrs):")
ablk = ablk[j:ablk.index("def _combat_url()", j)]
assert "refresh_tip_tiers(" in ablk, "набор тиров никто не сверяет с замером — Ш24b мёртв"
assert "plan_ladder_resigns(" in ablk, "аудит решает сам, мимо потолка переезда"
assert "_PROBE_TIP_WEI" not in ablk, "в аудите осталась общая константа"

# --- Плотность: попавший тир обязан давать ~8 проб на блок -------------------------------
per_block = fe.ENGINE_HOLD_RATE * 2.0                      # блок Base = 2с
assert per_block / len(ex.probe_tip_tiers()) >= 7.0, \
    "в классе %.1f проб на блок — меньше, чем было у победителей" % (per_block / len(ex.probe_tip_tiers()))
print("ПРОВОДКА: аудит сверяет набор с замером и планирует пересборку через plan_ladder_resigns; "
      "темп %.0f/с -> %.1f проб в классе на блок (у победителей 8)"
      % (fe.ENGINE_HOLD_RATE, per_block / len(ex.probe_tip_tiers())))
print("=== Ш24b ЗАКРЫТ: набор тиров едет за ИЗМЕРЕННЫМ классом, под гистерезисом и потолком ===")
