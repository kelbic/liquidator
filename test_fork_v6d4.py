"""ПРИЁМКА БОЕВОГО v6-D4 НА ФОРКЕ BASE (docs/SPEC_V6D4.md §5, восемь пунктов).

Продукт файла — ВЕРДИКТ ПО ВОСЬМИ ПУНКТАМ С КВИТАНЦИЙ. Ничего в бой не идёт: контракт новый,
адрес новый, деплоя нет.

Идиома взята у test_fork_gas_multislot.py (форк Base 49666337 + переигранный НАСТОЯЩИЙ транзит
оракула из 49666338, та же жертва, газ — с КВИТАНЦИИ, вердикт — с квитанции, не с хэша).

КАНОНЫ, ПО КОТОРЫМ ПОСТРОЕН СТЕНД:
 * ПОЗИТИВНЫЙ КОНТРОЛЬ ПЕРВЫМ. Пока настоящая ликвидация не прошла через v6 насквозь
   (Liquidated + Morpho.Liquidate + профит на квитанции), остальные числа не читаются.
 * У КАЖДОГО негативного теста есть ПАРНЫЙ ПОЗИТИВНЫЙ НА ТОМ ЖЕ ОБЪЕКТЕ. Тест, который умеет
   вернуть только ноль, ничего не доказывает: «огня нет» принимается лишь там, где на том же
   контракте и той же доске огонь БЫЛ показан.
 * ЭТАЛОН ОДНОСРЕЛЬНОСТИ — СЫРОЙ СТОРАДЖ (eth_getStorageAt), а не view контракта: число,
   которое механизм сообщает о себе сам, приёмкой не является. View сверяется со стораджем
   отдельной строкой — расхождение печатается, а не заметается.
 * ДЕФЕКТ §2.2 ДОКАЗЫВАЕТСЯ ДИФФЕРЕНЦИАЛЬНО: тот же сценарий подаётся ПРОТОТИПУ
   (LiquidatorV6Probe.sol:LiquidatorV6D4) — он обязан молчать там, где боевой v6 стреляет.
   Иначе «v6 стреляет» не отличить от «стенд не умеет ловить разницу».

ЗАПУСК (нужен anvil + python с web3). Форк обязан быть СВЕЖИЙ: транзит переигрывается один раз,
на уже отработанном форке стенд падает на сверке цены (и это правильно — грязное состояние не
должно давать чисел):
    anvil --fork-url https://mainnet.base.org --fork-block-number 49666337 --port 8551 --silent &
    FORK_RPC=http://127.0.0.1:8551 python3 test_fork_v6d4.py
Без FORK_RPC печатает SKIP и выходит с нулём. Ненулевой код возврата = провал приёмки.
"""
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RPC_URL = os.environ.get("FORK_RPC", "")
if not RPC_URL:
    print("SKIP: FORK_RPC не задан (нужен anvil-форк, см. докстринг)")
    sys.exit(0)

LIVE = "https://mainnet.base.org"

import requests                                            # noqa: E402
from eth_account import Account                            # noqa: E402
from web3 import Web3                                      # noqa: E402

import chain.execute as ex                                 # noqa: E402
from chain.hotpath import _seized_exact                    # noqa: E402
from strategy.pnl import lif_from_lltv                     # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MID = "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109"
BORROWER = Web3.to_checksum_address("0xfda213169284e80af6a9cbfd06431f50d6d8bf74")
PRIZE_USDC = 2510.19                       # приз, который в блоке 49666338 взял конкурент
TRANSIT = "0xfb3460bed87acd44c350675d5b80c194f76ebead150c372246bf3cb31d92f63d"
ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ZERO = "0x0000000000000000000000000000000000000000"

ROUTE_TYPICAL = 2048                       # боевой размер роута Kyber (STATE.md:2881)
TTL_SEC = 900
# Эталоны ТЗ §5.2 (прототип из репетиции Г1б-1: промах 54 906 газа при G=8 x M=8).
PROTO_REF_GAS = 54906
LIMIT_EXPLICIT = 55200
LIMIT_HOT = 58000
# ЭТАЛОН НЕИЗМЕННОСТИ ПРОМАХА (§5.9): числа ред.1 контракта, снятые ЭТИМ ЖЕ стендом на этом же
# блоке ДО ротации §2.4 (явный байт / горячий указатель count=1 / count=4). Ротация обязана их
# не сдвинуть: на промахе цикл огня не исполняется вовсе.
MISS_REF_R1 = (54965, 57431, 57571)          # ред.1/ред.2 (замер этого же стенда)
# +3 газа в ред.3 — цена СОГЛАСОВАНИЯ ГРАНИЦ TTL (§2.5в: `now >= deadline` вместо `>`), а НЕ
# ротации: ротация живёт после гарда `n == 0` и на промахе не исполняется. Attribution доказана
# диффом рантайм-байткода двух сборок: разница ровно 1 байт (лишний ISZERO), 3 газа.
MISS_TTL_COST = 3
MISS_REF = tuple(v + MISS_TTL_COST for v in MISS_REF_R1)
SEL_CANNOT_REPAY = "0x" + Web3.keccak(text="CannotRepay()")[:4].hex()
SEL_ERROR_STRING = "0x08c379a0"
VIRTUAL_SHARES = 10 ** 6
VIRTUAL_ASSETS = 1

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 120}))
assert w3.is_connected(), RPC_URL
owner = Account.from_key(ANVIL0)
S = requests.Session()

T_LIQUIDATED = Web3.keccak(text="Liquidated(address,address,uint256)")
T_PLANSET = Web3.keccak(text="PlanSet(uint64)")
T_PLANFAILED = Web3.keccak(text="PlanFailed(bytes4)")
T_PLANEXEC = Web3.keccak(text="PlanExecuted(address,uint256)")
T_SWAPINPUT = Web3.keccak(text="SwapInput(uint256)")
T_HOTSET = Web3.keccak(text="HotSet(uint8,uint8,uint64)")
T_MORPHO_LIQ = Web3.keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")

RESULTS = []          # (пункт §5, что померено, вердикт)
FAILS = []


def verdict(point, what, ok):
    RESULTS.append((point, what, ok))
    if not ok:
        FAILS.append(point)
    return ok


def rpc(method, params, url=RPC_URL):
    return S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                  timeout=120).json()


def send_raw(tx, key=ANVIL0, gas=6_000_000):
    acct = Account.from_key(key)
    tx.setdefault("chainId", 8453)
    tx.setdefault("nonce", w3.eth.get_transaction_count(acct.address))
    tx.setdefault("maxFeePerGas", int(2e9))
    tx.setdefault("maxPriorityFeePerGas", int(1e6))
    tx.setdefault("gas", gas)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h, timeout=300), signed.raw_transaction


ART = {}
for _n in ("LiquidatorV6.sol/LiquidatorV6", "LiquidatorV6Probe.sol/LiquidatorV6D4"):
    ART[_n.split("/")[-1]] = json.load(open("contracts/out/%s.json" % _n))


def deploy(name="LiquidatorV6"):
    a = ART[name]
    rec, _ = send_raw({"data": a["bytecode"]["object"] + ("0" * 24 + MORPHO[2:]).lower(),
                       "to": None}, gas=8_000_000)
    assert rec["status"] == 1, "деплой %s не сел" % name
    return Web3.to_checksum_address(rec["contractAddress"])


def cabi(name, addr=None):
    return w3.eth.contract(address=addr, abi=ART[name]["abi"])


def enc(name, fn, args):
    return cabi(name).encode_abi(fn, args=args)


def call_fn(name, addr, fn, args):
    return cabi(name, addr).functions[fn](*args).call()


