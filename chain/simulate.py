"""Simulate a liquidation BEFORE submitting — the on-chain equivalent of the
dry-run (docs/WORKFLOW.md). A tx that would revert or net < min_profit is never
sent. Phase 1 uses this to paper-trade; Phase 2 gates real submission on it."""
from __future__ import annotations
from dataclasses import dataclass

from chain.rpc import BaseRpc
from chain.morpho import Position


@dataclass
class SimResult:
    profitable: bool
    reverted: bool
    net_usd: float
    repaid_usd: float
    seized_usd: float
    gas_usd: float
    note: str = ""


class Simulator:
    def __init__(self, rpc: BaseRpc):
        self.rpc = rpc

    def simulate_liquidation(self, pos: Position, min_profit_usd: float) -> SimResult:
        """eth_call / fork-simulate the full liquidate+swap bundle and price it.
        TODO(phase1): build calldata, eth_call vs latest state (or anvil/Tenderly
        fork), decode seized/repaid, price to USD, subtract gas + sequencer tip."""
        raise NotImplementedError("phase1: implement liquidation simulation")
