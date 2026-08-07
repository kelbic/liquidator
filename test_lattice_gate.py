"""Юниты фазового гейта по решётке OCR (07.08, «го» kelbic).

Замер (analysis/push_hazard.py, 2520 пушей/7д): пуш садится на границе 30с-раунда от
предыдущего — ±2с накрывает 96% у BTC/ETH при гэпе <1800с. Значит 24 секунды из 30 в горячей
фазе холостые. Проверено отдельно, что решётка держится на КРУПНЫХ движениях (внутрифидовые
квартили 87/91/90/90 против контроля 90/90/89/89) — гейт не слепнет там, где приз.

ГЛАВНОЕ, ЧТО ТУТ ПРОВЕРЯЕТСЯ — FAIL-OPEN: цена ложного закрытия гейта = пропущенная гонка,
поэтому любое сомнение обязано открывать огонь. Без сети.
"""
import sys
import threading
import time
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive

sfl_mod.SPEC_FIRE_PEAK_OWED = False

P = sfl_mod.SPEC_FIRE_LATTICE_P
TOL = sfl_mod.SPEC_FIRE_LATTICE_TOL
MIN_N = sfl_mod.SPEC_FIRE_LATTICE_MIN_N
LOGS = []
CLOCK = {"t": 100000.0}


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l, bonus=0.05)


shared = types.SimpleNamespace(lock=threading.Lock(), groups={"0xbtc": ["0xFAT"]},
                              debt_by={("0xbtc", "0xFAT"): 1_500_000.0},
                              by_id={"0xbtc": mk("cbBTC", "USDC")})
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)
sent = []


def make(ret=0.0, now=None):
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: ret,
                        dev_by={"BTC": 0.0010},
                        build_fn=lambda m, b, d: {"calldata": "0xde", "gas": 625_000},
                        send_fn=lambda c, g: (sent.append(c), "0xh%d" % len(sent))[1],
                        receipt_fn=lambda h: (0, 0.011), log_fn=LOGS.append,
                        now=now or (lambda: CLOCK["t"]))


def lattice_hist(sf, feed="BTC", n=MIN_N + 2, step=P, t0=None):
    """История пушей РОВНО по решётке — гейт обязан включиться."""
    t0 = CLOCK["t"] - n * step if t0 is None else t0
    sf._push_ts[feed] = [t0 + i * step for i in range(n + 1)]
    return sf._push_ts[feed][-1]


# ---- 1. FAIL-OPEN: истории нет ----
sf = make()
assert sf._gate_open("BTC") is True
assert sf._lattice_frac("BTC") == -1.0
print("UNIT_OK: без истории пушей гейт открыт (fail-open)")

# ---- 2. FAIL-OPEN: пушей меньше порога ----
sf = make()
sf._push_ts["BTC"] = [CLOCK["t"] - i * P for i in range(MIN_N - 2, 0, -1)]
assert sf._gate_open("BTC") is True
print("UNIT_OK: гэпов меньше MIN_N — гейт открыт (fail-open)")

# ---- 3. FAIL-OPEN: решётки нет (гэпы вразнобой) ----
sf = make()
t = CLOCK["t"] - 10000.0
ts = [t]
for k in range(MIN_N + 4):                       # шаги, не кратные P и вне допуска
    t += 7.0 + (k % 5) * 3.0
    ts.append(t)
sf._push_ts["BTC"] = ts
assert sf._lattice_frac("BTC") < sfl_mod.SPEC_FIRE_LATTICE_MIN_FRAC
assert sf._gate_open("BTC") is True
print("UNIT_OK: решётка НЕ подтверждена потоком — гейт открыт (fail-open)")

