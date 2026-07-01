"""Read-only recon: Morpho oracle wrapper -> Chainlink proxy -> OCR aggregator (the update emitter).
We need the AGGREGATOR addresses (emit AnswerUpdated/transmit) winners back-run, so the sub-block
reader (step #3) knows what to watch. Wrapper hides the feed behind an immutable getter whose NAME
varies by version, so we probe candidate getters via eth_call. Read-only: eth_call only, no key.
    python -m analysis.oracle_recon
"""
from __future__ import annotations
import sys

ORACLES = {
    "cbXRP/USDC": "0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE",
    "cbADA/USDC": "0x35D87a743D1F2f7CaFb42D855dC1c5Df857Ce45f",
    "cbDOGE/USDC": "0xA9D36600Fb9eba7548857e61F836Ec951e3091B2",
}
S2_SEEN = {a.lower() for a in [
    "0x92a7c3a57e17aff701c159c5480073b095100b62", "0x9491aedfe0da70eb110582b66b2edace28d82a5d",
    "0xf0f5eebea4910927b7165d8e3824abb7d7215825", "0x71e021bc2e8a709b72ac7b6036e5b2bf30f263d0",
    "0x646ec7e0ed8d0a8ab6b7e51792a7c5267e4f3201", "0xa678cb16980289f2f0053176dbf5a7fb16e37052",
]}
FEED_GETTERS = ["BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2",
                "baseFeed1", "baseFeed2", "quoteFeed1", "quoteFeed2",
                "feed", "priceFeed", "aggregator", "chainlinkFeed", "source"]
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
        c = rpc.contract(addr, _str_abi(getter))
        return getattr(c.functions, getter)().call()
    except Exception:
        return None


def main():
    from config import Config
    from chain.rpc import BaseRpc

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)

    print("==== РАЗВЕДКА оракул-обёрток: wrapper -> proxy -> OCR aggregator ====")
    resolved = {}
    for pair, wrapper in ORACLES.items():
        print(f"\n  {pair}  wrapper={wrapper}")
        feeds = []
        for g in FEED_GETTERS:
            v = _try_addr(rpc, wrapper, g)
            if v:
                feeds.append((g, v))
        if not feeds:
            print("    НИ ОДИН кандидат-геттер не ответил адресом -> нужен ABI обёртки с Basescan вручную")
            print("    адрес обёртки: " + wrapper)
            continue
        for g, proxy in feeds:
            print(f"    feed-getter {g}() -> {proxy}")
            agg = _try_addr(rpc, proxy, "aggregator")
            desc = _try_str(rpc, proxy, "description")
            if agg:
                seen = "  <== СОВПАЛ с S2 (это эмиттер обновлений!)" if agg.lower() in S2_SEEN else ""
                print(f"       proxy.aggregator() -> {agg}{seen}")
                resolved.setdefault(pair, []).append(agg.lower())
            else:
                seen = "  <== САМ совпал с S2" if proxy.lower() in S2_SEEN else ""
                print(f"       (aggregator() нет — {g}() уже сам OCR/прямой фид){seen}")
                resolved.setdefault(pair, []).append(proxy.lower())
            if desc:
                print(f"       description() -> {desc!r}")

    print("\n==== ИТОГ: какие OCR-агрегаторы слушать в под-блоках ====")
    if not resolved:
        print("  не разрешилось — нужен ABI обёртки с Basescan; скинь, подберу геттер.")
        return
    all_aggs = set()
    for pair, aggs in resolved.items():
        for a in aggs:
            all_aggs.add(a)
        print(f"  {pair}: {aggs}")
    hit = all_aggs & S2_SEEN
    print(f"\n  пересечение с S2-виденными: {len(hit)}/{len(all_aggs)} "
          + ("-> механика подтверждена: это эмиттеры, что бэк-ранят победители." if hit
             else "-> НЕ совпало: проверить getter/фид (возможно multi-feed)."))
    print("  Шаг #2 развернёт это на ВСЕ 40 рынков (idToMarketParams->wrapper->этот путь) -> feed_to_market.json")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
