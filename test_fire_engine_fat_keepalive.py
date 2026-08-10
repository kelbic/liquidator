"""Право огня движку от fat-окна (вскрытие 07.08 17:00, блок 49666338).

Месяц журнала: движок включён, доска армится, 110 ног профинансированы — и 0 боевых проб,
потому что единственный keepalive шёл через _volley за тир-гейтом (армы>=4/час И свежий
файл-факт), который в решающий блок ложен по построению (10 открытий / 198 fail-closed).
Победители стояли на idx 2176-2187 сразу за транзитом (~2175), наши пробы — на 2439+.

Проверяем НАСТОЯЩИЕ функции (урок seam-stubbed-above-the-defect): реальный FireEngine с
реальными тредами и реальный SpecFireLive._engine_fat_keepalive; швы — только на отправке.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.fire_engine as fe                           # noqa: E402
import chain.spec_fire_live as sfl                       # noqa: E402


# ---------- A. настоящий FireEngine: огонь только под keepalive, гаснет сам ----------
def _wait(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


sent = []
eng = fe.FireEngine(legs_fn=lambda: ["0xLegA", "0xLegB"],
                    send_fn=lambda a: (sent.append(a) or "0xh%d" % len(sent)),
                    collector=None, threads=2, rate=500.0, ttl=0.4)
eng.start()
time.sleep(0.35)
assert len(sent) == 0, "движок стреляет БЕЗ права огня: %d отправок" % len(sent)

eng.keepalive()
assert _wait(lambda: len(sent) >= 5), "keepalive дан, а огня нет (sent=%d)" % len(sent)
assert {"0xLegA", "0xLegB"} <= set(sent), "round-robin не задел обе ноги: %s" % set(sent)

assert _wait(lambda: not eng.armed(), timeout=3.0), "TTL истёк, а право огня живо"
n1 = len(sent)
time.sleep(0.3)
assert len(sent) == n1, "огонь пережил свой TTL: %d -> %d" % (n1, len(sent))

eng.keepalive(ttl=30.0)
assert eng.armed()
eng.disarm()
assert not eng.armed(), "disarm не погасил право огня"
eng.stop()

# сухой send (гейт send_presigned_probe закрыт -> None) не крутит счётчик отправок
eng2 = fe.FireEngine(legs_fn=lambda: ["0xL"], send_fn=lambda a: None,
                     collector=None, threads=1, rate=500.0, ttl=0.6)
eng2.start()
eng2.keepalive()
assert _wait(lambda: eng2.stats["skipped_dry"] > 0), "сухой цикл не отмечен"
assert eng2.stats["sent"] == 0, "None от send_fn посчитан отправкой"
eng2.stop()


# ---------- B. настоящий _engine_fat_keepalive: ключ — живой ПЛАН доски ----------
class FakeEng:
    def __init__(self):
        self.calls = []

    def keepalive(self, target=None, ttl=None):
        self.calls.append(target)


def mk(now=1000.0, ready_delta=-1.0, until_delta=100.0):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: now
    sf._board = {"key": ("0xMID", "0xBOR"), "debt": 31000.0,
                 "until": now + until_delta, "ready_at": now + ready_delta,
                 "armed_ts": now - 10.0,
                 # #84: план ПОДТВЕРЖДЁН чтением с контракта. Без этого флага доска не живая
                 # (три недели она считалась живой по одному факту отправки, а все боевые
                 # setPlan сгорали по газу) — и право огня движку выдавать не с чего.
                 "verified": True}
    sf._fire_engine = FakeEng()
    sf._logs = []
    sf._log = sf._logs.append
    return sf


_saved = (sfl.SPEC_FIRE_V2_BOARD, sfl.SPEC_FIRE_ENGINE_FAT_KEEPALIVE, fe.ENGINE_ON)
try:
    sfl.SPEC_FIRE_V2_BOARD = True
    sfl.SPEC_FIRE_ENGINE_FAT_KEEPALIVE = True
    fe.ENGINE_ON = True

    # 1. план жив -> право продлено, цель = цель ПЛАНА (не тика), DIAG-строка есть
    sf = mk()
    assert sf._engine_fat_keepalive("XRP") is True
    assert sf._fire_engine.calls == [("XRP", "0xMID", "0xBOR", 31000.0)], sf._fire_engine.calls
    assert any("право огня от fat-окна" in m for m in sf._logs), sf._logs

    # 2. КОНТРОЛЬ отката: флаг выключен -> движок нем (поведение до фикса)
    sfl.SPEC_FIRE_ENGINE_FAT_KEEPALIVE = False
    sf = mk()
    assert sf._engine_fat_keepalive("XRP") is False and sf._fire_engine.calls == []
    sfl.SPEC_FIRE_ENGINE_FAT_KEEPALIVE = True

    # 3. план протух -> права нет
    sf = mk(until_delta=-1.0)
    assert sf._engine_fat_keepalive("XRP") is False and sf._fire_engine.calls == []

    # 4. запись плана ещё НЕ села в блок -> права нет (1-байт пробы ушли бы в пустоту)
    sf = mk(ready_delta=+5.0)
    assert sf._engine_fat_keepalive("XRP") is False and sf._fire_engine.calls == []

    # 5. движок выключен конфигом -> права нет, ошибки нет
    fe.ENGINE_ON = False
    sf = mk()
    assert sf._engine_fat_keepalive("XRP") is False and sf._fire_engine.calls == []
    fe.ENGINE_ON = True

    # 6. КОНТРОЛЬ: тир-путь _volley не тронут — при закрытом гейте залп V1 не идёт,
    #    и keepalive оттуда тоже не приходит (право огня теперь несёт ОКНО)
    sf = mk()
    sf._arm_ts = []                                   # армов за час нет -> _cascade_active False
    called = []
    sf._volley_accounts_fn = lambda: called.append(1) or []
    sf._volley({"calldata": "0x", "gas": 1}, "0xMID", "0xBOR", 1.0, "XRP")
    assert called == [], "тир-гейт закрыт, а _volley дошёл до ног"
    assert sf._fire_engine.calls == [], "закрытый тир не должен продлевать право огня"
finally:
    sfl.SPEC_FIRE_V2_BOARD, sfl.SPEC_FIRE_ENGINE_FAT_KEEPALIVE, fe.ENGINE_ON = _saved


# ---------- C. колл-сайты: право огня продлевают ОБЕ точки петли ----------
# Тик окна и pending-handoff (handoff делает continue МИМО тикового кода — без второй
# точки keepalive гас в самый момент транзита).
src = pathlib.Path("chain/spec_fire_live.py").read_text()
assert src.count("self._engine_fat_keepalive(feed)") >= 2, \
    "право огня должно продлеваться и тиком, и pending-handoff"
assert "SPEC_FIRE_ENGINE_FAT_KEEPALIVE" in src

print("OK: движок стреляет только под keepalive, право огня выдаёт fat-окно по живому плану")
