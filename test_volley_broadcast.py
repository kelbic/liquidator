"""Юниты PARALLEL_BROADCAST (залп юнитов в dispatch_liquidations) — инжект-фейки, без сети.
Ключевой инвариант: при флаге ON все raw уходят в секвенсер ДО первой Alchemy-ноги
(юнит u+1 не ждёт RTT юнита u); при OFF порядок бит-в-бит старый (seq,alch,seq,alch...)."""
import logging
import sys
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.execute as ex

logging.basicConfig(level=logging.INFO, format="%(message)s")
tlog = logging.getLogger("volley-test")

EVENTS: list = []                      # ("seq", nonce) / ("alch", nonce) в порядке исполнения


class _Hex:
    def __init__(self, s): self._s = s
    def hex(self): return self._s


class FakeAcct:
    address = "0xB07"
    fail_sign_nonces: set = set()

    def sign_transaction(self, t):
        if t["nonce"] in self.fail_sign_nonces:
            raise RuntimeError("sign boom")
        assert t["chainId"] == 8453 and t["from"] == self.address and t["gas"]
        return types.SimpleNamespace(raw_transaction=b"raw%d" % t["nonce"],
                                     hash=_Hex("0xhash%d" % t["nonce"]))


class FakeEth:
    fail_alch_nonces: set = set()
    get_block_calls = 0
    max_priority_fee = 1_000_000

    def get_block(self, _tag):
        FakeEth.get_block_calls += 1
        return {"baseFeePerGas": 5_000_000}

    def estimate_gas(self, _t):
        return 100_000

    def send_raw_transaction(self, raw):
        nonce = int(bytes(raw)[3:])
        EVENTS.append(("alch", nonce))
        if nonce in FakeEth.fail_alch_nonces:
            raise RuntimeError("alch down")
        return _Hex("0xhash%d" % nonce)

    def get_transaction_count(self, _addr, _tag="pending"):
        return 100


W3 = types.SimpleNamespace(eth=FakeEth())
RPC = types.SimpleNamespace(_web3=lambda: W3, chain_id=8453)


class SyncThread:                      # детерминизм: "тред" исполняется в момент .start()
    def __init__(self, target=None, args=(), daemon=None):
        self._t, self._a = target, args
    def start(self):
        self._t(*self._a)


def _fake_post_raw(url, raw_hex, timeout=5):
    EVENTS.append(("seq", int(raw_hex[2:].encode() and bytes.fromhex(raw_hex[2:])[3:])))


_real_threading = ex.threading
ex.threading = types.SimpleNamespace(Thread=SyncThread, Lock=_real_threading.Lock)
ex._post_raw_tx = _fake_post_raw
ex.account_for = lambda w3, key: FakeAcct()
ex._reconcile_nonce = lambda w3, bot: None


def cfg(par, batch=False):
    return types.SimpleNamespace(parallel_broadcast=par, wallet_key="k", liquidator_address="0xL1Q",
                                 tip_gwei=0.05, batch_liquidate=batch, batch_max_items=5,
                                 gas_limit_est=500_000, sequencer_rpc="https://seq.example",
                                 eth_price_usd=1730.0)


def prep(with_fee=True, gas=30_000):
    p = {"calldata": "0xdead", "net_usd": 5.0}
    if gas:
        p["gas"] = gas
    if with_fee:
        p["max_fee"] = 2_000_000; p["tip"] = 50_000
    return p


def rows(n, **kw):
    return [("0xmid%d" % i, "0xb%d" % i, prep(**kw)) for i in range(n)]


def reset():
    EVENTS.clear()
    ex._RECENT_SENDS.clear()
    ex._LOCAL_NONCE.clear()
    ex._LOCAL_NONCE["0xB07"] = 100
    ex._INFLIGHT = 0
    FakeEth.fail_alch_nonces = set()
    FakeEth.get_block_calls = 0
    FakeAcct.fail_sign_nonces = set()


# 1) флаг ON, 3 юнита: ВСЕ seq до ПЕРВОГО alch; nonce последовательные; local nonce продвинут
reset()
res = ex.dispatch_liquidations(RPC, cfg(True), rows(3), log=tlog, max_inflight=5, wait_receipts=False)
assert EVENTS == [("seq", 100), ("seq", 101), ("seq", 102),
                  ("alch", 100), ("alch", 101), ("alch", 102)], EVENTS
assert [r["sent"] for r in res] == [True] * 3 and sorted(r["hash"] for r in res) == \
       ["0xhash100", "0xhash101", "0xhash102"], res
assert ex._LOCAL_NONCE["0xB07"] == 103
assert ex._INFLIGHT == 0            # wait_receipts=False вернул inflight
print("UNIT_OK: залп — все raw в секвенсер ДО первой Alchemy-ноги, nonce 100..102, local=103")

