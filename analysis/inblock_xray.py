"""Read-only: WHY do winners win? In-block back-run vs race-on-confirmed.
S1 (decisive, oracle-agnostic): oracle price() at block B vs B-1. Winners use clean liquidate,
status=1 (position unhealthy at exec) -> price@B!=price@B-1 means oracle updated IN block B,
ordered before them -> they read it pre-confirmed = IN-BLOCK BACK-RUN. Equal -> updated earlier,
they raced on confirmed state. S2 (archive-free): Pyth/Chainlink update events in block B before
the winner tx (confirms S1, reveals the feed). Read-only: eth_call/getLogs only, no key, no tx.
    python -m analysis.inblock_xray
"""
from __future__ import annotations
import sys

HOURS = 48
MIN_REPAID_USD = 100.0
MAX_WINNER_TX = 8
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _h(x):
    s = x.hex() if hasattr(x, "hex") else str(x)
    s = s.lower()
    return s if s.startswith("0x") else "0x" + s


def main():
    import json, time, urllib.request, urllib.error
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI, ORACLE_ABI
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    TOPIC_PYTH = _h(w3.keccak(text="PriceFeedUpdate(bytes32,uint64,int64,uint64)"))
    TOPIC_CL = _h(w3.keccak(text="AnswerUpdated(int256,uint256,uint256)"))
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower() for m in markets}
    ours = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    since = int(time.time()) - HOURS * 3600

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
        print("не нашёл tx-hash поле:", (d or {}).get("errors")); return
    items = (((data.get("data") or {}).get("marketTransactions") or {}).get("items")) or []

    winners = []
    for it in items:
        if int(it["timestamp"]) < since:
            break
        mk = it.get("market") or {}
        mid = (mk.get("marketId") or "").lower()
        if mid not in by_id:
            continue
        dd = it.get("data") or {}; la = mk.get("loanAsset") or {}
        liq = (dd.get("liquidator") or "").lower()
        if liq in ours:
            continue
        repaid = int(dd.get("repaidAssets") or 0) / 10 ** int(la.get("decimals") or 18)
        if repaid < MIN_REPAID_USD:
            continue
        winners.append({"txh": it.get(field), "liq": liq, "repaid": repaid, "mid": mid,
                        "pair": f"{(mk.get('collateralAsset') or {}).get('symbol','?')}/{la.get('symbol','?')}"})
    winners.sort(key=lambda x: -x["repaid"]); winners = winners[:MAX_WINNER_TX]

    print(f"==== МЕХАНИКА ПОБЕДЫ: in-block back-run? ({HOURS}ч, repaid>=${MIN_REPAID_USD:.0f}) ====")
    tally = {"backrun": 0, "confirmed": 0, "inconclusive": 0}
    oracle_cache = {}
    for w in winners:
        txh = w["txh"]
        if not txh:
            continue
        txh = txh if str(txh).startswith("0x") else "0x" + str(txh)
        try:
            rc = w3.eth.get_transaction_receipt(txh)
        except Exception as e:
            print(f"\n  {w['pair']} {txh[:12]}… receipt err {type(e).__name__}"); continue
        B = rc["blockNumber"]; widx = rc["transactionIndex"]
        mid_b32 = rpc.to_bytes32(w["mid"])
        oracle = oracle_cache.get(w["mid"])
        if oracle is None:
            try:
                oracle = morpho.functions.idToMarketParams(mid_b32).call()[2]
            except Exception as e:
                oracle = f"err:{type(e).__name__}"
            oracle_cache[w["mid"]] = oracle

        print(f"\n  {w['pair']:<14} repaid ${w['repaid']:.0f}  by {w['liq'][:12]}…  block {B} txIdx {widx}")
        print(f"    oracle(Morpho wrapper)={oracle}")

        s1 = None
        if isinstance(oracle, str) and oracle.startswith("0x"):
            oc = rpc.contract(oracle, ORACLE_ABI)
            try:
                p_b = oc.functions.price().call(block_identifier=B)
                p_bm1 = oc.functions.price().call(block_identifier=B - 1)
                s1 = (p_b != p_bm1)
                print(f"    S1 price@B-1={p_bm1}  price@B={p_b}  changed_in_block={'YES' if s1 else 'no'}")
            except Exception as e:
                print(f"    S1 archive eth_call err: {type(e).__name__}: {str(e)[:80]} "
                      "(нужен archive RPC для исторического price())")
        else:
            print("    S1 пропущен (oracle не прочитан)")

        s2_before = None
        try:
            logs = w3.eth.get_logs({"fromBlock": B, "toBlock": B, "topics": [[TOPIC_PYTH, TOPIC_CL]]})
            before = [(l["transactionIndex"], l["address"].lower(),
                       "Pyth" if _h(l["topics"][0]) == TOPIC_PYTH else "Chainlink")
                      for l in logs if l["transactionIndex"] < widx]
            s2_before = len(before)
            feeds = sorted(set((a, k) for _, a, k in before))
            print(f"    S2 oracle-update событий в блоке B до победителя: {s2_before}"
                  + (f"  фиды: {feeds}" if feeds else ""))
        except Exception as e:
            print(f"    S2 getLogs err: {type(e).__name__}: {str(e)[:80]}")

        if s1 is True or (s2_before or 0) > 0:
            verdict = "IN-BLOCK BACK-RUN (цену обновили в блоке B перед ними; нужен sub-block pre-confirmed ассесс)"
            tally["backrun"] += 1
        elif s1 is False and s2_before == 0:
            verdict = "RACED-ON-CONFIRMED (цена обновлена раньше; хватит чаще поллить подтверждённое)"
            tally["confirmed"] += 1
        else:
            verdict = "INCONCLUSIVE (сигналы не сошлись/ошибки — см. выше)"
            tally["inconclusive"] += 1
        print(f"    -> {verdict}")

    print(f"\n  ИТОГ: back-run={tally['backrun']}  raced-on-confirmed={tally['confirmed']}  "
          f"inconclusive={tally['inconclusive']}  (из {len(winners)})")
    print("  back-run доминирует -> билд: ассесс против ПОСЛЕДНЕГО flashblock pre-confirmed под-блока")
    print("    (~200мс), отправка в том же окне. Переиспользует контракт+dispatch+Kyber; меняется источник")
    print("    цены/каденс детекции. НЕ денежно-контрактный билд.")
    print("  raced-on-confirmed доминирует -> дешевле: чаще поллить подтверждённое, тот же путь.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
