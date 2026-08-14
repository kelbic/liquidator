"""Дрель Э1б: дист-гейт hold во времени — три фазы темпа token bucket.

HOT (окно тикает) -> полный темп; ближний хвост NEAR_SEC после угасания -> NEAR_RATE (16/с,
деньги Ш26 живут тут); дальний хвост -> тонкий HOLD_RATE. Откат NEAR_RATE=0 = Э1a бит-в-бит.
Часы инжектируются — сеть и треды не трогаются.
"""
import os
import sys

os.environ.setdefault("SPEC_FIRE_ENGINE_HOLD_NEAR_RATE", "16")
os.environ.setdefault("SPEC_FIRE_ENGINE_HOLD_NEAR_SEC", "30")

import chain.fire_engine as fe                                # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def drain_rate(eng, clk, t0, dur=4.0, step=0.05):
    """Сколько жетонов ведро отдаёт за dur секунд с шагом step (бурст исчерпан заранее)."""
    n = 0
    clk.t = t0
    eng._tokens = 0.0
    eng._tok_ts = clk.t
    t_end = t0 + dur
    while clk.t < t_end:
        clk.t += step
        if eng._take_token():
            n += 1
    return n / dur


def mk():
    clk = Clock()
    eng = fe.FireEngine.__new__(fe.FireEngine)
    import threading
    eng._lock = threading.Lock()
    eng._now = clk
    eng._until = 0.0
    eng._hold_until = 0.0
    eng._rate = fe.ENGINE_MAX_RATE
    eng._tokens = 0.0
    eng._burst_tokens = 0.0
    eng._tok_ts = clk.t
    eng.stats = {}
    return eng, clk


# --- 1. HOT: полный темп ---
eng, clk = mk()
eng._until = 10_000.0                       # горячая фаза не кончается
r = drain_rate(eng, clk, 1000.0)
check("HOT ~ полный темп (>=19/с при шаге 20/с)", r >= 19.0, "%.1f/с" % r)

# --- 2. БЛИЖНИЙ ХВОСТ: NEAR_RATE (окно угасло 5с назад) ---
eng, clk = mk()
eng._until = 1000.0                         # горячая фаза кончилась в t=1000
r = drain_rate(eng, clk, 1005.0)            # 5с после угасания, внутри NEAR_SEC=30
check("ближний хвост ~ NEAR_RATE 16/с", 14.0 <= r <= 16.5, "%.1f/с" % r)

# --- 3. ДАЛЬНИЙ ХВОСТ: тонкий HOLD_RATE ---
_saved_rate = fe.ENGINE_HOLD_RATE
fe.ENGINE_HOLD_RATE = 1.0                   # боевое значение Шарлотты (.env)
try:
    eng, clk = mk()
    eng._until = 1000.0
    r = drain_rate(eng, clk, 1040.0)        # 40с после угасания, за NEAR_SEC=30
    check("дальний хвост ~ HOLD_RATE 1/с", 0.5 <= r <= 1.5, "%.1f/с" % r)

    # --- 4. ОТКАТ: NEAR_RATE=0 -> ближний хвост тоже тонкий (Э1a бит-в-бит) ---
    _saved = fe.ENGINE_HOLD_NEAR_RATE
    fe.ENGINE_HOLD_NEAR_RATE = 0.0
    try:
        eng, clk = mk()
        eng._until = 1000.0
        r = drain_rate(eng, clk, 1005.0)
        check("откат NEAR_RATE=0: ближний хвост = HOLD_RATE", 0.5 <= r <= 1.5, "%.1f/с" % r)
        check("откат: in_hold_near()=False", eng.in_hold_near() is False)
    finally:
        fe.ENGINE_HOLD_NEAR_RATE = _saved
finally:
    fe.ENGINE_HOLD_RATE = _saved_rate

# --- 5. Граница NEAR_SEC точная ---
eng, clk = mk()
eng._until = 1000.0
clk.t = 1000.0 + fe.ENGINE_HOLD_NEAR_SEC - 0.01
check("на границе изнутри: near", eng.in_hold_near() is True)
clk.t = 1000.0 + fe.ENGINE_HOLD_NEAR_SEC + 0.01
check("за границей: не near", eng.in_hold_near() is False)
eng._until = clk.t + 100
check("в HOT: не near (горячая фаза не хвост)", eng.in_hold_near() is False)

# --- 6. Бурст ближнего хвоста остаётся HOLD_BURST (ровный поток, не вспышка) ---
eng, clk = mk()
eng._until = 1000.0
clk.t = 1005.0
eng._tokens = 0.0
eng._tok_ts = clk.t - 100.0                 # ведро копилось 100с на NEAR_RATE
eng._take_token()
check("бурст ближнего хвоста клампится HOLD_BURST", eng._tokens <= fe.ENGINE_HOLD_BURST,
      str(eng._tokens))

# --- 7. Проводка приёмки: sent_hold_near считается в send-цикле ---
src = open(os.path.join(os.path.dirname(fe.__file__), "fire_engine.py")).read()
check("проводка: sent_hold_near в send-цикле", 'stats["sent_hold_near"]' in src and "in_hold_near()" in src)
check("боевой дефолт NEAR_RATE>0", fe.ENGINE_HOLD_NEAR_RATE > 0)

if FAILS:
    print("=== ДРЕЛЬ Э1б КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ Э1б ЗЕЛЁНАЯ: три фазы темпа, откат бит-в-бит, приёмка проведена ===")
