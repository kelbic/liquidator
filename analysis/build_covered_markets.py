"""Build covered_markets.json from the Morpho API (Base) — the monitor's market set,
WITHOUT a Dune dependency.

Selection: markets with real borrowing (liquidations are possible and worth the gas) AND
a liquidation bonus worth more than slippage, bounded count to keep the scan light and
within positions_at_risk's page. Competition is NOT scored here — the monitor measures it
live (a position that vanishes before the next scan = contested; one that lingers = our
tail edge), which later informs a stricter Dune-based selection.

Run on a host that can reach api.morpho.org (the VPS); stdlib only, plain `python3`.
Usage:
    python3 analysis/build_covered_markets.py [OUT_PATH] [MIN_BORROW_USD] [MAX_MARKETS]
Defaults: OUT_PATH=covered_markets.json  MIN_BORROW_USD=50000  MAX_MARKETS=40
"""
import json
import sys
import urllib.request

MORPHO_API_URL = "https://api.morpho.org/graphql"
BASE_CHAIN_ID = 8453
DEFAULT_SLIPPAGE = 0.01   # conservative placeholder; refined per-market by monitor/Dune later
MIN_BONUS = 0.01          # drop markets whose LIF bonus <= this (uneconomical: bonus < slippage)
MAX_BORROW_USD = 30_000_000   # exclude mega-cap markets: contested (we won't win) AND huge position counts

_MARKETS_QUERY = """
query Markets($chains: [Int!], $first: Int!, $skip: Int!) {
  markets(first: $first, skip: $skip, orderBy: BorrowAssetsUsd, orderDirection: Desc,
          where: { chainId_in: $chains }) {
    items {
      marketId
      oracleAddress
      lltv
      loanAsset { symbol decimals address }
      collateralAsset { symbol decimals address }
      state { borrowAssetsUsd supplyAssetsUsd }
    }
  }
}
"""


def _query(chain_id, skip, first, api_url, timeout):
    body = json.dumps({"query": _MARKETS_QUERY,
                       "variables": {"chains": [chain_id], "first": first, "skip": skip}}).encode()
    req = urllib.request.Request(api_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise RuntimeError(f"Morpho API errors: {payload['errors']}")
    return (((payload.get("data") or {}).get("markets") or {}).get("items")) or []


def fetch_markets(chain_id=BASE_CHAIN_ID, api_url=MORPHO_API_URL, page=100, max_pages=5, timeout=30):
    """Base markets ordered by borrow desc, paginated; top markets land in the first pages."""
    items, skip = [], 0
    for _ in range(max_pages):
        batch = _query(chain_id, skip, page, api_url, timeout)
        items.extend(batch)
        if len(batch) < page:
            break
        skip += page
    return items


def _lif_bonus(lltv: float) -> float:
    """Morpho LIF - 1 (the liquidation bonus), exact from lltv."""
    if not (0.0 < lltv < 1.0):
        return 0.0
    return min(1.15, 1.0 / (1.0 - 0.3 * (1.0 - lltv))) - 1.0


def _lltv_fraction(raw) -> float:
    """API lltv may be WAD (e.g. 860000000000000000) or already a fraction. Normalize."""
    v = float(raw or 0)
    return v / 1e18 if v > 100 else v


def select_markets(items, min_borrow_usd=50_000.0, max_borrow_usd=MAX_BORROW_USD, max_markets=40,
                   default_slippage=DEFAULT_SLIPPAGE, min_bonus=MIN_BONUS, exclude_oracles=None):
    """Pure: filter (real borrow, valid collateral, bonus>slippage) + map -> records.
    Carries a private _borrow_usd for inspection/sort; stripped before JSON."""
    rows = []
    for it in items:
        st = it.get("state") or {}
        borrow = float(st.get("borrowAssetsUsd") or 0)
        if borrow < min_borrow_usd or borrow > max_borrow_usd:
            continue
        key = it.get("marketId")
        col = it.get("collateralAsset") or {}
        loan = it.get("loanAsset") or {}
        if not key or not col.get("symbol"):   # idle/unrecognized-collateral markets -> skip
            continue
        if exclude_oracles and (it.get("oracleAddress") or "").lower() in exclude_oracles:
            continue                            # SVR/OEV-recapture oracle -> bonus leaks to protocol
        lltv = _lltv_fraction(it.get("lltv"))
        bonus = _lif_bonus(lltv)
        if bonus <= min_bonus:                  # uneconomical (bonus <= slippage) -> skip
            continue
        rows.append({
            "market_id": key,
            "collateral_token": col.get("symbol") or "",
            "loan_token": loan.get("symbol") or "",
            "lltv": round(lltv, 6),
            "bonus": round(bonus, 6),
            "expected_slippage": default_slippage,
            "_borrow_usd": round(borrow, 0),
        })
    rows.sort(key=lambda m: m["_borrow_usd"], reverse=True)
    return rows[:max_markets]


def to_json_records(selected) -> list:
    """Strip private fields -> clean Market(**rec)-compatible records."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in selected]


def main(argv):
    out_path = argv[1] if len(argv) > 1 else "covered_markets.json"
    min_borrow = float(argv[2]) if len(argv) > 2 else 50_000.0
    max_borrow = float(argv[3]) if len(argv) > 3 else MAX_BORROW_USD
    max_markets = int(argv[4]) if len(argv) > 4 else 40

    items = fetch_markets()
    selected = select_markets(items, min_borrow_usd=min_borrow, max_borrow_usd=max_borrow, max_markets=max_markets)
    records = to_json_records(selected)

    print(f"Base markets fetched: {len(items)}; selected (${min_borrow:,.0f}<=borrow<=${max_borrow:,.0f}, bonus>{MIN_BONUS*100:.0f}%, top {max_markets}): {len(records)}")
    print(f"{'collateral/loan':<24}{'lltv':>7}{'bonus%':>8}{'borrow_usd':>16}")
    for m in selected:
        print(f"{(m['collateral_token']+'/'+m['loan_token']):<24}{m['lltv']:>7.3f}{m['bonus']*100:>8.2f}{m['_borrow_usd']:>16,.0f}")

    with open(out_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\nwrote {len(records)} markets -> {out_path}")


if __name__ == "__main__":
    main(sys.argv)
