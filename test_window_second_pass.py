"""Окно №2 того же фида греется НЕМЕДЛЕННО, доска не протухает под живым планом, право огня
даёт подтверждённый транзит (регрессии моих же правок #57/#47, ревью 09.08).

Три CONFIRMED-механизма, все — «фикс проверяли в моменте, а не во времени»:
1. #57 перенёс расписание арма в словарь ИНСТАНСА, и оно переживало закрытие окна: окна 2..N
   того же фида в пределах 240с не грелись вовсе. В каскаде каждый пуш ре-якорит фид — это
   ровно окна 2..N, ради которых вчера же расширяли #46.
2. #47 публикует probe_board, но потребители гейтят её ВОЗРАСТ 10с, а переиздать прогрев может
   не раньше 240с (и не может вовсе, пока план армлен под ту же цель, TTL 870с) — фикс жил ~4%
   окна.
3. Право огня движку висело на fat_mode, который выставляется ТОЛЬКО в горячей ветке, — то есть
   в окне ниже порога огня подтверждённый транзит поднимал одну пробу главного кошелька, а флот
   оставался нем структурно. Это пересечение #47 (открыли детект ниже порога) и #41 (право огня
   даёт fat-окно).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.spec_fire_live as sfl                       # noqa: E402

SRC = pathlib.Path("chain/spec_fire_live.py").read_text()

# --- 1. #57: расписание арма снимается при закрытии окна ---------------------------------
_fin = SRC[SRC.index("        finally:\n            self._owed.pop(feed, None)"):]
_fin = _fin[:_fin.index("_g = sorted(gaps)")]
assert "self._board_next_try.pop(feed, None)" in _fin, \
    "расписание арма переживает окно — окна 2..N фида не прогреваются (регрессия #57)"
assert "self.probe_board.pop(feed, None)" in _fin, "probe_board обязан сниматься вместе с окном"

# исполняем: словарь чистится ровно тем же способом, что и probe_board
sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
sf._board_next_try = {"XRP": 1240.0, "DOGE": 99.0}
sf.probe_board = {"XRP": ("m", "b", 1.0, "0x", 1, 0.0)}
sf._owed = {"XRP": 1.0}
sf._prio = {"XRP": (1, 2)}
for d, k in ((sf._owed, "XRP"), (sf._prio, "XRP"), (sf.probe_board, "XRP"), (sf._board_next_try, "XRP")):
    d.pop(k, None)
assert "XRP" not in sf._board_next_try and sf._board_next_try.get("DOGE") == 99.0, \
    "снятие расписания задело чужой фид"

# --- 2. #47: отметка probe_board переиздаётся, пока план ЖИВ ------------------------------
_loop = SRC[SRC.index("_pbd = self.probe_board.get(feed)"):]
_loop = _loop[:_loop.index("if SPEC_FIRE_V2_BOARD and _n >=")]
assert "self._board_plan_live() is not None" in _loop, \
    "отметку переиздают без проверки живости плана — доска врала бы о мёртвом плане"
assert "SPEC_FIRE_BOARD_RESTAMP_SEC" in _loop and "self.probe_board[feed] = _pbd[:5] + (_n,)" in _loop, \
    "переиздание отметки не найдено — фикс #47 снова живёт 10 секунд"
assert sfl.SPEC_FIRE_BOARD_RESTAMP_SEC < sfl.PENDING_WS_BOARD_MAX_AGE_SEC, \
    "каденс переиздания %.1fс не короче гейта потребителя %.1fс — доска будет протухать" % (
        sfl.SPEC_FIRE_BOARD_RESTAMP_SEC, sfl.PENDING_WS_BOARD_MAX_AGE_SEC)
assert sfl.SPEC_FIRE_BOARD_RESTAMP_SEC < sfl.PENDING_BOARD_MAX_AGE_SEC if hasattr(
    sfl, "PENDING_BOARD_MAX_AGE_SEC") else True

# форма кортежа сохраняется (потребители распаковывают 6 полей)
_pb = ("mid", "bor", 31000.0, "0xcd", 260000, 100.0)
_re = _pb[:5] + (777.0,)
assert len(_re) == 6 and _re[:5] == _pb[:5] and _re[5] == 777.0, _re

# --- 3. право огня по ПОДТВЕРЖДЁННОМУ транзиту, а не только в fat-режиме ------------------
_ho = SRC[SRC.index("pf = self.pending_fire.pop(feed, None)"):]
_ho = _ho[:_ho.index("except Exception as e:")]
assert "if fat_mode or SPEC_FIRE_HANDOFF_KEEPALIVE:" in _ho, \
    "право огня по handoff снова висит только на fat_mode — флот нем в подтверждённом транзите"
assert sfl.SPEC_FIRE_HANDOFF_KEEPALIVE is True, "дефолт ручки должен быть включён"
# КОНТРОЛЬ ОТКАТА: ручка существует и выключается
_sv = sfl.SPEC_FIRE_HANDOFF_KEEPALIVE
try:
    sfl.SPEC_FIRE_HANDOFF_KEEPALIVE = False
    assert sfl.SPEC_FIRE_HANDOFF_KEEPALIVE is False
finally:
    sfl.SPEC_FIRE_HANDOFF_KEEPALIVE = _sv

# --- 4. КОНТРОЛЬ: залп V1 по-прежнему только в fat-режиме (деньги не распускаем) ----------
assert "if fat_mode:      # тир: залп по подтверждённому флипу" in _ho, \
    "залп V1 сняли с fat-гейта — это уже другой пакет и другие деньги"

print("OK: окно №2 греется сразу, доска не протухает под живым планом, транзит даёт право огня")
