"""ПРИЁМКА ПРОВОДКИ v6-D4 НА ФОРКЕ BASE (задача #135, приёмка «б» и «в»). Эталон — ЦЕПЬ.

Отличие от соседей:
  * `test_fork_v6d4.py`      — принимает КОНТРАКТ;
  * `test_fork_v6d4_wiring.py` — принимает МОДУЛЬ `chain/board_v6.py` (его кодирование);
  * ЗДЕСЬ — принимается БОЕВОЙ ПУТЬ: `SpecFireLive._arm_board` при `SPEC_FIRE_V6=1` доходит до
    настоящего контракта настоящим отправителем `chain.execute.send_owner_tx_v6`, а
    предподписанная лестница `presign_ladder` реально стреляет байтом с цепи.

Каноны стенда:
  * «ХЭШ TX — НЕ ЭФФЕКТ»: арм засчитывается ТОЛЬКО чтением стораджа, и читается он НЕЗАВИСИМО
    от board_v6 (сырой eth_call + eth_abi здесь же) — иначе модуль проверял бы сам себя.
  * ПАРНЫЙ КРАСНЫЙ у каждого пункта: малый лимит газа обязан дать «не сел»; байт 0x01 на той же
    доске обязан НЕ выстрелить (иначе сентинел 0xFF ничего не доказывает).
  * «0xFF ДОЕХАЛ ДО ЦЕЛИ» доказывается КАНАРЕЙКОЙ (unwrapTarget-контракт ревертит своим
    селектором, он доезжает до PlanFailed) — денежный путь и живой Morpho для этого не нужны.

Без web3 (его нет в этом окружении): сырой JSON-RPC + eth_account + eth_abi. Боевые функции при
этом НАСТОЯЩИЕ — подменён только HTTP-клиент узла, то есть шов НИЖЕ проверяемой логики.

ЗАПУСК:
    anvil --fork-url https://mainnet.base.org --fork-block-number 49666337 --port 8553 --silent &
    FORK_RPC=http://127.0.0.1:8553 python3 test_fork_v6_wiring.py
Без FORK_RPC — SKIP (код 0). Ненулевой код = провал приёмки.
"""
import os
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

RPC_URL = os.environ.get("FORK_RPC", "")
if not RPC_URL:
    print("SKIP: FORK_RPC не задан (нужен anvil-форк Base, см. докстринг)")
    sys.exit(0)

import json                                          # noqa: E402
import requests                                      # noqa: E402
import rlp                                           # noqa: E402
from eth_abi import decode as abi_decode             # noqa: E402
from eth_account import Account                      # noqa: E402
from eth_utils import keccak, to_checksum_address    # noqa: E402

import chain.board_v6 as bd                          # noqa: E402
import chain.execute as ex                           # noqa: E402
import chain.spec_fire_live as sfl_mod               # noqa: E402
from chain.spec_fire_live import SpecFireLive        # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ZERO = bd.ZERO_ADDR
MP = {"loanToken": to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
      "collateralToken": to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
      "oracle": to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE"),
      "irm": to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
      "lltv": 625000000000000000}
PX_SEL = "0x" + keccak(text="price()")[:4].hex()
T_PLANFAILED = keccak(text="PlanFailed(bytes4)")
# Группа НАРОЧНО не нулевая: при g=0 байт 0x01 (v5) означал бы «явная группа 0» и попал бы в ту
# же доску — красный контроль байта не доказывал бы ничего.
bd.SPEC_FIRE_V6_GROUP = 3

S = requests.Session()
_ID = [0]


def rpc(method, params):
    _ID[0] += 1
    r = S.post(RPC_URL, json={"jsonrpc": "2.0", "id": _ID[0], "method": method, "params": params},
               timeout=120).json()
    if "error" in r:
        raise RuntimeError("%s -> %s" % (method, r["error"]))
    return r["result"]


CHAIN_ID = int(rpc("eth_chainId", []), 16)


def _nonce(addr):
    return int(rpc("eth_getTransactionCount", [addr, "pending"]), 16)


