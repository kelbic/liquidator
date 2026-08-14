"""Настоящий `send_board_tx` — не заглушка вместо него.

Повод (07.08). `test_v2_board.py` подменяет шов `sf._board_arm_fn`, поэтому НАСТОЯЩАЯ
`chain.execute.send_board_tx` не исполнялась ни в одном из 38 зелёных тестов. Внутри неё жил
вызов `_fresh_basefee()` без импорта — имя из `chain.hotpath`. Итог: набор тестов зелёный,
арм доски в бою не проходил НИ РАЗУ, `board_arms=0`, залп молча деградировал до V1.

Здесь шов опущен на транспорт: подменяются только `send_tx`, `account_for`, `_pending_nonce`
и источник basefee. Всё тело `send_board_tx` — включая разрешение имён — исполняется по-настоящему.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                              # noqa: E402
import chain.hotpath as hp                              # noqa: E402


class _Cfg:
    wallet_key = "0x" + "11" * 32
    liquidator_address = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
    tip_gwei = 0.05


class _Rpc:
    def _web3(self):
        return object()


class _Acct:
    address = "0xF0000000000000000000000000000000000000F0"


SENT = []
orig = (ex.send_tx, ex.account_for, ex._pending_nonce, hp._fresh_basefee)
ex.send_tx = lambda rpc, key, tx, wait=True, min_tip_wei=0, **kw: (
    SENT.append(dict(tx)), {"hash": "0xboard1"})[1]
ex.account_for = lambda w3, key: _Acct()
ex._pending_nonce = lambda w3, addr: 7
hp._fresh_basefee = lambda *a, **k: 50_000_000            # 0.05 gwei

try:
    ex._LOCAL_NONCE.pop(_Acct.address, None)

    # --- 1. основной путь: доска армится, tx уходит --------------------------------------
    cd = ex.encode_set_plan(
        {"loanToken": "0x" + "aa" * 20, "collateralToken": "0x" + "bb" * 20,
         "oracle": "0x" + "cc" * 20, "irm": "0x" + "dd" * 20, "lltv": 860000000000000000},
        "0x" + "ee" * 20, 12345, "0x" + "ff" * 20, "0xdeadbeef", 0, 1786112076, 0)
    assert cd.startswith("0x") and len(cd) > 200, cd

    h = ex.send_board_tx(_Rpc(), _Cfg(), cd, 130_000, log=None, tag="setPlan")
    assert h == "0xboard1", "арм доски не отправился: %r" % h
    assert len(SENT) == 1, SENT
    tx = SENT[0]
    assert tx["to"] == _Cfg.liquidator_address, tx
    assert tx["data"] == cd, "калдата плана подменилась"
    assert tx["nonce"] == 7, tx
    assert tx["gas"] == 130_000, tx
    # fee по СВЕЖЕМУ basefee: плану торопиться некуда, но и застрять он не должен
    assert tx["maxPriorityFeePerGas"] == int(_Cfg.tip_gwei * 1e9), tx
    assert tx["maxFeePerGas"] == 50_000_000 * 2 + int(_Cfg.tip_gwei * 1e9), tx
    # nonce главного кошелька продвинут — иначе следующая боевая tx уйдёт в дыру
    assert ex._LOCAL_NONCE[_Acct.address] == 8, ex._LOCAL_NONCE

    # --- 2. боевая tx в полёте: план ждёт, выстрел главнее -------------------------------
    with ex._INFLIGHT_LOCK:
        ex._INFLIGHT += 1
    try:
        assert ex.send_board_tx(_Rpc(), _Cfg(), cd, 130_000) is None, "план полез вперёд выстрела"
        assert len(SENT) == 1, "при _INFLIGHT>0 отправки быть не должно: %s" % SENT
    finally:
        with ex._INFLIGHT_LOCK:
            ex._INFLIGHT -= 1

    # --- 3. осечка отправки: nonce инвалидируется, наружу None ---------------------------
    def _boom(*a, **k):
        raise RuntimeError("nonce too low")

    ex.send_tx = _boom
    assert ex.send_board_tx(_Rpc(), _Cfg(), cd, 130_000) is None
    assert _Acct.address not in ex._LOCAL_NONCE, \
        "после осечки локальный nonce обязан быть сброшен, иначе следующая tx уйдёт в дыру"

    # --- 4. КОНТРОЛЬ: тест обязан падать, если убрать разрешение имени -------------------
    # Без этого «тест зелёный» неотличимо от «тест не дошёл до строки» — ровно так и жил дефект.
    ex.send_tx = lambda rpc, key, tx, wait=True, min_tip_wei=0, **kw: {"hash": "0xboard2"}
    del hp._fresh_basefee
    try:
        ex.send_board_tx(_Rpc(), _Cfg(), cd, 130_000)
        raise AssertionError("контроль: пропажа _fresh_basefee осталась незамеченной")
    except ImportError:
        pass                                   # имя разрешается ИЗ hotpath — что и требовалось
finally:
    ex.send_tx, ex.account_for, ex._pending_nonce, hp._fresh_basefee = orig
    ex._LOCAL_NONCE.pop(_Acct.address, None)

print("OK: настоящая send_board_tx исполняется, гейт выстрела держит, контроль сошёлся")
