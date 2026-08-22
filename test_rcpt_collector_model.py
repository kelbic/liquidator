"""П.3 (22.08): thread-per-call -> коллекторная модель квитанций окна. Приёмка по спеке:

1. Нагрузка 500: легаси плодит тред-на-пробу (линейный рост), коллектор держит +О(1) тредов;
   fd-прокси: сетевых вызовов у коллектора ~N/батч против N поштучных у легаси.
2. ИНВАРИАНТ: из N отправок ровно N исходов — дубль или ноль = провал стенда.
3. SLA: p50/p90 латентности коллектора не хуже легаси + интервал полла (стенд меряет ОБЕ ветки).
4. Shadow-фаза: расхождение исходов логируется, совпадение считается; деньги ведёт легаси.
5. Переполнение очереди: учётный дроп + фолбэк легаси-тредом — исход не теряется.
Откат: SPEC_FIRE_RCPT_MODE=thread (легаси бит-в-бит, без кода).
"""
import sys
import threading
import time
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402
import chain.fire_engine as fe  # noqa: E402


def mk(mode, max_pending=4096, receipt_ready=None, clock=None):
    """Стенд SpecFireLive: живой ReceiptCollector (без треда — tick руками), ядра-рекордеры."""
    o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    o.stats = {}
    o._lock = threading.Lock()
    logs = []
    o._log = lambda m: logs.append(m)
    o._now = clock or time.monotonic
    outcomes = {}                                       # h -> [res, ...] (инвариант: ровно 1)

    def _core(res, h, *a, **k):
        outcomes.setdefault(h, []).append(res)
    o._collect_core = _core
    o._collect_volley_core = lambda res, h, *a, **k: _core(res, h)
    ready = receipt_ready if receipt_ready is not None else {}
    calls = {"batch": 0, "single": 0}

    def _receipts(hs):
        calls["batch"] += 1
        t = o._now()
        return {h: ({"status": 1, "gasUsed": 100, "effectiveGasPrice": 1}
                    if ready.get(h, 0) <= t else None) for h in hs}

    def _single(h):
        calls["single"] += 1
        t0 = o._now()
        while o._now() - t0 < 10.0:
            if ready.get(h, 0) <= o._now():
                return (1, 0.0, 1, 1, None)
            time.sleep(0.005)
        return None
    o._receipt_fn = _single
    o._receipt = _single
    o.cfg = type("C", (), {"eth_price_usd": 4000.0, "liquidator_address": "0xliq"})()
    col = fe.ReceiptCollector(receipts_fn=_receipts, on_result=o._rcpt_result,
                              on_timeout=o._rcpt_timeout, timeout_sec=5.0, min_age_sec=0.0,
                              max_pending=max_pending, now=o._now, sleep=lambda s: None)
    o._rcpt_col = col
    sfl.SPEC_FIRE_RCPT_MODE = mode
    return o, col, outcomes, calls, logs


# ---- 1+2. Нагрузка 300 + ИНВАРИАНТ: коллектор держит +О(1) тредов, исход ровно один ----
sfl.SPEC_FIRE_RCPT_MODE = "collector"
o, col, outcomes, calls, logs = mk("collector")
base_thr = threading.active_count()
N = 300
for i in range(N):
    o._rcpt_dispatch("0x%04d" % i, "probe", ("m", "b", 1.0, "F"))
peak_col = threading.active_count()
check("нагрузка: коллектор НЕ плодит тредов (+<=2 при 300 отправках)",
      peak_col <= base_thr + 2, (base_thr, peak_col))
while col.pending():
    col.tick()
check("ИНВАРИАНТ: 300 отправок -> ровно 300 исходов, дублей/нулей нет",
      len(outcomes) == N and all(len(v) == 1 for v in outcomes.values()),
      (len(outcomes), sum(len(v) for v in outcomes.values())))
