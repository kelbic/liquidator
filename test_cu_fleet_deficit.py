# -*- coding: utf-8 -*-
"""Стенд ФЛОТСКОГО ДЕФИЦИТА (ПРЕДЕЛ 4, 26.08).

ПОЧЕМУ ЮНИТ, А НЕ «тревога позвонила». Молчание внутри окна дедупа неотличимо от сломанной
записи (канон 26.08 по #ed9e), поэтому приёмка идёт по ЧИСТОЙ функции. Каждый тест, который
проверяет фикс, идёт в паре с НЕГАТИВНЫМ КОНТРОЛЕМ к самой суите: со снятым фиксом он обязан
краснеть (канон seam-stubbed-above-the-defect, повтор 21.08).
"""
import analysis.cu_quota_watch as W

QUOTA = 30e6


def _recs(per_hour, t0=1_787_700_000.0):
    """per_hour: список dict host->cu; одно окно = один час."""
    return [{"ts": t0 + 3600 * i, "until": t0 + 3600 * i, "restarts": 1,
             "hosts": {h: {"cu": cu, "sec": 3600.0, "cu_boot": 0, "n": 30, "boot": 0}
                       for h, cu in d.items()}}
            for i, d in enumerate(per_hour)]


def _anchor(mtd, locked=()):
    hosts = {}
    for h, m in mtd.items():
        hosts[h] = {"mtd_cu": m, "as_of": 1_787_338_435, "reset": "calendar"}
        if h in locked:
            hosts[h]["locked_reason"] = "звено квитанций"
    return {"quota_cu": QUOTA, "reset": "calendar", "hosts": hosts}


NOW = 1_787_764_021.0            # 26.08 17:07Z; до календарного 01.09 ~5.29 сут


def test_перелив_между_ключами_не_меняет_флотского_приговора():
    """ГЛАВНОЕ СВОЙСТВО: величина инвариантна к раскладке спроса по ключам.

    Два ряда с ОДИНАКОВОЙ суммой, но разным распределением (в первом жжёт A, во втором тот
    же объём переехал на B — ровно пинг-понг 15:05-15:18). Приговор обязан совпасть.
    """
    a = _anchor({"A": 12e6, "B": 12e6})
    r1 = _recs([{"A": 400_000, "B": 0}] * 12)
    r2 = _recs([{"A": 0, "B": 400_000}] * 6 + [{"A": 400_000, "B": 0}] * 6)
    f1 = W.fleet_deficit(a, {"hosts": {}}, r1, now=NOW)
    f2 = W.fleet_deficit(a, {"hosts": {}}, r2, now=NOW)
    assert f1 is not None and f2 is not None
    assert abs(f1["supply_cu"] - f2["supply_cu"]) < 1.0
    assert abs(f1["demand_hi"] - f2["demand_hi"]) < 1.0
    assert (f1["deficit_lo"] > 0) == (f2["deficit_lo"] > 0)


def test_запертое_ведро_в_предложение_не_идёт_и_названо():
    a = _anchor({"A": 12e6, "D": 0.0}, locked=("D",))
    f = W.fleet_deficit(a, {"hosts": {}}, _recs([{"A": 400_000}] * 12), now=NOW)
    assert abs(f["supply_cu"] - 18e6) < 1.0, f["supply_cu"]     # 30-12, D не в счёте
    assert [h for h, _rem, _why in f["locked"]] == ["D"]
    assert abs(f["locked"][0][1] - 30e6) < 1.0                  # но его остаток НАЗВАН
    assert "D" not in [h for h, _ in f["hosts"]]


def test_дефицит_есть_на_обеих_границах_полосы():
    """Реальная раскладка 26.08 17:07Z: A 12.03 + B 3.37 + C 24.65 = 40.06M против спроса."""
    a = _anchor({"A": 17.97e6, "B": 26.63e6, "C": 5.35e6})
    # ровный спрос ~13.4M/сут => 71M за 5.29 сут: дыра выходит даже по нижней границе
    f = W.fleet_deficit(a, {"hosts": {}}, _recs([{"A": 370_000, "C": 190_000}] * 12), now=NOW)
    assert abs(f["supply_cu"] - 40.05e6) < 5e4, f["supply_cu"]
    assert 5.0 < f["days_reset"] < 5.6
    assert f["deficit_lo"] > 0 and f["deficit_hi"] >= f["deficit_lo"]


def test_запаса_хватает_молчим():
    a = _anchor({"A": 1e6, "C": 1e6})
    f = W.fleet_deficit(a, {"hosts": {}}, _recs([{"A": 20_000, "C": 20_000}] * 12), now=NOW)
    assert f["deficit_lo"] < 0 and f["deficit_hi"] < 0


def test_несосчитанное_возвращает_None_а_не_ноль():
    """«Не судим» обязано отличаться от «дыры нет» — иначе молчание врёт (канон нулевого теста)."""
    assert W.fleet_deficit({}, {"hosts": {}}, _recs([{"A": 400_000}] * 12), now=NOW) is None
    assert W.fleet_deficit(_anchor({"A": 12e6}), {"hosts": {}}, [], now=NOW) is None
    assert W.fleet_deficit(_anchor({"A": 12e6}), {"hosts": {}},
                           _recs([{"A": 400_000}]), now=NOW) is None   # одно окно — не полоса


def test_полоса_это_несколько_кандидатов_а_не_одно_окно():
    """Канон «кандидаты+максимум»: пустой/односвечный хвост не должен давать ноль в полосу."""
    band = W.fleet_demand_band(_recs([{"A": 100_000}] * 3 + [{"A": 1_000_000}] * 3))
    assert len(band) >= 2
    assert max(r for _h, r in band) > min(r for _h, r in band)


def test_книга_складывается_с_якорем():
    a = _anchor({"A": 10e6})
    f = W.fleet_deficit(a, {"hosts": {"A": {"cu": 5e6}}}, _recs([{"A": 400_000}] * 12), now=NOW)
    assert abs(f["supply_cu"] - 15e6) < 1.0, f["supply_cu"]
