"""Юнит-тест batch-ветки dispatch_liquidations: моки send_fn + крафтнутый receipt с событиями.
Проверяет: (A) флаг OFF -> одиночный путь (N tx), (B) флаг ON -> один batch-tx + per-item разбор из
Liquidated/BatchItemFailed, (C) учёт net/gas точен, (D) пустой батч -> counter, (E) top-level revert.
Гоняется в песочнице (нужны eth_abi/eth_utils; web3 НЕ нужен — w3 замокан)."""
import sys, types
sys.path.insert(0, ".")
from eth_utils import keccak
import chain.execute as ex

TESTKEY = "0x" + "11" * 32           # детерминированный тест-ключ
LIQ = "0x" + "cc" * 20
LOAN = "0x" + "aa" * 20
B1 = "0x" + "b1" * 20
B2 = "0x" + "b2" * 20


class _Acct:
    address = None


class _FakeAccountNS:
    def from_key(self, key):
        # eth_account реально доступен в песочнице -> вернём настоящий адрес
        from eth_account import Account
        return Account.from_key(key)


class _FakeEth:
    def __init__(self, receipts):
        self._receipts = receipts     # hash -> receipt dict
        self.account = _FakeAccountNS()
        self.sent_nonce_counts = 0
    def get_transaction_count(self, addr, blk="pending"):
        return 0
    def wait_for_transaction_receipt(self, h, timeout=120):
        return self._receipts[h]


class _FakeW3:
    def __init__(self, receipts):
        self.eth = _FakeEth(receipts)


class _FakeRpc:
    def __init__(self, receipts):
        self._w3 = _FakeW3(receipts)
    def _web3(self):
        return self._w3


class _Cfg:
    wallet_key = TESTKEY
    liquidator_address = LIQ
    tip_gwei = 0.05   # боевой с 23.07; на старом 6.0 EV-отсечка (27.07) справедливо режет net $4-5
    gas_limit_est = 500000
    eth_price_usd = 1900.0
    sequencer_rpc = "off"
    fast_lane_max_consec_reverts = 3
    batch_liquidate = False
    batch_max_items = 5


def _prep(borrower, net, profit_usd, debt_usd=1000.0, debt_assets=1000.0, fast=False):
    mp = {"loanToken": LOAN, "collateralToken": "0x" + "22" * 20, "oracle": "0x" + "33" * 20,
          "irm": "0x" + "44" * 20, "lltv": 860000000000000000}
    return {"ok": True, "calldata": "0x1234", "gas": 600000, "net_usd": net, "profit_usd": profit_usd,
            "cost_usd": 0.5, "mp": mp, "borrower": borrower, "repaid_shares": 1000,
            "swap_target": "0x" + "55" * 20, "swap_calldata": "0xdead", "min_profit": 1,
            "debt_usd": debt_usd, "debt_assets": debt_assets, "fast": fast}


def _liquidated_log(borrower, loan, profit_wei):
    sig = "0x" + keccak(text="Liquidated(address,address,uint256)").hex()
    return {"address": LIQ,
            "topics": [sig, "0x" + borrower[2:].rjust(64, "0"), "0x" + loan[2:].rjust(64, "0")],
            "data": "0x" + profit_wei.to_bytes(32, "big").hex()}


def _item_failed_log(i, borrower, reason4=b"\x08\xc3\x79\xa0"):
    sig = "0x" + keccak(text="BatchItemFailed(uint256,address,bytes4)").hex()
    return {"address": LIQ,
            "topics": [sig, "0x" + i.to_bytes(32, "big").hex(), "0x" + borrower[2:].rjust(64, "0")],
            "data": "0x" + reason4.ljust(32, b"\x00").hex()}


def _reset():
    ex._LOCAL_NONCE.clear(); ex._RECENT_SENDS.clear()
    ex._BATCH_EMPTY_STREAK = 0; ex._BATCH_TRIP_MSG = None
    ex._FL_CONSEC_REVERTS = 0; ex._FL_DISABLED = False; ex._FL_TRIP_MSG = None


def _send_fn_factory(hash_by_nonce):
    sends = []
    def send_fn(rpc, key, txd, wait=False, min_tip_wei=0):
        n = txd["nonce"]; h = hash_by_nonce(n)
        sends.append((n, txd))
        return {"hash": h}
    return send_fn, sends


def test_flag_off_single_path():
    _reset()
    cfg = _Cfg(); cfg.batch_liquidate = False
    # 2 жертвы, флаг OFF -> 2 отдельных tx
    rc = {"0xh0": {"status": 1, "gasUsed": 200000, "effectiveGasPrice": 10**9, "blockNumber": 1, "transactionIndex": 3, "logs": []},
          "0xh1": {"status": 1, "gasUsed": 200000, "effectiveGasPrice": 10**9, "blockNumber": 1, "transactionIndex": 4, "logs": []}}
    rpc = _FakeRpc(rc)
    send_fn, sends = _send_fn_factory(lambda n: f"0xh{n}")
    prepared = [("m1", B1, _prep(B1, 5.0, 6.0)), ("m2", B2, _prep(B2, 4.0, 5.0))]
    res = ex.dispatch_liquidations(rpc, cfg, prepared, max_inflight=5, send_fn=send_fn)
    assert len(sends) == 2, f"OFF -> 2 отдельных tx, got {len(sends)}"
    assert all(r["sent"] and r["status"] == 1 for r in res), res
    assert [s[0] for s in sends] == [0, 1], "nonce base+0, base+1"
    print("  [OK] flag OFF -> одиночный путь (2 tx, nonce 0/1)")


