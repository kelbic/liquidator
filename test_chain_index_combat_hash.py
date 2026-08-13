"""БЛОКЕР Р2 №1: форма хэша БОЕВОГО пути + №3/№4 (слепой суд и владение транзитом).

ПОЧЕМУ ЭТОТ ТЕСТ СУЩЕСТВУЕТ. Прежний набор кормил индекс литералами `'0x' + '11'*32` — то
есть формой, которой в бою НЕ БЫВАЕТ. Настоящая лестница подписывает пробу через
`eth_account`, а `hexbytes` 1.3.1 отдаёт `signed.hash.hex()` БЕЗ префикса '0x'; узел же
всегда отвечает С префиксом. Из-за этого `_by_hash` (форма узла) и `_wanted`/`state()`
(форма подписи) не пересекались НИКОГДА: `receipts_for` возвращал {} всегда, `lookup` не
находил севшую пробу, а `state()` на СЕВШУЮ пробу отвечал 'absent' — индекс, поставленный
опровергать ложь «NO-RECEIPT ⇒ проба не села», сам её ПОДТВЕРЖДАЛ и сбрасывал склад живой ноги.

ЗДЕСЬ ХЭШ НЕ ПИШЕТСЯ РУКАМИ. Он приезжает из настоящего `presign_ladder` через настоящий
`send_presigned_probe`, вызванный настоящим боевым замыканием `_send` (взято из
`SpecFireLive._engine()`, а не переписано). Подменён РОВНО один стык — сокет.

ДРЕЛЬ УМЕЕТ ПАДАТЬ: мутация механизма (нормализация возвращается к прежнему `str(h).lower()`)
обязана вернуть все прежние симптомы, включая ЛОЖНЫЙ приговор и сброс склада. Если мутация
проходит зелёной — стенда нет.
"""
import gzip
import json
import pathlib
import sys
import threading
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                 # noqa: E402
import chain.execute as ex                                     # noqa: E402
import chain.feeds as feeds                                    # noqa: E402
import chain.fire_engine as fe                                 # noqa: E402
import chain.spec_fire_live as sfl                             # noqa: E402
from eth_account import Account                                # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
BN = 49890266
BOARD = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
KEY = "0x" + "42" * 32
ADDR = Account.from_key(KEY).address
BASEFEE = int(0.005e9)
BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS = json.loads((FIX / "logs_49890266.json").read_text())["result"]


# ---------------------------------------------------------------- двойники (только сокет)
class _FakeEth:
    account = Account

    def __init__(self, o):
        self.o = o

    def get_block(self, _b):
        return {"baseFeePerGas": BASEFEE}

    def get_balance(self, _a):
        return self.o.balance_wei

    def get_transaction_count(self, _a, _b="pending"):
        return 0

    def send_raw_transaction(self, raw):
        self.o.sent.append(raw)
        return b"\x00"


class _FakeW3:
    def __init__(self, o):
        self.eth = _FakeEth(o)


class _FakeRpc:
    chain_id = 8453
    rpc_url = "https://combat.example/COMBAT"

    def __init__(self):
        self.sent = []
        self.balance_wei = int(0.05e18)

    def _web3(self):
        return _FakeW3(self)


CFG = types.SimpleNamespace(liquidator_address=BOARD, tip_gwei=0.05, gas_limit_est=500_000,
                            eth_price_usd=3000.0, sequencer_rpc="off",
                            rpc_url="https://combat.example/COMBAT")
LOG = []


def make_sf(rpc):
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf.cfg = CFG
    sf.rpc = rpc
    sf.stats = {}
    sf._lock = threading.RLock()
    sf._log = lambda m: LOG.append(m)
    sf._now = lambda: 0.0
    sf._volley_accounts_fn = lambda: [(ADDR, KEY)]
    return sf


