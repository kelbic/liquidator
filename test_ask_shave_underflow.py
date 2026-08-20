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

# ==================================================================================================
# #121 НА ЗАЛОГОВОЙ ГРАНИЦЕ (20.08). Форк-репро: test_fork_route_cbbtc_weth П5 — рынок 0x5dffffc7
# (cbBTC/WETH, lltv 0.915), залог урезан до 3 000 000, полная заявка не влезает, `_cap_repaid_shares`
# её урезает — и БОЕВАЯ tx падает Panic 0x11, потому что кап целился в collateral−1 (запас 1
# единица = 0.0033 bps), а Morpho считает seized уже ПОСЛЕ _accrueInterest, по выросшему tba.
# ==================================================================================================
print()
CAP_BS = 1773455292033251489375494        # borrowShares жертвы 0x1a1e7a3b на блоке 50191666
CAP_COL = 3_000_000                       # залог, подкрученный стендом (полный сейз не влезает)
CAP_TBA = 4_512_206_780_060_186_855_119   # market(0x5dffffc7) на том же блоке
CAP_TBS = 4_350_845_318_692_506_419_524_477
CAP_PRICE = 313141147717123148067449096879623662857680012047
CAP_LIF = 1026167404351114682              # lltv 0.915 -> LIF 1.0261674


def cap_ask(bps=None):
    return _cap_repaid_shares(_shave_ask(CAP_BS, bps), CAP_COL, CAP_PRICE, CAP_LIF,
                              CAP_TBA, CAP_TBS, shave_bps=bps)


# 1. КРАСНЫЙ-НА-БАЗЕ: откат (bps=0) обязан воспроизводить ГРАНИЦУ — сейз впритык к залогу.
_old = cap_ask(0)
_old_seized = _seized_exact(_old, CAP_LIF, CAP_PRICE, CAP_TBA, CAP_TBS)
assert _old > 0 and _old < CAP_BS, "репро обязано попадать в cap-ветку: %d" % _old
assert CAP_COL - _old_seized <= 2, (
    "откат обязан оставлять заявку НА ГРАНИЦЕ (запас %d единиц) — иначе дрель слепа"
    % (CAP_COL - _old_seized))
print("UNIT_OK: откат воспроизводит залоговую границу — сейз %d при залоге %d (запас %d ед. = "
      "%.4f bps), на цепи это Panic 0x11"
      % (_old_seized, CAP_COL, CAP_COL - _old_seized,
         (CAP_COL - _old_seized) * 10_000.0 / CAP_COL))

# 2. ЗЕЛЁНЫЙ: боевой дефолт отодвигает заявку от границы на замеренный запас.
_new = cap_ask(None)
_new_seized = _seized_exact(_new, CAP_LIF, CAP_PRICE, CAP_TBA, CAP_TBS)
_margin_bps = (CAP_COL - _new_seized) * 10_000.0 / CAP_COL
assert _new < _old, "боевой дефолт обязан просить МЕНЬШЕ границы: %d >= %d" % (_new, _old)
assert _margin_bps >= 9.0, "запас %.2f bps меньше ручки %d bps" % (_margin_bps, ASK_SHAVE_BPS)
# ХУДШИЙ ДРЕЙФ ТРАКТА: план доски живёт TTL 900с, потолок ставки Morpho ~800%/год =>
# tba растёт на 2.28 bps, seized ∝ tba. Запас обязан ЭТО покрывать.
_worst_drift_bps = 800.0 * 900.0 / 31_536_000.0 * 100.0
assert _margin_bps > _worst_drift_bps, (
    "запас %.2f bps не покрывает худший дрейф начисления %.2f bps" % (_margin_bps, _worst_drift_bps))
print("UNIT_OK: с боевым запасом сейз %d при залоге %d — отступ %.2f bps против худшего дрейфа "
      "начисления %.2f bps (покрытие %.1fx); недобор пыли %d ед."
      % (_new_seized, CAP_COL, _margin_bps, _worst_drift_bps, _margin_bps / _worst_drift_bps,
         _old_seized - _new_seized))

# 3. ДРЕЙФ tba: то, что на цепи делает _accrueInterest. Заявка, посчитанная по СТАРОМУ tba,
#    обязана пережить рост tba в пределах запаса — и это ровно то, чего не было до фикса.
def _seized_after_accrual(rs, growth_bps):
    return _seized_exact(rs, CAP_LIF, CAP_PRICE, CAP_TBA + CAP_TBA * growth_bps // 10_000, CAP_TBS)


for _g in (1, 2, 5, 8):
    assert _seized_after_accrual(_old, _g) > CAP_COL, (
        "откат обязан переполнять залог уже при дрейфе %d bps" % _g)
    assert _seized_after_accrual(_new, _g) <= CAP_COL, (
        "боевой запас обязан переживать дрейф %d bps" % _g)
print("UNIT_OK: дрейф tba 1..8 bps — откат переполняет залог, боевой запас держит")

# 4. ВНЕ CAP-ВЕТКИ — БИТ-В-БИТ. Эталон считается ПРЕЖНЕЙ формулой (граница = полный collateral),
#    выписанной здесь заново: сравнение с самим собой доказательством не является.
def _legacy_cap(borrow_shares, collateral, price, lif_wad, tba, tbs):
    from chain.simulate import to_shares_down
    from chain.execute import ORACLE_PRICE_SCALE, WAD
    bs2 = int(borrow_shares)
    if bs2 <= 0:
        return 0
    if _seized_exact(bs2, lif_wad, price, tba, tbs) <= collateral:
        return bs2
    cap_assets = (collateral * price // ORACLE_PRICE_SCALE) * WAD // lif_wad
    rs = min(to_shares_down(cap_assets, tba, tbs), bs2)
    for _ in range(4):
        if rs <= 0:
            return 0
        if _seized_exact(rs, lif_wad, price, tba, tbs) <= collateral:
            return rs
        rs -= max(1, rs // 10 ** 6)
    return 0


import hashlib

_table = []
for _bs in (BS, BS // 2, BS // 7, 10 ** 9, 12345, 2, 1, 0):
    for _col in (COLL, COLL * 4, COLL * 100, CAP_COL * 1000):
        _a = _cap_repaid_shares(_bs, _col, PRICE, LIF_WAD, TBA, TBS)
        _b = _legacy_cap(_bs, _col, PRICE, LIF_WAD, TBA, TBS)
        _seized_now = _seized_exact(_bs, LIF_WAD, PRICE, TBA, TBS)
        if _seized_now <= _col:                    # ВНЕ cap-ветки — обязан совпасть до бита
            assert _a == _b == _bs, ("вне cap-ветки поведение изменилось: %d != %d (bs=%d col=%d)"
                                     % (_a, _b, _bs, _col))
            _table.append("%d:%d:%d" % (_bs, _col, _a))
_digest = hashlib.sha256("|".join(_table).encode()).hexdigest()[:16]
assert _table, "контрольная таблица пуста — тест ничего не сравнил"
print("UNIT_OK: вне cap-ветки поведение БИТ-В-БИТ прежнее (%d входов, отпечаток заявок %s)"
      % (len(_table), _digest))

# 5. ОТКАТ РУЧКИ снимает механизм целиком и в этой ветке тоже.
assert cap_ask(0) == _legacy_cap(_shave_ask(CAP_BS, 0), CAP_COL, CAP_PRICE, CAP_LIF,
                                 CAP_TBA, CAP_TBS), "HOT_ASK_SHAVE_BPS=0 обязан вернуть прежнее"
print("UNIT_OK: HOT_ASK_SHAVE_BPS=0 возвращает прежнюю cap-ветку бит-в-бит")

