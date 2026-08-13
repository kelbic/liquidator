"""#110/#109: коллектор спрашивает ИНДЕКС раньше сети, а «нет квитанции» перестаёт быть приговором.

ЧТО ЗАКРЫВАЕТСЯ. За сутки 12-13.08 измерительный тракт дал 889 «FIRE collect batch fail» и 169
COMBAT-FAILOVER, а каждая проверенная проба с меткой «NO-RECEIPT (nonce инвалидирован)» на самом
деле была ЗАМАЙНЕНА (j2 — блок 49820480, j1 — 49805074). Ценой лжи был сброс склада ЖИВОЙ ноги
(40 пред-подписанных проб + 28-32с пересборки флота) ровно в каскадные секунды: бот сносил
собственную боевую модель по показаниям слепого узла.

ШВЫ. Подменяются только два внешних стыка: `receipts_fn` (сеть квитанций) и содержимое индекса
(через настоящий `ChainIndex`, накормленный записанным ответом узла в `test_chain_index.py`;
здесь — облегчённый двойник с тем же контрактом `receipts_for/state`). Настоящими исполняются
`ReceiptCollector.tick` целиком и настоящий `SpecFireLive._engine_timeout`.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.fire_engine as fe                                # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

H = ["0x" + "%064x" % i for i in range(6)]


def rcpt(i, gas=54710):
    return {"status": 1, "gasUsed": gas, "effectiveGasPrice": 8088466, "logs": [],
            "blockNumber": 49890266, "transactionIndex": 1000 + i}


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- 1. индекс закрывает партию
net = []
clk = Clock()


def _net(hs):
    net.append(list(hs))
    return {h: None for h in hs}


col = fe.ReceiptCollector(receipts_fn=_net, index_fn=lambda hs: {h: rcpt(0) for h in hs},
                          on_result=lambda m, h, r: got.append(h), on_timeout=None,
                          now=clk, min_age_sec=0.0)
got = []
for h in H[:4]:
    col.track(h, ("0xleg", None))
n = col.tick()
assert n == 4 and len(got) == 4, (n, got)
assert net == [], "партия закрыта индексом — в сеть идти НЕ за чем: %s" % net
assert col.stats["index_hits"] == 4 and col.stats["polls"] == 0 and col.stats["polls_saved"] == 1
assert col.stats["cu"] == 0.0, "нулевая сеть = нулевой CU; иначе счётчик врёт в нашу пользу"
print("UNIT_OK: индекс закрыл партию целиком — 0 запросов, 0 CU вместо 4 подзапросов")

# ---------------------------------------------------------------- 2. остаток честно уходит в сеть
net.clear()
got.clear()
col2 = fe.ReceiptCollector(receipts_fn=_net,
                           index_fn=lambda hs: {hs[0]: rcpt(1)},      # знает ОДИН из трёх
                           on_result=lambda m, h, r: got.append(h), on_timeout=None,
                           now=Clock(), min_age_sec=0.0)
for h in H[:3]:
    col2.track(h, ("0xleg", None))
col2.tick()
assert net and sorted(net[0]) == sorted(H[1:3]), \
    "в сеть обязан уйти ТОЛЬКО остаток (индекс не хоронит незнакомцев): %s" % net
assert col2.stats["cu"] == 2 * fe.CU_PER_RECEIPT, col2.stats
assert got == [H[0]], got
print("UNIT_OK: знает индекс — берём оттуда, не знает — спрашиваем сеть; CU считается по "
      "ФАКТИЧЕСКИ отправленным подзапросам")

# ---------------------------------------------------------------- 3. отказ индекса = прежний путь
net.clear()


def _boom(hs):
    raise RuntimeError("индекс лёг")


col3 = fe.ReceiptCollector(receipts_fn=_net, index_fn=_boom,
                           on_result=lambda m, h, r: None, on_timeout=None,
                           now=Clock(), min_age_sec=0.0)
for h in H[:3]:
    col3.track(h, ("0xleg", None))
col3.tick()
assert sorted(net[0]) == sorted(H[:3]), "упавший индекс не смеет отнять у нас сеть: %s" % net
assert col3.stats["index_fail"] == 1
print("UNIT_OK: индекс упал — партия целиком уходит прежним путём (fail-open, счётчик растёт)")

# ---------------------------------------------------------------- 4. ОТКАТ МЕХАНИЗМА
net.clear()
col4 = fe.ReceiptCollector(receipts_fn=_net, index_fn=None,           # ручка отката
                           on_result=lambda m, h, r: None, on_timeout=None,
                           now=Clock(), min_age_sec=0.0)
for h in H[:3]:
    col4.track(h, ("0xleg", None))
col4.tick()
# Счётчики механизма ЗАВЕДЕНЫ ВСЕГДА и стоят на нуле: приёмка обязана отличать «механизм снят»
# от «механизм есть и молчит», а отсутствующий ключ читается и так, и так (блокер приёмки #3).
assert sorted(net[0]) == sorted(H[:3]), net
assert col4.stats["index_hits"] == 0 and col4.stats["polls_saved"] == 0 and col4.stats["polls"] == 1, \
    "index_fn=None обязан давать путь БИТ-В-БИТ прежний: %s / %s" % (net, col4.stats)
print("UNIT_OK: SPEC_FIRE_COLLECTOR_INDEX=0 (index_fn=None) — прежний путь, а счётчики "
      "механизма честно стоят на нуле (снят, а не «молчит»)")

# ---------------------------------------------------------------- 5. NO-RECEIPT против индекса
class _Idx:
    """Двойник ИНДЕКСА с настоящим контрактом трёх исходов (см. chain/chain_index.py:state)."""

    def __init__(self, verdict, fact=None):
        self.verdict, self.fact = verdict, fact

    def state(self, h, sent_ts=None):
        return (self.verdict, self.fact)

    def transit_of(self, bn, agg=None):
        return (1083, 2000000, 37)

    def lookup(self, h):
        return {"tip": 1100000}

    def our_position_report(self, bn):
        return "ПОЗИЦИИ blk=%s: …" % bn


class _Cfg:
    liquidator_address = "0x" + "ab" * 20
    eth_price_usd = 3000.0


def make_sf():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf.cfg = _Cfg()
    sf.rpc = None
    sf.stats = {}
    sf._log = lambda m: LOG.append(m)
    sf._now = lambda: 0.0
    return sf


LOG = []
import chain.chain_index as ci                                 # noqa: E402
import chain.execute as ex                                     # noqa: E402

_saved = (ci._INDEX, ex.volley_invalidate_nonce, ex.invalidate_ladder, ex.volley_gas_settle,
          ex.volley_leg_landed)
CALLS = []
ex.volley_invalidate_nonce = lambda a: CALLS.append(("nonce", a))
ex.invalidate_ladder = lambda a: CALLS.append(("ladder", a))
ex.volley_gas_settle = lambda h, u: CALLS.append(("settle", h, u))
ex.volley_leg_landed = lambda rpc, a: False        # цепь «не подтверждает» — как в бою под 429
try:
    # 5a. индекс ВИДЕЛ пробу в блоке -> склад ноги обязан УЦЕЛЕТЬ
    ci._INDEX = _Idx("mined", {"block": 49890266, "index": 1146, "tip": 1100000,
                               "gas_used": 54710, "eff_gas_price": 6188466})
    sf = make_sf()
    _gas_added = []
    _sv_add, _sv_charge = ex.volley_gas_add, ex.volley_bal_charge
    ex.volley_gas_add = lambda usd: _gas_added.append(usd)
    ex.volley_bal_charge = lambda a, w: _gas_added.append(("wei", w))
    try:
        sf._engine_timeout(("0xleg1", None), H[0])
    finally:
        ex.volley_gas_add, ex.volley_bal_charge = _sv_add, _sv_charge
    assert ("nonce", "0xleg1") not in CALLS and ("ladder", "0xleg1") not in CALLS, CALLS
    assert sf.stats.get("noreceipt_false") == 1, sf.stats
    assert _gas_added and abs(_gas_added[0] - 54710 * 6188466 * 3000.0 / 1e18) < 1e-12, _gas_added
    assert ("wei", 54710 * 6188466) in _gas_added, _gas_added
    assert sf.stats.get("noreceipt_gas_booked") == 1, \
        "tx села => газ ПОТРАЧЕН; прежний путь записывал ноль и недосчитывал сутки"
    assert any("ИНДЕКС ОПРОВЕРГ" in m for m in LOG), LOG
    assert sf.stats.get("probe_landed") == 1, "позиция подтверждённой пробы обязана считаться"
    print("UNIT_OK: NO-RECEIPT + индекс видит tx в блоке -> склад ноги СОХРАНЁН, счётчик лжи "
          "коллектора растёт (это и есть метрика «цели, потерянные своей инфраструктурой»)")

    # 5b. индекс сплошь покрыл окно и tx там НЕТ -> приговор обоснован
    CALLS.clear()
    ci._INDEX = _Idx("absent", None)
    sf2 = make_sf()
    sf2._engine_timeout(("0xleg2", None), H[1])
    assert ("nonce", "0xleg2") in CALLS and ("ladder", "0xleg2") in CALLS, CALLS
    assert sf2.stats.get("noreceipt_dropped") == 1
    assert sf2.stats.get("noreceipt_dropped_confirmed") == 1, sf2.stats
    print("UNIT_OK: индекс подтверждает отсутствие -> сброс склада обоснован ФАКТОМ и помечен "
          "как подтверждённый (честный знаменатель потерь)")

    # 5c. индекс НЕ ЗНАЕТ -> ведём себя ровно как до пакета
    CALLS.clear()
    ci._INDEX = _Idx("unknown", None)
    sf3 = make_sf()
    sf3._engine_timeout(("0xleg3", None), H[2])
    assert ("nonce", "0xleg3") in CALLS, CALLS
    assert sf3.stats.get("noreceipt_dropped") == 1
    assert "noreceipt_dropped_confirmed" not in sf3.stats, \
        "незнание НЕ имеет права выдавать себя за подтверждение: %s" % sf3.stats
    print("UNIT_OK: индекс не знает -> прежнее поведение, но БЕЗ отметки «подтверждено» — "
          "незнание не притворяется фактом")

    # 5d. индекса нет вовсе (откат/не поднялся)
    CALLS.clear()
    ci._INDEX = None
    sf4 = make_sf()
    sf4._engine_timeout(("0xleg4", None), H[3])
    assert ("nonce", "0xleg4") in CALLS and sf4.stats.get("noreceipt_dropped") == 1
    assert "noreceipt_false" not in sf4.stats
    print("UNIT_OK: индекса нет — путь прежний бит-в-бит")
finally:
    (ci._INDEX, ex.volley_invalidate_nonce, ex.invalidate_ladder, ex.volley_gas_settle,
     ex.volley_leg_landed) = _saved

# ---------------------------------------------------------------- 6. шов боевого метода
sf5 = make_sf()
sf5.stats = {}
_sv = sfl.SPEC_FIRE_COLLECTOR_INDEX
try:
    sfl.SPEC_FIRE_COLLECTOR_INDEX = False
    assert sf5._index_fn() is None, "ручка отката обязана снимать МЕХАНИЗМ (шов = None)"
    sfl.SPEC_FIRE_COLLECTOR_INDEX = True
    fn = sf5._index_fn()
    assert callable(fn) and fn(H[:2]) == {}, "индекс не поднят -> пусто, но БЕЗ исключения"
finally:
    sfl.SPEC_FIRE_COLLECTOR_INDEX = _sv
print("UNIT_OK: настоящий боевой шов SpecFireLive._index_fn — ручка отката снимает механизм")

# ---------------------------------------------------------------- 7. СКВОЗНОЙ прогон без двойников
# Настоящий ChainIndex на ЗАПИСАННОМ ответе узла + настоящий ReceiptCollector. Между ними —
# ничего подменённого: ни один шов не стоит выше проверяемого дефекта.
import gzip                                                    # noqa: E402
import json                                                    # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RCPTS = json.loads((FIX / "receipts_49890266.json").read_text())
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"


def _http(url, payload, timeout):
    m = payload["method"]
    if m == "eth_getLogs":
        return {"result": LOGS}
    if m == "eth_getBlockByNumber":
        return BLOCK
    if m == "eth_getBlockReceipts":
        return {"result": RCPTS}
    raise AssertionError(m)


real_idx = ci.ChainIndex("https://node.example/KEY", watch_to=[BOARD], http=_http,
                         now=Clock(), log_fn=lambda m: None)
real_idx.scan_block(49890266)
_have = [rc["transactionHash"].lower() for rc in RCPTS
         if rc["to"].lower() == BOARD]                          # две наши пробы с квитанцией
_miss = [h for h in real_idx.block_facts(49890266)["ours"] if h not in _have][:2]
assert len(_have) == 2 and len(_miss) == 2

_saved_idx = ci._INDEX
ci._INDEX = real_idx
try:
    sf6 = make_sf()
    sf6.stats = {}
    fn = sf6._index_fn()
    net.clear()
    seen = []
    col7 = fe.ReceiptCollector(receipts_fn=_net, index_fn=fn,
                               on_result=lambda m, h, r: seen.append((h, r["gasUsed"],
                                                                      r["transactionIndex"])),
                               on_timeout=None, now=Clock(), min_age_sec=0.0)
    for h in _have + _miss:
        col7.track(h, ("0xleg", None))
    col7.tick()
    # Форма квитанции — ТА ЖЕ, что у сети (hex): различие форматов уже стоило нам молчаливого
    # ValueError в денежном пути, поэтому проверяем именно её, а не «числа».
    assert len(seen) == 2 and all(g == "0xd5b6" for _h, g, _i in seen), seen
    assert all(ci.rcpt_int(g) == 54710 for _h, g, _i in seen), seen
    assert sorted(ci.rcpt_int(i) for _h, _g, i in seen) == [17, 163], seen
    assert sorted(net[0]) == sorted(_miss), \
        "в сеть ушли ровно те, кого индекс не знает: %s" % net
    assert sf6.stats.get("index_receipts") == 2, sf6.stats
finally:
    ci._INDEX = _saved_idx
print("UNIT_OK: СКВОЗЬ — настоящий индекс на настоящем блоке 49890266 отдал настоящему "
      "коллектору исходы двух проб (gas 54710, позиции 17 и 163), сеть спросили только про "
      "остальные; заглушек между ними НЕТ")

print("ПРОШЛИ: коллектор спрашивает индекс первым, NO-RECEIPT больше не приговор")
