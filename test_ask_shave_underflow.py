"""ДРЕЛЬ #121: заявка на границе underflow'а Morpho = отказ от готового приза.

ЧТО ВСКРЫТО (форк-репро, блок 49965022 + транзит idx853 блока 49965023, НАШЕЙ боевой калдатой
`0x5c4dafc6...`, цель cbADA/USDC 0xd7520ad1 / заёмщик 0x47e2ad2c, 14.08 14:56:31.947):

    repaidShares == borrowShares      -> eth_call OK, профит $17.55  (приз был НАШ)
    repaidShares == borrowShares + 1  -> Panic 0x11 (underflow)      -> `HOT skip`

Morpho вычитает `position[id][borrower].borrowShares -= repaidShares` БЕЗ zeroFloorSub.
Заявка «весь кэшированный borrowShares» ставит нас РОВНО на границу: любое уменьшение долга
между снимком позиции и симом — хоть на 1 wei — превращает выигрышный приз в `panic-underflow`.
Позиция при этом остаётся ликвидируемой в окне шириной (1-HF)*shares (на этой цели 0.51%), то
есть ВЕСЬ полезный диапазон дрейфа попадал в панику, а не в «healthy».

ПРИЁМКА НА ФОРКЕ (эталон, не самоотчёт) — та же калдата, bshares уменьшен в сторадже:
    shave=0bps:  дрейф 1 wei .. 7e11 -> Panic 0x11 (5/5)
    shave=10bps: дрейф 1 wei .. 1e10 -> OK $17.53 (5/5), 7e11 -> Panic (дрейф > запаса)
Цена запаса 10bps: $17.55 -> $17.53.

Канон: «незнание обязано УМЕНЬШАТЬ заявку». Откат: HOT_ASK_SHAVE_BPS=0.
Запуск: python3 test_ask_shave_underflow.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chain.hotpath import _shave_ask, _cap_repaid_shares, _seized_exact, ASK_SHAVE_BPS
from chain.simulate import to_assets_up

# --- ФАКТИЧЕСКИЕ числа инцидента 14.08 (сверены он-чейн на блоке 49965022) ---
BS = 137345076662251            # position.borrowShares заёмщика — И НАША ЗАЯВКА (кэш был ТОЧЕН)
COLL = 1271496616               # position.collateral
TBA = 1679653013875
TBS = 1622477709572778173
PRICE = 178002420000000000000000000000000000
LIF_WAD = 1126760563380281690   # lltv 0.625 -> LIF 1.12676
WINNER_SHARES = 137345075989228  # победитель попросил на 673023 меньше и ВЗЯЛ приз


def ask(borrow_shares, bps=None):
    """РОВНО композиция боевого пути (prepare_hot / prepare_fast / доска)."""
    return _cap_repaid_shares(_shave_ask(borrow_shares, bps), COLL, PRICE, LIF_WAD, TBA, TBS)


def morpho_underflows(ask_shares, real_shares):
    """Правило Morpho, ЗАМЕРЕННОЕ на форке: `borrowShares -= repaidShares` без zeroFloorSub."""
    return int(ask_shares) > int(real_shares)


# --- 1. ПОЗИТИВНЫЙ КОНТРОЛЬ: дефект воспроизводится на откате (bps=0 = поведение до фикса) ---
ask0 = ask(BS, bps=0)
assert ask0 == BS, f"откат должен вернуть заявку на границу: {ask0} != {BS}"
assert not morpho_underflows(ask0, BS), "на точном кэше граница ещё проходит (форк: OK $17.55)"
assert morpho_underflows(ask0, BS - 1), "ДРЕЛЬ СЛЕПА: bs-1 обязан давать underflow (форк: Panic 0x11)"
assert morpho_underflows(ask0, WINNER_SHARES), "bs победителя обязан давать underflow на старой заявке"
print("UNIT_OK: откат (HOT_ASK_SHAVE_BPS=0) воспроизводит границу — дрейф 1 wei = Panic 0x11")

# --- 2. НЕГАТИВНЫЙ КОНТРОЛЬ: «дрейфа нет» не должно ломаться ни на какой ставке ---
for bps in (0, 1, 10, 50, 100):
    assert not morpho_underflows(ask(BS, bps), BS), f"bps={bps}: чистый кэш обязан исполняться"
print("UNIT_OK: без дрейфа заявка исполняется при любом запасе (тест не «всегда красный»)")

# --- 3. ФИКС: кэш завышен на N ⇒ заявка ВСЁ РАВНО исполняется ---
BPS = ASK_SHAVE_BPS
assert BPS > 0, "боевой дефолт HOT_ASK_SHAVE_BPS обязан быть > 0 (иначе заявка на границе)"
a = ask(BS)
assert a < BS, f"боевая заявка обязана быть СТРОГО меньше долга: {a} !< {BS}"
covered = BS - a
for drift in (1, 673023, 10 ** 8, 10 ** 10, covered):
    real = BS - drift
    assert not morpho_underflows(a, real), (
        f"кэш завышен на {drift} — заявка {a} > реальных {real}, снова Panic 0x11")
print(f"UNIT_OK: запас {BPS}bps = {covered} shares — кэш завышен на 1..{covered} wei, заявка исполняется")

# --- 4. Тот же дрейф на СТАРОЙ заявке обязан падать (иначе п.3 ничего не доказал) ---
for drift in (1, 673023, 10 ** 8, 10 ** 10):
    assert morpho_underflows(ask(BS, bps=0), BS - drift), f"дрейф {drift} на откате обязан паниковать"
print("UNIT_OK: тот же дрейф на откате даёт underflow — фикс, а не совпадение чисел")

# --- 5. Цена запаса ограничена и предсказуема ---
full_assets = to_assets_up(ask(BS, bps=0), TBA, TBS)
shaved_assets = to_assets_up(a, TBA, TBS)
loss_bps = (full_assets - shaved_assets) * 10_000 / full_assets
assert loss_bps <= BPS + 1, f"запас стоит {loss_bps:.2f}bps при ставке {BPS}bps"
assert _seized_exact(a, LIF_WAD, PRICE, TBA, TBS) <= COLL, "сейз обязан остаться в пределах залога"
print(f"UNIT_OK: цена запаса {loss_bps:.2f}bps репэя (репэй {full_assets} -> {shaved_assets})")

# --- 6. Пыль не обнуляется: 0 shares Morpho отвергает (INCONSISTENT_INPUT) ---
for bs in (1, 2, 3, 999, 10_000):
    assert _shave_ask(bs) >= 1, f"пыль bs={bs} обнулена — Morpho отвергнет заявку целиком"
assert _shave_ask(0) == 0, "нулевой долг обязан оставаться нулём"
print("UNIT_OK: пылевые позиции не обнуляются запасом")

# --- 7. ШОВ НИЖЕ: боевые точки сайзинга реально ходят через _shave_ask (не заглушка этажом выше) ---
SITES = [
    ("chain/hotpath.py", "_cap_repaid_shares(_shave_ask(int(borrow_shares)), int(collateral_dbg)", "prepare_hot"),
    ("chain/hotpath.py", "_cap_repaid_shares(_shave_ask(int(borrow_shares)), int(collateral)", "prepare_fast"),
    ("chain/spec_fire_live.py", "_cap_repaid_shares(_shave_ask(int(bs))", "доска/spec-fire"),
    ("chain/execute.py", "repaid_shares = _shave_ask(int(borrow_shares))", "block-loop prepare_liquidation"),
]
root = os.path.dirname(os.path.abspath(__file__))
for path, needle, name in SITES:
    src = open(os.path.join(root, path), encoding="utf-8").read()
    assert needle in src, f"{name} ({path}) не проходит через _shave_ask — заявка снова на границе"
print("UNIT_OK: все 4 боевые точки сайзинга заявки идут через _shave_ask")

# --- 8. Откат снимает МЕХАНИЗМ, а не параметр ---
assert _shave_ask(BS, 0) == BS, "HOT_ASK_SHAVE_BPS=0 обязан возвращать прежнее поведение"
print("UNIT_OK: HOT_ASK_SHAVE_BPS=0 снимает механизм целиком")

print("=== ДРЕЛЬ #121 ЗЕЛЁНАЯ: заявка строго ниже долга, дрейф кэша больше не отдаёт приз ===")