# 2) флаг OFF: порядок бит-в-бит старый (seq,alch чередуются)
reset()
res = ex.dispatch_liquidations(RPC, cfg(False), rows(3), log=tlog, max_inflight=5, wait_receipts=False)
assert EVENTS == [("seq", 100), ("alch", 100), ("seq", 101), ("alch", 101), ("seq", 102), ("alch", 102)], EVENTS
assert [r["sent"] for r in res] == [True] * 3
assert ex._LOCAL_NONCE["0xB07"] == 103
print("UNIT_OK: флаг OFF — старый последовательный путь бит-в-бит")

# 3) 1 юнит: залп не активируется даже при ON (старый путь)
reset()
res = ex.dispatch_liquidations(RPC, cfg(True), rows(1), log=tlog, max_inflight=5, wait_receipts=False)
assert EVENTS == [("seq", 100), ("alch", 100)] and res[0]["sent"] is True
print("UNIT_OK: один юнит — залп не нужен, путь старый")

# 4) Alchemy-фейл среднего юнита: seq-залп уже ушёл -> все 3 учтены sent, nonce инвалидирован
reset()
FakeEth.fail_alch_nonces = {101}
res = ex.dispatch_liquidations(RPC, cfg(True), rows(3), log=tlog, max_inflight=5, wait_receipts=False)
assert EVENTS[:3] == [("seq", 100), ("seq", 101), ("seq", 102)]
assert [r["sent"] for r in res] == [True] * 3, res     # 101 raw в секвенсере -> sent/uncertain
assert "0xB07" not in ex._LOCAL_NONCE                  # инвалидация -> следующий dispatch с RPC
print("UNIT_OK: Alchemy-фейл юнита не стопит журнал остальных; nonce инвалидирован")

# 5) sign-фейл юнита 3: префикс 100,101 уходит залпом, хвост дропнут с ошибкой, nonce инвалидирован
reset()
FakeAcct.fail_sign_nonces = {102}
res = ex.dispatch_liquidations(RPC, cfg(True), rows(3), log=tlog, max_inflight=5, wait_receipts=False)
assert EVENTS == [("seq", 100), ("seq", 101), ("alch", 100), ("alch", 101)], EVENTS
_sent = [r for r in res if r["sent"]]; _err = [r for r in res if not r["sent"]]
assert len(_sent) == 2 and len(_err) == 1 and "sign error" in _err[0]["reason"]
assert "0xB07" not in ex._LOCAL_NONCE
print("UNIT_OK: sign-фейл — префикс залпом, хвост дропнут (nonce-гэп исключён)")

# 6) fee-контекст один на залп: 3 юнита БЕЗ max_fee -> ровно 1 get_block (старый путь делал бы 3)
reset()
ex.dispatch_liquidations(RPC, cfg(True), rows(3, with_fee=False), log=tlog, max_inflight=5, wait_receipts=False)
assert FakeEth.get_block_calls == 1, FakeEth.get_block_calls
print("UNIT_OK: fee-контекст один RTT на весь залп")

# 7) батч-группировка сохранена: batch ON, 7 жертв -> 2 юнита (5+2) залпом, encode вызван для обоих
reset()
_enc_calls = []
_real_enc = ex.encode_batch_liquidate
ex.encode_batch_liquidate = lambda preps: (_enc_calls.append(len(preps)), "0xba7c4")[1]
res = ex.dispatch_liquidations(RPC, cfg(True, batch=True), rows(7), log=tlog, max_inflight=5, wait_receipts=False)
ex.encode_batch_liquidate = _real_enc
assert EVENTS == [("seq", 100), ("seq", 101), ("alch", 100), ("alch", 101)], EVENTS
assert _enc_calls == [5, 2] and len(res) == 7 and all(r["sent"] for r in res)
print("UNIT_OK: батч-юниты (5+2) уходят одним залпом")

# 8) дедуп до раздачи nonce жив: повторный dispatch тех же жертв в TTL -> 0 отправок
reset()
ex.dispatch_liquidations(RPC, cfg(True), rows(3), log=tlog, max_inflight=5, wait_receipts=False)
EVENTS.clear()
res = ex.dispatch_liquidations(RPC, cfg(True), rows(3), log=tlog, max_inflight=5, wait_receipts=False)
assert EVENTS == [] and all(not r["sent"] for r in res)
print("UNIT_OK: дедуп _RECENT_SENDS сработал до nonce (0 повторных отправок)")

ex.threading = _real_threading
print("=== ВСЕ VOLLEY-ЮНИТЫ ПРОШЛИ ===")
