"""Точный таймлайн: реальные timestamp'ы блоков 48039554-48039562 (не оценка ~2с/блок) + HF
заёмщика 0x4Ffc5F22 на каждом. Закрывает дыру предыдущего drift-чека (сэмплировал редко, окно
флипа не сужено точнее чем 4 блока) и даёт точку сравнения с нашим sent=01:14:24.248 UTC
(=1782868464) без пересчёта секунд в блоки на глаз.
Read-only, архивные чтения, hot path не трогает.
Запуск: cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) \
        /root/liquidator/venv/bin/python -m analysis.block_timeline_check
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
w3 = rpc._web3()

MARKET_ID = "0x67ebd84b2fb39e3bc5a13d97e4c07abe1ea617e40654826e9abce252e95f049e"
BORROWER = "0x4Ffc5F22770ab6046c8D66DABAe3A9CD1E7A03e7"
N = 48039560
OUR_SENT_TS = 1782868464  # 01:14:24 UTC (округлено до секунды, .248 несущественно на фоне блоков)

b32 = rpc.to_bytes32(MARKET_ID)
params_res = aggregate3(rpc, [(MORPHO_BLUE, encode_id_to_market_params_call(b32))])
_loan, _coll, oracle, _irm, lltv_wad = decode_id_to_market_params(params_res[0][1])

print(f"{'block':>10} {'timestamp':>12} {'UTC':>10} {'Δsent':>8} {'HF':>10} {'liquidatable':>13}")
for blk in range(N - 6, N + 3):
    try:
        bl = w3.eth.get_block(blk)
        ts = bl["timestamp"]
        import datetime as dt
        utc = dt.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")
        delta = ts - OUR_SENT_TS
        res = aggregate3(rpc, [(MORPHO_BLUE, encode_market_call(b32)),
                               (MORPHO_BLUE, encode_position_call(b32, BORROWER))],
                         block_identifier=blk)
        if not res[0][0] or not res[1][0]:
            print(f"{blk:>10} {ts:>12} {utc:>10} {delta:>+7}с  чтение не удалось")
            continue
        m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
        bs, col = decode_position(res[1][1])
        price = rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier=blk)
        ctx = MarketContext(oracle=oracle, price=int(price), lltv_wad=int(lltv_wad),
                            total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
        hr = health_from(ctx, int(bs), int(col))
        marker = "  <-- N (конкурент)" if blk == N else ("  <-- мы" if blk == N + 1 else "")
        print(f"{blk:>10} {ts:>12} {utc:>10} {delta:>+7}с  {hr.hf:>10.4f} {str(hr.liquidatable):>13}{marker}")
    except Exception as e:
        print(f"{blk:>10}  ошибка: {type(e).__name__}: {str(e)[:60]}")

print(f"\nНаш sent = 01:14:24 UTC ({OUR_SENT_TS}). Δsent — на сколько секунд timestamp блока")
print("ПОЗЖЕ нашего sent (отрицательное = блок был ДО отправки, положительное = блок ПОСЛЕ).")
print("Смотрим: на каком именно блоке HF впервые < 1, и было ли это ДО или ПОСЛЕ нашего sent.")
