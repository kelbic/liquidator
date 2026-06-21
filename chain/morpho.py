"""Read Morpho Blue state on Base: markets, positions, health factors.
On-chain reads only; no writes in Phase 1. Heavy deps imported lazily."""
from __future__ import annotations
from dataclasses import dataclass

from chain.rpc import BaseRpc
from strategy.scanner import Market

# Morpho Blue singleton — VERIFY the Base address before use (docs/STATE.md backlog).
MORPHO_BLUE_ADDRESS = "0x0000000000000000000000000000000000000000"  # TODO(phase1)


@dataclass
class Position:
    market_id: str
    borrower: str
    health_factor: float
    debt_assets: int
    collateral_assets: int


class Morpho:
    def __init__(self, rpc: BaseRpc):
        self.rpc = rpc

    def positions_at_risk(self, market: Market, hf_ceiling: float = 1.0) -> list[Position]:
        """Positions with HF <= ceiling in a market.
        TODO(phase1): enumerate borrowers (subgraph/indexer or event replay),
        read position + oracle price, HF = collateral*price*lltv / debt."""
        raise NotImplementedError("phase1: implement Morpho position reads")
