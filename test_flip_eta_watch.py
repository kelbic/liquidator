"""Юниты вотчера rate-класса (28.07: известные дальние минусы долбили сводку ежедневно —
kelbic попросил убрать; кричим только на события). Без сети: margin_changes чист по файлу."""
import json
import os
import sys
import tempfile

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from analysis.flip_eta import margin_changes, ETA_ALERT_DAYS


def row(key, pair, margin, hf=1.015, market=False):
    return {"key": key, "pair": pair, "margin": margin, "hf": hf, "market_oracle": market,
            "debt_usd": 4e5, "apr": 0.03, "drift": 0.02, "borrower": "0x0"}


def st(**kv):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(kv, f)
    f.close()
    return f.name


# 1) известный дальний минус МОЛЧИТ (сегодняшний кейс: wstETH/msETH -0.67%, ETA 828 дн)
p = st(**{"k1": -0.0064})
assert margin_changes([row("k1", "wstETH/msETH", -0.0067)], path=p) == []
print("UNIT_OK: известный дальний минус молчит (было -0.64%, стало -0.67%)")

# 2) смена знака кричит
p = st(**{"k1": 0.004})
got = margin_changes([row("k1", "weETH/WETH", -0.001)], path=p)
assert len(got) == 1 and "ЗНАК" in got[0][4], got
print("UNIT_OK: смена знака (+ -> -) кричит")

# 3) новая цель сразу с минусом — один раз (второй прогон молчит: состояние записано)
p = st()
got = margin_changes([row("k2", "yoUSD/USDC", -0.0144)], path=p)
assert len(got) == 1 and "новая цель" in got[0][4], got
assert margin_changes([row("k2", "yoUSD/USDC", -0.0143)], path=p) == []
print("UNIT_OK: новая цель с минусом — ровно один крик")

# 4) короткий фитиль (ETA<=%.0f дн) кричит ЕЖЕДНЕВНО даже по известному минусу
p = st(**{"k3": -0.06})
r = row("k3", "LBTC/cbBTC", -0.062, hf=1.005)   # ln(1.005)/0.062*365 ~ 29 дн
got = margin_changes([r], path=p)
assert len(got) == 1 and "БЛИЗКО" in got[0][4], got
got = margin_changes([r], path=p)
assert len(got) == 1, "короткий фитиль обязан напоминать каждый прогон"
print("UNIT_OK: ETA<=%.0f дн кричит ежедневно" % ETA_ALERT_DAYS)

# 5) резкое ухудшение известного минуса (х1.5 = APR-скачок) кричит
p = st(**{"k4": -0.004})
got = margin_changes([row("k4", "wstETH/msETH", -0.007)], path=p)
assert len(got) == 1 and "ухудшился" in got[0][4], got
p = st(**{"k4": -0.004})
assert margin_changes([row("k4", "wstETH/msETH", -0.005)], path=p) == [], "лёгкий дрейф минуса — тишина"
print("UNIT_OK: ухудшение x1.5 кричит, лёгкий дрейф — нет")

# 6) рыночные оракулы и margin=None не участвуют в оси
p = st()
assert margin_changes([row("k5", "cbBTC/USDC", -0.5, market=True),
                       row("k6", "x/y", None)], path=p) == []
print("UNIT_OK: рыночные оракулы/None вне оси")

print("=== ВСЕ FLIP-ETA-WATCH ЮНИТЫ ПРОШЛИ ===")