def probe(addr, key, byte="01", gas=3_000_000, nonce=None):
    """НАСТОЯЩАЯ проба: отдельная tx с ноги, <=1 байт калдаты. Вердикт с КВИТАНЦИИ."""
    acct = Account.from_key(key)
    tx = {"to": addr, "data": "0x" + byte, "chainId": 8453, "gas": gas,
          "nonce": w3.eth.get_transaction_count(acct.address) if nonce is None else nonce,
          "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h, timeout=300)


def logs_of(rec, topic, addr=None):
    return [lg for lg in rec["logs"] if lg["topics"] and lg["topics"][0] == topic
            and (addr is None or lg["address"].lower() == addr.lower())]


def note(rec):
    pf = logs_of(rec, T_PLANFAILED)
    return "Liq=%s PlanExec=%s PlanFailed=%s логов=%d" % (
        "ДА" if logs_of(rec, T_LIQUIDATED) else "нет",
        "ДА" if logs_of(rec, T_PLANEXEC) else "нет",
        ("0x" + pf[0]["data"].hex()[:8]) if pf else "нет", len(rec["logs"]))


def snapshot():
    return rpc("evm_snapshot", [])["result"]


def revert(sid):
    r = rpc("evm_revert", [sid])
    assert r.get("result") is True, r


def storage_at(addr, slot):
    return int(w3.eth.get_storage_at(addr, slot).hex(), 16)


# --- СЫРОЙ СТОРАДЖ v6 (эталон приёмки; раскладка подтверждена forge inspect storage) ---------
#   _groups[8] c 3 (7 слотов на группу), _targets (mapping) слот 59, _hot слот 60.
SLOT_GROUPS, SLOT_TARGETS, SLOT_HOT = 3, 59, 60


def group_slot(g, off):
    return SLOT_GROUPS + 7 * g + off


def target_base(g):
    return int(Web3.keccak(hexstr="%064x%064x" % (g, SLOT_TARGETS)).hex(), 16)


def raw_target(addr, g, i):
    """(borrower, repaidShares, seizedAssets, triggerPrice) ИЗ СЫРОГО СТОРАДЖА."""
    b = target_base(g) + 7 * i
    borrower = storage_at(addr, b)
    packed = storage_at(addr, b + 1)
    return ("0x%040x" % borrower, packed & ((1 << 128) - 1), packed >> 128,
            storage_at(addr, b + 6))


def raw_group(addr, g):
    """(oracle, gate, deadline, n) ИЗ СЫРОГО СТОРАДЖА."""
    packed = storage_at(addr, group_slot(g, 6))
    return ("0x%040x" % storage_at(addr, group_slot(g, 2)), storage_at(addr, group_slot(g, 5)),
            packed & ((1 << 64) - 1), (packed >> 64) & 0xFF)


def raw_borrower(addr, g, i):
    """Один SLOAD: borrower цели (i-я ячейка группы g). Дешёвый зонд для замеров по флоту."""
    return storage_at(addr, target_base(g) + 7 * i)


def raw_hot(addr):
    v = storage_at(addr, SLOT_HOT)
    return v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & ((1 << 64) - 1)


def write_hot_raw(addr, g0, count, until):
    """Записать указатель ПРЯМО В СТОРАДЖ, минуя setHot. Нужно для проверки гардов ЧТЕНИЯ:
    они обязаны держать ЛЮБОЕ содержимое слота, а не только то, что пропустил арм (после ред.3
    негодный спан через setHot вообще не установить — а гард на чтении всё равно обязан быть)."""
    v = (int(g0) & 0xFF) | ((int(count) & 0xFF) << 8) | ((int(until) & ((1 << 64) - 1)) << 16)
    r = rpc("anvil_setStorageAt", [addr, hex(SLOT_HOT), "0x%064x" % v])
    assert r.get("result") in (True, None, "0x0"), r
    assert raw_hot(addr) == (int(g0), int(count), int(until)), "сырая запись указателя не легла"


print("=" * 100)
print("ПРИЁМКА v6-D4 (SPEC_V6D4.md §5): восемь пунктов, вердикт с КВИТАНЦИЙ")
print("=" * 100)
FORK_BLOCK = w3.eth.block_number
# Заголовок САМОГО форк-блока читать нельзя: апстрим отдаёт «backend response too large» на
# блоке каскада, и anvil пробрасывает ошибку. Мелем один пустой ЛОКАЛЬНЫЙ блок — дальше
# `latest` всегда локальный, и время цепи читается без похода в апстрим.
rpc("evm_mine", [])
CHAIN_TS = w3.eth.get_block("latest")["timestamp"]
print("форк %s: блок %d, chainId %d, block.timestamp %d" % (RPC_URL, FORK_BLOCK, w3.eth.chain_id,
                                                            CHAIN_TS))


def deadline():
    return int(max(w3.eth.get_block("latest")["timestamp"], time.time())) + TTL_SEC


# --- 0. ПОЗИТИВНЫЙ КОНТРОЛЬ САМОГО ФОРКА + транзит ------------------------------------------
MP = {"loanToken": Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
      "collateralToken": Web3.to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
      "oracle": Web3.to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE"),
      "irm": Web3.to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
      "lltv": 625000000000000000}
MP_T = (MP["loanToken"], MP["collateralToken"], MP["oracle"], MP["irm"], int(MP["lltv"]))
PX_SEL = "0x" + Web3.keccak(text="price()")[:4].hex()
# Отпечаток рынка (§2.5б): keccak256(abi.encode(MarketParams)) — 5 статических полей подряд.
MPH = Web3.keccak(hexstr="".join(["0" * 24 + MP["loanToken"][2:], "0" * 24 + MP["collateralToken"][2:],
                                  "0" * 24 + MP["oracle"][2:], "0" * 24 + MP["irm"][2:],
                                  "%064x" % int(MP["lltv"])]).lower())
# Есть ли в боевом ABI отпечаток в setHot: стенд обязан уметь прогоняться и на ред.2 (красный
# на базе), и на ред.3 — иначе «красный до / зелёный после» пришлось бы предъявлять двумя файлами.
_HOT_IN = [f for f in ART["LiquidatorV6"]["abi"] if f.get("name") == "setHot"][0]["inputs"]
HOT_FP = len(_HOT_IN) == 4
print("    редакция контракта под замером: setHot(%s) => отпечаток рынка в подписи: %s"
      % (",".join(i["type"] for i in _HOT_IN), "ЕСТЬ (ред.3)" if HOT_FP else "НЕТ (ред.2)"))

_chain_mp = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(
    text="idToMarketParams(bytes32)")[:4].hex() + MID[2:]}).hex()
for _k in ("loanToken", "collateralToken", "oracle", "irm"):
    assert MP[_k][2:].lower() in _chain_mp.lower(), "%s не подтверждён цепью" % _k

pos = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="position(bytes32,address)")[:4].hex()
                   + MID[2:] + "0" * 24 + BORROWER[2:]})
B_SHARES = int(pos[32:64].hex(), 16)
mkt = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="market(bytes32)")[:4].hex() + MID[2:]})
TBA, TBS = int(mkt[64:96].hex(), 16), int(mkt[96:128].hex(), 16)
_code = len(w3.eth.get_code(MORPHO))
px_before = int(w3.eth.call({"to": MP["oracle"], "data": PX_SEL}).hex(), 16)
DEBT = B_SHARES * TBA // TBS if TBS else 0
assert _code > 1000 and B_SHARES > 0 and px_before > 0, (
    "ФОРК ОТДАЁТ ПУСТОТУ (code=%d shares=%d price=%d) — апстрим не архивный" % (_code, B_SHARES, px_before))
assert abs(DEBT / 1e6 - PRIZE_USDC) < 0.01, (
    "долг жертвы %.6f != приз конкурента %.2f — воспроизведена НЕ ТА гонка" % (DEBT / 1e6, PRIZE_USDC))
print("0/  ПОЗИТИВНЫЙ КОНТРОЛЬ ФОРКА: Morpho code %dБ, долг жертвы %.6f USDC = приз конкурента "
      "(%.2f) => апстрим отдаёт НАСТОЯЩЕЕ состояние блока %d" % (_code, DEBT / 1e6, PRIZE_USDC, FORK_BLOCK))

raw = None
for _try in range(4):
    for _m in ("eth_getRawTransactionByHash", "debug_getRawTransaction"):
        try:
            raw = rpc(_m, [TRANSIT], LIVE).get("result")
        except Exception:
            raw = None
        if raw:
            break
    if raw:
        break
    time.sleep(3.0)
assert raw, ("сырую tx транзита не отдали ни одним методом — это НЕ повод подставить ноль: "
             "без транзита жертва не захватываема и позитивный контроль огня недостроим")
rpc("eth_sendRawTransaction", [raw])
time.sleep(1.0)
PX = int(w3.eth.call({"to": MP["oracle"], "data": PX_SEL}).hex(), 16)
assert PX < px_before, "транзит не снизил цену: %d -> %d" % (px_before, PX)
print("    транзит переигран: цена оракула %.6e -> %.6e (жертва стала захватываемой)" % (px_before, PX))

LIF_WAD = int(round(lif_from_lltv(MP["lltv"] / 10 ** 18) * 10 ** 18))
SEIZED = _seized_exact(B_SHARES, LIF_WAD, PX, TBA, TBS)
TRIG_FIRE = PX * 2                    # огонь: price() <= trigger
TRIG_HEAL = PX * 9 // 10              # здоров: price() > trigger  <- ГОСПОДСТВУЮЩИЙ случай
ORACLE_CODE = w3.eth.get_code(MP["oracle"]).hex()
print("    жертва: shares %d, seized(точно) %d, LIF %.4f; TRIG_FIRE %.4e / TRIG_HEAL %.4e"
      % (B_SHARES, SEIZED, LIF_WAD / 1e18, TRIG_FIRE, TRIG_HEAL))


def route_for(addr, amount=None, min_out=None):
    """НАСТОЯЩИЙ роут боевым сборщиком (ex.direct_swap); получатель = контракт под замером."""
    s = ex.direct_swap(MP["collateralToken"], MP["loanToken"], int(amount or SEIZED), addr, addr,
                       repaid_assets=int(min_out or DEBT))
    assert s, "прямой роут cbXRP->USDC не собрался — позитивный контроль огня не построить"
    return Web3.to_bytes(hexstr=s["calldata"]), Web3.to_checksum_address(s["router"])


_r0, ROUTER = route_for(Web3.to_checksum_address("0x" + "11" * 20))
print("    настоящий роут: router=%s, калдата %d байт (боевой размер роута для арма %d Б)"
      % (ROUTER[:12], len(_r0), ROUTE_TYPICAL))

# --- ноги ------------------------------------------------------------------------------------
LEGS = []                                  # авторизуются setAuthorizedBatch (пункт §5.8)
# Нужны ноги ВСЕХ классов вычета: mod 4 — для спана горячего указателя (§5.4), mod 3 — для
# ротации старта цикла огня (§5.9, n=3). Класс вычета — это и есть «смещение ноги».
while (len({int(Web3.keccak(hexstr=a.address).hex(), 16) % 8 for a in LEGS}) < 8
       or len({int(a.address, 16) % 4 for a in LEGS}) < 4
       or len({int(Web3.keccak(hexstr=a.address).hex(), 16) % 3 for a in LEGS}) < 3
       or len(LEGS) < 5):
    LEGS.append(Account.create())
    if len(LEGS) > 200:
        break
assert len({int(Web3.keccak(hexstr=a.address).hex(), 16) % 8 for a in LEGS}) == 8, \
    "не собрались ноги всех СТАРТОВЫХ индексов"
OUTSIDER = Account.create()                # НЕ авторизуется никогда (пункт §5.8)


def start_model(addr, n):
    """Старт цикла огня по §2.4 ред.3: keccak256(abi.encodePacked(msg.sender)) % n.
    Это МОДЕЛЬ. Она не принимается на веру: §5.9 сверяет её с ЗАМЕРОМ по каждой ноге и
    печатает долю совпадений. Прежняя модель (`uint160(addr) % n`) была вычетом ТОГО ЖЕ числа,
    что и выбор группы — на ней стенд и покраснел, когда контракт развязали."""
    return int(Web3.keccak(hexstr=addr).hex(), 16) % n


def leg_with(mod, r):
    """Нога с ЗАДАННЫМ стартовым индексом при n=mod (выбор ЦЕЛИ). Выбор ГРУППЫ — другой вычет
    (`uint160(addr) % count`), для него ноги подбираются отдельно (§5.4)."""
    return next(a for a in LEGS if start_model(a.address, mod) == r)


def hot_until():
    """TTL указателя. С ред.3 группа обязана пережить указатель (`gr.deadline >= until`), а армы
    стенда делаются на deadline()=now+TTL за секунды до этого — берём заведомо меньший срок."""
    return deadline() - 60


