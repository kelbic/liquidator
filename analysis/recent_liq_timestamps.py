"""Точные UTC-таймстемпы последних реальных ликвидаций на ВСЕХ покрытых рынках (не только cbXRP —
тот сейчас молчит 5 дней, но другие рынки живы: 25 событий 30 июня + 1 июля). Для немедленной
сверки с журналом бота в те же моменты — не ждём, берём что реально было последние пару дней.
Read-only, переиспользует ту же _gql/_LIQ_QUERY инфраструктуру.
Запуск: cd /root/liquidator && /root/liquidator/venv/bin/python -m analysis.recent_liq_timestamps [n]
"""
import sys, time, datetime as dt
sys.path.insert(0, ".")
from config import Config
from strategy.scanner import load_covered_markets
from analysis.competition_report import _gql, _LIQ_QUERY

cfg = Config.from_env()
n = int(sys.argv[1]) if len(sys.argv) > 1 else 20

markets = load_covered_markets(cfg.covered_markets_path)
by_id = {m.market_id.lower(): m for m in markets}
our = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}

rows = []
skip = 0
while len(rows) < n and skip <= 1000:
    r = _gql(_LIQ_QUERY, {"first": 100, "skip": skip, "cid": [cfg.chain_id]})
    if r.get("errors"):
        print("API errors:", r["errors"]); sys.exit(1)
    batch = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
    if not batch:
        break
    for it in batch:
        mk = it.get("market") or {}
        m = by_id.get((mk.get("marketId") or "").lower())
        if not m:
            continue
        ca = mk.get("collateralAsset") or {}; la = mk.get("loanAsset") or {}
        d = it.get("data") or {}
        liq = (d.get("liquidator") or "").lower()
        rows.append({
            "ts": int(it["timestamp"]),
            "pair": f"{ca.get('symbol','?')}/{la.get('symbol','?')}",
            "who": "МЫ" if liq in our else "конкурент",
            "liquidator": d.get("liquidator"),
        })
    skip += 100

rows.sort(key=lambda r: -r["ts"])
print(f"Последние {min(n, len(rows))} реальных ликвидаций на наших рынках:\n")
for row in rows[:n]:
    utc = dt.datetime.utcfromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  {utc} UTC  {row['pair']:<14} ({row['who']}, liquidator={row['liquidator']})")

if rows:
    r0 = rows[0]
    utc0 = dt.datetime.utcfromtimestamp(r0["ts"])
    since_s = (utc0 - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    until_s = (utc0 + dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nСамое свежее ({r0['pair']}, {utc0.strftime('%Y-%m-%d %H:%M:%S')} UTC) — сверить так:")
    print(f'  journalctl -u liquidator-bot --since "{since_s}" --until "{until_s}" --no-pager')
