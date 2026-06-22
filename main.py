"""Entry point. Phase 1 = monitor (paper-trade): scan covered markets, confirm health
on-chain, estimate net, log what we WOULD do. No transactions are submitted in monitor.

Two-tier scan (cheap -> precise):
  1) positions_at_risk (Morpho API, USD-approx HF) enumerates borrowers and casts a wide
     net at HF_API_CEILING — one API call for all covered markets.
  2) for the near-risk set: ONE Multicall3 aggregate3 reads market()+oracle.price() per
     market + position() per candidate across ALL markets -> EXACT on-chain HF -> analytic
     net (estimate). Immutable params (oracle, lltv) are cached, so on-chain cost is ~1-2
     eth_calls/cycle regardless of how many markets/positions are in flight.
"""
from __future__ import annotations
import json
import asyncio
import threading
import logging
import time
from collections import defaultdict

from config import Config
from chain.execute import try_liquidate
from store import Store
from alerts import Alerter
from strategy.scanner import load_covered_markets
from strategy.guard import KillSwitch, GuardState
from chain.rpc import BaseRpc
from chain.morpho import positions_at_risk, MORPHO_BLUE_ADDRESS
from chain.simulate import assess_candidates_batched, estimate

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


def fetch_candidates(markets):
    """SLOW path (Morpho API): enumerate at-risk borrowers (HF <= HF_API_CEILING), grouped by
    market + debt maps. Candidate SET drifts slowly; refresh periodically (~60s), NOT per block.
    Returns (groups, debt_by, debt_assets_by, n)."""
    candidates = positions_at_risk(markets, hf_ceiling=HF_API_CEILING)
    groups = defaultdict(list)
    debt_by = {}
    debt_assets_by = {}
    for c in candidates:
        groups[c.market_id].append(c.borrower)
        debt_by[(c.market_id, c.borrower)] = c.debt_usd
        debt_assets_by[(c.market_id, c.borrower)] = c.debt_assets
    return dict(groups), debt_by, debt_assets_by, len(candidates)


