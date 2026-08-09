"""ФОРК-РЕПЕТИЦИЯ ПОБЕДЫ (#68): та самая гонка 49666338, только без конкурентов.

Тракт плотности в бою не побеждал НИ РАЗУ. Форк-тесты контракта (38/38) доказывают контракт,
дрель огня (5/5) — что наши байты садятся в блок. Здесь замыкается последнее: НАСТОЯЩИЙ план
бота, армированный НАСТОЯЩИМ кодировщиком, срабатывает по НАСТОЯЩЕЙ жертве и отдаёт профит.

Стенд — реконструкция боевого проигрыша: форк на 49666337 (блок ПЕРЕД каскадом), затем
переигрывается ПОДПИСАННАЯ tx транзита оракула из 49666338. Позитивный контроль стенда: долг
жертвы обязан сойтись с призом, который в том блоке забрал конкурент ($16 495) — иначе мы
воспроизвели не ту гонку.

ЗАПУСК (нужен anvil + venv с web3):
    anvil --fork-url https://mainnet.base.org --fork-block-number 49666337 --port 8550 --silent &
    FORK_RPC=http://127.0.0.1:8550 python3 test_fork_win_rehearsal.py
"""
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RPC = os.environ.get("FORK_RPC", "")
if not RPC:
    print("SKIP: FORK_RPC не задан (нужен anvil-форк, см. докстринг)")
    sys.exit(0)

import requests                                            # noqa: E402
from eth_account import Account                            # noqa: E402
from web3 import Web3                                      # noqa: E402

import chain.execute as ex                                 # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MID = "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109"
# Цель задаётся снаружи: кит C ($16 494.54) не берётся прямым путём по глубине пула, поэтому
# по умолчанию репетируем приз снайпера D ($2 510) — он под капом маршрута и проверяет ФИКС.
BORROWER = os.environ.get("FORK_BORROWER", "0xfda213169284e80af6a9cbfd06431f50d6d8bf74")
PRIZE_USDC = float(os.environ.get("FORK_PRIZE", "2510.19"))
TRANSIT = "0xfb3460bed87acd44c350675d5b80c194f76ebead150c372246bf3cb31d92f63d"
ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
assert w3.is_connected(), RPC
owner = Account.from_key(ANVIL0)
S = requests.Session()


def rpc(method, params, url=RPC):
    return S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                             "params": params}, timeout=45).json()


def send(tx, key=ANVIL0):
    acct = Account.from_key(key)
    tx.setdefault("chainId", 8453)
    tx.setdefault("nonce", w3.eth.get_transaction_count(acct.address))
    tx.setdefault("maxFeePerGas", int(2e9))
    tx.setdefault("maxPriorityFeePerGas", int(1e6))
    tx.setdefault("gas", 3_000_000)
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h, timeout=60)


# --- 1. ПЕРЕИГРЫВАЕМ НАСТОЯЩИЙ ТРАНЗИТ -------------------------------------------------
raw = (rpc("eth_getRawTransactionByHash", [TRANSIT], "https://mainnet.base.org").get("result")
       or rpc("debug_getRawTransaction", [TRANSIT], "https://mainnet.base.org").get("result"))
# публичный узел Base не поддерживает eth_getRawTransactionByHash, но отдаёт
# debug_getRawTransaction — порядок именно такой, чтобы работать и там, где наоборот
assert raw, "сырую tx транзита не отдали ни одним методом"
# Параметры рынка — явные константы, СВЕРЕННЫЕ с цепью ниже (мой разбор ABI-структуры дал
# мусорный lltv; явные значения + проверка честнее, чем «умный» парсер, который врёт молча).
MP = {"loanToken": Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
      "collateralToken": Web3.to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
      "oracle": Web3.to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE"),
      "irm": Web3.to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
      "lltv": 625000000000000000}
_raw = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(
    text="idToMarketParams(bytes32)")[:4].hex() + MID[2:]}).hex()
for _k in ("loanToken", "collateralToken", "oracle", "irm"):
    assert MP[_k][2:].lower() in _raw.lower(), "%s не подтверждён цепью" % _k
assert "%064x" % MP["lltv"] in _raw.lower(), "lltv не подтверждён цепью"
oracle = MP["oracle"]
price_before = int(w3.eth.call({"to": oracle, "data": "0x" + Web3.keccak(text="price()")[:4].hex()}).hex(), 16)
rpc("eth_sendRawTransaction", [raw])
time.sleep(2)
price_after = int(w3.eth.call({"to": oracle, "data": "0x" + Web3.keccak(text="price()")[:4].hex()}).hex(), 16)
POST_TRANSIT = 1013981470000000000000000000000000000     # замеренная цена ПОСЛЕ транзита
if price_after == price_before:
    # повторный прогон на том же форке: транзит уже применён, nonce отправителя потрачен
    assert price_after == POST_TRANSIT, "цена не та и транзит не прошёл: %d" % price_after
    print("1/5 транзит УЖЕ применён на этом форке (цена %.6e) — продолжаем" % price_after)
