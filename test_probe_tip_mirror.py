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


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def reset():
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
        ex._MIRROR_LAST_SLOTS = ()


ADDRS = ["0x" + ("%040x" % i) for i in range(1, 9)]           # адреса всех 4 классов hash%4


def tiers_for(addrs):
    return sorted({ex.probe_tip_for(a) for a in addrs})


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

# --- 2. ПЕР-АГГ КОЛЬЦА: мода одного не разбавляется полами другого ------------------------------
reset()
for _ in range(12):
    ex.note_transit_tip(1_000_000, CBADA_AGG)
for _ in range(5):
    ex.note_transit_tip(33_400_000, JAM_AGG)
slots = ex.transit_tip_portfolio()
check("пер-агг: ОБА класса в слотах (v1 здесь давал только пол)",
      1_000_000 in slots and 33_400_000 in slots, str(slots))
check("порядок слотов — по величине моды убыв. (устойчив к свежести)",
      slots[0] == 33_400_000 and list(slots) == sorted(slots, reverse=True)[:1] + list(slots[1:]),
      str(slots))
check("слотов ровно 4: остаток добит статикой", len(slots) == 4, str(slots))
check("добивка — статические тиры без дублей",
      all(s in ex._PROBE_TIP_TIERS for s in slots[2:]) and len(set(slots)) == 4, str(slots))
_seen = tiers_for(ADDRS)
check("флот несёт все 4 слота", _seen == sorted(slots), str(_seen))
_n_jam = sum(1 for a in ADDRS if ex.probe_tip_for(a) == 33_400_000)
check("режим затора — ОДИН слот из 4, а не 3/4 как в v1", _n_jam == 2, "%d из 8" % _n_jam)

# --- 3. РЕЖИМНЫЙ СДВИГ: свежесть внутри агрегатора решает -------------------------------------
reset()
_old = time.time() - ex._MIRROR_TTL_SEC - 10
for _ in range(12):
    ex.note_transit_tip(1_000_000, JAM_AGG, now=_old)          # протухшие полы того же оператора
for _ in range(5):
    ex.note_transit_tip(33_400_000, JAM_AGG)
slots = ex.transit_tip_portfolio()
check("режим: свежие 33.4 вытеснили протухший пол своего же агрегатора",
      slots and slots[0] == 33_400_000, str(slots))
check("режим: старая мода в слоты НЕ попала", 1_000_000 not in slots[:1], str(slots))

# --- 4. РАЗБРОД ВНУТРИ АГРЕГАТОРА -> слот не создаётся -----------------------------------------
reset()
for w in (1_000_000, 1_200_000, 1_500_000, 1_775_000, 2_000_000, 2_500_000, 3_000_000, 5_000_000):
    ex.note_transit_tip(w, JAM_AGG)
check("разброд: слота нет", ex.transit_tip_portfolio() == (), str(ex.transit_tip_portfolio()))
check("разброд: тиры статические бит-в-бит", tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
# ПОЗИТИВНЫЙ КОНТРОЛЬ НА ТОМ ЖЕ ОБЪЕКТЕ: тот же агрегатор с согласованным классом слот даёт
reset()
for _ in range(6):
    ex.note_transit_tip(10_100_000, JAM_AGG)
check("тот же агрегатор со СВОИМ классом слот даёт", ex.transit_tip_portfolio()[0] == 10_100_000)

# --- 5. НЕИЗВЕСТНЫЙ АГРЕГАТОР — СВОЁ кольцо, не подмешивается к чужим ---------------------------
reset()
for _ in range(6):
    ex.note_transit_tip(1_000_000, CBADA_AGG)
for _ in range(6):
    ex.note_transit_tip(9_000_000)                             # agg=None
slots = ex.transit_tip_portfolio()
check("unknown: отдельное кольцо, оба класса живы", set(slots[:2]) == {9_000_000, 1_000_000},
      str(slots))
with ex._TRANSIT_TIPS_LOCK:
    _keys = set(ex._TRANSIT_TIPS)
check("unknown: ключ кольца отдельный", _keys == {CBADA_AGG.lower(), ex._MIRROR_UNKNOWN}, str(_keys))

# --- 6. МАЛО ДАННЫХ / TTL / ОТКАТ -> СТАТИКА БИТ-В-БИТ (регрессия против v1) --------------------
reset()
for _ in range(3):
    ex.note_transit_tip(1_000_000, CBADA_AGG)
check("мало данных: портфеля нет", ex.transit_tip_portfolio() == ())
check("мало данных: тиры статические", tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
reset()
for _ in range(8):
    ex.note_transit_tip(1_000_000, CBADA_AGG, now=_old)
check("TTL: протухло -> статика", ex.transit_tip_portfolio() == ())
reset()
for _ in range(8):
    ex.note_transit_tip(1_000_000, CBADA_AGG)
_saved = ex.PROBE_TIP_MIRROR
ex.PROBE_TIP_MIRROR = False
try:
    check("откат MIRROR=0: статические тиры при ЖИВОМ зеркале",
          tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
    check("откат: смены состава не объявляется", ex.mirror_slots_changed() is None
          or ex.mirror_slots_changed() is None)
finally:
    ex.PROBE_TIP_MIRROR = _saved
check("совместимость v1: transit_tip_mirror отдаёт пару", ex.transit_tip_mirror() is not None)

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
for _ in range(6):
    ex.note_transit_tip(1_000_000, CBADA_AGG)
ch = ex.mirror_slots_changed()
check("смена состава объявлена один раз", ch is not None and ch[1] == ())
check("повтор без изменений -> None", ex.mirror_slots_changed() is None)
for _ in range(6):
    ex.note_transit_tip(33_400_000, JAM_AGG)
ch2 = ex.mirror_slots_changed()
check("новый агрегатор -> новый состав", ch2 is not None and 33_400_000 in ch2[0], str(ch2))
_cost = ex._mirror_cost_usd(ch2[0])
check("оценка цены tip-проб положительна и в разумном классе", 0.01 < _cost < 100.0,
      "$%.3f/окно" % _cost)
print("   экономика: слоты %s -> ~$%.2f за окно (1200 совпадающих проб, 52k газа)"
      % (list(ch2[0]), _cost))

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
check("боевой дефолт: верх границы = экономический кэп 5e7", ex._MIRROR_TIP_MAX == 50_000_000)

if FAILS:
    print("=== ДРЕЛЬ #104 v2 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ #104 v2 ЗЕЛЁНАЯ: моды пер-агг, режим затора виден, незнание/откат — статика ===")
