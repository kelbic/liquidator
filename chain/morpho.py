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
import urllib.error
import time
from dataclasses import dataclass

from strategy.scanner import Market

# Morpho Blue singleton — same deterministic CREATE2 address on every chain incl. Base.
# Verify on BaseScan before execute. Used by the on-chain leg (simulate, Phase 2).
MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

MORPHO_API_URL = "https://api.morpho.org/graphql"

# Floor: позиции с долгом в USD ниже этого — пыль: невыгодны всем (бонус < газ+флор) и
# Morpho ревертит 0x81ceff30 на ~нулевом долге. Режем на стадии кандидата, чтобы они не
# засоряли hot set / simulations. (Бэклог STATE; в Config не тянем — хватает дефолта модуля.)
MIN_DEBT_USD = 1.0

# marketUniqueKey is keccak(loanToken,collateralToken,oracle,irm,lltv); token/oracle
# addresses differ per chain, so Base keys can't collide with other chains -> no chainId
# filter needed. `market { marketId }` is the position's market id (== the key we filter
# by). TODO: paginate via skip if a market ever exceeds `first`.
_POSITIONS_QUERY = """
query Positions($keys: [String!], $first: Int!, $skip: Int!) {
  marketPositions(first: $first, skip: $skip, orderBy: BorrowShares, orderDirection: Desc,
                  where: { marketUniqueKey_in: $keys }) {
    items {
      user { address }
      market { marketId }
      state { collateral collateralUsd borrowAssets borrowAssetsUsd borrowShares }
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


def parse_positions(payload: dict, lltv_by_key: dict, hf_ceiling: float = 1.0, min_debt_usd: float = MIN_DEBT_USD) -> list[Position]:
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
        if borrow_usd < min_debt_usd:
            continue  # пыль/нулевой долг (< MIN_DEBT_USD): невыгодно + ревертит 0x81ceff30; не тащим
        collateral_usd = _num(st.get("collateralUsd"))
        if collateral_usd < borrow_usd:
            continue  # залог < долга -> безнадёжный долг (изъятие не покрывает погашение); не наш
        hf = health_factor(collateral_usd, borrow_usd, lltv)
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


_RETRYABLE_HTTP = (429, 500, 502, 503, 504)


def _query_page(keys: list[str], skip: int, first: int, api_url: str, timeout: int,
                retries: int = 3) -> list:
    """One page of marketPositions (ordered by borrowShares desc). I/O with retry-on-transient
    (timeout / URLError / 429 / 5xx) so a single API hiccup doesn't drop a whole scan cycle.
    Non-transient (HTTP 4xx, GraphQL errors) propagate immediately."""
    body = json.dumps({"query": _POSITIONS_QUERY,
                       "variables": {"keys": keys, "first": first, "skip": skip}}).encode()
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(api_url, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
            if payload.get("errors"):
                raise RuntimeError(f"Morpho API errors: {payload['errors']}")
            return (((payload.get("data") or {}).get("marketPositions") or {}).get("items")) or []
        except urllib.error.HTTPError as e:
            if e.code not in _RETRYABLE_HTTP or attempt == retries - 1:
                raise
            last_exc = e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            last_exc = e
        time.sleep(0.5 * (2 ** attempt))
    raise last_exc


def _fetch_borrower_positions(keys: list[str], api_url: str, page: int = 1000,
                              max_pages: int = 20, timeout: int = 15) -> list:
    """All BORROWER positions across markets, no truncation. Because positions are ordered
    by borrowShares desc, suppliers (borrowShares==0) sort last -> stop at the first zero.
    Single first:1000 would have capped at one big market; this paginates all of them."""
    out: list = []
    skip = 0
    for _ in range(max_pages):
        batch = _query_page(keys, skip, page, api_url, timeout)
        for it in batch:
            if _int((it.get("state") or {}).get("borrowShares")) == 0:
                return out                      # reached the supplier tail -> done
            out.append(it)
        if len(batch) < page:
            break
        skip += page
    return out


def positions_at_risk(markets: list[Market], hf_ceiling: float = 1.0, min_debt_usd: float = MIN_DEBT_USD,
                      api_url: str = MORPHO_API_URL) -> list[Position]:
    """At-risk positions across covered markets, via the Morpho API."""
    keys = [m.market_id for m in markets if m.market_id]
    if not keys:
        return []
    lltv_by_key = {m.market_id: m.lltv for m in markets if m.market_id}
    items = _fetch_borrower_positions(keys, api_url)
    return parse_positions({"data": {"marketPositions": {"items": items}}}, lltv_by_key, hf_ceiling, min_debt_usd)
