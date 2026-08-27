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


# 1) _top_fat: ДВА РЕЖИМА (27.08). Ранг стал «долг x P(флип)», а не чистый долг: цель дальше
#    замеренного предела толчков фида не флипает вовсе и слот занимать не должна. Фикстура:
#    ЖИР $1.5M на 0.50%, КРОМКА $600k на 0.10%; замер BTC (журнал PREDICT, 3 суток): max 0.226%.
import chain.push_scale as _ps

# 1а) МИР, ГДЕ ЖИР ДОСЯГАЕМ (кольцо BTC знает толчки 0.6%): прежний контракт БИТ-В-БИТ —
#     top-1 по долгу, кромка отдельно. Без этого «кромка победила» неотличимо от «ранг сломан».
_ps._RINGS.clear()
for _i in range(_ps.MIN_N + 2):
    _ps.note_push("BTC", 0.60)
sf = make(mk_shared())
tgts, hold, edge = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
assert [t[1] for t in tgts] == ["0xFATWHALE"], tgts
assert edge is not None and edge[1] == "0xEDGESMALL", edge
print("UNIT_OK: досягаемый жир держит top-1 по долгу, кромка отдаётся отдельно (контракт цел)")

# 1б) МИР, ГДЕ ЖИР ДОСЯГАЕМ РЕДКО: кольцо знает 90 толчков 0.10% и 2 по 1.00%, то есть жир на
#     0.50% перекрывается лишь 2 пушами из 92 (вес 0.022), а кромка на 0.10% — всеми. Ожидание
#     $1.5M x 0.022 = $33k против $600k x 1.00 = $600k -> слот забирает кромка. Это и есть
#     каскад 26.08 в миниатюре: у нас стоял заёмщик на 2.27% (вес 0.021 по замеру XRP).
_ps._RINGS.clear()
for _i in range(90):
    _ps.note_push("BTC", 0.10)
for _i in range(2):
    _ps.note_push("BTC", 1.00)
sf = make(mk_shared())
tgts, hold, edge = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
assert [t[1] for t in tgts] == ["0xEDGESMALL"], (
    "жир, досягаемый 2 пушами из 92, обязан уступить слот кромке: %s" % (tgts,))
assert edge is not None and edge[1] == "0xEDGESMALL", edge
print("UNIT_OK: редко-досягаемый жир уступает слот кромке (вес 0.022 против 1.00)")

# 1в) БЕЗ ЗАМЕРА — прежний ранг по долгу бит-в-бит (замера нет -> p_flip None -> вес 1.0 всем)
_ps._RINGS.clear()
sf = make(mk_shared())
tgts, _, _ = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
assert [t[1] for t in tgts] == ["0xFATWHALE"], (
    "без замера ранг обязан деградировать к долгу: %s" % (tgts,))
print("UNIT_OK: без замера — прежний ранг по долгу (деградация, а не молчание)")

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
# Ротация «жир + кромка» — контракт, ОРТОГОНАЛЬНЫЙ досягаемости, поэтому мир фикстуры делаем
# таким, где досягаемы ОБЕ (толчки 0.6% при жире на 0.50%). Иначе проверялась бы не ротация,
# а гейт досягаемости, у которого есть свой стенд (test_push_scale_reach.py).
_ps._RINGS.clear()
for _i in range(_ps.MIN_N + 2):
    _ps.note_push("BTC", 0.60)
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
