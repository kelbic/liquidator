"""27.08 ПОВЕДЕНЧЕСКИЙ стенд на _top_fat под новым рангом (линза: у половины правки было
покрытие «грепом по исходнику», а не поведением).

Закрывает три случая, которые линза назвала регрессами прежней редакции:
  (1) НИЧЬЯ НА НУЛЕ: когда ни одна цель не перекрывается кольцом, все ключи равны, и устойчивая
      сортировка отдавала top-1 по ПОРЯДКУ ВСТАВКИ — произвольную цель. Это НОРМАЛЬНЫЙ случай:
      замер линзы по 1975 строкам hold показал, что жирные цели практически никогда не лежат
      внутри одного толчка (min 1.25%, p50 2.42%).
  (2) СЛЕПОЙ HF КАК ПРЕМИЯ: вес 1.0 у цели без HF обходил ДОСЯГАЕМЫЙ жир.
  (3) БЕЗ ЗАМЕРА ранг обязан деградировать к прежнему «чисто по долгу».
"""
import sys, time, types
sys.path.insert(0, ".")
import chain.push_scale as ps
import chain.spec_fire_live as sfl

ps.STATE_PATH = "/tmp/push_scale_tiebreak.json"


class _Shared:
    def __init__(self, groups, debt_by, hf_by, by_id):
        import threading
        self.lock = threading.Lock()
        self.groups, self.debt_by, self.hf_by, self.by_id = groups, debt_by, hf_by, by_id


def _mk(hf_by, debt_by, order):
    # collateral_token — ТИКЕР, который _label_of сводит к фиду ("cbBTC" -> "BTC"); с "BTC"
    # рынок не сматчился бы с фидом и _top_fat молча вернул бы пусто (поймано первым прогоном).
    m = types.SimpleNamespace(collateral_token="cbBTC", loan_token="USDC", bonus=0.05)
    sh = _Shared({"0xm": list(order)}, debt_by, hf_by, {"0xm": m})
    sf = object.__new__(sfl.SpecFireLive)
    sf.shared = sh
    sf._unbuildable, sf._unbuildable_mid = {}, {}
    sf._now = lambda: time.time()
    return sf


FAT, SMALL = "0xFAT", "0xSMALL"
DEBT = {("0xm", FAT): 5_000_000.0, ("0xm", SMALL): 600_000.0}

# --- 1. НИЧЬЯ НА НУЛЕ: обе цели вне кольца -> побеждает ДОЛГ, а не порядок обхода ---------
ps._RINGS.clear()
now = time.time()
for i in range(40):
    ps.note_push("BTC", 0.20, now=now - i)          # кольцо знает только мелкие толчки
hf = {("0xm", FAT): 1.0101, ("0xm", SMALL): 1.0204}   # 1.0% и 2.0% — обе вне 0.20%
assert ps.p_flip("BTC", 0.010) == 0.0 and ps.p_flip("BTC", 0.020) == 0.0, "предпосылка: обе вне"
for order in ([SMALL, FAT], [FAT, SMALL]):            # ПОРЯДОК ОБХОДА МЕНЯЕМ
    sf = _mk(hf, DEBT, order)
    tgts, _, _ = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
    assert [t[1] for t in tgts] == [FAT], (
        "ничья на нуле уехала в порядок вставки: order=%s -> %s" % (order, tgts))
print("UNIT_OK 1: ничья на нуле разрешается ДОЛГОМ при любом порядке обхода")

# --- 2. СЛЕПОЙ HF НЕ ПРЕМИЯ ---------------------------------------------------------------
# Фикстура ДОЛЖНА создавать конфликт, иначе тест зелен при любом весе слепого (первая редакция
# этого стенда была именно такой и негативный контроль её не покрасил — поймано 27.08).
# Условие конфликта: вес известной цели < долг_слепой / долг_известной = 600k/5M = 0.12.
# Берём кольцо, где жир на 2.0% перекрывается лишь 2 пушами из 92 (вес 0.022 < 0.12):
#   вес слепого 1.0        -> слепая мелочь $600k x 1.00 = 600k  >  жир $5M x 0.022 = 110k  (РЕГРЕСС)
#   вес слепого = медиана  -> слепая $600k x 0.022 = 13k         <  жир 110k                (верно)
ps._RINGS.clear()
for i in range(90):
    ps.note_push("BTC", 0.20, now=now - i)
for i in range(2):
    ps.note_push("BTC", 2.50, now=now - i)
_w_fat = ps.p_flip("BTC", 0.020)
assert 0.0 < _w_fat < 600_000.0 / 5_000_000.0, ("фикстура не создаёт конфликта", _w_fat)
hf2 = {("0xm", FAT): 1.0204}                           # жир 2.0% (редко); SMALL — БЕЗ HF
sf = _mk(hf2, DEBT, [SMALL, FAT])
tgts, _, _ = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
assert [t[1] for t in tgts] == [FAT], (
    "слепая мелочь забрала слот у жира (вес слепого = премия): %s, вес жира %.4f" % (tgts, _w_fat))
print("UNIT_OK 2: слепой HF получает медианный вес (%.3f), а не 1.0 — премии за незнание нет" % _w_fat)

# --- 3. БЕЗ ЗАМЕРА — прежний ранг по долгу ----------------------------------------------
ps._RINGS.clear()                                      # ни одного замера -> p_flip = None
sf = _mk({("0xm", FAT): 1.02, ("0xm", SMALL): 1.001}, DEBT, [SMALL, FAT])
tgts, _, _ = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
assert [t[1] for t in tgts] == [FAT], ("без замера ранг обязан быть чисто по долгу: %s" % (tgts,))
print("UNIT_OK 3: без замера — прежнее поведение бит-в-бит (жир по долгу)")

# --- 4. С ЗАМЕРОМ: досягаемая мелочь ОБХОДИТ недосягаемый жир (то, ради чего правка) -----
ps._RINGS.clear()
for i in range(90):
    ps.note_push("BTC", 0.20, now=now - i)
for i in range(2):
    ps.note_push("BTC", 2.50, now=now - i)             # редкий крупный толчок: 2/92
hf4 = {("0xm", FAT): 1.0204, ("0xm", SMALL): 1.001}    # жир 2.0% (редко), мелочь 0.1% (всегда)
sf = _mk(hf4, DEBT, [FAT, SMALL])
tgts, _, _ = sf._top_fat("BTC", "down", max_dist=0.03, k=1)
w_fat, w_small = ps.p_flip("BTC", 0.020), ps.p_flip("BTC", 0.001)
assert 0.0 < w_fat < 0.05 and w_small == 1.0, (w_fat, w_small)
assert [t[1] for t in tgts] == [SMALL], (
    "ожидание: $%.0f x %.3f против $%.0f x %.3f -> %s" % (5e6, w_fat, 6e5, w_small, tgts))
print("UNIT_OK 4: досягаемая мелочь ($600k x %.2f) обходит редко-досягаемый жир ($5M x %.3f)"
      % (w_small, w_fat))
print("=== _top_fat ПОВЕДЕНЧЕСКИ: 4/4 ===")