def process_candidates(rpc, by_id, groups, debt_by, debt_assets_by, store, cfg, guard, alerter,
                       log, ctx_cache, n_candidates=0):
    """FAST path (on-chain): ONE aggregate3 over the candidate set -> exact HF; per
    liquidatable+profitable+unblocked candidate, execute. ~1-2 eth_calls — cheap per block."""
    summary = _empty_summary(len(by_id))
    summary["api_candidates"] = n_candidates
    if not groups:
        return summary
    assessed = assess_candidates_batched(rpc, MORPHO_BLUE_ADDRESS, groups, ctx_cache)

    for mid, borrower, hr in assessed:
        mkt = by_id.get(mid)
        if mkt is None:
            continue
        summary["confirmed"] += 1
        summary["min_hf"] = hr.hf if summary["min_hf"] is None else min(summary["min_hf"], hr.hf)
        store.upsert_position(mid, borrower, hr.hf)
        if not hr.liquidatable:
            continue
        summary["liquidatable"] += 1
        sr = estimate(hr, debt_by.get((mid, borrower), 0.0), mkt.expected_slippage,
                      GAS_USD_EST, TIP_USD_EST, cfg.min_profit_usd)        # analytic net, pure
        store.log_simulation(market_id=mid, borrower=borrower,
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
        tx_hash = None
        if cfg.mode == "execute" and not blocked:
            out = try_liquidate(rpc, cfg, mid, borrower,
                                debt_by.get((mid, borrower), 0.0),
                                debt_assets_by.get((mid, borrower), 0), log)
            if out["sent"]:
                tx_hash = out["hash"]
                status = f"submitted:{out['status']} ${out.get('profit_usd', 0):.2f}"
            else:
                status = f"skip:{out['reason'][:50]}"
        elif cfg.mode == "execute":
            status = f"blocked:{blocked}"
        else:
            status = "paper"
        store.log_action(market_id=mid, borrower=borrower, mode=cfg.mode, tx_hash=tx_hash,
                         net_usd=sr.net_usd, gas_usd=sr.gas_usd, status=status)
        log.info("ACTIONABLE %s/%s HF=%.4f net=$%.2f mode=%s status=%s",
                 mid[:10], borrower[:10], hr.hf, sr.net_usd, cfg.mode, status)
        alerter.send(f"\U0001F4B0 {cfg.mode} liquidation {borrower[:10]} "
                     f"net ${sr.net_usd:.2f} HF={hr.hf:.4f}", key=f"act:{mid}:{borrower}")
    return summary


def scan_once(rpc, markets, store, cfg, guard, alerter, log, ctx_cache) -> dict:
    """One full scan pass (poll mode): fetch candidate set (API) then process it (on-chain)."""
    by_id = {m.market_id: m for m in markets}
    groups, debt_by, debt_assets_by, ncand = fetch_candidates(markets)
    if ncand and rpc is None:
        log.warning("near-risk positions found but RPC_URL empty — on-chain confirm skipped")
        s = _empty_summary(len(markets)); s["api_candidates"] = ncand; return s
    return process_candidates(rpc, by_id, groups, debt_by, debt_assets_by, store, cfg, guard,
                              alerter, log, ctx_cache, ncand)


def rescan_markets(cfg, current_markets, ctx_cache, log):
    """Re-fetch + re-select the covered set: catches new tail markets, drops out-of-band ones,
    excludes configured SVR/OEV oracles. Hot-swaps, prunes ctx_cache, persists JSON, reloads.
    Returns the new list, or the current one unchanged on any failure."""
    try:
        from analysis.build_covered_markets import fetch_markets, select_markets, to_json_records
        exclude = {a.strip().lower() for a in (cfg.exclude_oracles or "").split(",") if a.strip()}
        records = to_json_records(select_markets(fetch_markets(), exclude_oracles=exclude))
        if not records:
            log.warning("market rescan: empty selection — keeping current %d markets", len(current_markets))
            return current_markets
        old_ids = {m.market_id for m in current_markets}
        new_ids = {r["market_id"] for r in records}
        for mid in (old_ids - new_ids):
            ctx_cache.pop(mid, None)
        try:
            with open(cfg.covered_markets_path, "w") as fh:
                json.dump(records, fh, indent=2)
        except OSError as e:
            log.warning("market rescan: persist failed: %s", e)
        new_markets = load_covered_markets(cfg.covered_markets_path)
        added, removed = new_ids - old_ids, old_ids - new_ids
        if added or removed:
            log.info("market rescan: %d markets (+%d/-%d) added=%s removed=%s", len(new_markets),
                     len(added), len(removed), sorted(a[:10] for a in added), sorted(r[:10] for r in removed))
        else:
            log.info("market rescan: %d markets (no change)", len(new_markets))
        return new_markets
    except Exception as e:
        log.warning("market rescan failed (%s) — keeping current %d markets", type(e).__name__, len(current_markets))
        return current_markets


class _Shared:
    """Candidate set shared between the API-refresh thread (writer) and the wss block loop (reader)."""
    def __init__(self, markets):
        self.lock = threading.Lock()
        self.by_id = {m.market_id: m for m in markets}
        self.groups, self.debt_by, self.debt_assets_by, self.n = {}, {}, {}, 0

    def snapshot(self):
        with self.lock:
            return self.by_id, self.groups, self.debt_by, self.debt_assets_by, self.n


def _refresh_worker(shared, markets, cfg, ctx_cache, log, stop):
    """Background thread: periodically re-fetch the at-risk candidate set (slow API) + rescan
    markets (6h). API-only (no web3) -> safe alongside the loop. Updates `shared` atomically."""
    last_rescan = time.time()
    while not stop.is_set():
        try:
            if time.time() - last_rescan >= cfg.rescan_interval_sec:
                markets = rescan_markets(cfg, markets, ctx_cache, log)
                last_rescan = time.time()
            g, db, dab, n = fetch_candidates(markets)
            with shared.lock:
                shared.by_id = {m.market_id: m for m in markets}
                shared.groups, shared.debt_by, shared.debt_assets_by, shared.n = g, db, dab, n
        except Exception:
            log.exception("candidate refresh error")
        stop.wait(cfg.candidate_refresh_sec)


async def _new_heads(ws):
    """Subscribe to newHeads; yield the LATEST header each time, draining any buffered backlog so a
    slow assess never makes us act on stale state (the loop self-aligns to the current block)."""
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
    sub = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
    if "error" in sub:
        raise RuntimeError(f"wss subscribe rejected: {sub['error']}")
    while True:
        msg = json.loads(await ws.recv())                       # block for next head
        head = (msg.get("params") or {}).get("result")
        if not head:
            continue
        while True:                                             # drain backlog -> keep only latest
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 0.01))
            except asyncio.TimeoutError:
                break
            nh = (msg.get("params") or {}).get("result")
            if nh:
                head = nh
        yield head


