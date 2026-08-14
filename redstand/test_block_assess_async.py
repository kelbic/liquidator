"""Юниты фикса флапа block-loop 30.07 (83 обрыва WS за 3ч при 0 у hot_loop: inline-ассессмент
блокировал event loop на 7-10с под штормом медленных отказов RPC — 408/таймаут ~7.4с).
Проверяет: (A) _offer_latest — слот «последний блок» вытесняет устаревший, не копит очередь;
(B) PredictDriver: серия отказов полла -> экспоненциальный бэкофф 5с..30с, успех сбрасывает,
POLL_BACKOFF_ON=False возвращает старое поведение. Без сети/TG."""
import queue
import sys

sys.path.insert(0, ".")
from main import _offer_latest
import chain.predict as predict
from chain.predict import PredictDriver, PredictEngine

# A) слот «последний блок»
q = queue.Queue(maxsize=1)
assert _offer_latest(q, 101) is False and q.qsize() == 1
assert _offer_latest(q, 102) is True, "занятый слот обязан вытеснить устаревший блок"
assert q.get_nowait() == 102, "в слоте всегда свежайший блок"
assert _offer_latest(q, 103) is False
print("UNIT_OK: _offer_latest — свежайший блок, вытеснение без накопления")


# B) бэкофф полла агрегаторов
def make_driver(clock, calls, logs, fail=lambda: True):
    def poll_fn():
        calls.append(clock["t"])
        if fail():
            raise RuntimeError("429 Too Many Requests")
        return {}
    return PredictDriver(PredictEngine(), mid_fn=lambda s: None, poll_fn=poll_fn,
                         log=logs.append, now=lambda: clock["t"],
                         sleep=lambda s: None, wall=lambda: clock["t"])


clock = {"t": 1000.0}
calls, logs = [], []
drv = make_driver(clock, calls, logs)
for _ in range(3):                    # отказы #1-#3 (полл каждые poll_interval=2с)
    drv.step()
    clock["t"] += 2.5
assert len(calls) == 3
assert drv._poll_backoff_until > 0, "после 3 отказов подряд обязан включиться бэкофф"
assert any("backoff 5s" in m for m in logs), logs[-3:]

n_before = len(calls)
drv.step()                            # мы внутри бэкоффа (5с) — попытки НЕТ
assert len(calls) == n_before, "внутри бэкофф-окна полл не долбит эндпоинт"
clock["t"] += 5.0
drv.step()                            # окно вышло — попытка #4 -> бэкофф 10с
assert len(calls) == n_before + 1
assert any("backoff 10s" in m for m in logs), logs[-3:]

# кап 30с: догоняем серию до глубокой
for _ in range(6):
    clock["t"] += 40.0
    drv.step()
assert "backoff 30s" in logs[-1], logs[-1]

# успех сбрасывает серию и бэкофф
ok = {"v": False}
calls2, logs2 = [], []
drv2 = make_driver(clock, calls2, logs2, fail=lambda: not ok["v"])
for _ in range(4):
    drv2.step()
    clock["t"] += 6.0
ok["v"] = True
clock["t"] += 31.0
drv2.step()
assert drv2._poll_fail_streak == 0 and drv2._poll_backoff_until == 0.0, "успех обязан сбросить бэкофф"
print("UNIT_OK: бэкофф 5->10->кап 30с после 3 отказов, успех сбрасывает")

# шторм 60с: с бэкоффом попыток кратно меньше, чем без него
def storm_attempts(backoff_on):
    old = predict.POLL_BACKOFF_ON
    predict.POLL_BACKOFF_ON = backoff_on
    try:
        c = {"t": 5000.0}
        cs, ls = [], []
        d = make_driver(c, cs, ls)
        while c["t"] < 5060.0:
            d.step()
            c["t"] += 0.5
        return len(cs)
    finally:
        predict.POLL_BACKOFF_ON = old


with_b, without_b = storm_attempts(True), storm_attempts(False)
assert without_b >= 25 and with_b <= 8, (with_b, without_b)
print("UNIT_OK: шторм 60с — %d попыток без бэкоффа vs %d с ним; kill-switch возвращает старое"
      % (without_b, with_b))
print("=== ВСЕ ASSESS-ASYNC/BACKOFF ЮНИТЫ ПРОШЛИ ===")
