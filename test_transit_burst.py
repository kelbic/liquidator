"""Ш29: залп по ФАКТУ транзита — двадцать проб за миллисекунды, а не ровным потоком.

ВСКРЫТИЕ блока 49834406 (11.08), самый информативный замер проекта. Призовое окно [436..455]
заняли ДВАДЦАТЬ транзакций ОДНОГО адреса 0x7de3002da4, все на tip 1 000 000 — том же классе,
что и наши пробы. Наши двадцать стояли на 407..426: класс мы угадали верно и окно ОБХВАТИЛИ,
но встали на 29 позиций раньше. Транзит лёг на 435, ровно между их серией и нашей.

Наши пробы шли РОВНЫМ потоком (~4мс между соседними). Конкурент уместил двадцать транзакций
МЕЖДУ двумя нашими соседними — залп за миллисекунды, синхронно с транзитом. Разница не в темпе
и не в цене, а в МОМЕНТЕ.

Крючок — ветка pending-handoff: там жертва флипнута в ФОРМИРУЕМОМ блоке, то есть момент
транзита ИЗВЕСТЕН, а не предсказан.
"""
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.fire_engine as fe                                # noqa: E402

LEGS = ["0x%040x" % i for i in range(110)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        pass


def mk(clk):
    return fe.FireEngine(legs_fn=lambda: LEGS, send_fn=lambda a: "0x1",
                         rate=250.0, now=clk.now, sleep=clk.sleep)


def drain(e, n=80):
    return sum(1 for _ in range(n) if e._take_token())


# --- 1. ГЛАВНОЕ: залп ПЕРЕЖИВАЕТ кламп ведра ---------------------------------------------
# Ведро делает `min(burst, ...)`, и в фазе удержания кап равен 2. Если бы жетоны залпа клали
# в ведро, их срезало бы в тот же миг и правка была бы мертва.
clk = Clock()
e = mk(clk)
e.keepalive()
clk.t += 5.0                                  # горячая истекла -> удержание, ведро пусто
base = drain(e)
assert base <= fe.ENGINE_HOLD_BURST + 1, "предпосылка: в удержании ведро даёт единицы, дало %d" % base
e.transit_burst()
got = drain(e)
assert got >= fe.ENGINE_TRANSIT_BURST - 1, \
    "залп срезан клампом ведра: %d вместо %.0f" % (got, fe.ENGINE_TRANSIT_BURST)
print("OK 1/6: в удержании подряд %d, после залпа %d — кламп ведра залп не съел" % (base, got))

# --- 2. Двадцать подряд — столько же, сколько у победителя в 49834406 ---------------------
assert fe.ENGINE_TRANSIT_BURST >= 20, \
    "залп %.0f меньше двадцати — победитель занял ровно двадцать слотов" % fe.ENGINE_TRANSIT_BURST
print("OK 2/6: размер залпа %.0f >= 20 (у победителя было ровно 20)" % fe.ENGINE_TRANSIT_BURST)

# --- 3. КАП: поток событий не даёт неограниченной экспозиции ------------------------------
clk = Clock()
e = mk(clk)
e.keepalive()
for _ in range(50):
    e.transit_burst()
got = drain(e, 500)
assert got <= fe.ENGINE_TRANSIT_BURST_MAX + fe.ENGINE_BURST + 2, \
    "пятьдесят залпов дали %d жетонов — экспозиция неограничена" % got
print("OK 3/6: 50 залпов подряд -> не больше %.0f жетонов (кап держит)" % fe.ENGINE_TRANSIT_BURST_MAX)

# --- 4. НИ ОДНОГО НОВОГО ТРЕДА (урок инцидента 15:41) ------------------------------------
# Треды на горячем пути пробили cgroup-кап и уронили hot_loop. Залп обязан тратиться теми
# шестнадцатью тредами, что уже есть.
n0 = threading.active_count()
clk = Clock()
e = mk(clk)
e.keepalive()
for _ in range(200):
    e.transit_burst()
    drain(e, 30)
assert threading.active_count() <= n0, \
    "залп плодит треды (%d -> %d) — так мы уронили hot_loop 11.08" % (n0, threading.active_count())
print("OK 4/6: 200 залпов -> НИ ОДНОГО нового треда")

# --- 5. КОНТРОЛЬ: без права огня залп не стреляет ----------------------------------------
# Иначе транзит по чужому фиду поднимал бы флот вне окна.
clk = Clock()
e = mk(clk)
e.transit_burst()
assert not e.armed(), "предпосылка: движок не взведён"
# жетоны есть, но воркер до _take_token не дойдёт — гейт armed() стоит РАНЬШЕ
src = pathlib.Path("chain/fire_engine.py").read_text()
w = src[src.index("def _worker(self):"):]
w = w[:w.index("\n    def ")]
assert w.index("self.armed()") < w.index("_take_token"), \
    "гейт права огня стоит ПОСЛЕ жетонов — залп стрелял бы вне окна"
print("OK 5/6: КОНТРОЛЬ — гейт armed() стоит РАНЬШЕ жетонов, вне окна залп не стреляет")

# --- 6. ОТКАТ и счётчики ------------------------------------------------------------------
clk = Clock()
e = mk(clk)
e.keepalive()
e.transit_burst()
drain(e)
assert e.stats.get("transit_bursts") == 1 and e.stats.get("sent_burst", 0) > 0, e.stats
assert e.transit_burst(0) == 0.0, "нулевой залп обязан быть no-op"
print("OK 6/6: счётчики transit_bursts/sent_burst растут, нулевой залп — no-op")

# --- СТРУКТУРНО: заведён в ветке ПОДТВЕРЖДЁННОГО транзита, ПОСЛЕ права огня ---------------
sfl = pathlib.Path("chain/spec_fire_live.py").read_text()
i = sfl.index("pf = self.pending_fire.pop(feed, None)")
blk = sfl[i:i + 2500]
assert "transit_burst()" in blk, "залп НЕ заведён в ветке подтверждённого транзита — правка мертва"
assert blk.index("_engine_fat_keepalive") < blk.index("transit_burst()"), \
    "залп идёт ДО права огня — жетоны некому тратить (armed() ещё ложно)"
assert "SPEC_FIRE_TRANSIT_BURST_ON" in blk, "нет ручки отката"
print("СТРУКТУРНО: залп заведён на подтверждённом транзите, ПОСЛЕ права огня, с ручкой отката")
print("=== Ш29 ЗАКРЫТ: бьём в момент транзита, а не размазываем поток ===")
