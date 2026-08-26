"""П.2 ревьюера 22.08: метрика class_catch_lag — блоков от первого tx нового класса X до
первой нашей пробы на X.

Цель замера — назвать, где живёт лаг: подача (детект→кольцо) или переодевание лестниц
(класс→пресайн). Канон честного знаменателя ПРИ РОЖДЕНИИ прибора: пойманные классы — кольцо
class_catch_ring, непойманные смены — счётчик class_catch_uncaught; читать только парой,
иначе выборка цензурирована (p50 кольца «улучшается» ровно тогда, когда ловим меньше).
"""
import sys
import threading
import time
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402

o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o.stats = {}
logs = []
o._log = lambda m: logs.append(m)
AGG = "0x0E3dC8A6"                                   # ключ кольца = str(agg).lower()

# ---- 1. Первый класс в жизни, пробы ещё не на нём: тишина (не «непойманная смена») ----
o._class_catch(100, AGG, 1_775_000, False)
check("первый класс без match — тишина", not logs and "class_catch_uncaught" not in o.stats,
      logs)

# ---- 2. Тот же класс, первый match: class_catch_lag = b1-b0 ----
o._class_catch(103, AGG, 1_775_000, True)
check("поимка пишет строку с литералом class_catch_lag=3",
      len(logs) == 1 and "class_catch_lag=3" in logs[0] and "b0=100" in logs[0]
      and "b1=103" in logs[0], logs)
check("кольцо лагов пополнено", o.stats.get("class_catch_ring") == [3], o.stats)
check("непойманных в строке = 0 (честный знаменатель в самой строке)",
      "непойманных смен=0" in logs[0], logs)

# ---- 3. Тот же класс, пойман — тишина; смена ПОСЛЕ поимки — тихий сброс ----
logs.clear()
o._class_catch(104, AGG, 1_775_000, True)
o._class_catch(110, AGG, 2_000_000, False)
check("пойманный класс и тихая смена не пишут строк", not logs, logs)

# ---- 4. Смена НЕПОЙМАННОГО класса: счётчик + строка (цензуру видно) ----
o._class_catch(115, AGG, 2_100_000, False)
check("непойманная смена: строка + class_catch_uncaught=1",
      len(logs) == 1 and "СМЕНИЛСЯ НЕПОЙМАННЫМ" in logs[0] and "за 5 блоков" in logs[0]
      and o.stats.get("class_catch_uncaught") == 1, (logs, o.stats))

# ---- 5. Мгновенная поимка нового класса в том же блоке: lag=0 ----
logs.clear()
o._class_catch(120, AGG, 2_200_000, True)
check("смена+match в одном вызове: непойманная смена И lag=0",
      any("СМЕНИЛСЯ" in l for l in logs) and any("class_catch_lag=0" in l for l in logs),
      logs)
check("кольцо лагов = [3, 0], капирование в исходнике",
      o.stats["class_catch_ring"] == [3, 0], o.stats)

# ---- 6. Возраст кольца зеркала: якорь — штамп b0, ключ — как у note_transit_tip ----
import chain.execute as ex  # noqa: E402

logs.clear()
_saved = dict(ex._TRANSIT_TIPS)
ex._TRANSIT_TIPS.clear()
ex._TRANSIT_TIPS[AGG.lower()] = [(time.time() - 7.0, 3_000_000)]
o._class_catch(130, AGG, 3_000_000, False)           # b0: штамп стенки
o._class_catch(133, AGG, 3_000_000, True)
_catch = [l for l in logs if "class_catch_lag=" in l]
import re  # noqa: E402
_m = re.search(r"кольцо_знало≥(\d+\.\d)с-до-b0", _catch[0]) if _catch else None
check("кольцо_знало≥ считается от штампа b0 (~7с, тот же тред)",
      len(_catch) == 1 and _m is not None and 5.0 <= float(_m.group(1)) <= 9.0, _catch)
logs.clear()
ex._TRANSIT_TIPS.clear()                             # класс, которого кольцо НЕ видело
o._class_catch(140, AGG, 3_100_000, True)
_c2 = [l for l in logs if "class_catch_lag=" in l]
check("кольцо не знало класс — суффикса нет (не ноль, а отсутствие)",
      len(_c2) == 1 and "кольцо" not in _c2[0], _c2)
ex._TRANSIT_TIPS.update(_saved)

# ---- 7. Карта капирована, чужой отказ глотается ----
for i in range(20):
    o._class_catch(150, "0xagg%02d" % i, 1_000_000, False)
check("карта состояний капирована (<=8 агрегаторов)", len(o._ccl) <= 8, len(o._ccl))
o2 = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o2.stats = {}
o2._log = lambda m: (_ for _ in ()).throw(RuntimeError("журнал упал"))
o2._class_catch(100, AGG, 1, False)
o2._class_catch(103, AGG, 1, True)                   # _log бросает на поимке
check("отказ журнала глотается (прибор сбоку)", True)

# ---- 8. Гарды дрейфа боевого потребителя ----
# ---- ПОЧИНКА 26.08: ключ = ИМЯ агрегатора транзита, не frozenset владельцев ----
o9 = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o9.stats = {}
o9._lock = threading.Lock()
logs9 = []
o9._log = logs9.append
o9._now = lambda: 3000.0
o9._class_catch(200, frozenset({"0xaaa", "0xbbb"}), 1_775_000, False)
o9._class_catch(203, frozenset({"0xaaa", "0xbbb"}), 1_775_000, True)
check("frozenset в ключе больше не появляется в строке (регресс 26.08)",
      all("frozenset" not in l for l in logs9), logs9)
logs9.clear()
o9b = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o9b.stats = {}; o9b._lock = threading.Lock(); o9b._log = logs9.append; o9b._now = lambda: 3000.0
o9b._class_catch(200, "0x92A7c3a5", 1_775_000, False)
o9b._class_catch(203, "0x92A7c3a5", 1_775_000, True)
check("строка несёт ИМЯ агрегатора (нижним регистром, как ключ кольца)",
      any("agg=0x92a7c3a5" in l for l in logs9), logs9)
_S2 = open("chain/spec_fire_live.py").read()
check("вызов берёт agg ИЗ ТРАНЗИТА, а не набор владельцев",
      "self._class_catch(bn, _t_agg, t_tip, bool(match_d))" in _S2
      and '_t_agg = _tr.get("agg")' in _S2)

_SRC = open("chain/spec_fire_live.py").read()
check("вызов стоит в _pos_table после копилки match (с agg транзита)",
      "self._class_catch(bn, _t_agg, t_tip, bool(match_d))" in _SRC)
check("fail-safe нормализации набора в приборе (детерминированный первый)",
      "isinstance(agg, (frozenset, set, list, tuple))" in _SRC and "sorted(str(a).lower()" in _SRC)
check("кольцо лагов капировано (~100)", "del cring[:-100]" in _SRC)
check("литерал class_catch_lag= в строке (приёмка грепает каноничное имя)",
      "class_catch_lag=%d" in _SRC)
check("честный знаменатель: счётчик непойманных существует", "class_catch_uncaught" in _SRC)
check("разрез на СТАДИИ назван в докстринге (возраст_лестницы = вторая половина)",
      "возраст_лестницы» на первой match-пробе" in _SRC)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: class_catch_lag судит парой пойманные+непойманные; прибор сбоку"))
sys.exit(1 if FAILS else 0)
