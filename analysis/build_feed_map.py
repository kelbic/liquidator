"""Read-only: build feed_to_market.json — reverse index aggregator -> [our markets].
Validated chain (oracle_recon 3/3): idToMarketParams(id).oracle -> oracle.BASE_FEED_1() ->
proxy.aggregator() (+description()). OCR aggregator = emits AnswerUpdated / carries transmit in a
sub-block = what step #3 watches. Non-Chainlink oracles (no BASE_FEED_1/aggregator) flagged, not
guessed. Read-only: eth_call only, no key. Writes feed_to_market.json.
    python -m analysis.build_feed_map
"""
from __future__ import annotations
import sys

ZERO = "0x0000000000000000000000000000000000000000"


def _addr_abi(name):
    return [{"name": name, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "address"}]}]


def _str_abi(name):
    return [{"name": name, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "string"}]}]


def _try_addr(rpc, addr, getter):
    try:
        c = rpc.contract(addr, _addr_abi(getter))
        v = getattr(c.functions, getter)().call()
        return v if v and v != ZERO else None
    except Exception:
        return None


def _try_str(rpc, addr, getter):
    try:
        return getattr(rpc.contract(addr, _str_abi(getter)).functions, getter)().call()
    except Exception:
        return None


def main():
    import json
    from collections import defaultdict
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)

    markets = load_covered_markets(cfg.covered_markets_path)
    print(f"==== feed-карта по {len(markets)} covered-рынкам ====")

    feed_map = defaultdict(lambda: {"markets": [], "pair": None, "proxy": None})
    chainlink, dropped = 0, []
    oracle_cache = {}

    for m in markets:
        mid = m.market_id
        try:
            oracle = morpho.functions.idToMarketParams(rpc.to_bytes32(mid)).call()[2]
        except Exception as e:
            dropped.append((mid, f"idToMarketParams err:{type(e).__name__}")); continue

        if oracle in oracle_cache:
            agg, proxy, desc = oracle_cache[oracle]
        else:
            proxy = _try_addr(rpc, oracle, "BASE_FEED_1")
            if not proxy:
                agg, desc = None, None
            else:
                agg = _try_addr(rpc, proxy, "aggregator") or proxy
                desc = _try_str(rpc, proxy, "description")
            oracle_cache[oracle] = (agg, proxy, desc)

        if not agg:
            reason = "no-BASE_FEED (не MorphoChainlinkV2 — Pyth/иной оракул)" if not proxy else "no-aggregator()"
            dropped.append((mid, f"{reason} oracle={oracle}"))
            continue

        a = agg.lower()
        feed_map[a]["markets"].append(mid)
        feed_map[a]["pair"] = feed_map[a]["pair"] or desc
        feed_map[a]["proxy"] = feed_map[a]["proxy"] or proxy
        chainlink += 1
        print(f"  {desc or '?':<14} agg={agg}  market={mid[:12]}…")

    out = {a: dict(v) for a, v in feed_map.items()}
    with open("feed_to_market.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n==== СВОДКА ====")
    print(f"  на Chainlink-BASE_FEED_1: {chainlink}/{len(markets)} рынков  ->  {len(out)} уникальных агрегаторов")
    for a, v in sorted(out.items(), key=lambda kv: -len(kv[1]["markets"])):
        print(f"    {v['pair'] or '?':<14} {a}  рынков: {len(v['markets'])}")
    if dropped:
        print(f"\n  ВЫПАЛИ (не Chainlink-ветка — обработать отдельно по факту): {len(dropped)}")
        for mid, why in dropped:
            print(f"    {mid[:14]}…  {why}")
    print(f"\n[=] записан feed_to_market.json (aggregator -> markets) — lookup под под-блок-ридер шага #3")
    print("    непокрытые рынки -> отдельная ветка (свой оракул), НЕ тащим в Chainlink-ридер вслепую.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
