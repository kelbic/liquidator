"""БЛОКЕР Р2 №5: застрявшая голова узла морозила индекс МОЛЧА. Дрель ВО ВРЕМЕНИ, не в моменте.

СЦЕНАРИЙ РОВНО ТОТ, ЧТО НАЗВАН В ВЕРДИКТЕ: голова узла замирает ПОСЛЕ 60-й секунды, то есть
после того, как одноразовая приёмочная строка уже напечатала «ВЕРДИКТ: ЖИВ». Дальше индекс
живёт как труп: тред жив, отказов ноль, ERROR ноль, `_last_bn` не двигается, потребители
получают «unknown» и молча ведут себя как до пакета. Молчание неотличимо от исправности —
это буквально «мёртвый сторож хуже отсутствующего».

Работают НАСТОЯЩИЕ `_loop`, `_catch_up`, `_confirmed_head`, `scan_block`, `_selfcheck`,
`_health` в НАСТОЯЩЕМ треде. Подменены три шва: сокет, часы и шаг ожидания петли (иначе
дрель «неделя штиля» шла бы неделю).

ПОЗИТИВНЫЙ КОНТРОЛЬ: та же дрель с вырезанным механизмом (`_health` -> заглушка) обязана
пройти БЕЗ единого ERROR. Если она молчит в обоих случаях — дрели нет.
"""
import logging
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402

BN0 = 49890266
LOGGER = logging.getLogger("liquidator")


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errs, self.warns, self.infos = [], [], []

    def emit(self, rec):
        m = rec.getMessage()
        (self.errs if rec.levelno >= logging.ERROR
         else self.warns if rec.levelno >= logging.WARNING else self.infos).append(m)


