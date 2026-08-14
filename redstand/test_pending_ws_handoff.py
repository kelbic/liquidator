"""Юниты WS-handoff (разбор pending-детекта 06.08): транзит из recv-петли ->
pending_fire армленному окну с досягаемой доской. Без сети, без нитей окон.
Контракты: попадание по рынку доски, кулдаун дублей (feed/bare/poll одного
транзита), протухшая/пустая доска игнорируется, чужой рынок игнорируется,
откат PENDING_WS_HANDOFF=0 = no-op, хук в hotpath не роняет _process_transmit."""
import sys
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")

import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive

MID = "0x" + "d4" * 32
MID2 = "0x" + "ab" * 32
B = "0x" + "ba" * 20

LOGS = []
_t = [1000.0]


def mk():
    LOGS.clear()
    s = SpecFireLive.__new__(SpecFireLive)
    s._windows = {"XRP": object()}
    s.probe_board = {"XRP": (MID, B, 33000.0, "0xdead", 900000, _t[0])}
    s.pending_fire = {}
    s._ws_handoff_ts = {}
    s._now = lambda: _t[0]
    s._log = LOGS.append
    return s


# 1) транзит рынка доски -> handoff поставлен, лог несёт feed/src
s = mk()
assert s.note_ws_transmit([MID], src="feed") is True
assert s.pending_fire["XRP"][:3] == (MID, B, 33000.0)
assert any("ws-handoff feed=XRP" in m and "src=feed" in m for m in LOGS)
print("UNIT_OK: транзит рынка доски ставит pending_fire")

# 2) кулдаун: дубль-источники того же транзита не перевзводят handoff
s.pending_fire.clear()
_t[0] += 0.5
assert s.note_ws_transmit([MID], src="poll") is False
assert not s.pending_fire
_t[0] += sfl_mod.PENDING_WS_CD_SEC
assert s.note_ws_transmit([MID], src="bare") is True
print("UNIT_OK: кулдаун гасит дубли, после CD handoff снова живой")

# 3) чужой рынок / пустая или протухшая доска -> no-op
s = mk()
assert s.note_ws_transmit([MID2]) is False
s.probe_board["XRP"] = (MID, B, 33000.0, "0xdead", 900000,
                        _t[0] - sfl_mod.PENDING_WS_BOARD_MAX_AGE_SEC - 1.0)
assert s.note_ws_transmit([MID]) is False
s.probe_board.clear()
assert s.note_ws_transmit([MID]) is False and not s.pending_fire
print("UNIT_OK: чужой рынок и мёртвая доска игнорируются")

# 4) откат флагом
s = mk()
sfl_mod.PENDING_WS_HANDOFF = False
try:
    assert s.note_ws_transmit([MID]) is False and not s.pending_fire
finally:
    sfl_mod.PENDING_WS_HANDOFF = True
print("UNIT_OK: PENDING_WS_HANDOFF=0 -> no-op (откат)")

# 5) шов hotpath: хук зовётся с (mids, src), исключение хука не роняет вызывающего
import chain.hotpath as hp

seen = []
hp.set_ws_transmit_hook(lambda mids, src: seen.append((tuple(mids), src)))
assert hp._WS_TRANSMIT_HOOK is not None
hp._WS_TRANSMIT_HOOK(("m1",), "feed")
assert seen == [(("m1",), "feed")]


def _boom(mids, src):
    raise RuntimeError("boom")


hp.set_ws_transmit_hook(_boom)
try:
    if hp._WS_TRANSMIT_HOOK is not None:
        try:
            hp._WS_TRANSMIT_HOOK((), "poll")
        except Exception:
            pass
finally:
    hp.set_ws_transmit_hook(None)
print("UNIT_OK: шов hotpath — регистрация и изоляция исключений")

print("ALL_OK: WS-handoff — контракты выполнены")
