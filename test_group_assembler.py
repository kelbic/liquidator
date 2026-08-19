"""ПРИЁМКА СБОРЩИКА МУЛЬТИЦЕЛЕВОЙ ГРУППЫ §9 (задача #133) — БЕЗ СЕТИ.

Предмет. Каркас §9 стоял целиком: математика худшего порядка (`board_v6.same_pool_min_outs`),
единая дверь (`assert_group_min_out_safe`), кумулятивный квотер (`route_depth`) и приём
`extra_plans` в `_arm_board_v6`. Не было ровно ОДНОГО звена — того, кто эти extra_plans
заполняет. Пока его нет, «мультицелевая группа» это группа из ОДНОЙ цели: код есть, эффекта нет.

Каноны стенда:
 * КРАСНЫЙ-НА-БАЗЕ. У каждого пункта есть негативный контроль: измерение обязано УМЕТЬ
   провалиться. П1 прямо противопоставляет «сборщик есть» и «сборщика нет» на ОДНОМ входе.
 * ПРИЁМКА ЭТАЛОНОМ, А НЕ САМООТЧЁТОМ. minOut, испечённый в калдату, декодируется ИЗ КАЛДАТЫ
   и сверяется с НЕЗАВИСИМЫМ прогоном `same_pool_min_outs` по котировщику, построенному прямо
   из модели пула (мимо route_depth). Совпасть обязаны до wei.
 * ШОВ НИЖЕ ПРОВЕРЯЕМОЙ ЛОГИКИ. Подменён ТОЛЬКО транспорт котировок (`route_depth._quote_batch`)
   и сборка чужих проб (`build_fn` — штатный шов класса). Карта роутов, кодирование пути,
   `make_cumulative_quote_fn`, `same_pool_min_outs`, `direct_swap` и `_arm_board` — боевые.
 * СВОЙСТВО ПРОВЕРЯЕТСЯ ТАМ, ГДЕ ОНО ЛОМАЕТСЯ: пул мелкий, и одна из целей худший порядок НЕ
   выживает, хотя одиночный своп прошла бы. На бесконечной глубине тест не значил бы ничего.

ЗАПУСК: python3 -m pytest test_group_assembler.py -x -q
"""
import os
import pathlib
import sys
import threading
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eth_abi import decode as abi_decode           # noqa: E402
from eth_abi import encode as abi_encode           # noqa: E402

import chain.board_v6 as bd                        # noqa: E402
import chain.execute as ex                         # noqa: E402
import chain.route_depth as rd                     # noqa: E402
import chain.spec_fire_live as sfl_mod             # noqa: E402
from chain.spec_fire_live import SpecFireLive      # noqa: E402

COLL = ex._CBBTC                                   # прямой роут v1/Slipstream, кап 30 BTC
LOAN = ex._UNIV3_USDC
MID = "0x" + "d1" * 32
OWNER = "0x" + "01" * 20
E1 = "0x" + "02" * 20
E2 = "0x" + "03" * 20
E3 = "0x" + "04" * 20
LIQ = "0x" + "9a" * 20
MP = {"loanToken": LOAN, "collateralToken": COLL, "oracle": "0x" + "0a" * 20,
      "irm": "0x" + "0b" * 20, "lltv": 860000000000000000}

# Размеры сейза (единицы cbBTC, 1e8) и долги. Числа подобраны так, что цель E2 одиночный своп
# проходит, а худший порядок (все n−1 раньше) — НЕТ: ровно тот класс, ради которого §9 и есть.
# E2 — НЕПРОХОДНАЯ по построению (одиночный своп проходит, худший порядок нет), E3 — проходная
# того же размера: пара нужна, чтобы «вынесли непроходную» отличалось от «вынесли последнюю».
SEIZED = {OWNER: 100_000_000, E1: 150_000_000, E2: 50_000_000, E3: 50_000_000}
REPAID = {OWNER: 90_000_000_000, E1: 135_000_000_000, E2: 49_500_000_000, E3: 45_000_000_000}
DEBT_USD = {OWNER: 9_000_000.0, E1: 13_500_000.0, E2: 4_950_000.0, E3: 4_500_000.0}
TRIGGER = 2 * 10 ** 36              # граница здоровья цели (fail-open только при 0)
HR_DEFAULT = 1300                   # боевой дефолт запаса §9 (см. _assemble_group)


