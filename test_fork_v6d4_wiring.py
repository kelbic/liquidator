"""ПРИЁМКА ОБВЯЗКИ v6-D4 НА ФОРКЕ (SPEC_V6D4_WIRING.md §8), эталон — ЦЕПЬ, не самоотчёт.

Отличие от test_fork_v6d4.py: тот принимает КОНТРАКТ (§5, 12/12). Здесь принимается ОБВЯЗКА
chain/board_v6.py — что ЕЁ кодирование садится в реальный контракт, приёмка арма ловит несевший
арм, указатель берёт until С ЦЕПИ, байт 0xFF доезжает до цели, промах стоит обещанное.

Каноны стенда:
 * ПОЗИТИВ + ПАРНЫЙ КРАСНЫЙ: у арма-сел стоит красный контроль (арм с малым газом = «не сел»).
 * ЭТАЛОН — СЫРОЙ СТОРАДЖ / КВИТАНЦИЯ, не счётчик обвязки.
 * 0xFF доезжает до цели ДОКАЗЫВАЕТСЯ КАНАРЕЙКОЙ (unwrapTarget-контракт ревертит своим
   селектором; он доезжает до PlanFailed => квитанция САМА называет клетку — денежный путь и
   реальный Morpho для этого не нужны).

Без web3: сырой JSON-RPC (requests) + eth_account + eth_abi. Кодирование — ТОЛЬКО через board_v6
(в этом и приёмка обвязки). Форк Base нужен ради РЕАЛЬНОГО оракула (промах несёт цену его
STATICCALL — иначе число промаха не сойдётся с эталоном 57 434).

ЗАПУСК:
    anvil --fork-url https://mainnet.base.org --fork-block-number 49666337 --port 8552 --silent &
    FORK_RPC=http://127.0.0.1:8552 python3 test_fork_v6d4_wiring.py
Без FORK_RPC — SKIP (код 0). Ненулевой код = провал приёмки.
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RPC_URL = os.environ.get("FORK_RPC", "")
if not RPC_URL:
    print("SKIP: FORK_RPC не задан (нужен anvil-форк Base, см. докстринг)")
    sys.exit(0)

import json                                          # noqa: E402
import requests                                      # noqa: E402
from eth_account import Account                      # noqa: E402
from eth_abi import decode as abi_decode             # noqa: E402
from eth_utils import keccak, to_checksum_address    # noqa: E402

import chain.board_v6 as bd                          # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
ANVIL0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ZERO = bd.ZERO_ADDR
ROUTE_TYPICAL = 2048
MISS_LIMIT = 58000          # §8.4 порог
MISS_REF = 57434            # §8.4 эталон (count=1, замер test_fork_v6d4.py MISS_REF)
# Реальный рынок Base (тот же, что в test_fork_v6d4.py) — ради живого оракула.
MP = {"loanToken": to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
      "collateralToken": to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
      "oracle": to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE"),
      "irm": to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
      "lltv": 625000000000000000}
PX_SEL = "0x" + keccak(text="price()")[:4].hex()
T_PLANFAILED = keccak(text="PlanFailed(bytes4)")
T_HOTSET = keccak(text="HotSet(uint8,uint8,uint64)")

S = requests.Session()
_ID = [0]


def rpc(method, params):
    _ID[0] += 1
    r = S.post(RPC_URL, json={"jsonrpc": "2.0", "id": _ID[0], "method": method, "params": params},
               timeout=120).json()
    if "error" in r:
        raise RuntimeError("%s -> %s" % (method, r["error"]))
    return r["result"]


def chain_id():
    return int(rpc("eth_chainId", []), 16)


CHAIN_ID = chain_id()
OWNER = Account.from_key(ANVIL0)


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


def revert_selector(to, data, frm):
    """eth_call, вернуть 4-байтовый селектор реверта (или None, если не ревертнул). anvil кладёт
    байты реверта в error.data — так читаем HotSpanLive без web3."""
    _ID[0] += 1
    r = S.post(RPC_URL, json={"jsonrpc": "2.0", "id": _ID[0], "method": "eth_call",
                              "params": [{"to": to, "data": data, "from": frm}, "latest"]},
               timeout=120).json()
    if "error" not in r:
        return None
    d = r["error"].get("data")
    if isinstance(d, dict):
        d = d.get("data")
    if isinstance(d, str) and d.startswith("0x") and len(d) >= 10:
        return "0x" + d[2:10]
    return repr(r["error"])[:80]


def status_of(rec):
    return int(rec["status"], 16) if isinstance(rec["status"], str) else int(rec["status"])


def gas_used(rec):
    return int(rec["gasUsed"], 16) if isinstance(rec["gasUsed"], str) else int(rec["gasUsed"])


def deploy():
    a = json.load(open("contracts/out/LiquidatorV6.sol/LiquidatorV6.json"))
    data = a["bytecode"]["object"] + ("0" * 24 + MORPHO[2:]).lower()
    rec = send({"to": None, "data": data}, gas=8_000_000)
    assert status_of(rec) == 1, "деплой v6 не сел"
    return to_checksum_address(rec["contractAddress"])


def snapshot():
    return rpc("evm_snapshot", [])


def revert(sid):
    assert rpc("evm_revert", [sid]) is True


def probe(addr, key, byte, gas=3_000_000):
    acct = Account.from_key(key)
    return send({"to": addr, "data": "0x" + byte}, key=key, gas=gas)


def logs_of(rec, topic, addr=None):
    out = []
    for lg in rec["logs"]:
        tp = lg["topics"]
        if tp and bytes.fromhex(tp[0][2:]) == topic and (addr is None or lg["address"].lower() == addr.lower()):
            out.append(lg)
    return out


def deadline(ttl=900):
    ts = int(rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    return max(ts, int(time.time())) + ttl


RESULTS, FAILS = [], []


def verdict(point, what, ok):
    RESULTS.append((point, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
    if not ok:
        FAILS.append(point)


print("=" * 100)
print("ПРИЁМКА ОБВЯЗКИ v6-D4 (SPEC_V6D4_WIRING.md §8) — кодирование ТОЛЬКО через board_v6")
print("=" * 100)
rpc("evm_mine", [])           # локальный блок: дальше latest всегда локальный
PX = int(call(MP["oracle"], PX_SEL).hex(), 16)
assert PX > 0, "оракул рынка отдал 0 — форк не архивный / не Base"
print("форк %s: chainId %d, oracle.price()=%.4e" % (RPC_URL, CHAIN_ID, PX))
GATE_FIRE = PX * 2            # p <= gate => огонь
GATE_HEAL = PX // 2           # p >  gate => промах (здоров)

# ноги: несколько классов вычета для спана
LEGS = [Account.create() for _ in range(8)]
for a in LEGS:
    send({"to": a.address, "value": int(2 * 10 ** 18)}, gas=21000)
LEG = LEGS[0]


def send_fn(cd, gas):
    return send({"to": ADDR, "data": cd}, gas=gas)


def authorize(addr, legs):
    rec = send({"to": addr, "data": bd.encode_set_authorized_batch([a.address for a in legs], True)},
               gas=2_000_000)
    assert status_of(rec) == 1


# --- канарейка: unwrapTarget, ревертит своим селектором (доезжает до PlanFailed) --------------
def put_canary(sel4):
    a = to_checksum_address("0x%040x" % (0xCA0000 + int(sel4, 16) % 0xFFFF))
    # PUSH4 sel; MSTORE@0; REVERT(0x1c,4)  => вернёт ровно 4 байта sel
    rpc("anvil_setCode", [a, "0x63" + sel4 + "600052" + "6004601cfd"])
    return a


CANARY_SEL = "aabbccdd"
CANARY = put_canary(CANARY_SEL)


def one_target(borrower=None, route_len=0, trig=0, unwrap=ZERO):
    return bd.make_target(borrower or ("0x" + "be" * 20), repaid_shares=7, swap_target=("0x" + "51" * 20),
                          swap_calldata=bytes(route_len), min_profit=0, unwrap_target=unwrap,
                          trigger_price=trig)


ADDR = deploy()
authorize(ADDR, LEGS)
BASE = snapshot()

# ============================================================================================
# W1 (§8.1). АРМ СЕЛ ПО ЦЕПИ + ПАРНЫЙ КРАСНЫЙ: арм с малым газом = «не сел», а не «armed»
# ============================================================================================
print()
print("W1 (§8.1). Приёмка арма ПО ЦЕПИ (verify_arm), красный контроль — малый лимит газа")
sid = snapshot()
dl = deadline()
ts = [one_target(borrower="0x%040x" % (0xB0 + k), route_len=64) for k in range(4)]
# КРАСНЫЙ: тот же арм с заведомо малым лимитом — обязан дать «не сел»
cd = bd.encode_set_group(1, MP, dl, GATE_HEAL, ts)
rec_bad = send({"to": ADDR, "data": cd}, gas=200_000)         # << стоимости арма
ok_bad, why_bad = bd.verify_arm(call, ADDR, 1, rec_bad, dl, [t[0] for t in ts])
# ЗЕЛЁНЫЙ (парный позитив): полный газ через оркестратор board_v6.arm_group
ok, rec, why = bd.arm_group(send_fn, call, ADDR, 1, MP, dl, GATE_HEAL, ts)
print("    красный (газ 200k): статус=%d verify_arm ok=%s (%s)" % (status_of(rec_bad), ok_bad, why_bad))
print("    зелёный (arm_group): статус=%d gasUsed=%d verify_arm ok=%s (%s)"
      % (status_of(rec), gas_used(rec), ok, why))
W1_OK = (not ok_bad) and ok and status_of(rec) == 1
verdict("§8.1", "verify_arm: малый газ -> «не сел» (%s); полный газ arm_group -> сел по цепи "
        "(deadline+n+borrowers)" % ("status!=1" if status_of(rec_bad) != 1 else "storage пуст"), W1_OK)
revert(sid)

# ============================================================================================
# W2 (§8.2). УКАЗАТЕЛЬ: until БЕРЁТСЯ С ЦЕПИ (min дедлайнов спана), verify_hot
# ============================================================================================
print()
print("W2 (§8.2). Указатель: until = min(groupInfo.deadline по спану) С ЦЕПИ")
sid = snapshot()
dl_a, dl_b = deadline(900), deadline(600)          # РАЗНЫЕ дедлайны -> until = меньший
ta = [one_target(route_len=64)]
tb = [one_target(route_len=64)]
ok_a, _, _ = bd.arm_group(send_fn, call, ADDR, 2, MP, dl_a, GATE_HEAL, ta)
ok_b, _, _ = bd.arm_group(send_fn, call, ADDR, 3, MP, dl_b, GATE_HEAL, tb)
chain_until = bd.until_from_chain(call, ADDR, 2, 2)
ok_hot, rec_hot, why_hot, used_until = bd.set_hot_pointed(send_fn, call, ADDR, 2, 2, MP)
print("    группы 2,3 армлены (deadline %d / %d); until_from_chain=%d (== min: %s)"
      % (dl_a, dl_b, chain_until, chain_until == min(dl_a, dl_b)))
print("    set_hot_pointed: статус=%d until=%d verify_hot ok=%s (%s)"
      % (status_of(rec_hot) if rec_hot else -1, used_until, ok_hot, why_hot))
W2_OK = (ok_a and ok_b and chain_until == min(dl_a, dl_b) and used_until == min(dl_a, dl_b)
         and ok_hot)
verdict("§8.2", "until взят С ЦЕПИ = min(%d,%d)=%d; указатель сел (hotInfo с цепи)"
        % (dl_a, dl_b, chain_until), W2_OK)
revert(sid)

# ============================================================================================
# W3 (§8.3). БАЙТ 0xFF ДОЕЗЖАЕТ ДО ЦЕЛИ через горячий указатель (доказано КАНАРЕЙКОЙ)
# ============================================================================================
print()
print("W3 (§8.3). Байт 0xFF доезжает до цели через _hot (эталон — селектор канарейки в PlanFailed)")
sid = snapshot()
dl = deadline()
# цель с unwrapTarget=канарейка: попадание => liquidateEx зовёт canary.asset() => реверт селектором
tc = [one_target(borrower="0x" + "be" * 20, route_len=64, trig=0, unwrap=CANARY)]
ok_c, _, _ = bd.arm_group(send_fn, call, ADDR, 4, MP, dl, GATE_FIRE, tc)   # GATE_FIRE => огонь
ok_hot, rec_hot, why_hot, _ = bd.set_hot_pointed(send_fn, call, ADDR, 4, 1, MP)
rp = probe(ADDR, LEG.key.hex(), "ff")
pf = logs_of(rp, T_PLANFAILED, ADDR)
got_sel = ("0x" + pf[0]["data"][2:10]) if pf else None
print("    группа 4 армлена (unwrap=канарейка), указатель count=1 наведён (ok=%s)" % ok_hot)
print("    проба 0xFF с авторизованной ноги: PlanFailed=%s (ждём 0x%s => 0xFF дошёл до цели g=4)"
      % (got_sel, CANARY_SEL))
W3_OK = ok_c and ok_hot and got_sel == ("0x" + CANARY_SEL)
verdict("§8.3", "байт 0xFF через _hot доехал до цели g=4: PlanFailed нёс селектор канарейки %s"
        % CANARY_SEL, W3_OK)
revert(sid)

# ============================================================================================
# W4 (§8.4). ГАЗ ПРОМАХА ≈ 57 434 (порог 58 000) С КВИТАНЦИИ (0xFF, count=1, здоров по цене)
# ============================================================================================
print()
print("W4 (§8.4). Газ промаха 0xFF (count=1, здоров) с КВИТАНЦИИ, порог %d" % MISS_LIMIT)
sid = snapshot()
dl = deadline()
# боевой роут 2КБ в арме (промах роут НЕ читает — число должно быть тем же), gate здоров => промах
tm = [one_target(borrower="0x" + "be" * 20, route_len=ROUTE_TYPICAL, trig=0)]
ok_m, _, _ = bd.arm_group(send_fn, call, ADDR, 5, MP, dl, GATE_HEAL, tm)
bd.set_hot_pointed(send_fn, call, ADDR, 5, 1, MP)
rmiss = probe(ADDR, LEG.key.hex(), "ff")
mg = gas_used(rmiss)
fired = bool(logs_of(rmiss, T_PLANFAILED, ADDR)) or len(rmiss["logs"]) > 0
print("    проба 0xFF на ЗДОРОВОЙ группе: gasUsed=%d, логов=%d (промах = тихо, огня нет)"
      % (mg, len(rmiss["logs"])))
print("    эталон 57 434 | порог 58 000 | измерено %d (Δ к эталону %+d)" % (mg, mg - MISS_REF))
W4_OK = ok_m and status_of(rmiss) == 1 and (not fired) and mg < MISS_LIMIT
verdict("§8.4", "промах 0xFF count=1 = %d газа (порог %d, эталон %d)" % (mg, MISS_LIMIT, MISS_REF), W4_OK)
revert(sid)

# ============================================================================================
# W5 (§8.5). ГАРД §1 ЖИВОЙ В БОЮ: setGroup/clearGroup в живой спан -> HotSpanLive (боевой рынок)
# ============================================================================================
print()
print("W5 (§8.5). Гард §1 (HotSpanLive) живой на форке с БОЕВЫМ рынком, не только на фордж-стенде")
HOTSPANLIVE = "0x" + keccak(text="HotSpanLive()")[:4].hex()
sid = snapshot()
dl_a, dl_b = deadline(900), deadline(600)
bd.arm_group(send_fn, call, ADDR, 2, MP, dl_a, GATE_HEAL, [one_target(route_len=64)])
bd.arm_group(send_fn, call, ADDR, 3, MP, dl_b, GATE_HEAL, [one_target(route_len=64)])
bd.set_hot_pointed(send_fn, call, ADDR, 2, 2, MP)                 # спан [2,4)
# setGroup в живой спан — ОТКАЗ (селектор реверта == HotSpanLive), и tx откатывается
sel_sg = revert_selector(ADDR, bd.encode_set_group(2, MP, deadline(), GATE_HEAL,
                                                    [one_target(route_len=64)]), OWNER.address)
sel_cg = revert_selector(ADDR, bd.encode_clear_group(2), OWNER.address)
# ПАРНЫЙ ПОЗИТИВ: clearHot -> setGroup(2) проходит (перезарядка спана после разоружения указателя)
rec_ch = send({"to": ADDR, "data": bd.encode_clear_hot()}, gas=200_000)
ok_c, rec_re, why_re = bd.arm_group(send_fn, call, ADDR, 2, MP, deadline(), GATE_HEAL,
                                    [one_target(borrower="0x" + "cd" * 20, route_len=64)])
print("    setGroup в живой спан [2,4): реверт %s (ждём %s)" % (sel_sg, HOTSPANLIVE))
print("    clearGroup в живой спан [2,4): реверт %s (ждём %s)" % (sel_cg, HOTSPANLIVE))
print("    ПАРНЫЙ ПОЗИТИВ: clearHot -> setGroup(2) статус=%d verify=%s" % (status_of(rec_re), ok_c))
W5_OK = (sel_sg == HOTSPANLIVE and sel_cg == HOTSPANLIVE and status_of(rec_ch) == 1 and ok_c)
verdict("§8.5", "гард живой в бою: setGroup/clearGroup в спан -> HotSpanLive; после clearHot "
        "перезарядка проходит", W5_OK)
revert(sid)

# ============================================================================================
print()
print("=" * 100)
print("ТАБЛИЦА ПРИЁМКИ ОБВЯЗКИ §8 (кодирование через board_v6, эталон — цепь/квитанция)")
print("=" * 100)
for p, ok, what in RESULTS:
    print("%-7s %-8s %s" % (p, ok, what))
print("=" * 100)
if FAILS:
    print("ПРИЁМКА ОБВЯЗКИ НЕ СДАНА: %s" % ", ".join(sorted(set(FAILS))))
    sys.exit(1)
print("ПРИЁМКА ОБВЯЗКИ СДАНА: %d/%d пунктов" % (len(RESULTS), len(RESULTS)))
