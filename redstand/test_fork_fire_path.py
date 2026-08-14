"""ФОРК-РЕПЕТИЦИЯ БОЕВОГО ПУТИ ОГНЯ: настоящие пробы уходят в настоящую цепь.

Зачем. Весь тракт плотности (доска v5 -> движок -> пред-подписанные пробы -> коллектор
квитанций) НИ РАЗУ не работал в бою: за 7 суток 292 армированных окна, 273 с probes=0,
board_probes=0 за всю историю, fat-окон 0. Юниты покрывают его со швами на транспорте, форк-
тесты контракта (38/38) покрывают контракт — но между ними оставался незакрытый стык:
«наши подписанные байты, отправленные НАШИМ кодом, действительно садятся в блок и
действительно резолвятся НАШИМ коллектором».

Здесь этот стык и закрывается. Исполняются НАСТОЯЩИЕ presign_ladder, send_presigned_probe,
batch_receipts и ReceiptCollector против anvil-форка Base (реальное состояние сети, реальный
контракт v5, реальные балансы и nonce). Швов нет вообще — только адрес узла подменён.

ЗАПУСК (не в общем наборе — нужен anvil):
    anvil --fork-url https://mainnet.base.org --port 8547 --silent &
    forge create contracts/src/Liquidator.sol:Liquidator --rpc-url http://127.0.0.1:8547 \
        --private-key <anvil#0> --broadcast --constructor-args 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb
    FORK_RPC=http://127.0.0.1:8547 FORK_LIQUIDATOR=<addr> python3 test_fork_fire_path.py
"""
import os
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RPC = os.environ.get("FORK_RPC", "http://127.0.0.1:8547")
LIQ = os.environ.get("FORK_LIQUIDATOR", "")
if not LIQ:
    print("SKIP: FORK_LIQUIDATOR не задан (нужен anvil-форк, см. докстринг)")
    sys.exit(0)

from eth_account import Account                           # noqa: E402
from web3 import Web3                                     # noqa: E402

import chain.execute as ex                                # noqa: E402
import chain.fire_engine as fe                            # noqa: E402
from chain.rpc import BaseRpc                             # noqa: E402

ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
assert w3.is_connected(), "форк недоступен: %s" % RPC
funder = Account.from_key(ANVIL0)
rpc = BaseRpc(RPC, 8453)
cfg = types.SimpleNamespace(liquidator_address=Web3.to_checksum_address(LIQ),
                            tip_gwei=0.05, eth_price_usd=3000.0, sequencer_rpc=RPC,
                            wallet_key=ANVIL0)

print("форк: блок %d, chainId %d, контракт %s" % (
    w3.eth.block_number, w3.eth.chain_id, cfg.liquidator_address))

