"""RUNBOOK ДОЗАПРАВКИ (25.07): приз приходит в WETH/USDC, а газ ест ETH — после первой победы
резерв сам не восстановится. Скрипт готовится ЗАРАНЕЕ, чтобы в каскадный день не искать способ.

ПО УМОЛЧАНИЮ — ТОЛЬКО ПЛАН (dry-run): печатает балансы, маршрут и суммы, НИЧЕГО не отправляет.
Исполнение — осознанно: `--execute` (и только владельцем; агент сам не запускает).

Пути:
  WETH -> ETH   : withdraw(uint256) на 0x4200…0006 — 1:1, без слиппажа, ~30k газа. ПРЕДПОЧТИТЕЛЬНО.
  USDC -> ETH   : UniV3 SwapRouter02 exactInputSingle USDC->WETH (fee 500), затем withdraw.

  python analysis/refuel_eth.py                 # план на дефолтную цель 0.3 ETH
  python analysis/refuel_eth.py --target 0.45   # план под полный флот (inflight=5)
  python analysis/refuel_eth.py --execute       # ИСПОЛНИТЬ (подтверждение внутри)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from chain.rpc import BaseRpc
from chain.execute import account_for, send_tx

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
UNIV3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
SEL_BALANCE = "0x70a08231"
SEL_WITHDRAW = "0x2e1a7d4d"          # withdraw(uint256) на WETH
SEL_EXACT_IN_SINGLE = "0x04e45aaf"   # exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))


def _bal_erc20(w3, token, who):
    data = SEL_BALANCE + who[2:].rjust(64, "0")
    return int(w3.eth.call({"to": w3.to_checksum_address(token), "data": data}).hex() or "0", 16)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=0.30, help="целевой баланс ETH")
    ap.add_argument("--execute", action="store_true", help="ИСПОЛНИТЬ (иначе только план)")
    a = ap.parse_args()

    cfg = Config.from_env()
    rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)
    w3 = rpc._web3()
    me = account_for(w3, cfg.wallet_key).address

    eth = int(w3.eth.get_balance(me))
    weth = _bal_erc20(w3, WETH, me)
    usdc = _bal_erc20(w3, USDC, me)
    need = max(0, int(a.target * 10 ** 18) - eth)
    print("кошелёк %s" % me)
    print("  ETH   %.6f  (цель %.3f -> нужно добрать %.6f)" % (eth / 1e18, a.target, need / 1e18))
    print("  WETH  %.6f" % (weth / 1e18))
    print("  USDC  %.2f" % (usdc / 1e6))
    if need == 0:
        print("ЦЕЛЬ УЖЕ ДОСТИГНУТА — дозаправка не нужна")
        return

    steps = []
    from_weth = min(weth, need)
    if from_weth > 0:
        steps.append(("WETH->ETH withdraw", from_weth, "1:1, без слиппажа"))
    left = need - from_weth
    if left > 0:
        # оценка через прямой пул fee=500 (тот же, что у боевого выхода cbBTC->USDC)
        usdc_needed = None
        try:
            from eth_abi import encode as abi_encode, decode as abi_decode
            from eth_utils import keccak
            q = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"           # QuoterV2
            sel = keccak(text="quoteExactOutputSingle((address,address,uint256,uint24,uint160))")[:4]
            data = sel + abi_encode(["(address,address,uint256,uint24,uint160)"],
                                    [(USDC, WETH, left, 500, 0)])
            usdc_needed = abi_decode(["uint256", "uint160", "uint32", "uint256"],
                                     w3.eth.call({"to": q, "data": data}))[0]
        except Exception as e:
            print("  (кво́тер недоступен: %s — оцени вручную)" % type(e).__name__)
        steps.append(("USDC->WETH->ETH", left,
                      "нужно ~%.2f USDC" % (usdc_needed / 1e6) if usdc_needed else "оценить"))

    print("\nПЛАН:")
    for i, (what, amt, note) in enumerate(steps, 1):
        print("  %d) %s: %.6f ETH-эквивалента (%s)" % (i, what, amt / 1e18, note))
    if not a.execute:
        print("\nDRY-RUN. Для исполнения: --execute (осознанно, владельцем).")
        return

    if input("\nИСПОЛНИТЬ? введи 'yes': ").strip().lower() != "yes":
        print("отменено")
        return
    if from_weth > 0:
        data = SEL_WITHDRAW + hex(from_weth)[2:].rjust(64, "0")
        res = send_tx(rpc, cfg.wallet_key, {"to": w3.to_checksum_address(WETH), "data": data},
                      min_tip_wei=int(0.05 * 1e9))
        print("WETH->ETH отправлено: %s status=%s" % (res["hash"], res.get("status")))
    if need - from_weth > 0:
        print("USDC-нога: исполнить вручную через роутер %s (exactOutputSingle USDC->WETH fee=500), "
              "затем повторить этот скрипт для withdraw" % UNIV3_ROUTER)


if __name__ == "__main__":
    main()
