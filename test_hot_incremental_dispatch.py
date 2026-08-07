"""Инкрементальный hot-dispatch (дефект №3 вскрытия каскада 06.08: 11 preps серией + один
батч-dispatch = first-ready ждал хвост 7.8с при окне победы 0.5-2.2мс).
Инварианты: ON — готовая цель уходит ДО prepare следующей; OFF — прежний батч бит-в-бит;
kill-switch посреди потока стопит дальнейшие сенды, уже ушедшие остаются."""
import threading
import types
import chain.hotpath as hp

MID = "0x" + "ab" * 32
AGG = "0x" + "11" * 20
ORACLE = "0x" + "22" * 20
LLTV = 860000000000000000


def _mk_env(events, dispatch_results=None, blocked_after=None):
    """Собирает все зависимости _process_transmit; events пишет порядок prep/dispatch."""
    shared = types.SimpleNamespace(
        lock=threading.Lock(),
        groups={MID: ["0xb1", "0xb2"]},
        debt_by={(MID, "0xb1"): 3000.0, (MID, "0xb2"): 4000.0},
        debt_assets_by={},
    )
    store = types.SimpleNamespace(realized_today=lambda: {"net": 0.0, "gas": 0.0},
                                  log_action=lambda **kw: None)
    calls = {"guard": 0}

    def _blocked(state):
        calls["guard"] += 1
        if blocked_after is not None and calls["guard"] > blocked_after:
            return "test-cap"
        return None

    guard = types.SimpleNamespace(blocked_reason=_blocked)
    alerter = types.SimpleNamespace(send_async=lambda *a, **k: None)
    cfg = types.SimpleNamespace(mode="live", hot_min_repaid_usd=1000.0, fast_lane=False,
                                positions_from_cache=False, max_inflight=4)

    def reads_fn(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
        if src_out is not None:
            src_out["src"] = "test"
        return (10**6, 10**6, [(b, 10**6, 10**6) for b in borrowers])

    def prepare_fn(rpc, preconf_rpc, cfg, mid, b, du, debt_assets, price, state=None):
        events.append("prep:" + b)
        return {"ok": True, "calldata": "0x", "net_usd": 5.0}

    def dispatch_fn(rpc, cfg, ready, log=None, max_inflight=None):
        events.append("dispatch:" + "+".join(b for _, b, _ in ready))
        return [{"market_id": m, "borrower": b, "sent": True, "status": 1, "net_usd": 5.0,
                 "hash": "0xt", "gas_usd": 0.1} for m, b, _ in ready]

    return dict(rpc=None, preconf_rpc=None, cfg=cfg, feeds={AGG: [MID]},
                meta={MID: (ORACLE, LLTV)}, shared=shared, store=store, guard=guard,
                alerter=alerter, log=hp.log if hasattr(hp, "log") else _log(),
                reads_fn=reads_fn, prepare_fn=prepare_fn, dispatch_fn=dispatch_fn,
                px_hint=12345, hint_oracle=ORACLE)


def _log():
    import logging
    return logging.getLogger("test-hot-incr")


_orig_flips = hp._flips
hp._flips = lambda price, lltv, tba, tbs, positions: [
    (b, types.SimpleNamespace(hf=0.98), du) for (b, bs, col, du) in positions]
_orig_flag = hp.HOT_DISPATCH_INCREMENTAL
try:
    # 1. ON (дефолт кода): каждая готовая цель уходит ДО prepare следующей
    assert _orig_flag is True, "дефолт HOT_DISPATCH_INCREMENTAL должен быть ON"
    ev = []
    kw = _mk_env(ev)
    kw["log"] = _log()
    res = hp._process_transmit(AGG, 100, 0, __import__("time").time(), **kw)
    assert ev == ["prep:0xb1", "dispatch:0xb1", "prep:0xb2", "dispatch:0xb2"], ev
    assert len(res) == 2 and all(r["sent"] for r in res)
    print("UNIT_OK: ON — first-ready стреляет до хвоста preps")

    # 2. OFF: прежний батч бит-в-бит (все preps, потом один dispatch со всеми)
    hp.HOT_DISPATCH_INCREMENTAL = False
    ev = []
    kw = _mk_env(ev)
    kw["log"] = _log()
    res = hp._process_transmit(AGG, 101, 0, __import__("time").time(), **kw)
    assert ev == ["prep:0xb1", "prep:0xb2", "dispatch:0xb1+0xb2"], ev
    assert len(res) == 2
    print("UNIT_OK: OFF — батч бит-в-бит прежний")

    # 3. ON + kill-switch после первого сенда: второй НЕ уходит, первый остаётся в results
    hp.HOT_DISPATCH_INCREMENTAL = True
    ev = []
    kw = _mk_env(ev, blocked_after=1)
    kw["log"] = _log()
    res = hp._process_transmit(AGG, 102, 0, __import__("time").time(), **kw)
    assert ev == ["prep:0xb1", "dispatch:0xb1", "prep:0xb2"], ev
    assert len(res) == 1
    print("UNIT_OK: kill-switch стопит хвост, ушедшее в results")
finally:
    hp._flips = _orig_flips
    hp.HOT_DISPATCH_INCREMENTAL = _orig_flag

print("ПРОШЛИ: инкрементальный hot-dispatch")
