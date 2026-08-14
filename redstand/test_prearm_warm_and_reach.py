"""Прогрев вне гейта арма + досягаемость не от статической dev (#90/#92).

Аудит 10.08 назвал это ОДНИМ узлом отказа на весь тракт, а не тремя причинами.

1. Все пять точек создания движка стояли НИЖЕ проверки досягаемости в `_prearm_board`, и
   безусловного фонового прогрева не было вовсе. Значит один гейт решал и «класть ли план», и
   «греть ли флот»: осечка = разом ни доски, ни движка, ни лестниц, ни залпа, следующая попытка
   через 240с. Замер: `engine=—` во ВСЕХ close-строках за сутки, последняя подпись лестницы
   07.08 18:41, движок поднимался 5 раз за всю историю. Подпись 110 лестниц стоит 28-32с и
   обязана идти в спокойное время, а не в горячей фазе.

2. Досягаемость считалась `3 x dev`, где `dev` — СТАТИЧЕСКАЯ константа фида (BTC 0.10%,
   ETH 0.15%, альты 0.50%), и не смотрела на живое движение. Порог выходил 0.30%/0.45%/1.50%
   при сегодняшних расстояниях BTC 2.01-2.29%, XRP 2.24%, DOGE 2.82-3.27% — план не клался
   НИКОГДА.

3. Ветка «целей нет» — самая частая — была ЕДИНСТВЕННОЙ без переспроса, при том что попытка
   одна на окно и делается в момент t~0, когда |ret| минимален по построению. Осечка сборки
   ретраилась за 5с, а эта не ретраилась вовсе.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.spec_fire_live as sfl                           # noqa: E402

MID = "0x" + "11" * 32
BOR = "0x" + "22" * 20
FEED = "XRP"
DEV = 0.005                     # альтовая dev 0.50% -> прежняя досягаемость 1.50%


def mk(targets):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: 5000.0
    sf._logs = []
    sf._log = sf._logs.append
    sf.probe_board = {}
    sf.pending_fire = {}
    sf._ws_handoff_ts = {}
    sf._board_next_try = {}
    sf._board_probe_cache = {}
    sf._board = {}
    sf.stats = {}
    sf.warmed = []
    sf.reach_seen = []
    sf._engine = lambda: sf.warmed.append(1)
    sf._arm_board = lambda p, m, b, f, debt=0.0: True
    sf._board_ready = lambda m, b: False
    sf._build_fn = lambda m, b, d: {"calldata": "0xabcd", "gas": 260000}

    def _top(feed, direction, max_dist=None, k=1):
        sf.reach_seen.append(max_dist)
        return (targets, None, None)
    sf._top_fat = _top
    return sf


# --- 1. ГЛАВНОЕ: склад греется, даже когда целей в досягаемости НЕТ ----------------------
sf = mk([])
sf._prearm_board(FEED, "down", DEV)
assert sf.warmed, "целей нет -> флот не греется: тот самый один узел отказа на весь тракт"
assert sf._board_next_try.get(FEED), \
    "самая частая ветка отказа снова без переспроса — окно молчит следующие 240с"
assert any("вне досягаемости" in s for s in sf._logs), "причина отказа не названа в журнале"
print("OK 1/4: целей нет — склад набит, переспрос взведён, причина в журнале")

# --- 2. Досягаемость НЕ ниже порога, которым окно само себя армит ------------------------
assert sf.reach_seen and sf.reach_seen[0] >= sfl.SPEC_FIRE_ARM_MAX_DIST - 1e-12, \
    "досягаемость %.4f ниже порога арма окна %.4f — план не ляжет никогда" % (
        sf.reach_seen[0], sfl.SPEC_FIRE_ARM_MAX_DIST)
_old = DEV * (sfl.SPEC_FIRE_THRESHOLD + sfl.SPEC_FIRE_DIST_MARGIN_DEVS)
assert sf.reach_seen[0] > _old, "новая досягаемость не шире прежней (%.4f)" % _old
print("OK 2/4: досягаемость %.2f%% против прежних %.2f%% (порог арма окна %.2f%%)" % (
    sf.reach_seen[0] * 100, _old * 100, sfl.SPEC_FIRE_ARM_MAX_DIST * 100))

# --- 3. КОНТРОЛЬ: у волатильного фида широкая dev по-прежнему ПОБЕЖДАЕТ пол ---------------
# Пол не должен ЗАЖИМАТЬ фид, чья собственная dev шире: иначе в настоящий шторм мы сузим обзор.
sf3 = mk([])
_wide = 0.05                                    # dev 5% -> 3xdev = 15%
sf3._prearm_board(FEED, "down", _wide)
assert sf3.reach_seen[0] > sfl.SPEC_FIRE_ARM_MAX_DIST, \
    "пол зажал широкую dev — в шторм обзор сузится"
assert abs(sf3.reach_seen[0] - _wide * (sfl.SPEC_FIRE_THRESHOLD
                                        + sfl.SPEC_FIRE_DIST_MARGIN_DEVS)) < 1e-12
print("OK 3/4: КОНТРОЛЬ — широкая dev (5%%) даёт %.1f%%, пол её не зажимает" % (
    sf3.reach_seen[0] * 100))

# --- 4. КОНТРОЛЬ: цель есть -> прежний путь цел (арм, публикация доски, прогрев) ----------
sf4 = mk([(MID, BOR, 31000.0)])
sf4._prearm_board(FEED, "down", DEV)
assert sf4.warmed, "склад не набит на успешном пути — сломан прежний ход"
assert FEED in sf4.probe_board, "доска не опубликована (регрессия #47)"
assert not sf4._board_next_try.get(FEED), "успешный арм поставлен на переспрос"

# СТРУКТУРНО: прогрев стоит ВЫШЕ проверки целей, а не под ней
src = pathlib.Path("chain/spec_fire_live.py").read_text()
i = src.index("    def _prearm_board(")
blk = src[i:src.index("\n    def ", i + 10)]
assert blk.index("self._engine()") < blk.index("if not tgts:"), \
    "прогрев снова НИЖЕ гейта целей — узел отказа вернулся"
print("OK 4/4: цель есть — арм и публикация целы; прогрев структурно выше гейта целей")
print("=== #90/#92 ЗАКРЫТЫ ===")