class Pool:
    """Константное произведение с комиссией СО ВХОДА (concentrated liquidity — все наши роуты
    такие). Модель нужна стенду дважды и в двух разных ролях: как ответ подменённого квотера и
    как НЕЗАВИСИМЫЙ источник эталонных чисел."""
    FEE_NUM, FEE_DEN = 997, 1000

    def __init__(self, x, y):
        self.x0, self.y0 = int(x), int(y)

    def quote(self, x_in):
        """Выход ОДНОГО свопа размера x_in из начального состояния = кумулятивная котировка."""
        if int(x_in) <= 0:
            return 0
        dx = int(x_in) * self.FEE_NUM // self.FEE_DEN
        return self.y0 * dx // (self.x0 + dx)


POOL = Pool(20_000_000_000, 20_000_000_000_000)    # 200 BTC / 20M USDC — мелкий НАРОЧНО


# ===================================================================================================
# СТЕНД
# ===================================================================================================
def _mk_plan(borrower, seized=None, repaid=None, trigger=None):
    """Плановая часть пробы РОВНО той формы, что отдаёт `_build` (включая новые ключи §9),
    со свопом, собранным БОЕВЫМ direct_swap по одиночной семантике (minOut = долг)."""
    s = SEIZED[borrower] if seized is None else int(seized)
    ra = REPAID[borrower] if repaid is None else int(repaid)
    sw = ex.direct_swap(COLL, LOAN, s, LIQ, LIQ, repaid_assets=ra)
    assert sw is not None, "роут cbBTC->USDC обязан существовать — стенд без него бессмыслен"
    return {"mp": MP, "borrower": borrower, "repaid_shares": s * 7,
            "router": sw["router"], "swap_calldata": sw["calldata"],
            "min_profit": ra * sfl_mod.SPEC_FIRE_MIN_PROFIT_BPS // 10000,
            # НЕНУЛЕВОЙ по умолчанию: trigger==0 у любой цели обнуляет gate ВСЕЙ группы и
            # выключает фильтр цены для всех — сборщик обязан такую цель не брать (см. П10)
            "trigger": TRIGGER if trigger is None else int(trigger),
            "seized": s, "repaid_assets": ra, "coll": COLL, "loan": LOAN}


def _mk_probe(borrower, kyber=False, **kw):
    pl = _mk_plan(borrower, **kw)
    return {"calldata": "0x" + "cd" * 500, "gas": 625_000,
            # swap_meta = None означает «свап пришёл от Kyber-агрегата» (пул неизвестен)
            "swap_meta": None if kyber else (COLL, LOAN, pl["seized"]), "plan": pl}


class _Bench:
    """Собранный SpecFireLive + счётчики подменённого транспорта котировок."""

    def __init__(self, borrowers, probes, mode="same-pool", v6=True, batch_raises=False):
        self.quote_batches = 0                     # сколько ПАЧЕК квот ушло (смена состава = новая)
        self.quote_sizes = []                      # размеры последней пачки
        self.builds = []
        self.logs = []
        self._probes = probes
        self._prev = (bd.SPEC_FIRE_V6, bd.SPEC_FIRE_V6_POOL_MODE, sfl_mod.SPEC_FIRE_V2_BOARD,
                      rd._quote_batch)
        bd.SPEC_FIRE_V6 = bool(v6)
        bd.SPEC_FIRE_V6_POOL_MODE = mode
        sfl_mod.SPEC_FIRE_V2_BOARD = True
        self._batch_raises = batch_raises
        rd._quote_batch = self._batch                 # ШОВ = ТРАНСПОРТ, не логика квотера
        cfg = types.SimpleNamespace(liquidator_address=LIQ, gas_limit_est=500_000, tip_gwei=0.05,
                                    sequencer_rpc="off", eth_price_usd=1730.0,
                                    fast_lane_max_repaid_usd=500_000.0, wallet_key=None)
        shared = types.SimpleNamespace(
            lock=threading.Lock(), groups={MID: list(borrowers)}, by_id={},
            debt_by={(MID, b): DEBT_USD[b] for b in borrowers},
            hf_by={(MID, b): 1.02 for b in borrowers})
        self.sf = SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                               dev_by={"BTC": 0.0010}, build_fn=self._build,
                               log_fn=self.logs.append, now=lambda: 100000.0,
                               sleep=lambda s: None)
        self.sf._engine = lambda *a, **k: None      # склад/лестницы к предмету не относятся
        self.sf._arm_retry = lambda *a, **k: None
        self.armed = []
        self.sf._arm_board_v6 = lambda plan, mid, b, feed, debt=0.0, plan_ev=None, \
            extra_plans=None: (self.armed.append((plan, extra_plans)), True)[1]

    def _build(self, mid, borrower, debt_usd):
        self.builds.append(borrower)
        p = self._probes.get(borrower)
        return None if p is None else p

    def _batch(self, rpc, calls):
        if self._batch_raises:
            raise RuntimeError("транспорт молчит")
        self.quote_batches += 1
        out, sizes = [], []
        for _to, data in calls:
            _path, size = abi_decode(["bytes", "uint256"], data[4:])
            sizes.append(int(size))
            out.append((True, POOL.quote(int(size)).to_bytes(32, "big")))
        self.quote_sizes = sizes
        return out

    def close(self):
        (bd.SPEC_FIRE_V6, bd.SPEC_FIRE_V6_POOL_MODE, sfl_mod.SPEC_FIRE_V2_BOARD,
         rd._quote_batch) = self._prev


