"""Ручка порога арма (замер 06.08: альты армятся позже реального триггера оракула).
Инвариант №1 — дефолт бит-в-бит прежний. Инвариант №2 — гистерезис жив при любом ratio."""
import importlib, os, sys

def _fresh():
    for m in ("chain.predict",):
        sys.modules.pop(m, None)
    return importlib.import_module("chain.predict")

# 1. дефолт = прежнее поведение (arm 0.9*dev, disarm 0.7*dev)
for k in list(os.environ):
    if k.startswith("PREDICT_ARM_RATIO") or k == "PREDICT_DISARM_RATIO":
        del os.environ[k]
p = _fresh()
e = p.PredictEngine()
f = e.feeds["XRPUSDT"]
assert abs(f.arm_pct - 0.0050 * 0.9) < 1e-12, f.arm_pct
assert abs(f.disarm_pct - 0.0050 * 0.7) < 1e-12, f.disarm_pct
b = e.feeds["BTCUSDT"]
assert abs(b.arm_pct - 0.0010 * 0.9) < 1e-12, b.arm_pct
print("UNIT_OK: дефолт 0.9/0.7 — поведение бит-в-бит прежнее")

# 2. пер-фидовое переопределение бьёт только свой фид
os.environ["PREDICT_ARM_RATIO_XRP"] = "0.65"
p = _fresh(); e = p.PredictEngine()
assert abs(e.feeds["XRPUSDT"].arm_pct - 0.0050 * 0.65) < 1e-12, e.feeds["XRPUSDT"].arm_pct
assert abs(e.feeds["BTCUSDT"].arm_pct - 0.0010 * 0.9) < 1e-12, "BTC не должен пострадать"
assert abs(e.feeds["ADAUSDT"].arm_pct - 0.0050 * 0.9) < 1e-12, "ADA не должен пострадать"
print("UNIT_OK: PREDICT_ARM_RATIO_XRP бьёт только XRP")

# 3. ГЛАВНОЕ: гистерезис не переворачивается при низком ratio
os.environ["PREDICT_ARM_RATIO"] = "0.65"
p = _fresh(); e = p.PredictEngine()
for sym, f in e.feeds.items():
    assert f.disarm_pct < f.arm_pct, f"{sym}: disarm {f.disarm_pct} >= arm {f.arm_pct}"
print("UNIT_OK: disarm < arm при ratio 0.65 (гистерезис цел)")

# 4. арм реально наступает раньше: XRP-каскад 06.08 (ret -0.4319% при пороге 0.45%)
del os.environ["PREDICT_ARM_RATIO_XRP"]          # снять пер-фидовый оверрайд теста 2
os.environ["PREDICT_ARM_RATIO"] = "0.9"
p = _fresh(); e = p.PredictEngine()
assert 0.004319 < e.feeds["XRPUSDT"].arm_pct, "при 0.9 каскад 06.08 не армится — так и было"
os.environ["PREDICT_ARM_RATIO"] = "0.65"
p = _fresh(); e = p.PredictEngine()
assert 0.004319 > e.feeds["XRPUSDT"].arm_pct, "при 0.65 каскад 06.08 армился бы"
print("UNIT_OK: каскад cbXRP 06.08 (ret -0.4319%) армится при 0.65 и не армится при 0.9")

for k in list(os.environ):
    if k.startswith("PREDICT_ARM_RATIO") or k == "PREDICT_DISARM_RATIO":
        del os.environ[k]
print("ПРОШЛИ 4/4")
