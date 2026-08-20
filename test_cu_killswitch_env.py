"""Ручка отката пейджа квоты: инлайн-# в .env не должен ВКЛЮЧАТЬ глушилку.

Инцидент 20.08 ~20:0xZ: в /root/liquidator/.env строка `CU_ALERT_TG=0  # комментарий`.
systemd EnvironmentFile инлайн-комментарий не срезает, в процесс приезжало "0  # ...",
сравнение с ("0","false","no","off") не совпадало => глушилка читалась как ВКЛЮЧЕНО, и
kelbic получил квотные пейджи 18:10 и 19:18 ПОСЛЕ того, как отключение объявили сделанным.
Канон: «ручка отката обязана снимать механизм» + «незнание обязано закрывать гард».

ФИКСТУРА НЕ СПИСАНА С ПРОДУКТА ПО СМЫСЛУ: строка ниже — БАЙТ-В-БАЙТ то, что стояло в бою
(снято с машины), а не то, что удобно парсеру. Тест на «0 # c» прошёл бы и на старом коде
при другом раскладе пробелов; здесь воспроизводится ровно тот вход, что пробил защиту.
"""
import os
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.hotpath as h  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:160])))
    if not ok:
        FAILS.append(name)


PROD = ("0  # 20.08: квота -> инбокс агента (analysis/cu_quota_watch на агентской машине), "
        "человеку только HIL-эскалация; =1 возвращает прямой пейдж")


def flag(val, default=True):
    if val is None:
        os.environ.pop("CU_ALERT_TG", None)
    else:
        os.environ["CU_ALERT_TG"] = val
    try:
        return h._env_flag("CU_ALERT_TG", default)
    finally:
        os.environ.pop("CU_ALERT_TG", None)


# ---- 1. РЕГРЕССИЯ ИНЦИДЕНТА: боевая строка целиком ----
check("боевая строка с инлайн-# читается как ВЫКЛЮЧЕНО", flag(PROD) is False, PROD[:60])

# ---- 2. Старая механика на этом же входе действительно ПРОБИВАЛАСЬ (тест не пустой) ----
old_parse = (PROD or "1").lower() not in ("0", "false", "no", "off")
check("самоконтроль: ПРЕЖНЯЯ механика на той же строке давала ВКЛЮЧЕНО", old_parse is True)

# ---- 3. Обычные значения не сломаны ----
check("0 -> выключено", flag("0") is False)
check("off/no/false -> выключено", not any(flag(v) for v in ("off", "no", "FALSE", " 0 ")))
check("1/on/yes/true -> включено", all(flag(v) for v in ("1", "on", "YES", "true", " 1 ")))
check("пусто -> дефолт", flag("", default=True) is True and flag("", default=False) is False)
check("переменной нет -> дефолт", flag(None, default=True) is True)

# ---- 4. Непонятое значение ЗАКРЫВАЕТ гард (не открывает) ----
check("мусор -> ВЫКЛЮЧЕНО при дефолте True", flag("да", default=True) is False)
check("мусор -> ВЫКЛЮЧЕНО и при дефолте False", flag("вкл", default=False) is False)

# ---- 5. Комментарий без пробела и с табом тоже срезается ----
check("0#c и 0\\t# c -> выключено", flag("0#c") is False and flag("0\t# c") is False)
check("1  # c -> ВКЛЮЧЕНО (комментарий не переворачивает значение)", flag("1  # включено") is True)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: ручка отката пейджа — инлайн-# срезан, незнание закрывает гард"))
sys.exit(1 if FAILS else 0)