def test_flag_on_batch_partial():
    _reset()
    cfg = _Cfg(); cfg.batch_liquidate = True
    # 2 жертвы, флаг ON -> ОДИН batch-tx (nonce 0). item1 win (profit 3e18 loan-wei), item2 fail.
    prof = 3 * 10**18
    logs = [_liquidated_log(B1, LOAN, prof), _item_failed_log(1, B2)]
    rc = {"0xh0": {"status": 1, "gasUsed": 900000, "effectiveGasPrice": 10**9, "blockNumber": 7,
                   "transactionIndex": 2, "logs": logs}}
    rpc = _FakeRpc(rc)
    send_fn, sends = _send_fn_factory(lambda n: f"0xh{n}")
    # debt_usd/debt_assets = 1500/1000 -> USD за loan-unit = 1.5; profit 3e18 wei... но debt в тех же
    # единицах, что profit (loan-token). profit_usd = 3e18 * 1500 / 1000 = 4.5e18? Нет — profit_wei и
    # debt_assets в РАВНЫХ единицах (raw loan-token). Возьмём debt_assets = 3e18 (=$4500) -> USD/unit=1500/3e18.
    prepared = [("m1", B1, _prep(B1, 5.0, 6.0, debt_usd=4500.0, debt_assets=3 * 10**18)),
                ("m2", B2, _prep(B2, 4.0, 5.0))]
    res = ex.dispatch_liquidations(rpc, cfg, prepared, max_inflight=5, send_fn=send_fn)
    assert len(sends) == 1, f"ON -> ОДИН batch-tx, got {len(sends)}"
    assert sends[0][0] == 0, "batch nonce base+0"
    # селектор в calldata = batchLiquidate
    assert sends[0][1]["data"][2:10] == keccak(text=ex._BATCH_LIQUIDATE_SIG)[:4].hex(), "batch calldata"
    # gas: (600000+600000 + 30000*2)*1.25 = (1_260_000)*1.25 = 1_575_000
    assert sends[0][1]["gas"] == int((600000 + 600000 + 30000 * 2) * 1.25), sends[0][1]["gas"]
    assert len(res) == 2, "N строк на юнит"
    r1 = next(r for r in res if r["borrower"] == B1)
    r2 = next(r for r in res if r["borrower"] == B2)
    assert r1["status"] == 1 and r2["status"] == 0, (r1["status"], r2["status"])
    # profit_usd_i = 3e18 * 4500 / 3e18 = 4500; net = 4500 - gas (газ на первой строке B1)
    gas_total = 900000 * 10**9 * 1900.0 / 1e18
    assert abs(r1["net_usd"] - (4500.0 - gas_total)) < 1e-6, r1["net_usd"]
    assert r1["gas_usd"] == gas_total and r2["gas_usd"] == 0.0, (r1["gas_usd"], r2["gas_usd"])
    assert r2["net_usd"] == 0.0, r2["net_usd"]
    # сумма/день: net = 4500 - gas, gas_sum = gas_total (точно)
    assert abs(sum(r["net_usd"] for r in res) - (4500.0 - gas_total)) < 1e-6
    assert abs(sum(r["gas_usd"] for r in res) - gas_total) < 1e-6
    print("  [OK] flag ON -> 1 batch-tx, per-item (win/fail), учёт net/gas точен")


def test_empty_batch_counter():
    _reset()
    cfg = _Cfg(); cfg.batch_liquidate = True
    logs = [_item_failed_log(0, B1), _item_failed_log(1, B2)]   # оба провал, tx status=1
    rc = {"0xh0": {"status": 1, "gasUsed": 500000, "effectiveGasPrice": 10**9, "blockNumber": 5,
                   "transactionIndex": 1, "logs": logs}}
    prepared = [("m1", B1, _prep(B1, 5.0, 6.0)), ("m2", B2, _prep(B2, 4.0, 5.0))]
    for k in range(3):                                          # 3 подряд пустых -> trip
        _reset_streak = None
        rpc = _FakeRpc(rc); send_fn, _ = _send_fn_factory(lambda n: "0xh0")
        ex.dispatch_liquidations(rpc, cfg, prepared, max_inflight=5, send_fn=send_fn)
        ex._RECENT_SENDS.clear()                               # чтобы не дедупнуло на след. итерации
    trip = ex.pop_batch_empty_trip()
    assert trip is not None and "ПУСТЫХ" in trip, trip
    print("  [OK] пустой батч (nOk=0) -> счётчик серии + TG-trip после 3")


def test_batch_toplevel_revert():
    _reset()
    cfg = _Cfg(); cfg.batch_liquidate = True
    rc = {"0xh0": {"status": 0, "gasUsed": 400000, "effectiveGasPrice": 10**9, "blockNumber": 9,
                   "transactionIndex": 0, "logs": []}}     # top-level revert
    rpc = _FakeRpc(rc); send_fn, sends = _send_fn_factory(lambda n: "0xh0")
    prepared = [("m1", B1, _prep(B1, 5.0, 6.0)), ("m2", B2, _prep(B2, 4.0, 5.0))]
    res = ex.dispatch_liquidations(rpc, cfg, prepared, max_inflight=5, send_fn=send_fn)
    assert len(res) == 2 and all(r["status"] == 0 for r in res), res
    gas_total = 400000 * 10**9 * 1900.0 / 1e18
    assert abs(sum(r["gas_usd"] for r in res) - gas_total) < 1e-6, "газ реверта учтён один раз"
    assert abs(sum(r["net_usd"] for r in res) - (-gas_total)) < 1e-6, "net = -gas"
    print("  [OK] top-level revert -> все items fail, газ учтён один раз")


if __name__ == "__main__":
    test_flag_off_single_path()
    test_flag_on_batch_partial()
    test_empty_batch_counter()
    test_batch_toplevel_revert()
    print("ВСЕ 4 ТЕСТА BATCH-DISPATCH ПРОШЛИ")
