"""ПРИЁМКА §9, ВТОРОЙ РЕЖИМ (задача #133): minOut целей ОДНОГО пула с запасом на n−1.

Предмет. Строгий `assert_disjoint_pools` громко отказывает собрать группу из целей, делящих
пул, — а флагманский случай v6 (каскад ОДНОГО рынка на 8 целей) это ровно ОДИН пул на группу.
Второй режим считает выход каждой цели при ХУДШЕМ порядке (все n−1 прошли раньше) из РЕАЛЬНЫХ
котировок того же пула: worst_i = quote(T) − quote(T − a_i).

Каноны стенда:
 * ТЕСТ НА ПАРАМЕТРАХ, ГДЕ СВОЙСТВО МОЖЕТ СЛОМАТЬСЯ: пул берётся мелкий (константное
   произведение), а группа — такая, что часть целей худший порядок НЕ выживает. На бесконечной
   глубине измерение вернуло бы «всё ок» при любой формуле.
 * ПОЗИТИВНЫЙ КОНТРОЛЬ ФОРМУЛЫ: то же значение считается ВТОРЫМ способом — прямой симуляцией
   пула (последовательные свопы по x*y=k), а не той же формулой. Совпасть обязаны до wei.
 * «НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД»: нет котировок / нет размеров / нет полов / неизвестен пул —
   ОТКАЗ, а не выдуманный запас.
 * КОТИРОВЩИК ИЗ РЕПОЗИТОРИЯ, а не из стенда: `route_depth.make_cumulative_quote_fn` гоняется
   на настоящем `_SWAP_ROUTES` через подменённую пачку квот (шов — транспорт, не логика).

ЗАПУСК: python3 test_v6_same_pool_minout.py    (ненулевой код = провал приёмки)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.board_v6 as bd                      # noqa: E402
import chain.route_depth as rd                   # noqa: E402

RESULTS, FAILS = [], []


def verdict(point, what, ok, extra=""):
    RESULTS.append((point, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
    print("  %s %-8s %s%s" % ("[OK]  " if ok else "[FAIL]", point, what,
                              (" | " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(point)


print("=" * 100)
print("ПРИЁМКА #133: §9 same-pool — minOut с запасом на n−1 свопов того же пула")
print("=" * 100)


# ===================================================================================================
# МОДЕЛЬ ПУЛА. Константное произведение с комиссией — ровно та форма, на которой «выход второго
# свопа считается ПОСЛЕ первого» вообще имеет смысл. Пул НАРОЧНО мелкий: на нём свойство ломается.
# ===================================================================================================
class Pool:
    """Два режима комиссии, и разница между ними — не педантизм, а граница применимости §9.

    fee_in_reserve=False (ДЕФОЛТ, concentrated liquidity — UniV3/Slipstream): комиссия
        снимается со входа и цену НЕ двигает. Все 11 наших роутов такие.
    fee_in_reserve=True  (старый x*y=k): комиссия ОСТАЁТСЯ в резерве и двигает цену.
        На таком пуле разность котировок ЗАВЫШАЕТ маргинальный выход — предел назван в §9.
    """
    FEE_NUM, FEE_DEN = 997, 1000

    def __init__(self, x, y, fee_in_reserve=False):
        self.x0, self.y0 = int(x), int(y)
        self.fee_in_reserve = bool(fee_in_reserve)

    def _swap(self, x, y, amount_in):
        dx = int(amount_in) * self.FEE_NUM // self.FEE_DEN
        dy = y * dx // (x + dx)
        x_new = x + (int(amount_in) if self.fee_in_reserve else dx)
        return x_new, y - dy, dy

    def out_after(self, cumulative_in, amount_in):
        """Выход свопа `amount_in`, если пул уже съел `cumulative_in`. ПРЯМАЯ СИМУЛЯЦИЯ —
        независимый от §9 способ получить то же число."""
        x, y = self.x0, self.y0
        if int(cumulative_in) > 0:
            x, y, _ = self._swap(x, y, cumulative_in)
        if int(amount_in) <= 0:
            return 0
        return self._swap(x, y, amount_in)[2]

    def quote(self, x_in):
        """Кумулятивная котировка: ОДИН своп размера x_in из НАЧАЛЬНОГО состояния — ровно то,
        что отдаёт `quoteExactInput` квотера DEX'а."""
        if int(x_in) <= 0:
            return 0
        dx = int(x_in) * self.FEE_NUM // self.FEE_DEN
        return self.y0 * dx // (self.x0 + dx)


POOL = Pool(x=1_000_000, y=1_000_000)              # мелкий: 4 цели по 100k двигают его сильно
AMTS = [100_000, 90_000, 80_000, 70_000]
TOTAL = sum(AMTS)

