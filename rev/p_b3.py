"""НЕЗАВИСИМАЯ ПРОВЕРКА Б3: окно коллизии nonce + сценарий двух тредов на одной ноге."""
import pathlib, sys, threading, types, time
ROOT = pathlib.Path("/home/claude-agent/work/liquidator/.claude/worktrees/wf_73589e1f-2ff-2")
sys.path.insert(0, str(ROOT))
import chain.execute as ex
from eth_account import Account

KEY = "0x" + "3c" * 32
ACCT = Account.from_key(KEY); ADDR = ACCT.address
GAS = 900_000; BASEFEE = int(0.005e9)
cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)
PENDING = [100]

class _Eth:
    account = Account
    def get_block(self, _b): return {"baseFeePerGas": BASEFEE}
    def get_balance(self, _a): return int(1e18)
    def get_transaction_count(self, _a, _b="pending"): return PENDING[0]
class _W3: eth = _Eth()
class _Rpc:
    chain_id = 8453
    def _web3(self): return _W3()

# --- сетевой стык (НИЖЕ дефекта): пост блокируется, пока тест не разрешит
GATE = threading.Event()
FAIL = {"on": False}
class _Resp:
    def json(self):
        if FAIL["on"]:
            return {"error": {"message": "over rate limit"}}
        return {"result": "0xdead"}
class _Sess:
    def post(self, url, json=None, timeout=None):
        GATE.wait(5)
        return _Resp()
ex._seq_session = lambda url: _Sess()

ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
ex._VOLLEY_BAL[ADDR] = (int(1e18), time.monotonic())
ex._VOLLEY_BAL_MAX_AGE = 1e9
ex.invalidate_ladder(ADDR); ex._VOLLEY_NONCE.pop(ADDR, None)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=6, base_fee=BASEFEE, start_nonce=100)
assert ex._VOLLEY_LADDER[ADDR]["q"][0][0] == 100

# ---- сценарий 1: проба 100 в полёте, идёт ПОЛНАЯ пересборка (pending отстаёт) ----
res = {}
t = threading.Thread(target=lambda: res.update(h=ex.send_presigned_probe(
    _Rpc(), cfg, ADDR, log=None, broadcast_raw_urls=["https://seq/x"])))
t.start(); time.sleep(0.25)
print("счётчик во время полёта:", ex._VOLLEY_NONCE.get(ADDR), "(pending узла", PENDING[0], ")")
ex.invalidate_ladder(ADDR)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=4, base_fee=BASEFEE)
first = ex._VOLLEY_LADDER[ADDR]["q"][0][0]
print("СЦ1 первый nonce новой лестницы:", first, "->", "ОК" if first == 101 else "КОЛЛИЗИЯ")
GATE.set(); t.join()

# ---- сценарий 2: два треда на одной ноге, один ОТКАЗ, второй УСПЕХ ----
GATE.clear(); FAIL["on"] = False
ex.invalidate_ladder(ADDR); ex._VOLLEY_NONCE.pop(ADDR, None); PENDING[0] = 200
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=6, base_fee=BASEFEE, start_nonce=200)
G1, G2 = threading.Event(), threading.Event()
class _Sess2:
    def post(self, url, json=None, timeout=None):
        n = threading.current_thread().name
        if n == "T1":
            G1.wait(5); return _RespFail()
        G2.wait(5); return _Resp()
class _RespFail:
    def json(self): return {"error": {"message": "over rate limit"}}
ex._seq_session = lambda url: _Sess2()
out = {}
t1 = threading.Thread(name="T1", target=lambda: out.__setitem__("t1", ex.send_presigned_probe(
    _Rpc(), cfg, ADDR, log=None, broadcast_raw_urls=["https://seq/x"])))
t2 = threading.Thread(name="T2", target=lambda: out.__setitem__("t2", ex.send_presigned_probe(
    _Rpc(), cfg, ADDR, log=None, broadcast_raw_urls=["https://seq/x"])))
t1.start(); time.sleep(0.1); t2.start(); time.sleep(0.1)
print("СЦ2 после изъятия обоих: счётчик =", ex._VOLLEY_NONCE.get(ADDR), "(ожидаем 202)")
G2.set(); t2.join()                      # 201 УСПЕШНО ушёл
print("СЦ2 после успеха T2(201): счётчик =", ex._VOLLEY_NONCE.get(ADDR))
G1.set(); t1.join()                      # 200 ОТКАЗ -> откат вниз
print("СЦ2 после отказа T1(200): счётчик =", ex._VOLLEY_NONCE.get(ADDR),
      "  (201 УЖЕ у секвенсера)")
ex.invalidate_ladder(ADDR)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=4, base_fee=BASEFEE)
f2 = ex._VOLLEY_LADDER[ADDR]["q"][0][0]
nn = [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]]
print("СЦ2 пересборка после этого:", nn,
      "-> КОЛЛИЗИЯ с уже отправленным 201" if 201 in nn else "-> ОК")
