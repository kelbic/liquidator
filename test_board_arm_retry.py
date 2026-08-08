"""Осечка арма доски переспрашивается БЫСТРО — иначе окно короче каденса остаётся немым (#57).

Ревью 08.08 (CONFIRMED): арм доски — ЕДИНСТВЕННАЯ точка входа флота в бой. Право огня движку
даёт только живой СЕВШИЙ план (_engine_fat_keepalive), а V1-деградация сидит за тир-гейтом,
ложным в решающий блок по построению (это и был дефект 8ea97ca). При каденсе переарма 240с
осечка ОДНОГО setPlan на старте окна оставляла весь флот на 0 проб в решающем блоке — при
живом и «здоровом» на вид движке, без единой громкой строки.

Исполняются НАСТОЯЩИЕ _prearm_board и _arm_retry; швы — сборка пробы и запись плана.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.spec_fire_live as sfl                       # noqa: E402

MID, BOR, FEED = "0xMarketFat", "0xBorrowerFat", "XRP"
NOW = 5000.0


def mk(arm_ok=True, probe=True):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: NOW
    sf._logs = []
    sf._log = sf._logs.append
    sf.probe_board = {}
    sf.stats = {}
    sf._board_next_try = {}
    sf._windows = {FEED: True}
    sf._top_fat = lambda feed, direction, max_dist=None, k=1: ([(MID, BOR, 31000.0)], None, None)
    sf._board_ready = lambda m, b: False
    sf._build_fn = (lambda m, b, d: {"calldata": "0x01", "gas": 260000}) if probe else \
                   (lambda m, b, d: None)
    sf._arm_board = lambda p, m, b, f, debt=0.0: arm_ok
    sf._engine = lambda: None
    return sf


# --- 1. АРМ НЕ ВЗВЁЛСЯ: быстрый переспрос, громкая строка, счётчик ----------------------
sf = mk(arm_ok=False)
sf._prearm_board(FEED, "down", 0.005)
_next = sf._board_next_try.get(FEED)
assert _next is not None, "осечка арма не назначила переспрос — окно останется без доски"
assert _next == NOW + sfl.SPEC_FIRE_V2_ARM_RETRY_SEC, _next
assert _next < NOW + sfl.SPEC_FIRE_V2_REARM_SEC, \
    "переспрос назначен по каденсу переарма (%.0fс) — окно короче него остаётся немым" % \
    sfl.SPEC_FIRE_V2_REARM_SEC
assert sf.stats.get("board_arm_fails") == 1, sf.stats
assert any("арм НЕ ВЗВЁЛСЯ" in m for m in sf._logs), sf._logs

# --- 2. КОНТРОЛЬ: успешный арм НЕ дёргает быстрый ретрай -------------------------------
# Без него п.1 неотличим от «ретрай назначается всегда» (флот бы переармливался вечно).
sf2 = mk(arm_ok=True)
sf2._prearm_board(FEED, "down", 0.005)
assert FEED not in sf2._board_next_try, "успешный арм назначил переспрос: %s" % sf2._board_next_try
assert not sf2.stats.get("board_arm_fails"), sf2.stats
assert FEED in sf2.probe_board, "успешный арм обязан опубликовать доску (#47)"

# --- 3. проба не собралась -> тоже переспрос (осечка сборки = та же немота флота) -------
sf3 = mk(probe=False)
sf3._prearm_board(FEED, "down", 0.005)
assert sf3._board_next_try.get(FEED) == NOW + sfl.SPEC_FIRE_V2_ARM_RETRY_SEC
assert sf3.stats.get("board_arm_fails") == 1

# --- 4. исключение внутри прогрева -> переспрос, а не тихая потеря окна -----------------
sf4 = mk()
sf4._build_fn = lambda m, b, d: (_ for _ in ()).throw(RuntimeError("RPC down"))
sf4._prearm_board(FEED, "down", 0.005)
assert sf4._board_next_try.get(FEED) == NOW + sfl.SPEC_FIRE_V2_ARM_RETRY_SEC
assert any("prearm fail" in m for m in sf4._logs), sf4._logs

# --- 5. жира в досягаемости нет -> это НЕ осечка, переспрос не нужен --------------------
sf5 = mk()
sf5._top_fat = lambda feed, direction, max_dist=None, k=1: ([], None, None)
sf5._prearm_board(FEED, "down", 0.005)
assert FEED not in sf5._board_next_try and not sf5.stats.get("board_arm_fails"), \
    "пустой топ — не осечка арма, переспрашивать нечего"

# --- 6. петля окна ЧИТАЕТ расписание из _board_next_try, а не из локальной переменной ---
# Иначе быстрый ретрай никогда не доедет до планировщика (дефект жил бы дальше).
src = pathlib.Path("chain/spec_fire_live.py").read_text()
assert "self._board_next_try.get(feed, next_board_try)" in src, \
    "петля окна не читает _board_next_try — переспрос не попадёт в расписание"
assert "self._board_next_try[feed] = _n + SPEC_FIRE_V2_REARM_SEC" in src, \
    "петля окна не пишет каденс переарма в общее расписание"

print("OK: осечка арма доски переспрашивается быстро, успешный арм не дёргает ретрай")
