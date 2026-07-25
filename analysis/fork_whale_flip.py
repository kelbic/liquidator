"""ФОРК-ЭКЗАМЕН кит-флипа (25.07, замечание ревьювера): 54 живые пробы ревертились ВНУТРИ
Morpho.liquidate — ДО свопа. Значит боевой размер свопа, minProfit-гейт на РЕАЛЬНОМ выходе и
фактический consumed-газ никогда не исполнялись on-chain (были только quoter-оценки).

Прогон: anvil-форк Base -> подменяем КОД оракула рынка на константу «цена флипа» ->
гоняем БОЕВОЙ prepare-путь (direct_swap и Kyber, оба) -> отправляем tx -> смотрим
status / фактический gasUsed / РЕАЛЬНЫЙ прирост USDC. Read-only для мейннета.

ЗАЩИТА: перед любой отправкой скрипт обязан подтвердить, что RPC — локальный anvil
(anvil_nodeInfo отвечает). Иначе выход с ошибкой: на мейннете это была бы боевая ликвидация.
"""
import sys, os, json, time

sys.path.insert(0, "/root/liquidator")
from config import Config
from chain.rpc import BaseRpc
from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
                             encode_market_call, decode_market, encode_price_call, decode_price,
                             encode_position_call, decode_position)
from chain.simulate import to_assets_up
from strategy.pnl import lif_from_lltv
from chain.execute import (direct_swap, kyber_swap, encode_liquidate, expected_seized,
                           simulate_tx, account_for, send_tx)

ANVIL = os.environ["ANVIL_URL"]
MARKET = os.environ.get("FORK_MARKET", "0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836")
BORROWER = os.environ.get("FORK_BORROWER", "0x95ce58c5A3E9e00f2b8B5b7DB58b8f9C8FCA1D01")
DROP = float(os.environ.get("FORK_DROP", "0.10"))     # насколько ронять цену залога (0.10 = -10%)
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

cfg = Config.from_env()
rpc = BaseRpc(ANVIL, cfg.chain_id)
w3 = rpc._web3()

# --- ЗАЩИТА: только локальный anvil ---
try:
    w3.provider.make_request("anvil_nodeInfo", [])["result"]
except Exception as e:
    print("СТОП: RPC не отвечает на anvil_nodeInfo — это НЕ форк. Отправка запрещена. (%s)" % type(e).__name__)
    sys.exit(2)
print("форк подтверждён (anvil), block=%d" % w3.eth.block_number)

liq = cfg.liquidator_address
bot = account_for(w3, cfg.wallet_key).address
mid = rpc.to_bytes32(MARKET)
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

r1 = aggregate3(rpc, [(MORPHO, encode_id_to_market_params_call(mid)),
                      (MORPHO, encode_market_call(mid)),
                      (MORPHO, encode_position_call(mid, BORROWER))])
loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
mk = decode_market(r1[1][1]); tba, tbs = mk[2], mk[3]
bs, col = decode_position(r1[2][1])
px0 = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
if len(w3.eth.get_code(w3.to_checksum_address(oracle))) < 100:      # заглушка с прошлого прогона
    print("СТОП: оракул на этом форке УЖЕ подменён (codesize=%d) — цена не настоящая."
          " Пересоздай форк." % len(w3.eth.get_code(w3.to_checksum_address(oracle))))
    sys.exit(4)
repaid_assets0 = to_assets_up(int(bs), tba, tbs)
print("рынок %s/%s lltv=%.1f%% | долг=%.0f USDC | залог=%.4f cbBTC | oracle=%s" % (
    coll, loan, lltv_wad / 1e16, repaid_assets0 / 1e6, col / 1e8, oracle))

