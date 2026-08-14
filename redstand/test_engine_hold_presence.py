"""Ш26: стена стоит ДОЛГО и ТОНКО, а не вспыхивает на 10 секунд.

ЗАМЕР, породивший правку (11.08, блоки 49820459..49820472). Наша стена жила 5 блоков /
10 секунд: 1155 проб, по 231 на блок — при том что ПОБЕДИТЕЛЮ хватает восьми. Потом 8 блоков /
16 секунд пустоты, и ровно там, в 49820471 и 49820472, прошли 30 ликвидаций. Наших tx в блоках
с деньгами — НОЛЬ. Причина: право огня гасло через ENGINE_TTL_SEC=1.5с после последнего тика,
то есть стена была привязана к событию и умирала вместе с ним.

Отсюда же и 901 отказ «over rate limit»: лимит приёма мы выжигали САМИ, вываливая боезапас за
десять секунд.

ГЛАВНОЕ, ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ, — НЕПРЕРЫВНОСТЬ (замечание kelbic). Средний темп 4/с ничего не
гарантирует: с общим бурстом 40 шестнадцать тредов выплеснут накопленное разом, и «тонкий
поток» пойдёт рывками. Пауза, попавшая на транзит, обнуляет присутствие ровно так же, как
обнуляла его вспышка. Поэтому меряем МАКСИМАЛЬНУЮ ПАУЗУ, а не сумму.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.fire_engine as fe                                # noqa: E402

LEGS = ["0x%040x" % i for i in range(110)]


class Clock:
    """Ручные часы: тест обязан быть детерминированным и мгновенным."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):                  # треды спят «мгновенно», время двигает тест
        pass


def run(engine, clk, seconds, step=0.05, sends=None, threads=16):
    """Прокрутить движок на wall-clock `seconds` шагами `step`.

    За шаг делаем `threads` попыток — боевой движок держит 16 тредов, и стенд с ОДНОЙ попыткой
    за шаг ограничивал бы пропускную способность собственной сеткой, а не гейтами продукта
    (поймано первым прогоном: горячая фаза дала 33 отправки вместо сотен)."""
    for _ in range(int(seconds / step)):
        clk.t += step
        for _t in range(threads):
            if not engine.armed():
                break
            if not engine._take_token():
                break
            if engine._next_leg() is None:
                break
            sends.append(clk.t)


def gaps(ts):
    return [round(b - a, 3) for a, b in zip(ts, ts[1:])]


# ---------------------------------------------------------------- 1. ГЛАВНОЕ
clk = Clock()
sends = []
eng = fe.FireEngine(legs_fn=lambda: LEGS, send_fn=lambda a: "0x1",
                    rate=250.0, now=clk.now, sleep=clk.sleep)
eng.keepalive(target=("XRP", "mid", "b", 1.0))
run(eng, clk, 2.0, sends=sends)                     # горячая фаза ~1.5с
hot_n = len(sends)
sends.clear()
run(eng, clk, 30.0, sends=sends)                    # тики ПРЕКРАТИЛИСЬ
assert hot_n > 100, "горячая фаза не отработала: %d" % hot_n
assert sends, "ПОСЛЕ ТИКОВ ОГОНЬ УМЕР — дефект Ш26 не закрыт, стена снова вспышка"
print("OK 1/6: после прекращения тиков присутствие ЖИВО (%d проб за 30с против 0 раньше)"
      % len(sends))

# ------------------------------------------- 2. НЕПРЕРЫВНОСТЬ (замечание kelbic)
g = gaps(sends)
mx = max(g)
assert mx <= 0.55, "пауза %.2fс — рывки, а не поток; блок Base 2с, дыра съест транзит" % mx
# и поток не должен быть «пачка в начале, потом тишина»
first_half = sum(1 for t in sends if t < sends[0] + 15.0)
second_half = len(sends) - first_half
assert min(first_half, second_half) > 0.3 * max(first_half, second_half), \
    "поток неровный: %d проб в первой половине против %d во второй" % (first_half, second_half)
print("OK 2/6: поток РОВНЫЙ — макс. пауза %.2fс, распределение %d/%d по половинам окна"
      % (mx, first_half, second_half))