def _bench(borrowers=(OWNER, E1, E3), probes=None, **kw):
    probes = probes if probes is not None else {b: _mk_probe(b) for b in borrowers}
    return _Bench(borrowers, probes, **kw)


def _min_out_of(calldata):
    """amountOutMinimum ИЗ КАЛДАТЫ. Форма зависит от стиля роута: v1/Slipstream несёт deadline
    третьим полем, UniV3 — нет. Читаем по стилю карты роутов, а не «как обычно»."""
    style = ex._SWAP_ROUTES[(COLL.lower(), LOAN.lower())]["style"]
    raw = bytes.fromhex(calldata[10:])
    if style == "v1":
        return abi_decode(["(bytes,address,uint256,uint256,uint256)"], raw)[0][4]
    return abi_decode(["(bytes,address,uint256,uint256)"], raw)[0][3]


def _reference_rows(borrowers, hr=HR_DEFAULT):
    """ЭТАЛОН НЕЗАВИСИМЫМ ПРОХОДОМ: тот же расчёт §9, но котировщик построен ПРЯМО из модели
    пула — ни route_depth, ни карты роутов, ни кодирования пути здесь нет."""
    amounts = [SEIZED[b] for b in borrowers]
    floors = [REPAID[b] + REPAID[b] * sfl_mod.SPEC_FIRE_MIN_PROFIT_BPS // 10000 for b in borrowers]
    return bd.same_pool_min_outs(amounts, POOL.quote, floors=floors, headroom_bps=hr)


def _door(plans):
    """РОВНО то чтение, что делает `_arm_board_v6` перед дверью §9 — чтобы «ключи положены»
    доказывалось боевой дверью, а не наличием строк в словаре."""
    return bd.assert_group_min_out_safe(
        [p.get("pool") for p in plans],
        amounts_in=[p.get("amount_in") for p in plans],
        floors=[p.get("floor") for p in plans],
        quote_fn=plans[0].get("quote_fn"))


# ===================================================================================================
# П1. КРАСНЫЙ-НА-БАЗЕ: тот же вход, разница ровно в наличии сборщика.
# ===================================================================================================
def test_p1_group_reaches_arm_only_with_assembler():
    bh = _bench()
    try:
        sf = bh.sf
        sf._top_fat = lambda feed, direction, max_dist=None, k=1: ([(MID, OWNER, DEBT_USD[OWNER])],
                                                                   None, None)
        # (а) СО СБОРЩИКОМ: боевой `_prearm_board` -> стэш -> `_arm_board` -> дверь получает группу
        sf._prearm_board("BTC", "down", 0.01)
        assert len(bh.armed) == 1, bh.logs
        _plan, extras = bh.armed[0]
        assert extras and len(extras) == 2, ("группа обязана доехать до арма непустой", bh.logs)
        assert sf.stats["group_assembled"] == 3, sf.stats

        # (б) НЕГАТИВНЫЙ КОНТРОЛЬ: тот же вход, сборщик снят -> extra_plans пуст. Именно это
        # состояние и было в main до #133: каркас §9 на месте, группа всегда n=1.
        bh.armed.clear()
        sf._assemble_group = lambda *a, **k: None
        sf._prearm_board("BTC", "down", 0.01)
        assert len(bh.armed) == 1 and bh.armed[0][1] is None, bh.armed
    finally:
        bh.close()


# ===================================================================================================
# П2. СОСТАВ, КЛЮЧИ ДВЕРИ И minOut В КАЛДАТЕ ПРОТИВ НЕЗАВИСИМОГО ЭТАЛОНА.
# ===================================================================================================
def test_p2_keys_and_baked_min_out_match_reference():
    # Состав из трёх ПРОХОДНЫХ целей (непроходная E2 — предмет П5): здесь предмет арифметика.
    bh = _bench(borrowers=(OWNER, E1, E3))
    try:
        sf = bh.sf
        owner_probe = bh._probes[OWNER]
        before = owner_probe["plan"]["swap_calldata"]
        extras = sf._assemble_group("BTC", "down", MID, OWNER, owner_probe)
        # порядок экстр — ранг по EV (долг × близость), т.е. E1 впереди E3
        assert extras is not None and [p["borrower"] for p in extras] == [E1, E3], bh.logs
        plans = [owner_probe["plan"]] + extras

        # ключи двери — у ВСЕХ планов, quote_fn — у владельца (её читает дверь с plans[0])
        for p in plans:
            assert p["pool"] == ["%s->%s" % (COLL.lower(), LOAN.lower())], p.get("pool")
            assert p["amount_in"] == SEIZED[p["borrower"]]
            assert p["floor"] == REPAID[p["borrower"]] + p["min_profit"]
        assert callable(plans[0].get("quote_fn"))
        assert all(p.get("quote_fn") is None for p in plans[1:]), "quote_fn — свойство группы, а не каждой цели"

        # БОЕВАЯ ДВЕРЬ на собранной группе не отбивает и отдаёт те же строки
        rows_door = _door(plans)
        ref = _reference_rows([OWNER, E1, E3])
        assert [r["worst_out"] for r in rows_door] == [r["worst_out"] for r in ref], (rows_door, ref)

        # minOut ДЕКОДИРУЕТСЯ ИЗ КАЛДАТЫ и сверяется с эталоном (не с тем, что вернул сборщик)
        for p, r in zip(plans, ref):
            assert _min_out_of(p["swap_calldata"]) == r["min_out"], (p["borrower"], r)
            assert r["min_out"] > p["repaid_assets"], "групповой пол обязан быть ВЫШЕ долга"
            # боевой запас 1300bps шире любого бонуса карты роутов => клэмп полом связывает
            assert r["min_out"] == p["floor"], (p["borrower"], r["min_out"], p["floor"])
        # негативный контроль измерения: ДО сборки в калдате владельца стоял одиночный пол
        assert _min_out_of(before) == REPAID[OWNER]
        assert before != plans[0]["swap_calldata"]
        # и групповой пол СТРОГО ниже одиночного выхода — иначе тест не отличал бы формулы
        assert ref[0]["min_out"] < ref[0]["solo_out"]
    finally:
        bh.close()


# ===================================================================================================
# П3. DISJOINT (ДЕФОЛТ): сборщик не запускается вовсе — ни RPC, ни изменений в плане.
# ===================================================================================================
def test_p3_disjoint_mode_is_bit_for_bit_untouched():
    bh = _bench(mode=bd.POOL_MODE_DISJOINT)
    try:
        sf = bh.sf
        probe = bh._probes[OWNER]
        before = dict(probe["plan"])
        assert sf._assemble_group("BTC", "down", MID, OWNER, probe) is None
        assert bh.quote_batches == 0, "в строгом режиме сборщик обязан стоить НОЛЬ RPC"
        assert bh.builds == [], bh.builds
        assert probe["plan"] == before, "план владельца в disjoint не трогается ничем"
        assert all(v == 0 for k, v in sf.stats.items() if k.startswith("group_")), sf.stats
        # v6 выключен — тот же немедленный отказ (второй сторож того же гейта)
        bd.SPEC_FIRE_V6_POOL_MODE = bd.POOL_MODE_SAME_POOL
        bd.SPEC_FIRE_V6 = False
        assert sf._assemble_group("BTC", "down", MID, OWNER, probe) is None
        assert bh.quote_batches == 0
    finally:
        bh.close()


# ===================================================================================================
# П4. KYBER-ВЛАДЕЛЕЦ: пул неизвестен -> отказ ЗДЕСЬ, а не громкий отбой двери на живом арме.
# ===================================================================================================
def test_p4_kyber_owner_stops_assembly():
    probes = {OWNER: _mk_probe(OWNER, kyber=True), E1: _mk_probe(E1), E2: _mk_probe(E2)}
    bh = _bench(probes=probes)
    try:
        sf = bh.sf
        assert sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER]) is None
        assert sf.stats["group_skipped_kyber_owner"] == 1, sf.stats
        assert bh.quote_batches == 0 and bh.builds == []
        assert any("Kyber" in m for m in bh.logs), bh.logs
        # ПОЗИТИВНЫЙ КОНТРОЛЬ: тот же вход с прямым свопом владельца группу собирает
        probes[OWNER] = _mk_probe(OWNER)
        assert sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER]) is not None
    finally:
        bh.close()