# --- подмена оракула: код-заглушка, возвращающая константу на ЛЮБОЙ вызов ---
px_flip = int(px0 * (1.0 - DROP))
stub = "0x7f" + hex(px_flip)[2:].rjust(64, "0") + "600052" + "6020" + "6000" + "f3"
w3.provider.make_request("anvil_setCode", [w3.to_checksum_address(oracle), stub])
px_new = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
_hf = lambda p: (col * p // 10**36) * (lltv_wad / 1e18) / repaid_assets0   # обе части в единицах loan-токена
hf_before, hf_after = _hf(px0), _hf(px_new)
print("цена: %d -> %d (-%.0f%%) | HF: %.4f -> %.4f %s" % (
    px0, px_new, DROP * 100, hf_before, hf_after,
    "ФЛИП (ликвидируем)" if hf_after < 1 else "НЕ флипнулся — увеличь FORK_DROP"))
if hf_after >= 1:
    sys.exit(3)

w3.provider.make_request("anvil_setBalance", [bot, hex(10 ** 18)])

# --- боевой сайзинг (тот же код, что в бою) ---
repaid_shares = int(bs)
repaid_assets = to_assets_up(repaid_shares, tba, tbs)
seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10 ** 18), px_new)
lif = lif_from_lltv(lltv_wad / 10 ** 18)
print("сайзинг: repaid=%.0f USDC | seized=%.4f cbBTC | LIF=%.4f (бонус %.2f%%)" % (
    repaid_assets / 1e6, seized / 1e8, lif, (lif - 1) * 100))

mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
erc20_bal = lambda tok, who: int(w3.eth.call({"to": w3.to_checksum_address(tok),
    "data": "0x70a08231" + who[2:].rjust(64, "0")}).hex() or "0", 16)


def run(tag, swap):
    if swap is None:
        print("\n[%s] свопа нет — путь недоступен" % tag)
        return
    cd0 = encode_liquidate(mp, BORROWER, repaid_shares, swap["router"], swap["calldata"], 0)
    sim = simulate_tx(rpc, liq, bot, cd0)
    print("\n[%s] router=%s" % (tag, swap["router"]))
    if not sim["ok"]:
        print("   SIM REVERT: %s" % str(sim["error"])[:200])
        return
    profit_wei = sim["profit"]
    print("   sim profit = %.4f loan-токена" % (profit_wei / 10 ** int(
        w3.eth.call({"to": w3.to_checksum_address(loan), "data": "0x313ce567"}).hex() or "12", 16)))
    min_profit = profit_wei * 95 // 100                       # боевой гейт: >=95% симуляции
    cd1 = encode_liquidate(mp, BORROWER, repaid_shares, swap["router"], swap["calldata"], min_profit)
    _dec = int(w3.eth.call({"to": w3.to_checksum_address(loan), "data": "0x313ce567"}).hex() or "12", 16)
    b_liq0, b_bot0 = erc20_bal(loan, liq), erc20_bal(loan, bot)
    t0 = time.time()
    try:
        res = send_tx(rpc, cfg.wallet_key, {"to": liq, "data": cd1}, min_tip_wei=int(0.05 * 1e9))
    except Exception as e:
        print("   ОТПРАВКА УПАЛА: %s %s" % (type(e).__name__, str(e)[:160]))
        return
    rc = w3.eth.get_transaction_receipt(res["hash"])
    d_liq = erc20_bal(loan, liq) - b_liq0
    d_bot = erc20_bal(loan, bot) - b_bot0
    _u = 10 ** _dec
    gas_usd = int(rc["gasUsed"]) * int(rc.get("effectiveGasPrice") or 0) * cfg.eth_price_usd / 1e18
    print("   ИСПОЛНЕНО: status=%s gasUsed=%d (лимит-оценка %d) газ=$%.3f за %.1fс" % (
        rc["status"], rc["gasUsed"], cfg.gas_limit_est, gas_usd, time.time() - t0))
    print("   ФАКТ %s: контракт %+.4f | кошелёк %+.4f | minProfit-гейт стоял на %.4f" % (
        "loan-токен", d_liq / _u, d_bot / _u, min_profit / _u))
    print("   ВЕРДИКТ: %s" % ("ПРОШЁЛ — приз реальный" if rc["status"] == 1 and (d_liq + d_bot) > 0
                              else "ПРОВАЛ на боевом размере"))


run("direct cbBTC->USDC (fast-lane роут)", direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets))
try:
    run("Kyber-агрегат (мега-путь)", kyber_swap(coll, loan, seized, liq, liq, slippage_bps=100))
except Exception as e:
    print("\n[Kyber] недоступен: %s %s" % (type(e).__name__, str(e)[:120]))
