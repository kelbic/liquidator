"""НЕЗАВИСИМАЯ ПРОВЕРКА Б2: пин тира на пути доточки склада (боевая форма вызова _refill)."""
import pathlib, sys, threading, types
ROOT = pathlib.Path("/home/claude-agent/work/liquidator/.claude/worktrees/wf_73589e1f-2ff-2")
sys.path.insert(0, str(ROOT))
import chain.execute as ex
from eth_account import Account
import rlp  # noqa

KEY = "0x" + "5a" * 32
ACCT = Account.from_key(KEY); ADDR = ACCT.address
GAS = 900_000; BASEFEE = int(0.005e9)
cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)

class _Eth:
    account = Account
    def get_block(self, _b): return {"baseFeePerGas": BASEFEE}
    def get_balance(self, _a): return int(1e18)
    def get_transaction_count(self, _a, _b="pending"): return 0
class _W3:
    eth = _Eth()
class _Rpc:
    chain_id = 8453
    def _web3(self): return _W3()

def tip_of(raw_hex):
    b = bytes.fromhex(raw_hex[2:])
    assert b[0] == 2
    import rlp as _r
    items = _r.decode(b[1:])
    return int.from_bytes(items[2], "big")   # maxPriorityFeePerGas

ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
ex._VOLLEY_BAL[ADDR] = (int(1e18), 1e18)

# класс A защёлкнут
ex._TIP_CLASS_LATCHED = 1_000_000
ex._TIP_TIERS_LIVE = ex._tiers_from_class(1_000_000)
tierA = ex.probe_tip_for(ADDR)
ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, depth=6, base_fee=BASEFEE)
sig0 = {tip_of(e[1]) for e in ex._VOLLEY_LADDER[ADDR]["q"]}
assert sig0 == {tierA}, sig0

# класс уехал
ex._TIP_CLASS_LATCHED = 5_000_000
ex._TIP_TIERS_LIVE = ex._tiers_from_class(5_000_000)
tierB = ex.probe_tip_for(ADDR)
assert tierB != tierA, (tierA, tierB)

sv = ex._TIP_CLASS_MAX_RESIGN
ex._TIP_CLASS_MAX_RESIGN = 0            # ОТКАТ
try:
    # РОВНО форма _refill: presign_ladder(rpc,cfg,addr,key,GAS, base_fee=..., log=None)
    for _ in range(6):
        ex._VOLLEY_LADDER[ADDR]["q"] = ex._VOLLEY_LADDER[ADDR]["q"][:2]
        ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, base_fee=BASEFEE, log=None)
    held = {tip_of(e[1]) for e in ex._VOLLEY_LADDER[ADDR]["q"]}
    print("Б2 доточка без права: байты на", sorted(held), "класс требует", tierB,
          "-> ", "ПИН ДЕРЖИТ" if held == {tierA} else "ПИН ПРОБИТ")
    # аудитный путь: право есть
    ex.invalidate_ladder(ADDR)
    ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, base_fee=BASEFEE, depth=6,
                      allow_tip_resign=True)
    moved = {tip_of(e[1]) for e in ex._VOLLEY_LADDER[ADDR]["q"]}
    print("Б2 аудит с правом (после invalidate):", sorted(moved), "==", tierB, moved == {tierB})
    # ДЫРА? аудит БЕЗ invalidate, но с правом
    ex._TIP_CLASS_LATCHED = 1_000_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(1_000_000)
    ex.invalidate_ladder(ADDR)
    ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, base_fee=BASEFEE, depth=6)
    ex._TIP_CLASS_LATCHED = 5_000_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(5_000_000)
    ex._VOLLEY_LADDER[ADDR]["q"] = ex._VOLLEY_LADDER[ADDR]["q"][:2]
    ex.presign_ladder(_Rpc(), cfg, ADDR, KEY, GAS, base_fee=BASEFEE, log=None,
                      allow_tip_resign=True)
    got = {tip_of(e[1]) for e in ex._VOLLEY_LADDER[ADDR]["q"]}
    print("Б2 право БЕЗ invalidate (форма аудита при cap=0):", sorted(got),
          "-> потолок обойдён" if got != {tierA} else "-> удержано")
    print("resign_pinned =", ex.TIP_CLASS_STATS.get("resign_pinned"))
finally:
    ex._TIP_CLASS_MAX_RESIGN = sv
