"""ФОРК-СТЕНД #85: САДИТСЯ ЛИ АРМ ДОСКИ НА ЦЕПЬ — настоящая цепочка, без швов.

Зачем именно этот файл. Дефект #84 (лимит газа арма ниже цены самой записи) прожил три недели
НЕВИДИМЫМ для всех трёх уровней проверки ОДНОВРЕМЕННО:
  * юнит test_board_arm_lands.py подменяет `_board_arm_fn` и утверждает вакуумное `_gas>120_000`;
  * test_board_send_path.py передаёт газ ЛИТЕРАЛОМ, формулу не трогает вовсе;
  * test_fork_win_rehearsal.py шлёт setPlan СВОЕЙ функцией на gas=3M, не вызывая `_build`/`_arm_board`.
Формулу газа не исполнял НИКТО. Здесь она исполняется на настоящей цепи: `_prearm_board` ->
`_build` -> `_arm_board` -> `send_board_tx` -> `_verify_board`, и вердикт снимается с КВИТАНЦИИ
(status/gasUsed/gasLimit/PlanSet) и с `planInfo()`, а не с возвращённого хэша.

Подменены ТОЛЬКО адрес узла и источник целей (реестр shared, который в бою наполняет сканер).
Формула газа, `_board_arm_fn`, `send_board_tx` — настоящие.

ПОЗИТИВНЫЙ КОНТРОЛЬ обязателен и идёт ПЕРВЫМ: на том же форке, тем же кодом и той же калдатой
прогоняется ДО-фиксовая формула `120_000 + 22*байт`. Без него «после фикса село» неотличимо от
«садилось всегда».

ЗАПУСК (нужен anvil + venv с web3):
    anvil --fork-url https://mainnet.base.org --fork-block-number 49666337 --port 8551 --silent &
    FORK_RPC=http://127.0.0.1:8551 python3 test_fork_board_arm_lands.py
Без FORK_RPC печатает SKIP и выходит с нулём — офлайн-набор не ломается.
"""
import json
import os
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RPC_URL = os.environ.get("FORK_RPC", "")
if not RPC_URL:
    print("SKIP: FORK_RPC не задан (нужен anvil-форк, см. докстринг)")
    sys.exit(0)

# ВАЖНО: env читается на импорте модулей — ставим ДО них.
os.environ["SPEC_FIRE_V2_BOARD"] = "1"          # доска включается только с v5 (в бою так же)
os.environ.setdefault("SPEC_FIRE_MIN_REPAID_USD", "1000")   # боевой порог 500k; кит стенда $16.5k
os.environ.setdefault("SPEC_FIRE_ENGINE", "0")  # движок к арму отношения не имеет — не шумим
os.environ.setdefault("SPEC_FIRE_VOLLEY", "1")

import requests                                              # noqa: E402
from eth_account import Account                              # noqa: E402
from web3 import Web3                                        # noqa: E402

import chain.execute as ex                                   # noqa: E402
import chain.hotpath as hp                                   # noqa: E402
import chain.spec_fire_live as sfl_mod                       # noqa: E402
from chain.rpc import BaseRpc                                # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MID = "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109"
BORROWER = Web3.to_checksum_address(
    os.environ.get("FORK_BORROWER", "0xd57c4ceee023cfb78076bab0e31c1e5dfdfe2467"))
TRANSIT = "0xfb3460bed87acd44c350675d5b80c194f76ebead150c372246bf3cb31d92f63d"
ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
FEED, DIRECTION, DEV = "XRP", "down", 0.01

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 40}))
assert w3.is_connected(), RPC_URL
owner = Account.from_key(ANVIL0)
S = requests.Session()
PLAN_SET_T0 = Web3.keccak(text="PlanSet(uint64)")
LIQUIDATED_T0 = Web3.keccak(text="Liquidated(address,address,uint256)")
PLAN_FAILED_T0 = Web3.keccak(text="PlanFailed(bytes4)")
PLAN_EXEC_T0 = Web3.keccak(text="PlanExecuted(address,uint256)")


def rpc_call(method, params, url=RPC_URL):
    return S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                             "params": params}, timeout=60).json()