def combat_send():
    """НАСТОЯЩЕЕ боевое замыкание отправки пробы: то самое, что зовёт движок огня."""
    rpc = _FakeRpc()
    ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
    ex.invalidate_ladder(ADDR)
    ex._VOLLEY_NONCE.pop(ADDR, None)
    ex._VOLLEY_BAL.pop(ADDR, None)
    ex._VOLLEY_GAS_USD = 0.0
    ex._VOLLEY_GAS_RESERVED = 0.0
    ex._INFLIGHT = 0
    sf = make_sf(rpc)
    ex.presign_ladder(rpc, CFG, ADDR, KEY, 3_000_000, depth=4, start_nonce=0, log=None)
    # Движок выключен ручкой по умолчанию (SPEC_FIRE_ENGINE=0) — это ОТКАТ МЕХАНИЗМА, а не
    # признак его отсутствия. Для дрели включаем ровно тот флаг, что читает боевой код.
    _sv_on, fe.ENGINE_ON = fe.ENGINE_ON, True
    try:
        eng = sf._engine()
    finally:
        fe.ENGINE_ON = _sv_on
    assert eng is not None, "движок огня выключен — боевое замыкание не построить"
    send = eng._send_fn
    try:
        h = send(ADDR)
    finally:
        eng.stop()
    return sf, h


# ---------------------------------------------------------------- 1. форма боевого хэша
_sf, H = combat_send()
assert H, "боевая отправка не вернула хэш"
assert not H.startswith("0x") and len(H) == 64, (
    "ПОЗИТИВНЫЙ КОНТРОЛЬ ФОРМЫ: на hexbytes 1.3.1 боевой путь отдаёт хэш БЕЗ префикса. "
    "Если форма изменилась — этот тест обязан сказать об этом ВСЛУХ, а не тихо позеленеть: %r" % H)
NODE_H = "0x" + H                       # так эту же tx видит узел
print("UNIT_OK: боевой presign_ladder->send_presigned_probe отдал хэш В СВОЕЙ форме (%s…, без "
      "'0x'), узел ту же tx зовёт %s…" % (H[:10], NODE_H[:12]))


# ---------------------------------------------------------------- сокет-двойник узла
def make_http(nblocks=0):
    """Записанный блок 49890266, где ОДНА проба доски — наша, только что подписанная."""
    blk = json.loads(json.dumps(BLOCK["result"]))
    probe = None
    for tx in blk["transactions"]:
        if str(tx.get("to") or "").lower() == BOARD.lower():
            probe = tx
            break
    assert probe is not None
    probe["hash"] = NODE_H
    probe["from"] = ADDR.lower()
    probe["maxPriorityFeePerGas"] = hex(2_000_000)      # tip транзита — до wei (механика приза)
    rc = {"transactionHash": NODE_H, "status": "0x1", "gasUsed": hex(54710),
          "effectiveGasPrice": hex(BASEFEE + 2_000_000), "logs": []}
    calls = []

    def _http(url, payload, timeout):
        m = payload["method"]
        calls.append(m)
        if m == "eth_blockNumber":
            return {"result": hex(BN + nblocks)}
        if m == "eth_getLogs":
            frm = int(payload["params"][0]["fromBlock"], 16)
            return {"result": LOGS if frm == BN else []}
        if m == "eth_getBlockByNumber":
            bn = int(payload["params"][0], 16)
            if bn == BN:
                return {"result": blk}
            b = dict(blk)
            b["number"], b["transactions"] = hex(bn), []
            return {"result": b}
        if m == "eth_getBlockReceipts":
            bn = int(payload["params"][0], 16)
            return {"result": [rc] if bn == BN else []}
        return {"result": None}
    return _http, calls


def fed_index(nblocks=0):
    http, calls = make_http(nblocks)
    idx = ci.ChainIndex("https://node.test/KEY", watch_to=[BOARD], http=http)
    idx._head_conf = BN + nblocks
    idx.note_hash(H, head=BN - 1)          # ровно то, что делает боевой _send
    for bn in range(BN, BN + nblocks + 1):
        idx.scan_block(bn, full=True)
    return idx, calls


