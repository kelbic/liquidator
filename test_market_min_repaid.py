"""#138: ПОРОГ КАНДИДАТА — ПЕР-РЫНОЧНЫЙ, от безубытка своего рынка.

ПОЧЕМУ НЕ ПЛОСКОЕ ЧИСЛО. В тракте доски нет ни одного долларового пола прибыли
(SPEC_FIRE_FAT_PRIZE_USD=0, min_profit_usd не используется) — MIN_REPAID остаётся ЕДИНСТВЕННЫМ
экономическим гейтом. Приз цели = долг × бонус рынка, значит безубыток = цена окна / бонус, и на
нашей карте он разбегается в 12 раз:
    12.68% -> $17.7 | 4.38% -> $51 | 2.62% -> $86 | 1.68% -> $134 | 1.06% -> $212
Плоские $50 на половине карты стоят НИЖЕ безубытка даже при p=1 — то есть открывают заведомо
убыточную мелочь. Плоская $1000 наоборот закрывает жирнобонусную мелочь, которая окупается с
запасом 56x.

ЗАМЕР #138 (30 суток, analysis/liq_class_histogram.py): класса $50k+ на покрытых рынках НЕТ
вовсе, весь поток в $1k-50k; бакет $0-50 — это 1487 живых позиций ради $225/мес (в 50 раз хуже
соседнего бакета по выходу на кандидата). Отсюда пол $50 и отказ опускаться ниже.

ЗАПУСК: python3 test_market_min_repaid.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.spec_fire_live as sfl                           # noqa: E402

FAILS = []
_MR = sfl._market_min_repaid_usd


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


# --- 1. БОЕВЫЕ ДЕФОЛТЫ ------------------------------------------------------------------------
check("пол кода = $50 (жирнобонусные рынки)", sfl.SPEC_FIRE_MIN_REPAID_USD == 50.0)
check("цена окна = $2.25 (кап 150 проб × ~$0.015)", sfl.SPEC_FIRE_WINDOW_COST_USD == 2.25)
check("порог при неизвестном бонусе = $1000 (прежний)", sfl.SPEC_FIRE_MIN_REPAID_NOBONUS_USD == 1000.0)

# --- 2. КАРТА БОНУСОВ -> ПРИМЕНЁННЫЙ ПОРОГ ----------------------------------------------------
# Числа бонусов — из route_depth._lltv_table (единственный источник lltv карты в репозитории).
from chain.route_depth import lif_bonus_pct                  # noqa: E402

print("\nПРИМЕНЁННЫЙ ПОРОГ ПО ЖИВОЙ КАРТЕ (бонус = LIF−1 тесного рынка пары):")
TABLE = []
for lltv, name in ((0.625, "cbLTC/cbDOGE (lltv 0.625)"), (0.770, "cbXRP/cbADA (0.770)"),
                   (0.860, "cbBTC-USDC / WETH-USDC (0.860)"), (0.915, "cbBTC/WETH (0.915)"),
                   (0.945, "LBTC/weETH/cbETH (0.945)"), (0.965, "wstETH (0.965)")):
    b = lif_bonus_pct(lltv) / 100.0
    thr = _MR(b)
    TABLE.append((name, b * 100, thr))
    print("   %-32s бонус %6.4f%%  -> порог $%.0f" % (name, b * 100, thr))

check("жирнобонусный рынок (12.68%) пускает $50 (пол кода, безубыток $17.7)",
      abs(_MR(lif_bonus_pct(0.625) / 100.0) - 50.0) < 0.01,
      "$%.2f" % _MR(lif_bonus_pct(0.625) / 100.0))
_thin = _MR(lif_bonus_pct(0.965) / 100.0)
check("тонкобонусный рынок (1.06%) режет всё ниже ~$212 (безубыток, а не вкус)",
      210.0 < _thin < 215.0, "$%.2f" % _thin)
# Таблица идёт по ВОЗРАСТАНИЮ lltv, то есть по УБЫВАНИЮ бонуса => порог обязан не убывать.
check("порог растёт по мере ИСТОНЧЕНИЯ бонуса (и наоборот)",
      all(TABLE[i][1] > TABLE[i + 1][1] and TABLE[i][2] <= TABLE[i + 1][2]
          for i in range(len(TABLE) - 1)),
      str([(round(t[1], 2), round(t[2])) for t in TABLE]))
check("ни один рынок карты не пускает цель ниже своего безубытка",
      all(t[2] * (t[1] / 100.0) >= sfl.SPEC_FIRE_WINDOW_COST_USD - 1e-9 for t in TABLE),
      str([round(t[2] * t[1] / 100, 2) for t in TABLE]))

# --- 3. НЕИЗВЕСТНЫЙ БОНУС — ПРЕЖНИЙ ПОРОГ (незнание не открывает мелочь) ----------------------
check("бонус 0.0 (нет в covered-снапшоте) -> $1000", _MR(0.0) == 1000.0)
check("бонус None -> $1000", _MR(None) == 1000.0)
check("бонус-мусор -> $1000", _MR("нет") == 1000.0)
check("отрицательный бонус -> $1000 (мусор, а не подарок)", _MR(-0.5) == 1000.0)

# --- 4. ЯВНО ПОДНЯТЫЙ ПОЛ НИКОГДА НЕ ОСЛАБЛЯЕТСЯ ---------------------------------------------
# Владелец, поставивший MIN_REPAID=500k, не должен получить $1000 из-за неизвестного бонуса.
_old = sfl.SPEC_FIRE_MIN_REPAID_USD
sfl.SPEC_FIRE_MIN_REPAID_USD = 500_000.0
try:
    check("MIN_REPAID=500k при неизвестном бонусе -> 500k, а не $1000", _MR(0.0) == 500_000.0)
    check("MIN_REPAID=500k при жирном бонусе -> 500k (пол связывает)",
          _MR(lif_bonus_pct(0.625) / 100.0) == 500_000.0)
finally:
    sfl.SPEC_FIRE_MIN_REPAID_USD = _old

# --- 5. РУЧКА ЦЕНЫ ОКНА ------------------------------------------------------------------------
_oldc = sfl.SPEC_FIRE_WINDOW_COST_USD
sfl.SPEC_FIRE_WINDOW_COST_USD = 22.5                          # окно в 10 раз дороже
try:
    check("дороже окно -> выше порог всюду (ручка связывает, а не украшает)",
          abs(_MR(lif_bonus_pct(0.625) / 100.0) - 177.4) < 1.0,
          "$%.2f" % _MR(lif_bonus_pct(0.625) / 100.0))
finally:
    sfl.SPEC_FIRE_WINDOW_COST_USD = _oldc

# --- 6. ОБЕ ТОЧКИ ВХОДА ХОДЯТ ЧЕРЕЗ ОДНУ ФОРМУЛУ ---------------------------------------------
_src = open(pathlib.Path(__file__).parent / "chain" / "spec_fire_live.py").read()
check("_top_fat считает порог пер-рыночно", "_min_usd = _market_min_repaid_usd(getattr(m," in _src)
check("сборщик §9 считает порог пер-рыночно",
      "_min_usd = _market_min_repaid_usd(self._bonus_of(mid))" in _src)
check("плоского сравнения с SPEC_FIRE_MIN_REPAID_USD в отборе целей не осталось",
      "d < SPEC_FIRE_MIN_REPAID_USD" not in _src and "d2 < SPEC_FIRE_MIN_REPAID_USD" not in _src)
check("применённый порог печатается (видимость)", "SPECFIRE порог рынка" in _src)

# --- 7. ПРОГРЕВ KYBER НЕ ТРОНУТ ---------------------------------------------------------------
# PREARM_MIN_DEBT_USD — отдельный гейт другого механизма (прогрев квоты), решение владельца
# его не касалось. Порог кандидата и порог прогрева обязаны остаться разными числами.
from chain.prearm import PREARM_MIN_DEBT_USD                  # noqa: E402
check("PREARM_MIN_DEBT_USD остался $1000 (Kyber-прогрев не задет)",
      float(PREARM_MIN_DEBT_USD) == 1000.0, str(PREARM_MIN_DEBT_USD))

if FAILS:
    print("\n=== ПОРОГ: %d ПРОВАЛОВ ===" % len(FAILS))
    sys.exit(1)
print("\n=== ПОРОГ ПЕР-РЫНОЧНЫЙ: жирнобонусная мелочь открыта, убыточная пыль закрыта, "
      "незнание бонуса держит прежний порог ===")
