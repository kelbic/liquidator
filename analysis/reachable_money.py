"""Read-only: reachable-$ sweep — convert the latency gate (G_ours in tx-positions) into DOLLARS.
For won liquidations we have (gap, repaid). 'Reachable at G' = repaid where gap > G (we'd land before
that winner at equal fee). Sweep G over a latency ladder so the money reachable NOW (G=55, measured
cold) vs after cutting the quote (G~37 warm, G~20 on-chain quote) is sized in $, deciding whether the
latency engineering pays.

UPPER bound (same caveats as gap_profile/latency_probe): a winner's gap is how late they WERE, not
their floor; assumes equal fee priority. Read-only: historical receipts + single-block getLogs +
Morpho API. No key, no tx, no bot touch.
    DAYS=30 MAX_WINNERS=200 G_OURS=55  python -m analysis.reachable_money
"""
from __future__ import annotations
import os
import sys

DAYS = float(os.environ.get("DAYS", "30"))
MIN_REPAID_USD = float(os.environ.get("MIN_REPAID_USD", "100"))
MAX_WINNERS = int(os.environ.get("MAX_WINNERS", "200"))
G_OURS = float(os.environ.get("G_OURS", "55"))            # current measured (latency_probe)
SWEEP = [80, 55, 46, 40, 30, 20, 10]                       # slow -> fast (55=now, 20=on-chain-quote target)
TARGET_USD = 2000.0                                        # our segment floor ($2k+)


def main():
    sys.path.insert(0, ".")
    import json, time, urllib.request, urllib.error
    from collections import defaultdict
    from web3 import Web3
    from config import Config
    from chain.rpc import BaseRpc
    from chain.feeds import _resolve_one
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    T = Web3.keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
    TOPIC_CL = T if T.startswith("0x") else "0x" + T

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

    agg_cache, rows = {}, []
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
        if w["mid"] not in agg_cache:
            agg_cache[w["mid"]] = _resolve_one(rpc, w["mid"])
        agg = agg_cache[w["mid"]]
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
        rows.append({"pair": w["pair"], "repaid": w["repaid"], "gap": gap})

    # reaction universe = gap>2 (exclude atomic/bundle, which we don't contest)
    reac = [r for r in rows if r["gap"] is not None and r["gap"] > 2]
    reac_money = sum(r["repaid"] for r in reac) or 1.0
    reac_2k = [r for r in reac if r["repaid"] >= TARGET_USD]
    reac_2k_money = sum(r["repaid"] for r in reac_2k) or 1.0
    print(f"==== REACHABLE-$ sweep ({DAYS:.0f}д, {len(rows)} ликв обработано) ====")
    print(f"  reaction-universe (gap>2): ${reac_money:,.0f} / {len(reac)} ликв ; из них $2k+: ${reac_2k_money:,.0f} / {len(reac_2k)}\n")

    print("== reachable-$ по порогу латентности (gap > G => мы бы сели первыми; ВЕРХНЯЯ граница) ==")
    print(f"  {'G (tx-pos)':<12}{'reachable $':>14}{'% reac':>9}{'  | $2k+ only':>16}{'% reac2k':>10}")
    for G in sorted(set(SWEEP) | {int(G_OURS)}, reverse=True):
        rch = [r for r in reac if r["gap"] > G]
        rch_m = sum(r["repaid"] for r in rch)
        rch2 = [r for r in rch if r["repaid"] >= TARGET_USD]
        rch2_m = sum(r["repaid"] for r in rch2)
        mark = "  <- NOW (measured)" if G == int(G_OURS) else ("  <- warm pipeline" if G == 37 else ("  <- on-chain quote" if G == 20 else ""))
        print(f"  {G:<12}${rch_m:>12,.0f}{rch_m/reac_money*100:>8.0f}%${rch2_m:>14,.0f}{rch2_m/reac_2k_money*100:>9.0f}%{mark}")

    print(f"\n== при G_ours={int(G_OURS)} (сейчас) — по рынкам, что достижимо ==")
    by_pair = defaultdict(lambda: [0.0, 0, 0.0, 0])   # pair -> [reach$, reach_n, total$, total_n]
    for r in reac:
        p = by_pair[r["pair"]]
        p[2] += r["repaid"]; p[3] += 1
        if r["gap"] > G_OURS:
            p[0] += r["repaid"]; p[1] += 1
    for pair, (rm, rn, tm, tn) in sorted(by_pair.items(), key=lambda kv: -kv[1][2]):
        print(f"  {pair:<14} reachable ${rm:>11,.0f} / {rn:>3} из ${tm:>11,.0f} / {tn:>3}  ({rm/tm*100 if tm else 0:>3.0f}% денег рынка)")

    print(f"\n==== ВЫВОД ====")
    now = sum(r["repaid"] for r in reac if r["gap"] > G_OURS)
    warm = sum(r["repaid"] for r in reac if r["gap"] > 37)
    fast = sum(r["repaid"] for r in reac if r["gap"] > 20)
    print(f"  СЕЙЧАС (G=55, холодный):     ${now:,.0f}/{DAYS:.0f}д достижимо (верхняя граница).")
    print(f"  ТЁПЛЫЙ пайплайн (G~37):      ${warm:,.0f}  (+${warm-now:,.0f} за keep-alive + склейку multicall).")
    print(f"  ON-CHAIN котировка (G~20):   ${fast:,.0f}  (+${fast-warm:,.0f} сверху за локальный quote).")
    print("  Если дельта тёплый/on-chain жирная -> режем латентность (это код, дёшево), потом live paper-доля.")
    print("  Если даже G=20 тонкий -> главные призы структурно быстрее нас, решение про co-located узел/разворот.")
    print("  (NB: верхняя граница — gap победителя это как поздно он был, не его пол; под нашим давлением жмётся.)")


if __name__ == "__main__":
    main()
