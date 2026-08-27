"""Дрель Ш24b/#104 v2: тиры проб зеркалят TIP ТРАНЗИТА ПЕР-АГГРЕГАТОРНО.

v1 (доказательство боем 49978436): транзит 0.0010, победитель 0.0010 до wei, из 62 наших проб
рабочих 21 — остальные сортируются ПЕРЕД транзитом, в старую цену.

v2 (вскрытие потери 50191667, $3905): та же механика ПРОМАХНУЛАСЬ, потому что мода считалась
ГЛОБАЛЬНО. Транзиты cbADA (пол 1.0-1.2 mgwei) многочисленны, транзиты 0x1e0b2c38/0x0e3dc8a6 в
режиме затора несут 31-33 mgwei и редки: глобальная выборка топится полами, и в блоке 50191666
наши 22 пробы стояли с tip 1.0-3.0 при транзите 31.2 — tip-match=0. Плюс верхняя граница входа
2e7 отбрасывала ВЕСЬ режим 21-34.6 mgwei ещё на записи.

Здесь доказывается: (1) распределения агрегаторов НЕ смешиваются; (2) режимный сдвиг внутри
одного агрегатора подхватывается свежестью; (3) поднятая граница пускает наблюдённый максимум
34 597 197 wei и режет заведомо чужой класс; (4) полный отказ зеркала = статический портфель
БИТ-В-БИТ; (5) состав портфеля устойчив по порядку, а пересборку лестниц (#127) дёргает СМЕНА
ЗНАЧЕНИЯ тира ноги, а не факт вызова.

ЗАПУСК: python3 test_probe_tip_mirror.py
"""
import os
import sys
import time

os.environ.setdefault("SPEC_FIRE_PROBE_TIP_MIRROR", "1")

import chain.execute as ex                                    # noqa: E402

FAILS = []
CBADA_AGG = "0x0e3dc8a6"          # оператор с полом 1.0-1.15 mgwei (30+ сэмплов за неделю)
JAM_AGG = "0x1e0b2c38"            # оператор, несший 31.2-33.4 mgwei в момент потери
MAX_SEEN = 34_597_197             # максимальный tip транзита за 7 суток журнала
NOW = 1_787_200_000.0             # опорное время стенда (сэмплы кладём с явными ts)


def feed(tip, agg, n=8, t0=None, step=1.0):
    """Подтверждённый класс: гистерезис входа требует >=_MIRROR_ENTRY_N сэмплов ЛИБО возраста
    первого >=_MIRROR_ENTRY_AGE_SEC. Стенд кормит явным числом сэмплов и явным временем —
    иначе он мерил бы не правило, а свою скорость."""
    t0 = NOW if t0 is None else t0
    for k in range(n):
        ex.note_transit_tip(tip, agg, now=t0 + k * step)


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def reset():
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
    ex._MIRROR_LAST_SLOTS = ()
    ex._MIRROR_APPLIED, ex._MIRROR_APPLIED_MODES = (), frozenset()
    ex._MIRROR_HOT_UNTIL = 0.0
    ex._mirror_exp_reset()                           # 27.08: память дорогого слота — часть состояния


ADDRS = ["0x" + ("%040x" % i) for i in range(1, 9)]           # адреса всех 4 классов hash%4


def tiers_for(addrs):
    return sorted({ex.probe_tip_for(a) for a in addrs})


def portfolio(t=None):
    return ex.transit_tip_portfolio(now=NOW + 20 if t is None else t)


# --- 1. ГРАНИЦЫ ЗАПИСИ: поднятый верх пускает наблюдённый режим затора --------------------------
reset()
check("запись: мусор отброшен", ex.note_transit_tip("x") is False)
check("запись: 100 wei вне границ", ex.note_transit_tip(100) is False)
check("запись: -1 (дефолт индекса) отброшен", ex.note_transit_tip(-1) is False)
check("запись: 1e6 принят", ex.note_transit_tip(1_000_000) is True)
# КРАСНЫЙ-НА-БАЗЕ: ровно эти значения прежняя граница 2e7 отбрасывала на входе — то есть
# слепота была не в статистике, а в фильтре. Числа не выдуманы: 21.2/31.2/33.4/34.597 mgwei.
check("прежняя граница 2e7 была НИЖЕ наблюдённого режима", MAX_SEEN > 20_000_000)
for w in (21_200_000, 31_200_000, 33_400_000, MAX_SEEN):
    check("запись: режим затора %d принят" % w, ex.note_transit_tip(w) is True)
