"""Юниты дробления пробы (01.08, проигранная гонка cbDOGE: сейз $80k > ёмкость пула $6.8k).
Чистая математика + интеграция _build на швах. Без сети."""
import sys
import types

sys.path.insert(0, ".")
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive, partial_shares_for_cap
import chain.execute as ex
import chain.multicall as mc
import chain.prearm as pa

# 1) partial_shares_for_cap: помещается -> 0; сверх капа -> скейл с хедрумом; нулевые гарды
assert partial_shares_for_cap(1000, 500, 600) == 0, "сейз в капе — дробить нечего"
assert partial_shares_for_cap(1000, 0, 600) == 0 and partial_shares_for_cap(0, 500, 100) == 0
assert partial_shares_for_cap(1000, 500, 0) == 0
sp = partial_shares_for_cap(1_000_000, 800_000, 100_000)          # хедрум 95%
assert sp == 1_000_000 * 95_000 // 800_000 == 118_750, sp
assert partial_shares_for_cap(1_000_000, 800_000, 100_000, headroom_pct=100) == 125_000
# инвариант: пропорциональный сейз от sp не превышает кап
assert sp * 800_000 // 1_000_000 <= 100_000
print("UNIT_OK: partial_shares_for_cap — гарды, скейл, инвариант капа")

# 2) route_max_in на боевой таблице: cbDOGE капнут 100k токенов, LBTC/cbBTC без капа
CBDOGE = "0xcbd06e5a2b0c65597161de254aa074e489deb510"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
assert ex.route_max_in(CBDOGE, USDC) == 100_000 * 10**8
assert ex.route_max_in("0xecac9c5f704e954931349da37f60e39f515c11c1",
                       "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf") is None   # LBTC/cbBTC
assert ex.route_max_in("0x" + "11" * 20, USDC) is None, "нет роута -> None"
print("UNIT_OK: route_max_in — кап cbDOGE, None для бескапных/чужих пар")

# 3) direct_swap: сверх капа None, в капе — calldata (боевая таблица, без сети)
LIQ = "0x" + "12" * 20
big = ex.direct_swap(CBDOGE, USDC, 1_200_000 * 10**8, LIQ, LIQ, repaid_assets=10**6)
assert big is None, "сейз кита сверх капа обязан отдать None (как в гонке 01.08)"
ok = ex.direct_swap(CBDOGE, USDC, 90_000 * 10**8, LIQ, LIQ, repaid_assets=10**6)
assert ok is not None and ok.get("calldata", "").startswith("0x")
print("UNIT_OK: direct_swap — None сверх капа, роут в капе")

# 4) интеграция _build: полный сейз не лезет -> проба строится ЧАСТИЧКОЙ (shares < bs,
#    сейз в капе, minProfit скейлится), а с откатом SPEC_FIRE_PARTIAL=0 -> None
CAP = 100_000
BS, TBA, TBS = 800_000, 10**6, 10**6
PRICE = 10**36                     # ORACLE_PRICE_SCALE -> seized ~= repaid*LIF

captured = {}
_orig = (mc.aggregate3, mc.decode_id_to_market_params, mc.decode_market, mc.decode_position,
         mc.decode_price, ex.direct_swap, ex.encode_liquidate, pa.prearm_get, ex.route_max_in,
         mc.encode_id_to_market_params_call, mc.encode_market_call, mc.encode_position_call,
         mc.encode_price_call)


def fake_agg(rpc, calls):
    return [(True, b"\x00") for _ in calls]


def fake_direct(coll, loan, seized, s, r, *, repaid_assets, **kw):
    captured["swap_seized"] = int(seized)
    return None if int(seized) > CAP else {"router": "0xR", "calldata": "0xAB"}


def fake_encode(mp, borrower, repaid_shares, router, cdata, min_profit):
    captured["shares"] = int(repaid_shares)
    captured["min_profit"] = int(min_profit)
    return "0xCD"


mc.aggregate3 = fake_agg
mc.encode_id_to_market_params_call = lambda m32: b""
mc.encode_market_call = lambda m32: b""
mc.encode_position_call = lambda m32, b: b""
mc.encode_price_call = lambda: b""
mc.decode_id_to_market_params = lambda d: ("0xLOAN", "0xCOLL", "0xORCL", "0xIRM", 625 * 10**15)
mc.decode_market = lambda d: (0, 0, TBA, TBS)
mc.decode_position = lambda d: (BS, 0)
mc.decode_price = lambda d: PRICE
ex.direct_swap = fake_direct
ex.encode_liquidate = fake_encode
pa.prearm_get = lambda *a, **kw: None
ex.route_max_in = lambda c, l: CAP

cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                            fast_lane_max_repaid_usd=500_000.0, tip_gwei=0.05)
rpc = types.SimpleNamespace(to_bytes32=lambda s: b"\x00" * 32)
sf = SpecFireLive(rpc=rpc, cfg=cfg, shared=None, ret_fn=lambda l: None, dev_by={},
                  build_fn=lambda *a: None, send_fn=lambda *a: None,
                  receipt_fn=lambda h: (0, 0.0), log_fn=lambda m: None)
try:
    res = sf._build("0x" + "aa" * 32, "0xB0B", 66_000.0)
    assert isinstance(res, dict) and res["calldata"] == "0xCD", res
    assert 0 < captured["shares"] < BS, "repaid_shares обязан дробиться"
    assert captured["swap_seized"] <= CAP, "сейз частички обязан влезть в кап"
    assert captured["min_profit"] > 0, "minProfit скейлится с repaid, но не ноль"

    sfl_mod.SPEC_FIRE_PARTIAL = False        # откат: только полный сайз -> None
    captured.clear()
    assert sf._build("0x" + "aa" * 32, "0xB0B", 66_000.0) is None
    assert "shares" not in captured, "с откатом encode_liquidate не зовётся"
    sfl_mod.SPEC_FIRE_PARTIAL = True

    ex.route_max_in = lambda c, l: None      # роута нет вовсе -> дробить не во что -> None
    assert sf._build("0x" + "aa" * 32, "0xB0B", 66_000.0) is None
finally:
    (mc.aggregate3, mc.decode_id_to_market_params, mc.decode_market, mc.decode_position,
     mc.decode_price, ex.direct_swap, ex.encode_liquidate, pa.prearm_get, ex.route_max_in,
     mc.encode_id_to_market_params_call, mc.encode_market_call, mc.encode_position_call,
     mc.encode_price_call) = _orig
print("UNIT_OK: _build — частичка при капе, откат SPEC_FIRE_PARTIAL=0, без роута None")

print("=== ВСЕ SPEC-FIRE-PARTIAL ЮНИТЫ ПРОШЛИ ===")
