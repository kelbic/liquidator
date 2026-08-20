"""Брейкер fast-lane не должен считать ПРОИГРАННЫЕ ГОНКИ штормом.

Повод — живая гонка 07.08 17:00, блок 49666338. Каскад на cbXRP/USDC: бот собрал пять целей
(`HOT ready FAST`, среди них $30890 net≥$1957 и $16489 net≥$1045), проиграл первые три
конкурентам (`submit-xray: победитель ... отстали блоков=1/3/4`, tip победителей 0.0010 —
НИЖЕ нашего, то есть проигрыш по времени, не по цене), и на третьем реверте сработало:

    ERROR II.6 breaker: fast-lane DISABLED after 3 consecutive reverts -> sim fallback

Реверты стоили $0.11, $0.01, $0.01. Отключение fast-lane стоило остатка самого богатого окна
недели, и держится оно ДО РЕСТАРТА. Замысел брейкера с самого начала был «устойчивый шторм,
не разовый lost-race» — но отличить одно от другого код не умел, хотя вердикт `_submit_xray`
вычислялся строкой выше и выбрасывался.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                              # noqa: E402


def reset():
    ex._FL_CONSEC_REVERTS = 0
    ex._FL_DISABLED = False
    ex._FL_TRIP_MSG = None
    ex._FL_PAIR_REVERTS.clear()
    ex._FL_PAIR_OFF.clear()
    ex.FL_BREAKER_SCOPE = "pair"


# --- 1. проигранные гонки НЕ выключают fast-lane ------------------------------------------
reset()
for _ in range(10):
    ex._fl_record(True, 0, 3, xray="race")
assert not ex.fl_global_disabled(), "десять проигранных гонок выключили fast-lane"
assert ex._FL_CONSEC_REVERTS == 0, ex._FL_CONSEC_REVERTS

# --- 2. КОНТРОЛЬ: настоящий шторм по-прежнему выключает ----------------------------------
# Без него «не выключился» неотличимо от «брейкер сломан целиком».
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race")
assert ex.fl_global_disabled(), "настоящий шторм обязан выключать fast-lane"
assert "CIRCUIT BREAKER" in (ex.pop_fast_lane_trip() or ""), "one-shot TG-сообщение потерялось"

# --- 3. неизвестный вердикт считаем — консервативно --------------------------------------
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="unknown")
assert ex.fl_global_disabled(), "при неизвестном вердикте брейкер обязан считать"

# --- 4. смесь копится: гонки не сбрасывают серию настоящих дефектов ----------------------
# Иначе один проигрыш между дефектами глушил бы брейкер навсегда.
reset()
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 0, 3, xray="race")          # гонка: не растит и не сбрасывает
assert ex._FL_CONSEC_REVERTS == 1, ex._FL_CONSEC_REVERTS
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 0, 3, xray="not-race")
assert ex.fl_global_disabled(), "серия дефектов не должна теряться из-за гонки посередине"

# --- 5. успех сбрасывает серию, как и прежде ---------------------------------------------
reset()
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 1, 3)                        # успешный fast-lane
assert ex._FL_CONSEC_REVERTS == 0, ex._FL_CONSEC_REVERTS
ex._fl_record(True, 0, 3, xray="not-race")
assert not ex.fl_global_disabled(), "после успеха серия обязана начинаться заново"

# --- 6. неизвестный исход (таймаут) и не-fast путь не считаются, как и прежде ------------
reset()
for _ in range(5):
    ex._fl_record(True, None, 3, xray="not-race")   # статус неизвестен
    ex._fl_record(False, 0, 3, xray="not-race")     # не fast-lane
assert not ex.fl_global_disabled(), "таймаут и не-fast путь не должны считаться"

# --- 7. вердикты, которые обязан отдавать _submit_xray -----------------------------------
# Проверяем СИГНАТУРУ контракта: без строк-вердиктов пункты выше проверяли бы фикцию.
src = pathlib.Path("chain/execute.py").read_text()
for v in ('return "self-win" if _self else "race"', 'return "not-race"', 'return "unknown"'):
    assert v in src, "у _submit_xray нет ветки %s — брейкеру нечего передавать" % v

# --- 8. #44: своя победа — отдельный вердикт, серию тоже не растит -----------------------
# Победителем бывает НАШ тракт (проба доски/движка/ноги взяла жертву раньше боевой tx).
# Для брейкера класс тот же «не шторм», но вердикт обязан отличаться: иначе наша победа
# копится как проигранная гонка в счётчике оспоренных и в tip-функции конкурентов.
reset()
for _ in range(10):
    ex._fl_record(True, 0, 3, xray="self-win")
assert not ex.fl_global_disabled() and ex._FL_CONSEC_REVERTS == 0, ex._FL_CONSEC_REVERTS
reset()

reset()
print("OK: проигранная гонка не выключает fast-lane, настоящий шторм выключает")

# ==================================================================================================
# ПЕР-ПАРНЫЙ БРЕЙКЕР (20.08). Тот же инцидент 07.08 говорит и второе: дефект ОДНОЙ пары гасил
# fast-lane для ВСЕХ. Пара — естественная единица дефекта (пул, роут, оракул), ею и гасим.
# ==================================================================================================
A = ("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "0x1111111111111111111111111111111111111111")
B = ("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "0x1111111111111111111111111111111111111111")
C = ("0xcccccccccccccccccccccccccccccccccccccccc", "0x1111111111111111111111111111111111111111")

# --- П1. пара A гаснет, пара B ЖИВЁТ --------------------------------------------------------
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race", pair=A)
assert ex.fast_lane_disabled(A), "пара с тремя не-race ревертами обязана погаснуть"
assert not ex.fast_lane_disabled(B), "ЦЕНА ДЕФЕКТА 07.08: соседняя пара гаснуть НЕ обязана"
assert not ex.fl_global_disabled(), "глобальный рубеж (6) тремя ревертами одной пары не берётся"
print("UNIT_OK: пара A погашена, пара B и глобальный путь живы")

# --- П2. ГЛОБАЛЬНЫЙ РУБЕЖ: деградация каскада размазана по парам ---------------------------
# Без него выключение требовало бы 3×числа_пар ревертов — то есть не наступало бы никогда.
reset()
for _pair in (A, B):
    for _ in range(3):
        ex._fl_record(True, 0, 3, xray="not-race", pair=_pair)
assert ex.fl_global_disabled(), "6 не-race ревертов РАЗНЫХ пар обязаны взять глобальный рубеж"
assert ex.fast_lane_disabled(C), "глобальный рубеж перекрывает и нетронутые пары"
assert ex.pop_fast_lane_trip(), "глобальное срабатывание обязано дать сообщение владельцу"
print("UNIT_OK: глобальный рубеж %d берёт деградацию, размазанную по парам" % ex.FL_BREAKER_GLOBAL_N)

# --- П3. race/self-win не считаются НИ В ОДНОМ слое ----------------------------------------
reset()
for _ in range(10):
    ex._fl_record(True, 0, 3, xray="race", pair=A)
    ex._fl_record(True, 0, 3, xray="self-win", pair=A)
assert not ex.fast_lane_disabled(A) and not ex.fl_global_disabled(), "гонки/свои победы не шторм"
assert ex._FL_PAIR_REVERTS.get(("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                               "0x1111111111111111111111111111111111111111")) in (None, 0)
print("UNIT_OK: race/self-win не растят ни пер-парную серию, ни глобальную")

# --- П4. успех сбрасывает серию ПАРЫ (и глобальную) ---------------------------------------
reset()
ex._fl_record(True, 0, 3, xray="not-race", pair=A)
ex._fl_record(True, 0, 3, xray="not-race", pair=A)
ex._fl_record(True, 1, 3, pair=A)                      # успешный fast-lane выстрел
ex._fl_record(True, 0, 3, xray="not-race", pair=A)
ex._fl_record(True, 0, 3, xray="not-race", pair=A)
assert not ex.fast_lane_disabled(A), "успех обязан обрывать серию ПОДРЯД, а не только глобальную"
ex._fl_record(True, 0, 3, xray="not-race", pair=A)
assert ex.fast_lane_disabled(A), "после сброса третий реверт подряд обязан гасить пару"
print("UNIT_OK: успех сбрасывает пер-парную серию; погашенную пару успех НЕ воскрешает")

# --- П5. погашенная пара не воскресает успехом (шторм разбирают, а не переармливают) -------
ex._fl_record(True, 1, 3, pair=A)
assert ex.fast_lane_disabled(A), "погашенная пара обязана оставаться погашенной до рестарта"
print("UNIT_OK: погашенная пара переживает успех (семантика прежняя, как у глобального флага)")

# --- П6. ОТКАТ РУЧКОЙ: FL_BREAKER_SCOPE=global — прежнее поведение бит-в-бит ---------------
reset()
ex.FL_BREAKER_SCOPE = "global"
try:
    for _ in range(3):
        ex._fl_record(True, 0, 3, xray="not-race", pair=A)
    assert ex.fl_global_disabled(), "в global-режиме три реверта ЛЮБОЙ пары гасят ВСЁ (как было)"
    assert ex.fast_lane_disabled(B), "в global-режиме гаснут и нетронутые пары"
    assert not ex._FL_PAIR_OFF, "в global-режиме пер-парный слой обязан быть мёртв"
finally:
    ex.FL_BREAKER_SCOPE = "pair"
print("UNIT_OK: FL_BREAKER_SCOPE=global возвращает прежнее поведение целиком")

# --- П7. РЕВЕРТ БЕЗ ПАРЫ (батч/старый вызыватель) — прежний строгий порог ------------------
# Пер-парный слой его не покрывает, значит поднимать планку до глобальной нельзя: незнание
# обязано оставлять защиту НЕ СЛАБЕЕ прежней.
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race")
assert ex.fl_global_disabled(), "реверт без пары обязан считаться по ПРЕЖНЕМУ порогу"
print("UNIT_OK: реверт без пары не ослабляет брейкер (порог остаётся прежним)")

# --- П8. ПРОВОДКА: горячий путь спрашивает про СВОЮ пару -----------------------------------
_hp = open(pathlib.Path(__file__).parent / "chain" / "hotpath.py").read()
assert "fast_lane_disabled(_fl_pair)" in _hp, "горячий путь спрашивает брейкер без пары"
_ex_src = open(pathlib.Path(__file__).parent / "chain" / "execute.py").read()
assert 'pair=((prep.get("mp") or {}).get("collateralToken")' in _ex_src, \
    "одиночный send не передаёт пару в брейкер"
print("UNIT_OK: проводка — гейт и учёт ходят через пару")

# --- П9. ГАРД ЗАЩИЩАЕТ СЕБЯ САМ: спросить без пары в pair-режиме = получить ЗАПРЕТ ----------
# Прежде погашенная пара при пустом кэше параметров не проверялась ВОВСЕ: вопрос без пары
# отвечал «путь открыт», и защита держалась на чужом предусловии (что кэш всегда полон).
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race", pair=A)
assert ex.fast_lane_disabled(A), "пара погашена"
assert ex.fast_lane_disabled() is True, "вопрос БЕЗ пары в pair-режиме обязан закрывать гард"
assert ex.fl_global_disabled() is False, "при этом глобальный рубеж не взят — двери разные"
ex.FL_BREAKER_SCOPE = "global"
try:
    assert ex.fast_lane_disabled() is False, "в global-режиме вопрос без пары отвечает по флагу"
finally:
    ex.FL_BREAKER_SCOPE = "pair"
print("UNIT_OK: незнание пары закрывает гард; глобальная дверь отвечает отдельно")

# --- П10. КАНАЛ ТРЕВОГИ У ПЕР-ПАРНОГО ТРИПА -------------------------------------------------
# Защёлка до рестарта без сообщения = состояние, о котором узнать неоткуда.
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race", pair=A)
_msg = ex.pop_fast_lane_trip()
assert _msg and "ПАРА" in _msg, "пер-парный трип обязан класть one-shot сообщение: %r" % _msg
assert "рестарт" in _msg.lower(), "сообщение обязано называть способ снятия"
assert ex.pop_fast_lane_trip() is None, "one-shot обязан быть одноразовым"
# ГЛОБАЛЬНОЕ сообщение важнее и перезаписывает пустой слот
reset()
for _p in (A, B):
    for _ in range(3):
        ex._fl_record(True, 0, 3, xray="not-race", pair=_p)
_m2 = ex.pop_fast_lane_trip()
assert _m2 and "ПАРА" not in _m2, "при глобальном трипе сообщение обязано быть глобальным: %r" % _m2
print("UNIT_OK: пер-парный трип виден в TG; глобальный трип не заслоняется пер-парным")