# ---------------------------------------------------------------- 2. индекс УЗНАЁТ свою пробу
idx, _c = fed_index()
fact = idx.lookup(H)
assert fact is not None and fact["block"] == BN, "индекс обязан узнать свою пробу: %s" % (fact,)
r = idx.receipts_for([H])
assert list(r) == [H], "ключ ответа — В ФОРМЕ ВЫЗЫВАЮЩЕГО (inflight коллектора живёт в ней): %s" % r
assert ci.rcpt_int(r[H]["gasUsed"]) == 54710 and ci.rcpt_int(r[H]["status"]) == 1
assert idx.state(H)[0] == "mined"
assert len(idx._wanted) == 0, "посадка обязана СНИМАТЬ заявку, иначе _wanted течёт вечно"
print("UNIT_OK: lookup/receipts_for/state видят пробу, отправленную БОЕВЫМ путём (index_hits "
      "и polls_saved стали достижимы — приёмка (в) больше не пуста ПО ПОСТРОЕНИЮ)")


# ---------------------------------------------------------------- 3. МУТАЦИЯ: снять канон
def old_norm(h):
    """Дословно прежняя нормализация обоих швов: форма входа и оставалась формой ключа."""
    return str(h or "").lower()


_hx = ci.hx
try:
    ci.hx = old_norm
    idx_bad, _ = fed_index()
    assert idx_bad.lookup(H) is None, "мутация обязана вернуть слепоту"
    assert idx_bad.receipts_for([H]) == {}, "мутация обязана вернуть вечное {}"
    assert len(idx_bad.block_facts(BN)["ours"]) == 33, \
        "при этом блок разобран и наши tx в нём ВИДНЫ — слепота именно на стыке форм"
finally:
    ci.hx = _hx
print("UNIT_OK: МУТАЦИЯ (нормализация снята) возвращает receipts_for=={} и lookup=None при "
      "33 разобранных пробах в том же блоке — стенд умеет падать")


# ---------------------------------------------------------------- 4. ложь про 'absent'
_hx = ci.hx
try:
    ci.hx = old_norm
    idx_bad, _ = fed_index(nblocks=13)
    st_bad, _why = idx_bad.state(H)
    assert st_bad == "absent", (
        "БЕЗ канона индекс выносит ПРИГОВОР севшей пробе (та самая ложь, ради опровержения "
        "которой пакет и делался): %s / %s" % (st_bad, _why))
finally:
    ci.hx = _hx
idx_ok, _ = fed_index(nblocks=13)
assert idx_ok.state(H)[0] == "mined", "с каноном тот же прогон даёт ФАКТ посадки"
print("UNIT_OK: 13 блоков подряд прочитаны ЦЕЛИКОМ — без канона state()='absent' на СЕВШУЮ "
      "пробу (ложь), с каноном 'mined' (факт)")


# ------------------------------------------- 5. ДЕНЬГИ: склад ноги против ложного приговора
def run_timeout(index):
    calls = []
    saved = (ci._INDEX, ex.volley_invalidate_nonce, ex.invalidate_ladder, ex.volley_gas_settle,
             ex.volley_leg_landed, ex.volley_gas_add, ex.volley_bal_charge)
    ci._INDEX = index
    ex.volley_invalidate_nonce = lambda a: calls.append(("nonce", a))
    ex.invalidate_ladder = lambda a: calls.append(("ladder", a))
    ex.volley_gas_settle = lambda h, u: calls.append(("settle", h, u))
    ex.volley_leg_landed = lambda rpc, a: False       # цепь молчит — как в бою под 429
    ex.volley_gas_add = lambda usd: calls.append(("gas_usd", usd))
    ex.volley_bal_charge = lambda a, w: calls.append(("charge", a, w))
    sf = make_sf(_FakeRpc())
    try:
        sf._engine_timeout((ADDR, None), H)
    finally:
        (ci._INDEX, ex.volley_invalidate_nonce, ex.invalidate_ladder, ex.volley_gas_settle,
         ex.volley_leg_landed, ex.volley_gas_add, ex.volley_bal_charge) = saved
    return sf, calls


