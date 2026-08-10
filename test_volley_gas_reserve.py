"""Предохранитель залпа: бронь газа на ОТПРАВКЕ + снятие плана на loss-стопе (#93/#94).

#93. Расход рос ТОЛЬКО в `_engine_result`, то есть по пришедшей квитанции. Коллектор разбирает
64 квитанции/с (`COLLECT_BATCH`=64 за `COLLECT_POLL_SEC`=1.0), движок настроен на 400 отправок/с
— учёт отставал ВШЕСТЕРО. Пока триггер не сработал, проба стоит 23 619 газа и отставание
безобидно. Но ПОСЛЕ пересечения триггера проба копирует KB-план и зовёт Morpho: 445-710k.
Полный магазин 110x40 = 4400 проб ≈ $290 против бакета $150 и суточного выключателя $500, а
`check_gas_budget_sane` сверяет только `500 > 40+150` и перелёта не моделирует. Итог: проигранная
гонка сжигала бы бакет и половину суточного выключателя РАНЬШЕ, чем тормоз это увидит — а
сработавший суточный выключатель глушит И БОЕВОЙ путь.

#94. `encode_clear_plan()` был написан и не вызывался НИКЕМ. После проигранной гонки план лежит
на цепи весь TTL (900с) с УЖЕ ВЫПОЛНЕННЫМ триггером: фильтр `px > trig` больше не отсекает, и
каждая следующая проба проходит внутрь на полные 445-710k. Это и есть механизм, которым
проигрыш выжигает бакет — а проигрыш у нас сценарий базовый (0 побед из 9794).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                                   # noqa: E402
import chain.spec_fire_live as sfl                           # noqa: E402

MID = "0x" + "11" * 32
BOR = "0x" + "22" * 20
FEED = "XRP"


def reset():
    ex._VOLLEY_GAS_USD = 0.0
    ex._VOLLEY_GAS_RESERVED = 0.0
    ex._VOLLEY_RESERVE_BY_HASH.clear()


# --- 1. Бронь видна гейтам СРАЗУ, а суточный ФАКТ от неё не растёт ------------------------
reset()
ex.volley_gas_reserve("0xaaa", 40.0)
ex.volley_gas_reserve("0xbbb", 40.0)
assert ex.volley_gas_today() == 0.0, "бронь попала в СУТОЧНЫЙ ФАКТ — она его завысит"
assert ex.volley_gas_committed() == 80.0, "гейт не видит брони — вернулось отставание учёта"

# --- 2. Квитанция пишет ФАКТ и снимает бронь ИМЕННО этой пробы ---------------------------
ex.volley_gas_settle("0xaaa", 0.07)         # реальная холостая проба много дешевле брони
assert abs(ex.volley_gas_today() - 0.07) < 1e-9
assert abs(ex.volley_gas_committed() - (0.07 + 40.0)) < 1e-9, \
    "снята не та бронь (или снято больше занятого)"

# --- 3. КОНТРОЛЬ: повторная квитанция по тому же хэшу бронь второй раз НЕ снимает ---------
ex.volley_gas_settle("0xaaa", 0.07)
assert abs(ex.volley_gas_committed() - (0.14 + 40.0)) < 1e-9, \
    "повторная квитанция снесла чужую бронь — гейт откроется раньше времени"

# --- 4. Таймаут освобождает бронь и НЕ пишет факт (расход неизвестен) ---------------------
ex.volley_gas_settle("0xbbb", 0.0)
assert ex.volley_gas_committed() == ex.volley_gas_today(), "бронь по таймауту зависла навсегда"
assert not ex._VOLLEY_RESERVE_BY_HASH, "словарь брони течёт"

# --- 5. ГЛАВНОЕ: гейт огня закрывается ДО перелёта, а не после ----------------------------
# Магазин 4400 проб по цене каскада. Без брони гейт увидел бы расход только по квитанциям
# (64/с против 400 отправок/с) и пропустил бы весь магазин.
reset()
PER = 290.0 / 4400                          # $ на пробу по замеру каскада (0.0572 gwei, ETH $1918)
sent = 0
for i in range(4400):
    if ex.volley_gas_committed() >= ex.CASCADE_TIER_GAS_USD:
        break                               # ровно то, что делают оба гейта в execute.py
    ex.volley_gas_reserve("0x%04x" % i, PER)
    sent += 1
assert sent < 4400, "гейт пропустил весь магазин — предохранителя нет"
assert sent * PER <= ex.CASCADE_TIER_GAS_USD + PER, \
    "перелёт за бакет: отправлено на $%.2f при бакете $%.2f" % (sent * PER, ex.CASCADE_TIER_GAS_USD)
print("OK 1/3: бронь на отправке — гейт закрылся на %d пробе из 4400 ($%.0f из бакета $%.0f), "
      "суточный ФАКТ не завышен" % (sent, sent * PER, ex.CASCADE_TIER_GAS_USD))

# --- 6. КОНТРОЛЬ «до фикса»: без брони магазин уходит целиком -----------------------------
reset()
sent_old = 0
for i in range(4400):
    if ex.volley_gas_today() >= ex.CASCADE_TIER_GAS_USD:   # прежний гейт: только ФАКТ
        break
    sent_old += 1                                          # квитанций ещё нет — факт равен нулю
assert sent_old == 4400, "контроль не воспроизводит дефект — пример не тот"
print("OK 2/3: КОНТРОЛЬ до фикса — прежний гейт по одному ФАКТУ пропускал все 4400 проб "
      "($%.0f против бакета $%.0f и суточного выключателя $500)" % (4400 * PER, ex.CASCADE_TIER_GAS_USD))


# --- 7. clearPlan зовётся на loss-стопе, и только по СВОЕЙ цели ---------------------------
def mk(board_key):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._logs = []
    sf._log = sf._logs.append
    sf._now = lambda: 1000.0
    sf.stats = {}
    sf.rpc = None
    sf.cfg = type("C", (), {"liquidator_address": "0x" + "ff" * 20})()
    sf._board = {"key": board_key, "verified": True, "dl": 1, "until": 9e9} if board_key else {}
    sf.sent = []
    sf._board_arm_fn = lambda cd, gas, tag: sf.sent.append((tag, cd, gas)) or "0xclear"
    return sf


sf = mk((MID, BOR))
assert sf._clear_board(MID, BOR, FEED, "жертву забрали") is True
assert len(sf.sent) == 1 and sf.sent[0][0] == "clearPlan", sf.sent
assert sf._board == {}, "доска осталась живой после снятия"
assert sf.stats.get("board_cleared") == 1
assert any("СНЯТА" in s for s in sf._logs), "снятие молчит в журнале"

# КОНТРОЛЬ: доска армлена под ЧУЖУЮ цель -> не трогаем (иначе снесём живой чужой план)
sf2 = mk((MID, "0x" + "99" * 20))
assert sf2._clear_board(MID, BOR, FEED, "жертву забрали") is False
assert not sf2.sent and sf2._board, "снят ЧУЖОЙ план — так можно убить живое окно"

# КОНТРОЛЬ: доски нет вовсе -> тихо и без tx
sf3 = mk(None)
assert sf3._clear_board(MID, BOR, FEED, "жертву забрали") is False and not sf3.sent

# СТРУКТУРНО: loss-стоп в боевой петле действительно зовёт снятие
src = pathlib.Path("chain/spec_fire_live.py").read_text()
# вхождений два: первое в _prearm_board, второе — боевой loss-стоп в петле окна.
# Берём ПОСЛЕДНЕЕ: гард обязан смотреть на ту ветку, где цель снимается по факту разбора.
_ls = src.rindex('if probe == "gone"')
assert "_clear_board(" in src[_ls:_ls + 700], "loss-стоп не снимает план с контракта (#94)"
assert src.count('if probe == "gone"') >= 2, "срез потерял одну из веток — гард ослаб"
print("OK 3/3: clearPlan на loss-стопе — своя цель снимается, чужая нет, петля вызов содержит")
print("=== #93/#94 ЗАКРЫТЫ ===")
