"""Р1: ХВОСТ КАСКАДА. Дрель на пропускную способность подготовки целей.

ЗАЧЕМ. Ликвидации на Base приходят каскадами (11.08 06:38: 30 ликвидаций в блоках
49820471/49820472). Голову забирает «стена» (пул >=1133 EOA, 712-796 tx в блок на tip
транзита) — с ней не конкурируем. Хвост (2-я..N-я цель) берёт «объёмщик» мелкими деньгами
(43 события за 10 дней) — это единственная доказанно живая ниша без стены.

ЧЕМ МЫ ЕЁ НЕ БЕРЁМ. Цикл подготовки целей в `_process_transmit` шёл ПОСЛЕДОВАТЕЛЬНО, а цена
одной подготовки — 632мс и она СЕТЕВАЯ (eth_call + Kyber-квота; замер STATE §12.08 ночь-3),
не CPU. Каскад живёт 2-4с => успевали ~6 целей из 26, хвост физически недостижим.

ЧТО МЕРЯЕТ ДРЕЛЬ. Реальный `hp._process_transmit` (шов взят на уровне prepare_fn/reads_fn —
НАСТОЯЩИЙ цикл подготовки исполняется, а не его копия), 26 flipped-целей приходят разом,
каждая подготовка = 0.632с СЕТЕВОГО сна (не жжёт CPU), бюджет каскада 4.0с wall-clock.
Считаем, сколько целей дошли до `ready` внутри бюджета.

ПОРОГ. >=15 из 26. На последовательном коде это ~6 (4с/0.632с) — дрель обязана быть КРАСНОЙ
до правки. Дрель НЕ ТРОГАЕТСЯ после фикса.

Дополнительно заперты три вещи, без которых параллелизм был бы вреден:
  Б. откат REACTIVE_PREP_THREADS=1 снимает МЕХАНИЗМ (порядок подготовок = порядок целей,
     пропускная способность возвращается к последовательной) — не параметр, а механизм;
  В. пыль (du < floor) не доходит до дорогой подготовки ВООБЩЕ;
  Г. фильтр трупов (#108: 30 из 43 «healthy-race» были трупами) режет цель ДО подготовки,
     но НЕЗНАНИЕ (индекса нет / индекс протух) огонь НЕ гасит.
"""
import sys
import threading
import time
import types

import chain.hotpath as hp

MID = "0x" + "ab" * 32
AGG = "0x" + "11" * 20
ORACLE = "0x" + "22" * 20
LLTV = 860000000000000000

PREP_COST_SEC = 0.632          # ЗАМЕР STATE §12.08 ночь-3: сеть (eth_call + Kyber), не CPU
CASCADE_BUDGET_SEC = 4.0       # каскад 11.08 жил 2-4с
N_TARGETS = 26                 # целей во flipped-наборе каскада
WANT_IN_BUDGET = 15            # порог приёмки

_FAILS = []


def _check(cond, msg):
    if cond:
        print("  OK: %s" % msg)
    else:
        print("  ПРОВАЛ: %s" % msg)
        _FAILS.append(msg)


def _log():
    import logging
    lg = logging.getLogger("test-cascade-tail")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


def _mk_env(*, borrowers, debt_by, prep_sleep, rec):
    """Все зависимости _process_transmit. rec — общий журнал (порядок + времена подготовок)."""
    shared = types.SimpleNamespace(
        lock=threading.Lock(),
        groups={MID: list(borrowers)},
        debt_by={(MID, b): debt_by[b] for b in borrowers},
        debt_assets_by={},
    )
    store = types.SimpleNamespace(realized_today=lambda: {"net": 0.0, "gas": 0.0},
                                  log_action=lambda **kw: None)
    from strategy.guard import KillSwitch
    guard = types.SimpleNamespace(blocked_reason=lambda st: None,
                                  is_transient=KillSwitch.is_transient)
    alerter = types.SimpleNamespace(send_async=lambda *a, **k: None)
    cfg = types.SimpleNamespace(mode="live", hot_min_repaid_usd=2000.0, fast_lane=False,
                                positions_from_cache=False, max_inflight=64)

    def reads_fn(rpc, mid, bs_, preconf_rpc=None, src_out=None):
        if src_out is not None:
            src_out["src"] = "test"
        # borrow_shares = 10**6 у всех; труп задаётся через фейковый индекс
        return (10**6, 10**6, [(b, 10**6, 10**6) for b in bs_])

    def prepare_fn(rpc, preconf_rpc, cfg_, mid, b, du, debt_assets, price, state=None):
        t_in = time.perf_counter()
        time.sleep(prep_sleep)                       # СЕТЕВОЕ ожидание: сокет, не CPU
        with rec["lock"]:
            rec["order"].append(b)
            rec["done"].append((b, time.perf_counter() - rec["t0"], t_in - rec["t0"]))
        return {"ok": True, "calldata": "0x", "net_usd": 5.0}

    def dispatch_fn(rpc, cfg_, ready, log=None, max_inflight=None):
        with rec["lock"]:
            for _m, _b, _p in ready:
                rec["dispatched"].append(_b)
        return [{"market_id": m, "borrower": b, "sent": True, "status": 1, "net_usd": 5.0,
                 "hash": "0xt", "gas_usd": 0.1} for m, b, _ in ready]

    return dict(rpc=None, preconf_rpc=None, cfg=cfg, feeds={AGG: [MID]},
                meta={MID: (ORACLE, LLTV)}, shared=shared, store=store, guard=guard,
                alerter=alerter, log=_log(), reads_fn=reads_fn, prepare_fn=prepare_fn,
                dispatch_fn=dispatch_fn, px_hint=12345, hint_oracle=ORACLE)


