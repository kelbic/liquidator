"""#110 ДЕНЬГИ И ПРИЁМКА: настоящий `_engine_result` на квитанции ИЗ ИНДЕКСА + счётчики позиций.

ТРИ ВОПРОСА, на которые отвечает файл.

1) ФОРМАТ КВИТАНЦИИ. Сеть отдаёт hex-строки (`batch_receipts` кладёт сырой `result` узла),
   индекс раньше отдавал int. `int('0xd5b6')` в `_engine_result` бросал ValueError, ветка
   глоталась общим `except` и газ МОЛЧА книжился нулём — то есть включение индекса само по
   себе меняло поведение суточного kill-switch (#93). Проверяем НАСТОЯЩИЙ `_engine_result`
   на квитанции, собранной НАСТОЯЩИМ `receipt_like` из настоящего факта индекса, и требуем
   БИТ-В-БИТ то же число, что даёт сетевая квитанция.

2) СЧЁТЧИКИ ПОЗИЦИЙ. Раньше `probe_landed` рос ДО обращения к индексу — при снятом механизме
   тоже, и «в полосе=0» читалось как проигрыш позицией, хотя означало слепоту. Теперь счётчики
   образуют РАЗБИЕНИЕ, и тождество проверяется тестом.

3) ЧУЖОЙ ТРАНЗИТ. Единственный боевой вызов ходил в `transit_of` без agg: в блоке без
   Liquidate метрика №1 считалась против ПОСТОРОННЕГО транзита. Теперь исход «в блоке есть
   транзиты, но не нашего фида» отделён от «транзита нет» и от «блок не покрыт».
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402
import chain.execute as ex                                    # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

AGG_OURS = "0x" + "a1" * 20
AGG_THEIRS = "0x" + "b2" * 20
MID = "0x" + "cd" * 32
BOARD = "0x" + "ab" * 20


class _Cfg:
    liquidator_address = BOARD
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
FACT = {"hash": "0x" + "11" * 32, "block": 49890266, "index": 1084, "tip": 2000000,
        "eff_gas_price": 6188466, "gas_used": 54710, "status": 1, "logs": [],
        "from": "0x" + "ee" * 20, "to": BOARD, "src": "block+receipts", "ts": 0.0}

# ---------------------------------------------------------------- 1. деньги: индекс == сеть
r_idx = ci.receipt_like(FACT)
r_net = {"status": "0x1", "gasUsed": "0xd5b6", "effectiveGasPrice": hex(6188466),
         "logs": [], "blockNumber": hex(49890266), "transactionIndex": hex(1084)}
assert r_idx["gasUsed"] == r_net["gasUsed"] and r_idx["status"] == r_net["status"], (r_idx, r_net)

_saved = (ex.volley_gas_settle, ex.volley_bal_charge, ci._INDEX)
book = []
ex.volley_gas_settle = lambda h, usd: book.append(("settle", h, usd))
ex.volley_bal_charge = lambda a, wei: book.append(("charge", a, wei))
ci._INDEX = None
try:
    got = []
    for tag, r in (("index", r_idx), ("net", r_net)):
        book.clear()
        sf = make_sf()
        sf._engine_result(("0xleg", ("CBBTC", MID, "0xb", 1.0)), "0xdead", r)
        got.append((tag, dict(book[0][1:]) if False else book[0][2], book[1][2]))
    assert got[0][1] == got[1][1] and got[0][2] == got[1][2], \
        ("ОДИН И ТОТ ЖЕ исход обязан дать ОДНИ И ТЕ ЖЕ деньги из обоих источников: %s" % (got,))
    _want_wei = 54710 * 6188466
    assert got[0][2] == _want_wei, ("газ пробы обязан книжиться ФАКТОМ: %s против %d"
                                    % (got[0][2], _want_wei))
    assert abs(got[0][1] - _want_wei * 3000.0 / 1e18) < 1e-15, got
finally:
    ex.volley_gas_settle, ex.volley_bal_charge, ci._INDEX = _saved
print("UNIT_OK: НАСТОЯЩИЙ _engine_result на квитанции ИЗ ИНДЕКСА и на сетевой даёт одно и то же "
      "число (%d wei); прежний int('0xd5b6') ронял ветку молча и книжил ноль" % _want_wei)

# ...и позитивный контроль ошибки: старый разбор на сетевой квитанции даёт НОЛЬ.
try:
    _old = int(r_net.get("gasUsed") or 0) * int(r_net.get("effectiveGasPrice") or 0)
    raise AssertionError("позитивный контроль сломан: int() на hex обязан падать")
except ValueError:
    pass
print("UNIT_OK: позитивный контроль формата — прежний разбор на сетевой квитанции падает "
      "ValueError (то есть дефект был не гипотетический)")


# ---------------------------------------------------------------- 2. счётчики = РАЗБИЕНИЕ
class _Idx:
    """Двойник ТОЛЬКО индекса: сам `_note_probe_position` — настоящий."""

    def __init__(self, report):
        self._report = report

    def transit_report(self, bn, agg=None):
        return self._report(bn, agg)

    def lookup(self, h):
        return {"tip": 2000000}

    def our_position_report(self, bn, agg=None):
        return "ПОЗИЦИИ blk=%s agg=%s" % (bn, agg)


_TR = {"index": 100, "tip": 2000000, "agg": AGG_OURS, "band_ahead": 37, "band_full": 85}
_saved_idx = ci._INDEX
_saved_pm = sfl.SpecFireLive._agg_of_market
try:
    sfl.SpecFireLive._agg_of_market = lambda self, mid: AGG_OURS if mid == MID else None
    cases = [
        (None, lambda bn, agg: ("transit", _TR), "probe_idx_off"),
        (_Idx(lambda bn, agg: ("uncovered", None)), None, "probe_uncovered"),
        (_Idx(lambda bn, agg: ("no_transit", None)), None, "probe_no_transit"),
        (_Idx(lambda bn, agg: ("foreign", None)), None, "probe_foreign_transit"),
        (_Idx(lambda bn, agg: ("transit", _TR)), None, "probe_judged"),
    ]
    sf = make_sf()
    for idx_obj, _unused, key in cases:
        ci._INDEX = idx_obj
        sf._note_probe_position("0xdead", {"blockNumber": hex(101), "transactionIndex": hex(101)},
                                meta=("0xleg", ("CBBTC", MID, "0xb", 1.0)))
        assert sf.stats.get(key), ("исход %s не посчитан: %s" % (key, sf.stats))
    s = sf.stats
    parts = (s.get("probe_idx_off", 0) + s.get("probe_uncovered", 0) + s.get("probe_no_transit", 0)
             + s.get("probe_foreign_transit", 0) + s.get("probe_judged", 0))
    assert s["probe_landed"] == 5 == parts, \
        ("счётчики обязаны быть РАЗБИЕНИЕМ landed: landed=%s сумма=%s (%s)"
         % (s.get("probe_landed"), parts, s))
    assert s.get("probe_in_band") == 1 and s.get("probe_tip_match") == 1, s
    print("UNIT_OK: probe_landed = idx_off + uncovered + no_transit + foreign + judged (5=5); "
          "in_band/tip_match считаются ТОЛЬКО против транзита нашего фида")

    # снятый механизм ВИДЕН: landed == idx_off
    sf2 = make_sf()
    ci._INDEX = None
    for _ in range(3):
        sf2._note_probe_position("0xdead", {"blockNumber": 1, "transactionIndex": 2}, meta=None)
    assert sf2.stats["probe_landed"] == sf2.stats["probe_idx_off"] == 3, sf2.stats
    assert "probe_no_transit" not in sf2.stats, \
        "слепота не имеет права выглядеть как факт «транзита не было»: %s" % sf2.stats
    print("UNIT_OK: механизм СНЯТ — это видно по счётчикам (landed==idx_off), а не читается как "
          "«транзита в блоке не было»")
finally:
    ci._INDEX = _saved_idx
    sfl.SpecFireLive._agg_of_market = _saved_pm

# ---------------------------------------------------------------- 3. чужой транзит на живом индексе
idx = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=lambda *a: {}, now=lambda: 0.0)
idx._seen[7] = True
idx._blocks[7] = {"transit": [{"index": 5, "tip": 111, "agg": AGG_THEIRS, "band_ahead": 3,
                               "band_full": 3}], "winners": [], "ours": {}}
assert idx.transit_report(7, agg=AGG_OURS)[0] == "foreign", idx.transit_report(7, agg=AGG_OURS)
assert idx.transit_report(7, agg=AGG_THEIRS)[0] == "transit"
assert idx.transit_report(7)[0] == "transit", "без agg остаётся прежняя эвристика (для отчётов)"
idx._seen[8] = False
assert idx.transit_report(8)[0] == "uncovered", "лёгкое чтение = слепота, а не «транзита нет»"
idx._seen[9] = True
idx._blocks[9] = {"transit": [], "winners": [], "ours": {}}
assert idx.transit_report(9)[0] == "no_transit"
print("UNIT_OK: четыре исхода транзита различимы на живом индексе (uncovered / no_transit / "
      "foreign / transit) — метрика №1 больше не считается против чужого события")

# ---------------------------------------------------------------- 4. трупы: добой в два блока
idx2 = ci.ChainIndex("https://node.example/K", watch_to=[], http=lambda *a: {}, now=lambda: 1000.0)
key = (MID, "0x" + "b7" * 20)
idx2._last_bn = 100
idx2._ingest(100, {}, full=False, pairs={key: (10, 0)})
idx2._ingest(101, {}, full=False, pairs={key: (25, 3)})
cor = idx2.corpses()
assert cor[key] == (35, 3), ("двухблочный добой обязан СУММИРОВАТЬСЯ (иначе раннее погашение "
                             "теряется и фильтр трупов #108 травится): %s" % (cor,))
idx2._ingest(101 + ci.CHAIN_INDEX_CORPSE_BLOCKS + 1, {}, full=False, pairs={key: (7, 0)})
assert idx2.corpses()[key] == (7, 0), "за окном трупов счёт начинается заново"
print("UNIT_OK: погашения по паре (рынок, заёмщик) СУММИРУЮТСЯ между блоками, а за окном "
      "трупов счёт стартует заново")

# ---------------------------------------------------------------- 5. чужой Liquidate не труп
_lg_ours = {"topics": [ci.MORPHO_LIQUIDATE_TOPIC, "0x" + "11" * 32, "0x" + "22" * 32,
                       "0x" + "33" * 32], "address": ci.MORPHO_BLUE, "data": "0x" + "00" * 160,
            "transactionHash": "0x" + "aa" * 32}
_lg_alien = dict(_lg_ours, address="0x" + "ff" * 20)
p = ci.parse_logs([_lg_ours, _lg_alien])
assert len(p["liquidate"]) == 1 and p["liquidate_foreign"] == 1, p
print("UNIT_OK: Liquidate с ЧУЖОГО контракта отброшен (топик не уникален; чужой лог отравил бы "
      "и вердикт победы, и фильтр трупов), отброшенное посчитано")

# ---------------------------------------------------------------- 6. срок годности «mined»
clk = {"t": 0.0}
idx3 = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=lambda *a: {},
                     now=lambda: clk["t"])
idx3._last_ok = 0.0
idx3._by_hash["0xdead"] = dict(FACT, ts=0.0)
assert idx3.state("0xdead")[0] == "mined"
clk["t"] = ci.CHAIN_INDEX_MINED_TTL_SEC + 1.0
st, why = idx3.state("0xdead")
assert st == "unknown" and "протух" in why, (st, why)
print("UNIT_OK: у статуса «mined» есть срок годности — вечного факта о посадке нет (реорг)")

# ---------------------------------------------------------------- 7. перечитывание блока
idx4 = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=lambda *a: {}, now=lambda: 0.0)
idx4._ingest(10, {"ours": {"0xzz": {"hash": "0xzz", "block": 10, "index": 1}}, "pairs": {}}, full=True)
assert idx4.lookup("0xzz") is not None
idx4._ingest(10, {"ours": {}, "pairs": {}}, full=True)         # тот же номер, нашей tx там нет
assert idx4.lookup("0xzz") is None and idx4.stats["reingest"] == 1, idx4.stats
print("UNIT_OK: перечитывание того же номера ВЫЧИЩАЕТ прежние факты блока — «mined» не "
      "переживает исчезновение своей tx из канона")

# ---------------------------------------------------------------- 8. прогрев O(1), свой лок
idx5 = ci.ChainIndex("https://node.example/K", watch_to=[], http=lambda *a: {}, now=lambda: 0.0)
idx5._lock.acquire()                       # общий лок ЗАНЯТ разбором блока
try:
    idx5.note_hash("0x" + "5" * 64)        # горячий путь отправки не имеет права здесь ждать
    assert idx5._want_full() is True
finally:
    idx5._lock.release()
print("UNIT_OK: note_hash с горячего пути не берёт лок разбора блока, а гейт прогрева — O(1) "
      "(прежде это был обход словаря заявок под общим локом)")

# ---------------------------------------------------------------- 9. приёмочные строки собираются
# Строки приёмки (а) и (в) живут в hot_loop и в самом индексе. Формат-строка, читающая ключ,
# которого нет в snapshot(), упала бы ТОЛЬКО в бою и ТОЛЬКО раз в 120с — поэтому ключи
# проверяются здесь, а не «когда-нибудь увидим в журнале».
import re as _re                                             # noqa: E402
import chain.fire_engine as _fe                              # noqa: E402

_snap = ci.ChainIndex("https://n/K", watch_to=[], http=lambda *a: {}, now=lambda: 1.0).snapshot()
_src = pathlib.Path("chain/hotpath.py").read_text()
_blk = _src[_src.index("CHAIN-INDEX: блоков=%d"):]
_blk = _blk[:_blk.index("except Exception")]
for _k in set(_re.findall(r'_s\["(\w+)"\]', _blk)):
    assert _k in _snap, ("hot-stats читает ключ snapshot(), которого нет: %s" % _k)
_c9 = _fe.collector_stats()
_src9 = _src[_src.index("#109 квитанции:"):]
_src9 = _src9[:_src9.index("except Exception")]
for _k in set(_re.findall(r'_c9\["(\w+)"\]', _src9)):
    assert _k in _c9, ("суточная строка #109 читает ключ, которого нет в collector_stats(): %s" % _k)
assert set(("polls", "polls_saved", "index_hits")) <= set(_c9), _c9
print("UNIT_OK: приёмочные строки (а) hot-stats и (в) суточная #109 читают ТОЛЬКО существующие "
      "ключи snapshot()/collector_stats() — формат не упадёт в бою через 120с")

print("ПРОШЛИ: деньги из индекса == деньги из сети, счётчики приёмки читаемы")
