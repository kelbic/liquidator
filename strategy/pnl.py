"""Per-liquidation net-profit math. Mirrors analysis/liq_model.py so paper-trade
and the offline model agree. All USD."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class PnlInputs:
    debt_usd: float           # debt repaid
    bonus: float              # LIF - 1 (e.g. 0.07)
    slippage: float           # fraction lost selling seized collateral
    gas_usd: float
    tip_usd: float = 0.0      # priority fee paid to the Base sequencer (the "bribe")
    flashloan_fee: float = 0.0  # Morpho built-in flash loan = 0


def net_profit(p: PnlInputs) -> float:
    gross_bonus = p.debt_usd * p.bonus
    seized_value = p.debt_usd * (1.0 + p.bonus)
    swap_cost = seized_value * p.slippage
    fl_cost = p.debt_usd * p.flashloan_fee
    return gross_bonus - swap_cost - p.gas_usd - p.tip_usd - fl_cost


def lif_from_lltv(lltv: float, cursor: float = 0.3, max_lif: float = 1.15) -> float:
    """Morpho Blue: LIF = min(max_lif, 1/(1 - cursor*(1-lltv)))."""
    return min(max_lif, 1.0 / (1.0 - cursor * (1.0 - lltv)))
