"""МАЙОР C (ревью р-4): дельта окна обязана принадлежать СВОЕМУ окну — каскад держит их РЯДОМ.

ЧТО БЫЛО. Снимок брался на арме КАЖДОГО окна, но счётчики позиций жили в ОБЩЕМ `self.stats`, а
`_note_probe_position` зовётся из треда КОЛЛЕКТОРА, не из треда окна. В каскаде окна разных
фидов живут ОДНОВРЕМЕННО по построению, поэтому дельта окна BTC вбирала пробы окна ETH: зрячее
жирное окно несло чужой `probe_no_agg=1` и приёмка (б) читалась ПРОВАЛЕННОЙ ровно на том окне,
ради которого пакет и делался. Штатный `test_probe_window_verdict` этого не ловит: он гоняет
окна ПОСЛЕДОВАТЕЛЬНО.

ПОЧЕМУ ПРОВЕРКА ДВУСТОРОННЯЯ. «probe_no_agg=0 в окне BTC» — число, которое умеет вырасти при
бесполезном механизме: если пробы вообще перестанут учитываться, ноль будет тем же нулём.
Поэтому окно BTC обязано ОДНОВРЕМЕННО показать `probe_judged>=1` (свой суд состоялся), а окно
ETH — свой законный `probe_no_agg=1` (чужой суд не потерян, он просто не наш).

НАСТОЯЩИЕ: два `_window_loop` в ДВУХ тредах, `_probe_snap`/`_probe_delta`,
`_note_probe_position`, `_agg_of_market`, `feeds.resolve_feeds`, `ChainIndex` на записанном
блоке 49890266. Подменены только сокет узла и I/O-шов резолва ног.
"""
import gzip
import json
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402
import chain.feeds as feeds                                   # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
BN = 49890266
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
AGG_PRIZE = "0x9491aedfe0da70eb110582b66b2edace28d82a5d"
AGG_LEG2 = "0x" + "c3" * 20
MID_SEEN = "0x" + "11" * 32
MID_BLIND = "0x" + "22" * 32

BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS_FX = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RECEIPTS = json.loads((FIX / "receipts_49890266.json").read_text())
H_FIRE = next(rc["transactionHash"] for rc in RECEIPTS if rc["to"].lower() == BOARD)[2:]


def _http(url, payload, timeout):
    m = payload["method"]
    if m == "eth_getLogs":
        return {"result": LOGS_FX}
    if m == "eth_getBlockByNumber":
        return BLOCK
    if m == "eth_getBlockReceipts":
        return {"result": RECEIPTS}
    if m == "eth_blockNumber":
        return {"result": hex(BN)}
    raise AssertionError(m)


idx = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=_http,
                    now=lambda: 0.0, log_fn=lambda m: None)
idx.note_hash(H_FIRE, head=BN - 1)
idx.scan_block(BN)
FACTS = idx.block_facts(BN)
assert FACTS and len(FACTS["ours"]) == 33, (FACTS or {}).get("ours")

_sv_legs = feeds._resolve_legs
_mkt = lambda mid: types.SimpleNamespace(market_id=mid)                       # noqa: E731
try:
    feeds._resolve_legs = lambda rpc, mid, fb=None: (
        (AGG_PRIZE, [AGG_PRIZE, AGG_LEG2]) if mid == MID_SEEN else (None, []))
    feeds.resolve_feeds(None, [_mkt(MID_SEEN), _mkt(MID_BLIND)])
finally:
    feeds._resolve_legs = _sv_legs

H, F = sorted(FACTS["ours"].items(), key=lambda kv: kv[1]["index"])[0]
RCPT = ci.receipt_like(F)

_shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
_cfg = types.SimpleNamespace(liquidator_address=BOARD, gas_limit_est=500_000, tip_gwei=0.05,
                             sequencer_rpc="off", eth_price_usd=3000.0,
                             fast_lane_max_repaid_usd=500_000.0)


def run_pair():
    """Два окна ВНАХЛЁСТ: BTC судит свой рынок зряче, ETH — свой законно вслепую."""
    log = []
    _sv_board, _sv_idx = sfl.SPEC_FIRE_V2_BOARD, ci._INDEX
    ci._INDEX = idx
    try:
        sfl.SPEC_FIRE_V2_BOARD = False
        live = sfl.SpecFireLive(rpc=None, cfg=_cfg, shared=_shared, ret_fn=lambda lbl: 0.0,
                                dev_by={"BTC": 0.001, "ETH": 0.001},
                                build_fn=lambda m, b, d: None, send_fn=lambda c, g: None,
                                receipt_fn=lambda h: (0, 0.0), log_fn=log.append)
        stopA, stopB = threading.Event(), threading.Event()
        tA = threading.Thread(target=live._window_loop, args=("BTC", "down", stopA), daemon=True)
        tA.start()
        time.sleep(0.10)
        tB = threading.Thread(target=live._window_loop, args=("ETH", "down", stopB), daemon=True)
        tB.start()
        time.sleep(0.05)
        # В ОКНЕ BTC села НАША проба по НАШЕМУ рынку (владелец транзита известен),
        # в окне ETH — законный слепой суд (рынок без резолвнутых ног).
        live._note_probe_position(H, RCPT, meta=("0xleg", ("BTC", MID_SEEN, "0xb", 1.0)))
        live._note_probe_position(H, RCPT, meta=("0xleg", ("ETH", MID_BLIND, "0xb", 1.0)))
        time.sleep(0.05)
        stopB.set(); tB.join(timeout=5)
        time.sleep(0.05)
        stopA.set(); tA.join(timeout=5)
    finally:
        sfl.SPEC_FIRE_V2_BOARD = _sv_board
        ci._INDEX = _sv_idx
    out = {}
    for m in [x for x in log if "window close" in x]:
        out[m.split("feed=")[1].split(" ")[0]] = m.split("окно_позиции=")[1].split(" stats=")[0]
    return live, out