async def block_driven_loop(rpc, markets, store, cfg, guard, alerter, log, ctx_cache):
    """wss newHeads -> fast on-chain assess+execute every block (~2s). Background thread refreshes
    candidates+markets so the per-block path never blocks on the API. Reconnects on wss drop."""
    import websockets
    wss_url = cfg.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
    shared = _Shared(markets)
    stop = threading.Event()
    threading.Thread(target=_refresh_worker, args=(shared, markets, cfg, ctx_cache, log, stop),
                     daemon=True).start()
    last_hb = 0.0
    while True:
        try:
            async with websockets.connect(wss_url, open_timeout=20, ping_interval=20) as ws:
                log.info("wss connected — block-driven loop (mode=%s)", cfg.mode)
                async for head in _new_heads(ws):
                    blk = int(head.get("number", "0x0"), 16)
                    by_id, groups, debt_by, debt_assets_by, ncand = shared.snapshot()
                    today = store.realized_today()
                    gs = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"], inflight=0)
                    blocked = guard.blocked_reason(gs)
                    if blocked and cfg.mode == "execute":
                        if time.time() - last_hb >= HEARTBEAT_SEC:
                            log.error("kill switch engaged: %s", blocked)
                            alerter.send(f"\U0001F6D1 liquidator halted: {blocked}", key="killswitch")
                            last_hb = time.time()
                        continue
                    if not groups:
                        continue
                    t0 = time.time()
                    s = process_candidates(rpc, by_id, groups, debt_by, debt_assets_by, store, cfg,
                                           guard, alerter, log, ctx_cache, ncand)
                    if s["liquidatable"] or s["profitable"] or (time.time() - last_hb >= HEARTBEAT_SEC):
                        last_hb = time.time()
                        mh = f"{s['min_hf']:.4f}" if s["min_hf"] is not None else "n/a"
                        log.info("block %d: markets=%d cand=%d confirmed=%d liq=%d profit=%d min_hf=%s in %.2fs",
                                 blk, s["markets"], s["api_candidates"], s["confirmed"],
                                 s["liquidatable"], s["profitable"], mh, time.time() - t0)
        except Exception as e:
            log.warning("wss loop error (%s) — reconnecting in 3s", type(e).__name__)
            alerter.send("\U0001F6A8 wss loop reconnect — check journalctl", key="wsserr")
            await asyncio.sleep(3)


def main():
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    log = logging.getLogger("liquidator")

    store = Store(cfg.db_path)
    alerter = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, cfg.alert_antiflood_sec)
    guard = KillSwitch(cfg.max_daily_loss_usd, cfg.max_daily_gas_usd, cfg.max_inflight)
    markets = load_covered_markets(cfg.covered_markets_path)
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id) if cfg.rpc_url else None
    ctx_cache: dict = {}   # immutable per-market params (oracle, lltv_wad), filled on first sight

    log.info("liquidator start: mode=%s chain=%s markets=%d rpc=%s",
             cfg.mode, cfg.chain, len(markets), "set" if rpc else "MISSING")
    if not cfg.rpc_url:
        log.warning("RPC_URL empty — only API enumeration, no on-chain confirm")
    if cfg.mode == "execute":
        log.warning("EXECUTE mode — real txs. kill switch: loss<=$%.0f gas<=$%.0f",
                    cfg.max_daily_loss_usd, cfg.max_daily_gas_usd)

    if cfg.loop_mode == "block" and rpc:
        asyncio.run(block_driven_loop(rpc, markets, store, cfg, guard, alerter, log, ctx_cache))
        return

    last_hb = 0.0
    last_rescan = time.time()
    while True:
        try:
            if rpc and time.time() - last_rescan >= cfg.rescan_interval_sec:
                markets = rescan_markets(cfg, markets, ctx_cache, log)
                last_rescan = time.time()
            t0 = time.time()
            today = store.realized_today()
            st = GuardState(realized_net_today=today["net"], gas_spent_today=today["gas"], inflight=0)
            blocked = guard.blocked_reason(st)
            if blocked and cfg.mode == "execute":
                log.error("kill switch engaged: %s", blocked)
                alerter.send(f"\U0001F6D1 liquidator halted: {blocked}", key="killswitch")
            s = scan_once(rpc, markets, store, cfg, guard, alerter, log, ctx_cache) if markets else _empty_summary(0)
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