def send(tx, key=ANVIL0, gas=6_000_000):
    acct = Account.from_key(key)
    tx = dict(tx)
    tx.setdefault("chainId", CHAIN_ID)
    tx.setdefault("nonce", _nonce(acct.address))
    tx.setdefault("maxFeePerGas", int(2e9))
    tx.setdefault("maxPriorityFeePerGas", int(1e6))
    tx.setdefault("gas", int(gas))
    tx.setdefault("value", 0)
    signed = acct.sign_transaction(tx)
    h = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
    for _ in range(300):
        rec = rpc("eth_getTransactionReceipt", [h])
        if rec:
            return rec
        time.sleep(0.05)
    raise RuntimeError("квитанция не пришла: %s" % h)


def call(to, data):
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": data}, "latest"])[2:])


def status_of(rec):
    return int(rec["status"], 16) if isinstance(rec["status"], str) else int(rec["status"])


def assert_artifact_fresh():
    """ГАРД СВЕЖЕСТИ АРТЕФАКТА (#129): стенд деплоит ГОТОВЫЙ артефакт, значит способен судить
    байткод, не соответствующий исходнику. Ловушка двухполярная, и страшнее та, где протухший
    артефакт С гардом даёт ЛОЖНЫЙ ЗЕЛЁНЫЙ. Эталон — keccak исходника, записанный компилятором."""
    a = json.load(open("contracts/out/LiquidatorV6.sol/LiquidatorV6.json"))
    want = ((a.get("metadata") or {}).get("sources", {})
            .get("src/LiquidatorV6.sol", {}).get("keccak256"))
    got = "0x" + keccak(pathlib.Path("contracts/src/LiquidatorV6.sol").read_bytes()).hex()
    if not want:
        raise SystemExit("АРТЕФАКТ БЕЗ metadata.sources: свежесть недоказуема — forge build")
    if want.lower() != got.lower():
        raise SystemExit("АРТЕФАКТ ПРОТУХ: keccak(исходника)=%s, в артефакте %s — прогони "
                         "`cd contracts && forge build`" % (got[:18], want[:18]))
    return a


def deploy():
    a = assert_artifact_fresh()
    rec = send({"to": None, "data": a["bytecode"]["object"] + ("0" * 24 + MORPHO[2:]).lower()},
               gas=8_000_000)
    assert status_of(rec) == 1, "деплой v6 не сел"
    return to_checksum_address(rec["contractAddress"])


def put_canary(sel4):
    a = to_checksum_address("0x%040x" % (0xCA0000 + int(sel4, 16) % 0xFFFF))
    rpc("anvil_setCode", [a, "0x63" + sel4 + "600052" + "6004601cfd"])
    return a


# ===================================================================================================
# ШИМ УЗЛА: подменён HTTP-клиент web3 (его в окружении нет), НЕ наши функции. Через него ходят
# НАСТОЯЩИЕ send_owner_tx_v6 / send_tx / _pending_nonce / presign_ladder.
# ===================================================================================================
class _Hex(bytes):
    def hex(self):                     # web3 отдаёт HexBytes: .hex() c префиксом
        return "0x" + bytes(self).hex()


class _Eth:
    account = Account

    def __init__(self):
        self.max_priority_fee = 1_000_000

    def get_transaction_count(self, addr, blk="pending"):
        return int(rpc("eth_getTransactionCount", [addr, blk]), 16)

    def get_block(self, blk):
        b = rpc("eth_getBlockByNumber", [blk if blk != "latest" else "latest", False])
        return {"baseFeePerGas": int(b.get("baseFeePerGas") or "0x0", 16)}

    def get_balance(self, addr):
        return int(rpc("eth_getBalance", [addr, "latest"]), 16)

    def send_raw_transaction(self, raw):
        return _Hex(bytes.fromhex(rpc("eth_sendRawTransaction",
                                      ["0x" + bytes(raw).hex()])[2:]))

    def get_transaction_receipt(self, h):
        r = rpc("eth_getTransactionReceipt", [h if isinstance(h, str) else h.hex()])
        if not r:
            return None
        return {"status": int(r["status"], 16), "blockNumber": int(r["blockNumber"], 16),
                "transactionHash": r["transactionHash"], "logs": r["logs"]}

    def call(self, tx, blk="latest"):
        return bytes.fromhex(rpc("eth_call", [tx, blk])[2:])


