"""Токен — НЕ доллар (#30): деньги канона считались в единицах loan-токена.

Оба инструмента, кормящих канон трёх классов («фронт», «ход до жертвы»), делили repaidAssets на
decimals и ТУТ ЖЕ сравнивали результат с порогом В ДОЛЛАРАХ. Для USDC совпадало случайно, для
любого другого loan-токена ошибка на порядки: 0.197 WETH читались как «$0.197» вместо ~$670 —
занижение в 3400 раз. Тот же класс, что «цена оракула ≠ цена выхода»: величина названа не тем,
чем является.

Лечение не «угадать курс», а РАЗДЕЛИТЬ: что можно назвать долларом — называем, остальное
считаем отдельно и ГРОМКО.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from analysis.units import usd_of, fmt_skipped, USD_PEGGED     # noqa: E402

# --- 1. стейбл: доллары = токены, decimals уважаются -------------------------------------
usd, tok = usd_of(132_525_683_141, "USDC", 6)
assert usd is not None and abs(usd - 132525.683141) < 1e-6, usd
assert tok == usd, (tok, usd)
assert usd_of(1_000_000, "usdc", 6)[0] == 1.0, "регистр символа не должен решать"

# --- 2. ГЛАВНОЕ: не-стейбл НЕ выдаётся за доллары -----------------------------------------
usd, tok = usd_of(197_155_361_137_892_161, "WETH", 18)
assert usd is None, "WETH снова посчитан долларами — занижение в тысячи раз"
assert abs(tok - 0.197155361137892161) < 1e-12, tok
for sym in ("WETH", "cbBTC", "EURC", "cbXRP"):
    assert usd_of(10**18, sym, 18)[0] is None, "%s принят за доллар" % sym

# --- 3. КОНТРОЛЬ: список стейблов не пуст и не всеяден -----------------------------------
assert "USDC" in USD_PEGGED and "WETH" not in USD_PEGGED and "EURC" not in USD_PEGGED

# --- 4. мусор на входе не роняет и не выдаёт ложный ноль ---------------------------------
assert usd_of(None, "USDC", 6) == (0.0, 0.0)
assert usd_of("не число", "USDC", 6) == (None, 0.0)
assert usd_of(10**18, None, None)[0] is None, "неизвестный символ обязан быть НЕ долларом"

# --- 5. отчёт о невключённых строках: пустой, когда нечего сказать -----------------------
assert fmt_skipped(0, {}) == ""
_msg = fmt_skipped(3, {"WETH": 0.5, "cbBTC": 0.01})
assert "3 строк" in _msg and "WETH" in _msg and "НЕ включены" in _msg, _msg

# --- 6. СТРУКТУРНО: оба инструмента больше не делят на decimals «в доллары» --------------
for fn in ("analysis/gap_profile.py", "analysis/reachable_money.py"):
    src = pathlib.Path(fn).read_text()
    assert 'int(dd.get("repaidAssets") or 0) / 10 ** int(la.get("decimals")' not in src, \
        "%s снова считает токены долларами" % fn
    assert "usd_of(" in src and "fmt_skipped(" in src, "%s не подключил конвертер/отчёт" % fn
    assert "skipped_n" in src, "%s не считает невключённые строки" % fn

print("OK: доллары отделены от токенов, невключённое считается и печатается")
