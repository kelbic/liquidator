"""Теневой прогон B+6 — ЯДРО: точная целочисленная математика Morpho Blue и детектор
ликвидируемости. Модуль ЧИСТЫЙ: ни сети, ни файлов, ни времени. Его использует И реплей
(архивное состояние), И живая тень — один и тот же код, иначе реплей ничего не доказывает
о боевом пути ([[seam-stubbed-above-the-defect]]).

Формулы сверены с Morpho Blue (SharesMathLib, MathLib, _liquidate) и с chain/simulate.py
боевого бота; LIF = min(1.15, 1/(0.3·LLTV+0.7)) — как в RECHECK §5.
"""
from __future__ import annotations

WAD = 10 ** 18
ORACLE_PRICE_SCALE = 10 ** 36
VIRTUAL_SHARES = 10 ** 6
VIRTUAL_ASSETS = 1
MAX_LIF = 1_150_000_000_000_000_000          # 1.15e18
LIQUIDATION_CURSOR = 300_000_000_000_000_000  # 0.3e18


def mul_div_down(x: int, y: int, d: int) -> int:
    return (x * y) // d


def mul_div_up(x: int, y: int, d: int) -> int:
    return (x * y + (d - 1)) // d


def w_mul_down(x: int, y: int) -> int:
    return mul_div_down(x, y, WAD)


def w_div_down(x: int, y: int) -> int:
    return mul_div_down(x, WAD, y)


def w_div_up(x: int, y: int) -> int:
    return mul_div_up(x, WAD, y)


def to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    return mul_div_up(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def to_shares_down(assets: int, total_assets: int, total_shares: int) -> int:
    return mul_div_down(assets, total_shares + VIRTUAL_SHARES, total_assets + VIRTUAL_ASSETS)


def lif_from_lltv(lltv: int) -> int:
    """Liquidation Incentive Factor в WAD. Зашит в байткод, не параметризуется (RECHECK §5)."""
    return min(MAX_LIF, w_div_down(WAD, WAD - w_mul_down(LIQUIDATION_CURSOR, WAD - lltv)))


def w_taylor_compounded(x: int, n: int) -> int:
    """MathLib.wTaylorCompounded — три члена, ровно как в контракте."""
    first = x * n
    second = mul_div_down(first, first, 2 * WAD)
    third = mul_div_down(second, first, 3 * WAD)
    return first + second + third


def accrue_borrow_assets(total_borrow_assets: int, rate_per_sec: int, elapsed: int) -> int:
    """Начисление процентов на долг рынка к моменту (block.timestamp − lastUpdate).
    `Morpho.liquidate` делает _accrueInterest ПЕРЕД расчётом, поэтому без этого долг
    занижен и детектор смещён в сторону «здоров» (предел Л6 протокола)."""
    if rate_per_sec <= 0 or elapsed <= 0:
        return total_borrow_assets
    return total_borrow_assets + w_mul_down(total_borrow_assets, w_taylor_compounded(rate_per_sec, elapsed))


def health(collateral: int, borrow_shares: int, price: int, lltv: int,
           tba: int, tbs: int):
    """-> (borrowed, max_borrow, liquidatable, hf). Ровно проверка из Morpho._liquidate."""
    borrowed = to_assets_up(borrow_shares, tba, tbs)
    max_borrow = w_mul_down(mul_div_down(collateral, price, ORACLE_PRICE_SCALE), lltv)
    hf = (max_borrow / borrowed) if borrowed > 0 else float("inf")
    return borrowed, max_borrow, (max_borrow < borrowed), hf


def liquidation_plan(collateral: int, borrowed: int, price: int, lltv: int):
    """Максимальный законный выстрел по позиции.

    seizedAssets -> repaidAssets = wDivUp(mulDivUp(seized, price, SCALE), LIF)  (контракт)
    Обратно: из всего залога можно выжать repay = wDivDown(mulDivDown(coll, price, SCALE), LIF).
    Заявка ограничена долгом; если залога не хватает на весь долг — берём весь залог
    (остаток становится bad debt протокола, ликвидатору не достаётся).
    -> (repay, seized, gross_bonus, lif)
    """
    L = lif_from_lltv(lltv)
    repay_from_collateral = w_div_down(mul_div_down(collateral, price, ORACLE_PRICE_SCALE), L)
    if repay_from_collateral <= borrowed:
        repay = repay_from_collateral
        seized = collateral
    else:
        repay = borrowed
        seized = mul_div_down(w_mul_down(repay, L), ORACLE_PRICE_SCALE, price)
    gross_bonus = repay - w_div_down(repay, L)     # доля бонуса от погашения, в единицах loanToken
    return repay, seized, gross_bonus, L


def detect(market: dict, positions, price: int, min_gross_units: int = 0):
    """Детектор: кого можно ликвидировать на цене `price`.

    market: {"id","lltv","tba","tbs","rate","last_update","ts"} (tba/tbs — как отдал market(),
            rate/last_update/ts — для начисления; ts=timestamp целевого блока)
    positions: итерируемое (user, collateral, borrow_shares)
    -> список dict, отсортированный по валовому бонусу вниз
    """
    tba = accrue_borrow_assets(int(market["tba"]), int(market.get("rate") or 0),
                               max(0, int(market.get("ts") or 0) - int(market.get("last_update") or 0)))
    tbs = int(market["tbs"])
    lltv = int(market["lltv"])
    out = []
    for user, collateral, borrow_shares in positions:
        collateral = int(collateral); borrow_shares = int(borrow_shares)
        if borrow_shares <= 0 or collateral <= 0:
            continue
        borrowed, max_borrow, liq, hf = health(collateral, borrow_shares, price, lltv, tba, tbs)
        if not liq:
            continue
        repay, seized, gross, L = liquidation_plan(collateral, borrowed, price, lltv)
        if gross < min_gross_units:
            continue
        out.append({"id": market["id"], "user": user, "collateral": collateral,
                    "borrow_shares": borrow_shares, "borrowed": borrowed, "hf": hf,
                    "repay": repay, "seized": seized, "gross": gross, "lif": L})
    out.sort(key=lambda r: -r["gross"])
    return out
