"""Пробы spec-fire уходят с НОГ, главный кошелёк — только выстрелам (28.08, гонка 50572467)."""
import importlib, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eth_account import Account
K_MAIN = "0x" + "11" * 32; K_L1 = "0x" + "22" * 32; K_L2 = "0x" + "33" * 32
def _ex(env):
    os.environ.pop("SPEC_FIRE_PROBE_FROM_LEGS", None); os.environ.update(env)
    import chain.execute as ex; importlib.reload(ex)
    ex._VOLLEY_ACCTS = [(Account.from_key(K_L1).address, K_L1), (Account.from_key(K_L2).address, K_L2)]
    ex._VOLLEY_LOCKS.update({a: __import__("threading").Lock() for a, _ in ex._VOLLEY_ACCTS})
    sent = []
    def fake_send(rpc, key, tx, **kw):                       # путь ГЛАВНОГО (откат / нет ног)
        sent.append(Account.from_key(key).address); return {"hash": "0x" + "ab" * 32}
    ex.send_tx = fake_send
    def fake_volley(rpc, cfg, addr, key, calldata, gas, **kw):  # путь НОГИ (линза: тот же, что лестницы)
        sent.append(addr); return "0x" + "cd" * 32
    ex.send_volley_probe = fake_volley
    ex._pending_nonce = lambda w3, a: 0
    ex.account_for = lambda w3, key: Account.from_key(key)
    # гейты пробы (резерв/баланс/интервал/кап) — не предмет стенда: открываем
    ex.probe_allowed_by_reserve = lambda *a, **k: True
    ex.cached_balance_wei = lambda *a, **k: 10**18
    for name in ("_SPEC_LAST_TS", "_SPEC_PROBES", "_CANARY_GAS_USD"):
        if hasattr(ex, name): setattr(ex, name, 0)
    return ex, sent
class _Rpc:
    def _web3(self): return types.SimpleNamespace(eth=types.SimpleNamespace(chain_id=8453))
    chain_id = 8453
class _Cfg:
    wallet_key = K_MAIN; liquidator_address = "0x" + "aa" * 20; tip_gwei = 0.001
def _fire(ex, n):
    for _ in range(n):
        ex._SPECFIRE_MIN_INTERVAL = 0.0; ex._INFLIGHT = 0; ex._SPECFIRE_LAST_TS = 0.0   # гейты каденса — не предмет
        ex.send_spec_fire_probe(_Rpc(), _Cfg(), "0xff", 100000, log=None)
def test_probes_go_from_legs_round_robin():
    ex, sent = _ex({"SPEC_FIRE_PROBE_FROM_LEGS": "1"})
    _fire(ex, 4)
    main = Account.from_key(K_MAIN).address
    assert main not in sent, "проба ушла с ГЛАВНОГО: %s" % sent
    assert len(set(sent)) == 2 and sent[0] != sent[1], "ноги обязаны чередоваться: %s" % sent
def test_negative_control_rollback_uses_main():
    ex, sent = _ex({"SPEC_FIRE_PROBE_FROM_LEGS": "0"})
    _fire(ex, 2)
    assert set(sent) == {Account.from_key(K_MAIN).address}, "откат обязан вернуть главный: %s" % sent
def test_no_legs_falls_back_to_main():
    ex, sent = _ex({"SPEC_FIRE_PROBE_FROM_LEGS": "1"})
    ex._VOLLEY_ACCTS = []
    _fire(ex, 1)
    assert sent == [Account.from_key(K_MAIN).address], "без ног — главный (незнание не глушит пробу)"
if __name__ == "__main__":
    import pytest; sys.exit(pytest.main([__file__, "-q"]))
