"""Дрель 28.08 (#104-fix A+B): У КОЛЬЦА ЗЕРКАЛА ДВЕ ДВЕРИ, А ГАРД СТОЯЛ НА ОДНОЙ.

ЧТО ЧИНИТСЯ (вердикт линзы 28.08, перепроверен по коду):
 (A) ЧУЖОЕ СЫРЬЁ. Живая подача (chain/hotpath.py, ветка FORWARD_SEL) фильтрует по карте наших
     агрегаторов — `_dec[0] in agg_set`. Индексная (main._on_transit_104 <- chain_index) не
     фильтровала ничем: `chain_index.parse_logs` кладёт КАЖДЫЙ AnswerUpdated без проверки
     эмитента (адрес проверяется только у Liquidate — см. её docstring), потому что запрос
     идёт topics-OR без address. Замер live 300 блоков: наших 6 логов / 4 эмитента, чужих
     33 / 21 ⇒ ~85% сырья кольца — чужие тарифы, а портфель tip строится по МОДЕ кольца.
 (B) ДВОЙНАЯ ПОДАЧА. Наш транзит попадал в кольцо ДВАЖДЫ (живьём ~200мс и вторым заходом из
     индекса): у каждого пути свой дедуп, кросс-дедупа не было. Пороги веса (ENTRY_N=8,
     EXP_N=6, MIN_ENTRY=3) считают УЛИКИ — значит достигались вдвое раньше расчётных.

ПРОВЕРЯЕТСЯ ТА ЖЕ ФУНКЦИЯ, ЧТО ЗОВЁТ БОЙ: `note_transit_tip_indexed` — это ровно то, что
вызывает боевой колбэк main; гейт нарочно не спрятан в замыкании main(), где его проверял бы
только греп. Проводка колбэка (один вызов, который в функцию не спрятать) закрыта грепом ниже.

КРАСНЫЙ-НА-БАЗЕ — ПОВЕДЕНЧЕСКИЙ, А НЕ ПО ОТСУТСТВИЮ ИМЕНИ. Обращение к кольцу идёт через шов
(_index_door/_live_door): нет фикса — шов ведёт ровно туда, куда вёл базовый колбэк. На HEAD
без правки (i) падает как «чужой транзит принят индексной подачей», (iii) — как «ОДИН транзит
дал 2 сэмпла». Тот же красный даёт РУЧКА ОТКАТА на исправленном дереве
(SPEC_FIRE_MIRROR_INDEX_GATE=0), то есть откат проверен не только по имени флага.

ЗАПУСК: python3 -m pytest test_mirror_index_gate.py -q   (или python3 test_mirror_index_gate.py)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SPEC_FIRE_PROBE_TIP_MIRROR", "1")

import chain.execute as ex                                          # noqa: E402
import chain.feeds as fd                                            # noqa: E402

OUR = "0x0e3dc8a6a2c0f6e1e1b1b0f8a9d2c3b4a5968778"     # наша нога (в карте владения)
OUR2 = "0x1e0b2c38b1d0e9f8a7c6b5a4938271605f4e3d2c"    # вторая наша нога того же рынка
FOREIGN = "0xdeadbeef00112233445566778899aabbccddeeff"  # чужой эмитент AnswerUpdated
TIP = 2_200_000                                         # самый частый класс транзита (10.2% пула)
NOW = 1_787_300_000.0


# ДВЕ ДВЕРИ КОЛЬЦА — ЧЕРЕЗ ОДИН ШОВ. На исправленном дереве это боевые функции; на HEAD без
# правки шов ведёт туда же, куда вёл БАЗОВЫЙ колбэк (`_ntt104(t.get("tip"), t.get("agg"))`) и
# базовая живая ветка. Тогда (i) и (iii) падают ПОВЕДЕНЧЕСКИ — «чужой принят», «один транзит дал
# два сэмпла», — а не по отсутствию имени: тест на отсутствие имени доказывал бы только то, что
# правки нет, а не то, что без неё кольцо травится.
_HAS_FIX = hasattr(ex, "note_transit_tip_indexed")
# Гейт может быть СНЯТ РУЧКОЙ (SPEC_FIRE_MIRROR_INDEX_GATE=0) — тогда проверять свойства гейта
# нечего, и стенд судит ПРЕЖНЕЕ поведение (ниже это явные ветки, а не пропуск проверки).
_GATE_ON = _HAS_FIX and bool(getattr(ex, "_MIRROR_INDEX_GATE", False))


def _index_door(tip, agg, bn, now=NOW):
    if not _HAS_FIX:
        return ex.note_transit_tip(tip, agg, now=now)          # БАЗА: дверь без гейта и дедупа
    return ex.note_transit_tip_indexed(tip, agg, bn=bn, now=now)


def _live_door(tip, agg, bn, now=NOW):
    if not _HAS_FIX:
        return ex.note_transit_tip(tip, agg, now=now)          # БАЗА: bn живая ветка не несла
    return ex.note_transit_tip(tip, agg, now=now, bn=bn)


def _stat(name):
    return getattr(ex, "_MIRROR_IDX_STATS", {}).get(name)


def _reset():
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
        getattr(ex, "_MIRROR_SEEN", {}).clear()
    for k in getattr(ex, "_MIRROR_IDX_STATS", {}):
        ex._MIRROR_IDX_STATS[k] = 0
    getattr(ex, "_MIRROR_BLIND_WARN", [0.0])[0] = 0.0


def _seed_map(*aggs):
    """Карта владения — ТА ЖЕ, из которой hotpath делает agg_set (resolve_feeds -> _LEGS_AGG).
    Стенд сеет её напрямую, чтобы `mirror_our_aggs` шла своим боевым путём чтения, а не через
    тестовый параметр в боевой сигнатуре."""
    saved = {k: list(v) for k, v in fd._LEGS_AGG.items()}
    fd._LEGS_AGG.clear()
    if aggs:
        fd._LEGS_AGG["0x" + "11" * 32] = list(aggs)
    return saved


def _restore_map(saved):
    fd._LEGS_AGG.clear()
    fd._LEGS_AGG.update(saved)


def _ring(agg=None):
    with ex._TRANSIT_TIPS_LOCK:
        if agg is None:
            return sum(len(v) for v in ex._TRANSIT_TIPS.values())
        return len(ex._TRANSIT_TIPS.get(str(agg).lower(), []))


# --- (i) ЧУЖОЙ АГРЕГАТОР ЧЕРЕЗ ИНДЕКСНЫЙ КОЛБЭК В КОЛЬЦО НЕ ПОПАДАЕТ ---------------------------
def test_foreign_aggregator_rejected():
    saved = _seed_map(OUR, OUR2)
    try:
        _reset()
        ok = _index_door(TIP, FOREIGN, bn=50_500_001)
        assert ok is False, "чужой транзит принят индексной подачей"
        assert _ring() == 0, "кольцо пополнилось чужим тарифом: %r" % (ex._TRANSIT_TIPS,)
        # 21 чужой эмитент за 300 блоков — не выброс, а фон: гейт обязан держать поток
        for i, tip in enumerate((1_000_000, 5_000_000, 33_400_000, 1_150_000)):
            _index_door(tip, "0x%040x" % (0xF0F0 + i), bn=50_500_002 + i)
        assert _ring() == 0, "поток чужих просочился в кольцо"
        if _GATE_ON:
            assert _stat("foreign") == 5
    finally:
        _restore_map(saved)


# --- (ii) НАШ АГРЕГАТОР ПОПАДАЕТ (гейт не глушит фид целиком) ----------------------------------
def test_our_aggregator_accepted():
    saved = _seed_map(OUR, OUR2)
    try:
        _reset()
        assert _index_door(TIP, OUR, bn=50_500_010) is True
        assert _ring(OUR) == 1, "наш транзит не дошёл до кольца"
        # вторая нога того же рынка — тоже наша (владение = свойство рынка, не BF1-ноги)
        assert _index_door(TIP, OUR2, bn=50_500_010) is True
        # индекс отдаёт адрес эмитента строкой; регистр не должен решать судьбу транзита
        assert _index_door(TIP, OUR.upper(), bn=50_500_011) is True
        assert _ring(OUR) == 2 and _ring(OUR2) == 1
        if _GATE_ON:
            assert _stat("ours") == 3 and _stat("foreign") == 0
    finally:
        _restore_map(saved)


# --- (iii) LIVE + ИНДЕКС В ОДНОМ БЛОКЕ = ОДИН СЭМПЛ --------------------------------------------
def test_cross_dedup_live_and_index():
    saved = _seed_map(OUR)
    try:
        # порядок 1: живая первой (боевой порядок — она приходит на ~200мс раньше)
        _reset()
        r_live = _live_door(TIP, OUR, bn=50_500_020)                            # hotpath
        r_idx = _index_door(TIP, OUR, bn=50_500_020)                            # индекс
        assert _ring(OUR) == 1, ("ОДИН транзит дал %d сэмпла: вес класса (по нему идёт отбор "
                                 "в слоты и пороги ENTRY_N/EXP_N) удвоен" % _ring(OUR))
        assert (r_live, r_idx) == (True, False), "первый приход принят, повтор отвергнут"
        if _GATE_ON:
            assert _stat("cross_dup") == 1

        # порядок 2: индекс первым (полный скан блока может обогнать кадр суб-блока)
        _reset()
        r_idx = _index_door(TIP, OUR, bn=50_500_021)
        r_live = _live_door(TIP, OUR, bn=50_500_021)
        assert _ring(OUR) == 1, "порядок прихода решает: %d сэмпла на один транзит" % _ring(OUR)
        assert (r_idx, r_live) == (True, False)

        # ДЕДУП — ПО БЛОКУ, А НЕ ГЛУШИЛКА ФИДА: следующий блок кладётся как обычно
        assert _live_door(TIP, OUR, bn=50_500_022, now=NOW + 2) is True
        assert _ring(OUR) == 2

        # ОТВЕРГНУТЫЙ СЭМПЛ НЕ СЪЕДАЕТ КЛЮЧ: индекс несёт tip=-1 (свой дефолт «не знаю»),
        # а живая ветка того же блока считает tip из RLP кадра и считает верно.
        _reset()
        assert _index_door(-1, OUR, bn=50_500_030) is False
        assert _live_door(TIP, OUR, bn=50_500_030) is True
        assert _ring(OUR) == 1, "живой сэмпл потерян из-за брака, пришедшего первым"
    finally:
        _restore_map(saved)


# --- (iv) НЕГАТИВНЫЙ КОНТРОЛЬ: ручка =0 -> ПРЕЖНЕЕ ПОВЕДЕНИЕ (обе беды видны) ------------------
def test_negative_control_knob_off():
    saved = _seed_map(OUR)
    gate = ex._MIRROR_INDEX_GATE
    try:
        ex._MIRROR_INDEX_GATE = False
        _reset()
        # (A) прежнее: чужой эмитент кормит кольцо
        assert ex.note_transit_tip_indexed(TIP, FOREIGN, bn=50_500_040, now=NOW) is True
        assert _ring(FOREIGN) == 1, "негативный контроль не воспроизвёл прежнее поведение (A)"
        # (B) прежнее: один транзит = два сэмпла
        assert ex.note_transit_tip(TIP, OUR, bn=50_500_041, now=NOW) is True
        assert ex.note_transit_tip_indexed(TIP, OUR, bn=50_500_041, now=NOW) is True
        assert _ring(OUR) == 2, "негативный контроль не воспроизвёл прежнее поведение (B)"
        # БИТ-В-БИТ включает и отсутствие побочного состояния
        assert not ex._MIRROR_SEEN, "откат оставил дедуп-словарь живым"
        assert ex._MIRROR_IDX_STATS["cross_dup"] == 0
    finally:
        ex._MIRROR_INDEX_GATE = gate
        _restore_map(saved)


# --- НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: пустая карта владения -> кольцо не кормится ----------------------
def test_empty_ownership_map_fails_closed():
    saved = _seed_map()          # карты нет: до первого resolve_feeds / процесс без hot_loop
    try:
        _reset()
        if not _GATE_ON:        # ручка снята -> судим ПРЕЖНЕЕ поведение, а не пустоту
            assert _index_door(TIP, OUR, bn=50_500_050) is True, \
                "откат: прежнему пути карта владения была безразлична"
            return
        assert ex.note_transit_tip_indexed(TIP, OUR, bn=50_500_050, now=NOW) is False
        assert _ring() == 0
        assert ex._MIRROR_IDX_STATS["blind"] == 1, "слепой проход не посчитан — тишина без числа"
    finally:
        _restore_map(saved)


# --- РУЧКА ОТКАТА ДЕЙСТВИТЕЛЬНО ЧИТАЕТСЯ ИЗ ENV (обе половины фикса) --------------------------
def test_env_knob_maps_to_flag():
    root = os.path.dirname(os.path.abspath(__file__))
    code = ("import chain.execute as e;"
            "print(int(e._MIRROR_INDEX_GATE))")
    for val, want in (("0", "0"), ("1", "1")):
        env = dict(os.environ, SPEC_FIRE_MIRROR_INDEX_GATE=val, PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-400:]
        assert out.stdout.strip() == want, "SPEC_FIRE_MIRROR_INDEX_GATE=%s -> %r" % (val, out.stdout)


# --- ПРОВОДКА: один вызов в бою, который в функцию не спрятать -------------------------------
def test_wiring():
    root = os.path.dirname(os.path.abspath(__file__))
    src_main = open(os.path.join(root, "main.py")).read()
    src_hot = open(os.path.join(root, "chain", "hotpath.py")).read()
    src_ex = open(os.path.join(root, "chain", "execute.py")).read()

    assert "note_transit_tip_indexed as _ntt104" in src_main, "колбэк индекса зовёт дверь без гейта"
    assert '_ntt104(t.get("tip"), t.get("agg"), bn=_bn)' in src_main, "индексная подача без bn"
    assert "agg=_dec[0], bn=bn)" in src_hot, "живая подача не отдаёт bn — кросс-дедуп слеп"
    assert "_dec[0] in agg_set" in src_hot, "гейт живой ветки снят"
    # ИСТОЧНИК ОДИН: гейт индекса берёт карту владения того же resolve_feeds, а не свой список
    assert "from chain.feeds import legs_map" in src_ex, "гейт завёл вторую копию карты владения"
    assert 'os.environ.get("SPEC_FIRE_MIRROR_INDEX_GATE"' in src_ex, "ручки отката нет"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK   %s" % name)
            except AssertionError as e:
                fails.append(name)
                print("FAIL %s — %s" % (name, e))
    if fails:
        print("=== ДРЕЛЬ #104-fix КРАСНАЯ: %d провалов ===" % len(fails))
        sys.exit(1)
    print("=== ДРЕЛЬ #104-fix ЗЕЛЁНАЯ: чужие не кормят кольцо, один транзит = один сэмпл ===")


def test_index_late_120s_still_deduped():
    """ЛИНЗА Fable 28.08: индекс отстаёт от live на десятки секунд под нагрузкой. Второй приход того же
    (agg, bn) через 120с ОБЯЗАН остаться дедупнутым (кольцо = 1); через > TTL кольца — новый сэмпл."""
    import importlib, os, chain.execute as ex
    os.environ["SPEC_FIRE_MIRROR_INDEX_GATE"] = "1"; importlib.reload(ex)
    ex._TRANSIT_TIPS.clear(); ex._MIRROR_SEEN.clear()
    agg = sorted(ex._mirror_agg_set() if hasattr(ex, "_mirror_agg_set") else {OUR})[0] if False else OUR
    now = 1_800_000_000.0
    ex.note_transit_tip(1_800_000, agg, now=now, bn=500)
    ex.note_transit_tip_indexed(1_800_000, agg, bn=500, now=now + 120)
    n1 = sum(len(v) for v in ex._TRANSIT_TIPS.values())
    assert n1 == 1, "через 120с индекс снова лёг в кольцо: %d сэмплов" % n1
    # ПОЗИТИВНЫЙ КОНТРОЛЬ: за TTL дедуп ОТПУСКАЕТ — второй приход ПРИНЯТ (кольцо к тому моменту
    # само выкинуло первый сэмпл по своему TTL, поэтому судим по факту приёма, а не по сумме).
    ok = ex.note_transit_tip_indexed(1_800_000, agg, bn=500, now=now + ex._MIRROR_TTL_SEC + 5)
    assert ok is True, "за TTL кольца дедуп обязан отпустить, а вернул %r" % (ok,)