# ===================================================================================================
# П1. ФОРМУЛА ПРОВЕРЯЕТСЯ ВТОРЫМ СПОСОБОМ (позитивный контроль, эталон из отдельного расчёта).
#     worst_i из разности котировок ОБЯЗАН совпасть с прямой симуляцией «я последний».
# ===================================================================================================
print("\nП1. worst_out из разности котировок == прямая симуляция «все n−1 раньше меня»")
rows = bd.same_pool_min_outs(AMTS, POOL.quote)
sim = [POOL.out_after(TOTAL - a, a) for a in AMTS]
got = [r["worst_out"] for r in rows]
_dev = [s - w for s, w in zip(sim, got)]
verdict("П1.1", "worst_out — НИЖНЯЯ ГРАНИЦА прямой симуляции «все n−1 раньше меня» и отстаёт "
        "от неё не более чем на floor-зазор (%d wei)" % bd._FLOOR_SLACK_WEI,
        all(0 <= d <= bd._FLOOR_SLACK_WEI + 1 for d in _dev),
        "формула=%s симуляция=%s отклонение=%s" % (got, sim, _dev))
solo = [POOL.quote(a) for a in AMTS]
verdict("П1.2", "цена группировки НАЗВАНА: одиночный выход выше худшего по каждой цели",
        all(r["solo_out"] == s and r["worst_out"] < s for r, s in zip(rows, solo)),
        "solo=%s worst=%s" % (solo, got))
verdict("П1.3", "оценка КОНСЕРВАТИВНА (Д2): Σ worst <= quote(T) — обещаем не больше, чем "
        "пул отдаёт за один проход",
        sum(got) <= POOL.quote(TOTAL), "Σworst=%d quote(T)=%d" % (sum(got), POOL.quote(TOTAL)))

