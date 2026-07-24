"""Юниты SPEC-FIRE LIVE (Stage B): порог |ret|>=dev, каденс, капы, WIN-маркировка, изоляция. Без сети."""
import sys
import threading
import time
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.spec_fire_live as sfl_mod
import chain.execute as ex
from chain.spec_fire_live import SpecFireLive

LOGS = []


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)


shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT", "0xdust"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0, ("0xbtc", "0xdust"): 90_000.0},
    by_id={"0xbtc": mk("cbBTC", "USDC")},
)
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                            tip_gwei=0.05, sequencer_rpc="https://seq", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)

RET = {"v": 0.0}
sent, receipts = [], []


def build_fn(mid, b, d):
    return {"calldata": "0xdeadbeef", "gas": 625_000}


def send_fn(calldata, gas):
    sent.append((calldata, gas))
    return "0xhash%d" % len(sent)


def receipt_fn(h):
    receipts.append(h)
    return (0, 0.011)     # revert 'healthy'


def make(ret_v=RET, **kw):
    kw.setdefault("build_fn", build_fn)
    kw.setdefault("send_fn", send_fn)
    kw.setdefault("receipt_fn", receipt_fn)
    kw.setdefault("log_fn", LOGS.append)
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared,
                        ret_fn=lambda lbl: ret_v["v"], dev_by={"BTC": 0.0010}, **kw)


# 1) arm без жира -> петля не стартует
sf = make()
empty_shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
sf2 = SpecFireLive(rpc=None, cfg=cfg, shared=empty_shared, ret_fn=lambda l: -1.0,
                   dev_by={"BTC": 0.001}, build_fn=build_fn, send_fn=send_fn,
                   receipt_fn=receipt_fn, log_fn=LOGS.append)
sf2.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.2)
assert sf2.stats["windows_armed"] == 0 and not sent
print("UNIT_OK: окно без жира не запускает петлю")

# 2) ниже порога (|ret| < dev) — огня НЕТ; выше — пробы идут с каденсом
sent.clear(); RET["v"] = -0.0005          # 0.05% < dev 0.10%
sf.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.5)
assert sf.stats["windows_armed"] == 1 and not sent, sent
RET["v"] = -0.0011                        # пробили dev вниз
time.sleep(1.2)
sf.handle_event({"event": "push", "feed": "BTC"})
n1 = len(sent)
assert 1 <= n1 <= 3, sent                 # каденс 0.7с: за ~1.2с максимум 2-3 пробы, не пулемёт
print("UNIT_OK: до dev тишина, после dev пробы с каденсом (n=%d)" % n1)

# 3) push стопит окно; новых проб нет
time.sleep(0.6)
assert len(sent) == n1
print("UNIT_OK: push останавливает огонь")

# 4) dir=up не стреляет на ret<0 (направление сверяется)
sent.clear(); RET["v"] = -0.002
sf.handle_event({"event": "arm", "feed": "BTC", "dir": "up"})
time.sleep(0.5)
sf.handle_event({"event": "disarm", "feed": "BTC"})
assert not sent
print("UNIT_OK: направление окна сверяется со знаком ret")

# 5) кап проб на окно
sent.clear(); RET["v"] = -0.005
old_cap = sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW
sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW = 2
old_iv = sfl_mod.SPEC_FIRE_INTERVAL_SEC
sfl_mod.SPEC_FIRE_INTERVAL_SEC = 0.05
sf3 = make()
sf3.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.8)
sf3.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) == 2, sent
sfl_mod.SPEC_FIRE_MAX_PROBES_WINDOW = old_cap
sfl_mod.SPEC_FIRE_INTERVAL_SEC = old_iv
print("UNIT_OK: кап проб/окно соблюдён")

# 6) WIN: receipt status=1 -> _RECENT_SENDS маркирован; revert -> НЕ маркирован
ex._RECENT_SENDS.clear()
sf4 = make(receipt_fn=lambda h: (1, 0.02))
sf4._collect("0xh", "0xbtc", "0xFAT", 1_500_000.0)
assert ("0xbtc", "0xFAT") in ex._RECENT_SENDS and sf4.stats["wins"] == 1
ex._RECENT_SENDS.clear()
sf5 = make(receipt_fn=lambda h: (0, 0.011))
sf5._collect("0xh2", "0xbtc", "0xFAT", 1_500_000.0)
assert ("0xbtc", "0xFAT") not in ex._RECENT_SENDS and sf5.stats["wins"] == 0
print("UNIT_OK: WIN маркирует дедуп, revert — нет (реактивный путь не глушится)")

# 7) REACH-гейт: жир с HF далеко от флипа НЕ жжёт бакет на малом ret; большой ret — огонь
shared_far = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0},
    hf_by={("0xbtc", "0xFAT"): 1.0899},           # dist=8.25% (живой кит 24.07)
    by_id={"0xbtc": mk("cbBTC", "USDC")},
)
sent.clear(); LOGS.clear(); RET["v"] = -0.0011    # reach = 0.11%*3 = 0.33% << 8.25%
sf6 = SpecFireLive(rpc=None, cfg=cfg, shared=shared_far, ret_fn=lambda l: RET["v"],
                   dev_by={"BTC": 0.0010}, build_fn=build_fn, send_fn=send_fn,
                   receipt_fn=receipt_fn, log_fn=LOGS.append)
sf6.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.6)
assert sf6.stats["windows_armed"] == 1, "арм БЕЗ гейта — окно должно запускаться"
assert not sent, sent
assert any("hold" in m and "8.25" in m for m in LOGS), LOGS
RET["v"] = -0.05                                  # краш −5%: reach 15% >= 8.25% — огонь
time.sleep(1.0)
sf6.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) >= 1, "в пределах досягаемости гейт обязан пропустить огонь"
print("UNIT_OK: reach-гейт держит далёкий жир (hold-лог), краш-ret открывает огонь")

# 7b) HF неизвестен (нет hf_by / старый снапшот) -> fail-open, поведение прежнее
sent.clear(); RET["v"] = -0.0011
sf7 = make()                                      # shared без hf_by вовсе
sf7.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.9)
sf7.handle_event({"event": "disarm", "feed": "BTC"})
assert len(sent) >= 1, "без HF гейт должен быть открыт (fail-open)"
print("UNIT_OK: HF неизвестен -> fail-open (без hf_by поведение прежнее)")

# 8) send_spec_fire_probe: гейт inflight>0 молчит; газ-бакет стопит
ex._INFLIGHT = 1
assert ex.send_spec_fire_probe(None, cfg, "0x", 100000) is None
ex._INFLIGHT = 0
ex._SPECFIRE_GAS_USD = ex._SPECFIRE_MAX_GAS_USD + 1
assert ex.send_spec_fire_probe(None, cfg, "0x", 100000) is None
ex._SPECFIRE_GAS_USD = 0.0
print("UNIT_OK: probe-гейты (inflight, газ-бакет) атомарно закрывают отправку")

print("=== ВСЕ SPEC-FIRE-LIVE ЮНИТЫ ПРОШЛИ ===")