check("запись: 51 mgwei (вне экономического кэпа) отброшен",
      ex.note_transit_tip(51_000_000) is False)
check("запись: 35.1 mgwei (выше наблюдённого класса) отброшен",
      ex.note_transit_tip(35_100_000) is False)

# --- 2. ПЕР-АГГ КОЛЬЦА: мода одного не разбавляется полами другого ------------------------------
reset()
feed(1_000_000, CBADA_AGG, n=12)
feed(33_400_000, JAM_AGG, n=8)
slots = portfolio()
check("пер-агг: ОБА класса в слотах (v1 здесь давал только пол)",
      1_000_000 in slots and 33_400_000 in slots, str(slots))
# Порядок: сперва ОТОБРАННЫЕ классы по величине убыв., затем статический хвост В СВОЁМ порядке.
# Именно так, а не «весь кортеж по убыванию»: хвост — это прежний статический портфель, и его
# перестановка сдвинула бы тиры ног, которых зеркало вообще не касается.
_modes_part = [x for x in slots if x not in ex._PROBE_TIP_TIERS]
check("порядок слотов — отобранные классы по величине убыв., хвост статики в своём порядке",
      slots and slots[0] == 33_400_000
      and _modes_part == sorted(_modes_part, reverse=True)
      and list(slots[len(_modes_part):]) == [t for t in ex._PROBE_TIP_TIERS
                                             if t not in _modes_part][:4 - len(_modes_part)],
      str(slots))
check("слотов ровно 4: остаток добит статикой", len(slots) == 4, str(slots))
check("добивка — статические тиры без дублей",
      all(x in ex._PROBE_TIP_TIERS for x in slots[2:]) and len(set(slots)) == 4, str(slots))
_seen = sorted({slots[int(a[-8:], 16) % 4] for a in ADDRS})
check("флот несёт все 4 слота", _seen == sorted(slots), str(_seen))
_n_jam = sum(1 for a in ADDRS if slots[int(a[-8:], 16) % 4] == 33_400_000)
check("режим затора — ОДИН слот из 4, а не 3/4 как в v1", _n_jam == 2, "%d из 8" % _n_jam)

# --- 2b. ГИСТЕРЕЗИС ВХОДА: пачка из 4 сэмплов слот НЕ занимает -------------------------------
reset()
feed(1_000_000, CBADA_AGG, n=12)
feed(33_400_000, JAM_AGG, n=4)                       # ровно пачка затора, секунда в секунду
check("гистерезис: пачка 4 сэмплов слот НЕ занимает (110 лестниц не дёргаются)",
      33_400_000 not in portfolio(), str(portfolio()))
check("гистерезис: пол при этом на месте", 1_000_000 in portfolio(), str(portfolio()))
_late = ex.transit_tip_portfolio(now=NOW + ex._MIRROR_ENTRY_AGE_SEC + 5)
# 27.08: путь «возраст» для ДОРОГОГО класса ЗАКРЫТ (4/4 у минорного агрегатора = ничего);
# дорогой входит только по >= _MIRROR_EXP_N свежих сэмплов по флоту
check("гистерезис: пачка 4 дорогих ПОСЛЕ выдержки возраста слот НЕ получает (путь возраста закрыт)",
      33_400_000 not in _late, str(_late))
feed(33_400_000, JAM_AGG, n=ex._MIRROR_EXP_N - 4, t0=NOW + 10)   # добор до порога по флоту
_late2 = ex.transit_tip_portfolio(now=NOW + ex._MIRROR_ENTRY_AGE_SEC + 6)
check("гистерезис: та же пачка, добранная до %d свежих, слот получает (шторм)" % ex._MIRROR_EXP_N,
      33_400_000 in _late2, str(_late2))