# ГРАНИЦА ПРИМЕНИМОСТИ, НАЙДЕННАЯ ЭТИМ ЖЕ СТЕНДОМ (Д2). На пуле СТАРОГО типа (комиссия остаётся
# в резерве и двигает цену) равенство ломается, и разность котировок ЗАВЫШАЕТ выход — ошибается
# в ОПАСНУЮ сторону. Наших роутов это не касается (все 11 — CL), но предел обязан быть измерен,
# а не объявлен: молчаливое применение §9 к будущему V2-роуту дало бы оптимистичный minOut.
V2POOL = Pool(x=1_000_000, y=1_000_000, fee_in_reserve=True)
_v2_formula = [r["worst_out"] for r in bd.same_pool_min_outs(AMTS, V2POOL.quote)]
_v2_sim = [V2POOL.out_after(TOTAL - a, a) for a in AMTS]
_v2_err_bps = max((f - s) * 10000 // s for f, s in zip(_v2_formula, _v2_sim))
verdict("П1.4", "ПРЕДЕЛ Д2 ИЗМЕРЕН: на пуле с комиссией в резерве формула ЗАВЫШАЕТ выход "
        "(нужен headroom >= %d bp); наши 11 роутов — CL, их это не касается" % _v2_err_bps,
        all(f > s for f, s in zip(_v2_formula, _v2_sim)) and _v2_err_bps <= 100,
        "завышение до %d bp" % _v2_err_bps)
_styles = __import__("collections").Counter(
    r["style"] for r in __import__("chain.execute", fromlist=["x"])._SWAP_ROUTES.values())
verdict("П1.5", "позитивный контроль применимости: в боевой карте роутов НЕТ пулов V2-типа",
        set(_styles) <= {"v1", "02"}, str(dict(_styles)))

# ===================================================================================================
# П2. ПАРАМЕТРЫ, ГДЕ СВОЙСТВО ЛОМАЕТСЯ: полы подобраны так, что часть целей худший порядок НЕ
#     выживает. Наследование v5-логики (minOut от ОДИНОЧНОЙ котировки) здесь молча прошло бы.
# ===================================================================================================
print("\nП2. Кромка: полы между одиночным и худшим выходом — v5-логика прошла бы молча")
FLOORS = [(s + w) // 2 for s, w in zip(solo, got)]        # пол ВЫШЕ худшего, НИЖЕ одиночного
_v5_would_pass = all(s >= f for s, f in zip(solo, FLOORS))
try:
    bd.assert_same_pool_feasible(AMTS, FLOORS, POOL.quote)
    _refused, _why = False, ""
except bd.SamePoolInfeasible as e:
    _refused, _why = True, str(e)
verdict("П2.1", "КРАСНЫЙ У v5-ЛОГИКИ: по ОДИНОЧНОЙ котировке все 4 цели «проходят» — это и есть "
        "дефект по построению", _v5_would_pass)
verdict("П2.2", "ЗЕЛЁНЫЙ У §9: тот же вход отвергнут ГРОМКО, с поимённым перечнем целей и "
        "недостачей", _refused and "не выживают худший порядок" in _why, _why[:150])

# ПОЗИТИВНЫЙ КОНТРОЛЬ ТОГО ЖЕ ГАРДА: полы ниже худшего выхода — тот же вызов ОБЯЗАН пройти,
# иначе «отказывает всегда» неотличимо от «работает».
FLOORS_OK = [w * 90 // 100 for w in got]
rows_ok = bd.assert_same_pool_feasible(AMTS, FLOORS_OK, POOL.quote, headroom_bps=50)
verdict("П2.3", "ПОЗИТИВНЫЙ КОНТРОЛЬ: полы ниже худшего выхода -> группа собирается",
        all(r["ok"] for r in rows_ok) and len(rows_ok) == 4)
verdict("П2.4", "рекомендованный minOut считается от ХУДШЕГО выхода и НЕ опускается ниже пола",
        all(r["floor"] <= r["min_out"] <= r["worst_out"] for r in rows_ok),
        str([(r["floor"], r["min_out"], r["worst_out"]) for r in rows_ok]))
verdict("П2.5", "minOut с запасом СТРОГО НИЖЕ minOut «как в v5» (одиночная котировка) — иначе "
        "запаса нет вовсе",
        all(r["min_out"] < s for r, s in zip(rows_ok, solo)),
        str([(r["min_out"], s) for r, s in zip(rows_ok, solo)]))

# ===================================================================================================
# П3. РАЗМЕР ГРУППЫ, КОТОРЫЙ ПУЛ НЕ ТЯНЕТ ВОВСЕ: котировка на T ревертит -> ОТКАЗ, не ноль.
# ===================================================================================================
print("\nП3. Пул не тянет суммарный вход: реверт ступени = ДАННЫЕ, отказ громкий")


def _q_revert_on_total(x):
    return None if x >= TOTAL else POOL.quote(x)


try:
    bd.same_pool_min_outs(AMTS, _q_revert_on_total)
    _p3 = False
except bd.SamePoolInfeasible as e:
    _p3 = "СУММАРНЫЙ вход" in str(e)
verdict("П3", "ревертнувшая котировка на T -> SamePoolInfeasible (а не worst=0 и тихий арм)", _p3)

# ===================================================================================================
# П4. ЕДИНАЯ ДВЕРЬ: дефолт СТРОГИЙ, режимы не путаются, недостача входа = отказ.
# ===================================================================================================
print("\nП4. assert_group_min_out_safe: дефолт строгий, вход неполон -> отказ (не выдумка)")
POOLKEY = [{"cbBTC/USDC"}] * 4


def _raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


verdict("П4.1", "дефолт режима — disjoint (строгий): включение second-mode остаётся РЕШЕНИЕМ",
        bd.SPEC_FIRE_V6_POOL_MODE == bd.POOL_MODE_DISJOINT, bd.SPEC_FIRE_V6_POOL_MODE)
verdict("П4.2", "строгий режим: цели общего пула НЕ группируются (прежнее поведение цело)",
        _raises(lambda: bd.assert_group_min_out_safe(POOLKEY), bd.PoolOverlap))
verdict("П4.3", "same-pool БЕЗ котировщика -> отказ с названием недостающего входа",
        _raises(lambda: bd.assert_group_min_out_safe(
            POOLKEY, mode="same-pool", amounts_in=AMTS, floors=FLOORS_OK, quote_fn=None),
            bd.SamePoolInfeasible))
verdict("П4.4", "same-pool с неизвестным пулом цели -> отказ (незнание закрывает гард)",
        _raises(lambda: bd.assert_group_min_out_safe(
            [{"p"}, None, {"p"}, {"p"}], mode="same-pool", amounts_in=AMTS, floors=FLOORS_OK,
            quote_fn=POOL.quote), bd.SamePoolInfeasible))
verdict("П4.5", "same-pool: два РАЗНЫХ пула при ОДНОМ котировщике -> отказ (чужая глубина)",
        _raises(lambda: bd.assert_group_min_out_safe(
            [{"a"}, {"a"}, {"b"}, {"b"}], mode="same-pool", amounts_in=AMTS, floors=FLOORS_OK,
            quote_fn=POOL.quote), bd.SamePoolInfeasible))
_rows4 = bd.assert_group_min_out_safe(POOLKEY, mode="same-pool", amounts_in=AMTS,
                                      floors=FLOORS_OK, quote_fn=POOL.quote)
verdict("П4.6", "same-pool на полном входе: строки расчёта отданы вызывающему (их и печь в minOut)",
        len(_rows4) == 4 and all(r["min_out"] for r in _rows4)
        and {r["pool"] for r in _rows4} == {("cbbtc/usdc",)})
# РАЗНЫЕ пулы + словарь котировщиков: цели независимы, считаются пер-пул.
_two = bd.assert_group_min_out_safe([{"a"}, {"a"}, {"b"}, {"b"}], mode="same-pool",
                                    amounts_in=AMTS, floors=[1, 1, 1, 1],
                                    quote_fn={("a",): POOL.quote, ("b",): POOL.quote})
_sim_pair = POOL.out_after(AMTS[1], AMTS[0])
verdict("П4.7", "разные пулы + словарь котировщиков: запас считается ПЕР-ПУЛ (цель #0 видит "
        "перед собой только соседа по СВОЕМУ пулу)",
        0 <= _sim_pair - _two[0]["worst_out"] <= bd._FLOOR_SLACK_WEI + 1
        and _two[0]["worst_out"] > rows[0]["worst_out"],
        "пер-пул=%d (симуляция %d) против всей группы=%d"
        % (_two[0]["worst_out"], _sim_pair, rows[0]["worst_out"]))

# ===================================================================================================
# П5. КОТИРОВЩИК ИЗ РЕПОЗИТОРИЯ: route_depth.make_cumulative_quote_fn на НАСТОЯЩЕЙ карте роутов.
#     Подменена ровно пачка квот (транспорт), путь/квотер/кодирование — боевые.
# ===================================================================================================
print("\nП5. route_depth.make_cumulative_quote_fn: боевой квотер, подменён только транспорт")
from chain.execute import _SWAP_ROUTES, _CBBTC, _UNIV3_USDC   # noqa: E402

_seen = {"calls": None, "quoter": None}


def _fake_batch(rpc, calls):
    _seen["calls"] = calls
    _seen["quoter"] = calls[0][0]
    out = []
    for _to, data in calls:
        from eth_abi import decode as _dec
        _path, size = _dec(["bytes", "uint256"], data[4:])
        out.append((True, POOL.quote(int(size)).to_bytes(32, "big")))
    return out


qf = rd.make_cumulative_quote_fn(None, _CBBTC, _UNIV3_USDC, AMTS, batch_fn=_fake_batch)
_want_sizes = sorted({TOTAL} | {TOTAL - a for a in AMTS} | set(AMTS))
_got_sizes = []
if _seen["calls"]:
    from eth_abi import decode as _dec2
    _got_sizes = sorted(int(_dec2(["bytes", "uint256"], d[4:])[1]) for _t, d in _seen["calls"])
verdict("П5.1", "спрошены РОВНО нужные точки (T, T−aᵢ, aᵢ) и ОДНОЙ пачкой",
        qf is not None and _got_sizes == _want_sizes, "%d квот" % len(_got_sizes))
verdict("П5.2", "квотер взят из боевой карты роутов (%s), путь — её же" % (_seen["quoter"] or "-")[:10],
        _seen["quoter"] in (rd.UNIV3_QUOTER, rd.AERO_QUOTER)
        and _seen["calls"][0][1][4:].hex().find(
            _SWAP_ROUTES[(_CBBTC.lower(), _UNIV3_USDC.lower())]["path"].hex()) >= 0)
_rows5 = bd.same_pool_min_outs(AMTS, qf)
verdict("П5.3", "тот же расчёт на репозиторном котировщике даёт те же числа, что и на модели",
        [r["worst_out"] for r in _rows5] == got,
        str([r["worst_out"] for r in _rows5]))
verdict("П5.4", "роут БЕЗ прямого пути (Kyber-агрегат) -> котировщик None: данных нет, и §9 "
        "обязан отказать, а не выдумать запас",
        rd.make_cumulative_quote_fn(None, "0x" + "ab" * 20, "0x" + "cd" * 20, AMTS,
                                    batch_fn=_fake_batch) is None)

# ===================================================================================================
print("\n" + "=" * 100)
for p, st, what in RESULTS:
    print("%-6s %-8s %s" % (p, st, what))
print("=" * 100)
if FAILS:
    print("ПРИЁМКА #133 ПРОВАЛЕНА: %s" % ", ".join(FAILS))
    sys.exit(1)
print("ПРИЁМКА #133 ПРОШЛА: %d/%d" % (len(RESULTS), len(RESULTS)))