# ===================================================================================================
# П5. НЕПРОХОДНАЯ ЦЕЛЬ ВЫНОСИТСЯ (а не уходит в реверт по minOut), состав пересчитывается НОВЫМ
#     котировщиком. И вырожденный случай: не выживает никто -> группы нет.
# ===================================================================================================
def test_p5_infeasible_target_is_evicted_and_group_recomputed():
    bh = _bench(borrowers=(OWNER, E1, E2))          # E2 худший порядок НЕ тянет
    try:
        sf = bh.sf
        # предпосылка теста доказывается ЧИСЛАМИ, а не верой: E2 одиночный своп проходит...
        ref3 = bd.same_pool_min_outs(
            [SEIZED[b] for b in (OWNER, E1, E2)], POOL.quote,
            floors=[REPAID[b] + REPAID[b] * sfl_mod.SPEC_FIRE_MIN_PROFIT_BPS // 10000
                    for b in (OWNER, E1, E2)])
        assert ref3[2]["solo_out"] > ref3[2]["floor"], "стенд обязан ломать свойство, а не обходить"
        assert not ref3[2]["ok"] and ref3[0]["ok"] and ref3[1]["ok"], ref3

        extras = sf._assemble_group("BTC", "down", MID, OWNER, bh._probes[OWNER])
        assert extras is not None and [p["borrower"] for p in extras] == [E1], extras
        assert bh.quote_batches >= 2, "смена состава обязана снять НОВУЮ таблицу квот"
        # квоты последней пачки — точки УРЕЗАННОГО состава (старая таблица на них кинула бы KeyError)
        t2 = SEIZED[OWNER] + SEIZED[E1]
        assert sorted(set(bh.quote_sizes)) == sorted({t2, SEIZED[OWNER], SEIZED[E1]}), bh.quote_sizes
        plans = [bh._probes[OWNER]["plan"]] + extras
        for p, r in zip(plans, _reference_rows([OWNER, E1])):
            assert _min_out_of(p["swap_calldata"]) == r["min_out"], (p["borrower"], r)
        assert sf.stats["group_assembled"] == 2 and sf.stats["group_skipped_infeasible"] == 0
        assert any("ВЫНОСИМ" in m for m in bh.logs), bh.logs
    finally:
        bh.close()


