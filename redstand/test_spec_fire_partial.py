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

# 2) route_max_in на боевой таблице: cbDOGE капнут по замеру, LBTC/cbBTC — тоже (#75: раньше
#    он жил БЕЗ капа и потому был невидим живому слою, который меряет только капнутые роуты)
CBDOGE = "0xcbd06e5a2b0c65597161de254aa074e489deb510"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
assert ex.route_max_in(CBDOGE, USDC) == 32_000 * 10**8   # #71: кап по замеру обрыва (64.9k/2)
assert ex.route_max_in("0xecac9c5f704e954931349da37f60e39f515c11c1",
                       "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf") == 27 * 10**8   # LBTC/cbBTC
assert ex.route_max_in("0x" + "11" * 20, USDC) is None, "нет роута -> None"
print("UNIT_OK: route_max_in — капы cbDOGE/LBTC по замеру, None для чужих пар")

# 3) direct_swap: сверх капа None, в капе — calldata (боевая таблица, без сети)
LIQ = "0x" + "12" * 20
big = ex.direct_swap(CBDOGE, USDC, 1_200_000 * 10**8, LIQ, LIQ, repaid_assets=10**6)
assert big is None, "сейз кита сверх капа обязан отдать None (как в гонке 01.08)"
ok = ex.direct_swap(CBDOGE, USDC, 30_000 * 10**8, LIQ, LIQ, repaid_assets=10**6)  # #71: под капом 32k
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
# #74: залог теперь УЧАСТВУЕТ в сборке плана (_cap_repaid_shares держит seize в его
# пределах). Прежняя фикстура отдавала 0 — с ней новый код честно отказывается строить
# план (по нулевому залогу забирать нечего), и это правильное поведение. Даём залог с
# запасом, чтобы экзаменовать то, ради чего тест написан: дробление по капу РОУТА.
mc.decode_position = lambda d: (BS, 10**30)
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

# 5) РЕАКТИВ: prepare_fast режет до капа вместо «fast: no direct route» (го 01.08);
#    откат REACTIVE_PARTIAL=0 возвращает прежний отказ
import chain.hotpath as hp

cap2 = {}
_orig2 = (hp._fresh_basefee, hp._net_gate, hp.dynamic_tip_gwei, dict(hp._FULL_PARAMS_CACHE),
          ex.direct_swap, ex.encode_liquidate, ex.route_max_in, hp.REACTIVE_PARTIAL)


def fake_direct2(coll, loan, seized, s, r, *, repaid_assets, **kw):
    cap2.setdefault("seized_calls", []).append(int(seized))
    return None if int(seized) > CAP else {"router": "0xR", "calldata": "0xAB"}


def fake_encode2(mp, borrower, repaid_shares, router, cdata, min_profit):
    cap2["shares"] = int(repaid_shares)
    return "0xCD"


hp._fresh_basefee = lambda: 10**7
hp._net_gate = lambda mp_, du, da, c: (100.0, 1.0, 99.0)
hp.dynamic_tip_gwei = lambda c, d: 0.05
hp._FULL_PARAMS_CACHE["0xM"] = ("0xLOAN", "0xCOLL", "0xORCL", "0xIRM", 625 * 10**15)
ex.direct_swap = fake_direct2
ex.encode_liquidate = fake_encode2
ex.route_max_in = lambda c, l: CAP

cfg2 = types.SimpleNamespace(liquidator_address="0xLIQ", min_profit_usd=10.0,
                             gas_limit_floor=3_000_000, gas_limit_est=500_000)
state = (TBA, TBS, BS, 10**30)          # залог огромный — кап bad-debt не вмешивается
try:
    res = hp.prepare_fast(None, None, cfg2, "0xM", "0xB0B", 66_000.0, 66_000 * 10**6,
                          10**36, state)
    assert res.get("ok") is True and res.get("fast") is True, res
    assert 0 < cap2["shares"] < BS, "fast-lane обязан дробить shares"
    assert cap2["seized_calls"][0] > CAP and cap2["seized_calls"][-1] <= CAP, cap2["seized_calls"]

    hp.REACTIVE_PARTIAL = False
    cap2.clear()
    res = hp.prepare_fast(None, None, cfg2, "0xM", "0xB0B", 66_000.0, 66_000 * 10**6,
                          10**36, state)
    assert res.get("ok") is False and res.get("reason") == "fast: no direct route", res
    assert "shares" not in cap2