live, win = run_pair()
assert set(win) == {"BTC", "ETH"}, win
assert "'probe_no_agg': 0" in win["BTC"], \
    ("окно BTC судило ТОЛЬКО свой рынок зряче — чужой слепой суд не имеет права его "
     "проваливать: %s" % win["BTC"])
assert "'probe_judged': 1" in win["BTC"], \
    ("и это не пустой ноль: суд СВОЕЙ пробы обязан быть в дельте окна, иначе «0» означает "
     "«ничего не мерили»: %s" % win["BTC"])
assert "'probe_no_agg': 1" in win["ETH"] and "'probe_judged': 1" in win["ETH"], \
    ("слепой суд не теряется — он принадлежит СВОЕМУ окну: %s" % win["ETH"])
assert live.stats["probe_no_agg"] == 1 and live.stats["probe_judged"] == 2, live.stats
print("UNIT_OK: два окна ВНАХЛЁСТ судятся раздельно — BTC %s, ETH %s (кумулятив процесса "
      "probe_no_agg=%d)" % (win["BTC"], win["ETH"], live.stats["probe_no_agg"]))

# ---------------------------------------------------------------- проба БЕЗ фида не исчезает
_sv_board, _sv_idx = sfl.SPEC_FIRE_V2_BOARD, ci._INDEX
ci._INDEX = idx
log2 = []
try:
    sfl.SPEC_FIRE_V2_BOARD = False
    live2 = sfl.SpecFireLive(rpc=None, cfg=_cfg, shared=_shared, ret_fn=lambda lbl: 0.0,
                             dev_by={"BTC": 0.001}, build_fn=lambda m, b, d: None,
                             send_fn=lambda c, g: None, receipt_fn=lambda h: (0, 0.0),
                             log_fn=log2.append)
    stop = threading.Event()
    t = threading.Thread(target=live2._window_loop, args=("BTC", "down", stop), daemon=True)
    t.start()
    time.sleep(0.10)
    live2._note_probe_position(H, RCPT, meta=("0xleg", (None, MID_BLIND, "0xb", 1.0)))
    time.sleep(0.05)
    stop.set(); t.join(timeout=5)
finally:
    sfl.SPEC_FIRE_V2_BOARD = _sv_board
    ci._INDEX = _sv_idx
_close = [m for m in log2 if "window close" in m]
_w = _close[0].split("окно_позиции=")[1].split(" stats=")[0]
assert "'probe_no_feed': 1" in _w, \
    ("проба без фида не имеет права раствориться: ноль в probe_no_agg тогда означал бы «всё "
     "сужено зряче», хотя часть судов вообще не привязана к окну — %s" % _w)
print("UNIT_OK: суд без фида цели НАЗВАН отдельным ключом probe_no_feed в строке окна (%s) — "
      "незнание закрывает гард, а не открывает его" % _w)

# ---------------------------------------------------------------- МУТАЦИЯ: вернуть общий учёт
_sv_snap, _sv_delta = sfl.SpecFireLive._probe_snap, sfl.SpecFireLive._probe_delta
try:
    def _snap_global(self, feed=None):
        return {k: int(self.stats.get(k, 0) or 0) for k in self._PROBE_KEYS}

    def _delta_global(self, snap, feed=None):
        cur = _snap_global(self)
        out = {}
        for k in self._PROBE_KEYS:
            v = cur[k] - int((snap or {}).get(k, 0))
            if v or k in self._PROBE_ALWAYS:
                out[k] = v
        return out

    sfl.SpecFireLive._probe_snap = _snap_global
    sfl.SpecFireLive._probe_delta = _delta_global
    _live, _win = run_pair()
    assert "'probe_no_agg': 0" not in _win["BTC"], \
        ("мутация «дельта на ОБЩИХ счётчиках» (редакция до р-5) ОБЯЗАНА краснить стенд: иначе "
         "тест не держит ничего — %s" % _win["BTC"])
finally:
    sfl.SpecFireLive._probe_snap = _sv_snap
    sfl.SpecFireLive._probe_delta = _sv_delta
print("UNIT_OK: мутация (общие счётчики, как было) краснит стенд — окно BTC снова несёт чужой "
      "probe_no_agg")

print("ПРОШЛИ: приёмка (б) на жирном окне не зависит от того, что делает соседнее окно каскада")