def drill(freeze_after_sec=60.0, seconds=300, shed=False, kill_health=False):
    """Прогоняет `seconds` МОДЕЛЬНЫХ секунд жизни индекса. Голова замирает после N секунд."""
    T = [1000.0]
    calls = {"head": 0}

    def _http(url, payload, timeout):
        m = payload["method"]
        elapsed = T[0] - 1000.0
        head = BN0 + int(min(elapsed, freeze_after_sec) // 2)     # блок Base = 2с
        if m == "eth_blockNumber":
            calls["head"] += 1
            return {"result": hex(head)}
        if m == "eth_getLogs":
            return {"result": []}
        return {"result": None}

    idx = ci.ChainIndex("https://node.test/KEY", watch_to=["0x" + "ab" * 20], http=_http,
                        now=lambda: T[0], sleep=lambda s: None)
    if kill_health:
        idx._health = lambda: None            # МУТАЦИЯ: механизм надзора снят
    if shed:
        idx._shed = lambda: True
    cap = Cap()
    cap.setLevel(logging.DEBUG)
    LOGGER.addHandler(cap)
    # Боевой уровень журнала — INFO (config.py: log_level="INFO"), и строка «ЧИТАЮ ЦЕПЬ» идёт
    # именно на нём: раз в 120с положительное утверждение о живости в журнал попадать обязано,
    # а тревога («ЗАМОРОЖЕН») уходит на ERROR и переживает любое прореживание.
    _sv_lvl = LOGGER.level
    LOGGER.setLevel(logging.INFO)
    _sv_poll = ci.CHAIN_INDEX_SELF_POLL_SEC
    ci.CHAIN_INDEX_SELF_POLL_SEC = 0.001      # шаг ожидания петли — единственный «ускоритель»
    try:
        idx.start()
        for _ in range(seconds):
            T[0] += 1.0
            idx._wake.set()
            time.sleep(0.0015)                # дать НАСТОЯЩЕМУ треду отработать оборот
    finally:
        idx.stop()
        ci.CHAIN_INDEX_SELF_POLL_SEC = _sv_poll
        LOGGER.setLevel(_sv_lvl)
        LOGGER.removeHandler(cap)
        idx._thr.join(timeout=2)
    return idx, cap


# ---------------------------------------------------------------- 1. голова замерла на 60-й
idx, cap = drill()
s = idx.snapshot()
assert s["blocks"] > 0, "дрель бессмысленна, если индекс и не начинал читать: %s" % s
assert s["fresh"] is False, "голова замерла — индекс ОБЯЗАН быть протухшим: %s" % s
_accept = [m for m in cap.warns + cap.errs if "CHAIN-INDEX ПРИЁМКА" in m]
assert _accept and "МЁРТВ" not in _accept[0], \
    ("одноразовая приёмка первой минуты обязана была отчитаться положительно — именно поэтому "
     "её одной и не хватает: %s" % _accept)
assert "ВЕРДИКТ: ЧАСТИЧНО" in _accept[0], \
    ("в дрели узел не отдаёт интересных логов, значит полных чтений НОЛЬ — приёмка «а» "
     "требует полных>0 и обязана это РАЗЛИЧАТЬ, а не звать «ЖИВ»: %s" % _accept[0])
_froze = [m for m in cap.errs if "ЗАМОРОЖЕН" in m]
assert _froze, ("заморозка после 60-й секунды обязана быть ERROR, а не тишиной. "
                "ERROR за прогон: %s" % cap.errs)
assert "CHAIN-INDEX ЖИВОСТЬ(120с)" in _froze[0] and "блоков=%d" % s["blocks"] in _froze[0], _froze[0]
assert idx.stats["stale_events"] >= 1, idx.stats
print("UNIT_OK: голова узла замерла ПОСЛЕ приёмочной строки «ЖИВ» — 120с-строка выдала ERROR "
      "«ЗАМОРОЖЕН» (%d раз(а)), stale_events=%d" % (len(_froze), idx.stats["stale_events"]))

# ---------------------------------------------------------------- 2. позитивный контроль
idx_m, cap_m = drill(kill_health=True)
assert idx_m.snapshot()["fresh"] is False
assert not [m for m in cap_m.errs if "ЗАМОРОЖЕН" in m], \
    "МУТАЦИЯ обязана вернуть молчание — иначе дрель ловит не то: %s" % cap_m.errs
print("UNIT_OK: с вырезанным надзором та же заморозка проходит БЕЗ единого ERROR — дрель "
      "умеет падать (и ровно так пакет и выглядел до фикса)")

# ---------------------------------------------------------------- 3. здоровый индекс не орёт
idx_ok, cap_ok = drill(freeze_after_sec=10 ** 6)
assert idx_ok.snapshot()["fresh"] is True and idx_ok.snapshot()["blocks"] > 0
assert not [m for m in cap_ok.errs if "ЗАМОРОЖЕН" in m], cap_ok.errs
_alive = [m for m in cap_ok.infos if "ЧИТАЮ ЦЕПЬ" in m]
assert _alive, ("живость обязана утверждаться ПОЛОЖИТЕЛЬНО: отсутствие строки не умеет быть "
                "сигналом. info за прогон: %s" % cap_ok.infos[-3:])
print("UNIT_OK: здоровый индекс печатает ту же строку с «ЧИТАЮ ЦЕПЬ» (%d раз) и ни одного "
      "ERROR — тревога не ложная" % len(_alive))

# ------------------------------------- 4. уступка ключа бою не имеет права ГЛУШИТЬ диагностику
idx_sh, cap_sh = drill(shed=True)
assert idx_sh.stats["shed"] > 0
_lines = [m for m in cap_sh.errs + cap_sh.warns + cap_sh.infos if "CHAIN-INDEX ЖИВОСТЬ" in m]
assert _lines, ("уступая ключ, индекс протухает — и обязан об этом СКАЗАТЬ. Прежде ветка "
                "уступки делала `continue` и уносила с собой всю диагностику: %s" % cap_sh.errs)
assert any("уступок ключа бою" in m for m in _lines), _lines
print("UNIT_OK: при уступке ключа бою 120с-строка ВСЁ РАВНО печатается и называет причину "
      "(прежде `continue` глушил и чтение, и диагностику разом)")

print("ПРОШЛИ: молчаливой заморозки индекса больше нет (дрель ВО ВРЕМЕНИ, с позитивным контролем)")
