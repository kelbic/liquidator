"""STAGE 3 — read-only PAPER optimistic gate. At EQUAL-START detection (everyone gets the preconf
signal ~same sub-block), how many real liquidations would we DETECT in time? On each oracle update
caught in a sub-block (chain.feeds), recompute near-threshold candidates' health on the PRE-CONFIRMED
price (preconf RPC price('pending')) via the bot's EXACT health_from(), log PAPER 'would-take' flips.
Key signal = EDGE: liquidatable on PRECONF but NOT yet on CONFIRMED at detection = seen ~1.8s before
our current confirmed assess (which catches ~0 of these). Then check if each flagged position gets
liquidated within a couple blocks (real contested opp we'd race for). NO send. NO bot touch.
Read-only: ws + eth_call + Morpho API, no key, no tx.
    DURATION=600 python -m analysis.paper_gate
"""
from __future__ import annotations
import os
import sys

DURATION = float(os.environ.get("DURATION", "600"))
PRECONF_URL = os.environ.get("PRECONF_URL", "https://mainnet-preconf.base.org")
CAND_HF_CEILING = 1.05
MAX_CAND_PER_MARKET = 20
CAND_REFRESH_SEC = 60.0
CONFIRM_AFTER_BLOCKS = 2


async def run():
    import asyncio, json, time
    from collections import Counter, defaultdict
    import websockets, brotli
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS, MIN_DEBT_USD, positions_at_risk
    from chain.simulate import MarketContext, read_market_context, read_position, health_from, ORACLE_ABI
    from chain.feeds import resolve_feeds, extract_txs, block_number_of, FB_URL
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    pre = BaseRpc(PRECONF_URL, cfg.chain_id)
    MORPHO = MORPHO_BLUE_ADDRESS

    markets = load_covered_markets(cfg.covered_markets_path)
    feeds = resolve_feeds(rpc, markets)
    agg_set = set(feeds)
    print(f"==== STAGE 3 PAPER optimistic gate ({DURATION:.0f}s) ====")
    print(f"  {len(agg_set)} агрегаторов; HF на preconf price vs confirmed; NO send.")
    try:
        print(f"  preconf reachable: block_number={pre.block_number()}")
    except Exception as e:
        sys.exit(f"preconf endpoint недоступен ({type(e).__name__}) — нужен flashblock-aware RPC.")

    def preconf_price(oracle):
        return pre.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending")

    cand = defaultdict(list)
    last_cand = [0.0]

    def refresh_candidates():
        cand.clear()
        try:
            for p in positions_at_risk(markets, hf_ceiling=CAND_HF_CEILING):
                cand[p.market_id].append((p.borrower, p.debt_usd))
        except Exception as e:
            print(f"  ! refresh candidates err: {type(e).__name__}: {str(e)[:80]}")
        last_cand[0] = time.monotonic()

    flags = []
    pending_confirm = []
    cur_block = None
    t_end = time.monotonic() + DURATION

    def confirm_taken(now_block):
        still = []
        for ev in pending_confirm:
            if now_block < ev["block_due"]:
                still.append(ev); continue
            try:
                bs, col = read_position(rpc, MORPHO, ev["market"], ev["borrower"])
                taken = (bs == 0)
            except Exception:
                taken = None
            ev["taken"] = taken
            flags.append(ev)
            mark = "TAKEN(кем-то)" if taken else ("ещё открыта" if taken is False else "?")
            print(f"    confirm blk~{ev['block_due']} {ev['market'][:10]}…/{ev['borrower'][:8]}… -> {mark}")
        pending_confirm[:] = still

    async with websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None) as ws:
        refresh_candidates()
        print(f"  кандидатов (HF<{CAND_HF_CEILING}) на старте: {sum(len(v) for v in cand.values())} в {len(cand)} рынках")
        while time.monotonic() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, t_end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            try:
                txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                d = json.loads(txt)
            except Exception:
                continue
            bn = block_number_of(d)
            if bn is None:
                continue
            if time.monotonic() - last_cand[0] > CAND_REFRESH_SEC:
                refresh_candidates()
            if cur_block is not None and bn != cur_block:
                confirm_taken(bn)
            cur_block = bn
            idx = d.get("index")

            matched_aggs = set()
            for rawtx, to in extract_txs(d):
                a = to if (to and to in agg_set) else next((x for x in agg_set if rawtx and x[2:] in rawtx), None)
                if a:
                    matched_aggs.add(a)
            for agg in matched_aggs:
                for market in feeds.get(agg, []):
                    cands = cand.get(market, [])
                    if not cands:
                        continue
                    try:
                        ctx = read_market_context(rpc, MORPHO, market)
                        p_pre = preconf_price(ctx.oracle)
                    except Exception:
                        continue
                    ctx_pre = MarketContext(ctx.oracle, p_pre, ctx.lltv_wad,
                                            ctx.total_borrow_assets, ctx.total_borrow_shares)
                    for borrower, debt_usd in cands[:MAX_CAND_PER_MARKET]:
                        if (debt_usd or 0) < MIN_DEBT_USD:
                            continue
                        try:
                            bs, col = read_position(rpc, MORPHO, market, borrower)
                        except Exception:
                            continue
                        hr_pre = health_from(ctx_pre, bs, col)
                        if not hr_pre.liquidatable:
                            continue
                        hr_conf = health_from(ctx, bs, col)
                        edge = not hr_conf.liquidatable
                        tag = "EDGE(preconf-only, видим ~1.8с раньше)" if edge else "already-confirmed-liq"
                        print(f"  PAPER blk {bn} idx {idx} {market[:10]}…/{borrower[:8]}… "
                              f"HFpre={hr_pre.hf:.4f} HFconf={hr_conf.hf:.4f} ~${debt_usd:,.0f}  [{tag}]")
                        pending_confirm.append({"block_due": bn + CONFIRM_AFTER_BLOCKS, "market": market,
                                                "borrower": borrower, "debt_usd": debt_usd or 0,
                                                "index": idx, "edge": edge})

    if pending_confirm and cur_block is not None:
        confirm_taken(cur_block + CONFIRM_AFTER_BLOCKS)

    print(f"\n==== РЕЗУЛЬТАТ ({len(flags)} paper-флипов) ====")
    if not flags:
        print("  ни одного флипа за окно — тихо/нет ликвидируемых у порога. Гонять дольше/в волатильность.")
        return
    edge = [f for f in flags if f["edge"]]
    taken_edge = [f for f in edge if f.get("taken")]
    idxc = Counter(f["index"] for f in edge)
    print(f"  EDGE (ликвидируемо на PRECONF, ещё НЕ на confirmed = детект ~1.8с раньше): {len(edge)}")
    print(f"    из них реально ЛИКВИДИРОВАНЫ кем-то в пределах {CONFIRM_AFTER_BLOCKS} блоков: {len(taken_edge)}")
    print(f"    по под-блоку (index) детекта: {dict(sorted(idxc.items()))}")
    print(f"  already-confirmed-liq (поймали бы и текущим ассессом): {len(flags) - len(edge)}")
    print(f"\n  Смысл: EDGE-флипы — ликвидации, которые наш ТЕКУЩИЙ (confirmed) путь пропускает, а preconf-гейт")
    print(f"  видит ~1.8с раньше, на равных с полем. '{len(taken_edge)} TAKEN' = реальные контестованные сделки,")
    print(f"  за которые мы были бы в гонке. Это потолок ДЕТЕКЦИИ при равном старте; фактический win-rate")
    print(f"  решает латентность отправки (следующая стадия — с деньгами/гейтами).")
    print("  (read-only paper: ноль отправки, бот не тронут.)")


def main():
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
