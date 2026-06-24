"""Сводка 'пропущенные ликвидации vs наша реакция' с датами-часами и причиной пропуска. Read-only.
Запуск:  cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) \
         /root/liquidator/venv/bin/python -m analysis.missed [часов]    (по умолчанию 24)
"""
import sys, time, datetime as dt, subprocess, re
from collections import Counter
sys.path.insert(0, ".")
from config import Config

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
BLOCK1_HOURS = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0   # окно блока [1] (ликвидаций много) — отдельно
since_dt = dt.datetime.now() - dt.timedelta(hours=HOURS)
print(f"=== Окно: последние {HOURS:g}ч (блок [1] ликвидаций: {BLOCK1_HOURS:g}ч) (с {since_dt:%Y-%m-%d %H:%M} локального) ===\n")

# ---------- 1. РЫНОК: ликвидации с датами + РЕАЛЬНЫМ net (как competition_report) ----------
print("───────── [1] РЫНОЧНЫЕ ЛИКВИДАЦИИ (с датами + реальный net живой котировкой) ─────────")
try:
    from analysis.competition_report import _gql, _LIQ_QUERY, _loan_price, classify, kyber_quote, real_net_usd
    from strategy.scanner import load_covered_markets
    cfg = Config.from_env()
    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower(): m for m in markets}
    our = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    cost_usd = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
    floor = cfg.min_profit_usd
    HOT = cfg.hot_min_repaid_usd  # флор СКОРОСТИ хот-пути — читаем из конфига (не хардкод!)
    cut = time.time() - BLOCK1_HOURS * 3600   # блок [1]: своё окно
    price_cache, rows, skip = {}, [], 0
    where = {"type_in": ["Liquidation"], "chainId_in": [cfg.chain_id]}
    while skip <= 2000:
        r = _gql(_LIQ_QUERY, {"first": 100, "skip": skip, "where": where})
        batch = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        if not batch:
            break
        stop = False
        for it in batch:
            ts = int(it["timestamp"])
            if ts < cut:
                stop = True; break
            mk = it.get("market") or {}
            m = by_id.get((mk.get("marketId") or "").lower())
            if not m:
                continue
            d = it.get("data") or {}
            la = mk.get("loanAsset") or {}; ca = mk.get("collateralAsset") or {}
            ldec = int(la.get("decimals") or 18)
            lprice = _loan_price(la.get("address", ""), cfg.chain_id, price_cache)
            ra = int(d.get("repaidAssets") or 0); sz = int(d.get("seizedAssets") or 0)
            rows.append({"ts": ts, "pair": f"{ca.get('symbol','?')}/{la.get('symbol','?')}",
                         "repaid_usd": ra / 10 ** ldec * lprice, "who": classify(d.get("liquidator"), our),
                         "coll": ca.get("address", ""), "loan": la.get("address", ""),
                         "ldec": ldec, "lprice": lprice, "seized": sz, "repaid_assets": ra})
        if stop:
            break
        skip += 100
    rows.sort(key=lambda x: -x["ts"])
    print(f"  считаю живые котировки KyberSwap на {len(rows)} ликвидаций (~{len(rows)*0.5:.0f}с)...\n")
    now = time.time()
    table_net = 0.0; n_hot = n_block = 0
    for r in rows:
        rn = None
        if r["who"] != "us" and r["seized"] > 0 and r["repaid_usd"] >= 50:
            try:
                out = kyber_quote(r["coll"], r["loan"], r["seized"])
                rn = real_net_usd(out, r["repaid_assets"], r["ldec"], r["lprice"], cost_usd) if out else None
            except Exception:
                rn = None
            time.sleep(0.12)
        age_h = (now - r["ts"]) / 3600
        age = f"{age_h:.1f}ч" if age_h < 24 else f"{age_h/24:.1f}д"
        netstr = "  net      —" if rn is None else f"  net ~${rn:>7,.0f}"
        if r["who"] == "us":
            tag = "✅ ВЗЯЛИ МЫ"
        elif rn is None:
            tag = "НЕТ МАРШРУТА (не выйти)" if r["repaid_usd"] >= 50 else "· dust (не котирую)"
        elif rn >= floor and r["repaid_usd"] >= HOT:
            tag = f"◀◀◀ ХОТ-ЦЕЛЬ ПРОПУЩЕНА (repaid>=${HOT:.0f}) — NOT TAKEN"; table_net += rn; n_hot += 1
        elif rn >= floor:
            tag = f"·  СТОИТ — блок-луп (ниже хот-флора ${HOT:.0f}) [block-loop] NOT TAKEN"; table_net += rn; n_block += 1
        elif rn > 0:
            tag = "·  пыль (< $5 net)"
        else:
            tag = "·  убыток (газ съедает)"
        print(f"  {dt.datetime.fromtimestamp(r['ts']):%m-%d %H:%M} ({age:>5})  {r['pair']:<13} repaid ${r['repaid_usd']:>8,.0f}{netstr}  {tag}")
    print(f"\n  ИТОГ: стоящих ПРОПУЩЕНО {n_hot+n_block} (хот-цель >=${HOT:.0f}: {n_hot} | блок-луп ниже: {n_block}) "
          f"| реально на столе ~${table_net:,.0f}")
    print(f"  (>=${HOT:.0f} repaid = где хот-путь стреляет; ниже — задача блок-лупа; оба сейчас проигрывают по латентности)")
    # --- сверка: по каким хот-целям мы РЕАЛЬНО стреляли (ACTIONABLE в логах в ту же минуту) ---
    import subprocess as _sp
    _jl = _sp.run(["journalctl","-u","liquidator-bot","--since",f"-{int(HOURS*3600)+60} seconds","--no-pager"],
                  capture_output=True, text=True, timeout=30).stdout
    _shot_min = set(re.findall(r"(\d\d:\d\d):\d\d .*ACTIONABLE", _jl))
    _hot = [r for r in rows if r["who"]!="us" and r["repaid_usd"]>=HOT]
    _eng = sum(1 for r in _hot if dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M") in _shot_min)
    print(f"  из {len(_hot)} хот-целей: по {_eng} мы СТРЕЛЯЛИ (ACTIONABLE есть -> проигр. гонку по латентности),")
    print(f"     по {len(_hot)-_eng} НЕ участвовали (спали/рестарт/были ниже флора, действовавшего В ТОТ момент)")
    print(f"  -> ТОЧНУЮ причину непобеды даёт блок [3] reason=, НЕ метка в строках выше")
except Exception as e:
    print(f"  [1] фетч/котировки упали: {type(e).__name__}: {str(e)[:120]}")
    print(f"  фоллбэк: env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) {sys.executable} -m analysis.competition_report 1")

# ---------- журналлог-хелпер ----------
def jlog(grep, hours):
    since = f"-{int(hours*3600)+60} seconds"
    try:
        out = subprocess.run(["journalctl", "-u", "liquidator-bot", "--since", since, "--no-pager"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return f"  (journalctl err: {e})"
    keep = [ln for ln in out.splitlines() if re.search(grep, ln)]
    return "\n".join("  " + ln for ln in keep) or "  (нет)"

# ---------- 2. НАШИ ВЫСТРЕЛЫ (отдельно от рестартов) ----------
print("\n───────── [2] НАШИ ВЫСТРЕЛЫ (ACTIONABLE / submitted:N / revert) ─────────")
print(jlog(r"ACTIONABLE|liquidate revert", HOURS))
_rest = jlog(r"EXECUTE mode", HOURS)
n_rest = 0 if _rest.strip() == "(нет)" else sum(1 for l in _rest.splitlines() if "EXECUTE" in l)
print(f"\n  (рестартов/деплоев за окно: {n_rest} — это НЕ выстрелы, а перезапуски бота; "
      f"если их больше, чем ты деплоил — был crash-loop)")

# ---------- 3. РАСКЛАД revert-причин ----------
print("\n───────── [3] РАСКЛАД revert-ПРИЧИН (выборка для решения по латентности) ─────────")
try:
    since = f"-{int(HOURS*3600)+60} seconds"
    out = subprocess.run(["journalctl", "-u", "liquidator-bot", "--since", since, "--no-pager"],
                         capture_output=True, text=True, timeout=30).stdout
    reasons = re.findall(r"liquidate revert .*? reason=(.*?) tx=", out)
    if reasons:
        for reason, n in Counter(reasons).most_common():
            print(f"  {n:>3}x  {reason}")
        healthy = sum(n for rr, n in Counter(reasons).items() if "healthy" in rr.lower())
        print(f"\n  ИТОГО revert'ов: {len(reasons)} | 'position healthy' (проигр. гонка): {healthy}")
        if len(reasons) >= 5:
            pct = 100 * healthy / len(reasons)
            verdict = "ЛАТЕНТНОСТЬ доминирует → вариант A (срез build) оправдан" if pct >= 60 else "смешанные причины → смотреть структурные"
            print(f"  → {pct:.0f}% lost-race. {verdict}")
        else:
            print("  (нужно ≥5 revert'ов для статвывода; пока копим)")
    else:
        print("  (revert'ов с reason= нет — либо не было выстрелов, либо все submitted:1)")
except Exception as e:
    print(f"  (err: {e})")

# ---------- 4. Здоровье детекции (последние ~12 мин, не всё окно) ----------
print("\n───────── [4] ЗДОРОВЬЕ ДЕТЕКЦИИ (последние hot stats) ─────────")
print(jlog(r"hot stats", min(HOURS, 0.2)))
print("  (норма: poll≈60, poll_none=0, poll_fb=0, воронка proceed+skip+none ≈ spawn+pspawn)")
