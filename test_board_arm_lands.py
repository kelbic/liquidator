"""КОРНЕВОЙ ДЕФЕКТ ТРАКТА: арм доски не садился НИ РАЗУ за историю (#84).

Аудит 10.08, подтверждено на цепи. Формула газа `120_000 + 22*байт` МЕНЬШЕ стоимости самой
записи: на боевой калдате 772 Б она даёт лимит 137 006 при `eth_estimateGas` 505 718 (×3.69).
Все ЧЕТЫРЕ боевых setPlan за историю v5 — 0x3e73d952, 0xf25f8369, 0x6c6dcc8c, 0x754ec513 —
имеют `status=0` и `gasUsed == gasLimit == 137 006` ровно, логов ноль. На контракте
0x85BadE2c за всю его жизнь НОЛЬ событий PlanSet при позитивном контроле 118 AuthorizedChanged.

Наклон 22/байт был взят как «16 калдата + ~6 стораджа», но байты роута ложатся в bytes-поле
СТОРАДЖА: холодный SSTORE = 20k на слово, то есть ~625 газа на байт, плюс ~12 холодных слотов
фиксированных полей (240k) — одни они больше всего прежнего лимита.

Вторая половина дефекта и она хуже первой: `_arm_board` признавал арм по ВОЗВРАЩЁННОМУ ХЭШУ
(`send_board_tx(wait=False)`), писал «доска armed» и растил board_arms. `read_plan_info()` был
определён и НЕ ВЫЗЫВАЛСЯ НИГДЕ. Итог: доска считалась живой ~840с, ноги били 1 байтом в пустой
сторадж, а штатная деградация в V1 не срабатывала — то есть осечка арма была ХУЖЕ, чем полное
отсутствие доски. Все board_probes=0 и «0 побед» объясняются этим: плана в контракте не было.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.spec_fire_live as sfl                          # noqa: E402

MID = "0x" + "11" * 32
BOR = "0x" + "22" * 20
FEED = "XRP"

# --- 1. ГАЗ: боевой размер калдаты обязан покрываться с запасом --------------------------
# Числа с цепи: калдата 772 Б, estimate 505 718, прежний лимит 137 006 (сгорел).
REAL_BYTES, REAL_EST, OLD_LIMIT = 772, 505_718, 137_006


def gas_for(nbytes):
    """Та же арифметика, что в _arm_board — держим её здесь явно, чтобы тест ловил
    изменение ФОРМУЛЫ, а не только сегодняшнее значение."""
    return max(700_000, 300_000 + 800 * nbytes)


old = 120_000 + 22 * REAL_BYTES
assert abs(old - OLD_LIMIT) <= 30, "воспроизведение прежней формулы разошлось с цепью: %d" % old
assert old < REAL_EST, "прежняя формула обязана быть НИЖЕ estimate — иначе пример не тот"
assert gas_for(REAL_BYTES) >= REAL_EST * 3 // 2, \
    "новый лимит %d не даёт полуторного запаса к замеренному estimate %d" % (
        gas_for(REAL_BYTES), REAL_EST)
# и на вдвое более длинном роуте (жирный путь через несколько ног) запас обязан сохраниться:
# наклон, а не только константа, должен быть выше настоящей цены байта в сторадже (~625)
assert gas_for(2 * REAL_BYTES) - gas_for(REAL_BYTES) >= 625 * REAL_BYTES, \
    "наклон формулы ниже цены холодного SSTORE на байт — длинный роут снова сгорит"
print("OK 1/4: газ арма — прежний %d < estimate %d, новый %d (запас %.2fx), наклон выше SSTORE" % (
    old, REAL_EST, gas_for(REAL_BYTES), gas_for(REAL_BYTES) / REAL_EST))

# --- 2. СТРУКТУРНО: формула в бою — та же, и хэш больше не считается посадкой -------------
src = pathlib.Path("chain/spec_fire_live.py").read_text()
_s = src.index("    def _arm_board(")
arm = src[_s:src.index("\n    def ", _s + 10)]
assert "120_000 + 22 *" not in arm, "прежняя формула газа вернулась в _arm_board"
assert "max(700_000, 300_000 + 800 * _nb)" in arm, "формула газа в бою разошлась с тестом"
assert '"verified": False' in arm, "доска снова считается живой сразу после отправки"
assert "_verify_board" in arm, "проверка посадки не запускается из арма"
assert "read_plan_info" in src, "planInfo не читается вовсе (был определён и не вызывался)"
print("OK 2/4: боевая формула совпадает с тестом; арм ставит verified=False и шлёт проверку")


# --- 3. ДОСКА НЕ ЖИВАЯ, пока посадка не подтверждена -------------------------------------
def mk():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._logs = []
    sf._log = sf._logs.append
    sf._now = lambda: 1000.0
    sf.stats = {"board_fails": 0}
    sf._board_next_try = {}
    sf.cfg = type("C", (), {"liquidator_address": "0x" + "ff" * 20})()
    sf.rpc = None
    sf._board = {"key": (MID, BOR), "armed_ts": 1000.0, "until": 2000.0,
                 "ready_at": 900.0, "verified": False, "tx": "0xdead"}
    return sf


_v2 = sfl.SPEC_FIRE_V2_BOARD
sfl.SPEC_FIRE_V2_BOARD = True          # в бою включён через .env; юнит не должен от него зависеть
sf = mk()
assert sf._board_ready(MID, BOR) is True, "цель та же и не протухла — ready обязан быть True"
assert sf._board_live(MID, BOR) is False, \
    "доска живая БЕЗ подтверждения посадки — ровно тот дефект, что стоил трёх недель"
sf._board["verified"] = True
assert sf._board_live(MID, BOR) is True, "подтверждённая доска обязана быть живой"
sfl.SPEC_FIRE_V2_BOARD = _v2
print("OK 3/4: доска не живая до подтверждения, живая после (гейт различает оба состояния)")

# --- 4. НЕ СЕЛА -> доску снимаем ГРОМКО (деградация в V1), а не молчим --------------------
import chain.execute as ex                                  # noqa: E402

_saved = ex.read_plan_info
_settle = sfl.SPEC_FIRE_V2_BOARD_SETTLE_SEC
try:
    sfl.SPEC_FIRE_V2_BOARD_SETTLE_SEC = 0.0                 # тест не ждёт боевые 6с

    # 4a. пустой planInfo (ровно то, что было в бою: tx сгорела, план не записан)
    ex.read_plan_info = lambda rpc, addr: ("0x" + "00" * 20, 0, 0)
    sf = mk()
    sf._verify_board((MID, BOR), FEED, "0xdead")
    assert sf._board == {}, "доска НЕ снята при пустом плане — ноги били бы в пустой сторадж"
    assert sf.stats["board_fails"] == 1 and sf.stats.get("board_unlanded") == 1
    assert any("НЕ СЕЛА" in s for s in sf._logs), "осечка посадки молчит в журнале"
    assert sf._board_next_try.get(FEED), "переспрос арма не взведён"

    # 4b. чтение упало -> незнание ЗАКРЫВАЕТ гард (не «считаем, что село»)
    def _boom(rpc, addr):
        raise RuntimeError("узел не ответил")

    ex.read_plan_info = _boom
    sf = mk()
    sf._verify_board((MID, BOR), FEED, "0xdead")
    assert sf._board == {}, "чтение упало, а доска осталась живой — незнание как разрешение"

    # 4c. КОНТРОЛЬ: план на месте и на НАШЕГО заёмщика -> подтверждаем
    ex.read_plan_info = lambda rpc, addr: (BOR, int(time.time()) + 600, 12345)
    sf = mk()
    sf._verify_board((MID, BOR), FEED, "0xdead")
    assert sf._board.get("verified") is True, "живой план не подтверждён — тракт был бы мёртв"
    assert sf.stats.get("board_verified") == 1
    assert sf.stats["board_fails"] == 0

    # 4d. КОНТРОЛЬ: план на ЧУЖОГО заёмщика подтверждением не считается
    ex.read_plan_info = lambda rpc, addr: ("0x" + "99" * 20, int(time.time()) + 600, 1)
    sf = mk()
    sf._verify_board((MID, BOR), FEED, "0xdead")
    assert sf._board == {}, "чужой план засчитан как наш — доска стреляла бы по чужой цели"

    # 4e. КОНТРОЛЬ: протухший дедлайн — не подтверждение
    ex.read_plan_info = lambda rpc, addr: (BOR, int(time.time()) - 10, 1)
    sf = mk()
    sf._verify_board((MID, BOR), FEED, "0xdead")
    assert sf._board == {}, "протухший план засчитан как живой"
finally:
    ex.read_plan_info = _saved
    sfl.SPEC_FIRE_V2_BOARD_SETTLE_SEC = _settle

print("OK 4/4: не села / чтение упало / чужой / протухший — доска снимается; живая — подтверждается")
print("=== #84 ЗАКРЫТ: арм доски садится и проверяется ===")
