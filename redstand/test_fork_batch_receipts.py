"""КОНТРАКТ batch_receipts ПРОТИВ НАСТОЯЩЕГО УЗЛА (#101).

Юнит-тест test_collector_no_fallback.py подменяет сетевой стык (_seq_session) — он доказывает
РЕШЕНИЕ, но не доказывает, что настоящий узел отвечает на батч именно так, как мы разбираем.
Здесь батч уходит в живой JSON-RPC (anvil), а хэш берётся из реально отправленной транзакции:

  * намайненный хэш      -> квитанция;
  * НЕИЗВЕСТНЫЙ узлу хэш -> ключ есть, значение None («запрошен, но не в блоке»);
  * мёртвый порт         -> ReceiptsUnavailable, а НЕ пустой словарь.

Именно на неразличимости второго и третьего исходов держался дефект: страховка уводила партию
на боевой Alchemy при каждом полле, где ещё ничего не намайнено (замер 12.08 на Шарлотте:
collector_fallback == polls в 134 окнах подряд).

Форк Base и web3 НЕ нужны: узел нужен только как настоящий JSON-RPC-собеседник, ходим к нему
голым HTTP — ровно так же, как ходит сам batch_receipts.

ЗАПУСК (не в общем наборе — нужен anvil):
    anvil --port 8547 --silent &
    FORK_RPC=http://127.0.0.1:8547 python3 test_fork_batch_receipts.py
"""
import json
import os
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RPC = os.environ.get("FORK_RPC", "")
if not RPC:
    print("SKIP: FORK_RPC не задан (нужен anvil, см. докстринг)")
    sys.exit(0)

import chain.execute as ex                                     # noqa: E402
import chain.rpc as rpc                                        # noqa: E402


def call(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read())
    assert not out.get("error"), out
    return out.get("result")


# --- реальная транзакция: её хэш узел знает --------------------------------------------
acct = call("eth_accounts", [])[0]
txh = call("eth_sendTransaction", [{"from": acct, "to": acct, "value": "0x0"}])
for _ in range(50):
    if call("eth_getTransactionReceipt", [txh]) is not None:
        break
    time.sleep(0.1)
UNKNOWN = "0x" + "ab" * 32                                     # узлу неизвестен

rpc.rpc_usage_take()
got = ex.batch_receipts(RPC, [txh, UNKNOWN])
assert set(got) == {txh, UNKNOWN}, "контракт нарушен на живом узле: %s" % sorted(got)
assert got[txh] is not None and int(str(got[txh].get("status")), 16) == 1, got[txh]
assert got[UNKNOWN] is None, \
    "неизвестный узлу хэш приехал не как None: %r — «не намайнено» снова неотличимо" % got[UNKNOWN]
assert got != {}, "живой узел с ненамайненной партией снова отдаёт пустоту"
print("OK 1/4: живой узел — намайненный резолвится, НЕИЗВЕСТНЫЙ приезжает как None "
      "(словарь НЕ пуст, страховке нечего срабатывать)")

# --- партия, где не намайнено НИЧЕГО: это НЕ отказ --------------------------------------
none_batch = ex.batch_receipts(RPC, ["0x" + "%064x" % i for i in range(1, 9)])
assert len(none_batch) == 8 and all(v is None for v in none_batch.values()), none_batch
print("OK 2/4: партия из 8 ненамайненных -> 8 ключей со значением None (не исключение)")

# --- учёт CU: батч виден телеметрии по ПОДЗАПРОСАМ --------------------------------------
snap = rpc.rpc_usage_take()
_n = sum(d.get("eth_getTransactionReceipt", 0) for d in snap.values())
assert _n == 10, "учтено %d подзапросов вместо 10 (у Alchemy тариф поштучный)" % _n
print("OK 3/4: учтено %d подзапросов = %d CU за два HTTP-поста" % (_n, rpc.rpc_usage_cu(snap)))

# --- мёртвый узел: ОТКАЗ, а не пустой словарь -------------------------------------------
dead = "http://127.0.0.1:1/"
_why = None
try:
    ex.batch_receipts(dead, [txh], timeout=2)
    raise AssertionError("мёртвый узел вернул словарь вместо ReceiptsUnavailable")
except ex.ReceiptsUnavailable as e:
    _why = str(e)
    assert "127.0.0.1:1" in _why, _why
print("OK 4/4: мёртвый узел -> ReceiptsUnavailable(%s) — отличие настоящее" % _why[:70])
print("=== контракт batch_receipts подтверждён на ЖИВОМ узле ===")
