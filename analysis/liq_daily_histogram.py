"""Разбивка реальных ликвидаций на наших покрытых рынках по ДНЯМ (UTC) — проверяет, был ли фон
~35/день ровным за всё окно, или события сконцентрированы в начале (после чего затишье). Тот же
источник данных, что competition_report.py, но без дорогой перекотировки — только счёт по дням,
быстро. Read-only.
Запуск: cd /root/liquidator && /root/liquidator/venv/bin/python -m analysis.liq_daily_histogram [days]
"""
import sys, time, datetime as dt
from collections import Counter
sys.path.insert(0, ".")
from config import Config
from strategy.scanner import load_covered_markets
from analysis.competition_report import _gql, _LIQ_QUERY

cfg = Config.from_env()
days = int(sys.argv[1]) if len(sys.argv) > 1 else 21

markets = load_covered_markets(cfg.covered_markets_path)
by_id = {m.market_id.lower(): m for m in markets}

since = int(time.time()) - days * 86400
by_day = Counter()
by_day_cbxrp = Counter()
skip = 0
scanned = 0
while skip <= 4000:
    r = _gql(_LIQ_QUERY, {"first": 100, "skip": skip, "cid": [cfg.chain_id]})
    if r.get("errors"):
        print("API errors:", r["errors"]); sys.exit(1)
    batch = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
    if not batch:
        break
    stop = False
    for it in batch:
        ts = int(it["timestamp"])
        if ts < since:
            stop = True; break
        scanned += 1
        mk = it.get("market") or {}
        m = by_id.get((mk.get("marketId") or "").lower())
        if not m:
            continue
        day = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        by_day[day] += 1
        ca = mk.get("collateralAsset") or {}; la = mk.get("loanAsset") or {}
        if f"{ca.get('symbol','?')}/{la.get('symbol','?')}" == "cbXRP/USDC":
            by_day_cbxrp[day] += 1
    if stop:
        break
    skip += 100

print(f"Просмотрено Base-ликвидаций: {scanned} за {days}д\n")
print(f"{'дата':<12} {'наши рынки':>11} {'cbXRP/USDC':>11}  гистограмма")
today = dt.datetime.utcnow().date()
for i in range(days, -1, -1):
    d = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
    n = by_day.get(d, 0)
    nx = by_day_cbxrp.get(d, 0)
    bar = "█" * min(n, 50)
    print(f"{d:<12} {n:>11} {nx:>11}  {bar}")

total = sum(by_day.values())
print(f"\nВсего на наших рынках за {days}д: {total} (~{total/max(1,days):.1f}/день в среднем по окну)")
