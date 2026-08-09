"""Измеритель ревалидации решётки (#59) сам обязан быть проверяемым.

Отчёт docs/LATTICE_REVALIDATION.md опирается на analysis/lattice_revalidate.py: он проигрывает
историю оракульных апдейтов через БОЕВОЙ фазовый гейт двумя режимами меток — «до фикса #49»
(метка = момент детекта поллом 4с) и «после» (метка = updatedAt оракула). Если измеритель
врёт, врёт и вывод «оставлять ли гейт включённым», поэтому здесь проверяется ровно то, что
делает вывод осмысленным:

  * база случайности НЕ выводится из измеряемых данных (иначе решётка доказывает сама себя);
  * измеритель умеет сказать «решётки НЕТ» на случайной серии (контроль падения);
  * счётчик артефактов скоса умеет и НОЛЬ (постоянный лаг), и НЕ-ноль (квант полла);
  * реплей исполняет НАСТОЯЩИЕ методы SpecFireLive, а не свою копию фазовой арифметики —
    доказывается подменой боевого метода: результат обязан измениться (урок «шов подменён
    выше дефекта»: 38 зелёных тестов при мёртвом боевом пути).

Сеть не трогается: все серии синтетические, транспорт разведки не зовётся.
"""
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import analysis.lattice_revalidate as lr                 # noqa: E402
import chain.spec_fire_live as sfl                       # noqa: E402