# --- ФРОНТ ДЛЯ ЗАМЕРА РОТАЦИИ (§5.9): не 4 ноги, а флот -------------------------------------
# 512 ног ~ 4.6x боевого флота (110). Столько нужно ИМЕННО ДЛЯ ЧЕСТНОГО ПОКРЫТИЯ: при count=8
# на группу приходится ~64 ноги, и «8 целей из 8» перестаёт зависеть от везения выборки.
FLEET_N = int(os.environ.get("V6_FLEET", "512"))
FLEET = [Account.create() for _ in range(FLEET_N)]
for _a in FLEET:                            # заправка чит-кодом: заправка — не предмет замера
    rpc("anvil_setBalance", [_a.address, hex(10 ** 18)])
print("    фронт замера ротации: %d ног (боевой флот 110)" % FLEET_N)

# НАСТОЯЩИЙ ЧУЖОЙ РЫНОК Base — для негатива отпечатка (§5.11) и демонстрации увода флота
_f2m = json.load(open("feed_to_market.json"))
FOREIGN = None
for _agg, _v in _f2m.items():
    for _mid in _v.get("markets", []):
        try:
            _raw = bytes(w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(
                text="idToMarketParams(bytes32)")[:4].hex() + _mid[2:]}))
            _lt, _ct, _orc, _irm, _lltv = ("0x" + _raw[12:32].hex(), "0x" + _raw[44:64].hex(),
                                           "0x" + _raw[76:96].hex(), "0x" + _raw[108:128].hex(),
                                           int(_raw[128:160].hex(), 16))
            if _orc.lower() in (MP["oracle"].lower(), "0x" + "00" * 20):
                continue
            if int(w3.eth.call({"to": Web3.to_checksum_address(_orc), "data": PX_SEL}).hex(), 16) == 0:
                continue
        except Exception:
            continue
        FOREIGN = (Web3.to_checksum_address(_lt), Web3.to_checksum_address(_ct),
                   Web3.to_checksum_address(_orc), Web3.to_checksum_address(_irm), _lltv)
        break
    if FOREIGN:
        break
assert FOREIGN, "чужой рынок Base с живым оракулом не найден — негатив отпечатка не построить"
FOREIGN_MPH = Web3.keccak(hexstr="".join(["0" * 24 + FOREIGN[0][2:], "0" * 24 + FOREIGN[1][2:],
                                          "0" * 24 + FOREIGN[2][2:], "0" * 24 + FOREIGN[3][2:],
                                          "%064x" % FOREIGN[4]]).lower())
print("    чужой рынок для негативов: oracle=%s (отпечаток %s...)"
      % (FOREIGN[2][:12], FOREIGN_MPH.hex()[:10]))


# --- ПРИБОР ВЫБОРА ЯЧЕЙКИ: 64 канарейки (по одной на клетку доски) -------------------------
# Замер «какую цель выбрало кольцо» НЕ обязан жечь настоящую ликвидацию: 750 ликвидаций на
# публичном апстриме упираются в rate-limit (первый прогон этого стенда там и завис — anvil
# ждал ответа узла). Канарейка = unwrapTarget цели: `_liquidate` первым делом зовёт
# IERC4626(unwrapTarget).asset(), канарейка ревертит СВОИМИ 4 байтами, они доезжают до
# PlanFailed — и квитанция САМА называет клетку (g,i). Денежный путь при этом не исполняется:
# он проверяется отдельно (§5.1, §5.3 и подпункт (в) ниже — там ликвидации настоящие).
CANARY, SEL2CELL = {}, {}
for _g in range(8):
    for _i in range(8):
        _a = Web3.to_checksum_address("0x%040x" % (0xCA0000 + _g * 16 + _i))
        _sel = "aa%02x%02x00" % (_g, _i)
        rpc("anvil_setCode", [_a, "0x63" + _sel + "600052" + "6004601cfd"])
        CANARY[(_g, _i)] = _a
        SEL2CELL["0x" + _sel] = (_g, _i)
try:
    w3.eth.call({"to": CANARY[(0, 0)], "data": "0x" + Web3.keccak(text="asset()")[:4].hex()})
    raise AssertionError("канарейка не ревертит — прибор выбора ячейки неисправен")
except Exception as _e:
    assert "aa000000" in repr(_e).lower() or "revert" in repr(_e).lower(), repr(_e)
print("    прибор выбора ячейки: 64 канарейки поставлены (отпечаток отказа = номер клетки)")


def authorize_fleet(addr, legs):
    for k in range(0, len(legs), 128):
        cd = enc("LiquidatorV6", "setAuthorizedBatch", [[a.address for a in legs[k:k + 128]], True])
        r, _ = send_raw({"to": addr, "data": cd}, gas=29_000_000)
        assert r["status"] == 1


def ring_first(start, n, eligible):
    """Эталон выбора цели: первый по КОЛЬЦУ индекс из `eligible`, считается вне контракта."""
    for k in range(n):
        i = (start + k) % n
        if i in eligible:
            return i
    return None
for a in LEGS + [OUTSIDER]:
    send_raw({"to": a.address, "value": int(2 * 10 ** 18), "gas": 21000})
LEG = LEGS[0]
print("    ног стенда: %d (остатки msg.sender %% 4: %s), плюс НЕавторизованный чужак %s"
      % (len(LEGS), sorted({int(a.address, 16) % 4 for a in LEGS}), OUTSIDER.address[:10]))


def target(borrower=BORROWER, shares=None, seized=0, route=None, route_len=None,
           trig=TRIG_HEAL, minprofit=0, unwrap=ZERO):
    r = route if route is not None else bytes.fromhex("11" * (route_len or 0))
    sh = B_SHARES if shares is None else shares
    return (borrower, int(sh), int(seized), ROUTER, r, int(minprofit), unwrap, int(trig))


def proto_target(borrower=BORROWER, shares=None, route=None, route_len=None, trig=TRIG_HEAL):
    r = route if route is not None else bytes.fromhex("11" * (route_len or 0))
    return (borrower, int(B_SHARES if shares is None else shares), ROUTER, r, 0, ZERO, int(trig))


def arm(addr, g, ts, gate=TRIG_FIRE, dl=None, name="LiquidatorV6"):
    fn = "setGroup"
    cd = enc(name, fn, [g, MP_T, int(dl or deadline()), int(gate), ts])
    r, _ = send_raw({"to": addr, "data": cd}, gas=29_000_000)
    assert r["status"] == 1 and logs_of(r, T_PLANSET), "арм g=%d не сел (gasUsed=%d)" % (g, r["gasUsed"])
    return r


def authorize_batch(addr, accts):
    cd = enc("LiquidatorV6", "setAuthorizedBatch", [[a.address for a in accts], True])
    r, _ = send_raw({"to": addr, "data": cd})
    assert r["status"] == 1
    return r


def set_hot(addr, g0, count, until=None, mph=None, expect=1, gas=6_000_000):
    """Указатель. На ред.3 подпись несёт отпечаток рынка (§2.5б), на ред.2 — нет; стенд подаёт
    то, что объявлено в ABI, и вердикт выносит по КВИТАНЦИИ (expect=0 — ждём отката).

    TTL указателя по умолчанию берётся ИЗ ЦЕПИ (минимум дедлайнов групп спана), а не из
    настенных часов: ред.3 требует `gr.deadline >= until`, и указатель, живущий дольше плана,
    законно отвергается. Первый прогон стенда на этом и споткнулся — сам стенд после 512 проб
    просил TTL длиннее плана, и гард сработал правильно."""
    if until is None:
        _dls = [raw_group(addr, g)[2] for g in range(int(g0), min(8, int(g0) + max(1, int(count))))]
        _dls = [d for d in _dls if d > 0]
        until = min(_dls) if _dls else deadline() - 60
    args = [int(g0), int(count), int(until)]
    if HOT_FP:
        args.append(mph if mph is not None else MPH)
    r, _ = send_raw({"to": addr, "data": enc("LiquidatorV6", "setHot", args)}, gas=gas)
    if expect is not None:
        assert r["status"] == expect, ("setHot(g0=%d,count=%d) status=%d, ожидался %d"
                                       % (g0, count, r["status"], expect))
    return r


def authorize_one(addr, who, name="LiquidatorV6"):
    cd = enc(name, "setAuthorized", [who, True])
    r, _ = send_raw({"to": addr, "data": cd})
    assert r["status"] == 1


def is_auth(addr, who):
    got = w3.eth.call({"to": addr, "data": "0x" + Web3.keccak(text="isAuthorized(address)")[:4].hex()
                       + "0" * 24 + who[2:].lower()})
    return int(got.hex(), 16) == 1


BASE = snapshot()

# ============================================================================================
# П1. ПОЗИТИВНЫЙ КОНТРОЛЬ: настоящая ликвидация НАСКВОЗЬ через v6 (§5.1)
# ============================================================================================
print()
print("=" * 100)
print("П1 (§5.1). ПОЗИТИВНЫЙ КОНТРОЛЬ ОГНЯ — без него остальные числа не читаются")
print("=" * 100)
sid = snapshot()
c = deploy()
authorize_batch(c, LEGS)
arm(c, 2, [target(route=route_for(c)[0], trig=TRIG_FIRE)])
print("    арм по цепи: groupInfo(2)=%s, targetInfo(2,0)[0]=%s"
      % (call_fn("LiquidatorV6", c, "groupInfo", [2])[1:],
         call_fn("LiquidatorV6", c, "targetInfo", [2, 0])[0]))
r1 = probe(c, LEG.key.hex(), "03")                       # байт 0x03 => группа #3 (индекс 2)
_liq = logs_of(r1, T_LIQUIDATED, c)
_mliq = logs_of(r1, T_MORPHO_LIQ)
P1_GAS = r1["gasUsed"]
P1_PROFIT = int(_liq[0]["data"][:32].hex(), 16) / 1e6 if _liq else 0.0
print("    проба 0x03 с авторизованной ноги: status=%d gasUsed=%d %s" % (r1["status"], P1_GAS, note(r1)))
print("    => Liquidated(profit=%.2f USDC) | Morpho.Liquidate=%s | PlanExecuted=%s"
      % (P1_PROFIT, "ДА" if _mliq else "нет", "ДА" if logs_of(r1, T_PLANEXEC, c) else "нет"))
P1_OK = bool(_liq) and bool(_mliq) and P1_PROFIT > 0 and bool(logs_of(r1, T_PLANEXEC, c))
verdict("§5.1", "ликвидация насквозь: profit %.2f USDC, %d газа (Liquidated+Morpho.Liquidate)"
        % (P1_PROFIT, P1_GAS), P1_OK)
