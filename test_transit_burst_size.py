"""27.08 П.0: размер залпа по транзиту 24 -> 500 + суточный предохранитель.

ЗАМЕР, на котором стоит правка (каскадный блок 50483683, 10 призов):
  победитель: 1 393 tx, 1 043 ноги, 139 на флешблок, охват idx 21..4553 (треть блока)
  мы:         8 tx, 5 долетевших ног из 220, 0.8 на флешблок
Механизм залпа РАБОТАЛ (687 залпов за 8 суток), но был зажат в 58 раз меньше противника.
Цена пробы $0.00115-0.00130 -> залп 500 = $0.57-0.65, 86 залпов/сутки = $49-56 ПРОТИВ
нынешних $271 размазкой: подъём делает огонь ВПЯТЕРО ДЕШЕВЛЕ и в 60 раз плотнее.

Проверяем ПОВЕДЕНИЕ: размер залпа, кап в воздухе, суточный предохранитель и его фолбэк.
"""
import sys, types
sys.path.insert(0, ".")
import chain.fire_engine as fe


class _Eng:
    """Минимальный носитель метода transit_burst — зовём БОЕВОЙ код, а не его копию."""
    def __init__(self, t0=0.0):
        import threading
        self._lock = threading.Lock()
        self._burst_tokens = 0.0
        self.stats = {}
        self.logs = []
        self._t = t0
        self._log = self.logs.append
    def _now(self):
        return self._t
    transit_burst = fe.FireEngine.transit_burst


# --- 1. ПОЗИТИВНЫЙ КОНТРОЛЬ: залп выдаёт заявленный размер --------------------------------
e = _Eng()
got = e.transit_burst()
assert got == fe.ENGINE_TRANSIT_BURST == 500.0, (got, fe.ENGINE_TRANSIT_BURST)
assert e.stats["transit_bursts"] == 1
print("UNIT_OK 1: залп выдаёт %.0f жетонов (было 24 — в 58 раз меньше противника)" % got)

# --- 2. КАП В ВОЗДУХЕ: два залпа подряд не копятся выше MAX --------------------------------
e.transit_burst()
assert e._burst_tokens == fe.ENGINE_TRANSIT_BURST_MAX == 600.0, e._burst_tokens
print("UNIT_OK 2: в воздухе не больше %.0f жетонов даже при потоке событий" % e._burst_tokens)

# --- 3. СУТОЧНЫЙ ПРЕДОХРАНИТЕЛЬ: исчерпан -> фолбэк, а НЕ ноль ----------------------------
e2 = _Eng()
n = 0
while e2.stats.get("burst_day_sent", 0) < fe.ENGINE_BURST_DAY_MAX:
    e2._burst_tokens = 0.0
    e2.transit_burst()
    n += 1
    assert n < 1000, "предохранитель не срабатывает — бюджет не считается"
e2._burst_tokens = 0.0
after = e2.transit_burst()
assert after == fe.ENGINE_BURST_FALLBACK, (
    "после исчерпания бюджета залп обязан упасть к %.0f, а не к %s" % (fe.ENGINE_BURST_FALLBACK, after))
assert after > 0, "механизм не должен умирать молча — фолбэк, а не ноль"
assert any("суточный бюджет залпа исчерпан" in m for m in e2.logs), e2.logs[-3:]
print("UNIT_OK 3: бюджет %.0f исчерпан за %d залпов -> фолбэк %.0f, и это НАПИСАНО в лог"
      % (fe.ENGINE_BURST_DAY_MAX, n, after))

# --- 4. НОВЫЕ СУТКИ СБРАСЫВАЮТ СЧЁТЧИК ----------------------------------------------------
e2._t = 86400.0 * 2
e2._burst_tokens = 0.0
assert e2.transit_burst() == fe.ENGINE_TRANSIT_BURST, "UTC-сутки обязаны сбрасывать бюджет"
print("UNIT_OK 4: смена UTC-суток возвращает полный залп")

# --- 5. ОТКАТ ОДНОЙ РУЧКОЙ ----------------------------------------------------------------
_saved = fe.ENGINE_TRANSIT_BURST
fe.ENGINE_TRANSIT_BURST = 24.0
e3 = _Eng()
assert e3.transit_burst() == 24.0, "SPEC_FIRE_ENGINE_TRANSIT_BURST=24 обязан возвращать прежнее"
fe.ENGINE_TRANSIT_BURST = _saved
print("UNIT_OK 5: откат ручкой в 24 возвращает прежнее поведение бит-в-бит")

# --- 6. АРИФМЕТИКА ЦЕНЫ (чтобы «дешевле» не было словом) ----------------------------------
GAS, ETH, BASE = 76214, 2506.85, 5_000_000
per = GAS * (BASE + 1_800_000) / 1e18 * ETH
day_new = per * fe.ENGINE_TRANSIT_BURST * 86        # 86 залпов/сутки (687 за 8 суток)
assert day_new < 271.0, ("подъём обязан быть ДЕШЕВЛЕ нынешних $271/сутки: получилось $%.0f" % day_new)
print("UNIT_OK 6: залп %.0f x 86/сутки = $%.0f против нынешних $271 размазкой" % (fe.ENGINE_TRANSIT_BURST, day_new))
print("=== ЗАЛП ПО ТРАНЗИТУ: 6/6 ===")
