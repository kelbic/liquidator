"""MONAD ЗАМЕР stage 2 — ЗНАМЕНАТЕЛЬ (G1) + концентрация (G2). Read-only blue-api, chainId 143.
Точная форма запросов = боевой competition_report (type_in:[Liquidation],
MarketTransactionLiquidationData). Выход: ликвидации/60д по рынкам -> $/мес объём + бонус-пул
(repaid×(LIF-1), LIF из lltv) + уникальные ликвидаторы и доля топ-1/топ-3 (контестабельность)."""
import json, urllib.request, time

API = "https://api.morpho.org/graphql"
CHAIN = 143
DAYS = 60

def gql(q, v, timeout=30):
    body = json.dumps({"query": q, "variables": v}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def lif_bonus(lltv):
    if not (0 < lltv < 1): return 0.0
    return min(1.15, 1.0/(1.0-0.3*(1.0-lltv))) - 1.0

# --- lltv по рынкам (для бонуса) ---
MK_Q = """query($c:[Int!],$f:Int!,$s:Int!){markets(first:$f,skip:$s,where:{chainId_in:$c}){
  items{ marketId lltv collateralAsset{symbol} loanAsset{symbol} }}}"""
lltv_by, sym_by = {}, {}
skip = 0
while skip <= 500:
    r = gql(MK_Q, {"c":[CHAIN],"f":100,"s":skip})
    b = (((r.get("data") or {}).get("markets") or {}).get("items")) or []
    for it in b:
        mid = (it.get("marketId") or "").lower()
        lv = float(it.get("lltv") or 0); lv = lv/1e18 if lv>100 else lv
        lltv_by[mid] = lv
        sym_by[mid] = f"{(it.get('collateralAsset') or {}).get('symbol','?')}/{(it.get('loanAsset') or {}).get('symbol','?')}"
    if len(b) < 100: break
    skip += 100
print(f"рынков в карте: {len(lltv_by)}")

# --- цена loan-актива (стейблы=1, иначе blue-api assetByAddress) ---
ASSET_Q = "query($a:String!,$c:Int!){assetByAddress(address:$a,chainId:$c){price{usd}}}"
price_cache = {}
def loan_price(addr, sym):
    s = (sym or "").upper()
    if any(k in s for k in ("USD","DAI")): return 1.0
    if not addr: return None
    if addr in price_cache: return price_cache[addr]
    try:
        r = gql(ASSET_Q, {"a": addr, "c": CHAIN})
        u = (((r.get("data") or {}).get("assetByAddress") or {}).get("price") or {}).get("usd")
        price_cache[addr] = float(u) if u else None
    except Exception:
        price_cache[addr] = None
    return price_cache[addr]

# --- ликвидации 60д (точная боевая форма) ---
LIQ_Q = """query($first:Int!,$skip:Int!,$cid:[Int!]){
  marketTransactions(first:$first,skip:$skip,orderBy:Timestamp,orderDirection:Desc,where:{type_in:[Liquidation],chainId_in:$cid}){
    items{ timestamp market{ marketId loanAsset{symbol address decimals} collateralAsset{symbol} }
      data{ ... on MarketTransactionLiquidationData{ liquidator repaidAssets seizedAssets } } } } }"""

cut = int(time.time()) - DAYS*86400
by_mkt = {}   # mid -> {"n":.., "vol":.., "bonus":..}
liquidators = {}  # addr -> {"n":.., "vol":..}
n_total, skip = 0, 0
stop = False
while skip <= 5000 and not stop:
    r = gql(LIQ_Q, {"first":100,"skip":skip,"cid":[CHAIN]})
    if r.get("errors"):
        print("LIQ errors:", json.dumps(r["errors"])[:300]); break
    items = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
    if not items: break
    for it in items:
        ts = int(it.get("timestamp") or 0)
        if ts < cut: stop = True; break
        mk = it.get("market") or {}; d = it.get("data") or {}
        mid = (mk.get("marketId") or "").lower()
        la = mk.get("loanAsset") or {}
        dec = int(la.get("decimals") or 18)
        px = loan_price(la.get("address"), la.get("symbol"))
        if px is None: continue
        repaid = float(d.get("repaidAssets") or 0)/10**dec * px
        n_total += 1
        m = by_mkt.setdefault(mid, {"n":0,"vol":0.0,"bonus":0.0})
        m["n"]+=1; m["vol"]+=repaid; m["bonus"]+=repaid*lif_bonus(lltv_by.get(mid,0))
        lq = (d.get("liquidator") or "?").lower()
        L = liquidators.setdefault(lq, {"n":0,"vol":0.0}); L["n"]+=1; L["vol"]+=repaid
    if stop or len(items)<100: break
    skip += 100

print(f"\n=== ЛИКВИДАЦИИ MONAD за {DAYS}д: {n_total} ===")
if n_total:
    tot_vol = sum(m["vol"] for m in by_mkt.values()); tot_bonus = sum(m["bonus"] for m in by_mkt.values())
    print(f"объём repaid: ${tot_vol:,.0f} ({DAYS}д) -> ${tot_vol/DAYS*30:,.0f}/мес")
    print(f"БОНУС-ПУЛ (G1): ${tot_bonus:,.0f} ({DAYS}д) -> ${tot_bonus/DAYS*30:,.0f}/мес  <<< ГЛАВНЫЙ ГЕЙТ")
    print(f"\n  {'рынок':<20}{'n':>4}{'vol$':>13}{'bonus$/мес':>13}")
    for mid,m in sorted(by_mkt.items(), key=lambda x:-x[1]["bonus"])[:12]:
        print(f"  {sym_by.get(mid,mid[:10]):<20}{m['n']:>4}${m['vol']:>11,.0f}${m['bonus']/DAYS*30:>11,.0f}")
    print(f"\n=== КОНЦЕНТРАЦИЯ (G2): {len(liquidators)} уникальных ликвидаторов ===")
    top = sorted(liquidators.values(), key=lambda x:-x["vol"])
    if top:
        t1 = top[0]["vol"]/tot_vol*100 if tot_vol else 0
        t3 = sum(x["vol"] for x in top[:3])/tot_vol*100 if tot_vol else 0
        print(f"  топ-1 доля объёма: {t1:.0f}% | топ-3: {t3:.0f}%  (≤60% = поле не занято одним — G2 gate)")
else:
    print("  НОЛЬ ликвидаций за 60д через API -> либо поле мёртвое (знаменатель≈0), либо API не индексит Monad-ликвидации (сверить getLogs)")
