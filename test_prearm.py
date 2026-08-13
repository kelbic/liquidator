"""Юниты PRE-ARM Stage A: полоса кэша, TTL, потребление в kyber_swap БЕЗ HTTP, воркер (швы). Без сети."""
import sys
import threading
import time
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.prearm as pa
import chain.execute as ex

SWAP = {"router": "0xR", "calldata": "0xCD", "amount_in": 970, "amount_out": 1200, "min_out": 1188}

# 1) полоса [BAND..1.0]: cached=970 хит для req 970..1000, мисс для 969 (cached>req) и 1200 (cached<0.97*req)
pa.prearm_clear()
pa.prearm_put("0xCOLL", "0xLOAN", SWAP)
assert pa.prearm_get("0xcoll", "0xloan", 1000) is not None, "970/1000=0.97 — нижняя граница хит"
assert pa.prearm_get("0xCOLL", "0xLOAN", 970) is not None, "ровно равен — хит"
assert pa.prearm_get("0xCOLL", "0xLOAN", 969) is None, "cached > req — контракт бы ревертнул, МИСС"
assert pa.prearm_get("0xCOLL", "0xLOAN", 1200) is None, "недобор >3% — мисс"
print("UNIT_OK: полоса [0.97..1.0] и регистронезависимый ключ")

# 2) TTL: старая запись не отдаётся
pa.prearm_clear()
pa.prearm_put("0xA", "0xB", SWAP, now=time.monotonic() - pa.PREARM_TTL_SEC - 1)
assert pa.prearm_get("0xA", "0xB", 1000) is None
print("UNIT_OK: TTL истёк — мисс")

# 3) потребление: kyber_swap отдаёт кэш и НЕ ходит в HTTP (сломанный _get как страж)
pa.prearm_clear()
pa.prearm_put("0xIN", "0xOUT", SWAP)
_real_get = ex._get
ex._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP FORBIDDEN IN TEST"))
try:
    out = ex.kyber_swap("0xIN", "0xOUT", 1000, "0xS", "0xS")
    assert out["router"] == "0xR" and out["calldata"] == "0xCD", out
    # мимо полосы -> должен пойти в HTTP -> страж бросит
    try:
        ex.kyber_swap("0xIN", "0xOUT", 5000, "0xS", "0xS")
        raise AssertionError("ожидали HTTP-страж")
    except RuntimeError as e:
        assert "FORBIDDEN" in str(e)
finally:
    ex._get = _real_get
print("UNIT_OK: kyber_swap хит из кэша без HTTP; мимо полосы — обычный путь")

# 4) воркер: arm стартует прогрев top-K по фиду/направлению, disarm стопит
def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)

shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xm_weeth": ["0xW"], "0xm_btc": ["0xF", "0xtiny"]},
    debt_by={("0xm_weeth", "0xW"): 450_000.0, ("0xm_btc", "0xF"): 600_000.0,
             ("0xm_btc", "0xtiny"): 500.0},
    by_id={"0xm_weeth": mk("weETH", "USDC"), "0xm_btc": mk("cbBTC", "USDC")},
)
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", fast_lane_max_repaid_usd=500_000.0)
reads, quotes = [], []

def read_fn(mid, b):
    reads.append((mid, b))
    return {"coll": "0xc_" + mid[-4:], "loan": "0xl", "seized": 1_000_000,
            "routed": (mid == "0xm_btc")}    # cbBTC роутнут, weETH нет

def quote_fn(coll, loan, seized):
    quotes.append((coll, seized))
    return {"router": "0xR", "calldata": "0xCD", "amount_in": seized, "amount_out": 1, "min_out": 1}

pa.prearm_clear()
w = pa.PreArmWorker(rpc=None, cfg=cfg, shared=shared, read_fn=read_fn, quote_fn=quote_fn,
                    log_fn=lambda m: None)
w.handle_event({"event": "arm", "feed": "ETH", "dir": "down"})     # weETH driven (down, залог ETH)
time.sleep(0.3)
w.handle_event({"event": "disarm", "feed": "ETH"})
assert ("0xm_weeth", "0xW") in reads, reads
assert any(c == "0xc_eeth" for c, _ in quotes), quotes             # weETH прогрет (не роутнут)
assert pa.prearm_get("0xc_eeth", "0xl", 1_000_000) is not None
print("UNIT_OK: ETH-окно греет weETH-цель, кэш наполнен")

# 5) BTC down: жир $600k на роутнутой паре >= fast_lane_max -> ГРЕЕТСЯ (sim-путь пойдёт в Kyber);
#    пыль $500 отфильтрована PREARM_MIN_DEBT_USD
reads.clear(); quotes.clear()
w.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
time.sleep(0.3)
w.handle_event({"event": "confirmed", "feed": "BTC"})
assert ("0xm_btc", "0xF") in reads and ("0xm_btc", "0xtiny") not in reads, reads
assert any(c == "0xc__btc" for c, _ in quotes), quotes
print("UNIT_OK: routed-мега греется, пыль отфильтрована, confirmed стопит окно")

# 6) окно останавливается: после disarm новых прогревов нет
n = len(quotes)
time.sleep(0.4)
assert len(quotes) == n, "прогрев после stop"
print("UNIT_OK: stop-событие реально останавливает воркер")

# 7) баг слотов (28.07, SOL-окно 01:06): роутнутые не-мега НЕ занимают top-K —
#    негретая мелкая цель без статик-роута обязана попасть в выборку
ex._SWAP_ROUTES[("0xrc", "0xrl")] = ("dummy",)      # помечаем пару роутнутой
try:
    shared_slots = types.SimpleNamespace(
        lock=threading.Lock(),
        groups={"0xm_routed": ["0xR1", "0xR2", "0xR3"], "0xm_usol": ["0xU"]},
        debt_by={("0xm_routed", "0xR1"): 100_000.0, ("0xm_routed", "0xR2"): 90_000.0,
                 ("0xm_routed", "0xR3"): 80_000.0, ("0xm_usol", "0xU"): 4_000.0},
        by_id={"0xm_routed": types.SimpleNamespace(collateral_token="0xRC", loan_token="0xRL"),
               "0xm_usol": types.SimpleNamespace(collateral_token="0xUC", loan_token="0xUL")},
    )
    _lbl = pa._label_of
    pa._label_of = lambda t: "SOL" if t in ("0xRC", "0xUC") else "USDC"
    try:
        w2 = pa.PreArmWorker(rpc=None, cfg=cfg, shared=shared_slots,
                             read_fn=read_fn, quote_fn=quote_fn, log_fn=lambda m: None)
        picked = w2._pick("SOL", "down")
    finally:
        pa._label_of = _lbl
    assert ("0xm_usol", "0xU", 4_000.0) in picked, picked
    assert not any(mid == "0xm_routed" for mid, _, _ in picked), \
        "роутнутые не-мега обязаны фильтроваться ДО top-K"
finally:
    ex._SWAP_ROUTES.pop(("0xrc", "0xrl"), None)
print("UNIT_OK: слоты top-K не съедаются роутнутыми не-мега — uSOL-цель греется")

print("=== ВСЕ PRE-ARM ЮНИТЫ ПРОШЛИ ===")
