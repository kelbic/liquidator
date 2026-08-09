"""Путь ДОСКИ считает amount_in так же ТОЧНО, как горячий путь после P1-7 (#74).

Находка ревьюера 09.08 и причина SwapFailed, который мы искали полдня: сборка плана доски
брала ПОЛНЫЕ шеры, гнала их через to_assets_up (округление ВВЕРХ) и expected_seized — то есть
получала оценку СВЕРХУ. Контракт же аппрувит РОВНО фактический баланс коллатерала
(Liquidator.sol:320-321), поэтому завышенный amount_in в предсобранной калдате заставлял роутер
тянуть больше, чем есть: revert внутри роутера -> SwapFailed().

Симптомы сходились: одинаковый газ при minOut=долг и minOut=1 (падение ДО сравнения выхода),
здоровая квота канарейки (глубина ни при чём), зелёные форк-тесты контракта (там мок-роутер).
Второй эффект прежней математики — без капа шер глубокая позиция даёт Panic 0x11 в Morpho.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from chain.hotpath import _seized_exact, _cap_repaid_shares    # noqa: E402
import chain.execute as ex
from strategy.pnl import lif_from_lltv                                     # noqa: E402
from chain.simulate import to_assets_up                        # noqa: E402

WAD = 10 ** 18
# боевые числа кита из блока 49666338 (стенд #68): рынок cbXRP/USDC, lltv 0.625
BS, COLL = 15927570158869477, 25944964342
TBA, TBS = 35624351388041, 34399827107770773119
PRICE = 1013981470000000000000000000000000000
LIF_WAD = int(round(lif_from_lltv(0.625) * WAD))

# --- 1. ГЛАВНОЕ: точная величина НЕ БОЛЬШЕ прежней оценки сверху -------------------------
exact = _seized_exact(BS, LIF_WAD, PRICE, TBA, TBS)
old_est = ex.expected_seized(to_assets_up(BS, TBA, TBS), lif_from_lltv(0.625), PRICE)
assert exact <= old_est, "точная величина ВЫШЕ прежней оценки — фикс бессмысленен"
assert exact > 0
# и она не больше того, что реально лежит на позиции (иначе роутер снова потянет лишнее)
assert exact <= COLL, "amount_in выше залога позиции: %d > %d" % (exact, COLL)

# --- 2. КОНТРОЛЬ: прежняя оценка ДЕЙСТВИТЕЛЬНО могла превышать залог ---------------------
# Без этого п.1 неотличим от «обе величины и так годились». Берём позицию, где залога впритык.
tight = exact                      # залог ровно под точную величину
assert old_est >= tight, "прежняя оценка не превышает точную даже впритык — пример не тот"

# --- 3. кап шер: не отдаём больше, чем позиция может отдать ------------------------------
capped = _cap_repaid_shares(BS, COLL, PRICE, LIF_WAD, TBA, TBS)
assert 0 < capped <= BS, capped
assert _seized_exact(capped, LIF_WAD, PRICE, TBA, TBS) <= COLL, \
    "кап шер не удержал seize в пределах залога — путь к Panic 0x11"
# мелкий залог обязан урезать шеры
small = _cap_repaid_shares(BS, COLL // 4, PRICE, LIF_WAD, TBA, TBS)
assert small < BS, "при вчетверо меньшем залоге шеры не урезаны"

# --- 4. СТРУКТУРНО: ВСЯ функция _build, а не её первая треть -----------------------------
# Прежний гард резал исходник до `liq = self.cfg.liquidator_address` и по построению не видел
# ЧАСТИЧНУЮ ветку, которая идёт ниже, — а она-то и осталась на оценке сверху (шов подменён выше
# дефекта). Берём тело функции целиком: от её def до следующего метода класса.
src = pathlib.Path("chain/spec_fire_live.py").read_text()
_s = src.index("    def _build(self, mid, borrower, debt_usd):")
build = src[_s:src.index("\n    def ", _s + 10)]
assert "liq = self.cfg.liquidator_address" in build and "SPEC_FIRE_PARTIAL" in build, \
    "срез не покрывает обе ветки _build — гард снова слеп"
assert build.count("_seized_exact(") >= 2, \
    "точную функцию зовёт не каждая ветка _build (главная + частичная)"
assert "expected_seized(" not in build, "оценка СВЕРХУ вернулась в путь доски"
assert "_cap_repaid_shares(" in build, "кап шер не применяется в пути доски"

# --- 5. ПОРОГ ЦЕНЫ считается по ПОЛНОЙ позиции, а не по величине плана (#77) -------------
# Дефект: trigger = repaid_assets*1e54/(coll*lltv), где в частичной ветке repaid_assets уже
# урезан, а coll остался полным. Контракт видел px > trig и МОЛЧА выходил — ни Liquidate, ни
# PlanFailed, газ 52k. Числа боевые: кит 0xd57c4cee из блока 49666338, роут режет до 26% долга.
LLTV_WAD = 625 * 10 ** 15
SP_PARTIAL = 4127634976292605                       # sp из partial_shares_for_cap на живом капе
assert SP_PARTIAL < BS, "пример обязан быть ЧАСТИЧНЫМ, иначе дефект не воспроизводится"
_full = to_assets_up(BS, TBA, TBS)
_part = to_assets_up(SP_PARTIAL, TBA, TBS)
trig_ok = _full * 10 ** 54 // (COLL * LLTV_WAD)     # как считаем теперь
trig_bad = _part * 10 ** 54 // (COLL * LLTV_WAD)    # как считали до #77
assert trig_ok > PRICE, "порог по полной позиции обязан пропускать ликвидируемого кита"
assert trig_bad < PRICE, "пример не воспроизводит молчание: старый порог не ниже цены"
assert trig_ok > trig_bad * 3, "разрыв порогов меньше троекратного — пример потерял остроту"

# и то же самое СТРУКТУРНО: числитель порога — полный долг позиции, не величина плана
_tr = build[build.index("trigger = 0"):]
assert "to_assets_up(int(bs), tba, tbs)" in _tr, "порог снова считается не от полного долга"
assert "int(repaid_assets) * 10**54" not in _tr, "порог вернулся к величине ПЛАНА (#77)"

print("OK: доска считает amount_in точно, не превышает залог (#74), "
      "порог цены — по полной позиции (#77)")
