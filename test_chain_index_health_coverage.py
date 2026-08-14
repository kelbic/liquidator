"""МАЙОРЫ B и A (ревью р-4): строка ЖИВОСТИ обязана утверждать ПОКРЫТИЕ, а не жизнь треда.

B. «— ЧИТАЮ ЦЕПЬ» и «подряд здоровых=N» смотрели только на протухание, уступку и «ни одного
блока»: ДЫРЫ и ОТКАЗЫ узла в причину не попадали. Значит число росло при ПОТЕРЯННОМ покрытии —
узел отдаёт 429 на две трети чтений, право говорить «absent» потеряно, а приёмка «а» первого
часа снимается как «прибор жив». Канон №5: число, растущее при бесполезном механизме, приёмкой
быть не может.

A. Тревога «ЗАМОРОЖЕН» подавлялась ФАКТОМ уступки ключа в окне (`_d('shed_events')>0`). Одно
касание боя за окно держало ERROR подавленным ВЕЧНО: обещание «решит следующее окно» не
наступало никогда, потому что в следующем окне уступка была снова. Подавлять имеет право
только ДОЛЯ: секунды уступки против возраста протухания, плюс эскалация после N подряд
подавленных окон.

ОБЕ правки проверяются В ОБЕ СТОРОНЫ — иначе лечение хуже болезни: здоровый узел обязан
по-прежнему давать «ЧИТАЮ ЦЕПЬ» подряд, а ШТАТНАЯ уступка на ЖИВОЙ голове — не давать ERROR
(ровно та ложная тревога на проектной норме, ради которой подавление и вводилось в р-3).

Подменён РОВНО ОДИН шов — сокет. Тред, `_loop`, `_catch_up`, `_confirmed_head`, `scan_block`,
`_ingest`, `_health` исполняются настоящие.
"""
import logging
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402

BN0 = 49890266
LOGGER = logging.getLogger("liquidator")


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, rec):
        self.rows.append((rec.levelno, rec.getMessage()))