revert(sid)
if not P1_OK:
    print("\nПОЗИТИВНЫЙ КОНТРОЛЬ ПРОВАЛЕН — остальные числа НЕ ЧИТАЮТСЯ (канон).")
    sys.exit(1)

# ============================================================================================
# П2. ЦЕНА ПРОМАХА O(1) (§5.2): явный байт <= 55 200, горячий указатель <= 58 000, при G=8 M=8
# ============================================================================================
print()
print("=" * 100)
print("П2 (§5.2). ЦЕНА ПРОМАХА при ПОЛНОСТЬЮ АРМЛЕНЫХ G=8 x M=8 = 64 цели. Газ — с квитанции")
print("=" * 100)
sid_p2 = snapshot()


def arm_board(addr, G, M, name="LiquidatorV6", route_len=ROUTE_TYPICAL, gate=TRIG_HEAL):
    """G групп по M целей, ВСЕ группы на ОДНОМ И ТОМ ЖЕ (нашем) рынке.

    Почему один рынок: в репетиции Г1б-1 «немонотонность по G» оказалась РАЗНОЙ ЦЕНОЙ РАЗНЫХ
    оракулов (доминирующий член промаха — вызов оракула, 54 906..92 467 между рынками Base).
    Зависимость от G/M меряется при ЗАФИКСИРОВАННОМ адресуемом оракуле; разброс по оракулам —
    отдельной строкой ниже."""
    tf = target if name == "LiquidatorV6" else proto_target
    gas = []
    for g in range(G):
        r = arm(addr, g, [tf(route_len=route_len, trig=TRIG_HEAL) for _ in range(M)], gate=gate,
                name=name)
        gas.append(r["gasUsed"])
    return gas


def miss_gas(addr, key, byte):
    """Промах меряется ДВАЖДЫ: совпадение доказывает, что это число, а не шум."""
    a = probe(addr, key, byte)
    b = probe(addr, key, byte)
    assert a["status"] == 1 and not a["logs"], "промах не молчалив: %s" % note(a)
    assert a["gasUsed"] == b["gasUsed"], "промах не детерминирован: %d != %d" % (a["gasUsed"], b["gasUsed"])
    return a["gasUsed"]


t0 = time.time()
c88 = deploy()
authorize_batch(c88, LEGS)
ARM_GAS = arm_board(c88, 8, 8)
_g, _ga, _dl, _n = raw_group(c88, 7)
_tb = raw_target(c88, 7, 7)
print("    доска армлена (СЫРОЙ сторадж, группа #8 цель #8): oracle=%s n=%d deadline=%d borrower=%s"
      % (_g[:12], _n, _dl, _tb[0][:12]))
assert _n == 8 and _dl > 0 and int(_tb[0], 16) != 0, "G=8 x M=8 не армлено по стораджу"
print("    арм 8 групп x 8 целей x роут %dБ занял %.1f с" % (ROUTE_TYPICAL, time.time() - t0))

P2_EXPLICIT = miss_gas(c88, LEG.key.hex(), "01")
r = set_hot(c88, 0, 1)
assert logs_of(r, T_HOTSET), "setHot прошёл без события HotSet"
HOT_SET_GAS = r["gasUsed"]
_hot_raw, _hot_view = raw_hot(c88), tuple(call_fn("LiquidatorV6", c88, "hotInfo", []))
assert _hot_raw == _hot_view and _hot_raw[1] == 1, (
    "горячий указатель не записан либо view расходится со стораджем: %s / %s" % (_hot_raw, _hot_view))
P2_HOT1 = miss_gas(c88, LEG.key.hex(), "ff")
r = set_hot(c88, 0, 4)
HOT_RESET_GAS = r["gasUsed"]               # ПЕРЕцеливание: слот уже ненулевой (боевой случай)
P2_HOT4 = miss_gas(c88, LEG.key.hex(), "ff")
_land = int(LEG.address, 16) % 4
print("    промах ЯВНЫМ байтом 0x01            : %6d газа" % P2_EXPLICIT)
print("    промах ГОРЯЧИМ указателем (count=1) : %6d газа (+%d к явному)"
      % (P2_HOT1, P2_HOT1 - P2_EXPLICIT))
print("    промах ГОРЯЧИМ указателем (count=4) : %6d газа (+%d; нога села в группу #%d)"
      % (P2_HOT4, P2_HOT4 - P2_EXPLICIT, _land + 1))
print("    setHot ПЕРВЫЙ (слот пустой)         : %6d газа" % HOT_SET_GAS)
print("    setHot ПЕРЕцеливание (слот занят)   : %6d газа  <= боевой случай: против %d газа за "
      "арм ОДНОЙ группы (8 целей x роут %dБ) = в %.0f раз дешевле"
      % (HOT_RESET_GAS, ARM_GAS[0], ROUTE_TYPICAL, ARM_GAS[0] / HOT_RESET_GAS))

# ЭТАЛОН: тот же замер на ПРОТОТИПЕ, в том же блоке, тем же оракулом и роутом.
cpr = deploy("LiquidatorV6D4")
authorize_one(cpr, LEG.address, "LiquidatorV6D4")
arm_board(cpr, 8, 8, name="LiquidatorV6D4")
P2_PROTO = miss_gas(cpr, LEG.key.hex(), "01")
print("    ЭТАЛОН прототипа (LiquidatorV6Probe:LiquidatorV6D4), тот же блок/оракул/роут: %d газа"
      % P2_PROTO)
print("    (репетиция Г1б-1 на этом же блоке дала %d — расхождение с моим замером %+d)"
      % (PROTO_REF_GAS, P2_PROTO - PROTO_REF_GAS))

# независимость от G и от M — при ЗАФИКСИРОВАННОМ адресуемом оракуле
print("    независимость промаха от G (адресуется всегда группа #1, оракул один и тот же):")
G_ROW = {}
for G in (1, 2, 4):
    a = deploy()
    authorize_one(a, LEG.address)
    arm_board(a, G, 8)
    G_ROW[G] = miss_gas(a, LEG.key.hex(), "01")
G_ROW[8] = P2_EXPLICIT
print("        G=1 %d | G=2 %d | G=4 %d | G=8 %d  => разброс %d газа"
      % (G_ROW[1], G_ROW[2], G_ROW[4], G_ROW[8], max(G_ROW.values()) - min(G_ROW.values())))
print("    независимость промаха от M (одна группа, M целей армлено):")
M_ROW = {}
for M in (1, 2, 4, 8):
    a = deploy()
    authorize_one(a, LEG.address)
    arm_board(a, 1, M)
    M_ROW[M] = miss_gas(a, LEG.key.hex(), "01")
print("        M=1 %d | M=2 %d | M=4 %d | M=8 %d  => разброс %d газа"
      % (M_ROW[1], M_ROW[2], M_ROW[4], M_ROW[8], max(M_ROW.values()) - min(M_ROW.values())))
G_FLAT = max(G_ROW.values()) - min(G_ROW.values())
M_FLAT = max(M_ROW.values()) - min(M_ROW.values())

P2_OK = (P2_EXPLICIT <= LIMIT_EXPLICIT and P2_HOT1 <= LIMIT_HOT and P2_HOT4 <= LIMIT_HOT
         and G_FLAT == 0 and M_FLAT == 0)
verdict("§5.2", "промах явным байтом %d (порог %d), горячим указателем %d/%d (порог %d); "
        "эталон-прототип %d; разброс по G %d, по M %d"
        % (P2_EXPLICIT, LIMIT_EXPLICIT, P2_HOT1, P2_HOT4, LIMIT_HOT, P2_PROTO, G_FLAT, M_FLAT), P2_OK)
revert(sid_p2)

# ============================================================================================
# П3. ОДНОСРЕЛЬНОСТЬ ПО СЫРОМУ СТОРАДЖУ (§5.3)
# ============================================================================================
print()
print("=" * 100)
print("П3 (§5.3). ОДНОСРЕЛЬНОСТЬ: выстрел по цели #3 из 8 снимает РОВНО её (эталон — сырой сторадж)")
print("=" * 100)
sid = snapshot()
c = deploy()
authorize_batch(c, LEGS)
_route = route_for(c)[0]
# цели #1,#2 — личный порог ЗДОРОВ (px > t => пропуск), #3..#8 — порог ОГНЯ.
arm(c, 0, [target(route=_route, trig=(TRIG_HEAL if i < 2 else TRIG_FIRE)) for i in range(8)])
before = [raw_target(c, 0, i) for i in range(8)]
assert all(int(b[0], 16) != 0 for b in before), "не все 8 целей армлены: %s" % [b[0] for b in before]
print("    ЭТАЛОН до выстрела (СЫРОЙ сторадж): 8/8 целей с ненулевым borrower")
# После §2.4 выбор цели зависит от СМЕЩЕНИЯ НОГИ, поэтому индекс не полагается нулевым, а
# ПРЕДСКАЗЫВАЕТСЯ вне контракта (ring_first) и проверяется по сырому стораджу. Две разные ноги:
# смещение 0 (проходит по отфильтрованным #1,#2 и садится на #3 — исходный сценарий редакции 1)
# и смещение 5 (садится на #6) — ротация не должна ломать односрельность ни при каком старте.
P3_CASES, P3_NEXT_GAS, P3_HIT_GAS = {}, 0, 0
for off in (0, 5):
    lg = leg_with(8, off)
    exp = ring_first(off, 8, set(range(2, 8)))
    s2 = snapshot()
    rhit = probe(c, lg.key.hex(), "01")
    after = [raw_target(c, 0, i) for i in range(8)]
    alive = [i for i, t in enumerate(after) if int(t[0], 16) != 0]
    _view_alive = [i for i in range(8)
                   if int(call_fn("LiquidatorV6", c, "targetInfo", [0, i])[0], 16) != 0]
    _hdr = raw_group(c, 0)
    r_next = probe(c, lg.key.hex(), "01")
    ok = (bool(logs_of(rhit, T_LIQUIDATED, c)) and alive == [i for i in range(8) if i != exp]
          and _view_alive == alive and _hdr[2] > 0 and _hdr[3] == 8
          and bool(logs_of(r_next, T_PLANFAILED, c) or logs_of(r_next, T_LIQUIDATED, c)))
    P3_CASES[off] = (ok, exp, alive)
    if off == 0:
        P3_NEXT_GAS, P3_HIT_GAS = r_next["gasUsed"], rhit["gasUsed"]
    print("    нога смещения %d: предсказана цель #%d -> выстрел %s (gasUsed=%d)"
          % (off, exp + 1, note(rhit), rhit["gasUsed"]))
    print("        СЫРОЙ сторадж: живых %d из 8 -> %s; заголовок жив (deadline=%d, n=%d); "
          "view %s; следующая проба: %s (%d газа)"
          % (len(alive), alive, _hdr[2], _hdr[3],
             "сходится" if _view_alive == alive else "РАСХОДИТСЯ (%s)" % _view_alive,
             note(r_next), r_next["gasUsed"]))
    revert(s2)