class _W3:
    def __init__(self):
        self.eth = _Eth()


class ForkRpc:
    chain_id = CHAIN_ID
    rpc_url = RPC_URL

    def __init__(self):
        self._w3o = _W3()

    def _web3(self):
        return self._w3o

    def eth_call(self, tx, block="latest", state_override=None):
        return self._w3o.eth.call(tx, block)

    def block_number(self):
        return int(rpc("eth_blockNumber", []), 16)


RESULTS, FAILS = [], []


def verdict(point, what, ok, extra=""):
    RESULTS.append((point, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
    print("  %s %-8s %s%s" % ("[OK]  " if ok else "[FAIL]", point, what,
                              (" | " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(point)


print("=" * 100)
print("ПРИЁМКА #135 НА ФОРКЕ: боевой путь _arm_board -> v6 на цепи; лестница стреляет 0xFF")
print("=" * 100)
rpc("evm_mine", [])
PX = int(call(MP["oracle"], PX_SEL).hex(), 16)
assert PX > 0, "оракул рынка отдал 0 — форк не архивный / не Base"
print("форк %s: chainId %d, oracle.price()=%.4e, группа доски g=%d"
      % (RPC_URL, CHAIN_ID, PX, bd.SPEC_FIRE_V6_GROUP))

ADDR = deploy()
CANARY_SEL = "aabbccdd"
CANARY = put_canary(CANARY_SEL)
LEG = Account.create()
send({"to": LEG.address, "value": int(2 * 10 ** 18)}, gas=21000)
send({"to": ADDR, "data": bd.encode_set_authorized_batch([LEG.address], True)}, gas=1_000_000)
OWNER = Account.from_key(ANVIL0)

# --- боевой объект: НАСТОЯЩИЙ SpecFireLive, транспорт — форк ---------------------------------
sfl_mod.SPEC_FIRE_PEAK_OWED = False
sfl_mod.SPEC_FIRE_V2_BOARD = True
bd.SPEC_FIRE_V6 = True

BORROWER = to_checksum_address("0x" + "be" * 20)
PLAN = {"mp": MP, "borrower": BORROWER, "repaid_shares": 7, "router": to_checksum_address("0x" + "51" * 20),
        "swap_calldata": "0x" + "11" * 2048, "min_profit": 0,
        # gate/триггер: px <= gate => огонь. Берём вдвое выше живой цены оракула.
        "trigger": PX * 2, "unwrap_target": CANARY}
PROBE = {"calldata": "0x" + "cd" * 900, "gas": 625_000, "swap_meta": None, "plan": PLAN}
MID = "0x" + "d7" * 32

fork_rpc = ForkRpc()
cfg = types.SimpleNamespace(liquidator_address=ADDR, gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=1730.0, wallet_key=ANVIL0,
                            fast_lane_max_repaid_usd=500_000.0)
shared = types.SimpleNamespace(lock=threading.Lock(), groups={MID: [BORROWER]},
                               debt_by={(MID, BORROWER): 1_500_000.0}, by_id={}, hf_by={})
LOGS = []
sf = SpecFireLive(rpc=fork_rpc, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                  dev_by={"BTC": 0.0010}, build_fn=lambda m, b, d: PROBE,
                  log_fn=LOGS.append, now=time.monotonic, sleep=lambda s: None)
sf._arm_retry = lambda *a, **k: None
# Ни одного шва v6 не ставим: call/send/head берутся боевые (`_v6_io`), то есть арм пойдёт
# через chain.execute.send_owner_tx_v6 и rpc.eth_call/block_number.

# ===================================================================================================
# F1 (б). КРАСНЫЙ ПЕРВЫМ: арм с заведомо малым лимитом газа обязан дать «НЕ СЕЛ».
# ===================================================================================================
print("\nF1 (б, красный). Арм с малым лимитом газа: обязан быть «не сел», а не «armed»")
_real_gas = bd.arm_gas_limit
bd.arm_gas_limit = lambda n, rb: 200_000
try:
    ok_red = sf._arm_board(PROBE, MID, BORROWER, "BTC", debt=1_500_000.0)
finally:
    bd.arm_gas_limit = _real_gas
_g_red = bd.read_group_info(call, ADDR, bd.SPEC_FIRE_V6_GROUP)
verdict("F1", "малый газ -> _arm_board=False, доска снята, сторадж группы ПУСТ, счётчик "
        "unlanded (не unknown)",
        ok_red is False and sf._board == {} and _g_red[3] == 0
        and sf.stats["board_v6_unlanded"] == 1 and sf.stats["board_v6_unknown"] == 0,
        "groupInfo=%s stats=%s" % (_g_red, {k: v for k, v in sf.stats.items()
                                            if k.startswith("board_v6")}))

# ===================================================================================================
# F2 (б). ЗЕЛЁНЫЙ: тот же вызов на боевом лимите. Эталон — СЫРОЙ СТОРАДЖ, читаемый ЗДЕСЬ.
# ===================================================================================================
print("\nF2 (б, зелёный). Боевой _arm_board: setGroup+setHot на цепи, сверка сырым стораджем")
_t0 = time.time()
ok = sf._arm_board(PROBE, MID, BORROWER, "BTC", debt=1_500_000.0)
_dt = time.time() - _t0
G = bd.SPEC_FIRE_V6_GROUP
# ЧИТАЕМ САМИ, не через board_v6: иначе модуль проверял бы сам себя.
_gi = abi_decode(["address", "uint64", "uint256", "uint8"],
                 call(ADDR, "0x03d480f3" + "%064x" % G))
_ti = abi_decode(["address", "uint128", "uint128", "uint256"],
                 call(ADDR, "0x6438be07" + "%064x" % G + "%064x" % 0))
_hi = abi_decode(["uint8", "uint8", "uint64"], call(ADDR, "0xa87531a8"))
print("    groupInfo(%d)=%s" % (G, _gi))
print("    targetInfo(%d,0)=%s" % (G, (_ti[0], _ti[1], _ti[2], "%.4e" % _ti[3])))
print("    hotInfo()=%s  (арм занял %.1fс)" % (_hi, _dt))
verdict("F2.1", "_arm_board=True и доска помечена боевой (verified) — но верим не ей, а строкам ниже",
        ok is True and sf._board.get("verified") is True and sf.stats["board_v6_arms"] == 1)
verdict("F2.2", "СЫРОЙ СТОРАДЖ: группа несёт наш оракул, n=1 и НАШЕГО заёмщика",
        _gi[0].lower() == MP["oracle"].lower() and _gi[3] == 1
        and _ti[0].lower() == BORROWER.lower(), "%s / %s" % (_gi[0], _ti[0]))
verdict("F2.3", "§6: в сторадже repaidShares=%d, seizedAssets=0 (заявка инвариантна к цене)"
        % PLAN["repaid_shares"], _ti[1] == PLAN["repaid_shares"] and _ti[2] == 0)
verdict("F2.4", "§4: указатель наведён на нашу группу, count=1, until == дедлайну ГРУППЫ С ЦЕПИ",
        _hi == (G, 1, _gi[1]), "hot=%s deadline=%d" % (_hi, _gi[1]))
verdict("F2.5", "проводка НАСТОЯЩАЯ: отправителем был chain.execute.send_owner_tx_v6 "
        "(швы _v6_*_fn не ставились)",
        sf._v6_send_fn is None and sf._v6_call_fn is None and sf._v6_head_fn is None)

# ===================================================================================================
# F3 (в). ЛЕСТНИЦА: реально подписана боевым presign_ladder и реально стреляет с цепи.
# ===================================================================================================
print("\nF3 (в). Предподписанная лестница: байт с ЦЕПИ + канарейка доказывает попадание в цель")
ex._VOLLEY_LOCKS[LEG.address] = threading.Lock()
ex._VOLLEY_BAL[LEG.address] = [10 ** 18, time.monotonic()]
ex._VOLLEY_LADDER.pop(LEG.address, None)
ex._VOLLEY_NONCE.pop(LEG.address, None)
ex.presign_ladder(fork_rpc, cfg, LEG.address, LEG.key.hex(), 3_000_000, depth=2,
                  base_fee=int(1e8), tip=1_000_000)
_st = ex._VOLLEY_LADDER[LEG.address]
_raw0 = _st["q"][0][1]
_h0 = rpc("eth_sendRawTransaction", [_raw0])
for _ in range(300):
    _rec0 = rpc("eth_getTransactionReceipt", [_h0])
    if _rec0:
        break
    time.sleep(0.05)
_tx0 = rpc("eth_getTransactionByHash", [_h0])
_pf = [lg for lg in _rec0["logs"]
       if lg["topics"] and bytes.fromhex(lg["topics"][0][2:]) == T_PLANFAILED]
_sel = ("0x" + _pf[0]["data"][2:10]) if _pf else None
verdict("F3.1", "ЦЕПЬ говорит: калдата предподписанной пробы = 0xff (не наш счётчик, а input tx)",
        _tx0["input"].lower() == "0xff", _tx0["input"])
verdict("F3.2", "0xFF доехал ДО ЦЕЛИ через _hot: PlanFailed нёс селектор канарейки 0x%s"
        % CANARY_SEL, _sel == ("0x" + CANARY_SEL), str(_sel))

# КРАСНЫЙ ТОГО ЖЕ ИЗМЕРЕНИЯ: байт v5 (0x01) на ТОЙ ЖЕ доске = «явная группа 0», она пуста ->
# тихий no-op. Значит сентинел 0xFF действительно необходим, а не совпал случайно.
_rec_red = send({"to": ADDR, "data": "0x01"}, key=LEG.key.hex(), gas=3_000_000)
_pf_red = [lg for lg in _rec_red["logs"]
           if lg["topics"] and bytes.fromhex(lg["topics"][0][2:]) == T_PLANFAILED]
verdict("F3.3", "КРАСНЫЙ: тот же выстрел байтом v5 (0x01) на той же доске НЕ попадает в цель "
        "(группа 0 пуста) — сентинел необходим",
        status_of(_rec_red) == 1 and not _pf_red, "логов PlanFailed=%d" % len(_pf_red))

# ===================================================================================================
# F4. §1 в бою: перезарядка группы под живым указателем ловится и лечится (clearHot -> setGroup).
# ===================================================================================================
print("\nF4. Перезарядка под живым указателем: обвязка сама снимает _hot (иначе HotSpanLive)")
_hi_before = abi_decode(["uint8", "uint8", "uint64"], call(ADDR, "0xa87531a8"))
sf._board = {}                     # сбросить дедуп: нас интересует ВТОРАЯ запись в ту же группу
ok2 = sf._arm_board(PROBE, MID, BORROWER, "BTC", debt=1_500_000.0)
_gi2 = abi_decode(["address", "uint64", "uint256", "uint8"],
                  call(ADDR, "0x03d480f3" + "%064x" % G))
_hi2 = abi_decode(["uint8", "uint8", "uint64"], call(ADDR, "0xa87531a8"))
verdict("F4", "переарм той же группы прошёл (дедлайн обновился), указатель снова наведён",
        ok2 is True and _gi2[1] >= _hi_before[2] and _hi2 == (G, 1, _gi2[1])
        and sf.stats["board_v6_arms"] == 2,
        "dl %d -> %d, hot=%s" % (_hi_before[2], _gi2[1], _hi2))

# ===================================================================================================
print("\n" + "=" * 100)
for p, st, what in RESULTS:
    print("%-6s %-8s %s" % (p, st, what))
print("=" * 100)
if FAILS:
    print("ФОРК-ПРИЁМКА #135 ПРОВАЛЕНА: %s" % ", ".join(FAILS))
    sys.exit(1)
print("ФОРК-ПРИЁМКА #135 ПРОШЛА: %d/%d" % (len(RESULTS), len(RESULTS)))
