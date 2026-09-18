"""Сборка конфига отслеживаемых рынков из данных перепроверки 17.09 (оракулы типизированы
на цепи). Отслеживаем рынок, если у его оракула резолвится Chainlink-агрегатор: только такой
рынок имеет РАУНД ЦЕНЫ, то есть событие B -> B+6. Рынки без Chainlink в знаменатель не входят
(RECHECK §3.4: приз там нулевой)."""
import json, sys
R = "/home/claude-agent/work/liquidator/_recheck0917"
D = json.load(open(f"{R}/markets.json"))
M, FI, AI = D["markets"], D["feedinfo"], D["agginfo"]
P8 = {"cbXRP/USDC", "cbADA/USDC", "cbDOGE/USDC", "cbBTC/USDC", "WETH/USDC", "cbLTC/USDC"}
out = {}
for mid, m in M.items():
    feeds = m.get("feeds") or {}
    legs = {}
    for name, fa in feeds.items():
        if not fa:
            continue
        agg = (FI.get(fa.lower(), {}) or {}).get("aggregator()")
        if agg:
            legs[name] = {"feed": fa.lower(), "agg": agg.lower(),
                          "tv": (AI.get(agg.lower(), {}) or {}).get("typeAndVersion")}
    pair = f"{m['collSym']}/{m['loanSym']}"
    if not legs:
        continue
    out[mid] = {"pair": pair, "premise8": pair in P8, "lltv": m["lltv"],
                "oracle": m["oracle"].lower(), "loan": m["loan"].lower(), "coll": m["coll"].lower(),
                "loan_sym": m["loanSym"], "loan_dec": m["loanDec"],
                "coll_sym": m["collSym"], "coll_dec": m["collDec"],
                "legs": legs, "n_liq_window": m.get("n", 0)}
json.dump(out, open("/home/claude-agent/work/liquidator/shadow_b6/markets_tracked.json", "w"), indent=1)
aggs = sorted({l["agg"] for v in out.values() for l in v["legs"].values()})
print("рынков с Chainlink-агрегатором:", len(out), "| из премисы 8:", sum(1 for v in out.values() if v["premise8"]))
print("уникальных агрегаторов (ключи наблюдения):", len(aggs))
for mid, v in sorted(out.items(), key=lambda kv: -kv[1]["n_liq_window"])[:14]:
    tvs = {l["tv"] for l in v["legs"].values()}
    print(f"  {v['pair']:>16} lltv {v['lltv']/1e16:>5.1f}% n_liq {v['n_liq_window']:>4} p8={v['premise8']!s:>5} legs {len(v['legs'])} {str(sorted(t for t in tvs if t))[:60]}")
missing = P8 - {v["pair"] for v in out.values()}
print("ПРОПУЩЕНЫ рынки премисы 8:", missing or "нет")