def test_p5b_nobody_survives_gives_no_group():
    # Пол экстры задран так, что она не проходит НИ В КАКОМ составе: выносить некого -> None.
    probes = {OWNER: _mk_probe(OWNER), E1: _mk_probe(E1, repaid=10 ** 14)}
    bh = _bench(borrowers=(OWNER, E1), probes=probes)
    try:
        sf = bh.sf
        assert sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER]) is None
        assert sf.stats["group_skipped_infeasible"] == 1, sf.stats
        assert sf.stats["group_assembled"] == 0
        # владелец НЕ мутирован: отказ обязан оставлять одиночный тракт бит-в-бит
        assert probes[OWNER]["plan"].get("pool") is None
        assert _min_out_of(probes[OWNER]["plan"]["swap_calldata"]) == REPAID[OWNER]
    finally:
        bh.close()


# ===================================================================================================
# П6. КОТИРОВОК НЕТ (роут агрегатный / транспорт молчит) — ОТДЕЛЬНАЯ причина от «не выживает».
# ===================================================================================================
def test_p6_no_quotes_is_its_own_refusal():
    bh = _bench(borrowers=(OWNER, E1), batch_raises=True)
    try:
        sf = bh.sf
        assert sf._assemble_group("BTC", "down", MID, OWNER, bh._probes[OWNER]) is None
        assert sf.stats["group_skipped_quotes_none"] == 1, sf.stats
        assert sf.stats["group_skipped_infeasible"] == 0, "незнание и факт о глубине — не одно и то же"
        assert bh._probes[OWNER]["plan"].get("pool") is None
    finally:
        bh.close()