def send_raw(tx, key=ANVIL0):
    """Служебная отправка стенда (деплой, setAuthorized, заправка ноги) — НЕ боевой путь.
    Идёт с того же кошелька, что и боевой setPlan, поэтому после неё сбрасываем локальный
    кэш nonce execute.py: в бою этот кошелёк не используют мимо execute, здесь используем."""
    acct = Account.from_key(key)
    tx.setdefault("chainId", 8453)
    tx.setdefault("nonce", w3.eth.get_transaction_count(acct.address))
    tx.setdefault("maxFeePerGas", int(2e9))
    tx.setdefault("maxPriorityFeePerGas", int(1e6))
    tx.setdefault("gas", 6_000_000)
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=90)
    with ex._SEND_LOCK:
        ex._LOCAL_NONCE.pop(acct.address, None)
    return r


def plan_info(addr):
    raw = w3.eth.call({"to": addr, "data": "0x" + Web3.keccak(text="planInfo()")[:4].hex()})
    from eth_abi import decode as abi_decode
    b, dl, tp = abi_decode(["address", "uint64", "uint256"], bytes(raw))
    return b, int(dl), int(tp)


def show_receipt(tag, txh):
    r = w3.eth.get_transaction_receipt(txh)
    t = w3.eth.get_transaction(txh)
    planset = [lg for lg in r["logs"] if lg["topics"] and lg["topics"][0] == PLAN_SET_T0]
    print("   %-12s status=%d gasUsed=%d gasLimit=%d maxFeePerGas=%d PlanSet=%s логов=%d"
          % (tag, r["status"], r["gasUsed"], t["gas"], t.get("maxFeePerGas") or 0,
             "ДА" if planset else "НЕТ", len(r["logs"])))
    return r, t


print("=" * 78)
print("ФОРК-СТЕНД #85: арм доски — садится ли на цепь")
print("=" * 78)
print("форк %s: блок %d, chainId %d" % (RPC_URL, w3.eth.block_number, w3.eth.chain_id))

# --- 0. НАСТОЯЩИЙ ТРАНЗИТ ОРАКУЛА: делает жертву захватываемой (реконструкция 49666338) ----
MP_ORACLE = Web3.to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE")
px_sel = "0x" + Web3.keccak(text="price()")[:4].hex()
price_before = int(w3.eth.call({"to": MP_ORACLE, "data": px_sel}).hex(), 16)
raw = None
for _try in range(4):                     # публичный узел Base отвечает не всегда — переспрос
    for _m in ("eth_getRawTransactionByHash", "debug_getRawTransaction"):
        try:
            raw = rpc_call(_m, [TRANSIT], "https://mainnet.base.org").get("result")
        except Exception:
            raw = None
        if raw:
            break
    if raw:
        break
    time.sleep(3.0)
assert raw, "сырую tx транзита не отдали ни одним методом"
rpc_call("eth_sendRawTransaction", [raw])
time.sleep(1.5)
price_after = int(w3.eth.call({"to": MP_ORACLE, "data": px_sel}).hex(), 16)
print("0/8 транзит: цена оракула %.6e -> %.6e" % (price_before, price_after))

pos = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="position(bytes32,address)")[:4].hex()
                   + MID[2:] + "0" * 24 + BORROWER[2:]})
b_shares, coll_amt = int(pos[32:64].hex(), 16), int(pos[64:96].hex(), 16)
mkt = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="market(bytes32)")[:4].hex() + MID[2:]})
tba, tbs = int(mkt[64:96].hex(), 16), int(mkt[96:128].hex(), 16)
debt_usdc = b_shares * tba / tbs / 1e6
assert b_shares > 0 and coll_amt > 0, "жертва пуста — стенд не о том"
print("    жертва %s: долг %.2f USDC, залог %d" % (BORROWER[:12], debt_usdc, coll_amt))

# --- 1. РАЗВЁРТЫВАНИЕ v5 -------------------------------------------------------------------
art = json.load(open("contracts/out/Liquidator.sol/Liquidator.json"))
rec = send_raw({"data": art["bytecode"]["object"] + ("0" * 24 + MORPHO[2:]).lower(), "to": None})
LIQ = Web3.to_checksum_address(rec["contractAddress"])
assert plan_info(LIQ)[1] == 0, "свежий контракт уже с планом?!"
print("1/8 v5 развёрнут: %s (owner=%s), planInfo пуст" % (LIQ, owner.address[:12]))

