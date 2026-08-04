"""Юниты LIVE-CAP (04.08, weETH tail): set_live_cap/TTL/рост-кап, live-aware direct_swap и
route_max_in (слайс-гейт), семантика ошибок замера (реверт=данные, транспорт=fail-open),
rate-limit maybe_refresh, гейт вотчера по долгу, cap-aware prearm на живом капе,
откат ROUTE_LIVE_CAP=0. Без сети."""
import sys
import threading
import time
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.execute as ex
import chain.route_depth as rd
import chain.prearm as pa
from chain.execute import (direct_swap, route_max_in, set_live_cap, partial_shares_for_cap,
                           _WEETH, _UNIV3_WETH, _LIVE_CAPS)
from chain.route_depth import measure_route_cap, maybe_refresh, RouteDepthWatcher

KEY = (_WEETH, _UNIV3_WETH.lower())
STATIC = 160 * 10**18
NOW = {"t": 1000.0}


def enc_out(amount_out):
    return (True, amount_out.to_bytes(32, "big") + b"\x00" * 96)


# ---- 1) set_live_cap: переопределение, TTL-протухание, кап роста, откат ----
_LIVE_CAPS.clear()
assert route_max_in(_WEETH, _UNIV3_WETH) == STATIC
got = set_live_cap(_WEETH, _UNIV3_WETH, 240 * 10**18, now=NOW["t"])
assert got == 240 * 10**18
assert ex._effective_cap(KEY, now=NOW["t"] + 1) == 240 * 10**18       # живой кап в силе
assert ex._effective_cap(KEY, now=NOW["t"] + ex.LIVE_CAP_TTL_SEC + 1) == STATIC   # протух -> статик
got = set_live_cap(_WEETH, _UNIV3_WETH, 10_000 * 10**18, now=NOW["t"])
assert got == int(STATIC * ex.LIVE_CAP_GROW_MAX)                      # рост капнут x2
assert set_live_cap("0x" + "ab" * 20, _UNIV3_WETH, 1) is None         # нет роута — игнор
ex.ROUTE_LIVE_CAP = False
assert ex._effective_cap(KEY, now=NOW["t"] + 1) == STATIC             # откат: только статик
ex.ROUTE_LIVE_CAP = True
print("OK 1: set_live_cap/TTL/рост-кап/откат ROUTE_LIVE_CAP=0")

# ---- 2) direct_swap живёт по живому капу: расширенный кап строит сайз > статик,
#         суженный кап отдаёт None (Kyber) на сайзе < статик ----
_LIVE_CAPS.clear()
SND, RCP = "0x" + "11" * 20, "0x" + "22" * 20
seize_today = 168 * 10**18                                            # жертва 04.08: 167.9 weETH
assert direct_swap(_WEETH, _UNIV3_WETH, seize_today, SND, RCP, repaid_assets=1) is None
set_live_cap(_WEETH, _UNIV3_WETH, 240 * 10**18)
sw = direct_swap(_WEETH, _UNIV3_WETH, seize_today, SND, RCP, repaid_assets=1)
assert sw is not None and sw["router"] == ex.AERO_CL_ROUTER           # прямой пул взял 168
set_live_cap(_WEETH, _UNIV3_WETH, 40 * 10**18)                        # пул осушен
assert direct_swap(_WEETH, _UNIV3_WETH, 100 * 10**18, SND, RCP, repaid_assets=1) is None
assert route_max_in(_WEETH, _UNIV3_WETH) == 40 * 10**18               # слайс-гейт видит живой кап
sp = partial_shares_for_cap(1000, 100 * 10**18, route_max_in(_WEETH, _UNIV3_WETH))
assert 0 < sp < 1000                                                  # дробим до живого капа
print("OK 2: direct_swap/слайс-гейт по живому капу (168 в прямой пул при live 240)")