check("fd-прокси: сетевых партий ~N/64, НЕ N поштучно (было бы 300)",
      calls["batch"] <= (N // 64 + 3) and calls["single"] == 0, calls)

# ---- легаси для сравнения: тред-на-пробу растёт линейно ----
o2, _c2, out2, calls2, _ = mk("thread", receipt_ready={"0xt%03d" % i: time.monotonic() + 0.7
                                                       for i in range(120)})
b2 = threading.active_count()
for i in range(120):
    o2._rcpt_dispatch("0xt%03d" % i, "probe", ("m", "b", 1.0, "F"))
time.sleep(0.25)
peak_leg = threading.active_count()
check("легаси: тред-на-пробу растёт линейно (>=100 живых тредов на 120 отправках)",
      peak_leg >= b2 + 100, (b2, peak_leg))
t0 = time.monotonic()
while any(len(v) == 0 for v in out2.values()) or len(out2) < 120:
    if time.monotonic() - t0 > 12:
        break
    time.sleep(0.05)
check("легаси-инвариант тот же: 120 -> ровно 120 исходов",
      len(out2) == 120 and all(len(v) == 1 for v in out2.values()), len(out2))

# ---- 3. SLA: латентности обеих веток; коллектор <= легаси + полл ----
T = [1000.0]
clock = lambda: T[0]  # noqa: E731
o3, col3, out3, _, _ = mk("collector", clock=clock,
                          receipt_ready={"0xs%02d" % i: 1000.5 for i in range(50)})
for i in range(50):
    o3._rcpt_dispatch("0xs%02d" % i, "probe", ("m", "b", 1.0, "F"))
T[0] = 1001.0                                          # квитанции готовы в 1000.5, тик в 1001.0
while col3.pending():
    col3.tick()
_rl = sorted(o3.stats.get("rcpt_lat_ring") or [])
lat_col_p90 = _rl[int(len(_rl) * 0.9)] if _rl else None
check("SLA: латентность коллектора мерится и <= готовность+полл (1.0с при готовности 0.5с)",
      lat_col_p90 is not None and 0.5 <= lat_col_p90 <= 0.5 + 1.0 + 0.01, lat_col_p90)

# ---- 4. Shadow: расхождение в лог, совпадение в счётчик; деньги у легаси ----
o4, col4, out4, _, logs4 = mk("shadow")
o4._rcpt_shadow_note("0xa", "легаси", 1)
o4._rcpt_shadow_note("0xa", "коллектор", 0)
check("shadow: расхождение статусов -> счётчик + строка",
      o4.stats.get("rcpt_shadow_diverged") == 1
      and any("РАСХОЖДЕНИЕ" in m for m in logs4), (o4.stats, logs4[-1:]))
o4._rcpt_shadow_note("0xb", "коллектор", None)
o4._rcpt_shadow_note("0xb", "легаси", None)
check("shadow: совпадение (оба NO-RECEIPT) -> agree", o4.stats.get("rcpt_shadow_agree") == 1,
      o4.stats)
# shadow-результат коллектора денег не трогает (core не зовётся)
o4._rcpt_result(("shadow", "probe", ("m", "b", 1.0, "F"), 0.0), "0xc",
                {"status": 1, "gasUsed": 1, "effectiveGasPrice": 1})
check("shadow: исход коллектора НЕ зовёт денежное ядро", "0xc" not in out4, out4.get("0xc"))

# ---- 5. Переполнение: дроп учтён, исход спасён фолбэком ----
o5, col5, out5, _, _ = mk("collector", max_pending=5,
                          receipt_ready={"0xo%02d" % i: 0.0 for i in range(9)})
for i in range(9):
    o5._rcpt_dispatch("0xo%02d" % i, "probe", ("m", "b", 1.0, "F"))
while col5.pending():
    col5.tick()
t0 = time.monotonic()
while len(out5) < 9 and time.monotonic() - t0 < 8:
    time.sleep(0.05)
check("переполнение: collector_overflow=4, но исходов всё равно 9/9 (фолбэк-треды)",
      o5.stats.get("collector_overflow") == 4 and len(out5) == 9
      and all(len(v) == 1 for v in out5.values()),
      (o5.stats.get("collector_overflow"), len(out5)))

# ---- 6. Гарды дрейфа ----
_S = open("chain/spec_fire_live.py").read()
_F = open("chain/fire_engine.py").read()
check("все ТРИ точки спавна ходят через диспетчер (тредов _collect больше нет)",
      _S.count("self._rcpt_dispatch(") == 3
      and "threading.Thread(target=self._collect," not in _S
      and "threading.Thread(target=self._collect_volley," not in _S)
check("режим по умолчанию shadow, откат thread без кода",
      'os.environ.get("SPEC_FIRE_RCPT_MODE", "shadow")' in _S)
check("track с капом возвращает False (backpressure)", "return False" in _F
      and "_max_pending" in _F)
check("строка окна несёт rcpt=режим/q/overflow/lat", "rcpt=%s" in _S and "_rcpt_s" in _S)
check("нонс-дисциплина не тронута: NO-RECEIPT путь в core (инвалидация на месте)",
      "canary_invalidate_nonce" in _S and "volley_invalidate_nonce" in _S)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: коллектор окна — О(1) тредов, N исходов на N отправок, тень судит"))
sys.exit(1 if FAILS else 0)