LOG.clear()
sf_ok, calls_ok = run_timeout(idx_ok)
assert ("ladder", ADDR) not in calls_ok and ("nonce", ADDR) not in calls_ok, \
    "проба СЕЛА — склад ноги трогать НЕЛЬЗЯ: %s" % calls_ok
assert sf_ok.stats.get("noreceipt_false") == 1 and sf_ok.stats.get("noreceipt_gas_booked") == 1
assert any("ИНДЕКС ОПРОВЕРГ" in m for m in LOG), LOG
assert ("charge", ADDR, 54710 * (BASEFEE + 2_000_000)) in calls_ok, calls_ok

_hx = ci.hx
try:
    ci.hx = old_norm
    idx_bad, _ = fed_index(nblocks=13)
    LOG.clear()
    sf_bad, calls_bad = run_timeout(idx_bad)
finally:
    ci.hx = _hx
assert ("ladder", ADDR) in calls_bad and ("nonce", ADDR) in calls_bad, \
    "МУТАЦИЯ обязана сбросить склад ЖИВОЙ ноги: %s" % calls_bad
assert sf_bad.stats.get("noreceipt_dropped_confirmed") == 1, \
    ("и записать потерю как ПОДТВЕРЖДЁННУЮ — то есть соврать в самом честном счётчике: %s"
     % sf_bad.stats)
print("UNIT_OK: ДЕНЬГИ — с каноном склад ноги сохранён и газ 54710 книжится по факту; без "
      "канона тот же боевой `_engine_timeout` сбрасывает лестницу и пишет "
      "noreceipt_dropped_confirmed=1 (ложь в знаменателе потерь)")


# ---------------------------------------------------------------- 6. #109: коллектор и сеть
net_calls = []


def _net(hs):
    net_calls.append(list(hs))
    return {h: None for h in hs}


col = fe.ReceiptCollector(receipts_fn=_net, index_fn=lambda hs: idx_ok.receipts_for(hs),
                          on_result=lambda m, h, r: None, on_timeout=None,
                          now=lambda: 0.0, min_age_sec=0.0)
col.track(H, (ADDR, None))
col.tick()
assert col.stats["index_hits"] == 1, ("приёмка (в): index_hits обязан расти на БОЕВОЙ форме "
                                      "хэша, иначе он структурно ноль: %s" % col.stats)
assert net_calls == [], "исход известен из блока — в сеть за ним идти незачем: %s" % net_calls
assert col.stats["polls_saved"] == 1
print("UNIT_OK: настоящий ReceiptCollector закрыл партию из индекса (index_hits=1, "
      "polls_saved=1, сеть не спрошена)")


# ------------------------------- 7. БЛОКЕР №4: владение транзитом — по НАБОРУ ног рынка
MID = "0x" + "77" * 32
TRANSITS = idx_ok.transits_of(BN)
AGG_PRIZE = TRANSITS[-1]["agg"]                 # 1083 — за ним стоит победитель
AGG_OTHER = TRANSITS[0]["agg"]                  # 406
BF1_OFFCHAIN = "0x" + "b1" * 20                 # BF1-нога, транзита которой в блоке НЕТ