# --- ноги: свежие EOA, заправленные как боевые (0.008 ETH) --------------------------------
LEGS = [Account.create() for _ in range(3)]
FUND = int(0.008 * 10**18)
n = w3.eth.get_transaction_count(funder.address)
for i, leg in enumerate(LEGS):
    tx = {"to": leg.address, "value": FUND, "gas": 21000, "nonce": n + i, "chainId": 8453,
          "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
    w3.eth.send_raw_transaction(funder.sign_transaction(tx).raw_transaction)
for leg in LEGS:
    assert w3.eth.get_balance(leg.address) == FUND, "нога не заправлена"
print("ноги заправлены: %d x %.4f ETH" % (len(LEGS), FUND / 1e18))

_saved = (ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY)
try:
    ex._VOLLEY_DAY = int(time.time() // 86400)
    ex._VOLLEY_GAS_USD = 0.0
    ex._INFLIGHT = 0
    for leg in LEGS:
        ex._VOLLEY_LOCKS[leg.address] = threading.Lock()

    # --- 1. НАСТОЯЩИЙ presign_ladder против форка: подпись, nonce с ЦЕПИ, прогрев патрона --
    for leg in LEGS:
        depth = ex.presign_ladder(rpc, cfg, leg.address, leg.key.hex(),
                                  gas=3_000_000, depth=6)
        assert depth == 6, "лестница %s: %s" % (leg.address[:10], depth)
        assert ex._VOLLEY_BAL.get(leg.address), "прогрев патрона не сработал на реальном узле"
        assert ex._VOLLEY_BAL[leg.address][0] == FUND, ex._VOLLEY_BAL[leg.address][0]
    print("OK 1/5: лестницы подписаны, патрон прочитан с ЦЕПИ (%s)" %
          ", ".join("%s=%d" % (l.address[:8], ex.ladder_depth(l.address)) for l in LEGS))

    # --- 2. НАСТОЯЩИЙ send_presigned_probe -> байты реально садятся в блок ------------------
    sent = []
    for leg in LEGS:
        for _ in range(2):
            h = ex.send_presigned_probe(rpc, cfg, leg.address, broadcast_raw_urls=[RPC])
            assert h is not None, "проба не ушла с ноги %s" % leg.address[:10]
            sent.append(h)
    assert len(sent) == 6, sent
    time.sleep(1.0)
    mined = 0
    for h in sent:
        r = w3.eth.get_transaction_receipt(h)
        assert r is not None and r["to"].lower() == cfg.liquidator_address.lower()
        mined += 1
    print("OK 2/5: %d/%d проб СЕЛИ В БЛОК (наш контракт как получатель)" % (mined, len(sent)))

    # --- 3. nonce расходуются строго подряд (дыра повесила бы ногу) ------------------------
    for leg in LEGS:
        assert w3.eth.get_transaction_count(leg.address) == 2, \
            "нога %s: nonce %d вместо 2" % (leg.address[:10],
                                            w3.eth.get_transaction_count(leg.address))
    print("OK 3/5: nonce каждой ноги продвинут ровно на число отправленных проб")

    # --- 4. НАСТОЯЩИЙ batch_receipts: коллектор резолвит партию одним запросом -------------
    got = ex.batch_receipts(RPC, sent)
    assert len(got) == len(sent) and all(got.get(h) for h in sent), \
        "батч-резолв неполон: %d из %d" % (sum(1 for h in sent if got.get(h)), len(sent))
    _statuses = {int(got[h].get("status"), 16) if isinstance(got[h].get("status"), str)
                 else int(got[h].get("status")) for h in sent}
    print("OK 4/5: batch_receipts резолвил %d квитанций одним запросом (status=%s — "
          "no-op пробы по пустой доске)" % (len(got), _statuses))

    # --- 5. НАСТОЯЩИЙ ReceiptCollector: партия доходит до обработчика ----------------------
    done = []
    col = fe.ReceiptCollector(receipts_fn=lambda hs: ex.batch_receipts(RPC, hs),
                              on_result=lambda meta, h, r: done.append(h),
                              min_age_sec=0.0, timeout_sec=60.0)
    for h in sent:
        col.track(h, ("leg", None))
    col.tick()
    assert len(done) == len(sent), "коллектор довёл %d из %d" % (len(done), len(sent))
    assert col.stats["cu"] > 0, "расход коллектора не посчитан"
    print("OK 5/5: коллектор довёл все %d квитанций до обработчика (учтено %.0f CU)" % (
        len(done), col.stats["cu"]))

    # --- КОНТРОЛЬ: гейт патрона на РЕАЛЬНОМ балансе закрывает опустевшую ногу --------------
    poor = Account.create()
    ex._VOLLEY_LOCKS[poor.address] = threading.Lock()
    # Сумму берём ИЗ ФАКТИЧЕСКОЙ ЦЕНЫ, а не константой: порог немоты = пол + gas x max_fee, и он
    # уехал вместе с множителем резерва (#60: 100 -> 20). Константа в тесте протухла бы молча —
    # ровно то, что этот набор и ловит.
    _bf = w3.eth.get_block("latest").get("baseFeePerGas") or 0
    _mf = int(_bf) * ex._VOLLEY_PRESIGN_MAXFEE_MULT + int(cfg.tip_gwei * 1e9)
    _mute_below = ex._VOLLEY_BAL_FLOOR_WEI + 3_000_000 * _mf
    _amt = _mute_below - 10**13                      # на 0.00001 ETH НИЖЕ порога немоты
    print("порог немоты при базе %.4f gwei: %.6f ETH (даём %.6f)" % (
        _bf / 1e9, _mute_below / 1e18, _amt / 1e18))
    n2 = w3.eth.get_transaction_count(funder.address)
    tx = {"to": poor.address, "value": _amt, "gas": 21000, "nonce": n2,
          "chainId": 8453, "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
    w3.eth.send_raw_transaction(funder.sign_transaction(tx).raw_transaction)
    ex.presign_ladder(rpc, cfg, poor.address, poor.key.hex(), gas=3_000_000, depth=2)
    assert ex.send_presigned_probe(rpc, cfg, poor.address, broadcast_raw_urls=[RPC]) is None, \
        "нога ниже пола патрона выстрелила на реальном балансе"
    assert ex.volley_probe_skips()[0] > 0, "отказ пола не посчитан"
    print("OK контроль: нога ниже порога немоты молчит по полу патрона (баланс с ЦЕПИ)")
finally:
    (ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY) = _saved
    for leg in LEGS:
        for d in (ex._VOLLEY_LOCKS, ex._VOLLEY_LADDER, ex._VOLLEY_BAL, ex._VOLLEY_NONCE):
            d.pop(leg.address, None)

print("\nOK ФОРК: боевой путь огня замкнут на настоящей цепи — "
      "подпись -> отправка -> блок -> квитанция -> коллектор")