P3_OK = all(v[0] for v in P3_CASES.values())
verdict("§5.3", "две ноги, два смещения: снята РОВНО предсказанная цель (#%d и #%d, сырой "
        "сторадж), 7 живы, заголовок жив (n=8), следующая проба ИСПОЛНЯЕТ (PlanFailed, %d газа)"
        % (P3_CASES[0][1] + 1, P3_CASES[5][1] + 1, P3_NEXT_GAS), P3_OK)
revert(sid)

# ============================================================================================
# П4. СПАН ГОРЯЧЕГО УКАЗАТЕЛЯ (§5.4)
# ============================================================================================
print()
print("=" * 100)
print("П4 (§5.4). ГОРЯЧИЙ УКАЗАТЕЛЬ: count=4 разводит ноги по 4 группам ДЕТЕРМИНИРОВАННО;")
print("           count=0 / протухший until / g0+count>8 — огня НЕТ (с парным позитивом)")
print("=" * 100)
sid_p4 = snapshot()
c = deploy()
authorize_batch(c, LEGS)
_route = route_for(c)[0]
for g in (2, 3, 4, 5):                     # спан g0=2, count=4 => группы #3..#6
    arm(c, g, [target(route=_route, trig=TRIG_FIRE)])
set_hot(c, 2, 4)
print("    hotInfo() = %s, СЫРОЙ сторадж _hot = %s"
      % (call_fn("LiquidatorV6", c, "hotInfo", []), raw_hot(c)))
spread = {}
for k in range(4):
    lg = next(a for a in LEGS if int(a.address, 16) % 4 == k)
    s2 = snapshot()
    r = probe(c, lg.key.hex(), "ff")
    zeroed = [g for g in (2, 3, 4, 5) if int(raw_target(c, g, 0)[0], 16) == 0]
    ok = bool(logs_of(r, T_LIQUIDATED, c)) and zeroed == [2 + k]
    spread[k] = ok
    print("    нога %s (адрес%%4=%d): предсказана группа #%d -> Liq=%s, погашена цель в группах %s -> %s"
          % (lg.address[:10], k, 2 + k + 1, "ДА" if logs_of(r, T_LIQUIDATED, c) else "нет",
             [g + 1 for g in zeroed], "СОВПАЛО" if ok else "НЕ СОВПАЛО"))
    revert(s2)
P4A_OK = all(spread.values()) and len(spread) == 4

print("    негативы (каждый с ПАРНЫМ позитивом на ТОМ ЖЕ контракте и той же доске):")
NEG = {}
for label, args in (("count=0", (2, 0, None)), ("until протух", (2, 4, 1)),
                    ("g0+count>8", (6, 4, None))):
    s2 = snapshot()
    until = args[2] if args[2] is not None else min(raw_group(c, g)[2] for g in (2, 3, 4, 5))
    # НЕГОДНЫЙ СПАН пишется прямо в сторадж: гард ЧТЕНИЯ обязан держать ЛЮБОЕ содержимое слота,
    # а не только то, что пропустил арм (после ред.3 такой спан через setHot не установить —
    # это проверяется отдельно, в §5.11, на пути АРМА).
    if label == "g0+count>8":
        write_hot_raw(c, args[0], args[1], until)
    else:
        set_hot(c, args[0], args[1], until if label != "count=0" else None)
    rn = probe(c, LEG.key.hex(), "ff")
    still = int(raw_target(c, 2 + (int(LEG.address, 16) % 4), 0)[0], 16) != 0
    set_hot(c, 2, 4)
    rp = probe(c, LEG.key.hex(), "ff")
    fired = bool(logs_of(rp, T_LIQUIDATED, c))
    NEG[label] = (not rn["logs"]) and still and fired
    print("      %-12s: проба 0xFF -> %s (%d газа), цель на месте=%s || ПОЗИТИВ после починки "
          "указателя -> Liq=%s (%d газа) => %s"
          % (label, "МОЛЧАНИЕ" if not rn["logs"] else note(rn), rn["gasUsed"], still,
             "ДА" if fired else "НЕТ", rp["gasUsed"], "ПРОШЁЛ" if NEG[label] else "ПРОВАЛ"))
    revert(s2)

# байты-guard'ы §2.1: 0x00 и 9..0xFE не стреляют, при том что 0x03 на этой же доске стреляет
s2 = snapshot()
byte_ok = {}
for b in ("00", "09", "fe"):
    rb = probe(c, LEG.key.hex(), b)
    byte_ok[b] = (rb["status"] == 1 and not rb["logs"])
rpos = probe(c, LEG.key.hex(), "03")       # явная группа #3 — та же доска, огонь обязан быть
byte_ok["позитив 0x03"] = bool(logs_of(rpos, T_LIQUIDATED, c))
print("      байты-гарды: 0x00 %s | 0x09 %s | 0xFE %s || позитив явным 0x03 -> Liq=%s"
      % (byte_ok["00"], byte_ok["09"], byte_ok["fe"], byte_ok["позитив 0x03"]))
revert(s2)
P4_OK = P4A_OK and all(NEG.values()) and all(byte_ok.values())
verdict("§5.4", "спан count=4: 4/4 ноги сели в предсказанную группу и погасили ТОЛЬКО её цель; "
        "негативы (count=0 / until / g0+count>8) молчат, парные позитивы стреляют; байты 0x00/0x09/0xFE молчат",
        P4_OK)
revert(sid_p4)

# ============================================================================================
# П5. FAIL-OPEN §2.2 (§5.5) — дифференциально против ПРОТОТИПА
# ============================================================================================
print()
print("=" * 100)
print("П5 (§5.5). НЕИЗВЕСТНАЯ ЦЕНА ВЫКЛЮЧАЕТ ФИЛЬТР, А НЕ ЗАПИРАЕТ ОГОНЬ")
print("=" * 100)
sid_p5 = snapshot()

# (б) gate == 0: оракул не вызывается вовсе, у цели ЕСТЬ личный порог. v6 обязан ПОПРОБОВАТЬ
# (и здесь настоящий оракул цел => ликвидация обязана СОСТОЯТЬСЯ). Прототип обязан молчать.
c = deploy()
authorize_batch(c, LEGS)
arm(c, 0, [target(route=route_for(c)[0], trig=TRIG_HEAL)], gate=0)
cp = deploy("LiquidatorV6D4")
authorize_one(cp, LEG.address, "LiquidatorV6D4")
_cd = enc("LiquidatorV6D4", "setGroup",
          [0, MP_T, deadline(), 0, [proto_target(route=route_for(cp)[0], trig=TRIG_HEAL)]])
_r, _ = send_raw({"to": cp, "data": _cd}, gas=29_000_000)
assert _r["status"] == 1
s2 = snapshot()
r_v6 = probe(c, LEG.key.hex(), "01")
revert(s2)
r_pr = probe(cp, LEG.key.hex(), "01")
P5B_V6 = bool(logs_of(r_v6, T_LIQUIDATED, c))
P5B_PR = not r_pr["logs"]
print("    (б) gate=0, у цели личный порог TRIG_HEAL:")
print("        v6       -> %s (gasUsed=%d)" % (note(r_v6), r_v6["gasUsed"]))
print("        ПРОТОТИП -> %s (gasUsed=%d)  <= дефект §2.2: «фильтра нет» = «никогда не стрелять»"
      % (note(r_pr), r_pr["gasUsed"]))
revert(sid_p5)

# (а) gate != 0, ОРАКУЛ РЕВЕРТИТ. Морфо ходит в тот же оракул => успеха быть не может по
# построению, но ПОПЫТКА обязана состояться: PlanFailed вместо тишины.
sid_p5 = snapshot()
c = deploy()
authorize_batch(c, LEGS)
arm(c, 0, [target(route=route_for(c)[0], trig=TRIG_HEAL)], gate=TRIG_FIRE)
cp = deploy("LiquidatorV6D4")
authorize_one(cp, LEG.address, "LiquidatorV6D4")
_cd = enc("LiquidatorV6D4", "setGroup",
          [0, MP_T, deadline(), TRIG_FIRE, [proto_target(route=route_for(cp)[0], trig=TRIG_HEAL)]])
send_raw({"to": cp, "data": _cd}, gas=29_000_000)
s2 = snapshot()
rpc("anvil_setCode", [MP["oracle"], "0x60006000fd"])       # price() ревертит пустотой
try:
    w3.eth.call({"to": MP["oracle"], "data": PX_SEL})
    _rev_ok = False
except Exception:
    _rev_ok = True
print("    (а) gate=%.3e (не ноль), оракул подменён ревертящим кодом: price() ревертит = %s"
      % (TRIG_FIRE, _rev_ok))
r_v6 = probe(c, LEG.key.hex(), "01")
r_pr = probe(cp, LEG.key.hex(), "01")
P5A_V6 = bool(logs_of(r_v6, T_PLANFAILED, c))
P5A_PR = not r_pr["logs"]
print("        v6       -> %s (gasUsed=%d)  <= попытка ДОШЛА до Morpho" % (note(r_v6), r_v6["gasUsed"]))
print("        ЧТО В КВИТАНЦИИ: причина 0x00000000 — это КЛАСС «реверт без данных» (пустой реверт "
      "оракула проброшен Morpho), а не опознание оракула; «Morpho healthy» даёт 0x08c379a0 (П7)")
print("        ПРОТОТИП -> %s (gasUsed=%d)  <= дефект §2.2: тихий no-op" % (note(r_pr), r_pr["gasUsed"]))
revert(s2)                                 # оракул восстановлен, доска та же

