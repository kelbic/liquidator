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

from chain.hotpath import (_shave_ask, _cap_repaid_shares, _seized_exact, ASK_SHAVE_BPS,
                           CAP_SHAVE_MAX_BPS)
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
#
# ОКНО НАЧИСЛЕНИЯ — СВОЙСТВО РЫНКА, А НЕ НАШЕГО TTL. Числа ниже сняты С ЦЕПИ на блоке 50191666
# (форк-стенд проверяет их ЖИВЫМИ, здесь они заморожены как эталон юнита):
#   elapsed = block.timestamp - market.lastUpdate = 664с (замер стенда НА ЭТОМ блоке; линза на
#   сутки позже видела 4714с — окно живое и растёт, пока рынок никто не трогает),
#   ставка IRM 1.666% годовых => дрейф tba 0.0035 bps.
#   Для сравнения LBTC/cbBTC: 9.8 СУТОК без accrual (замер линзы) — при ставке у потолка кривой
#   та же формула даёт тысячи bps, и ветка ПРАВИЛЬНО не строится.
# ==================================================================================================
print()
CAP_BS = 1773455292033251489375494        # borrowShares жертвы 0x1a1e7a3b на блоке 50191666
CAP_COL = 3_000_000                       # залог, подкрученный стендом (полный сейз не влезает)
CAP_TBA = 4_512_206_780_060_186_855_119   # market(0x5dffffc7) на том же блоке
CAP_TBS = 4_350_845_318_692_506_419_524_477
CAP_PRICE = 313141147717123148067449096879623662857680012047
CAP_LIF = 1026167404351114682              # lltv 0.915 -> LIF 1.0261674
CAP_ELAPSED = 664                          # block.timestamp - market.lastUpdate, с цепи
                                           # (форк-стенд перепроверяет ЖИВЫМ чтением)
CAP_RATE_PS = int(0.01666 / 31_536_000 * 1e18)  # 1.666% годовых в WAD/сек (borrowRateView)
LIVE_DRIFT_BPS = 10_000.0 * CAP_RATE_PS * CAP_ELAPSED / 1e18      # без запаса: чистый дрейф


def cap_ask(bps=None, drift=(CAP_ELAPSED, CAP_RATE_PS)):
    return _cap_repaid_shares(_shave_ask(CAP_BS, bps), CAP_COL, CAP_PRICE, CAP_LIF,
                              CAP_TBA, CAP_TBS, shave_bps=bps,
                              drift_fn=(lambda: drift) if drift else None)


def cap_seized(rs, tba=CAP_TBA):
    return _seized_exact(rs, CAP_LIF, CAP_PRICE, tba, CAP_TBS)


# 1. КРАСНЫЙ-НА-БАЗЕ: ПРЕЖНЯЯ ФОРМУЛА (выписана ниже как _legacy_cap) обязана воспроизводить
#    ГРАНИЦУ — сейз впритык к залогу. Именно она стояла до фикса; ручка HOT_ASK_SHAVE_BPS=0
#    больше не эквивалентна ей ПО ПОСТРОЕНИЮ: живой дрейф — измеренная величина, и откат
#    фиксированной части его не снимает (см. п.6).
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


_old = _legacy_cap(_shave_ask(CAP_BS, 0), CAP_COL, CAP_PRICE, CAP_LIF, CAP_TBA, CAP_TBS)
_old_seized = cap_seized(_old)
assert _old > 0 and _old < CAP_BS, "репро обязано попадать в cap-ветку: %d" % _old
assert CAP_COL - _old_seized <= 2, (
    "откат обязан оставлять заявку НА ГРАНИЦЕ (запас %d единиц) — иначе дрель слепа"
    % (CAP_COL - _old_seized))
print("UNIT_OK: откат воспроизводит залоговую границу — сейз %d при залоге %d (запас %d ед. = "
      "%.4f bps), на цепи это Panic 0x11"
      % (_old_seized, CAP_COL, CAP_COL - _old_seized,
         (CAP_COL - _old_seized) * 10_000.0 / CAP_COL))

# 2. ЗЕЛЁНЫЙ: боевой дефолт отодвигает заявку от границы, и запас ПОКРЫВАЕТ ЖИВОЕ ОКНО.
_new = cap_ask(None)
_new_seized = cap_seized(_new)
_margin_bps = (CAP_COL - _new_seized) * 10_000.0 / CAP_COL
assert _new < _old, "боевой дефолт обязан просить МЕНЬШЕ границы: %d >= %d" % (_new, _old)
assert _margin_bps >= LIVE_DRIFT_BPS * 1.5, (
    "запас %.4f bps не покрывает живой дрейф %.4f bps с множителем 1.5"
    % (_margin_bps, LIVE_DRIFT_BPS))
print("UNIT_OK: с боевым запасом сейз %d при залоге %d — отступ %.2f bps против ЖИВОГО дрейфа "
      "окна (%dс × %.2f%% APR = %.4f bps), покрытие %.0fx; недобор пыли %d ед."
      % (_new_seized, CAP_COL, _margin_bps, CAP_ELAPSED,
         CAP_RATE_PS * 31_536_000 / 1e18 * 100, LIVE_DRIFT_BPS,
         _margin_bps / max(LIVE_DRIFT_BPS, 1e-9), _old_seized - _new_seized))

