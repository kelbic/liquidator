"""Read-only competition report — the feedback loop's "what are we losing" half.

Two passes over liquidations on OUR covered markets:
  PASS 1 (fast, all): rank by size using a NOMINAL net (constant slippage) — ONLY a ranking heuristic.
  PASS 2 (real, top-N): for the biggest N, pull a LIVE KyberSwap quote on the actual seized size and
  compute TRUE net = amountOut - repaid - cost. This is the honest "$ on table": it sees real DEX
  price impact (a $100k dump of a thin token loses far more than a flat 1%), unlike the constant.

Caveats on PASS 2: quotes reflect CURRENT liquidity (proxy for "can we profit on these sizes going
forward", not an exact replay of a past block), and a single quote ignores that a real cascade has
many liquidators dumping at once -> so real net here is an UPPER bound on spike profitability.

Pure Morpho/Kyber reads; run on the VPS.   python -m analysis.competition_report [days] [topN]
"""
from __future__ import annotations
import json
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from urllib.parse import urlencode

MORPHO_API = "https://api.morpho.org/graphql"
KYBER = "https://aggregator-api.kyberswap.com"
_UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}


# ----- pure logic (unit-testable, no network) -------------------------------

def hypo_net(repaid_usd: float, bonus: float, slippage: float, cost_usd: float) -> float:
    """NOMINAL net at a constant slippage — ranking heuristic only (NOT the truth on big sizes)."""
    from strategy.pnl import net_profit, PnlInputs
    return net_profit(PnlInputs(debt_usd=repaid_usd, bonus=bonus, slippage=slippage,
                                gas_usd=0.0, tip_usd=cost_usd))


def real_net_usd(amount_out: int, repaid_assets: int, loan_dec: int,
                 loan_price: float, cost_usd: float) -> float:
    """TRUE net: sell the seized collateral for `amount_out` loan tokens (real quote), repay
    `repaid_assets`, minus our send cost. All loan-token amounts in smallest units."""
    return (amount_out - repaid_assets) / 10 ** loan_dec * loan_price - cost_usd


def classify(liquidator: str, our_addrs: set) -> str:
    return "us" if (liquidator or "").lower() in our_addrs else "other"


# ----- network (VPS only) ---------------------------------------------------

def _gql(query: str, variables: dict, timeout: int = 30, retries: int = 4) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(MORPHO_API, data=body, headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if d.get("errors") and "timed out" in json.dumps(d["errors"]).lower():
                last = d; time.sleep(1.5 * (attempt + 1)); continue
            return d
        except urllib.error.HTTPError as e:
            last = {"errors": [{"m": e.read().decode()[:300]}]}
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1)); continue
            return last
        except (urllib.error.URLError, TimeoutError) as e:
            last = {"errors": [{"m": str(e)[:200]}]}; time.sleep(1.5 * (attempt + 1)); continue
    return last or {"errors": [{"m": "exhausted"}]}