# --- 2. НАСТОЯЩИЙ SpecFireLive: швов нет, кроме узла и источника целей ---------------------
rpc = BaseRpc(RPC_URL, 8453)
cfg = types.SimpleNamespace(liquidator_address=LIQ, wallet_key=ANVIL0, tip_gwei=0.05,
                            eth_price_usd=3000.0, sequencer_rpc="off", rpc_url=RPC_URL,
                            gas_limit_est=500000, fast_lane_max_repaid_usd=500000.0)
_mkt = types.SimpleNamespace(collateral_token="cbXRP", loan_token="USDC")
shared = types.SimpleNamespace(lock=threading.Lock(), groups={MID: [BORROWER]},
                               debt_by={(MID, BORROWER): debt_usdc},
                               by_id={MID: _mkt}, hf_by={})
LOG = []
sfl = sfl_mod.SpecFireLive(rpc, cfg, shared, ret_fn=lambda f: 0.0, dev_by={FEED: DEV},
                           log_fn=lambda m: (LOG.append(m), print("      | " + m))[0])
# боевой fee-путь: send_board_tx берёт basefee из слота фид-петли (в бою его пишет hotpath)
_bf = int(w3.eth.get_block("latest").get("baseFeePerGas") or 0)
hp._BF_LATEST = (w3.eth.block_number, _bf, time.monotonic())
ex._INFLIGHT = 0
print("2/8 SpecFireLive собран (реестр целей — из shared, как в бою из сканера)")

# --- 3. ПОЗИТИВНЫЙ КОНТРОЛЬ: ДО-фиксовая формула на той же цепи и той же калдате ----------
CAP = {}
_real_send_board = ex.send_board_tx


def _prefix_gas_send(_rpc, _cfg, calldata, gas, log=None, tag="board"):
    """Тот же боевой send_board_tx, но лимит газа — ПРЕЖНИЙ (`120_000 + 22*байт`).
    Подменена ровно одна величина: всё остальное (подпись, nonce, fee, транспорт) настоящее."""
    nb = len(calldata) // 2
    old = 120_000 + 22 * nb
    CAP.update({"bytes": nb, "gas_new": int(gas), "gas_old": old, "calldata": calldata})
    return _real_send_board(_rpc, _cfg, calldata, old, log=log, tag=tag)


ex.send_board_tx = _prefix_gas_send
sfl._prearm_board(FEED, DIRECTION, DEV)
assert CAP.get("calldata"), "путь не дошёл до отправки — арм не исполнился вовсе"
tx_old = (sfl._board or {}).get("tx")
assert tx_old, "хэш setPlan не получен (контрольный прогон)"
print("3/8 ПОЗИТИВНЫЙ КОНТРОЛЬ (формула ДО фикса, лимит %d):" % CAP["gas_old"])
r_old, t_old = show_receipt("pre-fix", tx_old)
pi_old = plan_info(LIQ)
print("   planInfo() после контроля: borrower=%s deadline=%d" % (pi_old[0], pi_old[1]))
assert r_old["status"] == 0, "ДО-фиксовая формула НЕ сожгла газ — вся посылка #84 под вопросом"
assert r_old["gasUsed"] == t_old["gas"] == CAP["gas_old"], "газ не исчерпан ровно по лимиту"
assert pi_old[1] == 0, "план оказался в контракте при сгоревшей tx?!"
print("   => арм СГОРЕЛ ровно как в бою: status=0, gasUsed==gasLimit==%d, PlanSet нет, "
      "planInfo пуст" % CAP["gas_old"])
# ХОЛОДНАЯ оценка — снимается ЗДЕСЬ: сгоревший арм сторадж не тронул, слоты ещё пусты.
EST_COLD = w3.eth.estimate_gas({"from": owner.address, "to": LIQ, "data": CAP["calldata"]})

