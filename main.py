"""Entry point. Phase 1 = monitor (paper-trade): scan covered markets, confirm health
on-chain, estimate net, log what we WOULD do. No transactions are submitted in monitor.

Two-tier scan (cheap -> precise):
  1) positions_at_risk (Morpho API, USD-approx HF) enumerates borrowers and casts a wide
     net at HF_API_CEILING — one API call for all covered markets.
  2) for the near-risk set: read_market_context once/market + Multicall3 batch_positions
     (one eth_call/market) -> EXACT on-chain HF (health_from) -> analytic net (estimate).
Only positions the API flags as near liquidation are confirmed on-chain, so RPC stays light.
"""
from __future__ import annotations
import logging
import time
from collections import defaultdict

from config import Config
from store import Store
from alerts import Alerter
from strategy.scanner import load_covered_markets
from strategy.guard import KillSwitch, GuardState
from chain.rpc import BaseRpc
from chain.morpho import positions_at_risk, MORPHO_BLUE_ADDRESS
from chain.simulate import read_market_context, health_from, estimate
from chain.multicall import batch_positions

# API USD-approx wide net; the precise on-chain HF makes the real call. Loose on purpose
# (API prices lag the oracle) so nothing near liquidation is missed before on-chain confirm.
HF_API_CEILING = 1.10
# Base gas is tiny; these are monitor placeholders for the paper PnL. Execute computes real
# gas from the live base fee + a competitive sequencer tip.
GAS_USD_EST = 0.10
TIP_USD_EST = 0.05
# Heartbeat "scan:" line is throttled to this cadence to keep logs tiny; ACTIONABLE
# finds and errors are logged unconditionally (never throttled).
HEARTBEAT_SEC = 300


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")


def _empty_summary(n_markets: int) -> dict:
    return {"markets": n_markets, "api_candidates": 0, "confirmed": 0,
            "liquidatable": 0, "profitable": 0, "min_hf": None}


def scan_once(rpc, markets, store, cfg, guard, alerter, log) -> dict:
    """One scan pass. Returns a summary dict for the heartbeat line."""
    by_id = {m.market_id: m for m in markets}
    candidates = positions_at_risk(markets, hf_ceiling=HF_API_CEILING)  # API, USD-approx
    summary = _empty_summary(len(markets))
    summary["api_candidates"] = len(candidates)
    if not candidates:
        return summary
    if rpc is None:
        log.warning("near-risk positions found but RPC_URL empty — on-chain confirm skipped")
        return summary

    groups = defaultdict(list)
    for c in candidates:
        groups[c.market_id].append(c)

    for mid, cands in groups.items():
        mkt = by_id.get(mid)
        if mkt is None:
            continue
        ctx = read_market_context(rpc, MORPHO_BLUE_ADDRESS, mid)           # 3 eth_calls
        batched = batch_positions(rpc, MORPHO_BLUE_ADDRESS, mid, [c.borrower for c in cands])  # 1 eth_call
        for c, res in zip(cands, batched):
            if res is None:
                continue
            borrow_shares, collateral = res
            hr = health_from(ctx, borrow_shares, collateral)               # exact, pure
            summary["confirmed"] += 1
            summary["min_hf"] = hr.hf if summary["min_hf"] is None else min(summary["min_hf"], hr.hf)
            store.upsert_position(mid, c.borrower, hr.hf)
            if not hr.liquidatable:
                continue
            summary["liquidatable"] += 1
            sr = estimate(hr, c.debt_usd, mkt.expected_slippage, GAS_USD_EST, TIP_USD_EST,
                          cfg.min_profit_usd)                              # analytic net, pure
            store.log_simulation(market_id=mid, borrower=c.borrower,
                                 repaid_usd=sr.repaid_usd, seized_usd=sr.seized_usd,
                                 bonus_usd=sr.seized_usd - sr.repaid_usd, gas_usd=sr.gas_usd,
                                 net_usd=sr.net_usd, would_submit=int(sr.profitable),
                                 reverted=int(sr.reverted), note=sr.note)
            if not sr.profitable:
                continue
            summary["profitable"] += 1
            today = store.realized_today()
            gs = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"], inflight=0)
            blocked = guard.blocked_reason(gs)
            if cfg.mode == "execute" and not blocked:
                pass  # TODO(execute): simulate_tx + submit; record tx_hash + status
            status = "paper" if cfg.mode == "monitor" else (f"blocked:{blocked}" if blocked else "would_submit")
            store.log_action(market_id=mid, borrower=c.borrower, mode=cfg.mode, tx_hash=None,
                             net_usd=sr.net_usd, gas_usd=sr.gas_usd, status=status)
            log.info("ACTIONABLE %s/%s HF=%.4f net=$%.2f mode=%s status=%s",
                     mid[:10], c.borrower[:10], hr.hf, sr.net_usd, cfg.mode, status)
            alerter.send(f"\U0001F4B0 {cfg.mode} liquidation {c.borrower[:10]} "
                         f"net ${sr.net_usd:.2f} HF={hr.hf:.4f}", key=f"act:{mid}:{c.borrower}")
    return summary


def main():
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    log = logging.getLogger("liquidator")

    store = Store(cfg.db_path)
    alerter = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, cfg.alert_antiflood_sec)
    guard = KillSwitch(cfg.max_daily_loss_usd, cfg.max_daily_gas_usd, cfg.max_inflight)
    markets = load_covered_markets(cfg.covered_markets_path)
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id) if cfg.rpc_url else None

    log.info("liquidator start: mode=%s chain=%s markets=%d rpc=%s",
             cfg.mode, cfg.chain, len(markets), "set" if rpc else "MISSING")
    if not cfg.rpc_url:
        log.warning("RPC_URL empty — only API enumeration, no on-chain confirm")
    if cfg.mode == "execute":
        log.warning("EXECUTE mode — real txs. kill switch: loss<=$%.0f gas<=$%.0f",
                    cfg.max_daily_loss_usd, cfg.max_daily_gas_usd)

    last_hb = 0.0
    while True:
        try:
            t0 = time.time()
            today = store.realized_today()
            st = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"], inflight=0)
            blocked = guard.blocked_reason(st)
            if blocked and cfg.mode == "execute":
                log.error("kill switch engaged: %s", blocked)
                alerter.send(f"\U0001F6D1 liquidator halted: {blocked}", key="killswitch")
            s = scan_once(rpc, markets, store, cfg, guard, alerter, log) if markets else _empty_summary(0)
            now = time.time()
            if (s["liquidatable"] or s["profitable"]) or (now - last_hb >= HEARTBEAT_SEC):
                last_hb = now
                mh = f"{s['min_hf']:.4f}" if s["min_hf"] is not None else "n/a"
                log.info("scan: markets=%d api_cand=%d confirmed=%d liq=%d profit=%d min_hf=%s in %.2fs",
                         s["markets"], s["api_candidates"], s["confirmed"], s["liquidatable"],
                         s["profitable"], mh, time.time() - t0)
        except Exception:
            log.exception("loop error")
            alerter.send("\U0001F6A8 liquidator loop error — check journalctl", key="looperr")
        time.sleep(cfg.poll_interval_sec)


if __name__ == "__main__":
    main()
