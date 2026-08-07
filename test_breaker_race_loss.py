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


# --- 1. проигранные гонки НЕ выключают fast-lane ------------------------------------------
reset()
for _ in range(10):
    ex._fl_record(True, 0, 3, xray="race")
assert not ex.fast_lane_disabled(), "десять проигранных гонок выключили fast-lane"
assert ex._FL_CONSEC_REVERTS == 0, ex._FL_CONSEC_REVERTS

# --- 2. КОНТРОЛЬ: настоящий шторм по-прежнему выключает ----------------------------------
# Без него «не выключился» неотличимо от «брейкер сломан целиком».
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="not-race")
assert ex.fast_lane_disabled(), "настоящий шторм обязан выключать fast-lane"
assert "CIRCUIT BREAKER" in (ex.pop_fast_lane_trip() or ""), "one-shot TG-сообщение потерялось"

# --- 3. неизвестный вердикт считаем — консервативно --------------------------------------
reset()
for _ in range(3):
    ex._fl_record(True, 0, 3, xray="unknown")
assert ex.fast_lane_disabled(), "при неизвестном вердикте брейкер обязан считать"

# --- 4. смесь копится: гонки не сбрасывают серию настоящих дефектов ----------------------
# Иначе один проигрыш между дефектами глушил бы брейкер навсегда.
reset()
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 0, 3, xray="race")          # гонка: не растит и не сбрасывает
assert ex._FL_CONSEC_REVERTS == 1, ex._FL_CONSEC_REVERTS
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 0, 3, xray="not-race")
assert ex.fast_lane_disabled(), "серия дефектов не должна теряться из-за гонки посередине"

# --- 5. успех сбрасывает серию, как и прежде ---------------------------------------------
reset()
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 0, 3, xray="not-race")
ex._fl_record(True, 1, 3)                        # успешный fast-lane
assert ex._FL_CONSEC_REVERTS == 0, ex._FL_CONSEC_REVERTS
ex._fl_record(True, 0, 3, xray="not-race")
assert not ex.fast_lane_disabled(), "после успеха серия обязана начинаться заново"

# --- 6. неизвестный исход (таймаут) и не-fast путь не считаются, как и прежде ------------
reset()
for _ in range(5):
    ex._fl_record(True, None, 3, xray="not-race")   # статус неизвестен
    ex._fl_record(False, 0, 3, xray="not-race")     # не fast-lane
assert not ex.fast_lane_disabled(), "таймаут и не-fast путь не должны считаться"

# --- 7. вердикты, которые обязан отдавать _submit_xray -----------------------------------
# Проверяем СИГНАТУРУ контракта: без строк-вердиктов пункты выше проверяли бы фикцию.
src = pathlib.Path("chain/execute.py").read_text()
for v in ('return "race"', 'return "not-race"', 'return "unknown"'):
    assert v in src, "у _submit_xray нет ветки %s — брейкеру нечего передавать" % v

reset()
print("OK: проигранная гонка не выключает fast-lane, настоящий шторм выключает")