# контроль верификатора: он ОБЯЗАН снять доску, а не поверить хэшу
time.sleep(max(2.0, sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC) + 4.0)
assert not sfl._board, "доска не снята после несевшего арма — верификатор поверил хэшу"
assert sfl.stats.get("board_unlanded") == 1, sfl.stats
assert not sfl._board_live(MID, BORROWER)
print("   => _verify_board снял доску ГРОМКО (board_unlanded=1), _board_live=False")

# контраст: 1-байтовая проба по пустому стораджу — тихий no-op
rec_noop = send_raw({"to": LIQ, "data": ex.V2_PROBE_CALLDATA, "gas": 3_000_000})
print("   контраст: проба по ПУСТОЙ доске -> status=%d, газ %d, логов %d (тихий no-op)"
      % (rec_noop["status"], rec_noop["gasUsed"], len(rec_noop["logs"])))
assert rec_noop["status"] == 1 and not rec_noop["logs"]

# --- 4. НАСТОЯЩИЙ ПУТЬ С ФИКСОМ ------------------------------------------------------------
ex.send_board_tx = _real_send_board
sfl._board = {}
sfl._board_probe_cache = {}
sfl._prearm_board(FEED, DIRECTION, DEV)
tx_new = (sfl._board or {}).get("tx")
assert tx_new, "арм не отправлен вовсе"
print("4/8 ПОСЛЕ ФИКСА (лимит max(700_000, 300_000+800*байт)):")
r_new, t_new = show_receipt("fixed", tx_new)
pi_new = plan_info(LIQ)
print("   planInfo(): borrower=%s deadline=%d trigger=%.4e" % (pi_new[0], pi_new[1], pi_new[2]))
assert r_new["status"] == 1, "арм с новым лимитом ВСЁ РАВНО не сел (status=0)"
assert r_new["gasUsed"] < t_new["gas"], "газ снова исчерпан по лимиту"
assert any(lg["topics"] and lg["topics"][0] == PLAN_SET_T0 for lg in r_new["logs"]), \
    "события PlanSet нет — плана в контракте нет"
assert pi_new[0].lower() == BORROWER.lower(), "в контракте ЧУЖОЙ заёмщик: %s" % pi_new[0]
assert pi_new[1] > int(time.time()), "дедлайн плана в прошлом"
print("   => арм СЕЛ: status=1, PlanSet есть, planInfo несёт НАШЕГО заёмщика")

# --- 5. ВЕРИФИКАТОР ------------------------------------------------------------------------
time.sleep(max(2.0, sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC) + 3.0)
assert sfl._board.get("verified") is True, "verified не выставлен: %s" % sfl._board
assert sfl.stats.get("board_verified") == 1, sfl.stats
assert sfl._board_live(MID, BORROWER) is True, "_board_live=False при севшем плане"
print("5/8 _verify_board: verified=True, board_verified=%d, _board_live=True"
      % sfl.stats["board_verified"])

# --- 6. ЧИСЛА ------------------------------------------------------------------------------
est_warm = w3.eth.estimate_gas({"from": owner.address, "to": LIQ, "data": CAP["calldata"]})
gas_used = r_new["gasUsed"]
print("6/8 ЗАМЕР:")
print("   калдата плана           %d байт" % CAP["bytes"])
print("   лимит ДО фикса          %d  (120_000 + 22*байт)" % CAP["gas_old"])
print("   лимит ПОСЛЕ фикса       %d  (max(700_000, 300_000+800*байт))" % CAP["gas_new"])
print("   estimateGas ХОЛОДНЫЙ    %d  (первая запись: слоты пусты)" % EST_COLD)
print("   estimateGas ГОРЯЧИЙ     %d  (перезапись поверх живого плана)" % est_warm)
print("   фактический gasUsed     %d  (ХОЛОДНАЯ первая запись)" % gas_used)
print("   запас лимита к факту    x%.2f  (+%d газа)" % (CAP["gas_new"] / gas_used,
                                                        CAP["gas_new"] - gas_used))
print("   дефицит ДО фикса        x%.2f  (лимит был в %.2f раза МЕНЬШЕ факта)"
      % (CAP["gas_old"] / gas_used, gas_used / CAP["gas_old"]))