# НЕГАТИВНЫЙ КОНТРОЛЬ на том же объекте: цена ИЗВЕСТНА и px > t => пропуск законен.
r_skip = probe(c, LEG.key.hex(), "01")
P5_SKIP = (r_skip["status"] == 1 and not r_skip["logs"])
# ...и ПОЗИТИВНЫЙ на том же объекте: поднимаем личный порог -> та же цель стреляет.
r, _ = send_raw({"to": c, "data": enc("LiquidatorV6", "setTargets",
                                      [0, 0, [target(route=route_for(c)[0], trig=TRIG_FIRE)]])},
                gas=29_000_000)
assert r["status"] == 1
r_fire = probe(c, LEG.key.hex(), "01")
P5_FIRE = bool(logs_of(r_fire, T_LIQUIDATED, c))
print("        оракул восстановлен: pxKnown и px>t -> %s (%d газа)  <= законный пропуск"
      % ("МОЛЧАНИЕ" if P5_SKIP else note(r_skip), r_skip["gasUsed"]))
print("        порог цели поднят (setTargets) -> %s (%d газа)  <= позитив на том же объекте"
      % (note(r_fire), r_fire["gasUsed"]))
P5_OK = P5A_V6 and P5A_PR and P5B_V6 and P5B_PR and P5_SKIP and P5_FIRE
verdict("§5.5", "(а) оракул ревертит: v6 PlanFailed / прототип ТИШИНА; (б) gate=0: v6 ЛИКВИДИРОВАЛ / "
        "прототип ТИШИНА; негатив pxKnown&&px>t — пропуск, позитив после подъёма порога — огонь", P5_OK)
revert(sid_p5)

# ============================================================================================
# П6. К1: seizedAssets задаёт ТОЧНЫЙ вход свопа (§5.6) + старый ABI v5
# ============================================================================================
print()
print("=" * 100)
print("П6 (§5.6). К1: заявка по seizedAssets => SwapInput РАВЕН запечённому в calldata")
print("=" * 100)


def mul_div_up(x, y, d):
    return (x * y + d - 1) // d


def to_shares_up(assets, tba, tbs):
    return mul_div_up(assets, tbs + VIRTUAL_SHARES, tba + VIRTUAL_ASSETS)


def to_assets_up(shares, tba, tbs):
    return mul_div_up(shares, tba + VIRTUAL_ASSETS, tbs + VIRTUAL_SHARES)


sid = snapshot()
SEIZE_ASK = SEIZED // 2                     # половина позиции: частичная ликвидация законна
_quoted = mul_div_up(SEIZE_ASK, PX, 10 ** 36)
_repaid_assets_pre = mul_div_up(_quoted, 10 ** 18, LIF_WAD)
_shares = to_shares_up(_repaid_assets_pre, TBA, TBS)
REPAID_EXP = to_assets_up(_shares, TBA, TBS)
print("    заявка seizedAssets=%d (половина точного сейза %d); ожидаемый repaid ~%.6f USDC"
      % (SEIZE_ASK, SEIZED, REPAID_EXP / 1e6))
c = deploy()
authorize_batch(c, LEGS)
_rt, _ = route_for(c, amount=SEIZE_ASK, min_out=REPAID_EXP)
arm(c, 0, [target(shares=0, seized=SEIZE_ASK, route=_rt, trig=TRIG_FIRE)])
_ti = call_fn("LiquidatorV6", c, "targetInfo", [0, 0])
_rawt = raw_target(c, 0, 0)
print("    targetInfo(0,0) = repaidShares %d, seizedAssets %d | СЫРОЙ сторадж: %d / %d (один слот)"
      % (_ti[1], _ti[2], _rawt[1], _rawt[2]))
r6 = probe(c, LEG.key.hex(), "01")
_si = logs_of(r6, T_SWAPINPUT, c)
SWAP_IN = int(_si[0]["data"][:32].hex(), 16) if _si else -1
_liq = logs_of(r6, T_LIQUIDATED, c)
P6_PROFIT = int(_liq[0]["data"][:32].hex(), 16) / 1e6 if _liq else 0.0
print("    проба 0x01: %s, gasUsed=%d" % (note(r6), r6["gasUsed"]))
print("    SwapInput=%d против запечённого в calldata amount_in=%d => дельта %d"
      % (SWAP_IN, SEIZE_ASK, SWAP_IN - SEIZE_ASK))
print("    Liquidated(profit=%.6f USDC), Morpho.Liquidate=%s"
      % (P6_PROFIT, "ДА" if logs_of(r6, T_MORPHO_LIQ) else "нет"))
P6A_OK = bool(_liq) and SWAP_IN == SEIZE_ASK and _ti[2] == SEIZE_ASK and _rawt[2] == SEIZE_ASK
revert(sid)

# «ровно одно ненулевое — проверяет Morpho, мы пробрасываем» (§2.3): оба поля ненулевые обязаны
# дать ГОВОРЯЩИЙ отказ, а не тихий no-op и не выстрел мимо заявки.
sid = snapshot()
c = deploy()
authorize_batch(c, LEGS)
_rt2, _ = route_for(c, amount=SEIZE_ASK, min_out=REPAID_EXP)
arm(c, 0, [target(shares=B_SHARES, seized=SEIZE_ASK, route=_rt2, trig=TRIG_FIRE)])
rboth = probe(c, LEG.key.hex(), "01")
_pfb = logs_of(rboth, T_PLANFAILED, c)
P6C_OK = bool(_pfb) and not logs_of(rboth, T_LIQUIDATED, c)
print("    оба поля ненулевые (seized И shares): %s, причина %s  <= отказ Morpho проброшен"
      % (note(rboth), ("0x" + _pfb[0]["data"].hex()[:8]) if _pfb else "нет"))
revert(sid)

# старый ABI v5 (7 аргументов) — обёртка обязана работать как раньше
sid = snapshot()
c = deploy()
_rt, _ = route_for(c)
cd = enc("LiquidatorV6", "liquidate", [MP_T, BORROWER, B_SHARES, ROUTER, _rt, 0, ZERO])
r5, _ = send_raw({"to": c, "data": cd}, gas=3_000_000)
_liq5 = logs_of(r5, T_LIQUIDATED, c)
P6B_OK = r5["status"] == 1 and bool(_liq5) and bool(logs_of(r5, T_MORPHO_LIQ))
print("    старый ABI v5 liquidate(mp,borrower,shares,router,route,minProfit,unwrap): status=%d "
      "gasUsed=%d, Liquidated=%s (profit %.2f USDC)"
      % (r5["status"], r5["gasUsed"], "ДА" if _liq5 else "нет",
         (int(_liq5[0]["data"][:32].hex(), 16) / 1e6) if _liq5 else 0.0))
revert(sid)

# §2.3 обещает, что batchLiquidate не ломается — проверяем боем, а не утверждением.
sid = snapshot()
c = deploy()
_rt, _ = route_for(c)
item = (MP_T, BORROWER, int(B_SHARES), ROUTER, _rt, 0, ZERO)
rb, _ = send_raw({"to": c, "data": enc("LiquidatorV6", "batchLiquidate", [[item]])}, gas=3_000_000)
_liqb = logs_of(rb, T_LIQUIDATED, c)
P6D_OK = rb["status"] == 1 and bool(_liqb) and bool(logs_of(rb, T_MORPHO_LIQ))
print("    batchLiquidate([1 элемент], ABI v5): status=%d gasUsed=%d, Liquidated=%s"
      % (rb["status"], rb["gasUsed"], "ДА" if _liqb else "нет"))
revert(sid)
verdict("§5.6", "SwapInput=%d == запечённый amount_in=%d (дельта %d); старый ABI v5 liquidate() "
        "и batchLiquidate ликвидируют; оба поля ненулевые -> PlanFailed (отказ Morpho проброшен)"
        % (SWAP_IN, SEIZE_ASK, SWAP_IN - SEIZE_ASK), P6A_OK and P6B_OK and P6C_OK and P6D_OK)

# ============================================================================================
# П7. ОТКАЗ ГОВОРЯЩИЙ (§5.7)
# ============================================================================================
print()
print("=" * 100)
print("П7 (§5.7). НЕУДАЧА ЦЕЛИ = PlanFailed с 4 байтами причины, а не тишина (#70)")
print("=" * 100)
sid = snapshot()
c = deploy()
authorize_batch(c, LEGS)
_route = route_for(c)[0]
BOGUS = Web3.to_checksum_address("0x" + "de" * 20)     # позиции нет => Morpho отвергнет
arm(c, 0, [target(borrower=BOGUS, route=_route, trig=TRIG_FIRE),
           target(route=_route, trig=TRIG_FIRE)])
# нога со смещением 0 (§2.4: start = addr % n) — та, что упирается именно в отказывающую голову
LEG0 = leg_with(8, 0)
rf = probe(c, LEG0.key.hex(), "01")
_pf = logs_of(rf, T_PLANFAILED, c)
REASON = ("0x" + _pf[0]["data"].hex()[:8]) if _pf else None
print("    цель #1 = несуществующая позиция: %s, gasUsed=%d, причина %s (Error(string)=0x08c379a0)"
      % (note(rf), rf["gasUsed"], REASON))
# ПАРНЫЙ ПОЗИТИВ на том же объекте: гасим отказавшую цель -> следующая ликвидирует по-настоящему
send_raw({"to": c, "data": enc("LiquidatorV6", "clearTarget", [0, 0])})
rok = probe(c, LEG0.key.hex(), "01")
P7_OK = bool(_pf) and REASON not in (None, "0x00000000") and bool(logs_of(rok, T_LIQUIDATED, c))
print("    после clearTarget(0,0): %s, gasUsed=%d  <= парный позитив на том же контракте"
      % (note(rok), rok["gasUsed"]))
verdict("§5.7", "PlanFailed(%s) на неудаче + ликвидация после clearTarget на том же объекте"
        % REASON, P7_OK)
revert(sid)

