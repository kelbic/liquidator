import pathlib, sys, threading, types
ROOT = pathlib.Path("/home/claude-agent/work/liquidator/.claude/worktrees/wf_73589e1f-2ff-2")
sys.path.insert(0, str(ROOT))
import chain.execute as ex
from eth_account import Account

BASEFEE = int(0.005e9); GAS = 900_000
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

# ---- Б4: набор тиров ----
print("СТАТИЧЕСКИЙ набор (до защёлки):", ex._PROBE_TIP_TIERS)
print("MULT:", ex._TIP_TIER_MULT)
print("от класса 1.01e6:", ex._tiers_from_class(1_010_000))
keys = ["0x" + ("%064x" % (i + 7000)) for i in range(110)]
addrs = [Account.from_key(k).address for k in keys]
ex._TIP_CLASS_LATCHED = 1_010_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(1_010_000)
from collections import Counter
c = Counter(ex.probe_tip_for(a) for a in addrs)
print("распределение 110 ног по полосам:", dict(sorted(c.items())))

# ---- Б4: динамика на боевом каденсе ----
def sim(hold_min, label):
    ex._TIP_CLASS_WIN.clear(); ex._TIP_CLASS_LATCHED = 1_010_000
    ex._TIP_TIERS_LIVE = ex._tiers_from_class(1_010_000); ex._TIP_CLASS_SWITCH_TS = 0.0
    t = 0.0; switched_at = None
    for i in range(600):                        # 600 проходов аудита по 120с = 20 часов
        t = i * 120.0
        dom = 3_000_000 if t < hold_min * 60 else 1_010_000
        _n, sw = ex.refresh_tip_tiers(now=t, dom_wei=dom)
        if sw and switched_at is None and ex._TIP_CLASS_LATCHED > 2_000_000:
            switched_at = t
    print("%s: переезд на 3.0e6 %s" % (label,
          ("через %.0f мин" % (switched_at / 60)) if switched_at else "НЕ ПРОИЗОШЁЛ"))
sim(4, "каскад 4 мин   ")
sim(600, "удержание 10 ч ")

# ---- Б1: порядок причин ----
mod = ex
def stand():
    r = _Rpc()
    for a, k in zip(addrs, keys):
        mod._VOLLEY_LOCKS[a] = threading.Lock(); mod.invalidate_ladder(a)
        mod._VOLLEY_NONCE.pop(a, None); mod._VOLLEY_BAL.pop(a, None)
        mod.presign_ladder(r, cfg, a, k, GAS, depth=1, base_fee=BASEFEE)
ex._TIP_CLASS_LATCHED = 1_000_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(1_000_000)
stand()
mute = []
for i in (3, 17):
    ex._VOLLEY_LADDER[addrs[i]]["max_fee"] = BASEFEE // 2; mute.append(addrs[i])
st = ex._VOLLEY_LADDER[addrs[59]]
st["max_fee"] = BASEFEE * ex._VOLLEY_PRESIGN_MAXFEE_MULT * 10 + int(st["tip"]); mute.append(addrs[59])
ex._TIP_CLASS_LATCHED = 5_000_000; ex._TIP_TIERS_LIVE = ex._tiers_from_class(5_000_000)
stale, price, moved, deferred = ex.plan_ladder_resigns(addrs, BASEFEE, ex.probe_tip_for)
print("Б1: price=%d (немых %d, совпало %s), moved=%d (кап %d), отложено=%d, гейдж=%s"
      % (len(price), len(mute), sorted(price) == sorted(mute), len(moved),
         ex.tip_resign_cap(), deferred, ex.TIP_CLASS_STATS["resign_capped"]))
# повторный проход при неизменной картине: гейдж не должен расти
stale2, p2, m2, d2 = ex.plan_ladder_resigns(addrs, BASEFEE, ex.probe_tip_for)
print("Б1: второй проход — гейдж=%s (не сумма: %s)" % (ex.TIP_CLASS_STATS["resign_capped"],
      ex.TIP_CLASS_STATS["resign_capped"] == d2))