print("   САМОПОДДЕРЖАНИЕ ДЕФЕКТА: старого лимита %d ХВАТИЛО БЫ на перезапись (%d), но не на"
      " первую запись (%d) — ни один арм не сел, сторадж навсегда остался холодным"
      % (CAP["gas_old"], est_warm, gas_used))
assert est_warm < CAP["gas_old"] < gas_used, (est_warm, CAP["gas_old"], gas_used)
maxfee = int(t_new.get("maxFeePerGas") or 0)
bal = w3.eth.get_balance(owner.address)
print("   резерв txpool gas*maxFee = %d * %d = %.6f ETH при балансе %.4f ETH (запас x%.0f)"
      % (CAP["gas_new"], maxfee, CAP["gas_new"] * maxfee / 1e18, bal / 1e18,
         bal / max(1, CAP["gas_new"] * maxfee)))
assert bal > CAP["gas_new"] * maxfee, "баланс не покрывает gas*maxFee — txpool отверг бы tx"

# 6b. ФОРМУЛА ПО ВСЕМУ ДИАПАЗОНУ РОУТА, а не в одной точке. Один замер не отличает «наклон
# верен» от «повезло на этом размере»: цена записи растёт с байтами роута, и если наклон
# формулы меньше настоящего, дефект вернётся на первом же длинном роуте (Kyber даёт килобайты).
# Замер холодный: свежий контракт, слоты пусты — как при каждом боевом арме.
_rec2 = send_raw({"data": art["bytecode"]["object"] + ("0" * 24 + MORPHO[2:]).lower(), "to": None})
LIQ2 = Web3.to_checksum_address(_rec2["contractAddress"])
_mp = {"loanToken": Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
       "collateralToken": Web3.to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
       "oracle": MP_ORACLE,
       "irm": Web3.to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
       "lltv": 625000000000000000}
print("   наклон формулы по длине роута (ХОЛОДНАЯ запись, свежий контракт %s):" % LIQ2[:12])
print("      %-9s %-9s %-11s %-11s %-9s" % ("роут,Б", "калдата,Б", "цена(cold)", "лимит(нов)", "запас"))
_pts = []
for _rn in (0, 200, 400, 800, 1600, 3200):
    _cd2 = ex.encode_set_plan(_mp, BORROWER, 1, _mp["loanToken"], "0x" + "11" * _rn, 0,
                              int(time.time()) + 900, 0)
    _nb2 = len(_cd2) // 2
    _cost = w3.eth.estimate_gas({"from": owner.address, "to": LIQ2, "data": _cd2})
    _lim = max(700_000, 300_000 + 800 * _nb2)
    _pts.append((_nb2, _cost))
    print("      %-9d %-9d %-11d %-11d x%.2f%s" % (_rn, _nb2, _cost, _lim, _lim / _cost,
                                                   "" if _lim > _cost else "  <-- НЕ ХВАТАЕТ"))
    assert _lim > _cost, "новая формула НЕ покрывает роут %d Б: лимит %d < цена %d" % (_rn, _lim, _cost)
_slope = (_pts[-1][1] - _pts[0][1]) / (_pts[-1][0] - _pts[0][0])
print("      замеренный наклон %.0f газа/байт против %d в формуле — запас РАСТЁТ с длиной роута"
      % (_slope, 800))
assert _slope < 800, "наклон формулы МЕНЬШЕ настоящей цены байта (%.0f) — дефект вернётся" % _slope

# --- 7. ДЫРА: подтверждение ЧУЖИМ (предыдущим) планом --------------------------------------
# Верификатор проверяет СВОЙСТВО СЛОТА («какой-то живой план по этому заёмщику»), а не ПОСАДКУ
# СВОЕЙ tx. Доска — один слот, локальная запись живёт TTL-30с, а план на цепи — все TTL. Значит
# переарм по ТОМУ ЖЕ заёмщику (а он и есть частый случай: кит держится в top-1 часами) при
# несевшей tx читает ПРЕЖНИЙ план: borrower тот же, дедлайн ещё в будущем — «подтверждено».
# Моделируем самый обычный отказ ПОСЛЕ фикса газа — tx не доехала (дроп мемпула, отказ
# секвенсера, SEND-3 uncertain): хэш есть, в блок ничего не село.
print("7/8 ПРОВЕРКА ДЫРЫ: НЕсевший переарм при живом ПРЕЖНЕМ плане на цепи")
_dl_before = plan_info(LIQ)[1]