# ------------------------------------------- 3. Темп удержания — тонкий, а не боевой
rate = len(sends) / 30.0
# Порог берём ОТ КОНСТАНТЫ, а не хардкодом: Ш24 поднял темп 4 -> 16 (флот разложен на четыре
# тира, в классе оказывается четверть ног, и чтобы попавший тир дал 8 проб на блок, общий темп
# обязан быть вчетверо выше). Хардкод здесь дал бы ложное падение на каждой настройке темпа.
assert fe.ENGINE_HOLD_RATE * 0.5 <= rate <= fe.ENGINE_HOLD_RATE * 1.5, \
    "темп удержания %.1f/с против настроенных %.0f/с" % (rate, fe.ENGINE_HOLD_RATE)
assert rate < 100.0, "темп удержания перестал быть тонким — снова выжжем лимит приёма"
print("OK 3/6: темп удержания %.1f проб/с при настроенных %.0f (боевой был 115/с — им мы "
      "выжигали лимит приёма)" % (rate, fe.ENGINE_HOLD_RATE))

# ------------------------- 4. СЦЕНАРИЙ ЗАМЕРА: покрываем блок, где были деньги
# Тики кончились в 06:37:53, ликвидации в 06:38:09 и 06:38:11 = +16с и +18с.
clk2 = Clock()
s2 = []
eng2 = fe.FireEngine(legs_fn=lambda: LEGS, send_fn=lambda a: "0x1",
                     rate=250.0, now=clk2.now, sleep=clk2.sleep)
eng2.keepalive()
t_stop = clk2.t
run(eng2, clk2, 25.0, sends=s2)
money = [t for t in s2 if t_stop + 15.0 <= t <= t_stop + 19.0]
assert money, "в окне +16..+18с (там были 30 ликвидаций) проб НЕТ — правка не решает задачу"
print("OK 4/6: в окне +16..+18с после конца тиков стоит %d проб (было 0, деньги ушли мимо)"
      % len(money))

# ------------------------------------------- 5. КОНТРОЛЬ: удержание ИСТЕКАЕТ
clk3 = Clock()
s3 = []
eng3 = fe.FireEngine(legs_fn=lambda: LEGS, send_fn=lambda a: "0x1",
                     rate=250.0, now=clk3.now, sleep=clk3.sleep)
eng3.keepalive()
run(eng3, clk3, fe.ENGINE_HOLD_SEC + 10.0, sends=s3)
s3.clear()
run(eng3, clk3, 20.0, sends=s3)
assert not s3, "удержание НЕ истекает — флот стреляет вечно, это дыра в деньгах"
print("OK 5/6: КОНТРОЛЬ — удержание истекает через %.0fс, вечной стрельбы нет"
      % fe.ENGINE_HOLD_SEC)

# ------------------------------------------- 6. КОНТРОЛЬ: stop/disarm гасят НЕМЕДЛЕННО
clk4 = Clock()
s4 = []
eng4 = fe.FireEngine(legs_fn=lambda: LEGS, send_fn=lambda a: "0x1",
                     rate=250.0, now=clk4.now, sleep=clk4.sleep)
eng4.keepalive()
run(eng4, clk4, 3.0, sends=s4)
assert s4
eng4.disarm()
s4.clear()
run(eng4, clk4, 10.0, sends=s4)
assert not s4, "disarm не гасит удержание — команда «стоп» не останавливает флот"
eng4.keepalive()
eng4.stop()
s4.clear()
run(eng4, clk4, 10.0, sends=s4)
assert not s4, "stop не гасит удержание — флот стрелял бы ещё 3 минуты после остановки"
print("OK 6/6: КОНТРОЛЬ — disarm и stop гасят удержание немедленно")

# ------------------------------------------- СТРУКТУРНО
src = pathlib.Path("chain/fire_engine.py").read_text()
i = src.index("def _take_token(")
blk = src[i:src.index("\n    def ", i + 10)]
assert "ENGINE_HOLD_BURST" in blk, \
    "у фазы удержания общий бурст 40 — поток пойдёт рывками (замечание kelbic)"
_init = src[src.index("def __init__(self, legs_fn"):]
_init = _init[:_init.index("\n    def ")]
assert "self._hold_until = 0.0" in _init, \
    "_hold_until не инициализирован в КОНСТРУКТОРЕ — AttributeError в бою при первом keepalive"
print("СТРУКТУРНО: у удержания свой бурст; поле инициализировано в конструкторе")
print("=== Ш26 ЗАКРЫТ: стена стоит %.0fс тонким потоком вместо вспышки на 10с ==="
      % fe.ENGINE_HOLD_SEC)
