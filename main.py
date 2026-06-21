"""Entry point. Phase 1 = monitor (paper-trade): scan covered markets for HF<1,
simulate, log what we WOULD do. No transactions are submitted in monitor mode.

Until Phase 1 logic lands this loop runs idle, so the service + cgroup caps can be
validated on the VPS (systemd-cgtop) without doing anything risky."""
from __future__ import annotations
import logging
import time

from config import Config
from store import Store
from alerts import Alerter
from strategy.scanner import load_covered_markets
from strategy.guard import KillSwitch, GuardState


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")


def main():
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    log = logging.getLogger("liquidator")

    store = Store(cfg.db_path)
    alerter = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, cfg.alert_antiflood_sec)
    guard = KillSwitch(cfg.max_daily_loss_usd, cfg.max_daily_gas_usd, cfg.max_inflight)
    markets = load_covered_markets(cfg.covered_markets_path)

    log.info("liquidator start: mode=%s chain=%s markets=%d", cfg.mode, cfg.chain, len(markets))
    if cfg.mode == "execute":
        log.warning("EXECUTE mode — real txs. kill switch: loss<=$%.0f gas<=$%.0f",
                    cfg.max_daily_loss_usd, cfg.max_daily_gas_usd)

    while True:
        try:
            today = store.realized_today()
            st = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"], inflight=0)
            blocked = guard.blocked_reason(st)
            if blocked and cfg.mode == "execute":
                log.error("kill switch engaged: %s", blocked)
                alerter.send(f"\U0001F6D1 liquidator halted: {blocked}", key="killswitch")
            # TODO(phase1): per market -> positions_at_risk -> simulate ->
            #   if profitable & guards clear: paper-log (monitor) or submit (execute).
            log.debug("tick: markets=%d (phase1 scan not implemented)", len(markets))
        except Exception:
            log.exception("loop error")
            alerter.send("\U0001F6A8 liquidator loop error — check journalctl", key="looperr")
        time.sleep(cfg.poll_interval_sec)


if __name__ == "__main__":
    main()