def _dropped_send(_rpc, _cfg, calldata, gas, log=None, tag="board"):
    """setPlan не доехал: хэш вызывающему есть, в цепи — ничего (дроп/отказ секвенсера)."""
    return "0x" + "de" * 32


sfl._board = {}                       # локальная запись протухла (TTL-30) / рестарт бота
sfl._board_probe_cache = {}
sfl.stats["board_verified"] = 0
ex.send_board_tx = _dropped_send
sfl._prearm_board(FEED, DIRECTION, DEV)
_until_local = (sfl._board or {}).get("until")
time.sleep(max(2.0, sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC) + 4.0)
_dl_after = plan_info(LIQ)[1]
FALSE_CONFIRM = bool(sfl._board and sfl._board.get("verified"))
assert _dl_after == _dl_before, "tx всё-таки села — модель отказа неверна"
print("   переарм НЕ СЕЛ (в цепи ничего): дедлайн плана на цепи %d, не изменился" % _dl_after)
print("   вердикт верификатора: verified=%s, board_verified=%d, _board_live=%s"
      % (FALSE_CONFIRM, sfl.stats.get("board_verified", 0), sfl._board_live(MID, BORROWER)))
if FALSE_CONFIRM:
    _slack = (_until_local - sfl._now()) - (_dl_after - time.time())
    _worst = sfl_mod.SPEC_FIRE_V2_PLAN_TTL_SEC - 30.0      # переарм на исходе локального TTL
    print("   !! ДЫРА ПОДТВЕРЖДЕНА: несевший арм засчитан по ПРЕЖНЕМУ плану того же заёмщика.")
    print("      Локальный until пересчитан от НОВОГО арма, дедлайн на цепи остался от СТАРОГО.")
    print("      ЗДЕСЬ армы шли подряд, поэтому расхождение всего %.0fс (замер)." % _slack)
    print("      В БОЮ переарм приходит на исходе локального TTL (%.0fс) — тогда расхождение "
          "до %.0fс: доска «подтверждена», а плана на цепи уже нет (арифметика, не замер)."
          % (_worst, _worst - 30.0))
else:
    print("   дыра НЕ воспроизвелась: верификатор отличил прежний план от нового")

# 7b. КОНТРОЛЬ ТОЙ ЖЕ ДЫРЫ: по ДРУГОМУ заёмщику несевший арм ловится (сверка borrower работает)
_other = Web3.to_checksum_address("0xfda213169284e80af6a9cbfd06431f50d6d8bf74")
shared.groups = {MID: [_other]}
shared.debt_by = {(MID, _other): 2510.19}
sfl._board = {}
sfl._board_probe_cache = {}
sfl._prearm_board(FEED, DIRECTION, DEV)
time.sleep(max(2.0, sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC) + 4.0)
print("   контроль 7b (другой заёмщик, тот же несевший арм): доска %s"
      % ("снята — сверка borrower работает" if not sfl._board else "ЖИВА (сверка не работает!)"))
assert not sfl._board, "верификатор принял план ЧУЖОГО заёмщика"
shared.groups = {MID: [BORROWER]}
shared.debt_by = {(MID, BORROWER): debt_usdc}

# 7c. ГОНКА _verify_board vs следующий _arm_board: тред спит SETTLE_SEC и пишет вердикт в ТУ
# доску, что лежит в self._board на момент пробуждения, — а это уже доска СЛЕДУЮЩЕГО арма.
sfl._board = {}
sfl._board_probe_cache = {}
sfl.stats["board_verified"] = 0
sfl._prearm_board(FEED, DIRECTION, DEV)       # арм #1 (не сел), тред T1 уснул на SETTLE_SEC
_d1 = sfl._board
time.sleep(1.0)
sfl._board = {}
sfl._board_probe_cache = {}
sfl._prearm_board(FEED, DIRECTION, DEV)       # арм #2 (не сел) — новая доска, тот же ключ
_d2 = sfl._board
time.sleep(max(2.0, sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC) + 5.0)
print("   7c гонка: армов 2, СЕЛО 0, планов на цепи 1 (старый) -> board_verified=%d, "
      "доска #2 verified=%s (вердикт треда #1 сел на доску #2: %s)"
      % (sfl.stats.get("board_verified", 0), _d2.get("verified"), _d1 is not _d2))

