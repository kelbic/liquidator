"""Остаток Б2: путь _engine_timeout (invalidate_ladder БЕЗ права) при MAX_RESIGN=0."""
import pathlib, sys, threading, types, rlp
ROOT = pathlib.Path("/home/claude-agent/work/liquidator/.claude/worktrees/wf_73589e1f-2ff-2")
sys.path.insert(0, str(ROOT))
import chain.execute as ex
from eth_account import Account
KEY = "0x" + "9b" * 32; ADDR = Account.from_key(KEY).address
GAS = 900_000; BASEFEE = int(0.005e9)
cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)
class _Eth:
    account = Account
    def get_block(self, _b): return {"baseFeePerGas": BASEFEE}
    def get_balance(self, _a): return int(1e18)
    def get_transaction_count(self, _a, _b="pending"): return 0
class _W3: eth = _Eth()
class _Rpc:
    chain_id = 8453
    def _web3(self): return _W3()
def tip_of(rh):
    return int.from_bytes(rlp.decode(bytes.fromhex(rh[2:])[1:])[2], "big")
ex._VOLLEY_LOCKS[ADDR] = threading.Lock(); ex._VOLLEY_BAL[ADDR] = (int(1e18), 1e18)
ex._TIP_CLASS_MAX_RESIGN = 0
ex._TIP_CLASS_LATCHED = 1_000_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(1_000_000)
A = ex.probe_tip_for(ADDR)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=6, base_fee=BASEFEE)
ex._TIP_CLASS_LATCHED = 5_000_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(5_000_000)
B = ex.probe_tip_for(ADDR)
# ровно путь _engine_timeout: volley_invalidate_nonce + invalidate_ladder, затем ФОНОВЫЙ refill
ex.volley_invalidate_nonce(ADDR); ex.invalidate_ladder(ADDR)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, base_fee=BASEFEE, log=None)   # БЕЗ права
got = {tip_of(e[1]) for e in ex._VOLLEY_LADDER[ADDR]["q"]}
print("MAX_RESIGN=0, путь таймаута коллектора: тир был %d, стал %s (класс требует %d) -> %s"
      % (A, sorted(got), B, "ПЕРЕЕХАЛА МИМО ПОТОЛКА" if got == {B} else "удержана"))
