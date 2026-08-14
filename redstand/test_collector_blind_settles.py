"""#101 БЛОКЕР (ревью 12.08): слепота ОБОИХ узлов не должна запирать наш собственный огонь.

Что было сломано моей же правкой. `batch_receipts` перевели с «вернуть {}» на «поднять
ReceiptsUnavailable», а в `ReceiptCollector.tick` стоял `except -> return 0` ВЫШЕ цикла по
хэшам. Снятие брони #93 (`_on_timeout` -> `volley_gas_settle`) живёт ВНУТРИ цикла. Итог:
оба узла молчат -> брони не снимаются никогда -> гейт `CASCADE_TIER_GAS_USD` закрыт до
рестарта или полуночи UTC. Прежняя редакция самоисцелялась за 180с именно потому, что
пустой словарь не прерывал цикл.

Канон: «незнание обязано закрывать ГАРД», а не запирать собственный огонь. Дедлайн считает
ВРЕМЯ, а не ответы узла.

Дрель идёт ВО ВРЕМЕНИ, а не в моменте: слепота -> дедлайн не истёк (бронь держим) ->
дедлайн истёк (бронь снята) -> узел ожил (работаем дальше).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.fire_engine as fe                                 # noqa: E402
from chain.execute import ReceiptsUnavailable                   # noqa: E402

H = ["0x" + "%064x" % i for i in range(3)]


def _collector(receipts_fn, now_box):
    settled, resolved = [], []
    col = fe.ReceiptCollector(
        receipts_fn=receipts_fn,
        on_result=lambda meta, h, r: resolved.append(h),
        on_timeout=lambda meta, h: settled.append(h),     # ← снятие брони #93 живёт здесь
        now=lambda: now_box[0], sleep=lambda *_: None,
        min_age_sec=0.0, timeout_sec=100.0)
    return col, settled, resolved


# --- 1. ПОЗИТИВНЫЙ КОНТРОЛЬ: при живом узле таймаут снимает бронь ------------------------
now = [1000.0]
col, settled, resolved = _collector(lambda hs: {h: None for h in hs}, now)
for h in H:
    col.track(h, ("leg", None))
col.tick()
assert settled == [], "бронь снята ДО дедлайна — таймаут считает не время"
now[0] = 1101.0
col.tick()
assert sorted(settled) == sorted(H), "живой узел: бронь не снялась по дедлайну: %s" % settled
print("OK 1/4: контроль — при живом узле дедлайн снимает бронь (стенд рабочий)")


# --- 2. БЛОКЕР: оба узла слепы -> бронь ВСЁ РАВНО снимается по дедлайну ------------------
def _blind(hs):
    raise ReceiptsUnavailable("транспорт ConnectionError", "lb.drpc.live")


now = [1000.0]
col, settled, resolved = _collector(_blind, now)
for h in H:
    col.track(h, ("leg", None))
col.tick()
assert settled == [], "при слепоте бронь снята ДО дедлайна — так нельзя, это приговор вслепую"
assert col.pending() == 3, "записи потеряны на отказе узла: pending=%d" % col.pending()
now[0] = 1101.0                                     # дедлайн истёк, узел ВСЁ ЕЩЁ слеп
col.tick()
assert sorted(settled) == sorted(H), \
    "СЛЕПОТА ОБОИХ УЗЛОВ ЗАПИРАЕТ ОГОНЬ: брони не сняты (%s) — гейт закрыт до рестарта" % settled
assert col.pending() == 0, "pending не разгружается при слепоте: %d" % col.pending()
assert col.stats.get("timeouts") == 3, col.stats
assert col.stats.get("batch_fail", 0) >= 2, "отказ узла не посчитан: %s" % col.stats
print("OK 2/4: слепота обоих узлов — брони сняты по дедлайну, pending разгружен, огонь свободен")

# --- 3. Отказ НЕ засчитывается как успешный полл (счётчик не врёт) -----------------------
assert col.stats.get("polls", 0) == 0, "отказ посчитан как полл: %s" % col.stats
print("OK 3/4: отказ узла не увеличивает polls — collector_fallback/polls остаются сравнимы")


# --- 4. ВО ВРЕМЕНИ: узел ожил после слепоты — обычная работа продолжается ----------------
class _Flaky:
    def __init__(self):
        self.blind = True

    def __call__(self, hs):
        if self.blind:
            raise ReceiptsUnavailable("батч не принят 500", "lb.drpc.live")
        return {h: ({"status": "0x1"} if h == H[0] else None) for h in hs}


now = [2000.0]
fl = _Flaky()
col, settled, resolved = _collector(fl, now)
for h in H:
    col.track(h, ("leg", None))
col.tick()                                          # слепота, дедлайн далеко
assert settled == [] and resolved == []
fl.blind = False                                    # узел ожил
col.tick()
assert resolved == [H[0]], "после оживления узла квитанция не доехала: %s" % resolved
assert settled == [], "оживший узел не должен рождать таймауты: %s" % settled
print("OK 4/4: узел ожил после слепоты — резолв продолжается, ложных таймаутов нет")

print("=== #101-блокер закрыт: отказ узла квитанций больше не запирает боевой огонь ===")
