"""Прогрев доски ПУБЛИКУЕТ probe_board — иначе пойманный транзит выбрасывается (#47).

Ревью 08.08 (CONFIRMED): probe_board заполнялся ТОЛЬКО при пересборке в горячей фазе, то есть
за порогом огня |ret| >= 1.0*dev. А оба детект-пути формируемого блока читают именно его:
pending_detect._tier2 при пустой доске делает `return`, ВЫБРАСЫВАЯ уже обнаруженный транзит,
и note_ws_transmit без доски не может поставить handoff. В задокументированном классе каскадов
(cbXRP: оракул -0.50% при нашем ret -0.43%) оракульный базис держит ret ниже порога огня —
окно армлено (порог арма 0.65*dev), доска прогрета, а транзит проходит мимо в тишине.

_prearm_board СТРОИТ ровно тот probe-dict и раньше его не публиковал. Экзаменуем СКВОЗНОЙ путь:
синтетический транзит в формируемом блоке -> DISPATCH (pending_fire), а не discard. Именно этот
путь молчал в гонке №3. Исполняются НАСТОЯЩИЕ _prearm_board и note_ws_transmit.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.spec_fire_live as sfl                       # noqa: E402

MID = "0xMarketFat"
BOR = "0xBorrowerFat"
FEED = "XRP"
PROBE = {"calldata": "0xabcdef", "gas": 260000}


def mk(now=5000.0):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: now
    sf._logs = []
    sf._log = sf._logs.append
    sf.probe_board = {}
    sf.pending_fire = {}
    sf._ws_handoff_ts = {}
    sf._board_next_try = {}
    sf._board_probe_cache = {}
    sf._windows = {FEED: True}                 # окно армлено (порог арма 0.65*dev — ниже огня)
    sf._board = {}
    sf.stats = {}
    sf._arm_calls = []
    sf._arm_board = lambda p, m, b, f, debt=0.0: sf._arm_calls.append((m, b, f, debt)) or True
    sf._top_fat = lambda feed, direction, max_dist=None, k=1: ([(MID, BOR, 31000.0)], None, None)
    sf._board_ready = lambda m, b: False
    sf._build_fn = lambda m, b, d: dict(PROBE)
    sf._engine = lambda: None                  # движок не поднимаем: экзаменуем детект-путь
    return sf


_saved = sfl.PENDING_WS_HANDOFF
try:
    sfl.PENDING_WS_HANDOFF = True

    # --- 1. СКВОЗНОЙ ПУТЬ: прогрев -> транзит в формируемом блоке -> DISPATCH -------------
    sf = mk()
    sf._prearm_board(FEED, "down", 0.005)
    assert sf._arm_calls == [(MID, BOR, FEED, 31000.0)], sf._arm_calls
    assert FEED in sf.probe_board, "прогрев не опубликовал доску — транзит будет выброшен"
    _b = sf.probe_board[FEED]
    assert _b[0] == MID and _b[1] == BOR and _b[3] == PROBE["calldata"] and _b[4] == PROBE["gas"], _b

    assert sf.note_ws_transmit([MID], src="ws") is True, \
        "транзит армленного фида в формируемом блоке НЕ дошёл до огня (discard вместо dispatch)"
    assert sf.pending_fire.get(FEED) is not None, "handoff не поставлен — окно останется немым"
    assert sf.pending_fire[FEED][0] == MID and sf.pending_fire[FEED][1] == BOR, sf.pending_fire

    # --- 2. КОНТРОЛЬ «до фикса»: пустая доска -> транзит ВЫБРАСЫВАЕТСЯ -------------------
    # Без него п.1 неотличим от «handoff срабатывает всегда».
    sf2 = mk()
    assert sf2.note_ws_transmit([MID], src="ws") is False and not sf2.pending_fire, \
        "handoff сработал без доски — тест не различает дефект"

    # --- 3. КОНТРОЛЬ: чужой рынок не будит окно -----------------------------------------
    sf3 = mk()
    sf3._prearm_board(FEED, "down", 0.005)
    assert sf3.note_ws_transmit(["0xSomeOtherMarket"], src="ws") is False, \
        "транзит чужого агрегатора поднял наше окно"

    # --- 4. КОНТРОЛЬ: протухшая доска не стреляет (возраст > PENDING_WS_BOARD_MAX_AGE) ---
    sf4 = mk()
    sf4._prearm_board(FEED, "down", 0.005)
    sf4._now = lambda: 5000.0 + sfl.PENDING_WS_BOARD_MAX_AGE_SEC + 1
    assert sf4.note_ws_transmit([MID], src="ws") is False, "протухшая доска дала handoff"

    # --- 5. жира в досягаемости нет -> публиковать нечего, ошибки нет --------------------
    sf5 = mk()
    sf5._top_fat = lambda feed, direction, max_dist=None, k=1: ([], None, None)
    sf5._prearm_board(FEED, "down", 0.005)
    assert FEED not in sf5.probe_board and not sf5._arm_calls

    # --- 6. осечка сборки пробы не публикует мусор --------------------------------------
    sf6 = mk()
    sf6._build_fn = lambda m, b, d: None
    sf6._prearm_board(FEED, "down", 0.005)
    assert FEED not in sf6.probe_board, "не-dict проба попала на доску"
finally:
    sfl.PENDING_WS_HANDOFF = _saved

# --- 8. HIGH N1 (ревью 09.08): доска ЖИВОГО плана публикуется и в окнах 2..N -------------
# Ранний выход по _board_ready стоял ДО публикации, и после фикса #57 стал ЖИВЫМ путём: окна
# 2..N каскада доходят сюда гарантированно (план жив по TTL ~870с), а запись первого окна снял
# его же finally. Освежение отметки раз в 4с не спасает — обновлять нечего.
_saved2 = sfl.PENDING_WS_HANDOFF
try:
    sfl.PENDING_WS_HANDOFF = True

    # окно 1: обычная публикация + кэш пробы
    sf7 = mk()
    sf7._prearm_board(FEED, "down", 0.005)
    assert FEED in sf7.probe_board and sf7._board_probe_cache, "окно 1 не наполнило кэш пробы"

    # окно 2 того же фида: план ЖИВ, доска снята finally окна 1, сборка НЕ зовётся
    sf7.probe_board.pop(FEED, None)
    sf7._board_ready = lambda m, b: True                 # план жив по TTL
    _built = []
    sf7._build_fn = lambda m, b, d: _built.append(1) or {"calldata": "0xNEW", "gas": 1}
    sf7._arm_calls.clear()
    sf7._prearm_board(FEED, "down", 0.005)
    assert FEED in sf7.probe_board, \
        "окно 2 ушло в ранний выход БЕЗ публикации — транзит снова выбрасывается (HIGH N1)"
    assert sf7.probe_board[FEED][3] == PROBE["calldata"], "опубликована не та проба"
    assert _built == [], "под живым планом пересобрали пробу — платим за то, что уже есть"
    assert sf7._arm_calls == [], "под живым планом ушёл лишний setPlan"
    # и транзит доходит до огня
    assert sf7.note_ws_transmit([MID], src="ws") is True and sf7.pending_fire.get(FEED)

    # кэша нет (рестарт под живым планом) -> НЕ выходим молча, а собираем ради публикации
    sf8 = mk()
    sf8._board_ready = lambda m, b: True
    sf8._prearm_board(FEED, "down", 0.005)
    assert FEED in sf8.probe_board, "без кэша окно осталось без доски — фикс неполон"
finally:
    sfl.PENDING_WS_HANDOFF = _saved2

# --- 7. второй потребитель доски (pending-detect tier-2) читает тот же ключ -------------
_pd = pathlib.Path("chain/pending_detect.py").read_text()
assert 'getattr(self.sfl, "probe_board", {}).get(label)' in _pd, \
    "tier-2 больше не читает probe_board — сквозной путь надо перепроверить"

print("OK: прогрев публикует доску, транзит формируемого блока доходит до огня")
