"""ФОРК-ЭКЗАМЕН БАТЧА (25.07, флаг ревьювера #1): одиночки валидированы (2.17-2.47M газа),
но каскадный день = batchLiquidate по 3-5 позиций, и это ТРЕТИЙ путь лимита газа.
Прогон: форк -> оракул cbBTC-рынка на цену флипа -> берём N жирнейших флипнувшихся ->
собираем БАТЧ боевым энкодером -> отправляем -> сверяем ФАКТИЧЕСКИЙ gasUsed с нашей формулой
batch_gas_limit() и считаем резерв limit×maxFee против реального баланса кошелька.
ЗАЩИТА: без ответа anvil_nodeInfo — отказ (на мейннете это была бы боевая ликвидация)."""
import sys, os, time

sys.path.insert(0, "/root/liquidator")
from config import Config
from chain.rpc import BaseRpc
from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
                             encode_market_call, decode_market, encode_price_call, decode_price,
                             encode_position_call, decode_position)
from chain.simulate import to_assets_up
from strategy.pnl import lif_from_lltv
from chain.execute import (direct_swap, kyber_swap, encode_liquidate, encode_batch_liquidate,
                           expected_seized, simulate_tx, account_for, send_tx, batch_gas_limit,
                           clamp_fee_to_balance)

ANVIL = os.environ["ANVIL_URL"]
MARKET = "0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836"   # cbBTC/USDC
DROP = float(os.environ.get("FORK_DROP", "0.12"))
BORROWERS = [b for b in os.environ.get("FORK_BORROWERS", "").split(",") if b]
REAL_BAL = int(float(os.environ.get("REAL_BALANCE_ETH", "0.094")) * 10 ** 18)

cfg = Config.from_env()
rpc = BaseRpc(ANVIL, cfg.chain_id)
w3 = rpc._web3()
try:
    w3.provider.make_request("anvil_nodeInfo", [])["result"]
except Exception:
    print("СТОП: это не форк — отправка запрещена")
    sys.exit(2)

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
liq, bot = cfg.liquidator_address, account_for(w3, cfg.wallet_key).address
mid = rpc.to_bytes32(MARKET)
r1 = aggregate3(rpc, [(MORPHO, encode_id_to_market_params_call(mid)), (MORPHO, encode_market_call(mid))])
loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
if len(w3.eth.get_code(w3.to_checksum_address(oracle))) < 100:
    print("СТОП: оракул уже подменён на этом форке — пересоздай")
    sys.exit(4)
px0 = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])
px_flip = int(px0 * (1.0 - DROP))
stub = "0x7f" + hex(px_flip)[2:].rjust(64, "0") + "600052" + "6020" + "6000" + "f3"
w3.provider.make_request("anvil_setCode", [w3.to_checksum_address(oracle), stub])
w3.provider.make_request("anvil_setBalance", [bot, hex(10 ** 18)])
print("оракул %s: цена -%.0f%% (флип-сценарий каскада)" % (oracle[:10], DROP * 100))

mk = decode_market(r1[1][1]); tba, tbs = mk[2], mk[3]
mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
lif = lif_from_lltv(lltv_wad / 10 ** 18)

preps = []
for b in BORROWERS:
    res = aggregate3(rpc, [(MORPHO, encode_position_call(mid, b))])
    bs, col = decode_position(res[0][1])
    if bs == 0:
        print("  %s: долга нет — мимо" % b[:10]); continue
    ra = to_assets_up(int(bs), tba, tbs)
    hf = (col * px_flip // 10 ** 36) * (lltv_wad / 1e18) / ra
    if hf >= 1:
        print("  %s: HF=%.4f не флипнулся при -%.0f%% — мимо" % (b[:10], hf, DROP * 100)); continue
    seized = expected_seized(ra, lif, px_flip)
    swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=ra)
    src = "direct"
    if swap is None:
        swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=100); src = "kyber"
    cd0 = encode_liquidate(mp, b, int(bs), swap["router"], swap["calldata"], 0)
    sim = simulate_tx(rpc, liq, bot, cd0)
    if not sim["ok"]:
        print("  %s: sim revert %s" % (b[:10], str(sim["error"])[:70])); continue
    print("  %s: HF=%.4f repaid=%.0f USDC seized=%.3f cbBTC via %s | sim profit=%.0f USDC" % (
        b[:10], hf, ra / 1e6, seized / 1e8, src, sim["profit"] / 1e6))
    preps.append({"mp": mp, "borrower": b, "repaid_shares": int(bs), "swap_target": swap["router"],
                  "swap_calldata": swap["calldata"], "min_profit": sim["profit"] * 95 // 100,
                  "gas": None, "net_usd": sim["profit"] / 1e6})

if len(preps) < 2:
    print("ФЛИПНУЛОСЬ <2 позиций — батч не собрать, увеличь FORK_DROP")
    sys.exit(3)

# ЛИМИТ по нашей боевой формуле (у prep'ов gas=None -> фолбэк floor, худший случай)
limit_formula = batch_gas_limit(preps, cfg)
data = encode_batch_liquidate(preps)
print("\nБАТЧ n=%d | наша формула лимита: %.2fM (фолбэк floor, estimate не отвечал)" % (
    len(preps), limit_formula / 1e6))

erc20 = lambda tok, who: int(w3.eth.call({"to": w3.to_checksum_address(tok),
    "data": "0x70a08231" + who[2:].rjust(64, "0")}).hex() or "0", 16)
b0 = erc20(loan, bot) + erc20(loan, liq)
t = {"to": liq, "data": data, "gas": limit_formula}
res = send_tx(rpc, cfg.wallet_key, t, min_tip_wei=int(0.05 * 1e9))
rc = w3.eth.get_transaction_receipt(res["hash"])
used = int(rc["gasUsed"])
got = erc20(loan, bot) + erc20(loan, liq) - b0
print("ИСПОЛНЕНО: status=%s | ФАКТИЧЕСКИЙ gasUsed=%.2fM | формула/факт = %.2fx | приз %+.0f USDC" % (
    rc["status"], used / 1e6, limit_formula / used if used else 0, got / 1e6))
print("ВЕРДИКТ ЛИМИТА: %s" % ("ОК — формула покрывает факт с запасом" if limit_formula >= used
                              else "ПРОВАЛ — формула НИЖЕ факта (был бы out-of-gas)"))

# резерв против РЕАЛЬНОГО баланса кошелька при каскадном tip
for tip_gwei in (0.05, 2, 30):
    mf = int((tip_gwei + 0.02) * 1e9)
    _mf, _tp, ok = clamp_fee_to_balance(limit_formula, mf, int(tip_gwei * 1e9), REAL_BAL,
                                        min_tip_wei=int(0.05 * 1e9))
    need = limit_formula * mf / 1e18
    print("  tip %5.2f gwei -> резерв %.4f ETH при балансе %.3f: %s" % (
        tip_gwei, need, REAL_BAL / 1e18,
        "влезает" if ok and _mf == mf else ("КЛАМП до tip %.2f gwei" % (_tp / 1e9) if ok else "НЕ влезает — хвост дропнется")))
