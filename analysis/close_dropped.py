"""Read-only: close the non-Chainlink (no-BASE_FEED) markets among covered.
Self-derives them (BASE_FEED_1 check over covered), then per market: pair (idToMarketParams tokens
-> assetByAddress symbols), liquidation flow over DAYS, oracle type sniff (Pyth getter) -> decide:
Pyth reader branch (alive) or drop (dead). Read-only: eth_call/Morpho API, no key, no tx.
    python -m analysis.close_dropped
"""
from __future__ import annotations
import sys

DAYS = 30
ZERO = "0x0000000000000000000000000000000000000000"


def _addr_abi(n):
    return [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "address"}]}]


def _try_addr(rpc, addr, g):
    try:
        v = getattr(rpc.contract(addr, _addr_abi(g)).functions, g)().call()
        return v if v and v != ZERO else None
    except Exception:
        return None


def main():
    import json, time, urllib.request, urllib.error
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)

    def gql(q, v):
        body = json.dumps({"query": q, "variables": v}).encode()
        req = urllib.request.Request("https://api.morpho.org/graphql", data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as x:
                return json.loads(x.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"errors": [{"http": e.code}]}

    sym_cache = {}
    def symbol(addr):
        k = (addr or "").lower()
        if k in sym_cache:
            return sym_cache[k]
        d = gql("query($a:String!,$c:Int!){assetByAddress(address:$a,chainId:$c){symbol}}",
                {"a": addr, "c": cfg.chain_id})
        s = (((d.get("data") or {}).get("assetByAddress") or {}) or {}).get("symbol") or addr[:8]
        sym_cache[k] = s
        return s

    since = int(time.time()) - DAYS * 86400
    QL = ("query($f:Int!,$s:Int!,$w:MarketTransactionFilters!){"
          "marketTransactions(first:$f,skip:$s,orderBy:Timestamp,orderDirection:Desc,where:$w){"
          "items{ timestamp market{ marketId } } } }")
    liq = {}
    skip = 0
    while skip <= 4000:
        d = gql(QL, {"f": 100, "s": skip, "w": {"type_in": ["Liquidation"], "chainId_in": [cfg.chain_id]}})
        if d.get("errors"):
            print("  (liq API err:", str(d["errors"])[:120], ")"); break
        items = (((d.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        if not items:
            break
        stop = False
        for it in items:
            if int(it["timestamp"]) < since:
                stop = True; break
            mid = ((it.get("market") or {}).get("marketId") or "").lower()
            liq[mid] = liq.get(mid, 0) + 1
        if stop:
            break
        skip += 100

    PYTH_GETTERS = ["priceId", "feedId", "id", "PYTH", "pyth"]
    markets = load_covered_markets(cfg.covered_markets_path)
    print(f"==== не-Chainlink рынки среди {len(markets)} covered ({DAYS}д потока) ====")

    dropped = []
    for m in markets:
        mid = m.market_id
        try:
            loan, coll, oracle, _irm, _lltv = morpho.functions.idToMarketParams(rpc.to_bytes32(mid)).call()
        except Exception as e:
            print(f"  {mid[:12]}… idToMarketParams err {type(e).__name__}"); continue
        if _try_addr(rpc, oracle, "BASE_FEED_1"):
            continue
        pair = f"{symbol(coll)}/{symbol(loan)}"
        n = liq.get(mid.lower(), 0)
        otype = "unknown"
        for g in PYTH_GETTERS:
            try:
                c = rpc.contract(oracle, [{"name": g, "type": "function", "stateMutability": "view",
                                           "inputs": [], "outputs": [{"name": "", "type": "bytes32"}]}])
                v = getattr(c.functions, g)().call()
                if v and v.hex().strip("0x"):
                    otype = f"Pyth-like ({g})"; break
            except Exception:
                continue
        tag = "ЖИВОЙ" if n >= 5 else ("редкий" if n >= 1 else "МЁРТВЫЙ(0)")
        print(f"\n  {pair:<16} {mid[:14]}…  ликв/{DAYS}д={n:<4} [{tag}]  oracle={oracle} тип≈{otype}")
        dropped.append((pair, mid, n, otype))

    print(f"\n==== ИТОГ: {len(dropped)} выпавших ====")
    alive = [d for d in dropped if d[2] >= 1]
    dead = [d for d in dropped if d[2] == 0]
    if alive:
        print(f"  С ПОТОКОМ (планировать ветку ридера под их оракул): {len(alive)}")
        for pair, mid, n, ot in sorted(alive, key=lambda x: -x[2]):
            print(f"    {pair:<16} ликв/{DAYS}д={n:<4} тип≈{ot}")
    if dead:
        print(f"  МЁРТВЫЕ за {DAYS}д (выкидываем): {len(dead)}")
        for pair, mid, n, ot in dead:
            print(f"    {pair:<16} {mid[:14]}…")
    print("\n  Решение: живые -> отдельная (Pyth?) ветка ридера; мёртвые -> игнор.")
    print("  Chainlink-ветка (33 рынка, OCR */USD) — первая цель шага #3 в любом случае.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