# 3. ДРЕЙФ tba — ровно то, что на цепи делает _accrueInterest ПЕРЕД расчётом seized.
#    Проверяем на ЖИВОМ окне: заявка обязана пережить рост tba на посчитанную величину.
_live_growth = CAP_TBA * CAP_RATE_PS * CAP_ELAPSED // 10 ** 18
assert cap_seized(_old, CAP_TBA + _live_growth) > CAP_COL, (
    "откат обязан переполнять залог уже на ЖИВОМ дрейфе окна (рост tba %d)" % _live_growth)
assert cap_seized(_new, CAP_TBA + _live_growth) <= CAP_COL, "боевой запас обязан держать живое окно"
for _mult in (2, 5, 10, 100):
    if cap_seized(_new, CAP_TBA + _live_growth * _mult) > CAP_COL:
        print("UNIT_OK: живой дрейф держится, предел запаса — рост окна в %dx (дальше ветка "
              "обязана НЕ строиться по CAP_SHAVE_MAX_BPS)" % _mult)
        break
else:
    print("UNIT_OK: живой дрейф держится с запасом >100x окна")

# 4. НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: нет живого окна / окно слишком велико -> ветка НЕ строится.
assert cap_ask(None, drift=None) == 0, "без живого окна кап-ветка строиться не имеет права"
assert cap_ask(None, drift=(CAP_ELAPSED, 0)) > 0, "нулевая ставка — законное окно, ветка строится"
# ХУДШИЙ СЛУЧАЙ КОРРЕЛИРУЕТ С САМОЙ ВЕТКОЙ: подводный рынок = утилизация под 100% = ставка у
# ПОТОЛКА адаптивной кривой, и на нём же дольше всего никто не трогает accrual. Берём замеренное
# окно LBTC/cbBTC (9.8 суток) со ставкой потолка — ровно та комбинация, ради которой нужен кэп.
_lbtc_elapsed = int(9.8 * 86400)                       # LBTC/cbBTC: 9.8 суток без accrual
_lbtc_rate = int(8.0 / 31_536_000 * 1e18)              # 800% годовых — потолок кривой Morpho
_lbtc_drift = 10_000.0 * _lbtc_rate * _lbtc_elapsed / 1e18 * 1.5
assert _lbtc_drift > CAP_SHAVE_MAX_BPS, "стенд обязан брать окно, которое РЕАЛЬНО пробивает кэп"
assert cap_ask(None, drift=(_lbtc_elapsed, _lbtc_rate)) == 0, (
    "окно %d с дрейфом %.1f bps > кэпа %.0f bps обязано ЗАКРЫТЬ ветку"
    % (_lbtc_elapsed, _lbtc_drift, CAP_SHAVE_MAX_BPS))
# ...и НЕ переусердствовать: то же окно при спокойной ставке в кэп укладывается, ветка живёт.
_calm_rate = int(0.05 / 31_536_000 * 1e18)
_calm_drift = 10_000.0 * _calm_rate * _lbtc_elapsed / 1e18 * 1.5
assert 0 < _calm_drift < CAP_SHAVE_MAX_BPS and cap_ask(None, drift=(_lbtc_elapsed, _calm_rate)) > 0, (
    "спокойная ставка на том же окне (%.1f bps) не имеет права закрывать ветку" % _calm_drift)
print("UNIT_OK: нет окна -> ветки нет; окно 9.8 суток при ставке потолка (дрейф %.0f bps > кэпа "
      "%.0f) -> ветки нет; то же окно при 5%% APR (%.1f bps) ветку сохраняет; нулевая ставка — "
      "законное знание" % (_lbtc_drift, CAP_SHAVE_MAX_BPS, _calm_drift))

# 5. ВНЕ CAP-ВЕТКИ — БИТ-В-БИТ. Эталон — та же ПРЕЖНЯЯ формула `_legacy_cap` (п.1): сравнение
#    с самим собой доказательством не является.
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

# 6. ОТКАТ РУЧКИ снимает ФИКСИРОВАННУЮ часть, но НЕ живой дрейф — и это назван­ное решение:
#    дрейф не подобран, а ИЗМЕРЕН у рынка, и снимать его ручкой значит вернуть панику.
_rollback = cap_ask(0)
_rb_margin = (CAP_COL - cap_seized(_rollback)) * 10_000.0 / CAP_COL
assert _old < _rollback or _rb_margin >= LIVE_DRIFT_BPS, (
    "откат обязан остаться НЕ ХУЖЕ живого дрейфа: запас %.4f bps против дрейфа %.4f"
    % (_rb_margin, LIVE_DRIFT_BPS))
assert _rollback > cap_ask(None), "откат обязан просить БОЛЬШЕ боевого дефолта (запас меньше)"
print("UNIT_OK: HOT_ASK_SHAVE_BPS=0 снимает фиксированную часть (запас %.4f bps = живой дрейф "
      "с множителем), но НЕ возвращает границу — прежняя формула давала %.4f bps"
      % (_rb_margin, (CAP_COL - _old_seized) * 10_000.0 / CAP_COL))