def drill(seconds=600, step=1.0, shed_fn=None, logs_fail_fn=None, freeze_after=None):
    T = [1000.0]

    def _http(url, payload, timeout):
        m = payload["method"]
        el = T[0] - 1000.0
        if freeze_after is not None:
            el = min(el, freeze_after)
        if m == "eth_blockNumber":
            return {"result": hex(BN0 + int(el // 2))}
        if m == "eth_getLogs":
            if logs_fail_fn is not None and logs_fail_fn(T[0] - 1000.0):
                raise ci.IndexUnavailable("узел ответил 429", "node.test")
            return {"result": []}
        return {"result": None}

    idx = ci.ChainIndex("https://node.test/KEY", watch_to=["0x" + "ab" * 20], http=_http,
                        now=lambda: T[0], sleep=lambda s: None, log_fn=lambda m: None)
    if shed_fn is not None:
        idx._shed = lambda: shed_fn(T[0] - 1000.0)
    cap = Cap()
    cap.setLevel(logging.DEBUG)
    LOGGER.addHandler(cap)
    _lvl, _poll = LOGGER.level, ci.CHAIN_INDEX_SELF_POLL_SEC
    LOGGER.setLevel(logging.INFO)
    ci.CHAIN_INDEX_SELF_POLL_SEC = step
    class _Wake(object):
        """ЧАСЫ ПЕТЛИ ДВИГАЕТ САМ ТРЕД ИНДЕКСА (B4 р-7): модельное время идёт РОВНО внутри
        собственного ожидания петли, а не по реальным часам главного треда. Прежняя редакция
        (`T += шаг; _wake.set(); time.sleep(0.0015)`) надеялась, что настоящий тред успеет
        отработать оборот за 1.5мс: под нагрузкой не успевал, и прибор давал ложный диагноз на
        проектной норме. Показания прибора не имеют права зависеть от загрузки машины."""

        def __init__(self, step, stop_at, on_tick=None):
            self.step, self.stop_at, self.on_tick = step, stop_at, on_tick

        def wait(self, timeout=None):
            T[0] += self.step
            if self.on_tick is not None:
                self.on_tick()
            if (T[0] - 1000.0) >= self.stop_at:
                idx._stop.set()
            return False

        def set(self):
            pass

        def clear(self):
            pass

        def is_set(self):
            return False

    idx._wake = _Wake(step, seconds)
    try:
        idx._stop.clear()
        idx._loop()
    finally:
        ci.CHAIN_INDEX_SELF_POLL_SEC = _poll
        LOGGER.setLevel(_lvl)
        LOGGER.removeHandler(cap)
    live = [(lv, m) for lv, m in cap.rows if "CHAIN-INDEX ЖИВОСТЬ" in m]
    ok = [m for lv, m in live if "ЧИТАЮ ЦЕПЬ" in m]
    froze = [m for lv, m in live if lv >= logging.ERROR and "ЗАМОРОЖЕН" in m]
    streak = max([int(re.search(r"подряд здоровых=(\d+)", m).group(1)) for _lv, m in live] or [0])
    return idx, live, ok, froze, streak


# ---------------------------------------------------------------- B1. ПОЗИТИВНЫЙ КОНТРОЛЬ
# Здоровый узел: без этого прогона правка B могла бы просто сделать приёмку «а» недостижимой
# ПО ПОСТРОЕНИЮ (ровно тот дефект, за который р-3 снял МАЙОР-2).
idx, live, ok, froze, streak = drill()
assert idx.stats["blocks"] > 0 and idx.stats["gaps"] == 0 and idx.stats["fails"] == 0
assert len(ok) >= 2 and streak >= 2, (len(ok), streak)
assert not froze, froze
print("UNIT_OK: ЗДОРОВЫЙ узел даёт «ЧИТАЮ ЦЕПЬ» подряд (строк=%d, подряд=%d, дыр=0 отказов=0) "
      "— приёмка «а» достижима, правка B её не убила" % (len(ok), streak))

# ---------------------------------------------------------------- B2. ОТКАЗЫ УЗЛА
idx, live, ok, froze, streak = drill(logs_fail_fn=lambda t: int(t) % 3 != 0)
assert idx.stats["fails"] > 0, idx.stats
assert not ok and streak == 0, ("429 на 2 из 3 чтений — покрытие потеряно, «ЧИТАЮ ЦЕПЬ» "
                                "печататься не имеет права", len(ok), streak)
assert any("ОТКАЗЫ УЗЛА за окно" in m for _lv, m in live), live[-1]
print("UNIT_OK: 429 на 2 из 3 чтений -> «ЧИТАЮ ЦЕПЬ»=0, подряд здоровых=0, причина названа "
      "(отказов за окно %d) — число живости больше не растёт при потерянном покрытии"
      % idx.stats["fails"])

# ---------------------------------------------------------------- B3. ДЫРЫ УЗЛА В ПОКРЫТИИ
# Узел отказывает 120с из каждых 300с: пока он молчит, голова убегает дальше MAX_CATCHUP, и
# догон ПРЫГАЕТ через блоки. Это дыра ПО ВИНЕ УЗЛА — право говорить «absent» на пропущенных
# блоках потеряно, и строка живости обязана перестать быть здоровой.
idx, live, ok, froze, streak = drill(seconds=900, logs_fail_fn=lambda t: (t % 300.0) < 120.0)
assert idx.stats["gaps"] > 0 and idx.stats["gaps_shed"] == 0, idx.stats
assert any("ДЫРЫ В ПОКРЫТИИ за окно" in m for _lv, m in live), live[-1]
_bad = [m for _lv, m in live if re.search(r"ЗА ОКНО блоков=\d+ полных=\d+ лёгких=\d+ дыр=[1-9]", m)]
assert _bad and not [m for m in _bad if "ЧИТАЮ ЦЕПЬ" in m], \
    ("окно с ДЫРОЙ УЗЛА не имеет права печататься здоровым: %s" % (_bad[:1] or live[-1:]))
print("UNIT_OK: дыры УЗЛА при догоне (%d) -> окно с дырой не здорово и причина названа ценой "
      "«право говорить absent потеряно»" % idx.stats["gaps"])

# ------------------------------------- B3b. ПРОПУСК ПО УСТУПКЕ — НЕ ДЫРА УЗЛА (майор М3, р-6)
# ТА ЖЕ механика прыжка догона, но причина ПРОТИВОПОЛОЖНАЯ: мы сами уступили ключ бою. Считая
# это дырой узла, строка «ЧИТАЮ ЦЕПЬ» становилась недостижимой на боевом ритме #100 (уступка
# чаще раза в 4 минуты — замер дрели L1), то есть приёмка «а» гасла оттого, что механизм
# работает КАК ЗАДУМАНО. Цена пропуска при этом никуда не девается и обязана быть НАЗВАНА.
idx, live, ok, froze, streak = drill(shed_fn=lambda t: (t % 100.0) < 40.0)
assert idx.stats["gaps_shed"] > 0, ("прыжок догона после уступки обязан считаться СВОИМ "
                                    "именем: %s" % idx.stats)
assert idx.stats["gaps"] == 0, ("узел здоров — дыр УЗЛА быть не должно: %s" % idx.stats)
assert ok, ("проектная уступка не имеет права навсегда лишать индекс права сказать «ЧИТАЮ "
            "ЦЕПЬ»: %s" % live[-1:])
assert any("пропусков по уступке" in m for _lv, m in live), live[-1]
print("UNIT_OK: пропуск догона ПО УСТУПКЕ (%d) назван отдельно и НЕ дисквалифицирует строку "
      "(«ЧИТАЮ ЦЕПЬ» %d раз, дыр узла %d)" % (idx.stats["gaps_shed"], len(ok), idx.stats["gaps"]))

# МУТАЦИЯ М3: вернуть слияние причин (как было в р-5) — «ЧИТАЮ ЦЕПЬ» обязано исчезнуть.
_sv_ing = ci.ChainIndex._ingest
try:
    def _ingest_merge(self, bn, facts, full, pairs=None):
        self._skip_shed = False        # причина пропуска стёрта: всякая дыра считается дырой узла
        return _sv_ing(self, bn, facts, full, pairs=pairs)

    ci.ChainIndex._ingest = _ingest_merge
    _i2, _l2, _ok2, _fr2, _st2 = drill(shed_fn=lambda t: (t % 100.0) < 40.0)
    assert _i2.stats["gaps"] > 0 and not _ok2, \
        ("мутация «уступочный пропуск = дыра узла» ОБЯЗАНА краснить стенд: %s / ЧИТАЮ ЦЕПЬ=%d"
         % (_i2.stats, len(_ok2)))
finally:
    ci.ChainIndex._ingest = _sv_ing
print("UNIT_OK: мутация (слить пропуск уступки с дырами узла, как в р-5) краснит стенд — "
      "«ЧИТАЮ ЦЕПЬ» снова 0 при исправном узле")

# ---------------------------------------------------------------- A1. ЗАМЕРШАЯ ГОЛОВА + касание боя
# Голова узла встала на 60-й секунде; бой садится на ключ 6с из каждых 110с (чаще окна надзора).
idx, live, ok, froze, streak = drill(seconds=3600, step=4.0, freeze_after=60.0,
                                     shed_fn=lambda t: (t % 110.0) < 6.0)
assert len(froze) >= 10, ("одно касание боя за окно НЕ имеет права держать тревогу "
                          "подавленной: замершая на час голова обязана звучать", len(froze))
assert any("объясняет лишь" in m for _lv, m in live), live[-1]
print("UNIT_OK: касание боя 6с из 110с при ЗАМЕРШЕЙ голове -> ERROR «ЗАМОРОЖЕН» %d раз "
      "(было: подавление по ФАКТУ уступки давало 1 из 30 окон); строка называет, сколько "
      "секунд уступка объясняет из возраста" % len(froze))

# ---------------------------------------------------------------- A2. ПОЗИТИВНЫЙ КОНТРОЛЬ нормы
# Голова ЖИВА, уступка 40с из 100с (вчетверо агрессивнее боевого флапа failover ~6 мин, #100).
idx, live, ok, froze, streak = drill(seconds=3600, step=4.0,
                                     shed_fn=lambda t: (t % 100.0) < 40.0)
assert not froze, ("ложная тревога на ПРОЕКТНОЙ НОРМЕ — ровно то, что закрывал блокер р-3 №5",
                   froze[:1])
print("UNIT_OK: ЖИВАЯ голова + уступка 40с из 100с -> ERROR «ЗАМОРОЖЕН»=0: подавление по ДОЛЕ "
      "не воскресило ложную тревогу на проектной норме")

# ---------------------------------------------------------------- A3. МУТАЦИЯ: подавлять по ФАКТУ
_sv_snap = ci.ChainIndex.snapshot
try:
    def _snap_no_sec(self):
        # УСТУПКА «объясняет» заведомо больше любого возраста, то есть подавление снова стоит
        # на ФАКТЕ касания, а не на доле. Константа сюда не годится: она не растёт, а величина
        # обязана расти — как растёт сама уступка.
        s = _sv_snap(self)
        s["shed_sec_ok"] = self._now() * 1e6
        return s

    ci.ChainIndex.snapshot = _snap_no_sec
    idx, live, ok, froze, streak = drill(seconds=3600, step=4.0, freeze_after=60.0,
                                         shed_fn=lambda t: (t % 110.0) < 6.0)
    assert not froze, ("мутация «подавление по ФАКТУ уступки» ОБЯЗАНА краснить стенд — иначе "
                       "тест ничего не держит", len(froze))
finally:
    ci.ChainIndex.snapshot = _sv_snap
print("UNIT_OK: мутация (подавление по факту уступки — редакция до р-5) краснит стенд: тревога "
      "на замершей голове исчезает целиком")

print("ПРОШЛИ: строка ЖИВОСТИ утверждает ПОКРЫТИЕ (дыры/отказы), а подавление тревоги "
      "уступкой живёт ровно столько секунд, сколько уступка длилась")
