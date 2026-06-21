"""Circuit breaker. Halts execution when daily loss/gas limits are breached or too
many liquidations are in flight. Phase-1 monitor never executes, but guards are
evaluated in monitor mode too, to log when we WOULD have been halted."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class GuardState:
    realized_net_today: float = 0.0
    gas_spent_today: float = 0.0
    inflight: int = 0


class KillSwitch:
    def __init__(self, max_daily_loss_usd: float, max_daily_gas_usd: float, max_inflight: int):
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_daily_gas_usd = max_daily_gas_usd
        self.max_inflight = max_inflight

    def blocked_reason(self, st: GuardState) -> str | None:
        """Reason string if execution must be blocked, else None."""
        if -st.realized_net_today > self.max_daily_loss_usd:
            return f"daily loss ${-st.realized_net_today:.0f} > ${self.max_daily_loss_usd:.0f}"
        if st.gas_spent_today > self.max_daily_gas_usd:
            return f"daily gas ${st.gas_spent_today:.0f} > ${self.max_daily_gas_usd:.0f}"
        if st.inflight >= self.max_inflight:
            return f"inflight {st.inflight} >= {self.max_inflight}"
        return None