def note_position(legs, primary):
    saved = (dict(feeds._LEGS_AGG), dict(feeds._PRIMARY_AGG), ci._INDEX)
    feeds._LEGS_AGG.clear(); feeds._LEGS_AGG.update(legs)
    feeds._PRIMARY_AGG.clear(); feeds._PRIMARY_AGG.update(primary)
    ci._INDEX = idx_ok
    sf = make_sf(_FakeRpc())
    LOG.clear()
    try:
        sf._note_probe_position(H, {"blockNumber": hex(BN), "transactionIndex": hex(1083 + 1)},
                                meta=(ADDR, ("feed", MID, "0xb", 1.0)))
    finally:
        feeds._LEGS_AGG.clear(); feeds._LEGS_AGG.update(saved[0])
        feeds._PRIMARY_AGG.clear(); feeds._PRIMARY_AGG.update(saved[1])
        ci._INDEX = saved[2]
    return sf.stats, list(LOG)


# 7a. ПРЕЖНЕЕ владение (одна BF1-нога, транзита которой в блоке нет) -> свой транзит = чужой
st_bf1, log_bf1 = note_position({MID: [BF1_OFFCHAIN]}, {MID: BF1_OFFCHAIN})
assert st_bf1.get("probe_foreign_transit") == 1, st_bf1
assert not any("ПОЗИЦИИ" in m for m in log_bf1), \
    "строка ПОЗИЦИИ не печатается — жирное окно потрачено впустую: %s" % log_bf1

# 7b. НАБОР ног рынка: транзит приехал от НЕ-BF1 ноги — он НАШ
st_legs, log_legs = note_position({MID: [BF1_OFFCHAIN, AGG_PRIZE]}, {MID: BF1_OFFCHAIN})
assert st_legs.get("probe_judged") == 1 and st_legs.get("probe_foreign_transit") is None, st_legs
assert st_legs.get("probe_no_agg") is None and st_legs.get("probe_judged_blind") is None
assert st_legs.get("probe_in_band") == 1 and st_legs.get("probe_tip_match") == 1, \
    "проба стоит на транзит+1 с tip транзита до wei — это и есть метрика №1: %s" % st_legs
assert any("ПОЗИЦИИ blk=%d" % BN in m for m in log_legs), log_legs
print("UNIT_OK: транзит НЕ-BF1 ноги рынка признан СВОИМ (in_band=1, tip-match=1, строка "
      "ПОЗИЦИИ напечатана); прежний ключ по одной BF1 давал foreign и молчание")

# 7c. чужой рынок целиком — остаётся foreign (гард не размылся)
st_alien, _ = note_position({MID: ["0x" + "de" * 20]}, {MID: "0x" + "de" * 20})
assert st_alien.get("probe_foreign_transit") == 1, st_alien


# ------------------------------- 8. БЛОКЕР №3: слепой суд помечен и в деньги не идёт
st_blind, log_blind = note_position({}, {})
assert st_blind.get("probe_no_agg") == 1 and st_blind.get("probe_judged") == 1, st_blind
assert st_blind.get("probe_judged_blind") == 1, \
    "суд без владельца обязан быть ПОМЕЧЕН, иначе приёмка (б) проходится против чужого транзита"
assert st_blind.get("probe_tip_match") is None and st_blind.get("probe_in_band") is None, \
    ("вслепую денежные счётчики расти НЕ имеют права: %s" % st_blind)
assert st_blind.get("probe_tip_match_blind") == 1 and st_blind.get("probe_in_band_blind") == 1, \
    "но и терять замер нельзя — он уходит в blind-счётчики: %s" % st_blind
_parts = sum(st_blind.get(k, 0) for k in ("probe_idx_off", "probe_uncovered", "probe_no_transit",
                                          "probe_foreign_transit", "probe_judged"))
assert st_blind.get("probe_landed") == _parts == 1, \
    "разбиение probe_landed обязано остаться тождеством: %s" % st_blind
print("UNIT_OK: слепой суд (владелец транзита неизвестен) помечен probe_judged_blind, в "
      "in_band/tip_match НЕ идёт, а тождество probe_landed = сумма пяти классов держится")

print("ПРОШЛИ: боевая форма хэша, владение транзитом по набору ног, слепой суд помечен")