# --- 8. ДОВОДКА: настоящая 1-байтовая проба с АВТОРИЗОВАННОЙ ноги ---------------------------
# Возвращаем корректное состояние: план на цепи должен быть свежим и подтверждённым.
ex.send_board_tx = _real_send_board
sfl._board = {}
sfl._board_probe_cache = {}
sfl._prearm_board(FEED, DIRECTION, DEV)
time.sleep(max(2.0, sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC) + 3.0)
assert sfl._board.get("verified"), "перед выстрелом доска не подтверждена"
assert sfl._board_live(MID, BORROWER), "перед выстрелом _board_live=False"

leg = Account.create()
send_raw({"to": leg.address, "value": int(0.5 * 10**18), "gas": 21000})
send_raw({"to": LIQ, "data": "0x" + Web3.keccak(text="setAuthorized(address,bool)")[:4].hex()
          + "0" * 24 + leg.address[2:].lower() + "0" * 63 + "1"})
assert w3.eth.call({"to": LIQ, "data": "0x" + Web3.keccak(text="isAuthorized(address)")[:4].hex()
                    + "0" * 24 + leg.address[2:].lower()}).hex().endswith("1"), "нога не авторизована"
ex._VOLLEY_LOCKS[leg.address] = threading.Lock()
ex._VOLLEY_DAY = int(time.time() // 86400)
ex._VOLLEY_GAS_USD = 0.0
tip = sfl_mod._probe_tip_wei(cfg.tip_gwei)
h = ex.send_volley_probe(rpc, cfg, leg.address, leg.key.hex(), ex.V2_PROBE_CALLDATA,
                         sfl_mod.SPEC_FIRE_V2_FIRE_GAS, max_fee=_bf * 2 + tip, tip=tip)
assert h, "нога не отправила пробу"
rec_p = w3.eth.wait_for_transaction_receipt(h, timeout=90)
_liq = [lg for lg in rec_p["logs"] if lg["topics"] and lg["topics"][0] == LIQUIDATED_T0
        and lg["address"].lower() == LIQ.lower()]
_pf = [lg for lg in rec_p["logs"] if lg["topics"] and lg["topics"][0] == PLAN_FAILED_T0]
print("8/8 НАСТОЯЩАЯ 1-байтовая проба с авторизованной ноги %s:" % leg.address[:12])
print("   status=%d gasUsed=%d логов=%d Liquidated=%s PlanFailed=%s"
      % (rec_p["status"], rec_p["gasUsed"], len(rec_p["logs"]), "ДА" if _liq else "нет",
         ("0x" + _pf[0]["data"].hex()[:8]) if _pf else "нет"))
assert rec_p["gasUsed"] > 60_000 or _liq or _pf, \
    "проба тихо ушла в no-op (%d газа) — план НЕ ВИДЕН пробе" % rec_p["gasUsed"]
if _liq:
    _profit = int(_liq[0]["data"][:32].hex(), 16)
    _usdc = int(w3.eth.call({"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                             "data": "0x70a08231" + "0" * 24 + owner.address[2:]}).hex(), 16)
    print("   => ПЛАН СРАБОТАЛ: Liquidated(profit=%.2f USDC), баланс owner'а %.2f USDC"
          % (_profit / 1e6, _usdc / 1e6))
    assert plan_info(LIQ)[1] == 0, "one-shot не сработал: план остался"
else:
    print("   => план ВИДЕН пробе (осмысленный отказ, не тихий no-op по пустому стораджу)")

print("=" * 78)
print("ВЕРДИКТ: арм доски САДИТСЯ на цепь с новой формулой; со старой — сгорает на том же "
      "форке и той же калдате.")
print("ДЫРА ПОДТВЕРЖДЕНИЯ ЧУЖИМ ПЛАНОМ: %s" % ("ЕСТЬ" if FALSE_CONFIRM else "не воспроизвелась"))
print("=" * 78)