def kyber_quote(token_in: str, token_out: str, amount_in: int,
                chain: str = "base", timeout: int = 15) -> int | None:
    """LIVE KyberSwap output for amount_in (smallest units) -> amount_out (int) or None on
    no-route/error. /routes only (no build). Browser UA (Cloudflare 1010 on default UA)."""
    q = urlencode({"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(int(amount_in))})
    url = f"{KYBER}/{chain}/api/v1/routes?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    rs = (d.get("data") or {}).get("routeSummary") or {}
    out = rs.get("amountOut")
    return int(out) if out else None


def _loan_price(addr: str, chain_id: int, cache: dict) -> float:
    key = addr.lower()
    if key in cache:
        return cache[key]
    r = _gql("query($a:String!,$c:Int!){ assetByAddress(address:$a,chainId:$c){ price { usd } } }",
             {"a": addr, "c": chain_id})
    usd = (((r.get("data") or {}).get("assetByAddress") or {}).get("price") or {}).get("usd")
    cache[key] = float(usd) if usd else 1.0
    return cache[key]


_LIQ_QUERY = """query($first:Int!,$skip:Int!,$where:MarketTransactionFilters!){
  marketTransactions(first:$first,skip:$skip,orderBy:Timestamp,orderDirection:Desc,where:$where){
    items{ timestamp
      market{ marketId
        collateralAsset{ symbol address decimals }
        loanAsset{ symbol address decimals } }
      data{ ... on MarketTransactionLiquidationData{ liquidator repaidAssets seizedAssets } } } } }"""


def run(cfg, days: int = 30, top_n: int = 40) -> None:
    from strategy.scanner import load_covered_markets

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower(): m for m in markets}
    our_addrs = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    cost_usd = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
    floor = cfg.min_profit_usd
    since = int(time.time()) - days * 86400
    price_cache: dict = {}

    where = {"type_in": ["Liquidation"], "chainId_in": [cfg.chain_id]}
    rows, scanned, skip = [], 0, 0
    while skip <= 4000:
        r = _gql(_LIQ_QUERY, {"first": 100, "skip": skip, "where": where})
        if r.get("errors"):
            print("API errors:", r["errors"]); return
        batch = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        if not batch:
            break
        stop = False
        for it in batch:
            ts = int(it["timestamp"])
            if ts < since:
                stop = True; break
            scanned += 1
            mk = it.get("market") or {}
            m = by_id.get((mk.get("marketId") or "").lower())
            if not m:
                continue
            d = it.get("data") or {}
            ca = mk.get("collateralAsset") or {}; la = mk.get("loanAsset") or {}
            ldec = int(la.get("decimals") or 18)
            lprice = _loan_price(la.get("address", ""), cfg.chain_id, price_cache)
            repaid_assets = int(d.get("repaidAssets") or 0)
            seized = int(d.get("seizedAssets") or 0)
            repaid_usd = repaid_assets / 10 ** ldec * lprice
            rows.append({
                "pair": f"{ca.get('symbol','?')}/{la.get('symbol','?')}",
                "repaid_usd": repaid_usd, "nom_net": hypo_net(repaid_usd, m.bonus, m.expected_slippage, cost_usd),
                "who": classify(d.get("liquidator"), our_addrs), "ts": ts,
                "coll_addr": ca.get("address", ""), "loan_addr": la.get("address", ""),
                "loan_dec": ldec, "loan_price": lprice, "seized": seized, "repaid_assets": repaid_assets,
            })
        if stop:
            break
        skip += 100

    not_us = [r for r in rows if r["who"] != "us"]
    nom_worth = [r for r in not_us if r["nom_net"] >= floor]
    print(f"=== Конкуренция на наших {len(markets)} рынках за {days}д (cost/liq=${cost_usd:.2f}, флор=${floor:.0f}) ===")
    print(f"просмотрено Base-ликвидаций: {scanned}; на наших рынках: {len(rows)}  (~{len(rows)/max(1,days):.1f}/день)")
    print(f"выиграли МЫ: {sum(1 for r in rows if r['who']=='us')} | НЕ наши: {len(not_us)}")
    print(f"(номинально по константе ~1% 'стоящих' было бы {len(nom_worth)} — ЭТО завышено, проверяем реально ниже)")
    if not not_us:
        return

    top = sorted(not_us, key=lambda x: -x["repaid_usd"])[:top_n]
    print(f"\n=== РЕАЛЬНАЯ переоценка топ-{len(top)} по размеру (живые котировки KyberSwap на seized) ===")
    print(f"  {'pair':<18} {'repaid$':>11} {'seized':>18} {'real_net$':>11}  note")
    real_rows = []
    for r in top:
        out = kyber_quote(r["coll_addr"], r["loan_addr"], r["seized"]) if r["seized"] > 0 else None
        if out is None:
            print(f"  {r['pair']:<18} {r['repaid_usd']:>11.0f} {r['seized']:>18} {'—':>11}  НЕТ МАРШРУТА (не выйти)")
            real_rows.append({**r, "real_net": None}); time.sleep(0.12); continue
        rn = real_net_usd(out, r["repaid_assets"], r["loan_dec"], r["loan_price"], cost_usd)
        tag = "СТОИТ" if rn >= floor else ("пыль" if rn > 0 else "УБЫТОК")
        print(f"  {r['pair']:<18} {r['repaid_usd']:>11.0f} {r['seized']:>18} {rn:>11.2f}  {tag}")
        real_rows.append({**r, "real_net": rn}); time.sleep(0.12)

    priced = [r for r in real_rows if r["real_net"] is not None]
    worth = [r for r in priced if r["real_net"] >= floor]
    noroute = [r for r in real_rows if r["real_net"] is None]
    loss = [r for r in priced if r["real_net"] < 0]
    real_table = sum(r["real_net"] for r in worth)
    print(f"\n=== ИТОГ (реально, топ-{len(top)}) ===")
    print(f"  стоящих (real_net>=${floor:.0f}): {len(worth)}  | пыль/0: {len(priced)-len(worth)-len(loss)}  | "
          f"убыточных при реале: {len(loss)}  | нет маршрута: {len(noroute)}")
    print(f"  РЕАЛЬНО НА СТОЛЕ (топ-{len(top)}): ${real_table:.2f}")
    if worth:
        by_m = defaultdict(lambda: [0, 0.0])
        for r in worth:
            by_m[r["pair"]][0] += 1; by_m[r["pair"]][1] += r["real_net"]
        print("  стоящие по рынкам:", {k: f"{v[0]}x ${v[1]:.0f}" for k, v in sorted(by_m.items(), key=lambda kv:-kv[1][1])})
    print("\nЧитать так: 'РЕАЛЬНО на столе' — настоящие деньги (живой слиппедж на фактическом размере).")
    print("Если стоящих почти нет / убыток -> слиппедж-стена съедает всплески, гонка не окупается.")
    print("Если стоящих много $$ -> всплески = реальные деньги -> Flashblock-детекция/step 6 оправданы.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from config import Config
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    topn = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    run(Config.from_env(), days, topn)
