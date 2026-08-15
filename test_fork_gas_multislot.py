"""ФОРК-РЕПЕТИЦИЯ ГАЗА Г1б: СКОЛЬКО СТОИТ МУЛЬТИСЛОТ — оба конца, без швов.

Продукт файла — ЧИСЛА, а не построенный v6. Ничего в бой не идёт.

Зачем. v5 держит РОВНО ОДИН armed-план, а в блоке каскада бывает 25 ликвидаций в ОДНОМ рынке:
один слот берёт максимум одну цель. Прежде чем проектировать M слотов, нужна цена — и мерить
надо ОБА конца, потому что связывающий предел почти наверняка НЕ там, где кажется:
  * цена АРМА (владелец): setPlan(s), редко — раз в TTL 900с на рынок;
  * цена ПРОМАХА пробы (флот): умножается на ЗАМЕРЕННЫЕ 60-75k проб/сутки ($45-55/день при
    дневном газ-капе $100). Если промах станет линейным по M — мультислот убьёт экономику
    раньше, чем что-либо построит.

Стенд — брат test_fork_board_arm_lands.py: та же цепь (форк Base 49666337 + переигранный
НАСТОЯЩИЙ транзит оракула из 49666338), та же жертва, те же приёмы (send_raw / show_receipt /
вердикт с КВИТАНЦИИ, не с хэша). Швов нет: v6-прототипы наследуют денежный путь v5 БИТ-В-БИТ,
пробы уходят настоящими tx с настоящей авторизованной ноги, попадание реально ликвидирует.

КАНОН: ПОЗИТИВНЫЙ КОНТРОЛЬ ИДЁТ ПЕРВЫМ. Стенд A открывается замером НЫНЕШНЕГО v5 (иначе
мультислотовые числа не с чем сравнивать), стенд B — пробой v5, и у КАЖДОГО дизайна попадание
обязано реально ликвидировать: дешёвый промах, у которого попадание не работает, — это не
дешевизна, а неисполняемый код.

ПОЧЕМУ БЛОК СТАРЫЙ И ЧЕМ ЭТО ЗАКРЫТО. Позитивный контроль огня требует НАСТОЯЩЕЙ захватываемой
жертвы, а она есть только в блоке каскада 49666337/38 — на свежей голове ликвидировать некого,
и «попадание работает» доказать нечем. Риск известен (не-архивный апстрим отдаёт тихие пустые
чтения) и закрыт ЗАМЕРОМ, а не надеждой: стенд ПЕРВЫМ делом читает позицию жертвы и требует,
чтобы долг сошёлся с призом конкурента ($2 510.19) до сотых. Пустые чтения дают ноль и стенд
падает. Сверх того gas-числа перепроверяются на СВЕЖЕЙ голове режимом G1B_GASONLY=1 (см. ниже):
цена опкодов от высоты блока не зависит, и совпадение двух высот это доказывает, а не постулирует.

ЗАПУСК (нужен anvil + venv с web3):
  1) ОСНОВНОЙ прогон (нужен блок каскада — там живёт жертва для позитивного контроля огня):
     anvil --fork-url https://mainnet.base.org --fork-block-number 49666337 --port 8551 --silent &
     FORK_RPC=http://127.0.0.1:8551 python3 test_fork_gas_multislot.py
  2) СВЕРКА НА СВЕЖЕЙ ГОЛОВЕ (только газ: ни транзита, ни жертвы, ни огня):
     anvil --fork-url https://mainnet.base.org --port 8551 --silent &
     FORK_RPC=http://127.0.0.1:8551 G1B_GASONLY=1 python3 test_fork_gas_multislot.py
Без FORK_RPC печатает SKIP и выходит с нулём.
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
from eth_abi import decode as abi_decode                   # noqa: E402
from web3 import Web3                                      # noqa: E402

import chain.execute as ex                                 # noqa: E402
from chain.hotpath import _seized_exact                    # noqa: E402
from strategy.pnl import lif_from_lltv                     # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MID = "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109"
# Жертва D («снайпер», приз $2 510): БЕРЁТСЯ прямым роутом — на ней и стоит позитивный контроль
# огня. Кит C ($16 494) прямым роутом не берётся по глубине пула (см. test_fork_win_rehearsal).
BORROWER = Web3.to_checksum_address("0xfda213169284e80af6a9cbfd06431f50d6d8bf74")
PRIZE_USDC = 2510.19
TRANSIT = "0xfb3460bed87acd44c350675d5b80c194f76ebead150c372246bf3cb31d92f63d"
ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
GPO = "0x420000000000000000000000000000000000000F"          # OP-stack GasPriceOracle (L1 data fee)

# Боевой размер роута Kyber: «2KB роут» — STATE.md:2881 (таблица газа огня) и docs/ROLLBACKS.md:68
# («18.6k газа у отфильтрованной пробы против 213.6k у полной попытки с 2KB роутом»).
ROUTE_TYPICAL = 2048
PROBES_PER_DAY = (60_000, 75_000)                          # ЗАМЕР флота, STATE.md:439
COMBAT_USD_PER_DAY = (45.0, 55.0)                          # тот же замер
DAILY_CAP_USD = 100.0                                      # #53/#93
TTL_SEC = 900                                              # SPEC_FIRE_V2_PLAN_TTL_SEC
# КАДАНС АРМОВ — ЗАМЕРЕННЫЙ С БОЕВОЙ МАШИНЫ, а не выведенный из TTL (журнал «SPECFIRE доска
# armed», 7 суток: 37/68/68/91/88 армов В СУТКИ НА ВЕСЬ ФЛОТ, медиана ~68, пик 91). Армы живут
# в окнах волатильности и НЕ перекладываются непрерывно. Вердикт по капу выносится по ЭТОЙ строке.
ARMS_PER_DAY_MEASURED = (70, 90)
GASONLY = bool(os.environ.get("G1B_GASONLY"))              # сверка на свежей голове: только газ

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 60}))
assert w3.is_connected(), RPC_URL
owner = Account.from_key(ANVIL0)
S = requests.Session()
T_PLANSET = Web3.keccak(text="PlanSet(uint64)")
T_LIQUIDATED = Web3.keccak(text="Liquidated(address,address,uint256)")
T_PLANFAILED = Web3.keccak(text="PlanFailed(bytes4)")
T_PLANEXEC = Web3.keccak(text="PlanExecuted(address,uint256)")
T_MORPHO_LIQ = Web3.keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")


def rpc(method, params, url=RPC_URL):
    return S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                  timeout=90).json()


def send_raw(tx, key=ANVIL0, gas=6_000_000):
    """Служебная отправка стенда (деплой, setAuthorized, арм). Возвращает (квитанция, сырые байты)."""
    acct = Account.from_key(key)
    tx.setdefault("chainId", 8453)
    tx.setdefault("nonce", w3.eth.get_transaction_count(acct.address))
    tx.setdefault("maxFeePerGas", int(2e9))
    tx.setdefault("maxPriorityFeePerGas", int(1e6))
    tx.setdefault("gas", gas)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    return r, signed.raw_transaction


ART = {}
for _n in ("Liquidator.sol/Liquidator", "LiquidatorV6Probe.sol/LiquidatorV6D1",
           "LiquidatorV6Probe.sol/LiquidatorV6D2", "LiquidatorV6Probe.sol/LiquidatorV6D3",
           "LiquidatorV6Probe.sol/LiquidatorV6D4"):
    ART[_n.split("/")[-1]] = json.load(open("contracts/out/%s.json" % _n))

CDEPLOY = {}


def deploy(name):
    a = ART[name]
    rec, _ = send_raw({"data": a["bytecode"]["object"] + ("0" * 24 + MORPHO[2:]).lower(),
                       "to": None}, gas=6_000_000)
    assert rec["status"] == 1, "деплой %s не сел" % name
    addr = Web3.to_checksum_address(rec["contractAddress"])
    CDEPLOY[addr] = name
    return addr


def cabi(name, addr=None):
    return w3.eth.contract(address=addr, abi=ART[name]["abi"])


def enc(name, fn, args):
    return cabi(name).encode_abi(fn, args=args)


def authorize(addr, who):
    rec, _ = send_raw({"to": addr, "data": "0x" + Web3.keccak(text="setAuthorized(address,bool)")[:4].hex()
                       + "0" * 24 + who[2:].lower() + "0" * 63 + "1"})
    assert rec["status"] == 1
    got = w3.eth.call({"to": addr, "data": "0x" + Web3.keccak(text="isAuthorized(address)")[:4].hex()
                       + "0" * 24 + who[2:].lower()})
    assert int(got.hex(), 16) == 1, "нога не авторизована на %s" % addr


def probe(addr, key, byte="01", gas=3_000_000):
    """НАСТОЯЩАЯ проба: отдельная tx с авторизованной ноги, <=1 байт калдаты. Вердикт с квитанции."""
    acct = Account.from_key(key)
    tx = {"to": addr, "data": "0x" + byte, "chainId": 8453, "gas": gas,
          "nonce": w3.eth.get_transaction_count(acct.address),
          "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h, timeout=180), signed.raw_transaction


def logs_of(rec, topic, addr=None):
    return [lg for lg in rec["logs"] if lg["topics"] and lg["topics"][0] == topic
            and (addr is None or lg["address"].lower() == addr.lower())]


def snapshot():
    return rpc("evm_snapshot", [])["result"]


def revert(sid):
    r = rpc("evm_revert", [sid])
    assert r.get("result") is True, r


def storage_at(addr, slot):
    return int(w3.eth.get_storage_at(addr, slot).hex(), 16)


print("=" * 96)
print("ФОРК-РЕПЕТИЦИЯ ГАЗА Г1б (мультислот): цена АРМА и цена ПРОМАХА")
print("=" * 96)
FORK_BLOCK = w3.eth.block_number
print("форк %s: блок %d, chainId %d" % (RPC_URL, FORK_BLOCK, w3.eth.chain_id))

# --- ЖИВЫЕ ЦЕНЫ (НЕ с форка: форк заморожен, а платим мы по СЕГОДНЯШНЕЙ Base) ---------------
_lb = rpc("eth_getBlockByNumber", ["latest", False], LIVE)["result"]
LIVE_BLOCK = int(_lb["number"], 16)
BASEFEE_WEI = int(_lb["baseFeePerGas"], 16)
_eth = None
for _u, _k in ((("https://api.coinbase.com/v2/prices/ETH-USD/spot"), "cb"),
               ("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", "bn")):
    try:
        _j = S.get(_u, timeout=15).json()
        _eth = float(_j["data"]["amount"] if _k == "cb" else _j["price"])
        break
    except Exception:
        continue
assert _eth, "цену ETH не удалось снять — не выдумываем, стенд останавливается"
ETH_USD = _eth
TIP_WEI = int(0.001 * 1e9)          # пол приоритета Base (замер флота, см. память base-subblock)
print("живая Base: блок %d, baseFee %.4f gwei; ETH $%.2f; tip-пол %.3f gwei"
      % (LIVE_BLOCK, BASEFEE_WEI / 1e9, ETH_USD, TIP_WEI / 1e9))


_L1_CACHE = {}


def l1_fee(raw_tx):
    """L1 data fee (OP-stack): считается ЖИВОЙ GasPriceOracle по СЫРЫМ байтам подписанной tx.
    Anvil его не эмулирует вовсе — без этого вызова калдата арма была бы бесплатной по ошибке.

    ПЕРВЫЙ ПРОГОН ЭТО И ПОЙМАЛ НА МНЕ САМОМ: публичный узел отдавал `over rate limit`, функция
    возвращала None, а вызывающий трактовал None как 0 — то есть НЕЗНАНИЕ ЦЕНЫ РАЗРЕШАЛО СЧИТАТЬ
    ЕЁ НУЛЁМ, и ровно у самых больших калдат (M=8) L1-часть молча исчезала. Теперь: переспрос с
    отступом, а если замерить так и не вышло — ГРОМКИЙ отказ, а не тихий ноль."""
    key = bytes(raw_tx)
    if key in _L1_CACHE:
        return _L1_CACHE[key]
    data = ("0x" + Web3.keccak(text="getL1Fee(bytes)")[:4].hex() + ("%064x" % 32)
            + ("%064x" % len(raw_tx)) + key.hex()
            + "00" * ((32 - len(raw_tx) % 32) % 32))
    last = None
    for _a in range(8):
        try:
            r = rpc("eth_call", [{"to": GPO, "data": data}, "latest"], LIVE)
        except Exception as e:
            last, r = repr(e), {}
        if "result" in r:
            _L1_CACHE[key] = int(r["result"], 16)
            return _L1_CACHE[key]
        last = r.get("error", last)
        time.sleep(2.0 + 2.0 * _a)
    raise AssertionError("L1 data fee (%d Б) НЕ ЗАМЕРЕН: %s. Ноль вместо замера подставлять "
                         "нельзя — он занизил бы цену арма." % (len(raw_tx), last))


def usd(gas_used, raw_tx=None):
    """Полная цена одной tx на Base: L2 (gas x (basefee+tip)) + L1 data fee по сырым байтам."""
    l2 = gas_used * (BASEFEE_WEI + TIP_WEI)
    l1 = l1_fee(raw_tx) if raw_tx is not None else 0
    return (l2 + (l1 or 0)) / 1e18 * ETH_USD, l1


# --- 0. НАСТОЯЩИЙ ТРАНЗИТ ОРАКУЛА (жертва становится захватываемой) ------------------------
MP = {"loanToken": Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
      "collateralToken": Web3.to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
      "oracle": Web3.to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE"),
      "irm": Web3.to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
      "lltv": 625000000000000000}
_chain_mp = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(
    text="idToMarketParams(bytes32)")[:4].hex() + MID[2:]}).hex()
for _k in ("loanToken", "collateralToken", "oracle", "irm"):
    assert MP[_k][2:].lower() in _chain_mp.lower(), "%s не подтверждён цепью" % _k
PX_SEL = "0x" + Web3.keccak(text="price()")[:4].hex()

# ПОЗИТИВНЫЙ КОНТРОЛЬ САМОГО ФОРКА (идёт ПЕРВЫМ, до любых выводов): не-архивный апстрим отдал бы
# тихие нули, и весь стенд намерил бы пустоту. Требуем, чтобы позиция жертвы читалась и долг
# сошёлся с призом конкурента ДО СОТЫХ — ноль такую проверку не проходит по построению.
pos = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="position(bytes32,address)")[:4].hex()
                   + MID[2:] + "0" * 24 + BORROWER[2:]})
B_SHARES = int(pos[32:64].hex(), 16)
mkt = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(text="market(bytes32)")[:4].hex() + MID[2:]})
TBA, TBS = int(mkt[64:96].hex(), 16), int(mkt[96:128].hex(), 16)
_code = len(w3.eth.get_code(MORPHO))
if GASONLY:
    PX = int(w3.eth.call({"to": MP["oracle"], "data": PX_SEL}).hex(), 16)
    assert PX > 0 and _code > 1000, "форк не отдаёт состояние (price=%d, code=%d)" % (PX, _code)
    DEBT = max(1, B_SHARES) * (TBA or 1) // (TBS or 1)
    B_SHARES = B_SHARES or 10 ** 15
    print("0/  РЕЖИМ G1B_GASONLY: свежая голова, транзит/жертва/огонь пропущены (мерим только газ).")
    print("    контроль чтения состояния: Morpho code %dБ, oracle.price()=%.6e" % (_code, PX))
else:
    px_before = int(w3.eth.call({"to": MP["oracle"], "data": PX_SEL}).hex(), 16)
    DEBT = B_SHARES * TBA // TBS if TBS else 0
    assert _code > 1000 and B_SHARES > 0 and px_before > 0, (
        "ФОРК ОТДАЁТ ПУСТОТУ (code=%d shares=%d price=%d) — апстрим не архивный, "
        "любые числа отсюда недостоверны" % (_code, B_SHARES, px_before))
    assert abs(DEBT / 1e6 - PRIZE_USDC) < 0.01, (
        "долг жертвы %.6f != приз конкурента %.2f — воспроизведена НЕ ТА гонка ЛИБО состояние "
        "форка не то" % (DEBT / 1e6, PRIZE_USDC))
    print("0/  ПОЗИТИВНЫЙ КОНТРОЛЬ ФОРКА: Morpho code %dБ, позиция жертвы читается, долг %.6f USDC"
          " = приз конкурента (%.2f) => апстрим отдаёт НАСТОЯЩЕЕ состояние блока %d"
          % (_code, DEBT / 1e6, PRIZE_USDC, FORK_BLOCK))
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
    assert raw, "сырую tx транзита не отдали ни одним методом"
    rpc("eth_sendRawTransaction", [raw])
    time.sleep(1.0)
    PX = int(w3.eth.call({"to": MP["oracle"], "data": PX_SEL}).hex(), 16)
    assert PX < px_before, "транзит не снизил цену: %d -> %d" % (px_before, PX)
    print("    транзит переигран: цена оракула %.6e -> %.6e (жертва стала захватываемой)"
          % (px_before, PX))

# --- НАСТОЯЩИЙ РОУТ (прямой пул, как в бою на fast-lane) ------------------------------------
_lif = lif_from_lltv(MP["lltv"] / 10**18)
SEIZED = _seized_exact(B_SHARES, int(round(_lif * 10**18)), PX, TBA, TBS)

def route_for(addr):
    """НАСТОЯЩИЙ роут боевым сборщиком (ex.direct_swap), получатель = сам контракт под замером.
    Длина от адреса не зависит, но подставлять чужого получателя нельзя: тогда попадание в B5
    свопнуло бы в другой кошелёк, и позитивный контроль перестал бы быть контролем."""
    _amt, _min = (SEIZED, DEBT) if not GASONLY else (10 ** 6, 1)
    s = ex.direct_swap(MP["collateralToken"], MP["loanToken"], _amt, addr, addr,
                       repaid_assets=_min)
    assert s, "прямой роут cbXRP->USDC не собрался — позитивный контроль огня не построить"
    return Web3.to_bytes(hexstr=s["calldata"]), Web3.to_checksum_address(s["router"])


_r0, ROUTER = route_for(Web3.to_checksum_address("0x" + "11" * 20))
ROUTE_LEN = len(_r0)
print("    настоящий роут: router=%s, калдата роута %d байт (боевой Kyber-роут = %d Б, STATE.md:2881"
      " — на нём и стоит замер арма A2)" % (ROUTER[:12], ROUTE_LEN, ROUTE_TYPICAL))

TRIG_FIRE = PX * 2                    # огонь: price() <= trigger
TRIG_HEAL = PX * 9 // 10              # здоров: price() > trigger  <- ГОСПОДСТВУЮЩИЙ случай

LEG = Account.create()
send_raw({"to": LEG.address, "value": int(5 * 10**18), "gas": 21000})
print("    нога стенда %s заправлена" % LEG.address[:12])

BASE_SNAP = snapshot()


def plan_tuple(route_len=None, route=None, borrower=BORROWER, shares=B_SHARES,
               trig=TRIG_HEAL, dl=None, mp=None, minprofit=0):
    r = route if route is not None else bytes.fromhex("11" * (route_len or 0))
    m = mp or MP
    return ((m["loanToken"], m["collateralToken"], m["oracle"], m["irm"], int(m["lltv"])),
            borrower, int(shares), ROUTER, r, int(minprofit),
            "0x0000000000000000000000000000000000000000",
            int(dl or (time.time() + TTL_SEC)), int(trig))


def target_tuple(route=None, route_len=None, borrower=BORROWER, shares=B_SHARES, trig=TRIG_HEAL):
    r = route if route is not None else bytes.fromhex("11" * (route_len or 0))
    return (borrower, int(shares), ROUTER, r, 0,
            "0x0000000000000000000000000000000000000000", int(trig))


# ==========================================================================================
# СТЕНД A: ЦЕНА АРМА
# ==========================================================================================
print()
print("=" * 96)
print("СТЕНД A: ЦЕНА АРМА (владелец, раз в TTL=%dс)" % TTL_SEC)
print("=" * 96)

# --- A1. ПОЗИТИВНЫЙ КОНТРОЛЬ: НЫНЕШНИЙ v5 ---------------------------------------------------
print("A1. ПОЗИТИВНЫЙ КОНТРОЛЬ — НЫНЕШНИЙ v5 setPlan, gasUsed С КВИТАНЦИИ (не estimateGas)")
print("    %-9s %-11s %-12s %-12s %-9s" % ("роут,Б", "калдата,Б", "cold gasUsed", "warm gasUsed", "cold/warm"))
A1 = {}
for rn in (0, 200, 400, 800, 1600, 3200):
    liq = deploy("Liquidator")                 # свежий контракт => сторадж ХОЛОДНЫЙ
    cd = ex.encode_set_plan(MP, BORROWER, B_SHARES, ROUTER, "0x" + "11" * rn, 0,
                            int(time.time()) + TTL_SEC, TRIG_HEAL)
    nb = len(cd) // 2 - 1
    r1, raw1 = send_raw({"to": liq, "data": cd}, gas=25_000_000)
    assert r1["status"] == 1 and logs_of(r1, T_PLANSET), "cold setPlan(%dБ) не сел" % rn
    r2, raw2 = send_raw({"to": liq, "data": cd}, gas=25_000_000)   # перезапись ПОВЕРХ живого
    assert r2["status"] == 1 and logs_of(r2, T_PLANSET), "warm setPlan(%dБ) не сел" % rn
    A1[rn] = {"bytes": nb, "cold": r1["gasUsed"], "warm": r2["gasUsed"], "raw": raw1}
    print("    %-9d %-11d %-12d %-12d x%.2f" % (rn, nb, r1["gasUsed"], r2["gasUsed"],
                                                r1["gasUsed"] / r2["gasUsed"]))
_s_cold = (A1[3200]["cold"] - A1[0]["cold"]) / (A1[3200]["bytes"] - A1[0]["bytes"])
_s_warm = (A1[3200]["warm"] - A1[0]["warm"]) / (A1[3200]["bytes"] - A1[0]["bytes"])
print("    наклон: cold %.0f газа/байт калдаты, warm %.0f  (20000/слово=625/байт при нуле->значение)"
      % (_s_cold, _s_warm))

# опорная точка боевого размера роута — она же основа для сравнения с v6
liq0 = deploy("Liquidator")
CD_V5_TYP = ex.encode_set_plan(MP, BORROWER, B_SHARES, ROUTER, "0x" + "11" * ROUTE_TYPICAL, 0,
                               int(time.time()) + TTL_SEC, TRIG_HEAL)
r, raw_v5_typ = send_raw({"to": liq0, "data": CD_V5_TYP}, gas=25_000_000)
V5_ARM_COLD = r["gasUsed"]
r2, _ = send_raw({"to": liq0, "data": CD_V5_TYP}, gas=25_000_000)
V5_ARM_WARM = r2["gasUsed"]
L1_V5_ARM = l1_fee(raw_v5_typ)
print("    боевой размер (роут %dБ): cold %d, warm %d, L1-data-fee %s wei"
      % (ROUTE_TYPICAL, V5_ARM_COLD, V5_ARM_WARM, L1_V5_ARM))

# --- A2. ПРОТОТИПЫ v6 -----------------------------------------------------------------------
print()
print("A2. ПРОТОТИПЫ v6, роут %dБ на слот (боевой размер). gasUsed С КВИТАНЦИИ" % ROUTE_TYPICAL)
MS = (1, 2, 3, 5, 8)
A2 = {"D1/D2": {}, "D3": {}}
print("    D1/D2 (общая раскладка: массив ПОЛНЫХ планов) — setPlans(Plan[M])")
print("    %-4s %-11s %-12s %-12s %-13s %-13s" % ("M", "калдата,Б", "cold gasUsed", "warm gasUsed",
                                                  "cold/слот", "предельн.слот"))
for m in MS:
    a = deploy("LiquidatorV6D1")
    ps = [plan_tuple(route_len=ROUTE_TYPICAL) for _ in range(m)]
    cd = enc("LiquidatorV6D1", "setPlans", [ps])
    r1, raw1 = send_raw({"to": a, "data": cd}, gas=29_000_000)
    assert r1["status"] == 1, "cold setPlans(M=%d) не сел (status=0, gasUsed=%d)" % (m, r1["gasUsed"])
    ps2 = [plan_tuple(route_len=ROUTE_TYPICAL) for _ in range(m)]
    cd2 = enc("LiquidatorV6D1", "setPlans", [ps2])
    r2, _ = send_raw({"to": a, "data": cd2}, gas=29_000_000)
    assert r2["status"] == 1
    A2["D1/D2"][m] = {"bytes": len(cd) // 2 - 1, "cold": r1["gasUsed"], "warm": r2["gasUsed"],
                      "raw": raw1, "addr": a}
    prev = A2["D1/D2"].get(m - 1) or A2["D1/D2"].get(1)
    marg = (r1["gasUsed"] - A2["D1/D2"][1]["cold"]) / (m - 1) if m > 1 else 0
    print("    %-4d %-11d %-12d %-12d %-13.0f %-13s" % (
        m, len(cd) // 2 - 1, r1["gasUsed"], r2["gasUsed"], r1["gasUsed"] / m,
        ("%.0f" % marg) if m > 1 else "-"))
_b1, _b8 = A2["D1/D2"][1], A2["D1/D2"][8]
V6_BASE_COLD = (_b1["cold"] * 8 - _b8["cold"]) / 7          # экстраполяция к M=0
V6_MARG_COLD = (_b8["cold"] - _b1["cold"]) / 7
print("    => БАЗА (M=0, экстраполяция) %.0f + ПРЕДЕЛЬНАЯ ЦЕНА СЛОТА %.0f газа/слот (cold)"
      % (V6_BASE_COLD, V6_MARG_COLD))

print("    D3 (пакет ОДНОГО рынка: общий заголовок + цели) — setMarketPlan(mp,dl,gate,Target[M])")
print("    %-4s %-11s %-12s %-12s %-13s %-13s" % ("M", "калдата,Б", "cold gasUsed", "warm gasUsed",
                                                  "cold/слот", "предельн.слот"))
for m in MS:
    a = deploy("LiquidatorV6D3")
    ts = [target_tuple(route_len=ROUTE_TYPICAL) for _ in range(m)]
    mp_t = (MP["loanToken"], MP["collateralToken"], MP["oracle"], MP["irm"], int(MP["lltv"]))
    cd = enc("LiquidatorV6D3", "setMarketPlan",
             [mp_t, int(time.time() + TTL_SEC), TRIG_HEAL, ts])
    r1, raw1 = send_raw({"to": a, "data": cd}, gas=29_000_000)
    assert r1["status"] == 1, "cold setMarketPlan(M=%d) не сел" % m
    cd2 = enc("LiquidatorV6D3", "setMarketPlan",
              [mp_t, int(time.time() + TTL_SEC), TRIG_HEAL,
               [target_tuple(route_len=ROUTE_TYPICAL) for _ in range(m)]])
    r2, _ = send_raw({"to": a, "data": cd2}, gas=29_000_000)
    assert r2["status"] == 1
    A2["D3"][m] = {"bytes": len(cd) // 2 - 1, "cold": r1["gasUsed"], "warm": r2["gasUsed"],
                   "raw": raw1, "addr": a}
    marg = (r1["gasUsed"] - A2["D3"][1]["cold"]) / (m - 1) if m > 1 else 0
    print("    %-4d %-11d %-12d %-12d %-13.0f %-13s" % (
        m, len(cd) // 2 - 1, r1["gasUsed"], r2["gasUsed"], r1["gasUsed"] / m,
        ("%.0f" % marg) if m > 1 else "-"))
D3_BASE_COLD = (A2["D3"][1]["cold"] * 8 - A2["D3"][8]["cold"]) / 7
D3_MARG_COLD = (A2["D3"][8]["cold"] - A2["D3"][1]["cold"]) / 7
print("    => БАЗА %.0f + ПРЕДЕЛЬНАЯ ЦЕНА СЛОТА %.0f газа/слот (cold)" % (D3_BASE_COLD, D3_MARG_COLD))

# КОНТРОЛЬ: у D2 раскладка та же, что у D1 — цена арма обязана совпасть, а не «по построению»
_a_d2 = deploy("LiquidatorV6D2")
_cd = enc("LiquidatorV6D2", "setPlans", [[plan_tuple(route_len=ROUTE_TYPICAL) for _ in range(8)]])
_r, _ = send_raw({"to": _a_d2, "data": _cd}, gas=29_000_000)
assert _r["status"] == 1
print("    контроль тождества арма D1==D2 при M=8: D1 %d, D2 %d, дельта %d"
      % (A2["D1/D2"][8]["cold"], _r["gasUsed"], _r["gasUsed"] - A2["D1/D2"][8]["cold"]))

# ==========================================================================================
# СТЕНД B: ЦЕНА ПРОБЫ (СВЯЗЫВАЮЩИЙ ПРЕДЕЛ)
# ==========================================================================================
print()
print("=" * 96)
print("СТЕНД B: ЦЕНА ПРОБЫ — отдельная tx с авторизованной ноги, gasUsed С КВИТАНЦИИ")
print("=" * 96)
INTRINSIC = 21000 + 16          # 1 ненулевой байт калдаты
B = {}


def rec_note(rec):
    return "Liq=%s PlanExec=%s PlanFailed=%s логов=%d" % (
        "ДА" if logs_of(rec, T_LIQUIDATED) else "нет",
        "ДА" if logs_of(rec, T_PLANEXEC) else "нет",
        ("0x" + logs_of(rec, T_PLANFAILED)[0]["data"].hex()[:8])
        if logs_of(rec, T_PLANFAILED) else "нет",
        len(rec["logs"]))


# --- B1. ПОЗИТИВНЫЙ КОНТРОЛЬ: v5 как есть ---------------------------------------------------
print("B1. v5 (позитивный контроль): (a) плана нет / (b) армлен и ЗДОРОВ / (c) СРАБАТЫВАЕТ")
v5a = deploy("Liquidator")
authorize(v5a, LEG.address)
r_a, raw_a = probe(v5a, LEG.key.hex())
assert r_a["status"] == 1 and not r_a["logs"]
V5_MISS_EMPTY = r_a["gasUsed"]
print("    (a) пустой сторадж:      gasUsed %6d  (исполнение %5d)  %s"
      % (r_a["gasUsed"], r_a["gasUsed"] - INTRINSIC, rec_note(r_a)))

v5b = deploy("Liquidator")
authorize(v5b, LEG.address)
_cd = ex.encode_set_plan(MP, BORROWER, B_SHARES, ROUTER, "0x" + route_for(v5b)[0].hex(), 0,
                         int(time.time()) + TTL_SEC, TRIG_HEAL)
_r, _ = send_raw({"to": v5b, "data": _cd}, gas=25_000_000)
assert _r["status"] == 1 and logs_of(_r, T_PLANSET)
r_b, raw_b = probe(v5b, LEG.key.hex())
assert r_b["status"] == 1 and not r_b["logs"], "здоровая проба не молчалива: %s" % rec_note(r_b)
V5_MISS_HEALTHY = r_b["gasUsed"]
L1_PROBE = l1_fee(raw_b)
print("    (b) армлен, ЗДОРОВ:      gasUsed %6d  (исполнение %5d)  %s  <= ГОСПОДСТВУЮЩИЙ"
      % (r_b["gasUsed"], r_b["gasUsed"] - INTRINSIC, rec_note(r_b)))
print("        L1-data-fee пробы %s wei (OP-stack; anvil его не эмулирует)" % L1_PROBE)

V5_HIT = 0
if not GASONLY:
    _snap = snapshot()
    v5c = deploy("Liquidator")
    authorize(v5c, LEG.address)
    _cd = ex.encode_set_plan(MP, BORROWER, B_SHARES, ROUTER, "0x" + route_for(v5c)[0].hex(), 0,
                             int(time.time()) + TTL_SEC, TRIG_FIRE)
    send_raw({"to": v5c, "data": _cd}, gas=25_000_000)
    r_c, _ = probe(v5c, LEG.key.hex())
    V5_HIT = r_c["gasUsed"]
    _liq5 = logs_of(r_c, T_LIQUIDATED, v5c)
    print("    (c) СРАБАТЫВАЕТ:         gasUsed %6d  %s" % (r_c["gasUsed"], rec_note(r_c)))
    assert _liq5, "ПОЗИТИВНЫЙ КОНТРОЛЬ ПРОВАЛЕН: v5 не ликвидировал — сравнивать промахи не с чем"
    print("        => ПОЗИТИВНЫЙ КОНТРОЛЬ v5: Liquidated(profit=%.2f USDC), Morpho.Liquidate=%s"
          % (int(_liq5[0]["data"][:32].hex(), 16) / 1e6, "ДА" if logs_of(r_c, T_MORPHO_LIQ) else "нет"))
    revert(_snap)

# --- НАСТОЯЩИЕ ЧУЖИЕ ОРАКУЛЫ для случая «слоты в РАЗНЫХ рынках» ----------------------------
_f2m = json.load(open("feed_to_market.json"))
OTHER_MP = []
_seen = {MP["oracle"].lower()}
for _agg, _v in _f2m.items():
    if len(OTHER_MP) >= 7:
        break
    for _mid in _v.get("markets", []):
        if len(OTHER_MP) >= 7:
            break
        try:
            _raw = w3.eth.call({"to": MORPHO, "data": "0x" + Web3.keccak(
                text="idToMarketParams(bytes32)")[:4].hex() + _mid[2:]})
            lt, ct, orc, irm, lltv = abi_decode(
                ["address", "address", "address", "address", "uint256"], bytes(_raw))
            if orc.lower() in _seen or int(orc, 16) == 0:
                continue
            _p = w3.eth.call({"to": Web3.to_checksum_address(orc), "data": PX_SEL})
            if int(_p.hex(), 16) == 0:
                continue
        except Exception:
            continue
        _seen.add(orc.lower())
        OTHER_MP.append({"loanToken": Web3.to_checksum_address(lt),
                         "collateralToken": Web3.to_checksum_address(ct),
                         "oracle": Web3.to_checksum_address(orc),
                         "irm": Web3.to_checksum_address(irm), "lltv": int(lltv),
                         "px": int(_p.hex(), 16)})
print("    РАЗНЫЕ рынки: найдено %d настоящих Morpho-оракулов Base с живым price() на этом блоке"
      % len(OTHER_MP))


def mp_list(m):
    """M планов: первый — наш рынок, остальные — НАСТОЯЩИЕ чужие рынки (холодные аккаунты)."""
    out = [(MP, TRIG_HEAL)]
    for k in range(m - 1):
        o = OTHER_MP[k % len(OTHER_MP)]
        out.append((o, o["px"] * 9 // 10))
    return out[:m]


# --- B2. D1: байт = индекс слота ------------------------------------------------------------
print()
print("B2. D1 (байт пробы = ИНДЕКС слота). Промах обязан быть O(1) по M")
print("    %-4s %-14s %-14s %-14s" % ("M", "(a) пусто", "(b) здоров", "исполнение(b)"))
B["D1"] = {}
d1_empty = deploy("LiquidatorV6D1")
authorize(d1_empty, LEG.address)
r, _ = probe(d1_empty, LEG.key.hex(), "01")
D1_MISS_EMPTY = r["gasUsed"]
for m in MS:
    a = deploy("LiquidatorV6D1")
    authorize(a, LEG.address)
    cd = enc("LiquidatorV6D1", "setPlans",
             [[plan_tuple(route=route_for(a)[0], mp=mm, trig=tt) for mm, tt in mp_list(m)]])
    _r, _ = send_raw({"to": a, "data": cd}, gas=29_000_000)
    assert _r["status"] == 1
    rb, rawb = probe(a, LEG.key.hex(), "01")           # нога бьёт по СВОЕМУ (армленному) слоту
    assert rb["status"] == 1 and not rb["logs"], "D1 M=%d здоровая проба не молчалива" % m
    B["D1"][m] = {"empty": D1_MISS_EMPTY, "healthy": rb["gasUsed"], "raw": rawb, "addr": a}
    print("    %-4d %-14d %-14d %-14d" % (m, D1_MISS_EMPTY, rb["gasUsed"], rb["gasUsed"] - INTRINSIC))
# контроль: байт, указывающий на ПУСТОЙ слот при M=1
_rq, _ = probe(B["D1"][1]["addr"], LEG.key.hex(), "08")
print("    контроль: байт на ПУСТОЙ слот (0x08 при M=1): %d газа (дешевле — плана в слоте нет)"
      % _rq["gasUsed"])
D1_FLAT = max(v["healthy"] for v in B["D1"].values()) - min(v["healthy"] for v in B["D1"].values())
print("    => разброс промаха D1 по M=1..8: %d газа (%.2f%% от M=1) — %s"
      % (D1_FLAT, 100.0 * D1_FLAT / B["D1"][1]["healthy"],
         "O(1)" if D1_FLAT < 0.05 * B["D1"][1]["healthy"] else "НЕ O(1)"))

# --- B3. D2: перебор всех слотов ------------------------------------------------------------
print()
print("B3. D2 (fallback перебирает ВСЕ M слотов). Линейна ли цена промаха по M?")
print("    %-4s %-16s %-16s %-16s %-16s" % ("M", "(b) 1 рынок", "прирост/слот", "(b) M рынков",
                                            "прирост/слот"))
B["D2"] = {}
d2_empty = deploy("LiquidatorV6D2")
authorize(d2_empty, LEG.address)
r, _ = probe(d2_empty, LEG.key.hex(), "01")
D2_MISS_EMPTY = r["gasUsed"]
for m in MS:
    a1 = deploy("LiquidatorV6D2")
    authorize(a1, LEG.address)
    cd = enc("LiquidatorV6D2", "setPlans",
             [[plan_tuple(route=route_for(a1)[0]) for _ in range(m)]])    # ВСЕ в одном рынке
    _r, _ = send_raw({"to": a1, "data": cd}, gas=29_000_000)
    assert _r["status"] == 1
    rs, raws = probe(a1, LEG.key.hex(), "01")
    assert rs["status"] == 1 and not rs["logs"], "D2 M=%d (1 рынок) проба не молчалива" % m

    a2 = deploy("LiquidatorV6D2")
    authorize(a2, LEG.address)
    cd = enc("LiquidatorV6D2", "setPlans",
             [[plan_tuple(route=route_for(a2)[0], mp=mm, trig=tt) for mm, tt in mp_list(m)]])
    _r, _ = send_raw({"to": a2, "data": cd}, gas=29_000_000)
    assert _r["status"] == 1
    rd, rawd = probe(a2, LEG.key.hex(), "01")
    assert rd["status"] == 1 and not rd["logs"], "D2 M=%d (разные рынки) проба не молчалива" % m

    B["D2"][m] = {"empty": D2_MISS_EMPTY, "healthy": rs["gasUsed"], "healthy_multi": rd["gasUsed"],
                  "raw": raws, "addr": a1}
    g1 = (rs["gasUsed"] - B["D2"][1]["healthy"]) / (m - 1) if m > 1 else 0
    g2 = (rd["gasUsed"] - B["D2"][1]["healthy_multi"]) / (m - 1) if m > 1 else 0
    print("    %-4d %-16d %-16s %-16d %-16s" % (
        m, rs["gasUsed"], ("%.0f" % g1) if m > 1 else "-", rd["gasUsed"],
        ("%.0f" % g2) if m > 1 else "-"))
D2_SLOPE_1 = (B["D2"][8]["healthy"] - B["D2"][1]["healthy"]) / 7
D2_SLOPE_M = (B["D2"][8]["healthy_multi"] - B["D2"][1]["healthy_multi"]) / 7
print("    => наклон промаха D2: %.0f газа/слот (один рынок, оракул ГОРЯЧИЙ после 1-го вызова), "
      "%.0f газа/слот (разные рынки, каждый оракул ХОЛОДНЫЙ)" % (D2_SLOPE_1, D2_SLOPE_M))

# --- B4. D3: пакет одного рынка ------------------------------------------------------------
print()
print("B4. D3 (пакет ОДНОГО рынка: один oracle.price() на пакет). M=1..8 в одном рынке")
print("    %-4s %-14s %-14s %-14s" % ("M", "(a) пусто", "(b) здоров", "исполнение(b)"))
B["D3"] = {}
d3_empty = deploy("LiquidatorV6D3")
authorize(d3_empty, LEG.address)
r, _ = probe(d3_empty, LEG.key.hex(), "01")
D3_MISS_EMPTY = r["gasUsed"]
mp_t = (MP["loanToken"], MP["collateralToken"], MP["oracle"], MP["irm"], int(MP["lltv"]))
for m in (1, 2, 3, 4, 5, 6, 7, 8):
    a = deploy("LiquidatorV6D3")
    authorize(a, LEG.address)
    cd = enc("LiquidatorV6D3", "setMarketPlan",
             [mp_t, int(time.time() + TTL_SEC), TRIG_HEAL,
              [target_tuple(route=route_for(a)[0]) for _ in range(m)]])
    _r, _ = send_raw({"to": a, "data": cd}, gas=29_000_000)
    assert _r["status"] == 1
    rb, rawb = probe(a, LEG.key.hex(), "%02x" % m)
    assert rb["status"] == 1 and not rb["logs"], "D3 M=%d здоровая проба не молчалива" % m
    B["D3"][m] = {"empty": D3_MISS_EMPTY, "healthy": rb["gasUsed"], "raw": rawb, "addr": a}
    print("    %-4d %-14d %-14d %-14d" % (m, D3_MISS_EMPTY, rb["gasUsed"], rb["gasUsed"] - INTRINSIC))
D3_FLAT = max(v["healthy"] for v in B["D3"].values()) - min(v["healthy"] for v in B["D3"].values())
print("    => разброс промаха D3 по M=1..8: %d газа — %s (цели на здоровом пути НЕ читаются)"
      % (D3_FLAT, "O(1)" if D3_FLAT < 0.05 * B["D3"][1]["healthy"] else "НЕ O(1)"))

# --- B4b. D4 = D1 ⊕ D3: байт выбирает ГРУППУ (рынок), внутри группы один oracle на M целей ---
# Зачем отдельный дизайн: D3 как прототип держит РОВНО ОДИН рынок, а флот покрывает 23 фида /
# 40 рынков. Без этого замера рекомендация «берите D3» была бы про стенд, а не про бой.
print()
print("B4b. D4 (байт = ГРУППА/рынок; внутри группы 1 oracle.price() на M целей).")
print("     Промах обязан быть O(1) И по числу групп G, И по числу целей M")
print("     %-4s %-4s %-14s %-14s %-14s" % ("G", "M", "калдата арма,Б", "(b) здоров", "исполнение(b)"))
B["D4"] = {}
d4_empty = deploy("LiquidatorV6D4")
authorize(d4_empty, LEG.address)
r, _ = probe(d4_empty, LEG.key.hex(), "01")
D4_MISS_EMPTY = r["gasUsed"]
D4_ARM = {}
for G in (1, 2, 4, 8):
    a = deploy("LiquidatorV6D4")
    authorize(a, LEG.address)
    _nb = 0
    for g in range(G):
        _mm, _tt = mp_list(min(g + 1, len(OTHER_MP) + 1))[-1]
        _mpt = (_mm["loanToken"], _mm["collateralToken"], _mm["oracle"], _mm["irm"], int(_mm["lltv"]))
        cd = enc("LiquidatorV6D4", "setGroup",
                 [g, _mpt, int(time.time() + TTL_SEC), _tt,
                  [target_tuple(route_len=ROUTE_TYPICAL, trig=_tt) for _ in range(8)]])
        _r, _raw = send_raw({"to": a, "data": cd}, gas=29_000_000)
        assert _r["status"] == 1, "D4 setGroup(G=%d,g=%d) не сел" % (G, g)
        if g == 0:
            D4_ARM[G] = {"gas": _r["gasUsed"], "raw": _raw, "bytes": len(cd) // 2 - 1}
        _nb = len(cd) // 2 - 1
    # ВАЖНО: байт 0x01 всегда => группа #1 => ВСЕГДА ОДИН И ТОТ ЖЕ оракул. Первый прогон бил по
    # ПОСЛЕДНЕЙ группе, и цена «поехала» немонотонно (G=4 дороже G=8) — это была не зависимость
    # от G, а РАЗНАЯ цена разных настоящих оракулов. Стенд подозреваем наравне с предметом:
    # зависимость от G меряется при зафиксированном оракуле, разброс оракулов — отдельной строкой.
    rb, rawb = probe(a, LEG.key.hex(), "01")
    assert rb["status"] == 1 and not rb["logs"], "D4 G=%d здоровая проба не молчалива" % G
    B["D4"][G] = {"empty": D4_MISS_EMPTY, "healthy": rb["gasUsed"], "raw": rawb, "addr": a,
                  "arm1": D4_ARM[G]["gas"]}
    print("     %-4d %-4d %-14d %-14d %-14d" % (G, 8, _nb, rb["gasUsed"], rb["gasUsed"] - INTRINSIC))
    if G == 8:
        _per_oracle = []
        for _g in range(1, 9):
            _rr, _ = probe(a, LEG.key.hex(), "%02x" % _g)
            assert _rr["status"] == 1 and not _rr["logs"]
            _per_oracle.append(_rr["gasUsed"])
        print("     разброс ПО ОРАКУЛАМ при G=8 (байты 0x01..0x08, 8 РАЗНЫХ настоящих рынков): %s"
              % _per_oracle)
        print("     => %d..%d газа: цена промаха зависит от того, ЧЕЙ оракул адресован (+%d газа "
              "у самого дорогого), и НЕ зависит ни от G, ни от M"
              % (min(_per_oracle), max(_per_oracle), max(_per_oracle) - min(_per_oracle)))
D4_FLAT = max(v["healthy"] for v in B["D4"].values()) - min(v["healthy"] for v in B["D4"].values())
print("     => разброс промаха D4 по G=1..8 групп x 8 целей (=64 цели армлено) ПРИ ОДНОМ И ТОМ ЖЕ"
      " адресуемом оракуле: %d газа — %s"
      % (D4_FLAT, "O(1)" if D4_FLAT < 0.05 * B["D4"][1]["healthy"] else "НЕ O(1)"))
print("     арм ОДНОЙ группы (1 рынок x 8 целей, роут %dБ — боевой размер, сравнимо с A2): %d газа"
      % (ROUTE_TYPICAL, D4_ARM[1]["gas"]))

# --- B5. ПОЗИТИВНЫЙ КОНТРОЛЬ КАЖДОГО ДИЗАЙНА: попадание обязано ЛИКВИДИРОВАТЬ ---------------
print()
print("B5. ПОЗИТИВНЫЙ КОНТРОЛЬ ОГНЯ (без него числа B не принимаются)")
HIT = {}


def fire_case(design, arm_fn, byte, note):
    sid = snapshot()
    a = deploy(design)
    authorize(a, LEG.address)
    cd = arm_fn(a)
    _r, _ = send_raw({"to": a, "data": cd}, gas=29_000_000)
    assert _r["status"] == 1, "арм %s не сел" % design
    r, _raw = probe(a, LEG.key.hex(), byte)
    liq = logs_of(r, T_LIQUIDATED, a)
    mliq = logs_of(r, T_MORPHO_LIQ)
    print("    %-22s байт 0x%s: status=%d gasUsed=%d %s"
          % (design + " " + note, byte, r["status"], r["gasUsed"], rec_note(r)))
    assert liq and mliq, ("ПОЗИТИВНЫЙ КОНТРОЛЬ %s ПРОВАЛЕН: попадание не ликвидировало. "
                          "Дешёвый промах без работающего попадания — неисполняемый код." % design)
    prof = int(liq[0]["data"][:32].hex(), 16) / 1e6
    print("        => РЕАЛЬНАЯ ЛИКВИДАЦИЯ: Liquidated(profit=%.2f USDC), Morpho.Liquidate есть" % prof)
    HIT[design + note] = {"gas": r["gasUsed"], "profit": prof, "addr": a, "snap": sid}
    return a, sid, r


def arm_d1(m, fire_idx):
    def _f(addr):
        ps = []
        for i in range(m):
            ps.append(plan_tuple(route=route_for(addr)[0],
                                 trig=TRIG_FIRE if i == fire_idx else TRIG_HEAL))
        return enc("LiquidatorV6D1", "setPlans", [ps])
    return _f


def arm_d2(m, fire_idx):
    def _f(addr):
        ps = []
        for i in range(m):
            ps.append(plan_tuple(route=route_for(addr)[0],
                                 trig=TRIG_FIRE if i == fire_idx else TRIG_HEAL))
        return enc("LiquidatorV6D2", "setPlans", [ps])
    return _f


def arm_d3(m, fire_idx):
    def _f(addr):
        ts = [target_tuple(route=route_for(addr)[0],
                           trig=TRIG_FIRE if i == fire_idx else TRIG_HEAL) for i in range(m)]
        return enc("LiquidatorV6D3", "setMarketPlan",
                   [mp_t, int(time.time() + TTL_SEC), TRIG_FIRE, ts])
    return _f


D1_HIT = D2_HIT = D3_HIT = D4_HIT = 0
if not GASONLY:
    # D1: огонь по слоту с индексом 5 (байт 0x06) — доказывает, что индексная адресация РАБОТАЕТ
    _a, _s, _r = fire_case("LiquidatorV6D1", arm_d1(8, 5), "06", "M=8, слот#6")
    D1_HIT = _r["gasUsed"]
    revert(_s)
    _a, _s, _r = fire_case("LiquidatorV6D2", arm_d2(8, 5), "01", "M=8, огонь слот#6")
    D2_HIT = _r["gasUsed"]
    revert(_s)
    _a, _s, _r = fire_case("LiquidatorV6D3", arm_d3(8, 5), "06", "M=8, цель#6")
    D3_HIT = _r["gasUsed"]
    revert(_s)

    # D4: жертва лежит в ГРУППЕ #3 (байт 0x03), целью #6 внутри группы — проверяется вся
    # двухуровневая адресация сразу, а не только внешний индекс.
    def arm_d4(addr):
        _mpt = (MP["loanToken"], MP["collateralToken"], MP["oracle"], MP["irm"], int(MP["lltv"]))
        ts = [target_tuple(route=route_for(addr)[0], trig=TRIG_FIRE if i == 5 else TRIG_HEAL)
              for i in range(8)]
        return enc("LiquidatorV6D4", "setGroup",
                   [2, _mpt, int(time.time() + TTL_SEC), TRIG_FIRE, ts])

    _a, _s, _r = fire_case("LiquidatorV6D4", arm_d4, "03", "группа#3, цель#6")
    D4_HIT = _r["gasUsed"]
    revert(_s)
else:
    print("    ПРОПУЩЕНО (G1B_GASONLY): на свежей голове ликвидировать некого. Огонь доказан "
          "основным прогоном на блоке каскада.")

# --- B6. ОДНОСРЕЛЬНОСТЬ: победа снимает ТОЛЬКО СВОЙ слот ------------------------------------
print()
print("B6. ОДНОСРЕЛЬНОСТЬ при M слотов: победа обязана снять ТОЛЬКО свой слот")


def slot_info(addr, i):
    c = cabi("LiquidatorV6D1", addr)
    return c.functions.slotInfo(i).call()


def raw_slot(addr, i):
    """ЭТАЛОН: тот же факт из СЫРОГО стораджа, а не из view контракта (самоотчёт не принимаем).
    Раскладка V6Slots: планы с 3-го слота, Plan = 12 слотов; borrower = +5, unwrap|deadline = +10."""
    base = 3 + 12 * i
    b = storage_at(addr, base + 5)
    packed = storage_at(addr, base + 10)
    return "0x%040x" % b, (packed >> 160) & ((1 << 64) - 1)


B6_HIT_GAS = 0
if GASONLY:
    print("    ПРОПУЩЕНО (G1B_GASONLY): выстрел требует живой жертвы — доказано основным прогоном.")
else:
    sid = snapshot()
    a = deploy("LiquidatorV6D1")
    authorize(a, LEG.address)
    cd = enc("LiquidatorV6D1", "setPlans",
             [[plan_tuple(route=route_for(a)[0], trig=TRIG_FIRE) for _ in range(8)]])
    _r, _ = send_raw({"to": a, "data": cd}, gas=29_000_000)
    assert _r["status"] == 1
    before = [raw_slot(a, i) for i in range(8)]
    assert all(dl > 0 for _, dl in before), "не все 8 слотов армлены: %s" % before
    print("    ЭТАЛОН до выстрела (сырой сторадж): 8/8 слотов с ненулевым дедлайном")
    rhit, _ = probe(a, LEG.key.hex(), "03")             # огонь по слоту #3 (индекс 2)
    assert logs_of(rhit, T_LIQUIDATED, a), "выстрел по слоту #3 не ликвидировал"
    after = [raw_slot(a, i) for i in range(8)]
    alive = [i for i, (_, dl) in enumerate(after) if dl > 0]
    print("    M=8, огонь по слоту #3 (байт 0x03): gasUsed=%d, %s" % (rhit["gasUsed"], rec_note(rhit)))
    print("    СЫРОЙ сторадж после выстрела: живых слотов %d из 8 -> %s" % (len(alive), alive))
    print("    вид контракта slotInfo(2)=%s / slotInfo(0)=%s (сверка с сырым стораджем: %s)"
          % (slot_info(a, 2)[1], slot_info(a, 0)[1],
             "сходится" if (slot_info(a, 2)[1] == after[2][1] and slot_info(a, 0)[1] == after[0][1])
             else "РАСХОДИТСЯ"))
    assert after[2][1] == 0, "свой слот НЕ снят — one-shot не сработал"
    assert len(alive) == 7 and 2 not in alive, "выстрел разоружил чужие слоты: живы %s" % alive
    print("    => ОДНОСРЕЛЬНОСТЬ ДОКАЗАНА: снят только слот #3, остальные 7 живы (эталон = сырой "
          "сторадж, не view контракта)")
    # и они РАБОТОСПОСОБНЫ: следующая проба по слоту #4 доходит до исполнения (жертва уже взята ->
    # осмысленный отказ, а не тихий no-op по пустому стораджу)
    r2, _ = probe(a, LEG.key.hex(), "04")
    print("    контроль работоспособности оставшихся: проба по слоту #4 -> gasUsed=%d %s"
          % (r2["gasUsed"], rec_note(r2)))
    assert logs_of(r2, T_PLANFAILED) or logs_of(r2, T_LIQUIDATED, a), \
        "оставшийся слот НЕ исполняется — «живой» он только по дедлайну"
    B6_HIT_GAS = rhit["gasUsed"]
    revert(sid)

# ==========================================================================================
# СТЕНД C: АРИФМЕТИКА ПРЕДЕЛА
# ==========================================================================================
print()
print("=" * 96)
print("СТЕНД C: АРИФМЕТИКА СВЯЗЫВАЮЩЕГО ПРЕДЕЛА")
print("=" * 96)
print("исходные: форк-блок %d; живая Base блок %d, baseFee %.4f gwei, tip-пол %.3f gwei, ETH $%.2f"
      % (FORK_BLOCK, LIVE_BLOCK, BASEFEE_WEI / 1e9, TIP_WEI / 1e9, ETH_USD))

# ЯКОРЬ: не веря своей модели цены газа, привязываемся к ЗАМЕРЕННОМУ боевому факту.
P_LO, P_HI = PROBES_PER_DAY
C_LO, C_HI = COMBAT_USD_PER_DAY
ANCHOR_USD_PER_GAS_LO = C_LO / P_HI / V5_MISS_HEALTHY      # консервативный конец
ANCHOR_USD_PER_GAS_HI = C_HI / P_LO / V5_MISS_HEALTHY
_mdl_lo, _ = usd(V5_MISS_HEALTHY, raw_b)
print("ЯКОРЬ: боевой факт $%.0f-%.0f/сутки при %dk-%dk проб и промахе v5 %d газа =>"
      % (C_LO, C_HI, P_LO // 1000, P_HI // 1000, V5_MISS_HEALTHY))
_band_lo, _band_hi = C_LO / P_HI, C_HI / P_LO
_verdict = ("СХОДИТСЯ — независимая модель попала в замеренную боевую вилку"
            if _band_lo <= _mdl_lo <= _band_hi else
            ("модель НИЖЕ факта (в бою платят и за V1-пробы/тиры выше пола)"
             if _mdl_lo < _band_lo else "модель ВЫШЕ факта"))
print("       $%.3e..%.3e за газ. Своя модель (baseFee+tip+L1) даёт $%.3e за пробу против "
      "$%.3e..%.3e якоря — %s"
      % (ANCHOR_USD_PER_GAS_LO, ANCHOR_USD_PER_GAS_HI, _mdl_lo, _band_lo, _band_hi, _verdict))


def probe_day_usd(gas):
    """$/сутки на промахах. Якорь: цена за газ выведена из БОЕВОГО факта, а не из моей модели."""
    return gas * ANCHOR_USD_PER_GAS_LO * P_HI, gas * ANCHOR_USD_PER_GAS_HI * P_LO


print()
print("C1. ПРОМАХ (господствующий случай) — цена и доля суточного капа $%.0f" % DAILY_CAP_USD)
print("%-28s %-6s %-11s %-9s %-18s %-16s %-10s" % (
    "дизайн", "M", "промах,газ", "x v5", "$/сутки(60-75k)", "покрытие 1 пробой", "кап $100"))
ROWS = []
for m in MS:
    ROWS.append(("v5 (один слот)", 1, V5_MISS_HEALTHY, 1, "1 цель"))
    break
for m in MS:
    ROWS.append(("D1 индекс слота", m, B["D1"][m]["healthy"], m, "1 цель (1/M пакета)"))
for m in MS:
    ROWS.append(("D2 перебор, 1 рынок", m, B["D2"][m]["healthy"], m, "M целей"))
for m in MS:
    ROWS.append(("D2 перебор, M рынков", m, B["D2"][m]["healthy_multi"], m, "M целей"))
for m in MS:
    ROWS.append(("D3 пакет рынка", m, B["D3"][m]["healthy"], m, "M целей (перебор с байта)"))
for G in (1, 2, 4, 8):
    ROWS.append(("D4 группа=рынок, 8 целей", G, B["D4"][G]["healthy"], G,
                 "8 целей адресован. рынка"))
for name, m, g, _mm, cover in ROWS:
    lo, hi = probe_day_usd(g)
    fits = "влезает" if hi <= DAILY_CAP_USD else ("ГРАНИЦА" if lo <= DAILY_CAP_USD else "НЕ ВЛЕЗАЕТ")
    print("%-28s %-6d %-11d %-9.2f $%-17s %-16s %-10s"
          % (name, m, g, g / V5_MISS_HEALTHY, "%.0f-%.0f" % (lo, hi), cover, fits))

print()
print("C2. ЕСЛИ ДЕРЖАТЬ ПЛОТНОСТЬ НА ЦЕЛЬ (D1: одна проба = одна цель => проб нужно в M раз больше)")
print("%-28s %-6s %-14s %-18s %-10s" % ("дизайн", "M", "проб/сутки", "$/сутки", "кап $100"))
for m in MS:
    g = B["D1"][m]["healthy"]
    lo, hi = probe_day_usd(g)
    lo, hi = lo * m, hi * m
    fits = "влезает" if hi <= DAILY_CAP_USD else ("ГРАНИЦА" if lo <= DAILY_CAP_USD else "НЕ ВЛЕЗАЕТ")
    print("%-28s %-6d %-14s $%-17s %-10s" % ("D1 (плотность x M)", m,
                                             "%dk-%dk" % (P_LO * m // 1000, P_HI * m // 1000),
                                             "%.0f-%.0f" % (lo, hi), fits))
for m in MS:
    g = B["D3"][m]["healthy"]
    lo, hi = probe_day_usd(g)
    fits = "влезает" if hi <= DAILY_CAP_USD else ("ГРАНИЦА" if lo <= DAILY_CAP_USD else "НЕ ВЛЕЗАЕТ")
    print("%-28s %-6d %-14s $%-17s %-10s" % ("D3 (плотность НЕ делится)", m,
                                             "%dk-%dk" % (P_LO // 1000, P_HI // 1000),
                                             "%.0f-%.0f" % (lo, hi), fits))

print()
print("C3. АРМ. ГЛАВНАЯ строка — ЗАМЕРЕННЫЙ каданс %d-%d армов/сутки НА ВЕСЬ ФЛОТ (журнал боевой"
      % ARMS_PER_DAY_MEASURED)
print("    машины, 7 суток: 37/68/68/91/88, медиана ~68, пик 91; армы живут в окнах волатильности")
print("    и НЕ перекладываются непрерывно). Один арм пишет M слотов ОДНОЙ tx — число армов от M")
print("    не растёт. Каданс — от СУБЪЕКТА (замер), а не от TTL плана.")
print()
A_LO, A_HI = ARMS_PER_DAY_MEASURED
ARMS_TTL = 86400 // TTL_SEC
_l1_by = {}
for m in MS:
    _l1_by[("D1/D2", m)] = l1_fee(A2["D1/D2"][m]["raw"])
    _l1_by[("D3", m)] = l1_fee(A2["D3"][m]["raw"])
_l1_v5 = L1_V5_ARM


def arm_usd(gas, l1):
    """$/арм: L2 (gas x (basefee+tip)) + L1 data fee по СЫРЫМ байтам подписанной tx."""
    return (gas * (BASEFEE_WEI + TIP_WEI) + (l1 or 0)) / 1e18 * ETH_USD


print("ГЛАВНАЯ (замеренный каданс %d-%d армов/сутки на флот):" % (A_LO, A_HI))
print("%-16s %-4s %-12s %-13s %-11s %-16s %-10s" % (
    "дизайн", "M", "арм,газ", "L1-fee,wei", "$/арм", "$/сут (70-90)", "кап $100"))


def arm_row(label, m, gas, l1):
    per = arm_usd(gas, l1)
    lo, hi = per * A_LO, per * A_HI
    fits = "влезает" if hi <= DAILY_CAP_USD else ("ГРАНИЦА" if lo <= DAILY_CAP_USD else "НЕ ВЛЕЗАЕТ")
    print("%-16s %-4d %-12d %-13s $%-10.4f $%-15s %-10s" % (
        label, m, gas, l1, per, "%.2f-%.2f" % (lo, hi), fits))
    return per


V5_ARM_USD = arm_row("v5 (контроль)", 1, V5_ARM_COLD, _l1_v5)
ARM_USD = {}
for label, key in (("D1/D2", "D1/D2"), ("D3", "D3")):
    for m in MS:
        ARM_USD[(key, m)] = arm_row(label, m, A2[key][m]["cold"], _l1_by[(key, m)])

print()
print("СТРЕСС-ГРАНИЦА (непрерывная перекладка по TTL %dс = %d армов/сутки НА КАЖДЫЙ фид) —"
      % (TTL_SEC, ARMS_TTL))
print("    ВЕРХНЯЯ оценка на случай смены режима. СЕГОДНЯ ЭТОГО НИКТО НЕ ПЛАТИТ.")
print("%-16s %-4s %-14s %-14s %-14s" % ("дизайн", "M", "$/сут F=1", "$/сут F=3", "$/сут F=23"))
print("%-16s %-4d $%-13.2f $%-13.2f $%-13.2f" % (
    "v5 (контроль)", 1, V5_ARM_USD * ARMS_TTL, V5_ARM_USD * ARMS_TTL * 3, V5_ARM_USD * ARMS_TTL * 23))
for label, key in (("D1/D2", "D1/D2"), ("D3", "D3")):
    for m in MS:
        p = ARM_USD[(key, m)]
        print("%-16s %-4d $%-13.2f $%-13.2f $%-13.2f" % (
            label, m, p * ARMS_TTL, p * ARMS_TTL * 3, p * ARMS_TTL * 23))

print()
print("C4. СУММА (промах + арм). ВЕРДИКТ по ГЛАВНОЙ строке — замеренному кадансу армов")
print("%-22s %-4s %-16s %-14s %-16s %-12s %-14s" % (
    "дизайн", "M", "$/сут промахи", "$/сут армы", "$/сут ВСЕГО", "кап $100", "стресс-TTL F=23"))
for label, key, bkey in (("D1 (индекс)", "D1/D2", "D1"), ("D2 (перебор)", "D1/D2", "D2"),
                         ("D3 (пакет рынка)", "D3", "D3")):
    for m in MS:
        gmiss = B[bkey][m]["healthy"]
        plo, phi = probe_day_usd(gmiss)
        if bkey == "D1":
            plo, phi = plo * m, phi * m           # плотность на цель делится -> проб в M раз больше
        per = ARM_USD[(key, m)]
        alo, ahi = per * A_LO, per * A_HI
        tlo, thi = plo + alo, phi + ahi
        fits = "влезает" if thi <= DAILY_CAP_USD else ("ГРАНИЦА" if tlo <= DAILY_CAP_USD else "НЕ ВЛЕЗАЕТ")
        stress = phi + per * ARMS_TTL * 23
        print("%-22s %-4d $%-15s $%-13s $%-15s %-12s $%-13.0f" % (
            label, m, "%.0f-%.0f" % (plo, phi), "%.2f-%.2f" % (alo, ahi),
            "%.0f-%.0f" % (tlo, thi), fits, stress))
# D4: арм — ОДНА tx на группу (рынок), по 8 целей в каждой. Замеренный каданс 70-90 армов/сутки
# считает именно армы РЫНКОВ, поэтому от числа одновременно живых групп G он не растёт.
for G in (1, 2, 4, 8):
    gmiss = B["D4"][G]["healthy"]
    plo, phi = probe_day_usd(gmiss)
    per = arm_usd(D4_ARM[G]["gas"], l1_fee(D4_ARM[G]["raw"]))
    alo, ahi = per * A_LO, per * A_HI
    tlo, thi = plo + alo, phi + ahi
    fits = "влезает" if thi <= DAILY_CAP_USD else ("ГРАНИЦА" if tlo <= DAILY_CAP_USD else "НЕ ВЛЕЗАЕТ")
    print("%-22s %-4d $%-15s $%-13s $%-15s %-12s $%-13.0f" % (
        "D4 G групп x 8 целей", G, "%.0f-%.0f" % (plo, phi), "%.2f-%.2f" % (alo, ahi),
        "%.0f-%.0f" % (tlo, thi), fits, phi + per * ARMS_TTL * 23))

print()
print("=" * 96)
print("СВОДКА ЧИСЕЛ (для отчёта)")
print("=" * 96)
print("форк-блок %d | живая Base %d, baseFee %.4f gwei | ETH $%.2f | tip-пол %.3f gwei"
      % (FORK_BLOCK, LIVE_BLOCK, BASEFEE_WEI / 1e9, ETH_USD, TIP_WEI / 1e9))
print("v5: промах пусто %d, промах здоров %d, ПОПАДАНИЕ %d, арм cold %d / warm %d (роут %dБ)"
      % (V5_MISS_EMPTY, V5_MISS_HEALTHY, V5_HIT, V5_ARM_COLD, V5_ARM_WARM, ROUTE_TYPICAL))
print("D1: промах пусто %d, здоров %d..%d (M=1..8), ПОПАДАНИЕ %d"
      % (D1_MISS_EMPTY, B["D1"][1]["healthy"], B["D1"][8]["healthy"], D1_HIT))
print("D2: промах пусто %d, здоров 1рынок %d..%d / Mрынков %d..%d, ПОПАДАНИЕ %d"
      % (D2_MISS_EMPTY, B["D2"][1]["healthy"], B["D2"][8]["healthy"],
         B["D2"][1]["healthy_multi"], B["D2"][8]["healthy_multi"], D2_HIT))
print("D3: промах пусто %d, здоров %d..%d (M=1..8), ПОПАДАНИЕ %d, односрельность %d газа"
      % (D3_MISS_EMPTY, B["D3"][1]["healthy"], B["D3"][8]["healthy"], D3_HIT, B6_HIT_GAS))
print("D4: промах пусто %d, здоров %d..%d (G=1..8 групп x 8 целей = до 64 целей), ПОПАДАНИЕ %d,"
      " арм одной группы(8 целей, роут %dБ) %d газа"
      % (D4_MISS_EMPTY, B["D4"][1]["healthy"], B["D4"][8]["healthy"], D4_HIT, ROUTE_TYPICAL,
         D4_ARM[1]["gas"]))
print("арм cold, роут %dБ/слот: D1/D2 %s | D3 %s"
      % (ROUTE_TYPICAL, {m: A2["D1/D2"][m]["cold"] for m in MS}, {m: A2["D3"][m]["cold"] for m in MS}))
print("=" * 96)