def test_p6b_no_extras_after_filter():
    # Единственный кандидат идёт через Kyber -> годных экстр ноль (владелец при этом прямой).
    probes = {OWNER: _mk_probe(OWNER), E1: _mk_probe(E1, kyber=True)}
    bh = _bench(borrowers=(OWNER, E1), probes=probes)
    try:
        sf = bh.sf
        assert sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER]) is None
        assert sf.stats["group_skipped_no_extras"] == 1, sf.stats
        assert bh.quote_batches == 0
    finally:
        bh.close()


# ===================================================================================================
# П7. CONSUME-ONCE: протухший стэш не доезжает до двери на СЛЕДУЮЩЕМ арме.
# ===================================================================================================
def test_p7_stash_is_consumed_once():
    bh = _bench()
    try:
        sf = bh.sf
        sf._top_fat = lambda feed, direction, max_dist=None, k=1: ([(MID, OWNER, DEBT_USD[OWNER])],
                                                                   None, None)
        sf._prearm_board("BTC", "down", 0.01)
        assert bh.armed[-1][1], "первый арм обязан получить группу"
        assert sf._group_extra == {}, "стэш обязан опустеть на арме (consume-once)"
        # повторный арм БЕЗ нового прогона сборщика — строго n=1
        sf._arm_board(bh._probes[OWNER], MID, OWNER, "BTC", debt=DEBT_USD[OWNER])
        assert bh.armed[-1][1] is None, bh.armed[-1]
        # отказ сборщика на следующем проходе обязан СНИМАТЬ прошлую группу, а не оставлять её
        sf._group_extra = {(MID, OWNER): (id(bh._probes[OWNER]["plan"]), [object()])}
        sf._assemble_group = lambda *a, **k: None
        sf._prearm_board("BTC", "down", 0.01)
        assert bh.armed[-1][1] is None and sf._group_extra == {}, sf._group_extra
        # ...и то же самое, когда проба ВООБЩЕ не собралась: снятие обязано стоять ДО развилки,
        # иначе придержанная армом группа переживает проход "gone" и всплывает с чужой глубиной.
        sf._group_extra = {(MID, OWNER): (id(bh._probes[OWNER]["plan"]), [object()])}
        sf._build_fn = lambda m, b2, d: "gone"
        sf._prearm_board("BTC", "down", 0.01)
        assert sf._group_extra == {}, sf._group_extra
    finally:
        bh.close()


# ===================================================================================================
# П8. direct_swap: min_out=None — регрессия бит-в-бит; min_out задан — попадает в обе формы.
# ===================================================================================================
def _pairs_by_style():
    out = {}
    for (ti, to), r in ex._SWAP_ROUTES.items():
        out.setdefault(r["style"], (ti, to, r))
    assert "v1" in out and len(out) >= 2, "нужны обе ветки стилей, иначе тест проверяет половину"
    return out


def test_p8_direct_swap_min_out_both_styles():
    for style, (ti, to, r) in _pairs_by_style().items():
        amt, repaid = 1_000, 777
        # (а) РЕГРЕССИЯ: дефолт обязан совпасть с ЭТАЛОНОМ, посчитанным независимо (кодируем
        # аргументы здесь же, старой формулой minOut=repaid_assets), а не «с самим собой».
        got = ex.direct_swap(ti, to, amt, LIQ, LIQ, repaid_assets=repaid)
        if style == "v1":
            ref = abi_encode(["(bytes,address,uint256,uint256,uint256)"],
                             [(r["path"], LIQ, ex._AERO_DEADLINE, amt, repaid)])
            sel, idx, typ = ex._AERO_EXACT_INPUT_SEL, 4, "(bytes,address,uint256,uint256,uint256)"
        else:
            ref = abi_encode(["(bytes,address,uint256,uint256)"], [(r["path"], LIQ, amt, repaid)])
            sel, idx, typ = ex._UNIV3_EXACT_INPUT_SEL, 3, "(bytes,address,uint256,uint256)"
        assert got["calldata"] == "0x" + sel + ref.hex(), (style, got["calldata"])
        # (б) ЯВНЫЙ ПОЛ доезжает до amountOutMinimum и НЕ трогает остальные поля
        got2 = ex.direct_swap(ti, to, amt, LIQ, LIQ, repaid_assets=repaid, min_out=12_345)
        f0 = abi_decode([typ], bytes.fromhex(got["calldata"][10:]))[0]
        f1 = abi_decode([typ], bytes.fromhex(got2["calldata"][10:]))[0]
        assert f1[idx] == 12_345 and f0[idx] == repaid, (style, f0, f1)
        assert f1[:idx] == f0[:idx] and f1[idx + 1:] == f0[idx + 1:], (style, f0, f1)


