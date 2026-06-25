"""Read-only contestation analysis — do we have a CHANCE against other liquidation bots?

For liquidations on OUR covered markets, measure how contestable the tail is. We can't measure our
reaction speed retrospectively (needs historical HF reconstruction), but we CAN measure whether the
tail is locked by one fast bot or open:

  1. Winner diversity — how many distinct liquidators win, and the top-2 share. A long tail of
     OCCASIONAL winners (1-2 each) is the smoking gun: those liquidations were NOT instantly sniped
     by the dominant bot, so a non-dominant actor (us) can land them.
  2. Cascade clustering — how often several liquidations fall in one block (Base ~2s, so same
     timestamp == same block). Even a fast competitor takes only so many per block; the rest spill.
  3. Burst monopolization — in multi-liquidation blocks, taken by ONE bot or SPREAD across several?
     Spread == room for us in cascades (where the real money is).

Focuses on the LARGE liquidations (>= size threshold) — that's where real net survives slippage and
what's worth racing for; dust contestation is irrelevant (we skip dust). Read-only; run on the VPS.

    python -m analysis.contestation [days] [min_repaid_usd]
"""
from __future__ import annotations
import json
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict

MORPHO_API = "https://api.morpho.org/graphql"
_UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}


def winner_stats(liqs: list, top_n: int = 2) -> dict:
    """liqs: liquidator address per liquidation. Diversity + concentration. `occasional_share` =
    fraction of liquidations won by actors who won <=2 total (not the dominant bot -> catchable)."""
    c = Counter((a or "").lower() for a in liqs if a)
    total = sum(c.values())
    ranked = c.most_common()
    top = sum(n for _, n in ranked[:top_n])
    occasional = sum(n for a, n in c.items() if n <= 2)
    return {"total": total, "distinct": len(c),
            "top_n_share": top / total if total else 0.0,
            "occasional_share": occasional / total if total else 0.0,
            "ranked": ranked}


def cluster_by_block(items: list) -> dict:
    """items: [(ts, liq_addr)]. Group by ts (== block on Base ~2s). Block-size distribution + the
    multi-liquidation blocks (cascades)."""
    by_ts = defaultdict(list)
    for ts, liq in items:
        by_ts[ts].append((liq or "").lower())
    size_dist = Counter(len(v) for v in by_ts.values())
    multi = {ts: v for ts, v in by_ts.items() if len(v) >= 2}
    return {"blocks": len(by_ts), "size_dist": dict(size_dist), "multi": multi,
            "max_per_block": max((len(v) for v in by_ts.values()), default=0)}


def burst_monopolization(multi: dict) -> dict:
    """multi: {ts: [liq_addr,...]} for blocks with >=2 liqs. How many bursts were taken by ONE
    liquidator (monopolized) vs >=2 distinct (spread = room)?"""
    mono = spread = liqs_in_spread = liqs_total = 0
    for _, liqs in multi.items():
        liqs_total += len(liqs)
        if len(set(liqs)) == 1:
            mono += 1
        else:
            spread += 1
            liqs_in_spread += len(liqs)
    return {"bursts": len(multi), "monopolized": mono, "spread": spread,
            "liqs_in_spread_bursts": liqs_in_spread, "liqs_in_bursts": liqs_total}


def per_market_diversity(rows: list) -> dict:
    """rows: [{pair, liq}]. Per market: distinct liquidators + dominant share (1.0 = one bot owns it)."""
    by_m = defaultdict(list)
    for r in rows:
        by_m[r["pair"]].append((r["liq"] or "").lower())
    out = {}
    for pair, liqs in by_m.items():
        c = Counter(liqs)
        out[pair] = {"n": len(liqs), "distinct": len(c),
                     "dominant_share": c.most_common(1)[0][1] / len(liqs) if liqs else 0.0}
    return out


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


def _loan_price(addr: str, chain_id: int, cache: dict) -> float:
    key = (addr or "").lower()
    if key in cache:
        return cache[key]
    r = _gql("query($a:String!,$c:Int!){ assetByAddress(address:$a,chainId:$c){ price { usd } } }",
             {"a": addr, "c": chain_id})
    usd = (((r.get("data") or {}).get("assetByAddress") or {}).get("price") or {}).get("usd")
    cache[key] = float(usd) if usd else 1.0
    return cache[key]


_Q = """query($first:Int!,$skip:Int!,$cid:[Int!]){
  marketTransactions(first:$first,skip:$skip,orderBy:Timestamp,orderDirection:Desc,where:{type_in:[Liquidation],chainId_in:$cid}){
    items{ timestamp
      market{ marketId collateralAsset{symbol} loanAsset{symbol address decimals} }
      data{ ... on MarketTransactionLiquidationData{ liquidator repaidAssets } } } } }"""


