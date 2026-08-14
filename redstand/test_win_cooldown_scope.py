"""WIN-пауза: триггер = боевая ПОДТВЕРЖДЁННАЯ победа, скоуп = пер-фид (#46, решение kelbic).

Ревью 08.08 (две линзы независимо, обе CONFIRMED): кулдаун 900с взводила ЛЮБАЯ победа, включая
1-байтовую пробу движка, и гасил он арм ВСЕХ фидов. В каскаде (по определению >=4 армов/час)
это отдавало стене ноги 2+: каждый пуш ре-якорит и дизармит, новой ноге нужен новый arm, а он
заблокирован; pending-detect опрашивает только armed-окна — слепнет заодно.

Решение владельца: (1) триггер только боевая победа с ПОЛОЖИТЕЛЬНЫМ доказательством захвата,
не «uncertain» — фантом-победа глушила бы нас посреди живой возможности; (2) скоуп per-feed;
(3) обе ручки в env с откатом; TTL 900с этим пакетом не трогаем.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.spec_fire_live as sfl                       # noqa: E402

NOW = 1000.0


def mk():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: NOW
    sf._logs = []
    sf._log = sf._logs.append
    sf._cooldown_until = 0.0
    sf._cooldown_feed = {}
    return sf


_saved = (sfl.SPEC_FIRE_COOLDOWN_TRIGGER, sfl.SPEC_FIRE_COOLDOWN_SCOPE)
try:
    # ---- ДЕФОЛТ: триггер combat, скоуп feed --------------------------------------------
    sfl.SPEC_FIRE_COOLDOWN_TRIGGER, sfl.SPEC_FIRE_COOLDOWN_SCOPE = "combat", "feed"

    # 1. победа ПРОБЫ (в т.ч. 1-байтовой движка) паузу НЕ ставит — инвентарь не потрачен
    sf = mk()
    assert sf._set_cooldown("XRP", "probe", proven=True) is False, \
        "победа пробы снова гасит флот — самопоражение, ради которого всё и чинилось"
    assert not sf._cooldown_active("XRP") and not sf._cooldown_active("DOGE")

    # 2. боевая ПОДТВЕРЖДЁННАЯ победа гасит ТОЛЬКО свой фид
    sf = mk()
    assert sf.note_combat_win("XRP", proven=True) is True
    assert sf._cooldown_active("XRP"), "боевая победа не взвела паузу вовсе"
    assert not sf._cooldown_active("DOGE"), \
        "пауза по XRP погасила DOGE — ноги 2+ каскада снова уступаются стене"
    assert sf._cooldown_active("XRP") is True and sf._now() == NOW
    sf._now = lambda: NOW + sfl.SPEC_FIRE_WIN_COOLDOWN_SEC + 1
    assert not sf._cooldown_active("XRP"), "пауза не истекает по TTL"

    # 3. НЕподтверждённая боевая победа (SEND-3 uncertain) паузу НЕ ставит
    sf = mk()
    assert sf.note_combat_win("XRP", proven=False) is False, \
        "фантом-победа взвела паузу — заглушит нас посреди живой возможности"
    assert not sf._cooldown_active("XRP")

    # 4. пер-фид без фида гасить нечего (не роняем и не гасим глобально по ошибке)
    sf = mk()
    assert sf.note_combat_win(None, proven=True) is False
    assert not sf._cooldown_active("XRP")

    # ---- ОТКАТ 1: TRIGGER=any -> прежнее «любая победа» --------------------------------
    sfl.SPEC_FIRE_COOLDOWN_TRIGGER = "any"
    sf = mk()
    assert sf._set_cooldown("XRP", "probe", proven=True) is True, "откат триггера не работает"
    assert sf._cooldown_active("XRP") and not sf._cooldown_active("DOGE")

    # ---- ОТКАТ 2: SCOPE=global -> прежнее «все фиды» -----------------------------------
    sfl.SPEC_FIRE_COOLDOWN_SCOPE = "global"
    sf = mk()
    assert sf._set_cooldown("XRP", "probe", proven=True) is True
    assert sf._cooldown_active("XRP") and sf._cooldown_active("DOGE"), \
        "откат скоупа не работает — прежнее поведение недостижимо"
finally:
    sfl.SPEC_FIRE_COOLDOWN_TRIGGER, sfl.SPEC_FIRE_COOLDOWN_SCOPE = _saved

# ---- 5. СТРУКТУРНО: скалярная пауза больше не ставится напрямую из путей побед ---------
src = pathlib.Path("chain/spec_fire_live.py").read_text()
assert "self._cooldown_until = self._now() + SPEC_FIRE_WIN_COOLDOWN_SEC" not in src, \
    "путь победы снова ставит глобальную паузу мимо ручек"
assert src.count('self._set_cooldown(feed, "probe"') == 3, \
    "не все три пути победы пробы переведены на ручки"
assert "if self._cooldown_active(feed):" in src, "арм читает паузу мимо пер-фид скоупа"

print("OK: паузу ставит только подтверждённая боевая победа, гасит только свой фид")

# --- 6. #58: боевая победа доходит до паузы ЧЕРЕЗ ХУК (механизм больше не вакуумный) -----
import chain.execute as ex                                # noqa: E402

sf = mk()
sf.probe_board = {"XRP": ("0xMID", "0xBOR", 1.0, "0x", 1, 0.0)}
sf._board = {"key": ("0xMID", "0xBOR")}
sf._windows = {"XRP": True}
_sv_hook = ex._COMBAT_WIN_HOOK
try:
    ex.set_combat_win_hook(sf.note_combat_win_by_market)
    assert ex._note_combat_win("0xMID", "0xBOR", proven=True) is True, "хук не довёл победу"
    assert sf._cooldown_active("XRP"), "боевая победа не взвела паузу на своём фиде"
    assert not sf._cooldown_active("DOGE"), "пауза утекла на соседний фид"

    # КОНТРОЛЬ: неизвестный рынок паузу НЕ ставит и не падает (скоуп пер-фид)
    sf2 = mk()
    sf2.probe_board = {}
    sf2._board = {}
    sf2._windows = {}
    ex.set_combat_win_hook(sf2.note_combat_win_by_market)
    assert ex._note_combat_win("0xUNKNOWN", "0xB", proven=True) is False
    assert any("фид для рынка" in m for m in sf2._logs), sf2._logs

    # КОНТРОЛЬ: без хука путь молчит и не роняет диспатч
    ex.set_combat_win_hook(None)
    assert ex._note_combat_win("0xMID", "0xBOR", proven=True) is False
finally:
    ex._COMBAT_WIN_HOOK = _sv_hook

# структурно: диспатч зовёт хук на ПОДТВЕРЖДЁННОЙ квитанции, main его ставит
_ex_src = pathlib.Path("chain/execute.py").read_text()
assert "_note_combat_win(mid, borrower, proven=True, log=log)" in _ex_src, "диспатч не зовёт хук"
assert "set_combat_win_hook(_sfl.note_combat_win_by_market)" in pathlib.Path("main.py").read_text(), \
    "хук не подключён на старте — механизм снова вакуумный"

print("OK: боевая победа доходит до паузы через хук, чужие фиды не гасит")