# ---- 4. Гейт действует: фаза решает ----
sf = make()
last = lattice_hist(sf)
assert sf._lattice_ready("BTC"), sf._lattice_frac("BTC")
for k, want in ((0.0, True), (TOL, True), (TOL + 1.0, False), (P / 2.0, False),
                (P - TOL, True), (P, True),
                (2 * P + TOL, True),          # через два раунда узел ещё узел
                (2 * P + P / 3.0, False)):    # ...а середина раунда — по-прежнему холостая
    got = sf._gate_open("BTC", now=last + k)
    assert got is want, "фаза +%.0fс: ждали %s, получили %s" % (k, want, got)
print("UNIT_OK: гейт по фазе — узел открыт, середина раунда закрыта")

# ---- 5. FAIL-OPEN: фаза протухла (гэп > MAX_AGE) ----
assert sf._gate_open("BTC", now=last + sfl_mod.SPEC_FIRE_LATTICE_MAX_AGE + 15.0) is True
print("UNIT_OK: фаза старше MAX_AGE — гейт снимается (fail-open)")

# ---- 6. Откат флагом ----
sfl_mod.SPEC_FIRE_LATTICE_GATE = False
try:
    assert sf._gate_open("BTC", now=last + P / 2.0) is True
finally:
    sfl_mod.SPEC_FIRE_LATTICE_GATE = True
print("UNIT_OK: откат SPEC_FIRE_LATTICE_GATE=0 — гейт всегда открыт")

# ---- 7. note_push: дедуп парных событий ----
sf = make()
CLOCK["t"] = 200000.0
sf.note_push("BTC")
sf.note_push("BTC")                              # confirmed+push об одном апдейте
assert len(sf._push_ts["BTC"]) == 1, sf._push_ts["BTC"]
CLOCK["t"] += 6.0
sf.note_push("BTC")
assert len(sf._push_ts["BTC"]) == 2
print("UNIT_OK: note_push дедупит парные события (<5с), дальше пишет")

# ---- 8. Живой валидатор: попадание в фазу и промах мимо ----
sf = make()
last = lattice_hist(sf)
CLOCK["t"] = last + P                            # ровно на узле
sf.note_push("BTC")
assert sf.stats["lattice_hits"] == 1 and sf.stats["lattice_misses"] == 0, sf.stats
CLOCK["t"] = CLOCK["t"] + P / 2.0                # мимо узла
sf.note_push("BTC")
assert sf.stats["lattice_misses"] == 1, sf.stats
assert any("ВНЕ фазы" in m for m in LOGS), "промах обязан быть виден в логе"
print("UNIT_OK: живой валидатор считает попадания/промахи и кричит о промахе")

# ---- 9. Буфер меток не растёт без предела ----
sf = make()
CLOCK["t"] = 300000.0
for _ in range(200):
    CLOCK["t"] += P
    sf.note_push("BTC")
assert len(sf._push_ts["BTC"]) <= 64, len(sf._push_ts["BTC"])
print("UNIT_OK: буфер меток ограничен (утечки нет)")

# ---- 10. Проводка в петле окна: закрытый гейт НЕ даёт стрелять, открытый даёт ----
_orig_facts = sfl_mod._facts_fresh
sfl_mod._facts_fresh = lambda now_wall=None: True
try:
    for closed, expect_fire in ((True, False), (False, True)):
        sent.clear()
        sf = make(ret=-0.0011, now=time.monotonic)
        sf._gate_open = (lambda feed, now=None: not closed)
        sf.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
        time.sleep(1.2)
        with sf._lock:
            st = sf._windows.pop("BTC", None)
        if st is not None:
            st.set()
        got = len(sent) > 0
        assert got is expect_fire, "гейт closed=%s: выстрелов %d" % (closed, len(sent))
        if closed:
            assert sf.stats["lattice_skips"] > 0, sf.stats
    print("UNIT_OK: петля окна уважает гейт (закрыт — 0 проб и счётчик пропусков)")
finally:
    sfl_mod._facts_fresh = _orig_facts

print("ПРОШЛИ: фазовый гейт по решётке OCR")