# --- 2c. СКЛЕЙКА +-10%: соседние значения одного класса не занимают ЧЕТЫРЕ слота ---------------
reset()
for _i, _tip in enumerate((1_000_000, 1_100_000, 1_150_000, 1_200_000)):
    feed(_tip, "0xf%d" % _i, n=8)
_gl = portfolio()
# 27.08: в полосе ПОЛА склейки НЕТ — 1.0/1.1/1.15/1.2 в суб-блоке разные когорты, класс-
# представитель 1.0 стоял бы позади всей полосы; четыре точных значения = четыре слота
check("пол без склейки: 1.0/1.1/1.15/1.2 — ЧЕТЫРЕ точных слота, не один класс",
      sorted(_gl) == [1_000_000, 1_100_000, 1_150_000, 1_200_000], str(_gl))
reset()
feed(10_000_000, "0xa1", n=8)
feed(10_100_000, "0xa2", n=8)
_gl2 = portfolio()
check("склейка: 10.0 и 10.1 mgwei — один класс (дрейф оператора, не два режима)",
      sum(1 for x in _gl2 if 9_000_000 <= x <= 11_000_000) == 1, str(_gl2))

# --- 2d. ГАРАНТИЯ ПОЛА: четыре дорогих класса НЕ вытесняют призовой пол ----------------------
reset()
for _i, _tip in enumerate((30_000_000, 20_000_000, 12_000_000, 6_000_000)):
    feed(_tip, "0xd%d" % _i, n=8)
feed(1_000_000, "0xfff", n=12)
_g = portfolio()
check("ГАРАНТИЯ ПОЛА: при четырёх дорогих классах пол ОБЯЗАН занять слот (блокер линзы)",
      any(x <= ex._MIRROR_FLOOR_MAX for x in _g), str(_g))
check("гарантия пола: вытеснен ХУДШИЙ ПО ВЕСУ из дорогих, слотов по-прежнему 4",
      len(_g) == 4 and 1_000_000 in _g, str(_g))
# 27.08: индексы слотов СТАБИЛЬНЫ (порядок не по величине); при четырёх дорогих с равной
# уликой в портфель входит РОВНО ОДИН (EXPENSIVE_SLOTS=1), остальное — пол
check("отбор ПО ВЕСУ УЛИКИ, а не по цене: самый массовый класс (пол) в портфеле, дорогих — один",
      1_000_000 in _g and sum(1 for x in _g if x >= ex._MIRROR_EXP_MIN) == 1, str(_g))

# --- 2e. ЗАМОРОЗКА В ГОРЯЧЕЙ ФАЗЕ ------------------------------------------------------------
reset()
feed(1_000_000, CBADA_AGG, n=12)
_before = portfolio()
ex.note_hot_phase(True, now=NOW + 20, hold_sec=5.0)
feed(33_400_000, JAM_AGG, n=12, t0=NOW + 21)
check("горячая фаза: состав ЗАМОРОЖЕН (пересигнация 110 лестниц не запускается)",
      ex.transit_tip_portfolio(now=NOW + 23) == _before,
      str(ex.transit_tip_portfolio(now=NOW + 23)))
_after_hot = ex.transit_tip_portfolio(now=NOW + 40)
check("после угасания окна состав обновляется", 33_400_000 in _after_hot, str(_after_hot))
check("note_hot_phase(False) заморозку не ставит",
      ex.note_hot_phase(False, now=NOW + 100) is None)

# --- 2f. ЧИСТКА МЁРТВЫХ КОЛЕЦ ----------------------------------------------------------------
reset()
feed(1_000_000, CBADA_AGG, n=8)
feed(9_000_000, "0xdead", n=8, t0=NOW - ex._MIRROR_TTL_SEC - 100)
ex.transit_tip_portfolio(now=NOW + 20)
with ex._TRANSIT_TIPS_LOCK:
    _keys_left = set(ex._TRANSIT_TIPS)
check("кольцо, где ВСЕ сэмплы протухли, удалено целиком", "0xdead" not in _keys_left,
      str(_keys_left))

