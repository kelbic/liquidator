"""Раскидка квот (20.08): фоновые чтения патрона/аудита -> отдельный ключ VOLLEY_BAL_RPC_URL.

Гарантия отката: пустой env / отказ конструктора / чужая сеть => переданный боевой rpc
БИТ-В-БИТ (тот же объект). Денежный путь ручку не видит вовсе."""
import os
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.execute as ex  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


class _Rpc:
    rpc_url = "https://combat.example/key"
    chain_id = 8453


def _reset():
    ex._VOLLEY_BAL_RPC = None


# ---- 1. Пустой env -> тот же объект (бит-в-бит) ----
_reset()
os.environ.pop("VOLLEY_BAL_RPC_URL", None)
r = _Rpc()
check("пустой env -> боевой rpc тем же объектом", ex.volley_bal_read_rpc(r) is r)
check("решение закэшировано (False, не пересборка)", ex._VOLLEY_BAL_RPC is False)

# ---- 2. env == боевому URL -> тот же объект (не городим дубль) ----
_reset()
os.environ["VOLLEY_BAL_RPC_URL"] = _Rpc.rpc_url
check("env совпал с боевым -> тот же объект", ex.volley_bal_read_rpc(r) is r)

# ---- 3. Отказ конструктора/гарда -> боевой rpc, не падение ----
_reset()
os.environ["VOLLEY_BAL_RPC_URL"] = "https://other.example/key2"
import chain.rpc as _rpcmod
_orig = _rpcmod.BaseRpc
class _Boom:
    def __init__(self, *a, **k):
        raise RuntimeError("нет сети")
_rpcmod.BaseRpc = _Boom
try:
    check("отказ конструктора -> боевой rpc", ex.volley_bal_read_rpc(r) is r)
finally:
    _rpcmod.BaseRpc = _orig
    os.environ.pop("VOLLEY_BAL_RPC_URL", None)
    _reset()

# ---- 4. Гард дрейфа: оба потребителя реально идут через ручку ----
_SRC_EX = open(ex.__file__.replace(".pyc", ".py")).read()
check("volley_bal_warm читает через ручку", "volley_bal_read_rpc(rpc)._web3()" in _SRC_EX)
check("volley_bal_warm_fleet читает через ручку",
      "rpc = volley_bal_read_rpc(rpc)" in _SRC_EX)
import chain.spec_fire_live as sfl
_SRC_SFL = open(sfl.__file__.replace(".pyc", ".py")).read()
check("аудит лестниц читает через ручку", "_ex.volley_bal_read_rpc(self.rpc)" in _SRC_SFL)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS else "OK: ручка фоновых чтений — откат бит-в-бит"))
sys.exit(1 if FAILS else 0)
