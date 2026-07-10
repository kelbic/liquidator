"""MONAD ЗАМЕР stage 1 — фундамент: отдаёт ли Morpho blue-api данные Monad (chainId 143)?
Read-only, stdlib, БЕЗ ключей, БЕЗ ончейн. Standalone (не импортит боевой бот). Запуск на VPS
(Charlotte — egress к api.morpho.org есть). Отвечает: (1) есть ли рынки Monad в API + карта
(borrow/oracle/lltv); (2) отдаёт ли API ликвидации Monad (marketTransactions chainId 143) —
семя знаменателя G1. Если (2) пусто/400 — знаменатель мерить getLogs с Monad-RPC (не из API)."""
import json, urllib.request, sys

API = "https://api.morpho.org/graphql"
CHAIN = 143   # Monad (из MONAD_ANALYSIS §1.4, §2 — сверяем ответом)

def gql(query, variables, timeout=30):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# --- (1) карта рынков Monad ---
MARKETS_Q = """query($c:[Int!],$f:Int!,$s:Int!){
  markets(first:$f, skip:$s, orderBy:BorrowAssetsUsd, orderDirection:Desc, where:{chainId_in:$c}){
    items{ marketId oracleAddress lltv
      loanAsset{symbol decimals address} collateralAsset{symbol decimals address}
      state{ borrowAssetsUsd supplyAssetsUsd } } } }"""

print(f"=== (1) РЫНКИ MONAD (chainId {CHAIN}) через blue-api ===")
try:
    items, skip = [], 0
    while skip <= 500:
        r = gql(MARKETS_Q, {"c": [CHAIN], "f": 100, "s": skip})
        if r.get("errors"):
            print("  API errors:", json.dumps(r["errors"])[:300]); break
        batch = (((r.get("data") or {}).get("markets") or {}).get("items")) or []
        items += batch
        if len(batch) < 100: break
        skip += 100
    print(f"  рынков Monad в API: {len(items)}")
    if items:
        tot = sum(float((it.get("state") or {}).get("borrowAssetsUsd") or 0) for it in items)
        print(f"  суммарный borrow: ${tot:,.0f}")
        print(f"  {'collat/loan':<18}{'borrow$':>15}{'lltv':>8}  oracle")
        for it in items[:15]:
            st = it.get("state") or {}; col = it.get("collateralAsset") or {}; ln = it.get("loanAsset") or {}
            b = float(st.get("borrowAssetsUsd") or 0)
            if b < 1000: continue
            lltv = float(it.get("lltv") or 0); lltv = lltv/1e18 if lltv > 100 else lltv
            print(f"  {(col.get('symbol','?')+'/'+ln.get('symbol','?')):<18}${b:>13,.0f}{lltv:>8.3f}  {it.get('oracleAddress','?')}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {str(e)[:150]}")

# --- (2) ликвидации Monad через API (семя знаменателя G1) ---
LIQ_Q = """query($first:Int!,$skip:Int!,$cid:[Int!]){
  marketTransactions(first:$first, skip:$skip, orderBy:Timestamp, orderDirection:Desc,
    where:{chainId_in:$cid, type_in:[MarketLiquidation]}){
    items{ timestamp type
      data{ ...on MarketLiquidationTransactionData{ repaidAssets seizedAssets liquidator } }
      market{ marketId loanAsset{symbol decimals address} collateralAsset{symbol} } } } }"""

print(f"\n=== (2) ЛИКВИДАЦИИ MONAD через API (семя G1-знаменателя) ===")
try:
    r = gql(LIQ_Q, {"first": 100, "skip": 0, "cid": [CHAIN]})
    if r.get("errors"):
        print("  API errors:", json.dumps(r["errors"])[:400])
        print("  -> вероятно enum/поле отличается; знаменатель мерить getLogs с Monad-RPC (не API)")
    else:
        items = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        print(f"  ликвидаций в первой странице (100): {len(items)}")
        if items:
            print(f"  самая свежая: ts={items[0].get('timestamp')}  самая старая в стр.: ts={items[-1].get('timestamp')}")
            liqs = {it["data"].get("liquidator","?") for it in items if it.get("data")}
            print(f"  уникальных ликвидаторов в 100: {len(liqs)} -> концентрация (G2) мерибельна через API")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {str(e)[:150]}")

print("\n=== ВЕРДИКТ stage1 ===")
print("  Если (1) есть рынки И (2) отдаёт ликвидации -> весь §6 мерибелен через blue-api дёшево.")
print("  Если (2) 400/пусто -> нужен Monad-RPC endpoint (getLogs) для знаменателя.")