# ---- 3) measure_route_cap: кривая -> кап на верхней проходной ступени ----
_LIVE_CAPS.clear()
def batch_flat_to(limit_frac):
    def fn(rpc, calls):
        out = []
        sizes = [max(STATIC // 100, 1)] + [int(STATIC * f) for f in rd.LIVE_CAP_LADDER]
        for s in sizes:
            if s <= STATIC * limit_frac + 1:
                out.append(enc_out(int(s * 1.10)))                    # плоско: ratio 1.10
            else:
                out.append(enc_out(int(s * 1.10 * 0.90)))             # обрыв: импакт 10%
        return out
    return fn
cap = measure_route_cap(None, _WEETH, _UNIV3_WETH, batch_fn=batch_flat_to(1.5), log_fn=lambda m: None)
assert cap == int(STATIC * 1.5), cap                                  # сегодня (04.08): плоско до 1.5x
cap = measure_route_cap(None, _WEETH, _UNIV3_WETH, batch_fn=batch_flat_to(0.5), log_fn=lambda m: None)
assert cap == int(STATIC * 0.5), cap                                  # просел до полкапа
print("OK 3: лестница -> кап на верхней проходной ступени (обе стороны)")

# ---- 4) семантика ошибок: реверт базы = пул мёртв (кап 0 -> Kyber), обрыв ниже 0.25x =
#         статик//10, транспортная ошибка = fail-open (кап не тронут) ----
def batch_dead(rpc, calls):
    return [(False, b"")] * len(calls)
cap = measure_route_cap(None, _WEETH, _UNIV3_WETH, batch_fn=batch_dead, log_fn=lambda m: None)
assert cap == 0
assert direct_swap(_WEETH, _UNIV3_WETH, 1 * 10**18, SND, RCP, repaid_assets=1) is None
def batch_only_base(rpc, calls):
    return [enc_out(int(STATIC // 100 * 1.10))] + [(False, b"")] * (len(calls) - 1)
cap = measure_route_cap(None, _WEETH, _UNIV3_WETH, batch_fn=batch_only_base, log_fn=lambda m: None)
assert cap == STATIC // 10, cap
def batch_raise(rpc, calls):
    raise OSError("rpc down")
before = ex._effective_cap(KEY)
assert measure_route_cap(None, _WEETH, _UNIV3_WETH, batch_fn=batch_raise, log_fn=lambda m: None) is None
assert ex._effective_cap(KEY) == before                               # транспорт не трогает кап
print("OK 4: реверт=данные (0 / статик//10), транспорт=fail-open")

# ---- 5) maybe_refresh: rate-limit на роут, force пробивает, некапнутый роут = быстрый no-op ----
_LIVE_CAPS.clear(); rd._LAST_TS.clear()
calls_n = {"n": 0}
def batch_count(rpc, calls):
    calls_n["n"] += 1
    return batch_flat_to(1.5)(rpc, calls)
assert maybe_refresh(None, _WEETH, _UNIV3_WETH, now=100.0, batch_fn=batch_count, log_fn=lambda m: None)
assert maybe_refresh(None, _WEETH, _UNIV3_WETH, now=101.0, batch_fn=batch_count) is None   # рано
assert maybe_refresh(None, _WEETH, _UNIV3_WETH, now=101.0, force=True,
                     batch_fn=batch_count, log_fn=lambda m: None)                          # force
assert maybe_refresh(None, _WEETH, _UNIV3_WETH, now=100.0 + rd.LIVE_CAP_REFRESH_SEC + 1,
                     batch_fn=batch_count, log_fn=lambda m: None)
assert calls_n["n"] == 3
assert maybe_refresh(None, "0x" + "cd" * 20, _UNIV3_WETH, now=200.0) is None               # нет роута
print("OK 5: rate-limit/force/no-op без капа")

# ---- 6) вотчер: меряет ТОЛЬКО капнутые пары с долгом >= порога; ошибка цикла не роняет ----
_LIVE_CAPS.clear(); rd._LAST_TS.clear()
MID_W, MID_S = "0xweeth", "0xsmall"
shared = types.SimpleNamespace(
    lock=threading.Lock(),
    debt_by={(MID_W, "0xFAT"): 339_000.0, (MID_W, "0xtiny"): 900.0, (MID_S, "0xmid"): 24_000.0})
refreshed = []
w = RouteDepthWatcher(None, shared, log_fn=lambda m: None,
                      resolve_fn=lambda mids: {MID_W: KEY, MID_S: ("0x" + "ee" * 20, "0xff")},
                      refresh_fn=lambda a, b: refreshed.append((a, b)) or 42)
w._cycle()
assert refreshed == [KEY], refreshed          # только weETH-пара: $339k >= 25k и роут капнут
assert w.stats == {"cycles": 1, "refreshes": 1, "errors": 0}
w._cycle()
assert len(refreshed) == 2 and MID_W in w._pair_by_mid                # кэш резолва, повторный замер
def bad_refresh(a, b):
    raise RuntimeError("boom")
w2 = RouteDepthWatcher(None, shared, log_fn=lambda m: None,
                       resolve_fn=lambda mids: {MID_W: KEY}, refresh_fn=bad_refresh)
_pl = rd.LIVE_CAP_WATCH_POLL_SEC
rd.LIVE_CAP_WATCH_POLL_SEC = 0.01
t = threading.Thread(target=w2._loop, daemon=True)
t.start(); time.sleep(0.1); w2.stop(); t.join(1.0)
assert w2.stats["errors"] >= 1                                         # петля пережила ошибки цикла
rd.LIVE_CAP_WATCH_POLL_SEC = _pl
print("OK 6: гейт вотчера ($25k, только капнутые) + петля переживает ошибки")

# ---- 7) prearm cap-aware на ЖИВОМ капе: сегодняшний кейс — seize 168 при статик 160 ----
_LIVE_CAPS.clear()
assert pa.routed_at_size(_WEETH, _UNIV3_WETH, 168 * 10**18) is False   # статик: Kyber-прогрев
set_live_cap(_WEETH, _UNIV3_WETH, 240 * 10**18)
assert pa.routed_at_size(_WEETH, _UNIV3_WETH, 168 * 10**18) is True    # живой: прямой пул, греть нечего
assert pa.routed_at_size(_WEETH, _UNIV3_WETH, 250 * 10**18) is False   # сверх живого — греем
_pca = pa.PREARM_CAP_AWARE
pa.PREARM_CAP_AWARE = False
assert pa.routed_at_size(_WEETH, _UNIV3_WETH, 10**30) is True          # откат: бинарная семантика
pa.PREARM_CAP_AWARE = _pca
print("OK 7: prearm cap-aware по живому капу (168>160-статик -> прямой пул при live 240)")

# ---- 8) горячий замер в prearm-окне: maybe_refresh дёргается и routed пересчитывается ----
_LIVE_CAPS.clear(); rd._LAST_TS.clear()
rd_measured = {"n": 0}
_orig_mr = rd.maybe_refresh
def fake_mr(rpc, tin, tout, **kw):
    rd_measured["n"] += 1
    return set_live_cap(tin, tout, 240 * 10**18)
rd.maybe_refresh = fake_mr
try:
    cfgp = types.SimpleNamespace(liquidator_address="0xLIQ", fast_lane_max_repaid_usd=5e5)
    sh = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
    warmed = []
    pw = pa.PreArmWorker(None, cfgp, sh,
                         read_fn=lambda mid, b: {"coll": _WEETH, "loan": _UNIV3_WETH,
                                                 "seized": 168 * 10**18,
                                                 "routed": pa.routed_at_size(_WEETH, _UNIV3_WETH,
                                                                             168 * 10**18)},
                         quote_fn=lambda c, l, s: warmed.append(s) or {"amount_in": s},
                         log_fn=lambda m: None)
    pw._pick = lambda feed, direction: [("0xmid", "0xb", 339_000.0)]
    stop = threading.Event()
    threading.Thread(target=pw._window_loop, args=("ETH", "down", stop), daemon=True).start()
    time.sleep(0.5); stop.set()
    assert rd_measured["n"] >= 1                                       # замер в окне случился
    assert not warmed                                                  # live 240 покрыл 168 — Kyber не грели
finally:
    rd.maybe_refresh = _orig_mr
print("OK 8: prearm-окно перемеряет кап и НЕ греет Kyber, когда живой пул покрывает сейз")

print("\nВСЕ БЛОКИ LIVE-CAP: OK")
