"""Р1/#108: ФОРМА КЛЮЧА пары (рынок, заёмщик) — фильтр трупов против «нуля по построению».

ЗАЧЕМ ОТДЕЛЬНАЯ ДРЕЛЬ. У заёмщика в этом процессе ДВА источника и ДВЕ формы:
  * Morpho API (chain/morpho.py:157, `user.address`) — адрес в КОНТРОЛЬНОМ регистре (checksum),
    и именно он попадает в shared.groups, а оттуда в горячий путь;
  * лог цепи (chain/chain_index.py, `_topic_hex`) — всегда НИЖНИЙ регистр с префиксом.
Боевой код уже платил за эту развилку: сравнения заёмщика с он-чейн формой в chain/execute.py
(2698, 2701, 2941, 3397) стоят с явным `.lower()`, а канон `hx()` в индексе описан целой
статьёй — там несимметричная нормализация давала множества, которые НЕ ПЕРЕСЕКАЛИСЬ НИКОГДА.

ПОЧЕМУ ЭТОГО НЕ ЛОВИТ ОСНОВНАЯ ДРЕЛЬ. test_cascade_tail_parallel.py кормит горячий путь
ЛОВЕРКЕЙСНЫМИ заёмщиками — на них фильтр зелен и при разъехавшихся формах. То есть это ровно
«тест, который может вернуть только ноль»: он проверяет не тот объект. Здесь горячий путь
держит checksum-форму, индекс — форму цепи, и совпасть они обязаны.
"""
import sys
import threading
import time
import types

import chain.hotpath as hp

MID_LC = "0x" + "ab" * 32                      # рынок в форме цепи (нижний регистр)
MID_HOT = "0xAB" + "ab" * 31                   # тот же рынок, как его мог принести конфиг/API
# один адрес в двух законных формах: нижняя (лог цепи) и checksum (её отдаёт Morpho API)
B_LC2 = "0xdeadbeef00000000000000000000000000001234"
B_HOT2 = "0xDeadBeEf00000000000000000000000000001234"
AGG = "0x" + "11" * 20
ORACLE = "0x" + "22" * 20
LLTV = 860000000000000000

_FAILS = []


def _check(cond, msg):
    print(("  OK: " if cond else "  ПРОВАЛ: ") + msg)
    if not cond:
        _FAILS.append(msg)


def _log():
    import logging
    lg = logging.getLogger("test-corpse-keyform")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


class _FakeIndex:
    def __init__(self, pairs):
        self._pairs = pairs

    def corpses(self, max_age_blocks=None):
        return dict(self._pairs)


def _run(hot_mid, hot_borrowers, index_pairs, block):
    prepped = []
    shared = types.SimpleNamespace(lock=threading.Lock(),
                                   groups={hot_mid: list(hot_borrowers)},
                                   debt_by={(hot_mid, b): 5000.0 for b in hot_borrowers},
                                   debt_assets_by={})
    store = types.SimpleNamespace(realized_today=lambda: {"net": 0.0, "gas": 0.0},
                                  log_action=lambda **kw: None)
    from strategy.guard import KillSwitch
    guard = types.SimpleNamespace(blocked_reason=lambda st: None,
                                  is_transient=KillSwitch.is_transient)
    cfg = types.SimpleNamespace(mode="live", hot_min_repaid_usd=2000.0, fast_lane=False,
                                positions_from_cache=False, max_inflight=8)

    def reads_fn(rpc, mid, bs_, preconf_rpc=None, src_out=None):
        if src_out is not None:
            src_out["src"] = "test"
        return (10**6, 10**6, [(b, 10**6, 10**6) for b in bs_])

    def prepare_fn(rpc, preconf_rpc, cfg_, mid, b, du, debt_assets, price, state=None):
        prepped.append(b)
        return {"ok": True, "calldata": "0x", "net_usd": 5.0}

    def dispatch_fn(rpc, cfg_, ready, log=None, max_inflight=None):
        return [{"market_id": m, "borrower": b, "sent": True, "status": 1, "net_usd": 5.0,
                 "hash": "0xt", "gas_usd": 0.1} for m, b, _ in ready]

    _get = hp._chain_index.get_index
    fi = _FakeIndex(index_pairs)
    hp._chain_index.get_index = lambda: fi
    try:
        hp._process_transmit(AGG, block, 0, time.time(),
                             rpc=None, preconf_rpc=None, cfg=cfg, feeds={AGG: [hot_mid]},
                             meta={hot_mid: (ORACLE, LLTV)}, shared=shared, store=store,
                             guard=guard, alerter=types.SimpleNamespace(send_async=lambda *a, **k: None),
                             log=_log(), reads_fn=reads_fn, prepare_fn=prepare_fn,
                             dispatch_fn=dispatch_fn, px_hint=12345, hint_oracle=ORACLE)
    finally:
        hp._chain_index.get_index = _get
    return prepped


_orig_flips = hp._flips
hp._flips = lambda price, lltv, tba, tbs, positions: [
    (b, types.SimpleNamespace(hf=0.98), du) for (b, bs, col, du) in positions]
try:
    # ПОЗИТИВНЫЙ КОНТРОЛЬ: формы совпадают -> фильтр обязан сработать (иначе дрель проверяет
    # не механизм, а собственную опечатку)
    print("0. Позитивный контроль: обе формы нижние")
    got = _run(MID_LC, [B_LC2], {(MID_LC, B_LC2): (10**6, 0)}, 400)
    _check(got == [], "при совпадающих формах труп срезан (готовили %s)" % got)

    # ОСНОВНОЙ СЛУЧАЙ: горячий путь держит checksum-адрес (как отдаёт Morpho API),
    # индекс — форму цепи
    print("1. Горячий путь: checksum-адрес; индекс: форма цепи")
    got = _run(MID_LC, [B_HOT2], {(MID_LC, B_LC2): (10**6, 0)}, 401)
    _check(got == [], "труп срезан НЕЗАВИСИМО от регистра адреса (готовили %s)" % got)

    print("2. Плюс верхний регистр в market_id")
    got = _run(MID_HOT, [B_HOT2], {(MID_LC, B_LC2): (10**6, 0)}, 402)
    _check(got == [], "труп срезан НЕЗАВИСИМО от регистра market_id (готовили %s)" % got)

    print("3. Обратная развилка: индекс отдал форму с верхним регистром")
    got = _run(MID_LC, [B_LC2], {(MID_LC, B_HOT2): (10**6, 0)}, 403)
    _check(got == [], "нормализация симметрична — обе стороны, а не одна (готовили %s)" % got)

    print("4. НЕГАТИВНЫЙ контроль: другой заёмщик трупом не становится")
    other = "0xcafebabe00000000000000000000000000009999"
    got = _run(MID_LC, [other], {(MID_LC, B_LC2): (10**6, 0)}, 404)
    _check(got == [other], "чужая пара цель не гасит (готовили %s)" % got)
finally:
    hp._flips = _orig_flips

if _FAILS:
    print("\nКРАСНАЯ: провалов %d" % len(_FAILS))
    for m in _FAILS:
        print("  - %s" % m)
    sys.exit(1)
print("\nПРОШЛИ: форма ключа пары не разъезжается между горячим путём и индексом")