# ============================================================================================
# П8. АВТОРИЗАЦИЯ (§5.8)
# ============================================================================================
print()
print("=" * 100)
print("П8 (§5.8). Чужак — ТИХИЙ no-op (не revert); setAuthorizedBatch реально ставит флаги")
print("=" * 100)
sid = snapshot()
c = deploy()
_route = route_for(c)[0]
pre = [is_auth(c, a.address) for a in LEGS]
rbatch = authorize_batch(c, LEGS)
post = [is_auth(c, a.address) for a in LEGS]
print("    setAuthorizedBatch(%d ног): газ %d; чтение с цепи ДО %s, ПОСЛЕ %s; чужак=%s"
      % (len(LEGS), rbatch["gasUsed"], set(pre), set(post), is_auth(c, OUTSIDER.address)))
arm(c, 0, [target(route=_route, trig=TRIG_FIRE)])
r_out = probe(c, OUTSIDER.key.hex(), "01")
_still = int(raw_target(c, 0, 0)[0], 16) != 0
print("    проба ЧУЖАКА: status=%d (revert=%s), логов %d, gasUsed=%d, план на месте=%s"
      % (r_out["status"], r_out["status"] == 0, len(r_out["logs"]), r_out["gasUsed"], _still))
r_in = probe(c, LEG.key.hex(), "01")
print("    проба АВТОРИЗОВАННОЙ ноги (тот же контракт, та же доска): %s, gasUsed=%d"
      % (note(r_in), r_in["gasUsed"]))
# >1 байта = ошибка ABI, обязана ГРОМКО ревертить (семантика v5)
r_long = probe(c, LEG.key.hex(), "0102")
print("    проба в 2 байта: status=%d (обязан быть 0 — громкий revert, а не тихий no-op)"
      % r_long["status"])
P8_OK = (r_out["status"] == 1 and not r_out["logs"] and _still and set(post) == {True}
         and set(pre) == {False} and not is_auth(c, OUTSIDER.address)
         and bool(logs_of(r_in, T_LIQUIDATED, c)) and r_long["status"] == 0)
verdict("§5.8", "чужак: status=1, логов 0, план цел; авторизованная нога на той же доске "
        "ликвидирует; setAuthorizedBatch поставил %d/%d флагов (чтение с цепи); 2 байта -> revert"
        % (sum(post), len(LEGS)), P8_OK)
revert(sid)

# ============================================================================================
# П9. РОТАЦИЯ СТАРТА ЦИКЛА ОГНЯ (§5.9, ТЗ ред.2 §2.4)
# ============================================================================================
print()
print("=" * 100)
print("П9 (§5.9). ОТКАЗЫВАЮЩАЯ ГОЛОВА НЕ ЗАПИРАЕТ ГРУППУ: старт цикла = смещение ноги")
print("=" * 100)
print("    КОНФИГУРАЦИЯ БОЯ: сентинел 0xFF, спан count, n=8 целей, флот %d ног." % FLEET_N)
print("    Прежняя редакция пункта гоняла ротацию ЯВНЫМ байтом при n=3 — там, где корреляции")
print("    вычетов НЕТ по построению, и выдала ПРОШЁЛ на свойстве, которого в бою не было.")
sid_p9 = snapshot()
c = deploy()
authorize_fleet(c, FLEET)
# 8 групп ОДНОГО рынка (смысл спана по §2.5б), в каждой 8 целей-КАНАРЕЕК: выбранная кольцом
# клетка называет себя сама 4 байтами PlanFailed. Ни ликвидации, ни свопа — только ВЫБОР.
for g in range(8):
    arm(c, g, [target(route=b"", trig=TRIG_FIRE, unwrap=CANARY[(g, i)]) for i in range(8)])


def sweep(legs, count, g0=0):
    """Проба 0xFF с каждой ноги -> клетка (g,i) с КВИТАНЦИИ. Возвращает ячейки, старты по
    группам, число молчаливых проб. Снимки не нужны: канарейка ничего не потребляет."""
    set_hot(c, g0, count)
    cells, per_g, silent, model_ok = {}, {}, 0, 0
    for lg in legs:
        r = probe(c, lg.key.hex(), "ff", gas=400_000)
        pf = logs_of(r, T_PLANFAILED, c)
        cell = SEL2CELL.get(("0x" + pf[0]["data"].hex()[:8]) if pf else "")
        if cell is None:
            silent += 1
            continue
        cells[cell] = cells.get(cell, 0) + 1
        per_g.setdefault(cell[0], set()).add(cell[1])
        if cell == (g0 + int(lg.address, 16) % count, start_model(lg.address, 8)):
            model_ok += 1
    MODEL[(count, len(legs))] = (model_ok, len(legs) - silent)
    return cells, per_g, silent


MODEL = {}
# --- (а) БОЕВАЯ КОНФИГУРАЦИЯ count=4 --------------------------------------------------------
LEGS_A = FLEET[:128]
cells4, perg4, sil4 = sweep(LEGS_A, 4)
starts4 = {g + 1: len(s) for g, s in sorted(perg4.items())}
print("    (а) count=4, n=8, %d ног: РАЗНЫХ СТАРТОВ НА ГРУППУ %s (порог ТЗ: >=5 в КАЖДОЙ), "
      "молчаливых проб %d" % (len(LEGS_A), starts4, sil4))
P9A_OK = len(starts4) == 4 and all(v >= 5 for v in starts4.values())

# --- (б) count=8: покрытие целей в группе ----------------------------------------------------
cells8, perg8, sil8 = sweep(FLEET, 8)
starts8 = {g + 1: len(s) for g, s in sorted(perg8.items())}
COVER8 = len(cells8)
print("    (б) count=8, n=8, %d ног: покрытие целей ПО ГРУППАМ %s; всего клеток %d из 64"
      % (FLEET_N, starts8, COVER8))
_c40 = sweep(FLEET[:40], 8)[0]
print("        сверка с замером линзы на 40 ногах: клеток %d из 64 (у ред.2 было 8 из 64 — "
      "ровно одна цель на группу)" % len(_c40))
P9B_OK = len(starts8) == 8 and all(v >= 5 for v in starts8.values()) and COVER8 >= 48
_mk, _mn = MODEL[(8, FLEET_N)]
print("        сверка МОДЕЛИ с ЗАМЕРОМ: клетка (addr%%count, keccak(addr)%%n) совпала с квитанцией "
      "в %d пробах из %d" % (_mk, _mn))
P9B_OK = P9B_OK and _mk == _mn

# --- (в) ТРУП в цели №1 не запирает группу — ЗДЕСЬ ЛИКВИДАЦИИ НАСТОЯЩИЕ ----------------------
sid_c = snapshot()
cc = deploy()
authorize_fleet(cc, FLEET[:32])
_route = route_for(cc)[0]
arm(cc, 0, [target(borrower=BOGUS, route=_route, trig=TRIG_FIRE)]
    + [target(route=_route, trig=TRIG_FIRE) for _ in range(7)])
set_hot(cc, 0, 1)
fired, corpse, taken = 0, 0, set()
for lg in FLEET[:32]:
    s = snapshot()
    r = probe(cc, lg.key.hex(), "ff")
    if logs_of(r, T_LIQUIDATED, cc):
        fired += 1
        taken.add([i for i in range(8) if raw_borrower(cc, 0, i) == 0][0])
    elif logs_of(r, T_PLANFAILED, cc):
        corpse += 1
    revert(s)
print("    (в) НАСТОЯЩИЕ ликвидации: труп (цель уже взял конкурент) в ячейке #1, count=1, 32 ноги:"
      " ликвидировали %d, утонули в трупе %d (~1/n), взятые цели %s — БЕЗ clearTarget"
      % (fired, corpse, sorted(i + 1 for i in taken)))
P9C_OK = fired > 0 and len(taken) >= 5 and 0 not in taken
revert(sid_c)

# --- (г) КОНТРОЛЬ НЕИЗМЕННОСТИ ЦЕНЫ ПРОМАХА --------------------------------------------------
MISS_NOW = (P2_EXPLICIT, P2_HOT1, P2_HOT4)
P9_MISS_OK = MISS_NOW == MISS_REF
print("    (г) цена промаха: явный %d/%d, hot(1) %d/%d, hot(4) %d/%d (замер/эталон ред.3) => %s"
      % (P2_EXPLICIT, MISS_REF[0], P2_HOT1, MISS_REF[1], P2_HOT4, MISS_REF[2],
         "СОШЛАСЬ" if P9_MISS_OK else "РАЗОШЛАСЬ"))
print("        ротация стоит 0 газа на промахе (цикл не исполняется); эталон ред.3 = ред.1 + %d "
      "газа — это §2.5в (граница TTL `now >= deadline`), дельта рантайма ровно 1 байт ISZERO"
      % MISS_TTL_COST)
verdict("§5.9", "боевая конфигурация 0xFF: (а) count=4 разных стартов на группу %s (>=5); "
        "(б) count=8 покрытие %s, клеток %d/64 (40 ног: %d/64); (в) настоящие ликвидации: труп "
        "съел %d проб из 32, живые цели %s взяты без clearTarget; (г) промах %d/%d/%d = эталон"
        % (list(starts4.values()), list(starts8.values()), COVER8, len(_c40), corpse,
           sorted(i + 1 for i in taken), P2_EXPLICIT, P2_HOT1, P2_HOT4),
        P9A_OK and P9B_OK and P9C_OK and P9_MISS_OK)
revert(sid_p9)

# ============================================================================================
# П10. ВОСКРЕШЕНИЕ ЦЕЛЕЙ ПРОШЛОГО АРМА (§5.10, §2.5а)
# ============================================================================================
print()
print("=" * 100)
print("П10 (§5.10). setGroup(8) -> setGroup(2) -> setTargets(from=4): цели ПРОШЛОГО плана")
print("=" * 100)
sid_p10 = snapshot()
c = deploy()
authorize_fleet(c, FLEET[:64])
# ПЛАН A: 8 целей с ПРОТУХШИМ роутом (получатель свопа — чужой адрес): попадание в такую цель
# даёт CannotRepay, что отличимо от Error(string) плана B. «Куда ушла нога» читается по 4
# байтам причины, а не по догадке.
_stale = route_for(Web3.to_checksum_address("0x" + "11" * 20))[0]
_good = route_for(c)[0]
arm(c, 0, [target(route=_stale, trig=TRIG_FIRE) for _ in range(8)])
arm(c, 0, [target(borrower=BOGUS, route=_good, trig=TRIG_FIRE) for _ in range(2)])
_n_before = raw_group(c, 0)[3]
cd_gap = enc("LiquidatorV6", "setTargets",
             [0, 4, [target(borrower=BOGUS, route=_good, trig=TRIG_FIRE)]])
