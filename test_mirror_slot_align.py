"""Дрель (C): ВЫРАВНИВАНИЕ СЛОТОВ — перестановка того же множества не переодевает ни одной ноги.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ. Tip ноги = слот портфеля по h%4 (probe_tip_for). Смена ЗНАЧЕНИЯ слота
= пересборка лестниц 55 ног (28-32с каждая). Значит цена смены состава определяется не тем,
СКОЛЬКО раз состав пересчитан, а тем, СКОЛЬКО ИНДЕКСОВ сменило значение.

ДЕФЕКТ (вердикт линзы 28.08, воспроизведён замером analysis/mirror_slot_align_replay.py):
при протухании колец transit_tip_portfolio возвращает () — и ЗАОДНО обнуляет _MIRROR_APPLIED,
по которому идёт выравнивание. На возврате prev = [None]*4, и тот же самый набор значений
раскладывается по индексам в НОВОМ порядке: ноги переодеваются под ПЕРЕСТАНОВКУ. Плотный поток
(4 027 транзитов, POOLED=1): 27 из 253 смен — чистая перестановка, 64% смен трогают все 4
слота = 220 ног. Комментарий в execute.py «переходы статика<->слоты не меняют tip ни одной
ноги» на таком потоке ЛОЖЕН.

ФИКС: результат ВСЕГДА выравнивается по ПРЕДЫДУЩЕМУ составу (_align_slots), статика проходит
через ТОТ ЖЕ выравниватель (mirror_effective_slots — её и зовёт денежный путь probe_tip_for),
а память состава (_MIRROR_SLOT_PREV) ПЕРЕЖИВАЕТ уход в статику.

КРАСНЫЙ-НА-БАЗЕ: без фикса падают (i), (ii) и (v). Откат: SPEC_FIRE_MIRROR_SLOT_ALIGN=0 —
проверка (iv) снимает механизм módulo-глобалом и требует, чтобы (i) СНОВА упала.

ЗАПУСК: python3 -m pytest test_mirror_slot_align.py -q   (или python3 test_mirror_slot_align.py)
"""
import os
import sys

os.environ.setdefault("SPEC_FIRE_PROBE_TIP_MIRROR", "1")

import chain.execute as ex                                        # noqa: E402

NOW = 1_787_400_000.0
AGG_A, AGG_B, AGG_C, AGG_D = "0xa0a0", "0xb0b0", "0xc0c0", "0xd0d0"
PERM_STATIC = (1_150_000, 1_000_000, 1_100_000, 1_300_000)        # то же множество, что и тиры


def _reset():
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
    ex._MIRROR_APPLIED, ex._MIRROR_APPLIED_MODES = (), frozenset()
    ex._MIRROR_LAST_SLOTS = ()
    ex._MIRROR_HOT_UNTIL = 0.0
    if hasattr(ex, "_MIRROR_SLOT_PREV"):
        ex._MIRROR_SLOT_PREV = ()
    ex._mirror_exp_reset()


def _feed(tip, agg, n, t0, step=1.0):
    for k in range(n):
        ex.note_transit_tip(tip, agg, now=t0 + k * step)


def _eff(t):
    """СОСТАВ, КОТОРЫЙ НОСЯТ НОГИ. После фикса — тот же вызов, что у probe_tip_for; на базе —
    прежняя модель «портфель либо статические тиры» (как в analysis/mirror_v2_replay.py)."""
    fn = getattr(ex, "mirror_effective_slots", None)
    if fn is not None:
        s = fn(now=t)
        return tuple(int(x) for x in s) if s else tuple(int(x) for x in ex._PROBE_TIP_TIERS)
    p = ex.transit_tip_portfolio(now=t)
    return tuple(int(x) for x in p) if p else tuple(int(x) for x in ex._PROBE_TIP_TIERS)