def _new_rec():
    return {"lock": threading.Lock(), "order": [], "done": [], "dispatched": [], "t0": 0.0}


def _run(kw, rec, block):
    rec["t0"] = time.perf_counter()
    out = hp._process_transmit(AGG, block, 0, time.time(), **kw)
    return out, time.perf_counter() - rec["t0"]


class _FakeIndex:
    """Заменитель ChainIndex ровно в той части, которой пользуется фильтр трупов."""

    def __init__(self, pairs, fresh=True):
        self._pairs, self._fresh = pairs, fresh
        self.calls = 0

    def corpses(self, max_age_blocks=None):
        self.calls += 1
        return None if not self._fresh else dict(self._pairs)


_orig_flips = hp._flips
hp._flips = lambda price, lltv, tba, tbs, positions: [
    (b, types.SimpleNamespace(hf=0.98), du) for (b, bs, col, du) in positions]

in_budget = -1
try:
    # ---------------------------------------------------------------- А. пропускная способность
    print("А. КАСКАД: %d целей, подготовка %.3fс сети каждая, бюджет %.1fс" %
          (N_TARGETS, PREP_COST_SEC, CASCADE_BUDGET_SEC))
    bs = ["0xb%02d" % i for i in range(N_TARGETS)]
    rec = _new_rec()
    kw = _mk_env(borrowers=bs, debt_by={b: 5000.0 for b in bs},
                 prep_sleep=PREP_COST_SEC, rec=rec)
    res, el = _run(kw, rec, 200)
    in_budget = sum(1 for (_b, t_out, _t_in) in rec["done"] if t_out <= CASCADE_BUDGET_SEC)
    print("  ready за %.1fс: %d из %d (весь набор за %.2fс, dispatched=%d)" %
          (CASCADE_BUDGET_SEC, in_budget, N_TARGETS, el, len(rec["dispatched"])))
    _check(in_budget >= WANT_IN_BUDGET,
           "за бюджет каскада успевает >=%d из %d целей (факт %d)" %
           (WANT_IN_BUDGET, N_TARGETS, in_budget))
    _check(len(rec["done"]) == N_TARGETS,
           "подготовлены все %d целей (факт %d)" % (N_TARGETS, len(rec["done"])))
    _check(sorted(rec["dispatched"]) == sorted(bs),
           "каждая готовая цель ушла в dispatch ровно один раз (отправок %d, уник %d)" %
           (len(rec["dispatched"]), len(set(rec["dispatched"]))))

    # ---------------------------------------------------------------- Б. откат снимает механизм
    print("Б. ОТКАТ REACTIVE_PREP_THREADS=1: последовательное поведение возвращается")
    _n_roll = 10
    bs2 = ["0xc%02d" % i for i in range(_n_roll)]
    _had = hasattr(hp, "REACTIVE_PREP_THREADS")
    _saved = getattr(hp, "REACTIVE_PREP_THREADS", None)
    hp.REACTIVE_PREP_THREADS = 1
    try:
        rec2 = _new_rec()
        kw2 = _mk_env(borrowers=bs2, debt_by={b: 5000.0 for b in bs2},
                      prep_sleep=PREP_COST_SEC, rec=rec2)
        res2, el2 = _run(kw2, rec2, 201)
    finally:
        if _had:
            hp.REACTIVE_PREP_THREADS = _saved
        else:
            del hp.REACTIVE_PREP_THREADS
    seq_expect = int(CASCADE_BUDGET_SEC / PREP_COST_SEC)          # 6
    in_budget2 = sum(1 for (_b, t_out, _t_in) in rec2["done"] if t_out <= CASCADE_BUDGET_SEC)
    print("  ready за %.1fс: %d из %d (весь набор за %.2fс)" %
          (CASCADE_BUDGET_SEC, in_budget2, _n_roll, el2))
    _check(rec2["order"] == bs2,
           "порядок подготовок = порядок целей (факт %s)" % (rec2["order"][:3],))
    _check(in_budget2 <= seq_expect + 1,
           "ручка снимает МЕХАНИЗМ, а не параметр: за бюджет проходит <=%d (факт %d)" %
           (seq_expect + 1, in_budget2))
    _check(el2 >= _n_roll * PREP_COST_SEC * 0.9,
           "e2e отката ~ сумма подготовок (%.2fс при ожидании ~%.2fс)" %
           (el2, _n_roll * PREP_COST_SEC))

    # ---------------------------------------------------------------- В. пыль не готовим
    print("В. ПЫЛЬ (du < hot_min_repaid_usd) не доходит до дорогой подготовки")
    bs3 = ["0xd0", "0xd1", "0xd2", "0xd3"]
    rec3 = _new_rec()
    kw3 = _mk_env(borrowers=bs3,
                  debt_by={"0xd0": 5000.0, "0xd1": 1.0, "0xd2": 1999.0, "0xd3": 9000.0},
                  prep_sleep=0.0, rec=rec3)
    _run(kw3, rec3, 202)
    _check(sorted(rec3["order"]) == ["0xd0", "0xd3"],
           "подготовлены только цели выше порога (факт %s)" % (sorted(rec3["order"]),))

    # ---------------------------------------------------------------- Г. фильтр трупов (#108)
    print("Г. ФИЛЬТР ТРУПОВ (#108: 30 из 43 «healthy-race» были уже ликвидированы)")
    _get_index = hp._chain_index.get_index
    dead, live = "0xe0", "0xe1"

    def _corpse_case(pairs, fresh, block, note, want):
        fi = _FakeIndex(pairs, fresh=fresh) if pairs is not None else None
        hp._chain_index.get_index = (lambda: fi)
        try:
            r = _new_rec()
            k = _mk_env(borrowers=[dead, live], debt_by={dead: 5000.0, live: 5000.0},
                        prep_sleep=0.0, rec=r)
            _run(k, r, block)
        finally:
            hp._chain_index.get_index = _get_index
        _check(sorted(r["order"]) == sorted(want), "%s (готовили %s)" % (note, r["order"]))
        return fi

    # Г1. индекс ЗНАЕТ, что цель добита насухо (repaid >= наши borrow_shares) -> не готовим
    _corpse_case({(MID, dead): (10**6, 0)}, True, 203,
                 "доказанный труп не оплачен подготовкой", [live])
    # Г2. безнадёжный долг списан -> позиция обнулена -> труп
    _corpse_case({(MID, dead): (1, 5)}, True, 204,
                 "списанный безнадёжный долг = труп", [live])
    # Г3. ЧАСТИЧНАЯ ликвидация (88.7% ликвидаций частичные) — цель ЖИВА
    _corpse_case({(MID, dead): (10**5, 0)}, True, 205,
                 "частичное погашение НЕ труп — цель остаётся в работе", [dead, live])
    # Г4. индекс ПРОТУХ -> «не знаю» НЕ гасит огонь
    _corpse_case({(MID, dead): (10**6, 0)}, False, 206,
                 "протухший индекс = незнание, огонь НЕ гасим", [dead, live])
    # Г5. индекса НЕТ вовсе -> «не знаю» НЕ гасит огонь
    _corpse_case(None, True, 207,
                 "индекса нет = незнание, огонь НЕ гасим", [dead, live])

    # Г6. индекс не ходит в СЕТЬ с горячего пути и опрашивается ОДИН раз на рынок,
    #     а не на каждую цель (26 копий словаря пар в каскаде — это уже цена)
    fi5 = _FakeIndex({})
    hp._chain_index.get_index = lambda: fi5
    try:
        bs9 = ["0xf%02d" % i for i in range(8)]
        rec9 = _new_rec()
        kw9 = _mk_env(borrowers=bs9, debt_by={b: 5000.0 for b in bs9},
                      prep_sleep=0.0, rec=rec9)
        _run(kw9, rec9, 208)
    finally:
        hp._chain_index.get_index = _get_index
    _check(fi5.calls == 1,
           "индекс опрошен один раз на рынок, не на цель (вызовов %d при 8 целях)" % fi5.calls)
finally:
    hp._flips = _orig_flips

if _FAILS:
    print("\nКРАСНАЯ: провалов %d" % len(_FAILS))
    for m in _FAILS:
        print("  - %s" % m)
    sys.exit(1)
print("\nПРОШЛИ: хвост каскада берётся (%d из %d за %.1fс)" %
      (in_budget, N_TARGETS, CASCADE_BUDGET_SEC))
