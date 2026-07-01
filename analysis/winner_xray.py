"""Read-only x-ray: how do WINNERS liquidate on our markets, and why did WE lose? (v2)
A: replay our last sent tx on block-1 + read the borrower's position NOW (race vs self-heal).
B: fingerprint winners' liquidation txs -> Pyth bundle (pull) vs Chainlink in-tx vs clean.
Read-only: eth_call/get_tx only, no key, no tx, does not touch the bot.
    python -m analysis.winner_xray
"""
from __future__ import annotations
import sys

HOURS = 48
MIN_REPAID_USD = 100.0
MAX_WINNER_TX = 8
TOPIC_LIQUIDATE = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _h(x):
    s = x.hex() if hasattr(x, "hex") else str(x)
    s = s.lower()
    return s if s.startswith("0x") else "0x" + s


def _sel(inp):
    h = inp.hex() if hasattr(inp, "hex") else str(inp)
    h = h[2:] if h.startswith("0x") else h
    return "0x" + h[:8]


def main():
    import json, time, urllib.request, urllib.error, sqlite3
    from config import Config
    from chain.rpc import BaseRpc
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import read_health
    from strategy.scanner import load_covered_markets

    cfg = Config.from_env()
    if not cfg.rpc_url:
        sys.exit("RPC_URL not set (source .env first).")
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id); w3 = rpc._web3()
    TOPIC_PYTH = _h(w3.keccak(text="PriceFeedUpdate(bytes32,uint64,int64,uint64)"))
    TOPIC_CL = _h(w3.keccak(text="AnswerUpdated(int256,uint256,uint256)"))

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower() for m in markets}
    ours = {a.lower() for a in (cfg.liquidator_address, cfg.wallet_address) if a}
    since = int(time.time()) - HOURS * 3600

    # ---------------- PART A ----------------
    print("==== A. НАШ последний ОТПРАВЛЕННЫЙ tx ====")
    con = sqlite3.connect(cfg.db_path)
    r = con.execute("SELECT ts,market_id,borrower,tx_hash,net_usd,status FROM actions "
                    "WHERE tx_hash IS NOT NULL AND tx_hash<>'' ORDER BY ts DESC LIMIT 1").fetchone()
    con.close()
    if not r:
        print("  нет actions с tx_hash")
    else:
        ts, mid, borrower, txh, net, status = r
        txh = txh if str(txh).startswith("0x") else "0x" + str(txh)
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))}  borrower {borrower}  net=${net}")
        try:
            rc = w3.eth.get_transaction_receipt(txh); tx = w3.eth.get_transaction(txh)
            print(f"  onchain: status={rc['status']} block={rc['blockNumber']} gasUsed={rc['gasUsed']} nonce={tx['nonce']}")
            try:
                w3.eth.call({"to": tx["to"], "from": tx["from"], "data": tx["input"],
                             "value": tx.get("value", 0)}, rc["blockNumber"] - 1)
                print("  replay block-1: НЕ реверит (позиция была ликвидируема до блока)")
            except Exception as e:
                print(f"  replay block-1: {str(e)[:90]} (здорова уже к нашему блоку — резолвим ниже)")
        except Exception as e:
            print(f"  tx fetch err: {type(e).__name__}: {str(e)[:120]}")
        # позиция СЕЙЧАС: гонка (опустошена) vs само-излечение (жива+здорова)
        try:
            hr = read_health(rpc, MORPHO_BLUE_ADDRESS, mid, borrower)
            print(f"  позиция СЕЙЧАС: debt_assets={hr.borrowed_assets} collateral={hr.collateral} "
                  f"HF={hr.hf:.4f} liquidatable={hr.liquidatable}")
            if hr.borrowed_assets == 0 and hr.collateral == 0:
                print("  -> ОПУСТОШЕНА = ликвидировали (кто-то). Похоже на проигранную ГОНКУ.")
            elif not hr.liquidatable:
                print("  -> ЖИВА и ЗДОРОВА = к нашей отправке флипнула healthy (HF был 0.9951, маргинал/само-излечение)")
                print("     -> это НЕ чистая гонка, а выстрел по позиции на самом пороге. Урок: буфер на HF / latency детект->отправка.")
            else:
                print("  -> ЖИВА и ВСЁ ЕЩЁ ликвидируема (наш реверт был транзиентным; перечитать причину)")
            print("  (caveat: 'сейчас' != 'на нашем блоке' — если её взяли ПОЗЖЕ, опустошение могло прийти потом)")
        except Exception as e:
            print(f"  pos-now err: {type(e).__name__}: {str(e)[:120]}")

    # ---------------- PART B ----------------
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
        print(f"  [field '{cand}' отвергнут]: {str(d.get('errors'))[:160]}")
    if field is None:
        print("\n[B] не нашёл tx-hash поле — см. подсказку Morpho в ошибках выше, скинь мне."); return
    items = (((data.get("data") or {}).get("marketTransactions") or {}).get("items")) or []

    winners = []
    for it in items:
        if int(it["timestamp"]) < since:
            break
        mk = it.get("market") or {}
        if (mk.get("marketId") or "").lower() not in by_id:
            continue
        dd = it.get("data") or {}; la = mk.get("loanAsset") or {}
        liq = (dd.get("liquidator") or "").lower()
        if liq in ours:
            continue
        repaid = int(dd.get("repaidAssets") or 0) / 10 ** int(la.get("decimals") or 18)
        if repaid < MIN_REPAID_USD:
            continue
        winners.append({"txh": it.get(field), "liq": liq, "repaid": repaid,
                        "pair": f"{(mk.get('collateralAsset') or {}).get('symbol','?')}/{la.get('symbol','?')}"})
    winners.sort(key=lambda x: -x["repaid"]); winners = winners[:MAX_WINNER_TX]

    print(f"\n==== B. ВСКРЫТИЕ tx ПОБЕДИТЕЛЕЙ ({HOURS}ч, repaid>=${MIN_REPAID_USD:.0f}, поле='{field}') ====")
    print(f"  topic Pyth={TOPIC_PYTH[:14]}…  Chainlink={TOPIC_CL[:14]}…")
    tally = {"pyth": 0, "chainlink": 0, "clean": 0}
    for wdat in winners:
        txh = wdat["txh"]
        if not txh:
            continue
        txh = txh if str(txh).startswith("0x") else "0x" + str(txh)
        try:
            rc = w3.eth.get_transaction_receipt(txh); tx = w3.eth.get_transaction(txh)
        except Exception as e:
            print(f"\n  {wdat['pair']} {txh[:12]}… fetch err {type(e).__name__}"); continue
        topics = [_h(l["topics"][0]) for l in rc["logs"] if l["topics"]]
        addr_ct = {}
        for l in rc["logs"]:
            a = l["address"].lower(); addr_ct[a] = addr_ct.get(a, 0) + 1
        has_pyth = TOPIC_PYTH in topics; has_cl = TOPIC_CL in topics
        key = "pyth" if has_pyth else ("chainlink" if has_cl else "clean")
        tally[key] += 1
        verdict = {"pyth": "PYTH bundled (pull, feed-driven)",
                   "chainlink": "CHAINLINK update in-tx (push)",
                   "clean": "clean liquidate (no in-tx oracle update)"}[key]
        print(f"\n  {wdat['pair']:<14} repaid ${wdat['repaid']:.0f}  by {wdat['liq'][:12]}…")
        print(f"    to={tx['to']} sel={_sel(tx['input'])} gasUsed={rc['gasUsed']} logs={len(rc['logs'])} "
              f"liquidateEvt={TOPIC_LIQUIDATE in topics}")
        print(f"    -> {verdict}")
        for a, c in sorted(addr_ct.items(), key=lambda kv: -kv[1])[:6]:
            t0 = next((_h(l["topics"][0]) for l in rc["logs"] if l["address"].lower() == a and l["topics"]), "")
            tag = ("Morpho.Liquidate" if t0 == TOPIC_LIQUIDATE else "Pyth" if t0 == TOPIC_PYTH else
                   "Chainlink" if t0 == TOPIC_CL else "token.Transfer" if t0 == TOPIC_TRANSFER else "?")
            print(f"       {a} x{c} {tag}")

    print(f"\n  ИТОГ: pyth={tally['pyth']}  chainlink={tally['chainlink']}  clean={tally['clean']}  (из {len(winners)})")
    print("  Pyth доминирует -> Hermes-driven: updatePriceFeeds+liquidate. Chainlink/clean -> push: ловить pending transmit.")
    print("  '?' адреса сверху — оракул/роутер; вбей в Basescan если неясно.")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
