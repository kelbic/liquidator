"""Юниты гибрид-детекта каскада (калибровка 31.07: армы И факты) + вотчера фактов. Без сети."""
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, ".")
import chain.spec_fire_live as sfl_mod
import chain.execute as ex
from chain.spec_fire_live import SpecFireLive, _facts_fresh

# 1) _facts_fresh: свежий флаг True, старый False, нет файла False (fail-closed)
fd, flag = tempfile.mkstemp()
os.close(fd)
old_flag = sfl_mod.SPEC_FIRE_FACTS_FLAG
sfl_mod.SPEC_FIRE_FACTS_FLAG = flag
os.utime(flag, (time.time(), time.time()))
assert _facts_fresh() is True
os.utime(flag, (time.time() - 1000, time.time() - 1000))     # старше TTL 900
assert _facts_fresh() is False
os.remove(flag)
assert _facts_fresh() is False, "нет флага = fail-closed"
print("UNIT_OK: факт-флаг — свежий/устаревший/отсутствующий (fail-closed)")

# 2) откат SPEC_FIRE_HYBRID=0: фактов не требуется (бустер по одним армам)
sfl_mod.SPEC_FIRE_HYBRID = False
assert _facts_fresh() is True, "легаси-откат обязан открыть гейт без флага"
sfl_mod.SPEC_FIRE_HYBRID = True
print("UNIT_OK: откат SPEC_FIRE_HYBRID=0 возвращает армы-только")

# 3) интеграция арм-детекта: 4 арма БЕЗ флага -> бустер молчит + лог fail-closed;
#    с флагом -> бустер взводится
def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l)

shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0},
    by_id={"0xbtc": mk("cbBTC", "USDC")},
)
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                            tip_gwei=0.05, sequencer_rpc="https://seq", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)
LOGS = []
def make():
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda l: -0.0001,
                        dev_by={"BTC": 0.001}, build_fn=lambda m, b, d: None,
                        send_fn=lambda c, g: None, receipt_fn=lambda h: (0, 0.0),
                        log_fn=LOGS.append)

ex._SPECFIRE_CASCADE_GAS_USD = 100.0
ex._SPECFIRE_CASCADE_UNTIL = 0.0
sf = make()
for _ in range(sfl_mod.SPEC_FIRE_CASCADE_WINDOWS):
    sf.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
sf.handle_event({"event": "disarm", "feed": "BTC"})
assert ex._SPECFIRE_CASCADE_UNTIL == 0.0, "без факт-флага бустер обязан молчать"
assert any("БЕЗ факт-флага" in m for m in LOGS), LOGS

fd, flag2 = tempfile.mkstemp()
os.close(fd)
sfl_mod.SPEC_FIRE_FACTS_FLAG = flag2
LOGS.clear()
sf2 = make()
for _ in range(sfl_mod.SPEC_FIRE_CASCADE_WINDOWS):
    sf2.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
sf2.handle_event({"event": "disarm", "feed": "BTC"})
assert ex._SPECFIRE_CASCADE_UNTIL > 0.0, "армы + свежий флаг обязаны взвести бустер"
assert any("каскад-детект" in m and "факт-флаг" in m for m in LOGS), LOGS
os.remove(flag2)
ex._SPECFIRE_CASCADE_GAS_USD = 0.0
ex._SPECFIRE_CASCADE_UNTIL = 0.0
sfl_mod.SPEC_FIRE_FACTS_FLAG = old_flag
print("UNIT_OK: гибрид — армы без фактов молчат, армы+факты взводят бустер")

# ---- вотчер фактов ----
import analysis.cascade_facts_watch as w

# 4) count_api: корректный парс countTotal; ошибка API бросает
class _Resp:
    def __init__(self, payload):
        self._p = payload
    def read(self):
        import json as _j
        return _j.dumps(self._p).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def mk_urlopen(payload):
    def _u(req, timeout=None):
        return _Resp(payload)
    return _u

n = w.count_api(["0xaa"], now=1000000,
                _urlopen=mk_urlopen({"data": {"marketTransactions": {"pageInfo": {"countTotal": 7}}}}))
assert n == 7
try:
    w.count_api(["0xaa"], now=1000000, _urlopen=mk_urlopen({"errors": [{"message": "boom"}]}))
    raise AssertionError("ошибка API обязана бросать")
except RuntimeError:
    pass
print("UNIT_OK: count_api парсит countTotal, ошибки бросают (не тихий 0)")

# 5) count_logs: фильтр по topics[1] среди наших ids, диапазон 1800 блоков
calls = []
def fake_rpc(url, method, params, **kw):
    calls.append((method, params))
    if method == "eth_blockNumber":
        return hex(50_000_000)
    return [
        {"topics": [w.LIQUIDATE_TOPIC, "0xAA"]},
        {"topics": [w.LIQUIDATE_TOPIC, "0xbb"]},
        {"topics": [w.LIQUIDATE_TOPIC, "0xcc"]},
        {"topics": [w.LIQUIDATE_TOPIC]},
    ]
n = w.count_logs(["0xaa", "0xbb"], "http://rpc", _rpc_fn=fake_rpc)
assert n == 2, n
flt = calls[1][1][0]
assert int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) == 1800, "диапазон = час (<=2k drpc)"
print("UNIT_OK: count_logs — фильтр наших ids, диапазон 1800 блоков")

# 6) main: API умер -> фолбэк logs; оба умерли -> rc=1 и флаг НЕ трогается (fail-closed);
#    порог: n>=3 -> sink('flag'), n<3 -> sink не зовётся
sink_ops = []
def fake_sink(op, **kw):
    sink_ops.append(op)
    return "0xaa\n0xbb\n" if op == "ids" else ""

def run_main(api_fn, logs_fn, env_rpc="http://bg"):
    sink_ops.clear()
    old = (w.count_api, w.count_logs, w._ssh_sink, w.covered_ids)
    w.count_api = api_fn
    w.count_logs = logs_fn
    w._ssh_sink = fake_sink
    w.covered_ids = lambda now=None, _ssh=None: ["0xaa", "0xbb"]
    os.environ["BG_RPC_URL"] = env_rpc
    try:
        return w.main()
    finally:
        w.count_api, w.count_logs, w._ssh_sink, w.covered_ids = old
        os.environ.pop("BG_RPC_URL", None)

def boom(*a, **kw):
    raise RuntimeError("dead")

rc = run_main(boom, lambda ids, url: 5)
assert rc == 0 and "flag" in sink_ops, (rc, sink_ops)
rc = run_main(boom, boom)
assert rc == 1 and "flag" not in sink_ops, "оба источника умерли -> fail-closed, rc=1"
rc = run_main(lambda ids, now=None: 1, boom)
assert rc == 0 and "flag" not in sink_ops, "n<порога -> флаг не трогаем"
rc = run_main(lambda ids, now=None: 3, boom)
assert rc == 0 and "flag" in sink_ops, "n>=3 -> флаг поднят"
print("UNIT_OK: main — фолбэк-порядок, fail-closed при полной слепоте, порог 3")

print("=== ВСЕ CASCADE-HYBRID ЮНИТЫ ПРОШЛИ ===")
