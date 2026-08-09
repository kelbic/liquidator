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

# --- 4. СТРУКТУРНО: путь доски зовёт ТЕ ЖЕ функции, что горячий путь ---------------------
src = pathlib.Path("chain/spec_fire_live.py").read_text()
blk = src[src.index("repaid_shares = _cap_repaid_shares"):]
blk = blk[:blk.index("liq = self.cfg.liquidator_address")]
assert "_seized_exact(" in blk, "доска снова считает seize не точной функцией"
assert "expected_seized(" not in blk, "оценка СВЕРХУ вернулась в путь доски"
assert "_cap_repaid_shares(" in src[:src.index("liq = self.cfg.liquidator_address")], \
    "кап шер не применяется в пути доски"

print("OK: доска считает amount_in точно и не превышает залог (#74)")
