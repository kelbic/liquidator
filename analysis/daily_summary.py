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

# ---- ОСЬ НАБЛЮДЕНИЯ rate-класса (ревью 25.07): не «HF близко», а РАЗНИЦА СКОРОСТЕЙ ----
# margin = дрейф оракула − APR долга. margin>0 => цель уходит вверх (пег-ножи «в 0.4%» бессмертны);
# смена знака => класс ожил. APR адаптивен (Morpho IRM), за изломом утилизации растёт кратно —
# поэтому «уходит вверх» это УСЛОВИЕ под наблюдением, а не приговор.
try:
    from analysis.flip_eta import compute_rows, margin_changes
    _rows = compute_rows(cfg, hf_max=1.05, n=12)
    _rate = [r for r in _rows if not r["market_oracle"] and r["margin"] is not None]
    if _rate:
        _rate.sort(key=lambda r: r["margin"])
        lines.append("— ось rate-класса (дрейф−APR, + = уходит вверх):")
        for r in _rate[:5]:
            lines.append("   %-15s $%-9.0f HF=%.4f  APR %.2f%%  фид %+.2f%%  запас %+.2f%%/год"
                         % (r["pair"], r["debt_usd"], r["hf"], r["apr"] * 100,
                            (r["drift"] or 0) * 100, r["margin"] * 100))
    _ch = margin_changes(_rows)
    if _ch:
        lines.append("⚠️ RATE-КЛАСС: " + "; ".join(
            "%s %s (%s -> %+.2f%%)" % (p_, why, ("базлайн" if was is None else "%+.2f%%" % (was * 100)), now * 100)
            for _k, p_, was, now, why in _ch))
    _mkt = [r for r in _rows if r["market_oracle"]]
    if _mkt:
        lines.append("— рыночно-оракульные цели (там и живёт SPEC-FIRE): " +
                     ", ".join("%s $%.0fk HF=%.3f" % (r["pair"], r["debt_usd"] / 1e3, r["hf"])
                               for r in _mkt[:4]))
except Exception as e:
    lines.append(f"ось rate-класса упала: {type(e).__name__}: {str(e)[:70]}")

# ---- пульсы сторожей (урок 28.07: мёртвый сторож хуже отсутствующего) ----
# Независимый суточный канал надзора за самим heartbeat-check: если таймер надзора умрёт,
# его протухший пульс всплывёт здесь — в сводке, которую человек читает каждый день.
try:
    from heartbeat import age_sec
    from analysis.heartbeat_check import _host_registry, REGISTRY, SELF_THRESHOLD_SEC

    def _fmt_age(a):
        if a is None:
            return "нет"
        return "%.0fс" % a if a < 120 else "%.0fм" % (a / 60) if a < 7200 else "%.1fч" % (a / 3600)

    parts = []
    # пер-хостовый реестр (03.08): сырой REGISTRY показывал usde-watch 84.7ч⚠️ по мёртвому
    # локальному файлу — вотчер с 30.07 живёт на Вене (HB_WATCH_SKIP), надзор там свой
    for name in list(_host_registry()) + ["heartbeat-check"]:
        a = age_sec(name)
        thr = REGISTRY[name][1] if name in REGISTRY else SELF_THRESHOLD_SEC
        parts.append(name + " " + _fmt_age(a) + (" ⚠️" if a is None or a > thr else ""))
    lines.append("пульсы: " + "; ".join(parts))
except Exception as e:
    lines.append(f"пульсы сторожей: блок упал: {type(e).__name__}")

msg = "\n".join(lines)[:3900]
print(msg)
ok = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, antiflood_sec=0, tag=cfg.bot_tag).send(msg, key=f"daily:{dt.date.today()}")
print(f"\n[tg sent={ok}]")
if ok:
    from heartbeat import beat
    beat("daily-summary")
