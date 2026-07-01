"""Точные UTC-таймстемпы последних реальных ликвидаций на cbXRP/USDC (наш лучше всего покрытый
money-market, UniV3-direct, без Kyber) — для сверки с журналом бота В ТЕ САМЫЕ моменты. Не ждём
каскада — cbXRP/USDC даёт ~35/день фон, значит за последние дни таких моментов было много, и
можно посмотреть, что бот делал (или не делал) РОВНО тогда. Переиспользует ту же _gql/_LIQ_QUERY
инфраструктуру, что competition_report.py. Read-only.
Запуск: cd /root/liquidator && /root/liquidator/venv/bin/python -m analysis.recent_cbxrp_timestamps
"""
import sys, time, datetime as dt
sys.path.insert(0, ".")
from config import Config
from strategy.scanner import load_covered_markets
from analysis.competition_report import _gql, _LIQ_QUERY

cfg = Config.from_env()
markets = load_covered_markets(cfg.covered_markets_path)
by_id = {m.market_id.lower(): m for m in markets}
our = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}

rows = []
skip = 0
since = int(time.time()) - 3 * 86400  # последние 3 дня достаточно для нескольких примеров
while skip <= 1000:
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
        mk = it.get("market") or {}
        m = by_id.get((mk.get("marketId") or "").lower())
        if not m:
            continue
        ca = mk.get("collateralAsset") or {}; la = mk.get("loanAsset") or {}
        pair = f"{ca.get('symbol','?')}/{la.get('symbol','?')}"
        if pair != "cbXRP/USDC":
            continue
        d = it.get("data") or {}
        liq = (d.get("liquidator") or "").lower()
        rows.append({"ts": ts, "who": "МЫ" if liq in our else "конкурент", "liquidator": d.get("liquidator")})
    if stop:
        break
    skip += 100

rows.sort(key=lambda r: -r["ts"])
print(f"Последние cbXRP/USDC ликвидации за 3 дня: {len(rows)}\n")
for row in rows[:15]:
    utc = dt.datetime.utcfromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  {utc} UTC  ({row['who']}, liquidator={row['liquidator']})")

if rows:
    print(f"\nВозьмите пару свежих timestamp'ов сверху и сверьте с логом бота вокруг них:")
    print(f'  journalctl -u liquidator-bot --since "<UTC-1мин>" --until "<UTC+1мин>" --no-pager')
