"""Юниты PEAK-OWED fire-policy («го» 02.08): долг после пика, полоса гистерезиса,
sticky против disarm, [owed]-атрибуция (would-have-fired), откат SPEC_FIRE_PEAK_OWED=0."""
import threading
import time
import types

import chain.spec_fire_live as sfl
from chain.spec_fire_live import SpecFireLive

# боевой порог из .env Шарлотты (дефолт модуля $500k срезал бы цель на арме)
_orig_min = sfl.SPEC_FIRE_MIN_REPAID_USD
sfl.SPEC_FIRE_MIN_REPAID_USD = 1000.0

DEV = 0.005          # DOGE: порог огня 0.5%, полоса 0.3×dev => серая зона от 0.35%


def mk_shared():
    return types.SimpleNamespace(
        lock=threading.Lock(),
        groups={"0xm1": ["0xVIC"]},
        debt_by={("0xm1", "0xVIC"): 71_000.0},
        by_id={"0xm1": types.SimpleNamespace(collateral_token="cbDOGE", loan_token="USDC")})


def ret_script(segments):
    """[(до_смещения_сек, ret)] — значение по времени от старта окна."""
    t0 = time.monotonic()

    def fn(_label):
        dt = time.monotonic() - t0
        for until, v in segments:
            if dt < until:
                return v
        return segments[-1][1]
    return fn, t0


def run(segments, seconds, events=(), peak_owed=True, hold=None):
    """events: [(смещение_сек, dict-событие)] — впрыск в handle_event по ходу окна."""
    _po, _ho = sfl.SPEC_FIRE_PEAK_OWED, sfl.SPEC_FIRE_OWED_HOLD_SEC
    sfl.SPEC_FIRE_PEAK_OWED = peak_owed
    if hold is not None:
        sfl.SPEC_FIRE_OWED_HOLD_SEC = hold
    sends, logs = [], []
    cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                                tip_gwei=0.05, sequencer_rpc="https://seq",
                                eth_price_usd=1730.0, fast_lane_max_repaid_usd=500_000.0)
    fn, t0 = ret_script(segments)
    sf = SpecFireLive(rpc=None, cfg=cfg, shared=mk_shared(), ret_fn=fn,
                      dev_by={"DOGE": DEV},
                      build_fn=lambda mid, b, d: {"calldata": "0xCD", "gas": 100},
                      send_fn=lambda cd, g: (sends.append(time.monotonic() - t0), "0xH")[1],
                      receipt_fn=lambda h: (0, 0.01), log_fn=logs.append)
    sf.handle_event({"event": "arm", "feed": "DOGE", "dir": "down"})
    evq = sorted(events)
    while time.monotonic() - t0 < seconds:
        while evq and time.monotonic() - t0 >= evq[0][0]:
            sf.handle_event(evq.pop(0)[1])
        time.sleep(0.05)
    sf.handle_event({"event": "confirmed", "feed": "DOGE"})
    time.sleep(0.15)
    try:
        return sends, logs, sf
    finally:
        sfl.SPEC_FIRE_PEAK_OWED, sfl.SPEC_FIRE_OWED_HOLD_SEC = _po, _ho


# 1) Долг после отскока: пик 0.8с, отскок в ноль — огонь продолжается с [owed]-маркером
sends, logs, sf = run([(0.8, -0.01), (99.0, -0.0005)], seconds=2.6)
owed_sends = [t for t in sends if t > 1.0]
assert owed_sends, "после отскока пробы обязаны продолжаться (транзит заряжен)"
assert sf.stats["owed_probes"] >= 1, sf.stats
assert any("[owed" in m for m in logs), "would-have-fired обязан быть помечен в журнале"
assert any("peak-owed" in m and "отскок с пика" in m for m in logs), logs[-4:]
print("UNIT_OK: долг после пика — огонь в отскоке, [owed]-атрибуция")

# 2) Sticky: disarm при заряженном транзите окно НЕ убивает, confirmed — убивает
sends2, logs2, sf2 = run([(0.8, -0.01), (99.0, -0.0005)], seconds=3.0,
                         events=[(1.4, {"event": "disarm", "feed": "DOGE"})])
after_disarm = [t for t in sends2 if t > 1.6]
assert after_disarm, "окно с долгом обязано пережить disarm"
assert sf2.stats["sticky_windows"] == 1 and any("sticky" in m for m in logs2)
n_end = len(sends2)   # confirmed в конце run() окно закрыл — новых отправок нет
time.sleep(0.6)
assert len(sends2) == n_end, "confirmed обязан закрыть окно насмерть"
print("UNIT_OK: sticky — disarm пережит, confirmed закрывает")

# 3) Полоса гистерезиса: дрожание в серой зоне (0.4% > 0.35%) продлевает короткий долг;
#    уход из зоны в ноль — долг истекает, огонь глохнет
sends3, logs3, sf3 = run([(0.6, -0.01), (2.0, -0.004), (99.0, -0.0005)],
                         seconds=3.6, hold=0.7)
in_band = [t for t in sends3 if 1.4 <= t <= 2.0]     # hold=0.7 без полосы истёк бы к 1.3с
assert in_band, "серая зона обязана продлевать долг (иначе рулетка уровнем ниже)"
tail = [t for t in sends3 if t > 3.1]                # 2.0 (уход из зоны) + 0.7 + запас
assert not tail, "вне зоны долг обязан истечь: огонь глохнет, хвост=%s" % tail
print("UNIT_OK: полоса гистерезиса продлевает, уход из зоны гасит")

# 4) Откат SPEC_FIRE_PEAK_OWED=0: отскок глушит огонь, disarm убивает окно сразу
sends4, logs4, sf4 = run([(0.8, -0.01), (99.0, -0.0005)], seconds=2.4,
                         events=[(1.4, {"event": "disarm", "feed": "DOGE"})],
                         peak_owed=False)
assert not [t for t in sends4 if t > 1.1], "откат: пробы после отскока запрещены"
assert sf4.stats["owed_probes"] == 0 and sf4.stats["sticky_windows"] == 0
assert not any("[owed" in m for m in logs4)
print("UNIT_OK: откат SPEC_FIRE_PEAK_OWED=0 — прежняя политика")

sfl.SPEC_FIRE_MIN_REPAID_USD = _orig_min
print("=== ВСЕ PEAK-OWED ЮНИТЫ ПРОШЛИ ===")
