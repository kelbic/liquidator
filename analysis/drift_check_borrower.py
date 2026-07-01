"""Drift-реконструкция HF заёмщика 0x4Ffc5F22... на рынке 0x67ebd84b... назад от блока 48039560 —
тот же метод, что на cbDOGE. Архивные чтения position()+market()+oracle.price() на нескольких
прошлых блоках, HF той же формулой, что боевой путь (chain.simulate.health_from). Различает:
  флип В блоке N (блок-луп физически не мог успеть раньше следующего блока — не баг, цель
  горячего пути) vs флип за НЕСКОЛЬКО блоков ДО N (блок-луп проспал — баг, чинимо).
Read-only, архивные eth_call на confirmed RPC, hot path не трогает.
Запуск:  cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) \
         /root/liquidator/venv/bin/python -m analysis.drift_check_borrower
"""
import sys
sys.path.insert(0, ".")
from config import Config
from chain.rpc import BaseRpc
from chain.multicall import (aggregate3, encode_market_call, decode_market,
                              encode_position_call, decode_position,
                              encode_id_to_market_params_call, decode_id_to_market_params)
from chain.simulate import MarketContext, health_from, ORACLE_ABI
from chain.hotpath import MORPHO_BLUE

cfg = Config.from_env()
rpc = BaseRpc(cfg.rpc_url, cfg.chain_id)

MARKET_ID = "0x67ebd84b2fb39e3bc5a13d97e4c07abe1ea617e40654826e9abce252e95f049e"
BORROWER = "0x4Ffc5F22770ab6046c8D66DABAe3A9CD1E7A03e7"
N = 48039560  # блок, где конкурент ликвидировал (borrower_liq_check.py)

b32 = rpc.to_bytes32(MARKET_ID)

params_res = aggregate3(rpc, [(MORPHO_BLUE, encode_id_to_market_params_call(b32))])
if not params_res[0][0]:
    print("Не удалось прочитать market params — рынок/id проверить вручную")
    sys.exit(1)
loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(params_res[0][1])
print(f"Рынок: loan={loan} coll={coll} oracle={oracle} lltv={lltv_wad/1e18:.3f}\n")

offsets = [0, 1, 5, 15, 30, 60, 150, 300]  # блоков назад от N (~2с/блок на Base)
print(f"{'block':>10} {'~сек назад':>11} {'HF':>10} {'liquidatable':>13}")
for off in offsets:
    blk = N - off
    try:
        res = aggregate3(rpc, [(MORPHO_BLUE, encode_market_call(b32)),
                               (MORPHO_BLUE, encode_position_call(b32, BORROWER))],
                         block_identifier=blk)
        if not res[0][0] or not res[1][0]:
            print(f"{blk:>10} {off*2:>10}с  чтение не удалось (ok=False на этом блоке)")
            continue
        m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
        bs, col = decode_position(res[1][1])
        price = rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier=blk)
        ctx = MarketContext(oracle=oracle, price=int(price), lltv_wad=int(lltv_wad),
                            total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
        hr = health_from(ctx, int(bs), int(col))
        print(f"{blk:>10} {off*2:>10}с  {hr.hf:>10.4f} {str(hr.liquidatable):>13}")
    except Exception as e:
        print(f"{blk:>10} {off*2:>10}с  ошибка: {type(e).__name__}: {str(e)[:60]}")

print("\nЕсли liquidatable=True держится за много блоков назад от N — блок-луп проспал (баг).")
print("Если liquidatable становится True только на N (или N-1) — флип случился в блоке N,")
print("блок-луп физически не мог успеть раньше следующего блока (не баг, цель горячего пути).")
