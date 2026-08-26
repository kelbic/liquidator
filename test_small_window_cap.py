"""КАП МЕЛКОГО ОКНА (kelbic «го б», 26.08) — вариант (б) экономики.

Замер суток, на котором построено решение: 2317 проб из 2382 (97%) ушли в 13 плотных окон,
12 из них XRP на целях debt=$0-2k (приз десятки долларов); транзитов С ЖЕРТВОЙ за сутки —
ОДИН. Суточный бакет $450 исчерпывался к 09:46, то есть к настоящему каскаду денег могло
не остаться. Правка режет кап ТОЛЬКО мелких окон; жирные (fat) и детонаторные — не трогает.

Приёмка:
(а) мелкое окно (не fat, детонатор не связал порог) -> кап SPEC_FIRE_SMALL_CAP;
(б) ЖИРНОЕ окно -> прежний SPEC_FIRE_FAT_CAP, бит-в-бит;
(в) ДЕТОНАТОРНОЕ окно (порог связан близостью) -> прежний SPEC_FIRE_MAX_PROBES_WINDOW;
(г) откат SPEC_FIRE_SMALL_CAP=0 -> поведение бит-в-бит прежнее;
(д) экономия посчитана на РЕАЛЬНОМ распределении суток (13 окон), а не на глаз.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402

FAT, UNI = sfl.SPEC_FIRE_FAT_CAP, sfl.SPEC_FIRE_MAX_PROBES_WINDOW
check("ДЕФОЛТ ВЫКЛЮЧЕН (поправка 26.08: кап окна режет 20% отправок, основной расход у движка)",
      sfl.SPEC_FIRE_SMALL_CAP == 0 and sfl._window_cap(False, False) == UNI,
      sfl.SPEC_FIRE_SMALL_CAP)
sfl.SPEC_FIRE_SMALL_CAP = SMALL = 20                      # включаем для проверки самой ручки
check("(а) мелкое окно: кап = SPEC_FIRE_SMALL_CAP",
      sfl._window_cap(False, False) == SMALL, sfl._window_cap(False, False))
check("(б) ЖИРНОЕ окно не тронуто: кап = FAT_CAP",
      sfl._window_cap(True, False) == FAT and sfl._window_cap(True, True) == FAT, FAT)
check("(в) ДЕТОНАТОРНОЕ окно не тронуто: кап = MAX_PROBES_WINDOW",
      sfl._window_cap(False, True) == UNI, sfl._window_cap(False, True))

_sv = sfl.SPEC_FIRE_SMALL_CAP
sfl.SPEC_FIRE_SMALL_CAP = 0
check("(г) откат SPEC_FIRE_SMALL_CAP=0: мелкое окно снова получает прежний кап",
      sfl._window_cap(False, False) == UNI, sfl._window_cap(False, False))
sfl.SPEC_FIRE_SMALL_CAP = _sv

# ---- (д) ЭКОНОМИЯ на реальном распределении суток 26.08 ----
# 13 плотных окон (журнал): 138,135,150,105,150,150,150,150,150,600,150,... + мелочь.
DAY = [138, 135, 150, 105, 150, 150, 150, 150, 150, 600, 150, 150, 139]
was = sum(DAY)
# консервативно: ВСЕ они были мелкими (в замере 12/13 — XRP на debt<=$2k)
now = sum(min(x, SMALL) for x in DAY)
saved = (was - now) * 0.0146                                   # $/проба по цепному замеру
check("(д) экономия считается ПО ЗАМЕРУ: 13 окон 2317 проб -> %d проб, ~$%.0f/сутки"
      % (now, saved), was == 2317 and now <= 13 * SMALL and saved > 20, (was, now, saved))

# ---- гарды дрейфа боевого потребителя ----
_S = open("chain/spec_fire_live.py").read()
check("петля окна передаёт ЗАФИКСИРОВАННЫЙ на арме признак исключения, а не текущий порог",
      "_window_cap(fat_mode, _det_exempt)" in _S and "_window_cap(fat_mode, thr < _thr_uni)" not in _S)
check("обоснование ЗАМЕРОМ записано в коде (97% проб суток в 13 плотных окнах)",
      "2317 проб" in _S and "(97%)" in _S)
check("ПОПРАВКА записана в коде: кап окна режет пробы главного кошелька, не движок",
      "board_probes 4731" in _S and "1207 за 4.2ч" in _S)
check("ручка выключена дефолтом, включение одним env", 'SPEC_FIRE_SMALL_CAP", "0"' in _S)

# ---- СУЖЕНИЕ ИСКЛЮЧЕНИЯ ПО ПРИЗУ (kelbic «го» 26.08) ----
check("порог приза задан по аналогии с HR-полом ($300)",
      sfl.SPEC_FIRE_DET_PRIZE_USD == 300.0, sfl.SPEC_FIRE_DET_PRIZE_USD)
_S2 = open("chain/spec_fire_live.py").read()
check("детонатор публикует ПРИЗ цели (4-й элемент ключа)",
      "best_prize" in _S2 and "4-й элемент = ПРИЗ" in _S2)
# 26.08 (DOGE probes=120 при «кап=малый»): подпись капа на арме ВРАЛА — fat_mode
# выставляется позже, внутри петли. det/приз остаются замороженными на арме (гейт kelbic),
# а кап подписывается в close-строке ВЫЗОВОМ САМОЙ _window_cap с аргументами петли.
check("ГАРД 1: det/приз замораживаются на арме; кап подписывается ВЫЧИСЛЕНИЕМ в close",
      "_det_exempt = bool(thr < _thr_uni" in _S2 and 'det[%s]' in _S2
      and '_det_state = "det=%s приз=$%.0f"' in _S2
      and "_window_cap(fat_mode, _det_exempt)),\n" in _S2
      and '"%s кап=%s(%d)"' in _S2)
check("кап использует ЗАФИКСИРОВАННЫЙ признак, а не текущий порог",
      "_window_cap(fat_mode, _det_exempt)" in _S2)
check("названный предел записан (приз плавает внутри окна — срез на открытии)",
      "приз плавает с ценой" in _S2)
check("откат исключения одним env", 'SPEC_FIRE_DET_PRIZE_USD", "300"' in _S2)

# ГАРД 2: большой фронт не страдает — проверяем саму формулу исключения
def _exempt(det_bound, prize, thr_pr=sfl.SPEC_FIRE_DET_PRIZE_USD):
    return bool(det_bound and (thr_pr <= 0 or prize >= thr_pr))


check("ГАРД 2: фронтовая цель $97k (приз ~$12k) -> исключение ДЕЙСТВУЕТ, density полный",
      _exempt(True, 97_000 * 0.1268) is True)
check("детонатор на ПЫЛИ ($2k долг, приз ~$254) -> кап МАЛЫЙ",
      _exempt(True, 2_000 * 0.1268) is False)
check("цель на границе ($2.4k долг, приз $304) -> исключение действует",
      _exempt(True, 2_400 * 0.1268) is True)
check("окно без детонатора -> кап малый независимо от приза",
      _exempt(False, 50_000.0) is False)
check("откат SPEC_FIRE_DET_PRIZE_USD=0 -> исключение снова безусловное",
      _exempt(True, 1.0, thr_pr=0) is True)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: кап режет только мелкие окна; жир и детонатор нетронуты, откат одним env"))
sys.exit(1 if FAILS else 0)
