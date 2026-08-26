# -*- coding: utf-8 -*-
"""Приёмка полос тяжести остатка (26.08). ЭТАЛОН — РЕАЛЬНЫЙ ИНЦИДЕНТ, а не выдуманные числа:
08:0xZ #3133 звал «4.8 сут против 5.7» и был разобран; 10:07Z он же звал «1.7 сут против 5.6»
и БЫЛ СЪЕДЕН ДЕДУПОМ, потому что ключ не нёс величину. Тест обязан падать со снятым фиксом.
"""
import unittest

from analysis.cu_quota_watch import severity_band, _SEV_EDGES


class TestSeverityBand(unittest.TestCase):
    def test_реальный_инцидент_ухудшение_меняет_полосу(self):
        # Ровно те два звонка, между которыми механизм промолчал.
        мягкий = severity_band(4.8, 5.7)   # r = 0.842
        тяжёлый = severity_band(1.7, 5.6)  # r = 0.304
        self.assertNotEqual(мягкий, тяжёлый,
                            "ухудшение запаса ВТРОЕ обязано сменить полосу, иначе дедуп съест")
        self.assertLess(мягкий, тяжёлый, "полоса обязана расти в сторону худшего")

    def test_отображение_по_границам(self):
        self.assertEqual(severity_band(10.0, 10.0), 0)   # r=1.00
        self.assertEqual(severity_band(7.5, 10.0), 0)    # r=0.75 — граница включительно
        self.assertEqual(severity_band(7.49, 10.0), 1)
        self.assertEqual(severity_band(5.0, 10.0), 1)    # r=0.50
        self.assertEqual(severity_band(2.5, 10.0), 2)    # r=0.25
        self.assertEqual(severity_band(0.1, 10.0), 3)
        self.assertEqual(severity_band(0.0, 10.0), 3)

    def test_монотонность(self):
        пред = 0
        for dz in [x / 4.0 for x in range(0, 41)]:
            b = severity_band(dz, 10.0)
            self.assertLessEqual(b, пред if пред else b)  # не растёт при росте запаса
            пред = b

    def test_незнание_закрывает_гард_худшей_полосой(self):
        # Горизонт сброса не измерен ⇒ успокаивать нельзя (канон unknown-must-close-the-gate).
        for плохой in (0, -1, None, "нет", float("nan")):
            self.assertEqual(severity_band(3.0, плохой), len(_SEV_EDGES),
                             "неизмеренный горизонт обязан давать ХУДШУЮ полосу")
        self.assertEqual(severity_band(None, 5.0), len(_SEV_EDGES))

    def test_полоса_реально_попадает_в_ключ(self):
        # НЕГАТИВНЫЙ КОНТРОЛЬ К СУИТЕ: проверяем не функцию, а ШОВ — что ключ её несёт.
        # Без этого тест был бы зелен и при полосе, посчитанной и выброшенной.
        import io
        src = io.open("analysis/cu_quota_watch.py", encoding="utf-8").read()
        self.assertIn('"cu-quota:balance:%s:sev%d" % (host, _band)', src,
                      "ключ обязан нести полосу — иначе дедуп снова глотает ухудшение")
        self.assertNotIn('key = "cu-quota:balance:%s" % host', src,
                         "старый ключ без полосы не должен остаться")


if __name__ == "__main__":
    unittest.main()
