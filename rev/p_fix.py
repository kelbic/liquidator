"""Проверка предлагаемого гарда: откат счётчика ТОЛЬКО если его никто не двигал."""
import pathlib, sys, threading, types, time
ROOT = pathlib.Path("/home/claude-agent/work/liquidator/.claude/worktrees/wf_73589e1f-2ff-2")
sys.path.insert(0, str(ROOT))
OLD = ('                _cur = _VOLLEY_NONCE.get(addr)\n'
       '                if _cur is not None and int(_cur) > int(nonce):\n')
NEW = ('                _cur = _VOLLEY_NONCE.get(addr)\n'
       '                if _cur is not None and int(_cur) == int(nonce) + 1:\n')
src = (ROOT / "chain/execute.py").read_text()
assert src.count(OLD) == 1
m = types.ModuleType("chain._exec_guard"); m.__file__ = str(ROOT / "chain/execute.py")
sys.modules["chain._exec_guard"] = m
exec(compile(src.replace(OLD, NEW), m.__file__, "exec"), m.__dict__)
ex = m
from eth_account import Account
KEY = "0x" + "3c" * 32; ACCT = Account.from_key(KEY); ADDR = ACCT.address
GAS = 900_000; BASEFEE = int(0.005e9); PENDING = [200]
cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)
class _Eth:
    account = Account
    def get_block(self, _b): return {"baseFeePerGas": BASEFEE}
    def get_balance(self, _a): return int(1e18)
    def get_transaction_count(self, _a, _b="pending"): return PENDING[0]
class _W3: eth = _Eth()
class _Rpc:
    chain_id = 8453
    def _web3(self): return _W3()
G1, G2 = threading.Event(), threading.Event()
class _Ok:
    def json(self): return {"result": "0x1"}
class _Bad:
    def json(self): return {"error": {"message": "over rate limit"}}
class _S:
    def post(self, url, json=None, timeout=None):
        if threading.current_thread().name == "T1":
            G1.wait(5); return _Bad()
        G2.wait(5); return _Ok()
ex._seq_session = lambda u: _S()
ex._VOLLEY_LOCKS[ADDR] = threading.Lock(); ex._VOLLEY_BAL[ADDR] = (int(1e18), time.monotonic())
ex._VOLLEY_BAL_MAX_AGE = 1e9
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=6, base_fee=BASEFEE, start_nonce=200)
t1 = threading.Thread(name="T1", target=lambda: ex.send_presigned_probe(_Rpc(), cfg, ADDR, log=None, broadcast_raw_urls=["u"]))
t2 = threading.Thread(name="T2", target=lambda: ex.send_presigned_probe(_Rpc(), cfg, ADDR, log=None, broadcast_raw_urls=["u"]))
t1.start(); time.sleep(0.1); t2.start(); time.sleep(0.1)
G2.set(); t2.join(); G1.set(); t1.join()
print("С ГАРДОМ: счётчик после отказа T1 при успехе T2 =", ex._VOLLEY_NONCE.get(ADDR))
ex.invalidate_ladder(ADDR)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=4, base_fee=BASEFEE)
nn = [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]]
print("С ГАРДОМ пересборка:", nn, "->", "КОЛЛИЗИЯ" if 201 in nn else "ОК (201 не переиспользован)")
# одиночный отказ (главный сценарий блокера) — счётчик обязан вернуться
G1.clear(); ex._seq_session = lambda u: _S1()
class _S1:
    def post(self, url, json=None, timeout=None): return _Bad()
ex._seq_session = lambda u: _S1()
ex.invalidate_ladder(ADDR); ex._VOLLEY_NONCE.pop(ADDR, None); PENDING[0] = 300
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=4, base_fee=BASEFEE, start_nonce=300)
ex.send_presigned_probe(_Rpc(), cfg, ADDR, log=None, broadcast_raw_urls=["u"])
print("С ГАРДОМ одиночный отказ: счётчик =", ex._VOLLEY_NONCE.get(ADDR), "(ожидаем 300, дыры нет)")
