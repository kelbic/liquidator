"""Read-only RETROSPECTIVE: replay won liquidations to measure the preconf gate on PAST volatility.
For each won liquidation (block B) on our markets, find the in-block oracle update (AnswerUpdated from
the market's OCR aggregator) and compare its tx position to the winner's liquidate:
  (a) update BEFORE winner (U_idx<W_idx) -> preconf signal existed before they acted; at equal start
      we'd have it too -> DETECTION solved on real volatility.
  (b) gap = W_idx-U_idx (txs), ~sub-blocks = 10*gap/block_txs -> how fast the winner reacted = the
      send latency to match/beat.
Limit: says "we'd have SEEN it in time" + "how tight the winner was", NOT "we'd have won".
Read-only: historical receipts + single-block getLogs + Morpho API. No key, no tx, no bot touch.
    HOURS=72 MAX_WINNERS=40 python -m analysis.replay_winners
"""
from __future__ import annotations
import os
import sys

HOURS = float(os.environ.get("HOURS", "72"))
MIN_REPAID_USD = float(os.environ.get("MIN_REPAID_USD", "100"))
MAX_WINNERS = int(os.environ.get("MAX_WINNERS", "40"))


def main():
    sys.path.insert(0, ".")
    import json, time, statistics, urllib.request, urllib.error
    from collections import Counter
    from web3 import Web3
    from config import Config
    from chain.rpc import BaseRpc
    from chain.feeds import _resolve_one
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    TOPIC_CL = Web3.keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
    TOPIC_CL = TOPIC_CL if TOPIC_CL.startswith("0x") else "0x" + TOPIC_CL

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower() for m in markets}
    ours = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    since = int(time.time()) - int(HOURS * 3600)

    def gql(hash_field):
        Q = ("query($f:Int!,$s:Int!,$w:MarketTransactionFilters!){"
             "marketTransactions(first:$f,skip:$s,orderBy:Timestamp,orderDirection:Desc,where:$w){"
             "items{ timestamp " + hash_field +
             " market{ marketId collateralAsset{symbol} loanAsset{symbol decimals} }"
             " data{ ... on MarketTransactionLiquidationData{ liquidator repaidAssets } } } } }")
        body = json.dumps({"query": Q, "variables": {"f": 100, "s": 0,
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

    field, data = None, None
    for cand in ("txHash", "hash"):
        d = gql(cand)
        if not d.get("errors"):
            field, data = cand, d; break
    if field is None:
        sys.exit(f"Morpho API tx-hash поле не найдено: {(d or {}).get('errors')}")
    items = (((data.get("data") or {}).get("marketTransactions") or {}).get("items")) or []

    winners = []
    for it in items:
        if int(it["timestamp"]) < since:
            break
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
    winners = winners[:MAX_WINNERS]
    print(f"==== РЕТРО replay: {len(winners)} выигранных ликвидаций ({HOURS:.0f}ч, repaid>=${MIN_REPAID_USD:.0f}) ====")

    agg_cache, blocktx_cache, rows = {}, {}, []
    for w in winners:
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
        if not agg:
            rows.append((w["pair"], "non-Chainlink-market", None, W_idx, None)); continue
        try:
            logs = w3.eth.get_logs({"fromBlock": B, "toBlock": B,
                                    "address": Web3.to_checksum_address(agg), "topics": [TOPIC_CL]})
            u_idxs = [l["transactionIndex"] for l in logs]
            U_idx = min(u_idxs) if u_idxs else None
        except Exception:
            U_idx = None
        if B not in blocktx_cache:
            try:
                blocktx_cache[B] = len(w3.eth.get_block(B)["transactions"])
            except Exception:
                blocktx_cache[B] = None
        btx = blocktx_cache[B]
        gap = None
        if U_idx is None:
            status, sub = "no-update-in-block", None
        else:
            before = U_idx < W_idx
            gap = W_idx - U_idx
            sub = round(10 * gap / btx, 1) if btx else None
            status = "update-before-winner" if before else "update-after/with-winner"
        rows.append((w["pair"], status, U_idx, W_idx, sub))
        print(f"  {w['pair']:<14} ${w['repaid']:>7.0f} blk {B}  U_idx={U_idx} W_idx={W_idx} "
              f"blockTx={btx}  -> {status}" + (f"  gap={gap}tx ~{sub}под-бл" if gap is not None else ""))

    print(f"\n==== ИТОГ ({len(rows)}) ====")
    st = Counter(r[1] for r in rows)
    for k, v in st.most_common():
        print(f"  {k}: {v}")
    before_rows = [r for r in rows if r[1] == "update-before-winner"]
    subs = [r[4] for r in before_rows if r[4] is not None]
    n_chainlink = sum(v for k, v in st.items() if k != "non-Chainlink-market")
    if before_rows:
        share = len(before_rows) / max(1, n_chainlink) * 100
        print(f"\n  обновление БЫЛО в блоке и ПЕРЕД победителем: {len(before_rows)}/{n_chainlink} "
              f"Chainlink-ликвидаций ({share:.0f}%)")
        if subs:
            subs.sort()
            print(f"  разрыв обновление->liquidate победителя: median ~{statistics.median(subs):.1f} под-блоков "
                  f"(min {min(subs):.1f}, max {max(subs):.1f}) ≈ ~{statistics.median(subs)*200:.0f}мс медиана")
        print(f"\n  ЧТЕНИЕ: в {share:.0f}% реальных ликвидаций pre-confirmed сигнал был ДО действия победителя")
        print(f"  -> при равном старте мы видели бы их в тот же момент = ДЕТЕКЦИЯ решена на реальной воле.")
        print(f"  Медианный разрыв — окно отправки, которое надо закрыть, чтобы быть в гонке (≈200мс/под-блок).")
        print(f"  Маленький (~1 под-блок) = нужна near-co-located скорость; больше = реальное окно для нас.")
    else:
        print("\n  обновлений-перед-победителем не найдено — проверь окно/фиды (часть рынков exchange-rate,")
        print("  не эмитят AnswerUpdated; смотри non-Chainlink-market / no-update-in-block).")
    print("  (read-only ретро по историческим receipt'ам: ноль ожидания, бот не тронут.)")


if __name__ == "__main__":
    main()
