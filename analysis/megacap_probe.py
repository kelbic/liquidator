"""Drift-probe капа mega-fetch: есть ли near-risk НИЖЕ top-N по долгу (ранговая слепота bounded-fetch)
и что реально даёт HF-дополнение (_fetch_market_hf_filtered). Read-only, только Morpho API.
Запуск (Charlotte):  /root/liquidator/venv/bin/python -m analysis.megacap_probe [marketKey] [lltv]
По умолчанию cbBTC/USDC (единственный MEGA_MARKET). Родился 07-10 из watch-item «DIAG mega-fetch cap»
(замер тогда: rank-3000 = $82.5k долга; в рангах 3001-10000 ноль HF<=1.10, но 57 позиций HF<=1.12).
"""
import sys
sys.path.insert(0, ".")
from chain.morpho import (_query_page, _fetch_market_bounded, _fetch_market_hf_filtered,
                          MORPHO_API_URL, MEGA_FETCH_MAX_PAGES, MEGA_HF_FILTER, _int, _num)

KEY = sys.argv[1] if len(sys.argv) > 1 else "0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836"
LLTV = float(sys.argv[2]) if len(sys.argv) > 2 else 0.86
CEILING = 1.10   # == main.HF_API_CEILING (main не импортируем — тяжёлый web3-импорт не нужен пробнику)
FLOOR = 450.0    # hot-floor для near-risk-среза (сам бот кандидатит от MIN_DEBT_USD=$1)


def hf(b: float, c: float) -> float:
    return (c * LLTV / b) if b > 0 else float("inf")


def usd(it) -> tuple[float, float]:
    st = it.get("state") or {}
    return _num(st.get("borrowAssetsUsd")), _num(st.get("collateralUsd"))


# --- 1. Ранговый скан до API-skip-лимита: слепота bounded-капа как она есть ---
rows = []
tail_reached = False
for skip in range(0, 10000, 1000):
    batch = _query_page([KEY], skip, 1000, MORPHO_API_URL, 15)
    for it in batch:
        if _int((it.get("state") or {}).get("borrowShares")) == 0:
            tail_reached = True
            break
        rows.append(usd(it))
    if tail_reached or len(batch) < 1000:
        break

cap = MEGA_FETCH_MAX_PAGES * 1000
print(f"borrowers (до skip-лимита 10000): {len(rows)}; supplier-tail: {tail_reached}")
if len(rows) >= cap:
    print(f"debt @rank{cap} (граница bounded-слепоты): ${rows[cap - 1][0]:,.0f}")
tail = rows[cap:]
if tail:
    near = [(b, hf(b, c)) for b, c in tail if b >= FLOOR and hf(b, c) <= CEILING]
    print(f"хвост {cap + 1}..{len(rows)}: долги ${tail[-1][0]:,.0f}..${tail[0][0]:,.0f}; "
          f"NEAR-RISK (debt>=${FLOOR:.0f}, HF<={CEILING}) = {len(near)}  <-- было слепотой до HF-добавки")
    for thr in (1.12, 1.15):
        print(f"   хвост HF<={thr}: {sum(1 for b, c in tail if hf(b, c) <= thr)}")
if len(rows) == 10000 and not tail_reached:
    print(f"NB: за skip-лимитом ещё хвост (долги <= ${rows[-1][0]:,.0f}) — виден ТОЛЬКО HF-добавке")

# --- 2. Что даёт HF-дополнение прямо сейчас (прод-функции, тот же путь что refresh) ---
bounded = _fetch_market_bounded(KEY, MORPHO_API_URL)
extra = _fetch_market_hf_filtered(KEY, MORPHO_API_URL)
seen = {((it.get("user") or {}).get("address") or "").lower() for it in bounded}
added = [it for it in extra if ((it.get("user") or {}).get("address") or "").lower() not in seen]
resc = [(b, hf(b, c)) for b, c in map(usd, added) if hf(b, c) <= CEILING and b >= 1.0]
print(f"HF-добавка (server-HF<={MEGA_HF_FILTER}): fetched={len(extra)} added={len(added)} "
      f"из них КАНДИДАТЫ сейчас (client-HF<={CEILING}, debt>=$1): {len(resc)}")
for b, h in sorted(resc, key=lambda x: x[1])[:8]:
    print(f"   debt=${b:,.0f} HF={h:.4f}")
