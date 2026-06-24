"""Hot path (Phase 2, PRECONF-triggered). A SECOND trigger beside the block loop: on a real `transmit`
to one of our aggregators, recompute the affected candidates' HF on the PRE-CONFIRMED price, and for a
position that flips liquidatable + passes the preconf-pending sim + the net floor + the narrow reaction
filter ($2k+), prepare and dispatch a liquidation. Reuses the battle-tested prepare/dispatch building
blocks; ONLY the sourcing (preconf price for sizing + preconf-pending sim) and the trigger differ.

Latency shape: the async recv loop only DETECTS a transmit (precise 94-form match) and spawns a worker
thread; all slow work (preconf read -> flip -> KyberSwap quote -> sim -> dispatch) runs off the recv
loop so detection stays responsive. Cross-path nonce safety (this path + the block loop both send from
one wallet) is handled by the send-lock inside dispatch_liquidations (added in C2).

DORMANT until wired into block_driven_loop behind cfg.hot_path. No behavior change to the existing loop.
"""
from __future__ import annotations
import threading
import time

MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
FB_URL = "wss://mainnet.flashblocks.base.org/ws"
PRECONF_RPC_DEFAULT = "https://mainnet-preconf.base.org"


# ---- pure helpers (unit-tested) ----

def _flips(price, lltv_wad, tba, tbs, positions):
    """PURE flip recompute on the preconf price — identical to the armed assess (chain.simulate.health_from).
    positions: [(borrower, borrow_shares, collateral, debt_usd)] -> [(borrower, HealthReport, debt_usd)]."""
    from chain.simulate import MarketContext, health_from
    ctx = MarketContext(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                        total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
    out = []
    for (b, bs, col, du) in positions:
        hr = health_from(ctx, int(bs), int(col))
        if hr.liquidatable:
            out.append((b, hr, du))
    return out


def _net_gate(profit_wei, debt_usd, debt_assets, cfg):
    """PURE: profit/cost/net in USD — same arithmetic as prepare_liquidation's floor."""
    profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
    cost_usd = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
    return profit_usd, cost_usd, profit_usd - cost_usd


# ---- on-chain reads ----

def read_preconf_price(preconf_rpc, oracle):
    """oracle.price() at block='pending' on the preconf RPC = the pre-confirmed Morpho price. None on fail."""
    from chain.simulate import ORACLE_ABI
    try:
        return int(preconf_rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending"))
    except Exception:
        return None


def read_positions(rpc, mid, borrowers):
    """market totals + each position at latest -> (tba, tbs, [(borrower, bs, col)]). ONE aggregate3."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    b32 = rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))]
    for b in borrowers:
        calls.append((MORPHO_BLUE, encode_position_call(b32, b)))
    res = aggregate3(rpc, calls)
    if not res or not res[0][0] or not res[0][1]:
        return None
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    pos = []
    for b, (ok, data) in zip(borrowers, res[1:]):
        if ok and data:
            bs, col = decode_position(data)
            pos.append((b, bs, col))
    return tba, tbs, pos


def resolve_meta(rpc, markets):
    """{mid: (oracle, lltv_wad)} via ONE aggregate3 of idToMarketParams (immutable)."""
    from chain.multicall import aggregate3, encode_id_to_market_params_call, decode_id_to_market_params
    mids = [m.market_id for m in markets if m.market_id]
    calls = [(MORPHO_BLUE, encode_id_to_market_params_call(rpc.to_bytes32(mid))) for mid in mids]
    out = {}
    for mid, (ok, data) in zip(mids, aggregate3(rpc, calls)):
        if ok and data:
            _loan, _coll, oracle, _irm, lltv = decode_id_to_market_params(data)
            out[mid] = (oracle, lltv)
    return out


# ---- preconf-sourced prepare (mirror of execute.prepare_liquidation; 2 diffs only) ----

def prepare_hot(rpc, preconf_rpc, cfg, market_id, borrower, debt_usd, debt_assets, price, slippage_bps=100):
    """Mirror of execute.prepare_liquidation, PRECONF-sourced. Two and only two differences:
      (1) the seize is sized on the PRE-CONFIRMED `price` (passed in, read once by the caller), not a
          fresh latest read — so `expected_seized` matches what the contract seizes at execution;
      (2) the simulate is gated against PRECONF-PENDING (simulate_tx(preconf_rpc, ..., block='pending'))
          where the position is already liquidatable, instead of latest (still healthy pre-confirm).
    Everything else — fresh market-param/market/position reads, seize math, KyberSwap quote, encode,
    honest net floor, minProfit=95% — is identical to the armed prepare. Same {ok, ...} shape."""
    try:
        from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
            encode_market_call, decode_market, encode_position_call, decode_position)
        from chain.simulate import to_assets_up
        from chain.execute import kyber_swap, encode_liquidate, expected_seized, simulate_tx
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = w3.eth.account.from_key(cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        r1 = aggregate3(rpc, [(MORPHO_BLUE, encode_id_to_market_params_call(mid)),
                              (MORPHO_BLUE, encode_market_call(mid)),
                              (MORPHO_BLUE, encode_position_call(mid, borrower))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
        m = decode_market(r1[1][1]); tba, tbs = m[2], m[3]
        borrow_shares, _ = decode_position(r1[2][1])
        if borrow_shares == 0:
            return {"ok": False, "reason": "no debt (cleared)"}

        repaid_shares = int(borrow_shares)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), int(price))   # (1) preconf price
        if seized == 0:
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=slippage_bps)

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0)
        sim = simulate_tx(preconf_rpc, liq, bot, cd0, block="pending")        # (2) preconf-pending gate
        if not sim["ok"]:
            return {"ok": False, "reason": f"preconf sim revert: {sim['error']}"}
        profit_wei = sim["profit"]
        profit_usd, cost_usd, net_usd = _net_gate(profit_wei, debt_usd, debt_assets, cfg)
        if net_usd < cfg.min_profit_usd:
            return {"ok": False, "net_usd": net_usd,
                    "reason": f"net ${net_usd:.2f} (profit ${profit_usd:.2f} - cost ${cost_usd:.2f}) < min ${cfg.min_profit_usd:.2f}"}

        min_profit_final = profit_wei * 95 // 100
        cd1 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], min_profit_final)
        return {"ok": True, "calldata": cd1, "net_usd": net_usd, "profit_usd": profit_usd, "cost_usd": cost_usd}
    except Exception as e:
        return {"ok": False, "reason": f"error: {type(e).__name__}: {str(e)[:120]}"}


# ---- per-transmit worker (reaction filter + kill-switch + dispatch) ----

def hot_min_repaid(cfg):
    return float(getattr(cfg, "hot_min_repaid_usd", 2000.0))


def _process_transmit(agg, block, subidx, t, *, rpc, preconf_rpc, cfg, feeds, meta, shared,
                      store, guard, alerter, log,
                      price_fn=read_preconf_price, reads_fn=read_positions,
                      prepare_fn=prepare_hot, dispatch_fn=None):
    """For one detected transmit: per affected market, read the preconf price, recompute flips, keep only
    reaction prizes (repaid >= hot_min_repaid), check the SHARED kill-switch, prepare_hot, and dispatch
    the passers. Injectable seams (price_fn/reads_fn/prepare_fn/dispatch_fn) for unit tests. Returns the
    dispatch results (list) for logging/tests."""
    from strategy.guard import GuardState
    if dispatch_fn is None:
        from chain.execute import dispatch_liquidations as dispatch_fn  # noqa: F811
    floor = hot_min_repaid(cfg)
    with shared.lock:
        groups = dict(shared.groups); debt_by = dict(shared.debt_by); debt_assets_by = dict(shared.debt_assets_by)

    ready, meta_by = [], {}
    for mid in feeds.get(agg, []):
        om = meta.get(mid)
        borrowers = groups.get(mid, [])
        if not om or not borrowers:
            continue
        oracle, lltv_wad = om
        price = price_fn(preconf_rpc, oracle)
        if price is None:
            continue
        rp = reads_fn(rpc, mid, borrowers)
        if not rp:
            continue
        tba, tbs, pos = rp
        positions = [(b, bs, col, debt_by.get((mid, b), 0.0)) for (b, bs, col) in pos]
        for (b, hr, du) in _flips(price, lltv_wad, tba, tbs, positions):
            if du < floor:                                   # reaction filter: $2k+ only
                continue
            prep = prepare_fn(rpc, preconf_rpc, cfg, mid, b, du,
                              debt_assets_by.get((mid, b), 0), price)
            if prep.get("ok"):
                ready.append((mid, b, prep)); meta_by[(mid, b)] = (hr, du)
                log.info("HOT ready %s/%s hf=%.4f repaid~$%.0f net=$%.2f block=%d subidx=%s",
                         mid[:10], b[:10], hr.hf, du, prep["net_usd"], block, subidx)
            else:
                log.info("HOT skip %s/%s: %s", mid[:10], b[:10], str(prep.get("reason"))[:80])

    if not ready:
        return []

    today = store.realized_today()                           # SHARED kill-switch with the block loop
    blocked = guard.blocked_reason(GuardState(realized_net_today=today["net"],
                                              gas_spent_today=today["gas"], inflight=0))
    if blocked:
        log.error("HOT kill switch engaged: %s — %d ready NOT dispatched", blocked, len(ready))
        alerter.send(f"\U0001F6D1 hot path halted: {blocked}", key="hot-killswitch")
        return []

    results = dispatch_fn(rpc, cfg, ready, log=log, max_inflight=cfg.max_inflight)
    for r in results:
        hr, du = meta_by.get((r["market_id"], r["borrower"]), (None, 0.0))
        status = (f"submitted:{r.get('status')} net${r.get('net_usd',0):.2f}" if r["sent"]
                  else f"skip:{str(r.get('reason'))[:50]}")
        store.log_action(market_id=r["market_id"], borrower=r["borrower"], mode=cfg.mode,
                         tx_hash=r.get("hash"), net_usd=r.get("net_usd", 0.0), gas_usd=r.get("gas_usd", 0.0), status=status)
        log.info("HOT ACTIONABLE %s/%s status=%s tx=%s", r["market_id"][:10], r["borrower"][:10], status, r.get("hash"))
        alerter.send(f"\U0001F525 HOT {cfg.mode} {r['borrower'][:10]} {status}", key=f"hot:{r['market_id']}:{r['borrower']}")
    return results


# ---- async flashblock subscriber (detect + spawn) ----

async def hot_loop(rpc, preconf_rpc, cfg, shared, store, guard, alerter, log, stop, markets,
                   ws_factory=None):
    """Subscribe to Flashblocks; on a PRECISE transmit to one of our aggregators, spawn a worker thread
    that runs _process_transmit (the recv loop itself does no slow work). Re-resolves feeds/meta on a
    cadence (rotation). DORMANT until block_driven_loop starts this behind cfg.hot_path."""
    import asyncio
    import json
    import websockets
    import brotli
    from chain.feeds import resolve_feeds, extract_txs, block_number_of

    def _default_ws():
        return websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None)
    ws_factory = ws_factory or _default_ws

    feeds = resolve_feeds(rpc, markets)
    meta = resolve_meta(rpc, markets)
    agg_set = set(feeds)
    last_resolve = time.monotonic()
    log.info("hot path: %d aggregators / %d markets (preconf=%s, tip=%.1f gwei, floor repaid $%.0f)",
             len(agg_set), len(meta), preconf_rpc.rpc_url, cfg.tip_gwei, hot_min_repaid(cfg))

    while not stop.is_set():
        try:
            async with ws_factory() as ws:
                while not stop.is_set():
                    if time.monotonic() - last_resolve >= cfg.rescan_interval_sec:
                        try:
                            feeds = resolve_feeds(rpc, markets); meta = resolve_meta(rpc, markets)
                            agg_set = set(feeds); last_resolve = time.monotonic()
                        except Exception:
                            pass
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                        d = json.loads(txt)
                    except Exception:
                        continue
                    bn = block_number_of(d)
                    if bn is None:
                        continue
                    idx = d.get("index"); txs = extract_txs(d); now = time.time()
                    for agg in agg_set:                          # PRECISE 94-form match (no over-count)
                        pf = "94" + agg[2:]
                        if any((to_ == agg) or (raw_ and pf in raw_) for raw_, to_ in txs):
                            threading.Thread(target=_process_transmit, args=(agg, bn, idx, now),
                                             kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                         feeds=feeds, meta=meta, shared=shared, store=store,
                                                         guard=guard, alerter=alerter, log=log),
                                             daemon=True).start()
        except Exception as e:
            log.warning("hot path ws error (%s) — reconnecting in 3s", type(e).__name__)
            alerter.send("\U0001F6A8 hot-path reconnect — check journalctl", key="hot-wsserr")
            await asyncio.sleep(3)
