"""_INFLIGHT отпускается на КВИТАНЦИИ, а диагностика вскрытия идёт уже вне брекета.

Замер 08.08 по месяцу журнала: немота p50=1.8с на tx, а в каскаде 07.08 17:00 объединение
трёх немот съело 7.5с из 9.9с горячего окна (75%) — причём окно фида закрылось ВНУТРИ первой
немоты. Аудит кода показал состав брекета: в нём сидели `_revert_reason` (реплей + ретрай со
sleep 1.5с) и `_submit_xray` (get_logs + ретрай со sleep 2.0с) — то есть примерно половина
немоты была не полётом tx, а нашей же пост-диагностикой.

Исполняется НАСТОЯЩАЯ dispatch_liquidations (урок seam-stubbed-above-the-defect), швы стоят
на транспорте: send_tx, ожидание квитанции, реплей реверта и xray. Ключевая проверка —
inflight, ЗАМЕРЕННЫЙ ВНУТРИ диагностики: она обязана видеть 0.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                               # noqa: E402


class _Cfg:
    wallet_key = "0x" + "11" * 32
    liquidator_address = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
    tip_gwei = 0.05
    eth_price_usd = 3000.0
    batch_liquidate = False
    parallel_broadcast = False
    sequencer_rpc = "off"
    fast_lane_max_consec_reverts = 3


class _Eth:
    def __init__(self, receipts):
        self._receipts = receipts

    def wait_for_transaction_receipt(self, h, timeout=None):
        r = self._receipts[h]
        if isinstance(r, Exception):
            raise r
        return r


class _W3:
    def __init__(self, receipts):
        self.eth = _Eth(receipts)


class _Rpc:
    def __init__(self, receipts):
        self._w3 = _W3(receipts)

    def _web3(self):
        return self._w3


class _Acct:
    address = "0xF0000000000000000000000000000000000000F0"


def _prep(net=10.0):
    return {"calldata": "0x1234", "gas": 300000, "max_fee": 10**8, "tip": 10**7,
            "net_usd": net, "profit_usd": net + 1.0, "cost_usd": 1.0, "debt_usd": 5000.0,
            "fast": True, "mp": {"loanToken": "0x" + "cc" * 20}, "debt_assets": 1000}


def _rcpt(status, blk=100, idx=5):
    return {"status": status, "blockNumber": blk, "transactionIndex": idx,
            "gasUsed": 200000, "effectiveGasPrice": 10**7, "logs": []}


seen = {"revert_reason": [], "xray": [], "sent": []}
_orig = (ex.send_tx, ex.account_for, ex._pending_nonce, ex._revert_reason, ex._submit_xray,
         ex._reconcile_nonce, ex._fl_record)
try:
    ex.account_for = lambda w3, key: _Acct()
    ex._pending_nonce = lambda w3, addr: 50
    ex._reconcile_nonce = lambda *a, **k: None
    ex._fl_record = lambda *a, **k: None

    def _send(rpc, key, tx, wait=True, min_tip_wei=0, **kw):
        h = "0xh%d" % tx["nonce"]
        seen["sent"].append(h)
        return {"hash": h}

    # ГЛАВНЫЙ ЗАМЕР: диагностика запоминает inflight В МОМЕНТ СВОЕГО ВЫЗОВА
    def _rr(rpc, h, blk, **kw):
        seen["revert_reason"].append((h, ex.current_inflight()))
        return "position healthy (lost race / recovered)"

    def _xr(rpc, borrower, blk, idx, log, **kw):
        seen["xray"].append((borrower, ex.current_inflight()))
        return "race"

    ex.send_tx, ex._revert_reason, ex._submit_xray = _send, _rr, _xr

    # --- 1. каскад из 3 ревертов: диагностика КАЖДОЙ видит inflight=0 --------------------
    ex._INFLIGHT = 0
    ex._LOCAL_NONCE.pop(_Acct.address, None)
    ex._RECENT_SENDS.clear()
    rcpts = {"0xh50": _rcpt(0), "0xh51": _rcpt(0), "0xh52": _rcpt(0)}
    prepared = [("0xmid%d" % i, "0x" + "b%d" % i * 10, _prep()) for i in range(3)]
    res = ex.dispatch_liquidations(_Rpc(rcpts), _Cfg(), prepared, log=None, max_inflight=3)

    assert len(seen["sent"]) == 3, seen["sent"]
    assert len(res) == 3 and all(r["sent"] for r in res), res
    assert [i for _, i in seen["revert_reason"]] == [0, 0, 0], seen["revert_reason"]
    assert [i for _, i in seen["xray"]] == [0, 0, 0], seen["xray"]
    assert ex.current_inflight() == 0, ex.current_inflight()
    assert all(r.get("xray") == "race" for r in res), "вердикт xray потерялся при разносе проходов"

    # --- 2. КОНТРОЛЬ: брекет ВООБЩЕ существует — inflight>0 между отправкой и квитанцией --
    # Без него «диагностика видит 0» неотличимо от «счётчик мёртв».
    ex._INFLIGHT = 0
    ex._LOCAL_NONCE.pop(_Acct.address, None)
    ex._RECENT_SENDS.clear()
    inflight_at_receipt = []

    class _EthProbe(_Eth):
        def wait_for_transaction_receipt(self, h, timeout=None):
            inflight_at_receipt.append(ex.current_inflight())
            return super().wait_for_transaction_receipt(h, timeout=timeout)

    rpc2 = _Rpc({"0xh50": _rcpt(1), "0xh51": _rcpt(1)})
    rpc2._w3.eth = _EthProbe(rpc2._w3.eth._receipts)
    ex.dispatch_liquidations(rpc2, _Cfg(),
                             [("0xmidA", "0x" + "aa" * 20, _prep()),
                              ("0xmidB", "0x" + "ab" * 20, _prep())], log=None, max_inflight=3)
    assert inflight_at_receipt == [2, 1], \
        "брекет не держит tx в полёте до квитанции: %s" % inflight_at_receipt
    assert ex.current_inflight() == 0

    # --- 3. таймаут квитанции: inflight всё равно отпущен, лосс забронирован -------------
    ex._INFLIGHT = 0
    ex._LOCAL_NONCE[_Acct.address] = 50
    ex._RECENT_SENDS.clear()
    res3 = ex.dispatch_liquidations(_Rpc({"0xh50": TimeoutError("no receipt")}), _Cfg(),
                                    [("0xmidT", "0x" + "cd" * 20, _prep())], log=None)
    assert ex.current_inflight() == 0, "таймаут не отпустил inflight: %d" % ex.current_inflight()
    assert res3[0]["status"] is None and res3[0]["net_usd"] < 0, res3
    assert "receipt timeout" in res3[0]["reason"], res3
    assert _Acct.address not in ex._LOCAL_NONCE, "nonce не инвалидирован на таймауте"

    # --- 4. осечка САМОЙ диагностики: inflight уже свободен, строка книжится как лосс ----
    ex._INFLIGHT = 0
    ex._LOCAL_NONCE[_Acct.address] = 50
    ex._RECENT_SENDS.clear()
    ex._revert_reason = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("RPC down"))
    res4 = ex.dispatch_liquidations(_Rpc({"0xh50": _rcpt(0)}), _Cfg(),
                                    [("0xmidE", "0x" + "ef" * 20, _prep())], log=None)
    assert ex.current_inflight() == 0, "осечка диагностики удержала inflight"
    assert res4[0]["status"] is None and res4[0]["net_usd"] < 0, res4
    # квитанция на руках -> nonce потреблён on-chain, кэш ВАЛИДЕН; pop здесь снёс бы свежий
    # кэш параллельного dispatch/арма (гейты открыты с прохода 1) и подарил горячему пути
    # лишний RPC-RTT (находка ревью 08.08, обе линзы)
    assert ex._LOCAL_NONCE.get(_Acct.address) == 51, \
        "осечка диагностики при квитанции на руках снесла валидный nonce-кэш"
    ex._revert_reason = _rr

    # --- 5. wait_receipts=False: путь без ожидания не копит фантомный inflight ----------
    ex._INFLIGHT = 0
    ex._LOCAL_NONCE.pop(_Acct.address, None)
    ex._RECENT_SENDS.clear()
    res5 = ex.dispatch_liquidations(_Rpc({}), _Cfg(),
                                    [("0xmidN", "0x" + "ba" * 20, _prep())], log=None,
                                    wait_receipts=False)
    assert ex.current_inflight() == 0 and res5[0]["sent"] is True
finally:
    (ex.send_tx, ex.account_for, ex._pending_nonce, ex._revert_reason, ex._submit_xray,
     ex._reconcile_nonce, ex._fl_record) = _orig
    ex._INFLIGHT = 0
    ex._LOCAL_NONCE.pop(_Acct.address, None)
    ex._RECENT_SENDS.clear()

# --- 6. структурный контроль: диагностика НЕ должна вернуться внутрь брекета ------------
src = pathlib.Path("chain/execute.py").read_text()
_tail = src[src.index("    _got = []"):]
_dec = _tail.index("_inflight_add(-1)")
for _name in ("_revert_reason(", "_submit_xray("):
    assert _name not in _tail[:_dec], "%s снова оказался ДО декремента inflight" % _name

print("OK: inflight отпускается на квитанции, диагностика идёт вне брекета")
