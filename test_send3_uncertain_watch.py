"""Пост-контроль SEND-3 uncertain (дефект №2 вскрытия каскада 06.08): «учтён sent» без
положительного доказательства посадки держал жертву под дедупом 12с = всё каскадное окно.
Инварианты: дроп снимает дедуп; посадка (receipt ИЛИ съеденный nonce) не трогает; свежую
метку более новой отправки дроп не снимает."""
import types
from chain import execute as ex


class _Eth:
    def __init__(self, count, receipt=None):
        self._count = count
        self._receipt = receipt

    def get_transaction_receipt(self, h):
        if self._receipt is None:
            raise Exception("not found")
        return self._receipt

    def get_transaction_count(self, bot, block="latest"):
        return self._count


def _w3(count, receipt=None):
    return types.SimpleNamespace(eth=_Eth(count, receipt))


_nosleep = lambda s: None
BOT = "0xBOT"

# 1. дроп: nonce НЕ съеден за все проверки -> дедуп обеих жертв юнита снят
ex._RECENT_SENDS.clear()
ex._RECENT_SENDS[("m1", "b1")] = 111.0
ex._RECENT_SENDS[("m2", "b2")] = 222.0
r = ex._watch_uncertain(_w3(count=7), BOT, 7, "0xh", [("m1", "b1"), ("m2", "b2")], sleep_fn=_nosleep)
assert r == "dropped", r
assert ("m1", "b1") not in ex._RECENT_SENDS and ("m2", "b2") not in ex._RECENT_SENDS
assert ex._UNCERTAIN_DROPPED["n"] >= 1
print("UNIT_OK: дроп снимает дедуп жертв юнита")

# 2. посадка по nonce: count > tx_nonce -> landed, дедуп цел
ex._RECENT_SENDS[("m1", "b1")] = 111.0
r = ex._watch_uncertain(_w3(count=8), BOT, 7, "0xh", [("m1", "b1")], sleep_fn=_nosleep)
assert r == "landed" and ex._RECENT_SENDS.get(("m1", "b1")) == 111.0
print("UNIT_OK: nonce съеден = landed, дедуп цел")

# 3. посадка по receipt (nonce-нога не понадобилась)
r = ex._watch_uncertain(_w3(count=7, receipt={"status": 1}), BOT, 7, "0xh", [("m1", "b1")], sleep_fn=_nosleep)
assert r == "landed" and ex._RECENT_SENDS.get(("m1", "b1")) == 111.0
print("UNIT_OK: receipt = landed")

# 4. более новая отправка освежила метку ПОСЛЕ снапшота -> дроп её НЕ снимает (нет двойного огня)
ex._RECENT_SENDS[("m1", "b1")] = 111.0
def _sleep_bump(s):
    ex._RECENT_SENDS[("m1", "b1")] = 999.0     # новая легитимная отправка во время ожидания
r = ex._watch_uncertain(_w3(count=7), BOT, 7, "0xh", [("m1", "b1")], sleep_fn=_sleep_bump)
assert r == "dropped" and ex._RECENT_SENDS.get(("m1", "b1")) == 999.0
print("UNIT_OK: метка новой отправки не тронута")

# 5. дефолт флага = ON (канон: положительное доказательство посадки обязательно)
assert ex.SEND3_UNCERTAIN_WATCH is True
ex._RECENT_SENDS.clear()
print("ПРОШЛИ: SEND-3 пост-контроль uncertain")
