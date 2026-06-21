"""Find Morpho Blue positions at risk (HF <= ceiling) on Base.

Phase 1 (monitor): enumeration via the Morpho GraphQL API — read-only, stdlib HTTP,
no on-chain calls, no extra deps. HF here is the USD approximation
    HF = collateralUsd * lltv / borrowAssetsUsd
enough to FLAG candidates; the precise oracle HF + revert/profit gate lives in
chain/simulate (next step). For execute later this scan moves on-chain (lower
latency, no API rate-limit dependency — the API itself warns against hard deps on it).

lltv is taken from the Market objects (immutable, from the Dune scan), not from the
API, so the position query only needs `marketId` to map a position back to its lltv.
"""
from __future__ import annotations
import json
import urllib.request
from dataclasses import dataclass

from strategy.scanner import Market

# Morpho Blue singleton — same deterministic CREATE2 address on every chain incl. Base.
# Verify on BaseScan before execute. Used by the on-chain leg (simulate, Phase 2).
MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

MORPHO_API_URL = "https://api.morpho.org/graphql"

# marketUniqueKey is keccak(loanToken,collateralToken,oracle,irm,lltv); token/oracle
# addresses differ per chain, so Base keys can't collide with other chains -> no chainId
# filter needed. `market { marketId }` is the position's market id (== the key we filter
# by). TODO: paginate via skip if a market ever exceeds `first`.
_POSITIONS_QUERY = """
query Positions($keys: [String!]) {
  marketPositions(first: 1000, where: { marketUniqueKey_in: $keys }) {
    items {
      user { address }
      market { marketId }
      state { collateral collateralUsd borrowAssets borrowAssetsUsd }
    }
  }
}
"""


@dataclass
class Position:
    market_id: str
    borrower: str
    health_factor: float
    debt_assets: int
    collateral_assets: int
    debt_usd: float = 0.0
    collateral_usd: float = 0.0


def _num(x, default=0.0) -> float:
    return float(x) if x not in (None, "") else float(default)


def _int(x, default=0) -> int:
    return int(x) if x not in (None, "") else int(default)


def health_factor(collateral_usd: float, borrow_usd: float, lltv: float) -> float:
    """USD-approx HF. >1 healthy, <=1 liquidatable. No debt -> +inf."""
    if borrow_usd <= 0:
        return float("inf")
    return collateral_usd * lltv / borrow_usd


def parse_positions(payload: dict, lltv_by_key: dict, hf_ceiling: float = 1.0) -> list[Position]:
    """marketPositions payload -> at-risk Positions (HF <= ceiling), most-at-risk first.
    lltv_by_key maps marketId -> lltv (fraction). Pure: unit-tested on synthetic data."""
    if payload.get("errors"):
        raise RuntimeError(f"Morpho API errors: {payload['errors']}")
    items = (((payload.get("data") or {}).get("marketPositions") or {}).get("items")) or []
    out: list[Position] = []
    for it in items:
        st = it.get("state") or {}
        market_id = ((it.get("market") or {}).get("marketId")) or ""
        lltv = lltv_by_key.get(market_id, 0.0)
        if lltv <= 0:
            continue  # lltv не передан для этого рынка -> не оцениваем (безопасно)
        borrow_usd = _num(st.get("borrowAssetsUsd"))
        if borrow_usd <= 0:
            continue  # нет долга -> не ликвидируемо
        hf = health_factor(_num(st.get("collateralUsd")), borrow_usd, lltv)
        if hf <= hf_ceiling:
            out.append(Position(
                market_id=market_id,
                borrower=((it.get("user") or {}).get("address") or ""),
                health_factor=hf,
                debt_assets=_int(st.get("borrowAssets")),
                collateral_assets=_int(st.get("collateral")),
                debt_usd=borrow_usd,
                collateral_usd=_num(st.get("collateralUsd")),
            ))
    out.sort(key=lambda p: p.health_factor)
    return out


def _query_market_positions(keys: list[str], api_url: str, timeout: int = 15) -> dict:
    """POST the GraphQL query. I/O — dry-run on the VPS (sandbox can't reach the API)."""
    body = json.dumps({"query": _POSITIONS_QUERY, "variables": {"keys": keys}}).encode()
    req = urllib.request.Request(api_url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def positions_at_risk(markets: list[Market], hf_ceiling: float = 1.0,
                      api_url: str = MORPHO_API_URL) -> list[Position]:
    """At-risk positions across covered markets, via the Morpho API."""
    keys = [m.market_id for m in markets if m.market_id]
    if not keys:
        return []
    lltv_by_key = {m.market_id: m.lltv for m in markets if m.market_id}
    payload = _query_market_positions(keys, api_url)
    return parse_positions(payload, lltv_by_key, hf_ceiling)
