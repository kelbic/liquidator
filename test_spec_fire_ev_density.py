"""EV-плотность (06.08): жир в досягаемости -> каденс 0.12с / кап 600; иначе — штатный DENSE.

Контракты:
  1. _tgts_fat: порог по долгу цели (>= FAT_DEBT_USD), пустой список = не жир.
  2. _window_cap: жир -> FAT_CAP, штатно -> MAX_PROBES_WINDOW.
  3. _dense_interval: жирный каденс всегда плотнее штатного и не ниже пола 0.10;
     штатный не опускается ниже 0.25 (пол send-слоя legacy).
  4. Флаг SPEC_FIRE_EV_DENSITY=0 возвращает поведение DENSE целиком.
"""
import chain.spec_fire_live as sfl


def test_tgts_fat_threshold():
    assert sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD)])
    assert sfl._tgts_fat([("m", "b", 10.0), ("m2", "b2", sfl.SPEC_FIRE_FAT_DEBT_USD * 1.5)])
    assert not sfl._tgts_fat([("m", "b", sfl.SPEC_FIRE_FAT_DEBT_USD - 1)])
    assert not sfl._tgts_fat([])


def test_window_cap():
    assert sfl._window_cap(True) == sfl.SPEC_FIRE_FAT_CAP
    assert sfl.SPEC_FIRE_FAT_CAP > sfl.SPEC_FIRE_MAX_PROBES_WINDOW
    assert sfl._window_cap(False) == sfl.SPEC_FIRE_MAX_PROBES_WINDOW


def test_dense_interval_ranges():
    fat = [sfl._dense_interval(True) for _ in range(500)]
    base = [sfl._dense_interval(False) for _ in range(500)]
    hi = sfl.SPEC_FIRE_FAT_INTERVAL_SEC + sfl.SPEC_FIRE_FAT_JITTER_SEC + 1e-9
    assert all(0.10 <= v <= hi for v in fat)
    assert max(fat) < 0.25              # жирный каденс всегда плотнее штатного
    assert all(v >= 0.25 for v in base)


def test_flag_off_restores_dense(monkeypatch):
    monkeypatch.setattr(sfl, "SPEC_FIRE_EV_DENSITY", False)
    assert not sfl._tgts_fat([("m", "b", 1e9)])
    assert sfl._window_cap(True) == sfl.SPEC_FIRE_MAX_PROBES_WINDOW
    assert sfl._dense_interval(True) >= 0.25
