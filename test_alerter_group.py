"""Алертер шлёт и владельцу, и в группу флота — и НЕ теряет владельца, если группа мертва.

Повод (09.08): бот ClaudeLiquidator_bot не состоит в группе -1002408984740, Telegram отвечает
«chat not found» на любую отправку туда. Это чинится ЧЕЛОВЕКОМ (добавить бота), но проводка
обязана быть готова заранее и, главное, отказ группы не должен глотать доставку владельцу:
приёмка залпа и пейдж leg_maxfee_mute — это то, ради чего канал существует.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from alerts import Alerter                                # noqa: E402

sent = []


def mk(group=None, fail_for=None):
    a = Alerter("tok", 111, antiflood_sec=0, tag="base", group_id=group)
    a._post = lambda cid, text: (sent.append((str(cid), text)) or (str(cid) != str(fail_for)))
    return a


# 1. без группы — только владелец (прежнее поведение бит-в-бит)
sent.clear()
assert mk().send("x", key="k") is True
assert [c for c, _ in sent] == ["111"], sent

# 2. с группой — оба адресата, тег на месте
sent.clear()
assert mk(group="-1002408984740").send("тревога", key="k") is True
assert [c for c, _ in sent] == ["111", "-1002408984740"], sent
assert all(t.startswith("[base] ") for _, t in sent), sent

# 3. ГЛАВНОЕ: мёртвая группа («chat not found») НЕ отменяет доставку владельцу
sent.clear()
a = mk(group="-1002408984740", fail_for="-1002408984740")
assert a.send("тревога", key="k") is True, "отказ группы съел возврат доставки владельцу"
assert ("111", "[base] тревога") in sent

# 4. КОНТРОЛЬ: мёртвый ВЛАДЕЛЕЦ честно даёт False (иначе тест не различал бы успех)
sent.clear()
assert mk(group="-1002408984740", fail_for="111").send("y", key="k") is False

# 5. антифлуд считает СОБЫТИЕ, а не адресата (группа не удваивает окно)
sent.clear()
a = Alerter("tok", 111, antiflood_sec=600, group_id="-100")
a._post = lambda cid, text: sent.append((str(cid), text)) or True
a.send("first", key="same")
a.send("second", key="same")
assert [t for _, t in sent] == ["first", "first"], sent

print("OK: алертер шлёт обоим, мёртвая группа не отменяет владельца")
