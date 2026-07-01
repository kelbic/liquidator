"""Read-only: net-after-fee on the REACTION segment (gap>2 = open public fee-race, 75% of the money).
For won liquidations: bonus=(LIF-1)*repaid (LIF from market lltv) minus actual gas fee
(effectiveGasPrice*gasUsed valued in USD). Shows how many would stay POSITIVE if WE won paying the
same fee. Answers: is entering the fee-race profitable on our sizes, or do winners take near-zero for
volume? Honest limits: (a) ignores swap slippage -> UPPER bound on net (competition_report covers
slippage; liquid feeds XRP/DOGE/ADA survive it); (b) ignores cost of LOST races (fee on reverted
attempts) -> realized profit is LOWER than per-won-tx net. So this brackets the OPTIMISTIC side.
Read-only: historical receipts + single-block getLogs + Morpho API. No key, no tx, no bot touch.
    DAYS=30 MAX_WINNERS=200 python -m analysis.net_after_fee
"""
from __future__ import annotations
import os
import sys

DAYS = float(os.environ.get("DAYS", "30"))
MIN_REPAID_USD = float(os.environ.get("MIN_REPAID_USD", "100"))
MAX_WINNERS = int(os.environ.get("MAX_WINNERS", "200"))
ETH_USD_AGG = "0x1e0b2c3896338fbb201c4f0a27c6904801dca06b"  # Chainlink ETH/USD (from feed_map)


