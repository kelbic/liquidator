"""Юниты лаг-гарда диагностики (проигрыш cbBTC 14:07 31.07: нода отстала на T+0.7с ->
_revert_reason печатал '-32001 block not found' КАК причину, а _submit_xray по пустым логам
отставшей ноды вынес ложное «НЕ гонка», скрыв победителя blk-1/idx=2928/tip=0.0034).
Проверяет: (A) xray при head<нашего блока и пустых логах -> «НЕПОЛОН», не вердикт; (B) ретрай
догнавшей ноды находит победителя; (C) head>=блока и пусто -> прежний «НЕ гонка»; (D) reason:
транспорт -> ретрай -> успех; (E) транспорт дважды -> честный тег, не мусор. Без сети."""
import sys

sys.path.insert(0, ".")
import chain.rpc as R
import chain.execute as E


class FakeEth:
    def __init__(self, heads, txs=None):
        self._heads = list(heads)
        self._txs = txs or {}

    @property
    def block_number(self):
        return self._heads.pop(0) if len(self._heads) > 1 else self._heads[0]

    def get_transaction(self, h):
        return self._txs.get(h, {"from": "0x" + "1" * 40, "to": "0x" + "2" * 40,
                                 "input": "0x", "maxPriorityFeePerGas": 3400000})

    def get_block(self, n):
        return {"baseFeePerGas": 0}


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


class FakeRpc:
    def __init__(self, w3, call_errors):
        self._w3 = w3
        self._errs = list(call_errors)

    def _web3(self):
        return self._w3

    def eth_call(self, params, block=None):
        if self._errs:
            raise RuntimeError(self._errs.pop(0))
        return "0x"


BORROWER = "0x" + "ab" * 20
LOGS_HIT = [{"blockNumber": 100, "transactionIndex": 5, "transactionHash": "0x" + "cc" * 32}]


def run_xray(heads, logs_seq, block=101):
    calls = {"logs": 0, "slept": 0}
    seq = list(logs_seq)

    def fake_get_logs(w3, flt):
        calls["logs"] += 1
        return seq.pop(0) if seq else []

    old = R.get_logs_fb
    R.get_logs_fb = fake_get_logs
    lines = []

    class L:
        def info(self, msg, *a):
            lines.append(msg % a if a else msg)

    try:
        E._submit_xray(FakeRpc(FakeW3(FakeEth(heads)), []), BORROWER, block, 7, L(),
                       _sleep=lambda s: calls.__setitem__("slept", calls["slept"] + 1))
    finally:
        R.get_logs_fb = old
    return lines, calls


# A) нода отстала (head=99 < 101), логи пусты и после ретрая -> НЕПОЛОН, не «НЕ гонка»
lines, calls = run_xray([99, 99, 99], [[], []])
assert calls["logs"] == 2 and calls["slept"] == 1
assert any("НЕПОЛОН" in ln for ln in lines) and not any("НЕ гонка" in ln for ln in lines), lines
print("UNIT_OK: отставшая нода -> «xray НЕПОЛОН», ложного вердикта нет")

# B) первый заход пуст (head отстал), после паузы нода догнала и отдала победителя
lines, calls = run_xray([99, 101, 101], [[], LOGS_HIT])
assert any("победитель" in ln for ln in lines), lines
print("UNIT_OK: ретрай догнавшей ноды находит победителя")

# C) head покрывает окно и логов нет -> прежний честный «НЕ гонка», без ретрая
lines, calls = run_xray([101, 101], [[]])
assert calls["logs"] == 1 and any("НЕ гонка" in ln for ln in lines), (lines, calls)
print("UNIT_OK: head>=блока и пусто — прежний вердикт «не гонка»")

# D) reason: транспорт на первой попытке, реальный реверт на второй -> бакет, не мусор
slept = []
r = FakeRpc(FakeW3(FakeEth([101])), ["-32001 block not found: 0x2f12796",
                                     "execution reverted: 0x81ceff30"])
got = E._revert_reason(r, "0x" + "dd" * 32, 101, _sleep=lambda s: slept.append(s))
assert "block not found" not in got and len(slept) == 1, got
print("UNIT_OK: reason — транспорт ретраится, реальный реверт бакетится:", got[:40])

# E) транспорт дважды -> честный тег «нода отстала», не сырой RPC-словарь
r2 = FakeRpc(FakeW3(FakeEth([101])), ["-32001 block not found", "-32001 block not found"])
got2 = E._revert_reason(r2, "0x" + "dd" * 32, 101, _sleep=lambda s: None)
assert got2.startswith("reason недоступен"), got2
print("UNIT_OK: стойкий транспорт -> честный тег")

print("=== ВСЕ XRAY-LAG ЮНИТЫ ПРОШЛИ ===")
