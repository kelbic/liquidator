"""Юниты пакета B (SCHED, 04.08): слот -> самая богатая вооружённая жертва, ничья -> round-robin,
протухание заявки, yield без расхода капа, откат SPEC_FIRE_SCHED=0; интеграция: pending-handoff
стреляет вне очереди/гейтов, кипэлайв A3 растягивает on-chain пробы. Без сети."""
import sys
import threading
import time
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive
import chain.spec_fire_live as _sfl_v1
_sfl_v1.SPEC_FIRE_V1_MAX_PUSHES = 1   # В1: этим стендам нужна семантика «пуш закрывает»

sfl_mod.SPEC_FIRE_PEAK_OWED = False
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = 1000.0
sfl_mod.SPEC_FIRE_WIN_COOLDOWN_SEC = 0.0

LOGS = []


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)


cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="", eth_price_usd=1730.0, fast_lane_max_repaid_usd=5e5)


def make(shared, rets, sent, devs=None, **kw):
    def build_fn(mid, b, d):
        return {"calldata": "cd:" + mid, "gas": 625_000}

    def send_fn(calldata, gas):
        sent.append(calldata)
        return "0xh%d" % len(sent)

    kw.setdefault("build_fn", build_fn)
    kw.setdefault("send_fn", send_fn)
    kw.setdefault("receipt_fn", lambda h: (0, 0.011))
    kw.setdefault("log_fn", LOGS.append)
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared,
                        ret_fn=lambda lbl: rets.get(lbl.upper(), 0.0),
                        dev_by=devs or {"BTC": 0.0010, "XRP": 0.0050}, **kw)


# ---- 1) _prio_ok: богатый берёт слот, бедный уступает; протухшая заявка не голодит ----
shared0 = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
s = make(shared0, {}, [])
n = time.monotonic()
s._prio["BTC"] = (1_500_000.0, n)
assert s._prio_ok("XRP", 90_000.0) is False               # бедный уступает свежей заявке
assert s._prio_ok("BTC", 1_500_000.0) is True             # богатый берёт
s._prio["BTC"] = (1_500_000.0, n - sfl_mod.SPEC_FIRE_PRIO_TTL_SEC - 1)
assert s._prio_ok("XRP", 90_000.0) is True                # протухла — бедный не голодает
print("OK 1: приоритет по богатству + протухание заявки")

# ---- 2) ничья -> round-robin по временным слотам (оба получают слоты) ----
s._prio["BTC"] = (500_000.0, time.monotonic())
seen = set()
t_end = time.monotonic() + sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC * 4
while time.monotonic() < t_end:
    if s._prio_ok("XRP", 500_000.0):
        seen.add("XRP")
    else:
        seen.add("BTC-slot")
    time.sleep(0.05)
assert seen == {"XRP", "BTC-slot"}, seen                  # слоты чередуются, никто не монополист
print("OK 2: ничья — round-robin")

# ---- 3) откат SPEC_FIRE_SCHED=0: арбитр всегда пропускает ----
sfl_mod.SPEC_FIRE_SCHED = False
assert s._prio_ok("XRP", 1.0) is True
sfl_mod.SPEC_FIRE_SCHED = True
print("OK 3: откат SPEC_FIRE_SCHED=0")

# ---- 4) интеграция: бедное окно уступает, богатое стреляет; кап бедного НЕ горит ----
shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT"], "0xxrp": ["0xMID"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0, ("0xxrp", "0xMID"): 600_000.0},
    by_id={"0xbtc": mk("cbBTC", "USDC"), "0xxrp": mk("cbXRP", "USDC")},
)
rets = {"BTC": -0.002, "XRP": -0.006}                     # оба горячие
sent = []
s2 = make(shared, rets, sent)
s2.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
s2.handle_event({"event": "arm", "feed": "XRP", "dir": "down"})
time.sleep(1.4)
s2.handle_event({"event": "confirmed", "feed": "BTC"})
s2.handle_event({"event": "confirmed", "feed": "XRP"})
time.sleep(0.3)
btc_n = sum(1 for c in sent if c == "cd:0xbtc")
xrp_n = sum(1 for c in sent if c == "cd:0xxrp")
assert btc_n >= 2, (btc_n, xrp_n)                         # богатое окно молотит
assert xrp_n == 0, (btc_n, xrp_n)                         # бедное уступило все тики
assert any("yield" in m and "XRP" in m for m in LOGS)     # и сказало об этом в лог
assert any("window close" in m and "gap_p95" in m for m in LOGS)   # B3-метрика в close-строке
print("OK 4: богатое окно забрало слоты (%d проб), бедное уступило (%d), close-строка есть" % (btc_n, xrp_n))

# ---- 5) pending-handoff: огонь ВНЕ гейтов (холодный ret) и вне арбитра ----
sent2 = []
LOGS.clear()
rets2 = {"XRP": 0.0}                                      # окно холодное — обычных проб нет
s3 = make(types.SimpleNamespace(
    lock=threading.Lock(), groups={"0xxrp": ["0xMID"]},
    debt_by={("0xxrp", "0xMID"): 600_000.0}, by_id={"0xxrp": mk("cbXRP", "USDC")}), rets2, sent2)
s3.handle_event({"event": "arm", "feed": "XRP", "dir": "down"})
time.sleep(0.3)
assert not sent2                                          # холодно — молчим
s3.pending_fire["XRP"] = ("0xxrp", "0xMID", 600_000.0, time.monotonic())
time.sleep(0.5)
s3.handle_event({"event": "confirmed", "feed": "XRP"})
assert sent2 == ["cd:0xxrp"], sent2                       # выстрел по handoff, ровно один
assert s3.stats["pending_fires"] == 1
assert any("pending-handoff" in m for m in LOGS)
print("OK 5: pending-handoff стреляет по холодному окну, счётчик в stats")

# ---- 6) протухший handoff-флаг НЕ стреляет ----
sent3 = []
s4 = make(types.SimpleNamespace(
    lock=threading.Lock(), groups={"0xxrp": ["0xMID"]},
    debt_by={("0xxrp", "0xMID"): 600_000.0}, by_id={"0xxrp": mk("cbXRP", "USDC")}),
    {"XRP": 0.0}, sent3)
s4.handle_event({"event": "arm", "feed": "XRP", "dir": "down"})
s4.pending_fire["XRP"] = ("0xxrp", "0xMID", 600_000.0,
                          time.monotonic() - sfl_mod.PENDING_FIRE_FRESH_SEC - 1)
time.sleep(0.4)
s4.handle_event({"event": "confirmed", "feed": "XRP"})
assert not sent3
print("OK 6: протухший handoff-флаг глушится")

# ---- 7) кипэлайв A3: pending здоров -> on-chain пробы растягиваются до KEEPALIVE ----
sent4 = []
s5 = make(types.SimpleNamespace(
    lock=threading.Lock(), groups={"0xbtc": ["0xFAT"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0}, by_id={"0xbtc": mk("cbBTC", "USDC")}),
    {"BTC": -0.002}, sent4)
s5.pending_healthy_fn = lambda: True
s5.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(1.2)
s5.handle_event({"event": "confirmed", "feed": "BTC"})
assert len(sent4) == 1, sent4                             # 1 проба за 1.2с (кипэлайв 3.5с), не ~3
print("OK 7: кипэлайв — 1 проба вместо плотных (газ вниз, детект у pending)")

print("\nВСЕ БЛОКИ SCHED/ИНТЕГРАЦИИ: OK")