def scenario(align=None):
    """Три акта на ОДНОЙ ленте времени, состав снимается денежным аксессором.

    акт 1: три класса пола с уликами 10/9/8 -> состав по весу;
    акт 2: > TTL без транзитов (кольца протухли) -> статика; затем ТОТ ЖЕ набор классов, но
           улики переставлены (10/9/8 достаются другим значениям) -> кандидатный порядок иной;
    акт 3: четвёртый класс 1.01M вытесняет 1.3M -> меняется РОВНО одно значение множества.
    """
    saved = getattr(ex, "_MIRROR_SLOT_ALIGN", None)
    if align is not None and saved is not None:
        ex._MIRROR_SLOT_ALIGN = align       # align=None -> как настроено окружением (боевой откат)
    try:
        _reset()
        _feed(1_000_000, AGG_A, 10, NOW)
        _feed(1_150_000, AGG_B, 9, NOW)
        _feed(1_100_000, AGG_C, 8, NOW)
        r1 = _eff(NOW + 20)
        t2 = NOW + 2000.0                       # > _MIRROR_TTL_SEC (900) без транзитов
        empty = ex.transit_tip_portfolio(now=t2)
        static_eff = _eff(t2)
        _feed(1_150_000, AGG_B, 10, t2)         # улики переставлены: теперь 1.15M тяжелее 1.0M
        _feed(1_000_000, AGG_A, 9, t2)
        _feed(1_100_000, AGG_C, 8, t2)
        r2 = _eff(t2 + 20)
        _feed(1_010_000, AGG_D, 11, t2 + 21)    # четвёртый класс пола: вытесняет статический 1.3M
        r3 = _eff(t2 + 45)
        return {"r1": r1, "empty": empty, "static_eff": static_eff, "r2": r2, "r3": r3}
    finally:
        if align is not None and saved is not None:
            ex._MIRROR_SLOT_ALIGN = saved


def _moved(a, b):
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


# --- (i) ПЕРЕСТАНОВКА ТОГО ЖЕ МНОЖЕСТВА -> СОСТАВ НЕИЗМЕНЕН ------------------------------------
def test_i_permutation_does_not_redress():
    s = scenario()
    assert set(s["r2"]) == set(s["r1"]), "стенд не построил перестановку: %s / %s" % (s["r1"], s["r2"])
    assert s["r2"] == s["r1"], (
        "перестановка того же множества переодела %d слотов (%d ног): %s -> %s"
        % (len(_moved(s["r1"], s["r2"])), 55 * len(_moved(s["r1"], s["r2"])), list(s["r1"]), list(s["r2"])))


# --- (ii) УХОД В СТАТИКУ И ВОЗВРАТ -> НИ ОДИН ИНДЕКС НЕ СМЕНИЛ ЗНАЧЕНИЕ ------------------------
def test_ii_static_roundtrip_keeps_every_index():
    s = scenario()
    assert s["empty"] == (), "стенд не ушёл в статику: портфель %s" % (s["empty"],)
    assert _moved(s["r1"], s["r2"]) == [], "после возврата сменили значение индексы %s" % _moved(s["r1"], s["r2"])
    # множество статики совпадает с множеством акта 1 -> сам уход в статику не вправе переодевать
    assert set(s["static_eff"]) == set(ex._PROBE_TIP_TIERS)
    assert s["static_eff"] == s["r1"], (
        "уход в статику переодел %d слотов: %s -> %s"
        % (len(_moved(s["r1"], s["static_eff"])), list(s["r1"]), list(s["static_eff"])))


# --- (iii) ЗАМЕНА ОДНОГО ЗНАЧЕНИЯ -> МЕНЯЕТСЯ РОВНО ОДИН ИНДЕКС --------------------------------
def test_iii_single_replacement_moves_one_index():
    s = scenario()
    assert set(s["r3"]) - set(s["r2"]) == {1_010_000}, "%s -> %s" % (list(s["r2"]), list(s["r3"]))
    assert set(s["r2"]) - set(s["r3"]) == {1_300_000}, "%s -> %s" % (list(s["r2"]), list(s["r3"]))
    mv = _moved(s["r2"], s["r3"])
    assert len(mv) == 1, "сменилось %d индексов вместо одного: %s -> %s" % (len(mv), list(s["r2"]), list(s["r3"]))
    assert s["r3"][mv[0]] == 1_010_000 and s["r2"][mv[0]] == 1_300_000