r_gap, _ = send_raw({"to": c, "data": cd_gap}, gas=29_000_000)
_n_after = raw_group(c, 0)[3]
print("    n после перезарядки двумя целями: %d; докладка С ДЫРОЙ (from=4): status=%d, n стал %d"
      % (_n_before, r_gap["status"], _n_after))
if r_gap["status"] == 1:
    ghosts = [i for i in range(_n_after) if raw_borrower(c, 0, i) == int(BORROWER, 16)]
    hit_ghost = 0
    for lg in FLEET[:24]:
        s = snapshot()
        rp = probe(c, lg.key.hex(), "01")
        _pf = logs_of(rp, T_PLANFAILED, c)
        if _pf and ("0x" + _pf[0]["data"].hex()[:8]) == SEL_CANNOT_REPAY:
            hit_ghost += 1
        revert(s)
    print("    ЯЧЕЙКИ-ПРИЗРАКИ прошлого плана (сырой сторадж): %s — targetInfo показывает их как "
          "обычные цели, событий об этом нет" % [i + 1 for i in ghosts])
    print("    ЗАМЕР ФЛОТА: %d ног из 24 ушли в цель ПРОШЛОГО плана (причина %s = протухший роут)"
          % (hit_ghost, SEL_CANNOT_REPAY))
    P10_OK = False
    P10_TXT = ("докладка с дырой ПРИНЯТА (n %d->%d), призраки %s, %d ног из 24 ушли в протухший роут"
               % (_n_before, _n_after, [i + 1 for i in ghosts], hit_ghost))
else:
    cd_ok = enc("LiquidatorV6", "setTargets",
                [0, _n_before, [target(route=_good, trig=TRIG_FIRE)]])
    r_ok, _ = send_raw({"to": c, "data": cd_ok}, gas=29_000_000)
    _n2 = raw_group(c, 0)[3]
    hit = 0
    for lg in FLEET[:24]:
        s = snapshot()
        rp = probe(c, lg.key.hex(), "01")
        if logs_of(rp, T_LIQUIDATED, c):
            hit += 1
        revert(s)
    print("    докладка С ДЫРОЙ ОТВЕРГНУТА; непрерывная докладка (from=n=%d): status=%d, n=%d"
          % (_n_before, r_ok["status"], _n2))
    print("    ПАРНЫЙ ПОЗИТИВ: дописанная цель ДОСТИЖИМА — %d ног из 24 её ликвидировали" % hit)
    P10_OK = r_ok["status"] == 1 and _n2 == _n_before + 1 and hit > 0
    P10_TXT = ("докладка с дырой отвергнута (tx откат); непрерывная докладка села (n %d->%d) и "
               "цель достижима: %d ног из 24 ликвидировали" % (_n_before, _n2, hit))
verdict("§5.10", P10_TXT, P10_OK)
revert(sid_p10)

# ============================================================================================
# П11. ОТПЕЧАТОК ГОРЯЧЕГО УКАЗАТЕЛЯ (§5.11, §2.5б)
# ============================================================================================
print()
print("=" * 100)
print("П11 (§5.11). Указатель обязан нести признак ЦЕЛИ, а не голый индекс группы")
print("=" * 100)
sid_p11 = snapshot()
c = deploy()
authorize_fleet(c, FLEET[:64])
_good = route_for(c)[0]
arm(c, 0, [target(route=_good, trig=TRIG_FIRE)])                     # наш рынок, живая жертва
_fmp = (FOREIGN[0], FOREIGN[1], FOREIGN[2], FOREIGN[3], FOREIGN[4])  # ЧУЖОЙ рынок
_cdf = enc("LiquidatorV6", "setGroup", [1, _fmp, deadline(), 0,
                                        [target(borrower=BOGUS, route=_good, trig=0)]])
r, _ = send_raw({"to": c, "data": _cdf}, gas=29_000_000)
assert r["status"] == 1, "чужая группа не армлена"
arm(c, 2, [target(route=_good, trig=TRIG_FIRE)], dl=deadline() - 600)   # КОРОТКИЙ дедлайн
if HOT_FP:
    r1 = set_hot(c, 0, 1, mph=FOREIGN_MPH, expect=None)
    r2 = set_hot(c, 1, 1, expect=None)
    r3 = set_hot(c, 2, 1, until=raw_group(c, 0)[2], expect=None)   # until > dl группы
    r4 = set_hot(c, 0, 2, expect=None)
    r5 = set_hot(c, 6, 4, expect=None)
    r6 = set_hot(c, 0, 1, expect=None)
    rp = probe(c, FLEET[0].key.hex(), "ff")
    print("    чужой отпечаток в подписи        : status=%d" % r1["status"])
    print("    группа ЧУЖОГО рынка в спане      : status=%d" % r2["status"])
    print("    группа с deadline < until        : status=%d" % r3["status"])
    print("    спан «наш + чужой рынок»         : status=%d" % r4["status"])
    print("    спан за границу доски (6+4)      : status=%d" % r5["status"])
    print("    ВЕРНЫЙ указатель (парный позитив): status=%d, проба 0xFF -> %s"
          % (r6["status"], note(rp)))
    P11_OK = (r1["status"] == 0 and r2["status"] == 0 and r3["status"] == 0 and r4["status"] == 0
              and r5["status"] == 0 and r6["status"] == 1 and bool(logs_of(rp, T_LIQUIDATED, c)))
    P11_TXT = ("чужой отпечаток / чужая группа / deadline<until / смешанный спан / спан за 8 — "
               "все ОТКАТ; верный указатель сел и ликвидировал")
else:
    set_hot(c, 1, 1)
    away = 0
    for lg in FLEET[:24]:
        s = snapshot()
        rr = probe(c, lg.key.hex(), "ff")
        if not rr["logs"]:
            away += 1
        revert(s)
    set_hot(c, 0, 4)
    dead = 0
    for lg in FLEET[:24]:
        s = snapshot()
        rr = probe(c, lg.key.hex(), "ff")
        if not rr["logs"]:
            dead += 1
        revert(s)
    print("    указатель на группу ЧУЖОГО рынка ПРИНЯТ (status=1): %d ног из 24 ушли туда молча"
          % away)
    print("    спан 4 групп, где часть протухла/пуста, ПРИНЯТ: %d проб из 40 вернулись МОЛЧА — "
          "неотличимо от здорового промаха" % dead)
    P11_OK = False
    P11_TXT = ("отпечатка в setHot нет: указатель на чужой рынок принят (%d/24 ног уведены молча), "
               "спан с протухшими группами принят (%d/24 проб молча погашены)" % (away, dead))
verdict("§5.11", P11_TXT, P11_OK)
revert(sid_p11)

# ============================================================================================
# ДОП. (вопрос сессии): сужение до uint128 обязано падать ГРОМКО, а не обрезать заявку
# ============================================================================================
print()
print("=" * 100)
print("ДОП. uint128: переполнение на АРМЕ падает громко (молчаливое обрезание = класс #121)")
print("=" * 100)
sid = snapshot()
c = deploy()
MARK = 0xDEAD0000DEAD
cd_ok = enc("LiquidatorV6", "setGroup",
            [0, MP_T, deadline(), TRIG_FIRE, [target(shares=MARK, route=b"")]])
_w = "%064x" % MARK
assert cd_ok.count(_w) == 1, "маркер заявки не найден в калдате арма ровно один раз"
cd_bad = cd_ok.replace(_w, "%064x" % (2 ** 128))          # ровно на единицу выше uint128
r_bad, _ = send_raw({"to": c, "data": cd_bad}, gas=29_000_000)
cd_max = cd_ok.replace(_w, "%064x" % (2 ** 128 - 1))      # предельно допустимая заявка
r_max, _ = send_raw({"to": c, "data": cd_max}, gas=29_000_000)
_back = call_fn("LiquidatorV6", c, "targetInfo", [0, 0])[1]
r_ok, _ = send_raw({"to": c, "data": cd_ok}, gas=29_000_000)   # парный позитив: та же калдата
print("    repaidShares = 2^128     : status=%d (%s)" % (r_bad["status"],
      "ОТКАТ — декодер calldata отверг" if r_bad["status"] == 0 else "СЕЛ — МОЛЧАЛИВОЕ ОБРЕЗАНИЕ"))
print("    repaidShares = 2^128 - 1 : status=%d, targetInfo вернул %d (== 2^128-1: %s)"
      % (r_max["status"], _back, _back == 2 ** 128 - 1))
print("    та же калдата с законным значением (парный позитив): status=%d" % r_ok["status"])
U128_OK = (r_bad["status"] == 0 and r_max["status"] == 1 and _back == 2 ** 128 - 1
           and r_ok["status"] == 1)
verdict("uint128", "2^128 -> откат tx (декодер), 2^128-1 -> сел и прочитан без обрезания, "
        "законная калдата -> сел", U128_OK)
revert(sid)

# ============================================================================================
print()
print("=" * 100)
print("ТАБЛИЦА ПРИЁМКИ §5 (форк-блок %d, газ — с квитанций)" % FORK_BLOCK)
print("=" * 100)
for p, what, ok in RESULTS:
    print("%-7s %-8s %s" % (p, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
print()
print("ЧИСЛА ПРОМАХА (главное): явный байт %d | горячий указатель count=1 %d / count=4 %d | "
      "эталон-прототип %d (репетиция: %d)" % (P2_EXPLICIT, P2_HOT1, P2_HOT4, P2_PROTO, PROTO_REF_GAS))
print("огонь: позитивный контроль %d газа (профит %.2f USDC)" % (P1_GAS, P1_PROFIT))
print("арм ОДНОЙ группы (8 целей x роут %dБ) %d газа | setHot первый %d | setHot ПЕРЕцеливание %d "
      "(в %.0f раз дешевле арма)"
      % (ROUTE_TYPICAL, ARM_GAS[0], HOT_SET_GAS, HOT_RESET_GAS, ARM_GAS[0] / HOT_RESET_GAS))
print("=" * 100)
if FAILS:
    print("ПРИЁМКА НЕ СДАНА: %s" % ", ".join(sorted(set(FAILS))))
    sys.exit(1)
print("ПРИЁМКА СДАНА: %d/%d пунктов" % (len(RESULTS), len(RESULTS)))
