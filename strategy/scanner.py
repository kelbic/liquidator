"""Which Morpho-Base markets we cover. Source of truth is the Dune scan
(analysis/morpho_base_liq_scan.sql -> markets_to_config), exported to JSON."""
from __future__ import annotations
import json
from dataclasses import dataclass


@dataclass
class Market:
    market_id: str
    collateral_token: str
    loan_token: str = ""
    lltv: float = 0.0
    bonus: float = 0.0              # LIF - 1, exact from lltv
    expected_slippage: float = 0.01  # measured collateral exit cost


def load_covered_markets(path: str) -> list[Market]:
    """Load the curated market set produced from the Dune scan.
    Returns [] if the export is absent (e.g. before first scan)."""
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    return [Market(**m) for m in raw]