# --- (iv) НЕГАТИВНЫЙ КОНТРОЛЬ: ручка снимает МЕХАНИЗМ, (i) обязана упасть -----------------------
def test_iv_rollback_handle_restores_defect():
    assert hasattr(ex, "_MIRROR_SLOT_ALIGN"), "ручки отката SPEC_FIRE_MIRROR_SLOT_ALIGN нет"
    s = scenario(align=False)
    assert set(s["r2"]) == set(s["r1"])
    assert s["r2"] != s["r1"], "с ручкой=0 дефект не вернулся — значит ручка снимает не механизм"
    # вторая половина фикса (статика через выравниватель) обязана отключаться той же ручкой
    saved = ex._MIRROR_SLOT_ALIGN
    ex._MIRROR_SLOT_ALIGN = False
    try:
        _reset()
        ex._MIRROR_SLOT_PREV = PERM_STATIC
        tips = [ex.probe_tip_for("0x%040x" % i) for i in range(8)]
        assert tips == [ex._PROBE_TIP_TIERS[i % 4] for i in range(8)], \
            "с ручкой=0 статика не бит-в-бит: %s" % tips
    finally:
        ex._MIRROR_SLOT_ALIGN = saved


# --- (v) ДЕНЕЖНЫЙ ПУТЬ: probe_tip_for берёт ВЫРОВНЕННУЮ статику, а не сырые тиры ---------------
def test_v_probe_tip_for_serves_aligned_static():
    _reset()
    ex._MIRROR_SLOT_PREV = PERM_STATIC              # ноги уже носят перестановку статики
    tips = [ex.probe_tip_for("0x%040x" % i) for i in range(8)]
    assert tips == [PERM_STATIC[i % 4] for i in range(8)], \
        "probe_tip_for обошёл выравниватель: %s против %s" % (tips, [PERM_STATIC[i % 4] for i in range(8)])
    src = open(ex.__file__.replace(".pyc", ".py")).read()
    blk = src[src.index("def probe_tip_for("):src.index("def mirror_slots_changed(")]
    assert "mirror_effective_slots" in blk, "денежный путь не ходит через общий аксессор состава"
    _reset()


# --- (vi) ИНВАРИАНТ В КОДЕ: set(new)==set(prev) => new==prev побайтно --------------------------
def test_vi_invariant_holds_for_every_permutation():
    import itertools
    base = (1_000_000, 1_150_000, 1_100_000, 1_300_000)
    for p in itertools.permutations(base):
        assert tuple(ex._align_slots(list(p), base)) == base, "инвариант нарушен на %s" % (p,)
    # дубли значений: множество как МУЛЬТИМНОЖЕСТВО, ни одно значение не теряется
    assert tuple(ex._align_slots([1_000_000, 1_000_000, 1_100_000, 1_300_000],
                                 (1_300_000, 1_100_000, 1_000_000, 1_000_000))) \
        == (1_300_000, 1_100_000, 1_000_000, 1_000_000)
    # пустой prev -> порядок кандидатов как есть (холодный старт бит-в-бит)
    assert tuple(ex._align_slots(list(base), ())) == base
    src = open(ex.__file__.replace(".pyc", ".py")).read()
    assert "SPEC_FIRE_MIRROR_SLOT_ALIGN" in src, "ручки отката нет в коде"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
        try:
            fn()
            print("OK %s" % name)
        except Exception as e:                                    # noqa: BLE001
            print("FAIL %s — %s" % (name, e))
            fails.append(name)
    if fails:
        print("=== ДРЕЛЬ (C) КРАСНАЯ: %d провалов ===" % len(fails))
        sys.exit(1)
    print("=== ДРЕЛЬ (C) ЗЕЛЁНАЯ ===")
