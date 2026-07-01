"""Read-only: profile gap=1 winners + segment 30d liquidation money by ordering gap.
1) atomic(gap<=2) winners — priority fee (effectiveGasPrice-baseFee): LOW => bundle/privileged
   ordering (public send can't win); HIGH => fee war. (Pure fee race would order liquidate BEFORE
   the oracle tx — wrong side — so atomic backrun implies bundle, expect LOW fee.)
2) $ by gap bucket: atomic(<=2)=closed; reaction(>10)=sub-block-later window a public sender can hit.
3) per-market gap: markets without atomic backrun = the niche.
Read-only: historical receipts + single-block getLogs + Morpho API. No key, no tx, no bot touch.
    DAYS=30 MAX_WINNERS=200 python -m analysis.gap_profile
"""
from __future__ import annotations
import os
import sys

DAYS = float(os.environ.get("DAYS", "30"))
MIN_REPAID_USD = float(os.environ.get("MIN_REPAID_USD", "100"))
MAX_WINNERS = int(os.environ.get("MAX_WINNERS", "200"))


def bucket(gap):
    if gap is None:
        return "no-update"
    if gap < 0:
        return "update-after"
    if gap <= 2:
        return "atomic(<=2)"
    if gap <= 10:
        return "near(3-10)"
    return "reaction(>10)"


def main():
    sys.path.insert(0, ".")
    import json, time, statistics, urllib.request, urllib.error
    from collections import Counter, defaultdict
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
            liq = (dd.get("liquidator") or "").lower()
            if liq in ours:
                continue
            repaid = int(dd.get("repaidAssets") or 0) / 10 ** int(la.get("decimals") or 18)
            if repaid < MIN_REPAID_USD:
                continue
            winners.append({"txh": it.get(field), "mid": mid, "liq": liq, "repaid": repaid,
                            "pair": f"{(mk.get('collateralAsset') or {}).get('symbol','?')}/{la.get('symbol','?')}"})
        if stop:
            break
        skip += 100
    winners.sort(key=lambda x: -x["repaid"])
    sample = winners[:MAX_WINNERS]
    print(f"==== GAP-профиль: {len(sample)}/{len(winners)} ликвидаций (top по $, {DAYS:.0f}д) ====")

    agg_cache, blk_cache, rows = {}, {}, []
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
        eff = rc.get("effectiveGasPrice")
        if w["mid"] not in agg_cache:
            agg_cache[w["mid"]] = _resolve_one(rpc, w["mid"])
        agg = agg_cache[w["mid"]]
        if B not in blk_cache:
            try:
                blk = w3.eth.get_block(B)
                blk_cache[B] = (blk.get("baseFeePerGas"), len(blk["transactions"]))
            except Exception:
                blk_cache[B] = (None, None)
        base, _btx = blk_cache[B]
        prio = ((eff - base) / 1e9) if (eff is not None and base is not None) else None
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
        bk = "non-Chainlink" if not agg else bucket(gap)
        rows.append({"pair": w["pair"], "mid": w["mid"], "liq": w["liq"], "repaid": w["repaid"],
                     "gap": gap, "prio": prio, "bucket": bk})

    print(f"  обработано receipt'ов: {len(rows)}\n")
    money = defaultdict(float); cnt = Counter()
    for r in rows:
        money[r["bucket"]] += r["repaid"]; cnt[r["bucket"]] += 1
    total = sum(money.values()) or 1.0
    print("== ДЕНЬГИ ($ repaid) по размеру gap ==")
    for bk in ["atomic(<=2)", "near(3-10)", "reaction(>10)", "update-after", "no-update", "non-Chainlink"]:
        if cnt[bk]:
            print(f"  {bk:<16} {cnt[bk]:>4} шт  ${money[bk]:>12,.0f}  ({money[bk]/total*100:4.1f}% денег)")
    print("\n== PRIORITY FEE (gwei) по bucket -> bundle(низкий) vs fee-гонка(высокий) ==")
    for bk in ["atomic(<=2)", "near(3-10)", "reaction(>10)"]:
        ps = [r["prio"] for r in rows if r["bucket"] == bk and r["prio"] is not None]
        if ps:
            ps.sort()
            print(f"  {bk:<16} median {statistics.median(ps):.3f}  (min {min(ps):.3f} max {max(ps):.3f}, n={len(ps)})")
    print("\n== По РЫНКАМ: медианный gap (atomic везде? или ниша) ==")
    mk_gaps = defaultdict(list); mk_pair = {}
    for r in rows:
        if r["gap"] is not None and r["gap"] >= 0:
            mk_gaps[r["mid"]].append(r["gap"]); mk_pair[r["mid"]] = r["pair"]
    for mid, gs in sorted(mk_gaps.items(), key=lambda kv: len(kv[1]), reverse=True):
        gs.sort(); atom = sum(1 for g in gs if g <= 2)
        print(f"  {mk_pair[mid]:<14} ликв={len(gs):>3} med_gap={statistics.median(gs):>5.1f} atomic={atom}/{len(gs)}")
    print("\n== Топ-ликвидаторы: их gap и priority ==")
    liq_rows = defaultdict(list)
    for r in rows:
        liq_rows[r["liq"]].append(r)
    for liq, rs in sorted(liq_rows.items(), key=lambda kv: -sum(x["repaid"] for x in kv[1]))[:6]:
        gs = [x["gap"] for x in rs if x["gap"] is not None and x["gap"] >= 0]
        ps = [x["prio"] for x in rs if x["prio"] is not None]
        money_liq = sum(x["repaid"] for x in rs)
        gtxt = f"med_gap={statistics.median(gs):.1f}" if gs else "med_gap=n/a"
        ptxt = f"med_prio={statistics.median(ps):.3f}gwei" if ps else "med_prio=n/a"
        atom = sum(1 for g in gs if g <= 2)
        print(f"  {liq[:12]}…  ликв={len(rs)} ${money_liq:>10,.0f}  {gtxt}  atomic={atom}/{len(gs)}  {ptxt}")

    atom_money = money["atomic(<=2)"]; reac_money = money["reaction(>10)"] + money["near(3-10)"]
    print(f"\n==== ВЫВОД ====")
    print(f"  атомарно (gap<=2, закрыто ordering'ом): ${atom_money:,.0f} ({atom_money/total*100:.0f}% денег)")
    print(f"  реакция-через-под-блок (gap>2, окно под публичную отправку): ${reac_money:,.0f} ({reac_money/total*100:.0f}%)")
    print("  Если priority атомиков НИЗКИЙ -> привилегированный ordering, fee не поможет, крупное закрыто.")
    print("  Сегмент под нас = 'reaction' $ + рынки с atomic=0/N. Копеечный -> публичная отправка не")
    print("  окупается, edge только в смене площадки. Заметный -> строим узко под него.")
    print("  (read-only ретро: ноль ожидания, бот не тронут.)")


if __name__ == "__main__":
    main()