T0 = 1_700_000_000.0
FT = lr._FrozenTime(sfl.time)
sfl.time = FT                                            # шов часов только на время прогона
try:
    # --- 1. БАЗА СЛУЧАЙНОСТИ НЕ ИЗ ДАННЫХ -------------------------------------------------
    # На Base блок ровно 2с и updatedAt == block.timestamp -> все гэпы чётные, и равномерность
    # даёт 3 попадания ({0,2,28}) из 15 чётных остатков, а не 5/30.
    assert abs(lr.null_chance(30, 2, tick=2.0) - 0.2) < 1e-9, lr.null_chance(30, 2, 2.0)
    assert abs(lr.null_chance(30, 2, tick=1.0) - 5 / 30.0) < 1e-9, lr.null_chance(30, 2, 1.0)
    # КОНТРОЛЬ круговой логики: база обязана быть одинаковой для ИДЕАЛЬНОЙ и для СЛУЧАЙНОЙ
    # серии — она свойство ЧАСОВ, а не наблюдений.
    perfect = [T0 + 30.0 * i for i in range(60)]
    rnd = [T0]
    _r = random.Random(3)
    for _ in range(59):
        rnd.append(rnd[-1] + 2 * _r.randint(10, 100))    # чётные гэпы, но не решётка
    a, b = lr.static_frac(perfect), lr.static_frac(rnd)
    assert a["chance"] == b["chance"] == 0.2, (a["chance"], b["chance"])
    assert a["frac"] == 1.0 and abs(a["lift"] - 5.0) < 1e-9, a
    # --- 2. КОНТРОЛЬ: измеритель умеет сказать «решётки нет» -------------------------------
    assert b["frac"] < sfl.SPEC_FIRE_LATTICE_MIN_FRAC, \
        "случайная серия подтвердила решётку (frac=%.3f) — измеритель не умеет падать" % b["frac"]

    # --- 3. ГОРИЗОНТ: гэпы за MAX_AGE не свидетельствуют ни за, ни против ------------------
    far = [T0, T0 + 5000.0, T0 + 5030.0]                 # первый гэп за горизонтом 1800с
    assert lr.static_frac(far)["n"] == 1, lr.static_frac(far)

    # --- 4. МОДЕЛЬ ПОЛЛА: квантование и ПОТЕРЯ перекрытого апдейта ------------------------
    det = lr.poll_detects(perfect, seed=1)
    lags = [d[1] - d[0] for d in det if d[1] is not None]
    assert all(0 < g <= lr.POLL_SEC + lr.STEP_QUANTUM + lr.PROP_SEC for g in lags), \
        (min(lags), max(lags))
    assert max(lags) - min(lags) > 1.0, \
        "лаг детекта не гуляет — модель полла выродилась в постоянный сдвиг"
    pair = lr.poll_detects([T0, T0 + 0.5], seed=1)       # два апдейта внутри одного полла
    assert [p[2] for p in pair] == [False, True], pair

    # --- 5. РЕПЛЕЙ НА ИДЕАЛЬНОЙ РЕШЁТКЕ: гейт открыт, артефактов НЕТ ----------------------
    # Контроль «счётчик умеет ноль»: при ПОСТОЯННОМ лаге детекта фаза не едет.
    r_ok = lr.replay_feed("T", [(p, p + 0.4, True) for p in perfect], FT, probe_sec=1.0)
    assert r_ok["frac_o"] == 1.0, r_ok["frac_o"]
    assert r_ok["open_o"] == r_ok["decide"] == len(perfect), r_ok
    assert r_ok["cf_muted"] == 0 and r_ok["opened_by_skew"] == 0, r_ok
    # холостое время: вне узлов гейт обязан быть ЗАКРЫТ, иначе он ничего не экономит
    assert 0 < r_ok["probe_open_o"] < r_ok["probe_n"] * 0.4, \
        (r_ok["probe_open_o"], r_ok["probe_n"])

    # --- 6. РЕПЛЕЙ С КВАНТОМ ПОЛЛА: метка едет, артефакты появляются ----------------------
    worst = None
    for seed in range(6):
        r = lr.replay_feed("T", lr.poll_detects(perfect, seed=seed), FT, probe_sec=1.0)
        if worst is None or r["cf_muted"] > worst["cf_muted"]:
            worst = r
    assert worst["frac_o"] == 1.0, worst["frac_o"]        # оракульные метки НЕ портятся
    assert worst["frac_p"] < 1.0, worst["frac_p"]         # метки полла портятся
    assert worst["cf_muted"] > 0, \
        "квант полла 4с не дал НИ ОДНОГО артефакта при толерансе ±2с — счётчик мёртв"
    assert worst["cf_open_o"] > worst["cf_open_p"], (worst["cf_open_o"], worst["cf_open_p"])

    # --- 7. КВАРТИЛИ ПРИЗА: доля меряется по ЦЕНОВОМУ ШАГУ, а не по порядку ---------------
    rows, t, big = [], T0, False
    for i in range(40):
        step = 1_000 if i % 2 else 10                    # крупные шаги — у нечётных
        t += 30.0 if i % 2 else 37.0                     # ...и они же ровно на решётке
        rows.append({"ts_o": t, "answer": 100_000 + (step * i)})
    byq = lr.frac_by_prize(rows)
    assert byq and byq[0]["frac"] < byq[-1]["frac"], byq
    assert byq[-1]["move_lo"] >= byq[0]["move_hi"], byq   # квартили по МОДУЛЮ шага

    # --- 7b. РАЗРЕЗЫ СТРУКТУРЫ: расстояние, остатки, сутки -------------------------------
    # Вывод отчёта («в горячей фазе гейт острее всего») держится на разрезе по РАССТОЯНИЮ,
    # поэтому разрез обязан уметь показать и обратную картину.
    short_ok = [30.0] * 50 + [892.0] * 50                 # близкие на решётке, дальние — нет
    bg = {(r["lo"], r["hi"]): r for r in lr.frac_by_gap(short_ok)}
    assert bg[(0, 60)]["frac"] == 1.0 and bg[(480, 900)]["frac"] == 0.0, bg
    inverted = [38.0] * 50 + [900.0] * 50                 # ...и наоборот
    bi = {(r["lo"], r["hi"]): r for r in lr.frac_by_gap(inverted)}
    assert bi[(0, 60)]["frac"] == 0.0 and bi[(480, 900)]["frac"] == 1.0, bi
    h = lr.residues([30.0] * 7 + [32.0] * 3)
    assert h == {0: 7, 2: 3}, h
    # сутки: гэп за горизонтом не участвует ни в числителе, ни в знаменателе
    day = lr.frac_by_day([T0, T0 + 30.0, T0 + 30.0 + 5000.0, T0 + 30.0 + 5030.0])
    assert sum(v["n"] for v in day.values()) == 2, day

    # --- 8. КОНТРОЛЬ «ИСПОЛНЯЕТСЯ БОЕВОЙ КОД»: подменяем боевой метод --------------------
    # Если бы реплей нёс свою копию фазовой арифметики, подмена SpecFireLive._gate_open ничего
    # бы не изменила — и весь отчёт мерил бы не боевой гейт.
    assert lr.Replay().sf.note_push.__func__ is sfl.SpecFireLive.note_push
    _orig_gate = sfl.SpecFireLive._gate_open
    try:
        sfl.SpecFireLive._gate_open = lambda self, feed, now=None: False
        r_stub = lr.replay_feed("T", [(p, p + 0.4, True) for p in perfect], FT)
        assert r_stub["open_o"] == 0 and r_stub["cf_open_o"] == 0, \
            "подмена боевого _gate_open не изменила результат — реплей мерит НЕ боевой гейт"
    finally:
        sfl.SpecFireLive._gate_open = _orig_gate
    _orig_frac = sfl.SpecFireLive._lattice_frac
    try:
        sfl.SpecFireLive._lattice_frac = lambda self, feed: 0.0
        r_stub2 = lr.replay_feed("T", [(p, p + 0.4, True) for p in perfect], FT)
        assert r_stub2["ready_o"] == 0, \
            "подмена боевого _lattice_frac не изменила подтверждение — мерим НЕ боевой критерий"
    finally:
        sfl.SpecFireLive._lattice_frac = _orig_frac

    # --- 9. ПОЗИТИВНЫЕ КОНТРОЛИ САМОГО СКРИПТА исполняются и проходят ---------------------
    c3 = lr.pc3_replay_discriminates(FT)
    c4 = lr.pc4_poll_model_bites(FT)
    assert c3["perfect_frac"] == 1.0 and c3["random_frac"] < sfl.SPEC_FIRE_LATTICE_MIN_FRAC, c3
    assert c4["frac_p"] < c4["frac_o"] and c4["cf_muted"] > 0, c4
    # ...и умеют ПАДАТЬ: сломанный боевой критерий обязан завалить PC3
    _orig_frac = sfl.SpecFireLive._lattice_frac
    try:
        sfl.SpecFireLive._lattice_frac = lambda self, feed: 1.0
        try:
            lr.pc3_replay_discriminates(FT)
            raise AssertionError("PC3 прошёл при сломанном _lattice_frac — контроль слепой")
        except lr.Fail:
            pass
    finally:
        sfl.SpecFireLive._lattice_frac = _orig_frac
finally:
    sfl.time = FT._real                                   # часы боевого модуля возвращаем
assert sfl.time is not FT and hasattr(sfl.time, "monotonic")

print("ПРОШЛИ: ревалидация решётки — база не из данных, счётчик скоса умеет ноль и не-ноль, "
      "реплей исполняет боевой гейт")