def main():
    sys.path.insert(0, ".")
    import json, time, statistics, urllib.request, urllib.error
    from collections import Counter, defaultdict
    from web3 import Web3
    from config import Config
    from chain.rpc import BaseRpc
    from chain.feeds import _resolve_one
    from chain.simulate import MORPHO_READ_ABI
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from strategy.pnl import lif_from_lltv
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)
    T = Web3.keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
    TOPIC_CL = T if T.startswith("0x") else "0x" + T

    # ETH price (one read; priority fee dominates over its ±15%)
    eth_price = 0.0
    try:
        la_abi = [{"name": "latestAnswer", "type": "function", "stateMutability": "view",
                   "inputs": [], "outputs": [{"name": "", "type": "int256"}]}]
        eth_price = rpc.contract(ETH_USD_AGG, la_abi).functions.latestAnswer().call() / 1e8
    except Exception:
        pass
    if not eth_price:
        eth_price = float(os.environ.get("ETH_PRICE_USD", "3500"))
    print(f"==== NET-после-FEE на reaction-сегменте ({DAYS:.0f}д, ETH=${eth_price:,.0f}) ====")

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower() for m in markets}
    ours = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    since = int(time.time()) - int(DAYS * 86400)

    def gql(hf, skip):
        Q = ("query($f:Int!,$s:Int!,$w:MarketTransactionFilters!){"
             "marketTransactions(first:$f,skip:$s,orderBy:Timestamp,orderDirection:Desc,where:$w){"
             "items{ timestamp " + hf +
             " market{ marketId collateralAsset{symbol} loanAsset{symbol decimals} }"
             " data{ ... on MarketTransactionLiquidationData{ liquidator repaidAssets } } } } }")
        body = json.dumps({"query": Q, "variables": {"f": 100, "s": skip,
                          "w": {"type_in": ["Liquidation"], "chainId_in": [cfg.chain_id]}}}).encode()
        req = urllib.request.Request("https://api.morpho.org/graphql", data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as x:
                return json.loads(x.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"errors": [{"http": e.code}]}

    field = None
    for cand in ("txHash", "hash"):
        if not gql(cand, 0).get("errors"):
            field = cand; break
    if field is None:
        sys.exit("Morpho API tx-hash поле не найдено")

    winners, skip = [], 0
    while skip <= 4000:
        d = gql(field, skip)
        if d.get("errors"):
            break
        items = (((d.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        if not items:
            break
        stop = False
        for it in items:
            if int(it["timestamp"]) < since:
                stop = True; break
            mk = it.get("market") or {}; mid = (mk.get("marketId") or "").lower()
            if mid not in by_id:
                continue
            dd = it.get("data") or {}; la = mk.get("loanAsset") or {}
            if (dd.get("liquidator") or "").lower() in ours:
                continue
            repaid = int(dd.get("repaidAssets") or 0) / 10 ** int(la.get("decimals") or 18)
            if repaid < MIN_REPAID_USD:
                continue
            winners.append({"txh": it.get(field), "mid": mid, "repaid": repaid,
                            "pair": f"{(mk.get('collateralAsset') or {}).get('symbol','?')}/{la.get('symbol','?')}"})
        if stop:
            break
        skip += 100
    winners.sort(key=lambda x: -x["repaid"])
    sample = winners[:MAX_WINNERS]

    agg_cache, lltv_cache, rows = {}, {}, []
    for w in sample:
        txh = w["txh"]
        if not txh:
            continue
        txh = txh if str(txh).startswith("0x") else "0x" + str(txh)
        try:
            rc = w3.eth.get_transaction_receipt(txh)
        except Exception:
            continue
        B, W_idx = rc["blockNumber"], rc["transactionIndex"]
        eff, gas_used = rc.get("effectiveGasPrice"), rc.get("gasUsed")
        if eff is None or gas_used is None:
            continue
        if w["mid"] not in agg_cache:
            agg_cache[w["mid"]] = _resolve_one(rpc, w["mid"])
            try:
                lltv_cache[w["mid"]] = morpho.functions.idToMarketParams(rpc.to_bytes32(w["mid"])).call()[4]
            except Exception:
                lltv_cache[w["mid"]] = None
        agg = agg_cache[w["mid"]]; lltv = lltv_cache[w["mid"]]
        if lltv is None:
            continue
        gap = None
        if agg:
            try:
                logs = w3.eth.get_logs({"fromBlock": B, "toBlock": B,
                                        "address": Web3.to_checksum_address(agg), "topics": [TOPIC_CL]})
                u = [l["transactionIndex"] for l in logs]
                if u:
                    gap = W_idx - min(u)
            except Exception:
                pass
        if gap is None or gap < 0:
            continue
        seg = "atomic(<=2)" if gap <= 2 else ("near(3-10)" if gap <= 10 else "reaction(>10)")
        lif = lif_from_lltv(lltv / 1e18)
        bonus = w["repaid"] * (lif - 1.0)
        gas_usd = eff * gas_used / 1e18 * eth_price
        net = bonus - gas_usd
        fee_pct = (gas_usd / bonus * 100) if bonus > 0 else None
        rows.append({"pair": w["pair"], "repaid": w["repaid"], "gap": gap, "seg": seg,
                     "bonus": bonus, "gas_usd": gas_usd, "net": net, "fee_pct": fee_pct})

    print(f"  обработано: {len(rows)}\n")

    def report(label, rs):
        if not rs:
            print(f"  {label}: нет данных"); return
        pos = [r for r in rs if r["net"] > 0]
        nets = sorted(r["net"] for r in rs)
        fps = sorted(r["fee_pct"] for r in rs if r["fee_pct"] is not None)
        print(f"  {label}: {len(rs)} шт | net>0: {len(pos)}/{len(rs)} ({len(pos)/len(rs)*100:.0f}%)")
        print(f"     net median ${statistics.median(nets):,.0f} (min ${min(nets):,.0f} max ${max(nets):,.0f})")
        print(f"     bonus median ${statistics.median([r['bonus'] for r in rs]):,.0f}  "
              f"gas median ${statistics.median([r['gas_usd'] for r in rs]):,.1f}  "
              f"fee%% median {statistics.median(fps):.0f}%" if fps else "")
        print(f"     net-сумма по сегменту: ${sum(r['net'] for r in rs):,.0f}")

    print("== ПО СЕГМЕНТАМ (net = bonus - gas, ДО слиппеджа) ==")
    for seg in ["atomic(<=2)", "near(3-10)", "reaction(>10)"]:
        report(seg, [r for r in rows if r["seg"] == seg])

    reac = [r for r in rows if r["seg"] in ("near(3-10)", "reaction(>10)")]
    print("\n== REACTION (gap>2, НАШ сегмент) по размеру repaid ==")
    for lo, hi, lbl in [(100, 500, "$100-500"), (500, 2000, "$500-2k"), (2000, 1e12, "$2k+")]:
        report(lbl, [r for r in reac if lo <= r["repaid"] < hi])

    print(f"\n==== ВЫВОД ====")
    if reac:
        pos = [r for r in reac if r["net"] > 0]
        print(f"  reaction-сегмент: {len(pos)}/{len(reac)} ({len(pos)/len(reac)*100:.0f}%) плюсовые ДО слиппеджа, "
              f"net-сумма ${sum(r['net'] for r in reac):,.0f}/{DAYS:.0f}д (ВЕРХНЯЯ граница).")
        print("  Минус слиппедж (см. competition_report) и минус стоимость проигранных гонок -> реальное ниже.")
        print("  Высокий %% плюсовых + приличный median net -> сегмент окупается, узкий горячий билд оправдан.")
        print("  Если плюсовых мало / net тонкий -> победители берут ради объёма у нуля, вход не окупается.")
    print("  (read-only ретро: ноль ожидания/отправки, бот не тронут.)")


if __name__ == "__main__":
    main()
