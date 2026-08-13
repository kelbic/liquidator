"""Юниты EDGE-СЛОТА ротации (06.08, гонка №2 cbXRP: 119 проб в top-1-по-долгу, флипнулась
ближайшая-по-dist мелочь с 0 проб). Контракты: _top_fat отдаёт кромку, head ротации несёт
top-K по долгу + гарантированный edge-слот, дедуп при совпадении, откат SPEC_FIRE_EDGE_SLOT=0,
fail-open без hf_by. Без сети."""
import re
import sys
import threading
import time
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive

sfl_mod.SPEC_FIRE_PEAK_OWED = False               # тестируем до-owed семантику (как test_spec_fire_live)

LOGS = []
RET = {"v": 0.0}
sent = []


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)


# ЖИР $1.5M dist=0.50% (hf=1.00503), КРОМКА $600k dist=0.10% (hf=1.001) — обе >= MIN_REPAID
# (дефолт $500k) и обе досягаемы при ret=-0.5% (reach = 0.5% + 2×dev 0.1% = 0.7%).
def mk_shared(hf_fat=1.00503, hf_edge=1.001, edge_debt=600_000.0):
    return types.SimpleNamespace(
        lock=threading.Lock(),
        groups={"0xbtc": ["0xFATWHALE", "0xEDGESMALL"]},
        debt_by={("0xbtc", "0xFATWHALE"): 1_500_000.0, ("0xbtc", "0xEDGESMALL"): edge_debt},
        hf_by={("0xbtc", "0xFATWHALE"): hf_fat, ("0xbtc", "0xEDGESMALL"): hf_edge},
        by_id={"0xbtc": mk("cbBTC", "USDC")},
    )


cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                            tip_gwei=0.05, sequencer_rpc="https://seq", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)


def build_fn(mid, b, d):
    return {"calldata": "0xdeadbeef", "gas": 625_000}


def send_fn(calldata, gas):
    sent.append((calldata, gas))
    return "0xhash%d" % len(sent)


def make(shared, **kw):
    kw.setdefault("build_fn", build_fn)
    kw.setdefault("send_fn", send_fn)
    kw.setdefault("receipt_fn", lambda h: (0, 0.011))
    kw.setdefault("log_fn", LOGS.append)
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared,
                        ret_fn=lambda lbl: RET["v"], dev_by={"BTC": 0.0010}, **kw)


def probed_borrowers():
    out = []
    for m in LOGS:
        mm = re.search(r"SPECFIRE probe #\d+ feed=\S+ \S+/(\S+) ", m)
        if mm:
            out.append((mm.group(1), "[edge]" in m))
    return out


# 1) _top_fat: кромка возвращается отдельно, даже когда она не жирнейшая (k=1)
sf = make(mk_shared())
tgts, hold, edge = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
assert [t[1] for t in tgts] == ["0xFATWHALE"], tgts
assert edge is not None and edge[1] == "0xEDGESMALL", edge
print("UNIT_OK: _top_fat отдаёт top-1 по долгу И кромку по dist отдельно")

# 1b) hf<=1.0 (уже за флипом) = dist 0 — кромка вне конкуренции
sf1b = make(mk_shared(hf_edge=0.9990))
_, _, edge1b = sf1b._top_fat("BTC", "down", max_dist=0.03, k=1)
assert edge1b is not None and edge1b[1] == "0xEDGESMALL", edge1b
print("UNIT_OK: hf<=1 (за флипом) даёт dist=0 — кромка гарантирована")

# 1c) fail-open: без hf_by кромка не вычислима -> edge=None (поведение прежнее)
no_hf = mk_shared()
del no_hf.hf_by
sf1c = make(no_hf)
tgts1c, _, edge1c = sf1c._top_fat("BTC", "down", max_dist=0.03, k=1)
assert edge1c is None and len(tgts1c) == 1, (tgts1c, edge1c)
print("UNIT_OK: без hf_by edge=None (fail-open, чистый ранг по долгу)")

# 2) живой огонь: обе цели получают пробы, кромка помечена [edge]
sent.clear(); LOGS.clear(); RET["v"] = -0.005
old_iv, old_j = sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC, sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC
sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC = 0.05       # пол max(0.25,...) удержит 0.25
sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC = 0.0
sf2 = make(mk_shared())
sf2.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(1.4)
sf2.handle_event({"event": "disarm", "feed": "BTC"})
pb = probed_borrowers()
who = {b for b, _ in pb}
assert "0xFATWHALE"[:10] in who and "0xEDGESMALL"[:10] in who, pb
assert all(is_e for b, is_e in pb if b == "0xEDGESMALL"[:10]), pb
assert not any(is_e for b, is_e in pb if b == "0xFATWHALE"[:10]), pb
print("UNIT_OK: ротация чередует жир и кромку, пробы кромки тегированы [edge] (%d проб)" % len(pb))

# 3) дедуп: кромка == жирнейшая -> head из одной цели, проб дубль нет
sent.clear(); LOGS.clear()
solo = types.SimpleNamespace(
    lock=threading.Lock(), groups={"0xbtc": ["0xFATWHALE"]},
    debt_by={("0xbtc", "0xFATWHALE"): 1_500_000.0},
    hf_by={("0xbtc", "0xFATWHALE"): 1.001},
    by_id={"0xbtc": mk("cbBTC", "USDC")})
sf3 = make(solo)
sf3.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.7)
sf3.handle_event({"event": "disarm", "feed": "BTC"})
pb3 = probed_borrowers()
assert pb3 and all(b == "0xFATWHALE"[:10] for b, _ in pb3), pb3
print("UNIT_OK: кромка==жир — одна цель в head, дублей нет")

# 4) ОТКАТ SPEC_FIRE_EDGE_SLOT=0: чистый top-1 по долгу, кромка проб не получает
sent.clear(); LOGS.clear()
sfl_mod.SPEC_FIRE_EDGE_SLOT = False
sf4 = make(mk_shared())
sf4.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.9)
sf4.handle_event({"event": "disarm", "feed": "BTC"})
pb4 = probed_borrowers()
assert pb4 and all(b == "0xFATWHALE"[:10] for b, _ in pb4), pb4
sfl_mod.SPEC_FIRE_EDGE_SLOT = True
sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC = old_iv
sfl_mod.SPEC_FIRE_DENSE_JITTER_SEC = old_j
print("UNIT_OK: откат SPEC_FIRE_EDGE_SLOT=0 возвращает чистый ранг по долгу")

print("ALL_OK: edge-слот — все контракты выполнены")