def run(cfg, days: int = 30, min_repaid_usd: float = 10_000.0) -> None:
    from strategy.scanner import load_covered_markets

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower() for m in markets}
    since = int(time.time()) - days * 86400
    price_cache: dict = {}

    rows, skip = [], 0
    while skip <= 4000:
        r = _gql(_Q, {"first": 100, "skip": skip, "cid": [cfg.chain_id]})
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
            mk = it.get("market") or {}
            if (mk.get("marketId") or "").lower() not in by_id:
                continue
            d = it.get("data") or {}
            la = mk.get("loanAsset") or {}
            dec = int(la.get("decimals") or 18)
            price = _loan_price(la.get("address", ""), cfg.chain_id, price_cache)
            repaid_usd = int(d.get("repaidAssets") or 0) / 10 ** dec * price
            rows.append({"ts": ts, "liq": d.get("liquidator"),
                         "pair": f"{(mk.get('collateralAsset') or {}).get('symbol','?')}/{la.get('symbol','?')}",
                         "repaid_usd": repaid_usd})
        if stop:
            break
        skip += 100

    big = [r for r in rows if r["repaid_usd"] >= min_repaid_usd]
    print(f"=== Контестация на наших {len(markets)} рынках за {days}д ===")
    print(f"всего ликвидаций: {len(rows)} | КРУПНЫХ (repaid>=${min_repaid_usd:,.0f}): {len(big)}")
    if not big:
        print("крупных ликвидаций за окно нет — нечего анализировать на этом пороге."); return

    ws = winner_stats([r["liq"] for r in big])
    cl = cluster_by_block([(r["ts"], r["liq"]) for r in big])
    bm = burst_monopolization(cl["multi"])
    pm = per_market_diversity(big)

    print(f"\n--- 1. РАЗНООБРАЗИЕ ПОБЕДИТЕЛЕЙ (крупные) ---")
    print(f"  различных ликвидаторов: {ws['distinct']}")
    print(f"  доля топ-2: {ws['top_n_share']*100:.0f}%  |  доля разовых (<=2 побед): {ws['occasional_share']*100:.0f}%")
    print(f"  топ-5 победителей:")
    for addr, n in ws["ranked"][:5]:
        print(f"    {addr[:14]}…  {n} побед")
    print(f"  -> разовые {ws['occasional_share']*100:.0f}% = НЕ были мгновенно снайпнуты доминирующим ботом")
    print(f"     (если заметная доля — есть щель: мы быстрее разового актора)")

    print(f"\n--- 2. КАСКАДЫ (ликвидаций в одном блоке, Base ~2с) ---")
    print(f"  блоков с ликвидациями: {cl['blocks']}  |  макс. в одном блоке: {cl['max_per_block']}")
    print(f"  распределение размера блока: {dict(sorted(cl['size_dist'].items()))}")
    multi_liqs = sum(k*v for k, v in cl['size_dist'].items() if k >= 2)
    print(f"  ликвидаций в мульти-блоках (>=2): {multi_liqs} ({multi_liqs/len(big)*100:.0f}% крупных)")
    print(f"  -> в каскаде даже быстрый бот берёт ограниченно; остальное — другим (нам с параллелью)")

    print(f"\n--- 3. МОНОПОЛИЗАЦИЯ ВСПЛЕСКОВ (мульти-блоки) ---")
    print(f"  мульти-блоков: {bm['bursts']}  |  монополизировано одним: {bm['monopolized']}  |  разделено (>=2): {bm['spread']}")
    if bm['bursts']:
        print(f"  -> {bm['spread']/bm['bursts']*100:.0f}% всплесков разделены между ботами = в каскадах никто не успевал всё")

    print(f"\n--- 4. ПО РЫНКАМ (dominant_share=1.0 -> один бот владеет) ---")
    for pair, v in sorted(pm.items(), key=lambda kv: -kv[1]["n"])[:12]:
        lock = "ЗАПЕРТ" if v["dominant_share"] >= 0.8 else ("плотно" if v["dominant_share"] >= 0.5 else "открыт")
        print(f"    {pair:<20} ликв {v['n']:>3}  разных {v['distinct']:>2}  доминант {v['dominant_share']*100:>3.0f}%  [{lock}]")

    print(f"\nИтог: высокая доля разовых + разделённые всплески + 'открытые' рынки -> ЕСТЬ щель.")
    print(f"      1-2 бота с долей ~100% + монополизация всплесков + всё 'заперто' -> тяжело.")
    print(f"      Реакцию-в-блоках и наш win-rate измерит только первая живая ликвидация.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from config import Config
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    minr = float(sys.argv[2]) if len(sys.argv) > 2 else 10_000.0
    run(Config.from_env(), days, minr)