else:
    assert price_after < price_before, "транзит не снизил цену: %d -> %d" % (price_before, price_after)
    print("1/5 транзит переигран: цена оракула %.6e -> %.6e" % (price_before, price_after))

# --- 2. ПОЗИТИВНЫЙ КОНТРОЛЬ СТЕНДА: долг сходится с призом конкурента -------------------
pos = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="position(bytes32,address)")[:4].hex()
                   + MID[2:] + "0" * 24 + BORROWER[2:]})
b_shares = int(pos[32:64].hex(), 16)
mkt = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="market(bytes32)")[:4].hex() + MID[2:]})
tba, tbs = int(mkt[64:96].hex(), 16), int(mkt[96:128].hex(), 16)
debt = b_shares * tba // tbs
assert abs(debt / 1e6 - PRIZE_USDC) < 2.0, \
    "долг %.2f не сошёлся с призом конкурента %.2f — воспроизведена НЕ ТА гонка" % (debt / 1e6, PRIZE_USDC)
print("2/5 контроль стенда: долг %.2f USDC = приз конкурента (%.2f)" % (debt / 1e6, PRIZE_USDC))

# --- 3. РАЗВОРАЧИВАЕМ v5 И АРМИРУЕМ НАСТОЯЩИЙ ПЛАН -------------------------------------
art = json.load(open("contracts/out/Liquidator.sol/Liquidator.json"))
bytecode = art["bytecode"]["object"]
ctor = Web3.to_bytes(hexstr="0" * 24 + MORPHO[2:])
rec = send({"data": bytecode + ctor.hex(), "to": None, "gas": 5_000_000})
LIQ = rec["contractAddress"]
mp = MP
# expected_seized(repaid, LIF, price): второй аргумент — МНОЖИТЕЛЬ (1.127), а не bps.
# Передав 1270, я умножал долг на 1270 и получал число, не влезающее в uint256 —
# ошибка единиц ровно того класса, что чинилась в #30 часом раньше.
seized = ex.expected_seized(debt, 1.127, price_after)      # бонус 12.7% рынка cbXRP
swap = ex.direct_swap(mp["collateralToken"], mp["loanToken"], seized, LIQ, LIQ,
                      repaid_assets=debt)
assert swap, "прямой роут cbXRP->USDC не собрался — план не построить"
print("   аргументы плана: lltv=%s shares=%s router=%s cd_len=%d" % (
    mp["lltv"], b_shares, swap["router"], len(swap["calldata"])))
cd = ex.encode_set_plan(mp, Web3.to_checksum_address(BORROWER), b_shares,
                        swap["router"], swap["calldata"], 0, int(time.time()) + 900, 0)
send({"to": LIQ, "data": cd})
print("3/5 v5 развёрнут (%s), НАСТОЯЩИЙ план армирован (роут %s)" % (LIQ[:12], swap["router"][:12]))

# --- 4. ВЫСТРЕЛ 1-БАЙТОВОЙ ПРОБОЙ -------------------------------------------------------
rec = send({"to": LIQ, "data": ex.V2_PROBE_CALLDATA, "gas": 3_000_000})
assert rec["status"] == 1, "проба ревертнула"
LIQ_TOPIC = Web3.keccak(text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
fired = [lg for lg in rec["logs"] if lg["topics"] and lg["topics"][0] == LIQ_TOPIC]
if not fired:
    print("   логов в квитанции: %d, газ %d" % (len(rec["logs"]), rec["gasUsed"]))
    # #70: контракт теперь НАЗЫВАЕТ причину — читаем PlanFailed(bytes4)
    PF = Web3.keccak(text="PlanFailed(bytes4)")
    for lg in rec["logs"]:
        t0 = lg["topics"][0] if lg["topics"] else b""
        if t0 == PF:
            print("   ПРИЧИНА ОТКАЗА (селектор): 0x%s" % lg["data"].hex()[:8])
        else:
            print("     from=%s topic0=%s" % (lg["address"][:12], t0.hex()[:20]))
assert fired, "СОБЫТИЯ Liquidate НЕТ — план не сработал (газ %d)" % rec["gasUsed"]
repaid = int(fired[0]["data"][:32].hex(), 16)
print("4/5 ПРОБА СРАБОТАЛА: Liquidate, погашено %.2f USDC, газ %d" % (repaid / 1e6, rec["gasUsed"]))

# --- 5. ПРОФИТ ДОШЁЛ ДО OWNER'А СКВОЗНЫМ ПУТЁМ ------------------------------------------
bal = int(w3.eth.call({"to": mp["loanToken"], "data": "0x70a08231" + "0" * 24
                       + owner.address[2:]}).hex(), 16)
assert bal > 0, "профит не дошёл до owner'а: баланс loan-токена 0"
print("5/5 профит у owner'а: %.2f USDC" % (bal / 1e6))
print("\nOK #68: та самая гонка 49666338 отыграна — план бота взял жертву, профит у владельца")
