"""КАРТА ВЛАДЕНИЯ ТРАНЗИТОМ НЕ СТИРАЕТСЯ ШТОРМОМ (#110, ревью р-3, условие 4).

ДЕФЕКТ. `resolve_feeds` звал `_LEGS_AGG.clear()` / `_PRIMARY_AGG.clear()` ДО того, как узнавал,
что новый резолв непуст. А `_resolve_legs` по своему докстрингу ГЛОТАЕТ транспорт и отдаёт
(None, []) молча — то есть шторм 429/408 на ОБОИХ эндпоинтах обнулял карту владения целиком,
`_agg_of_market` начинал отдавать None, и КАЖДАЯ проба судилась ВСЛЕПУЮ: денежные
probe_in_band / probe_tip_match становились структурно нулями. При rescan_interval_sec=21600
это до 6 ЧАСОВ слепоты, причём карта фидов hot_loop в это время выглядит здоровой — у неё
СВОЙ гард («re-resolve пуст — держу старую карту»), а модульную карту не берёг никто. Шторм
не гипотеза: ради него гард hot_loop и написан (инцидент 07-23 «hot path: 0 aggregators /
40 markets»), и по времени он совпадает с каскадом, то есть с жирным окном.

ЧТО НАСТОЯЩЕЕ: `feeds.resolve_feeds`, `feeds.legs_map/primary_map/map_age`,
`SpecFireLive._agg_of_market`, `SpecFireLive._note_probe_position`, `ChainIndex.transit_report`
на записанном блоке 49890266. Подменён РОВНО ОДИН шов — `_resolve_legs` (тот же, что
подменяет штатный `test_feed_pattern.py`), потому что это и есть I/O-шов резолва.
"""
import gzip
import json
import logging
import pathlib
import sys
import threading
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402
import chain.feeds as feeds                                   # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
BN = 49890266
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
AGG_PRIZE = "0x9491aedfe0da70eb110582b66b2edace28d82a5d"
AGG_LEG2 = "0x" + "c3" * 20
MID_A = "0x" + "11" * 32
MID_B = "0x" + "44" * 32
BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS_FX = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RECEIPTS = json.loads((FIX / "receipts_49890266.json").read_text())
H_FIRE = next(rc["transactionHash"] for rc in RECEIPTS if rc["to"].lower() == BOARD)[2:]
MKT = lambda mid: types.SimpleNamespace(market_id=mid)         # noqa: E731
LOGGER = logging.getLogger("liquidator")


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


def _http(url, payload, timeout):
    m = payload["method"]
    if m == "eth_getLogs":
        return {"result": LOGS_FX}
    if m == "eth_getBlockByNumber":
        return BLOCK
    if m == "eth_getBlockReceipts":
        return {"result": RECEIPTS}
    if m == "eth_blockNumber":
        return {"result": hex(BN)}
    raise AssertionError(m)


idx = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=_http, now=lambda: 0.0,
                    log_fn=lambda m: None)
idx.note_hash(H_FIRE, head=BN - 1)
idx.scan_block(BN)
_h, _f = sorted(idx.block_facts(BN)["ours"].items(), key=lambda kv: kv[1]["index"])[0]
RCPT = ci.receipt_like(_f)


class _Cfg:
    liquidator_address = BOARD
    eth_price_usd = 3000.0


def make_sf():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf.cfg, sf.rpc, sf.stats = _Cfg(), None, {}
    sf._probe_by_key, sf._win_key = {}, {}          # учёт ПО КЛЮЧУ ПЛАНА (р-6, блокер C)
    sf._log = lambda m: None
    sf._now = lambda: 0.0
    return sf


def judge(sf, mid):
    """Один боевой замер позиции по рынку mid -> дельта счётчиков окна."""
    w = sf._probe_snap()
    sf._note_probe_position(_h, RCPT, meta=("0xleg", ("CBBTC", mid, "0xb", 1.0)))
    return sf._probe_delta(w)


