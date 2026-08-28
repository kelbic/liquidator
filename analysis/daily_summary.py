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
if len(rest) > 1:                       # 28.08 kelbic: один рестарт = плановый деплой, не событие
    lines.append(f"⚠ рестартов: {len(rest)} ({'; '.join(t[7:] for t in rest[:4])}{'…' if len(rest) > 4 else ''})")

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
    pass                                 # 28.08 kelbic: «детекция» — самоотчёт механизма, из сводки убрана
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
    # 28.08 kelbic: ИСХОД первым, одной строкой — «где мы» отвечает счёт побед с цепи, не механизм
    lines.append(f"🏁 ИСХОД: наших побед {n_us} | чужих {n_liq - n_us} на ${vol:,.0f} "
                 f"(из них хот-класса ≥${cfg.hot_min_repaid_usd:.0f}: {n_hot})")
except Exception as e:
    lines.append(f"рыночный блок упал: {type(e).__name__}: {str(e)[:80]}")

# ---- rate-класс: 28.08 kelbic — таблица убрана, остаётся ТОЛЬКО событие (ETA < 30 дн) ----
try:
    from analysis.flip_eta import compute_rows
    _rows = compute_rows(cfg, hf_max=1.05, n=12)
    # ETA до кромки = (hf-1) / |margin| лет, margin<0 = цель идёт ВНИЗ (ключи строки: pair, debt_usd,
    # hf, margin — как в прежней таблице; eta_days у compute_rows НЕТ, считаем здесь)
    _soon = []
    for r in _rows:
        if r["market_oracle"] or r["margin"] is None or r["margin"] >= 0:
            continue
        eta = (r["hf"] - 1.0) / abs(r["margin"]) * 365.0
        if 0 <= eta < 30:
            _soon.append((eta, r))
    if _soon:
        _soon.sort()
        lines.append("⚠️ RATE-КЛАСС близко: " + "; ".join(
            f"{r['pair']} ${r['debt_usd']:,.0f} ETA {eta:.0f} дн" for eta, r in _soon[:3]))
except Exception:
    pass                                 # молчание блока — не событие для человека

# ---- пульсы сторожей: 28.08 kelbic — убраны (живость — не событие; мёртвость ловит heartbeat-check) ----

# ---- KPI «транзит+N» (kelbic 27.08): вход в призовую полосу по прибору ПОЗИЦИИ, было -> стало ----
# Читаем ТОЛЬКО нужные строки через grep в пайпе: полный журнал штормового дня (330k строк)
# уже уложил сводку OOM-ом 27.08 (256M), второе окно удвоило бы память.
def _pos_lines(since, until):
    try:
        cmd = ("journalctl -u liquidator-bot --since '%s' --until '%s' --no-pager -o cat | "
               "grep -E 'SPECFIRE ПОЗИЦИИ|первый_delta p50'" % (since, until))
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def _kpi(text):
    v = re.findall(r"ПОЗИЦИИ blk=(\d+).*?транзит idx=(\d+) tip=(\d+).*?наших (\d+), в полосе "
                   r"\[T\+1,T\+20\]=(\d+), tip-match=(\d+), побед=(\d+); наши idx (\d+)\.\.(\d+)", text)
    v = [tuple(int(x) for x in r) for r in v if int(r[3]) >= 5]        # окна с <5 наших — не вердикт
    if not v:
        return None
    n = len(v)
    exact = sum(1 for r in v if r[5] > 0)
    band = sum(1 for r in v if r[4] > 0)
    wins = sum(r[6] for r in v)
    # первый наш ПОСЛЕ транзита: по нижней границе диапазона, если она за транзитом; иначе
    # (наши стоят и до транзита) честнее взять кольцо p50 из close-строки
    after = sorted(r[7] - r[1] for r in v if r[7] > r[1])
    ring = re.findall(r"первый_delta p50=([+-]?\d+)", text)
    p50_ring = int(ring[-1]) if ring else None
    best = min(after) if after else None
    return dict(n=n, exact=exact, band=band, wins=wins, best=best, p50=p50_ring,
                band_mean=(sum(r[4] for r in v if r[4] > 0) / band) if band else 0.0)


try:
    _H = int(HOURS)
    now_k = _kpi(_pos_lines("-%dh" % _H, "now"))
    prev_k = _kpi(_pos_lines("-%dh" % (2 * _H), "-%dh" % _H))

    def _f(k):
        if not k:
            return "нет вердиктов"
        return ("n=%d, exact %d%% , в полосе %d%% (ср. %.0f наших), первый наш p50 %s, лучший +%s, побед %d"
                % (k["n"], 100 * k["exact"] // k["n"], 100 * k["band"] // k["n"], k["band_mean"],
                   ("%+d" % k["p50"]) if k["p50"] is not None else "—",
                   k["best"] if k["best"] is not None else "—", k["wins"]))
    lines.append("KPI транзит+N: " + _f(now_k))
    if prev_k and now_k:
        lines.append("  было (пред. %dч): exact %d%% -> %d%%, в полосе %d%% -> %d%%"
                     % (_H, 100 * prev_k["exact"] // prev_k["n"], 100 * now_k["exact"] // now_k["n"],
                        100 * prev_k["band"] // prev_k["n"], 100 * now_k["band"] // now_k["n"]))
except Exception as e:
    lines.append(f"KPI транзит+N: блок упал: {type(e).__name__}")

# ---- прогресс по плану: 28.08 kelbic — список коммитов и шаги плана убраны (стена текста) ----

msg = "\n".join(lines)[:3900]
print(msg)
import os as _os
if _os.environ.get("DAILY_DRY") == "1":            # проверка без боевого канала и без пульса
    print("\n[DRY: не отправлено, пульс не бьётся]")
    sys.exit(0)
ok = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, antiflood_sec=0, tag=cfg.bot_tag).send(msg, key=f"daily:{dt.date.today()}")
print(f"\n[tg sent={ok}]")
if ok:
    from heartbeat import beat
    beat("daily-summary")