# ===================================================================================================
# П9. ЗАПАС (headroom): ручка живёт, дефолт 0, и НИЖЕ ПОЛА minOut не опускается никогда.
# ===================================================================================================
def test_p9_headroom_default_clamps_to_floor_and_knob_binds():
    """Дефолт 1300bps ШИРЕ любого бонуса карты роутов => minOut = пол ВЕЗДЕ (боевая семантика,
    прибыль стережёт minProfit контракта). Ручка обязана двигать число в обе стороны — иначе
    «дефолт 1300» неотличим от «запас не применяется вовсе»."""
    bh = _bench(borrowers=(OWNER, E1))
    try:
        sf = bh.sf
        extras = sf._assemble_group("BTC", "down", MID, OWNER, bh._probes[OWNER])
        assert extras is not None
        plans = [bh._probes[OWNER]["plan"]] + extras
        ref_def = _reference_rows([OWNER, E1], hr=HR_DEFAULT)
        for p, r in zip(plans, ref_def):
            assert _min_out_of(p["swap_calldata"]) == r["min_out"] == p["floor"], (p, r)
    finally:
        bh.close()
    # НЕГАТИВНЫЙ КОНТРОЛЬ РУЧКИ: при hr=0 тот же вход даёт minOut = worst_out, СТРОГО выше пола
    os.environ["SPEC_FIRE_SAME_POOL_HEADROOM_BPS"] = "0"
    bh = _bench(borrowers=(OWNER, E1))
    try:
        sf = bh.sf
        extras = sf._assemble_group("BTC", "down", MID, OWNER, bh._probes[OWNER])
        plans = [bh._probes[OWNER]["plan"]] + extras
        ref0 = _reference_rows([OWNER, E1], hr=0)
        for p, r in zip(plans, ref0):
            assert _min_out_of(p["swap_calldata"]) == r["min_out"] == r["worst_out"]
            assert r["min_out"] > p["floor"], "при нулевом запасе пол перестаёт связывать"
    finally:
        os.environ.pop("SPEC_FIRE_SAME_POOL_HEADROOM_BPS", None)
        bh.close()
    # ОТРИЦАТЕЛЬНЫЙ ЗАПАС — не «наоборот», а ноль: minOut ВЫШЕ worst искал бы прибыль там, где
    # роутер обязан лишь не пустить нас в убыток.
    os.environ["SPEC_FIRE_SAME_POOL_HEADROOM_BPS"] = "-500"
    bh = _bench(borrowers=(OWNER, E1))
    try:
        sf = bh.sf
        extras = sf._assemble_group("BTC", "down", MID, OWNER, bh._probes[OWNER])
        plans = [bh._probes[OWNER]["plan"]] + extras
        for p, r in zip(plans, _reference_rows([OWNER, E1], hr=0)):
            assert _min_out_of(p["swap_calldata"]) == r["min_out"]
    finally:
        os.environ.pop("SPEC_FIRE_SAME_POOL_HEADROOM_BPS", None)
        bh.close()