finally:
    (hp._fresh_basefee, hp._net_gate, hp.dynamic_tip_gwei, _pc,
     ex.direct_swap, ex.encode_liquidate, ex.route_max_in, hp.REACTIVE_PARTIAL) = _orig2
    hp._FULL_PARAMS_CACHE.clear()
    hp._FULL_PARAMS_CACHE.update(_pc)
print("UNIT_OK: prepare_fast — частичка вместо отказа, откат REACTIVE_PARTIAL=0")

# 6) PREARM cap-aware: routed_at_size по боевой таблице cbDOGE; откат PREARM_CAP_AWARE=0
import chain.prearm as pa2

assert pa2.routed_at_size(CBDOGE, USDC, 30_000 * 10**8) is True, "сейз в капе — прямой прикрыт"
assert pa2.routed_at_size(CBDOGE, USDC, 1_200_000 * 10**8) is False, "кит сверх капа — греть Kyber"
assert pa2.routed_at_size("0x" + "11" * 20, USDC, 1) is False, "нет роута — не прикрыт"
pa2.PREARM_CAP_AWARE = False
assert pa2.routed_at_size(CBDOGE, USDC, 1_200_000 * 10**8) is True, "откат = старая бинарность"
pa2.PREARM_CAP_AWARE = True
print("UNIT_OK: prearm routed_at_size — кап учитывается, откат PREARM_CAP_AWARE=0")

# 7) ФОЛБЭК ЦЕЛИ в окне (ревью гонки cbDOGE): топ-цель «нет свопа» НЕ монополизирует тик —
#    проба уходит следующей досягаемой; откат SPEC_FIRE_BUILD_FALLBACK=0 = прежний лок
import threading
import time as _time

# боевой порог из .env Шарлотты (дефолт модуля $500k срезал бы обе цели ещё на арме)
_orig_min_repaid = sfl_mod.SPEC_FIRE_MIN_REPAID_USD
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = 1000.0


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)


def run_window(build_fn, seconds=1.6):
    shared = types.SimpleNamespace(
        lock=threading.Lock(),
        groups={"0xm1": ["0xFATWHALE", "0xSMALL"]},
        debt_by={("0xm1", "0xFATWHALE"): 71_000.0, ("0xm1", "0xSMALL"): 4_100.0},
        by_id={"0xm1": mk("cbDOGE", "USDC")})
    sent, logs = [], []
    cfgw = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                                 tip_gwei=0.05, sequencer_rpc="https://seq",
                                 eth_price_usd=1730.0, fast_lane_max_repaid_usd=500_000.0)
    sf = SpecFireLive(rpc=None, cfg=cfgw, shared=shared, ret_fn=lambda l: -0.01,
                      dev_by={"DOGE": 0.005}, build_fn=build_fn,
                      send_fn=lambda cd, g: (sent.append(cd), "0xH")[1],
                      receipt_fn=lambda h: (0, 0.01), log_fn=logs.append)
    sf.handle_event({"event": "arm", "feed": "DOGE", "dir": "down"})
    _time.sleep(seconds)
    sf.handle_event({"event": "disarm", "feed": "DOGE"})
    _time.sleep(0.1)
    return sent, logs


builds = []
def build_fallback(mid, b, d):
    builds.append(b)
    return None if b == "0xFATWHALE" else {"calldata": "0xCD", "gas": 100}

sent, logs = run_window(build_fallback)
assert "0xFATWHALE" in builds and "0xSMALL" in builds, builds
assert sent, "фолбэк обязан отправить пробу по второй цели"
assert any("нет свопа" in m and "фолбэк" in m for m in logs), logs[-3:]
assert any("probe #1" in m and "0xSMALL" in m for m in logs), "проба именно по 0xSMALL"

builds2 = []
def build_lock(mid, b, d):
    builds2.append(b)
    return None

sfl_mod.SPEC_FIRE_BUILD_FALLBACK = False
sent2, logs2 = run_window(build_lock)
sfl_mod.SPEC_FIRE_BUILD_FALLBACK = True
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = _orig_min_repaid
assert not sent2 and set(builds2) == {"0xFATWHALE"}, (sent2, set(builds2))
print("UNIT_OK: фолбэк цели — вторая цель получает пробу, откат возвращает top-1 лок")

print("=== ВСЕ SPEC-FIRE-PARTIAL ЮНИТЫ ПРОШЛИ ===")