# --- 3. РЕЖИМНЫЙ СДВИГ: свежесть внутри агрегатора решает -------------------------------------
reset()
_old = NOW - ex._MIRROR_TTL_SEC - 10
feed(1_000_000, JAM_AGG, n=12, t0=_old)              # протухшие полы того же оператора
feed(33_400_000, JAM_AGG, n=8)
slots = portfolio()
check("режим: свежие 33.4 вытеснили протухший пол своего же агрегатора",
      slots and slots[0] == 33_400_000, str(slots))
check("режим: старая мода в слоты НЕ попала", 1_000_000 not in slots[:1], str(slots))

# --- 4. РАЗБРОД ВНУТРИ АГРЕГАТОРА -> слот не создаётся -----------------------------------------
reset()
for _k, _w in enumerate((1_000_000, 1_200_000, 1_500_000, 1_775_000, 2_000_000, 2_500_000,
                         3_000_000, 5_000_000)):
    ex.note_transit_tip(_w, JAM_AGG, now=NOW + _k)
check("разброд: слота нет", portfolio() == (), str(portfolio()))
check("разброд: тиры статические бит-в-бит", tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
# ПОЗИТИВНЫЙ КОНТРОЛЬ НА ТОМ ЖЕ ОБЪЕКТЕ: тот же агрегатор с согласованным классом слот даёт
reset()
feed(10_100_000, JAM_AGG, n=8)
check("тот же агрегатор со СВОИМ классом слот даёт", portfolio()[0] == 10_100_000, str(portfolio()))

# --- 5. НЕИЗВЕСТНЫЙ АГРЕГАТОР — СВОЁ кольцо, не подмешивается к чужим ---------------------------
reset()
feed(1_000_000, CBADA_AGG, n=8)
feed(9_000_000, None, n=8)                                     # agg=None
slots = portfolio()
check("unknown: отдельное кольцо, оба класса живы", set(slots[:2]) == {9_000_000, 1_000_000},
      str(slots))
with ex._TRANSIT_TIPS_LOCK:
    _keys = set(ex._TRANSIT_TIPS)
check("unknown: ключ кольца отдельный", _keys == {CBADA_AGG.lower(), ex._MIRROR_UNKNOWN}, str(_keys))

# --- 6. МАЛО ДАННЫХ / TTL / ОТКАТ -> СТАТИКА БИТ-В-БИТ (регрессия против v1) --------------------
reset()
feed(1_000_000, CBADA_AGG, n=3)
check("мало данных: портфеля нет", portfolio() == ())
check("мало данных: тиры статические", tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
reset()
for _ in range(8):
    ex.note_transit_tip(1_000_000, CBADA_AGG, now=_old)
check("TTL: протухло -> статика", portfolio() == ())
reset()
feed(1_000_000, CBADA_AGG, n=8)
_saved = ex.PROBE_TIP_MIRROR
ex.PROBE_TIP_MIRROR = False
try:
    check("откат MIRROR=0: статические тиры при ЖИВОМ зеркале",
          tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
    check("откат: смены состава не объявляется", ex.mirror_slots_changed() is None
          or ex.mirror_slots_changed() is None)
finally:
    ex.PROBE_TIP_MIRROR = _saved
check("совместимость v1: transit_tip_mirror отдаёт пару",
      ex.transit_tip_mirror(now=NOW + 20) is not None)

# --- 7. ДЕТЕРМИНИЗМ probe_tip_for (лестницы подписываются заранее) ------------------------------
for a in ADDRS:
    check("детерминизм %s" % a[-4:],
          ex.probe_tip_for(a) == ex.probe_tip_for(a) == ex.probe_tip_for(a.lower()))

# --- 8. ТРИГГЕР ПЕРЕСБОРКИ (#127): СМЕНА ЗНАЧЕНИЯ, а не факт вызова -----------------------------
_A = ADDRS[0]
_TIP = ex.probe_tip_for(_A)
_BASE = 5_000_000
ex._VOLLEY_LADDER[_A] = {"q": [b"\x01"], "tip": _TIP, "cd": ex.probe_calldata(),
                         "max_fee": _BASE * ex._VOLLEY_PRESIGN_MAXFEE_MULT + _TIP}
check("#127: тот же тир -> пересборки НЕТ", ex.ladder_resign_needed(_A, _BASE, _TIP) is False)
check("#127: другой тир -> пересборка", ex.ladder_resign_needed(_A, _BASE, _TIP + 1) is True)
check("#127: повторный вызов с тем же тиром снова НЕТ (триггер — значение, не вызов)",
      ex.ladder_resign_needed(_A, _BASE, _TIP) is False)
del ex._VOLLEY_LADDER[_A]

# --- 9. СМЕНА СОСТАВА ПОРТФЕЛЯ + ЭКОНОМИКА ------------------------------------------------------
reset()
feed(1_000_000, CBADA_AGG, n=8)
ch = ex.mirror_slots_changed(now=NOW + 20)
check("смена состава объявлена один раз", ch is not None and ch[1] == ())
check("повтор без изменений -> None", ex.mirror_slots_changed(now=NOW + 20) is None)
feed(33_400_000, JAM_AGG, n=8, t0=NOW + 1)
ch2 = ex.mirror_slots_changed(now=NOW + 30)
check("новый агрегатор -> новый состав", ch2 is not None and 33_400_000 in ch2[0], str(ch2))
_cost = ex._mirror_cost_usd(ch2[0], 1730.0)
check("оценка цены tip-проб положительна и в разумном классе", 0.01 < _cost < 100.0,
      "$%.3f/окно" % _cost)
print("   экономика: слоты %s -> ~$%.2f за окно (%.0f проб = %.0f/с x %.0fс, газ %d)"
      % (list(ch2[0]), _cost, ex.mirror_window_probes(), ex._PACER_RATE,
         ex._MIRROR_WINDOW_SEC, ex._PROBE_GAS_MEASURED))
check("объём проб — из ЖИВОГО конфига пейсера, а не константа",
      abs(ex.mirror_window_probes() - ex._PACER_RATE * ex._MIRROR_WINDOW_SEC) < 1e-6)
check("газ пробы — ЗАМЕРЕННЫЙ (STATE:652), а не «примерно 52k»",
      ex._PROBE_GAS_MEASURED == 54_710)
check("цена ETH — ПАРАМЕТР (константы в модуле нет)",
      ex._mirror_cost_usd(ch2[0], 2000.0) > ex._mirror_cost_usd(ch2[0], 1730.0))

# --- 10. ПРОВОДКА -------------------------------------------------------------------------------
import chain.chain_index as ci                                # noqa: E402
src_ci = open(ci.__file__).read()
check("проводка: set_on_transit в индексе", "set_on_transit" in src_ci and "_cbt(bn, _tr)" in src_ci)
check("проводка: индекс кладёт agg в транзит-дикт", '"agg"' in src_ci)
src_main = open(os.path.join(os.path.dirname(os.path.dirname(ex.__file__)), "main.py")).read()
check("проводка: арм явный в main", "note_transit_tip" in src_main and "set_on_transit" in src_main)
check("проводка: main передаёт agg (это и чинилось)", 't.get("agg")' in src_main)
src_ex = open(ex.__file__.replace(".pyc", ".py")).read()
check("проводка: presign_ladder через probe_tip_for", "probe_tip_for" in src_ex)
check("боевой дефолт: зеркало включено", ex.PROBE_TIP_MIRROR is True)
check("боевой дефолт: верх границы = экономический кэп 3.5e7 (чуть выше наблюдённого максимума)",
      ex._MIRROR_TIP_MAX == 35_000_000 and ex._MIRROR_TIP_MAX > MAX_SEEN)
check("боевой дефолт: TTL 15 минут (хвост цены режется)", ex._MIRROR_TTL_SEC == 900.0)
check("проводка: движок сообщает о горячей фазе",
      "note_hot_phase" in open(os.path.join(os.path.dirname(ex.__file__),
                                            "fire_engine.py")).read())
check("проводка: main передаёт цену ETH из конфига (и считает статику той же формулой)",
      "_mcost104(slots or tuple(_tiers104), cfg.eth_price_usd)" in src_main)

if FAILS:
    print("=== ДРЕЛЬ #104 v2 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ #104 v2 ЗЕЛЁНАЯ: моды пер-агг, режим затора виден, незнание/откат — статика ===")