_sv_legs, _sv_idx = feeds._resolve_legs, ci._INDEX
_sv_maps = (dict(feeds._LEGS_AGG), dict(feeds._PRIMARY_AGG))
cap = Cap()
cap.setLevel(logging.DEBUG)
LOGGER.addHandler(cap)
ci._INDEX = idx
try:
    feeds._LEGS_AGG.clear()
    feeds._PRIMARY_AGG.clear()
    # ------------------------------------------------ 1. удачный резолв: владелец известен
    feeds._resolve_legs = lambda rpc, mid, fb=None: (AGG_PRIZE, [AGG_PRIZE, AGG_LEG2])
    feeds.resolve_feeds(None, [MKT(MID_A), MKT(MID_B)])
    assert len(feeds.legs_map()) == 2 and len(feeds.primary_map()) == 2, feeds.legs_map()
    sf = make_sf()
    d_ok = judge(sf, MID_A)
    assert d_ok["probe_no_agg"] == 0 and d_ok["probe_judged"] == 1, d_ok
    print("UNIT_OK: карта построена (2 рынка), суд ЗНАЯ владельца: %s" % d_ok)

    # ------------------------------------------------ 2. ШТОРМ: оба эндпоинта отдали пусто
    cap.msgs.clear()
    feeds._resolve_legs = lambda rpc, mid, fb=None: (None, [])
    feeds.resolve_feeds(None, [MKT(MID_A), MKT(MID_B)])
    assert len(feeds.legs_map()) == 2, \
        ("штормовой резолв СТЁР живую карту владения — до 6ч слепоты при внешне здоровой "
         "карте фидов: %s" % feeds.legs_map())
    assert len(feeds.primary_map()) == 2, "запасной ключ владения стёрт: %s" % feeds.primary_map()
    assert any("КАРТА ВЛАДЕНИЯ не обновлена" in m for m in cap.msgs), \
        "молчаливое сохранение старой карты — тот же мёртвый сторож: %s" % cap.msgs
    d_storm = judge(sf, MID_A)
    assert d_storm["probe_no_agg"] == 0 and d_storm["probe_judged"] == 1, \
        ("после шторма проба обязана судиться ЗНАЯ владельца (иначе денежные счётчики "
         "структурно нули): %s" % d_storm)
    print("UNIT_OK: шторм НЕ стёр карту (2 рынка живы, возраст %.0fс), в журнале названа "
          "причина, суд остался зрячим: %s" % (feeds.map_age(), d_storm))

    # ------------------------------------------------ 3. ЧАСТИЧНЫЙ шторм
    feeds._resolve_legs = lambda rpc, mid, fb=None: (
        (AGG_PRIZE, [AGG_PRIZE]) if mid == MID_A else (None, []))
    feeds.resolve_feeds(None, [MKT(MID_A), MKT(MID_B)])
    assert feeds.legs_map().get(MID_A) == [AGG_PRIZE], feeds.legs_map()
    assert feeds.legs_map().get(MID_B) == [AGG_PRIZE, AGG_LEG2], \
        ("частичный шторм не имеет права уносить рынки, которые резолвнулись РАНЬШЕ: %s"
         % feeds.legs_map())
    print("UNIT_OK: частичный шторм — обновлён только резолвнувшийся рынок, второй сохранил "
          "прежние ноги")

    # --------------------------- 4. ПОЗИТИВНЫЙ КОНТРОЛЬ: слепота ВСЁ ЕЩЁ достижима и видна
    d_blind = judge(sf, "0x" + "99" * 32)          # рынка в карте нет вовсе
    assert d_blind["probe_no_agg"] == 1 and d_blind["probe_judged_blind"] == 1, \
        ("гард не имеет права ВЫДУМЫВАТЬ владельца: незнакомый рынок обязан остаться слепым "
         "судом: %s" % d_blind)
    print("UNIT_OK: позитивный контроль — незнакомый рынок по-прежнему судится ВСЛЕПУЮ (%s), "
          "то есть гард не подменил незнание знанием" % d_blind)
finally:
    feeds._resolve_legs, ci._INDEX = _sv_legs, _sv_idx
    feeds._LEGS_AGG.clear()
    feeds._LEGS_AGG.update(_sv_maps[0])
    feeds._PRIMARY_AGG.clear()
    feeds._PRIMARY_AGG.update(_sv_maps[1])
    LOGGER.removeHandler(cap)

print("ПРОШЛИ: пустой штормовой резолв больше не стирает карту владения транзитом")
