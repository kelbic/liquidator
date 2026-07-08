"""Ежедневная TG-сводка (P0-2, systemd timer liquidator-daily.timer). ДЁШЕВО: без Kyber-котировок
(в отличие от missed.py) — рестарты с временами, наши выстрелы/реверты, рыночные ликвидации на
covered-рынках (кол-во/объём/наши), здоровье детекции. Один TG-месседж, antiflood обойдён (key=дата).
Запуск вручную: cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) \
                /root/liquidator/venv/bin/python -m analysis.daily_summary [часов]   (по умолчанию 24)
"""
import sys, re, time, subprocess, datetime as dt
from collections import Counter
sys.path.insert(0, ".")
from config import Config
from alerts import Alerter

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
cfg = Config.from_env()
lines = [f"📊 liquidator за {HOURS:g}ч ({dt.datetime.now():%m-%d %H:%M})"]

# ---- журнал: рестарты (с временами), выстрелы, реверты, hot stats ----
try:
    out = subprocess.run(["journalctl", "-u", "liquidator-bot", "--since",
                          f"-{int(HOURS*3600)+60} seconds", "--no-pager"],
                         capture_output=True, text=True, timeout=30).stdout
except Exception as e:
    out = ""
    lines.append(f"journalctl недоступен: {type(e).__name__}")

rest = [re.match(r"\s*(\w{3} +\d+ \d\d:\d\d:\d\d)", l).group(1)
        for l in out.splitlines() if "EXECUTE mode" in l and re.match(r"\s*(\w{3} +\d+ \d\d:\d\d:\d\d)", l)]
lines.append(f"рестартов: {len(rest)}" + (f" ({'; '.join(t[7:] for t in rest[:6])}{'…' if len(rest) > 6 else ''})" if rest else ""))

shots = len(re.findall(r"HOT ACTIONABLE|ACTIONABLE \S+/\S+ HF", out))
lines.append(f"выстрелов (ACTIONABLE): {shots}")
reasons = Counter(re.findall(r"liquidate revert .*? reason=(.*?) tx=", out))
if reasons:
    lines.append("revert'ы: " + "; ".join(f"{n}x {r[:40]}" for r, n in reasons.most_common(4)))
warm = len(re.findall(r"warm-start:", out))
if rest and not warm:
    lines.append("⚠ рестарты БЕЗ warm-start в логе — снапшот не грузился, проверь candidates_snapshot.json")
hs = re.findall(r"hot stats .*", out)
if hs:
    lines.append("детекция: " + hs[-1].split("hot stats ", 1)[-1][:180])
else:
    lines.append("⚠ hot stats в журнале НЕТ — hot path не жив?")

# ---- рынок: ликвидации на covered-рынках (Morpho API, без котировок) ----
try:
    from analysis.competition_report import _gql, _LIQ_QUERY, _loan_price, classify
    from strategy.scanner import load_covered_markets
    by_id = {m.market_id.lower(): m for m in load_covered_markets(cfg.covered_markets_path)}
    our = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    cut = time.time() - HOURS * 3600
    price_cache, n_liq, n_us, vol, n_hot, skip = {}, 0, 0, 0.0, 0, 0
    while skip <= 2000:
        r = _gql(_LIQ_QUERY, {"first": 100, "skip": skip, "cid": [cfg.chain_id]})
        batch = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        if not batch:
            break
        stop = False
        for it in batch:
            if int(it["timestamp"]) < cut:
                stop = True; break
            mk = it.get("market") or {}
            if (mk.get("marketId") or "").lower() not in by_id:
                continue
            la = mk.get("loanAsset") or {}
            ru = int((it.get("data") or {}).get("repaidAssets") or 0) / 10 ** int(la.get("decimals") or 18) \
                * _loan_price(la.get("address", ""), cfg.chain_id, price_cache)
            n_liq += 1; vol += ru
            if classify((it.get("data") or {}).get("liquidator"), our) == "us":
                n_us += 1
            elif ru >= cfg.hot_min_repaid_usd:
                n_hot += 1
        if stop:
            break
        skip += 100
    lines.append(f"рынок (covered): {n_liq} ликв. на ${vol:,.0f}; наших {n_us}; "
                 f"чужих хот-класса (repaid>=${cfg.hot_min_repaid_usd:.0f}): {n_hot}")
except Exception as e:
    lines.append(f"рыночный блок упал: {type(e).__name__}: {str(e)[:80]}")

msg = "\n".join(lines)[:3900]
print(msg)
ok = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, antiflood_sec=0).send(msg, key=f"daily:{dt.date.today()}")
print(f"\n[tg sent={ok}]")