# ===================================================================================================
# П10. СЛЕПАЯ ЦЕЛЬ (trigger=0) ОТРАВЛЯЕТ ГЕЙТ ВСЕЙ ГРУППЫ -> в группу не берётся; владелец без
#      границы здоровья закрывает сборку целиком.
# ===================================================================================================
def test_p10_zero_trigger_extra_is_dropped():
    probes = {OWNER: _mk_probe(OWNER), E1: _mk_probe(E1, trigger=0), E3: _mk_probe(E3)}
    bh = _bench(borrowers=(OWNER, E1, E3), probes=probes)
    try:
        sf = bh.sf
        extras = sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER])
        assert [p["borrower"] for p in extras] == [E3], "слепая цель обязана выпасть из состава"
    finally:
        bh.close()
    # ПОЗИТИВНЫЙ КОНТРОЛЬ на том же объекте: верни E1 границу — и она снова в группе
    probes = {OWNER: _mk_probe(OWNER), E1: _mk_probe(E1), E3: _mk_probe(E3)}
    bh = _bench(borrowers=(OWNER, E1, E3), probes=probes)
    try:
        extras = bh.sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER])
        assert [p["borrower"] for p in extras] == [E1, E3]
    finally:
        bh.close()


def test_p10b_zero_trigger_owner_stops_assembly():
    probes = {OWNER: _mk_probe(OWNER, trigger=0), E1: _mk_probe(E1)}
    bh = _bench(borrowers=(OWNER, E1), probes=probes)
    try:
        sf = bh.sf
        assert sf._assemble_group("BTC", "down", MID, OWNER, probes[OWNER]) is None
        assert sf.stats["group_skipped_no_trigger"] == 1, sf.stats
        assert bh.quote_batches == 0 and bh.builds == [], "отказ обязан стоить НОЛЬ RPC"
        assert probes[OWNER]["plan"].get("pool") is None
    finally:
        bh.close()


# ===================================================================================================
# П11. ГОНКА СТЭША МЕЖДУ ФИДАМИ: экстры годны ТОЛЬКО тому объекту плана, который их мутировал.
# ===================================================================================================
def test_p11_stash_belongs_to_its_plan_object():
    bh = _bench(borrowers=(OWNER, E1))
    try:
        sf = bh.sf
        probe_mine = bh._probes[OWNER]
        extras = sf._assemble_group("BTC", "down", MID, OWNER, probe_mine)
        assert extras
        # ЧУЖОЙ прогрев того же владельца: стэш принадлежит ЕГО плану, а армим МЫ — своим
        alien_plan = _mk_plan(OWNER)
        sf._group_extra = {(MID, OWNER): (id(alien_plan), extras)}
        sf._arm_board(probe_mine, MID, OWNER, "BTC", debt=DEBT_USD[OWNER])
        assert bh.armed[-1][1] is None, "чужие экстры обязаны молча деградировать к n=1"
        assert sf._group_extra == {}, "и всё равно быть съедены (не копиться)"
        # ПОЗИТИВНЫЙ КОНТРОЛЬ: СВОЙ id -> та же группа доезжает
        sf._group_extra = {(MID, OWNER): (id(probe_mine["plan"]), extras)}
        sf._arm_board(probe_mine, MID, OWNER, "BTC", debt=DEBT_USD[OWNER])
        assert bh.armed[-1][1] == extras
    finally:
        bh.close()


def test_p11b_door_exception_does_not_escape_arm():
    """Исключение ДВЕРИ §9 — отказ АРМА, а не отказ прогрева: наверх оно уйти не вправе, иначе
    вынесет публикацию probe_board и подъём движка (хвост #47/#78)."""
    bh = _bench(borrowers=(OWNER, E1))
    try:
        sf = bh.sf
        extras = sf._assemble_group("BTC", "down", MID, OWNER, bh._probes[OWNER])
        assert extras

        def _boom(*a, **k):
            raise bd.SamePoolInfeasible("§9: состав разъехался")

        sf._arm_board_v6 = _boom
        sf._group_extra = {(MID, OWNER): (id(bh._probes[OWNER]["plan"]), extras)}
        assert sf._arm_board(bh._probes[OWNER], MID, OWNER, "BTC",
                             debt=DEBT_USD[OWNER]) is False
        assert sf.stats["board_v6_group_refused"] == 1, sf.stats
        assert any("ДВЕРИ" in m for m in bh.logs), bh.logs
        # тот же случай для KeyError чужого котировщика
        sf._arm_board_v6 = lambda *a, **k: (_ for _ in ()).throw(KeyError("чужая точка состава"))
        sf._group_extra = {(MID, OWNER): (id(bh._probes[OWNER]["plan"]), extras)}
        assert sf._arm_board(bh._probes[OWNER], MID, OWNER, "BTC",
                             debt=DEBT_USD[OWNER]) is False
        assert sf.stats["board_v6_group_refused"] == 2, sf.stats
    finally:
        bh.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-x", "-q"]))
