"""27.08 ПОЛ ДОСЯГАЕМОСТИ У ПЕРЕХВАТА ДОСКИ ДЕТОНАТОРОМ.

ЗАМЕР НА ЦЕПИ: доска несла цели в 10.3-10.8% от кромки при p99 реального толчка 1.82% (всё поле).
Источник — ветка перехвата: за сутки 89 перехватов против 58 успешных армов, dist перехваченных
5.98-6.48%. Цель 0x5dffffc7/0x1a1e7a3B держалась на доске 5.5 часа, и её расстояние РОСЛО
(5.16% -> 5.68% -> 6.02% -> 6.48% -> 6.27%) при TTL доски 14 минут. Флот всё это время стрелял
вхолостую: 7 391 проба за 9.8 суток, все «position is healthy», ноль исполнений, ~$84/сутки газа.

Проверяем ПОВЕДЕНИЕ, а не наличие строки в исходнике:
  (1) недосягаемая цель перехват НЕ получает (флот молчит дешевле, чем стреляет впустую);
  (2) досягаемая — получает (позитивный контроль: пол не выключает механизм целиком);
  (3) без замера push_scale пол НЕ применяется — прежнее поведение бит-в-бит;
  (4) откат ручкой SPEC_FIRE_DET_REACH_PUSHES=0.
"""
import sys, time, types, threading
sys.path.insert(0, ".")
import chain.push_scale as ps
import chain.spec_fire_live as sfl

ps.STATE_PATH = "/tmp/ps_det_test.json"
LOGS = []


def _mk_sf(det_dist, feed="ETH"):
    """SpecFireLive с ПУСТОЙ вершиной (жир вне reach) и свежим ключом детонатора."""
    m = types.SimpleNamespace(collateral_token="cbBTC", loan_token="USDC", bonus=0.05)
    sh = types.SimpleNamespace(lock=threading.Lock(), groups={"0xm": ["0xDET"]},
                               debt_by={("0xm", "0xDET"): 900_000.0},
                               hf_by={("0xm", "0xDET"): 1.0 / (1.0 - det_dist)},
                               by_id={"0xm": m})
    sf = object.__new__(sfl.SpecFireLive)
    sf.shared = sh
    sf.stats = {}
    sf._unbuildable, sf._unbuildable_mid = {}, {}
    sf._now = lambda: time.time()
    sf._log = LOGS.append
    sf._det_key_by_feed = {feed: (("0xm", "0xDET"), det_dist, time.time())}
    return sf


def _override_taken(det_dist, feed="ETH"):
    """Зовёт БОЕВОЕ решение (_det_reach_ok), а не его копию: копия логики в стенде проходит
    мимо дефекта по построению — 27.08 это уже случилось с гейтом, охраняемым грепом."""
    sf = _mk_sf(det_dist, feed)
    LOGS.clear()
    return sf._det_reach_ok(feed, det_dist, "0xm", "0xDET")


# кольцо ETH: 40 толчков по 0.20%. 28.08: семя стало НИЖНЕЙ границей, поэтому reach = семя
# ETH 0.808% (не 0.20%), бюджет = 1.616%. Пороги целей ниже пересчитаны от бюджета, не от константы.
ps._RINGS.clear()
now = time.time()
for i in range(40):
    ps.note_push("ETH", 0.20, now=now - i)
assert ps.samples("ETH") >= ps.MIN_N and abs(ps.reach("ETH") - ps._SEED["ETH"] / 100.0) < 1e-9, ps.reach("ETH")

# (2) ПОЗИТИВНЫЙ КОНТРОЛЬ ПЕРВЫМ: досягаемая цель перехват получает
assert _override_taken(0.0015) is True, "пол не должен выключать перехват целиком"
print("UNIT_OK 1: позитивный контроль — цель на 0.150%% (бюджет 0.400%%) перехват ПОЛУЧАЕТ")

# (1) ГЛАВНОЕ: боевая цель 6.27% отклоняется
_budget = ps.reach("ETH") * sfl.SPEC_FIRE_DET_REACH_PUSHES          # граница — ОТ бюджета, не константой
assert _override_taken(0.0627) is False, "цель на 6.27%% при бюджете %.3f%% обязана быть отклонена" % (_budget * 100)
assert _override_taken(_budget + 0.0002) is False, "чуть за бюджетом — тоже отклонение (граница работает)"
assert _override_taken(_budget - 0.0002) is True, "чуть внутри бюджета — проходит"
print("UNIT_OK 2: боевая цель 6.270%% отклонена; граница бюджета режет по 0.400%%")

# (3) БЕЗ ЗАМЕРА пол не применяется — прежнее поведение
ps._RINGS.clear()
assert ps.reach("ETH") is not None, "reach на семени всё ещё отдаёт число"
_saved_on = ps.PUSH_SCALE_ON
ps.PUSH_SCALE_ON = False                       # механизм выключен -> reach None
assert ps.reach("ETH") is None
assert _override_taken(0.0627) is True, "без замера пол применяться НЕ ДОЛЖЕН (бит-в-бит)"
ps.PUSH_SCALE_ON = _saved_on
print("UNIT_OK 3: механизм молчит -> пол не применяется, перехват как раньше")

# (4) ОТКАТ РУЧКОЙ
ps._RINGS.clear()
for i in range(40):
    ps.note_push("ETH", 0.20, now=now - i)
_saved = sfl.SPEC_FIRE_DET_REACH_PUSHES
sfl.SPEC_FIRE_DET_REACH_PUSHES = 0.0
assert _override_taken(0.0627) is True, "SPEC_FIRE_DET_REACH_PUSHES=0 обязан снимать пол"
sfl.SPEC_FIRE_DET_REACH_PUSHES = _saved
print("UNIT_OK 4: откат SPEC_FIRE_DET_REACH_PUSHES=0 возвращает прежнее поведение")

# (5) ПРОВОДКА в боевом коде: пол стоит ПЕРЕД веткой перехвата и пишет отказ в журнал
src = open("chain/spec_fire_live.py").read()
i_floor = src.index("перехват детонатора ОТКЛОНЁН")
i_take = src.index("план ПОД ЦЕЛЬ ДЕТОНАТОРА")
assert i_floor < i_take, "пол обязан стоять ДО ветки, которая ставит цель"
assert "not self._det_reach_ok(feed, _dk[1]" in src, \
    "ветка перехвата обязана звать ИМЕННО этот метод — иначе стенд судит мёртвый код"
assert "board_det_out_of_reach" in src, "отказ обязан считаться (иначе слепота к его частоте)"
assert "SPEC_FIRE_DET_REACH_PUSHES" in src
print("UNIT_OK 5: проводка — пол перед перехватом, отказ логируется и считается")
print("=== ПОЛ ПЕРЕХВАТА: 5/5 ===")
