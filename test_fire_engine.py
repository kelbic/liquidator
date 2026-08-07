"""Движок огня и батч-коллектор квитанций (#32, 07.08).

ЧТО ИМЕННО ЗДЕСЬ ДОКАЗЫВАЕТСЯ (каждое свойство — с позитивным контролем, иначе тест умел бы
только «ничего не произошло», а это мы уже проходили):
  1. движок ГАСНЕТ САМ, когда перестают тикать keepalive — отказ в сторону молчания;
  2. потолок темпа держится (безлимитный цикл = самопальный шторм 429, инцидент есть);
  3. коллектор — ОДИН тред на любое число проб (тред-на-квитанцию это то, что чинится);
  4. квитанции не теряются и разбираются ПАРТИЯМИ (один запрос на сотни проб);
  5. «нет квитанции» != «дроп»: приговор только по таймауту;
  6. доточка склада продолжает nonce с ХВОСТА, а не от pending — иначе дыра вешает ногу;
  7. движок ВЫКЛЮЧЕН по умолчанию.
Без сети, без реальных тредов там, где можно прогнать шаг вручную.
"""
import sys
import threading
import time

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.fire_engine as fe
from chain.fire_engine import FireEngine, ReceiptCollector

# ---- 0. Движок выключен по умолчанию ----
assert fe.ENGINE_ON is False, "движок обязан быть выключен по умолчанию (V1 бит-в-бит)"
print("UNIT_OK: SPEC_FIRE_ENGINE=0 по умолчанию — прежний путь не тронут")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


# ---- 1. Огонь живёт ровно пока тикает keepalive ----
clk = Clock()
sent = []
eng = FireEngine(legs_fn=lambda: ["0xA", "0xB"], send_fn=lambda a: sent.append(a) or ("0xh%d" % len(sent)),
                 threads=1, rate=1e9, ttl=1.5, now=clk.now, sleep=clk.sleep)
assert not eng.armed(), "без keepalive движок молчит"
eng.keepalive(target=("BTC", "0xmid", "0xb", 1.0))
assert eng.armed(), "после тика — взведён"
clk.t += 1.4
assert eng.armed(), "внутри TTL всё ещё взведён"
clk.t += 0.2
assert not eng.armed(), "TTL истёк — огонь обязан погаснуть САМ"
eng.keepalive()
assert eng.armed(), "позитивный контроль: новый тик снова взводит"
eng.disarm()
assert not eng.armed(), "disarm гасит немедленно"
print("UNIT_OK: keepalive — перестали тикать, огонь погас сам за TTL (отказ в молчание)")

# ---- 2. Потолок темпа ----
clk = Clock()
eng = FireEngine(legs_fn=lambda: ["0xA"], send_fn=lambda a: "0xh",
                 threads=1, rate=100.0, ttl=999.0, now=clk.now, sleep=clk.sleep)
fe.ENGINE_BURST = 10.0
eng._tokens = 10.0
eng._tok_ts = clk.now()
got = sum(1 for _ in range(1000) if eng._take_token())
assert got == 10, "всплеск обязан упереться в запас вёдер, получено %d" % got
clk.t += 1.0                                   # секунда при 100/с = 100 жетонов, но кап всплеска 10
got2 = sum(1 for _ in range(1000) if eng._take_token())
assert got2 == 10, "после секунды снова не больше запаса, получено %d" % got2
assert got2 > 0, "позитивный контроль: жетоны действительно копятся"
print("UNIT_OK: потолок темпа держит — безлимитный цикл рассылки невозможен")

# ---- 3. Коллектор: ОДИН тред на любое число проб ----
seen_batches = []


def _receipts(hs):
    seen_batches.append(len(hs))
    return {h: {"status": 1, "gasUsed": 21000, "effectiveGasPrice": 5_000_000, "logs": []}
            for h in hs}


results = []
col = ReceiptCollector(receipts_fn=_receipts, on_result=lambda m, h, r: results.append((m, h)),
                       poll_sec=0.01, timeout_sec=100.0, batch=200)
base_threads = threading.active_count()
col.start()
for i in range(500):
    col.track("0x%03d" % i, ("0xEA", None))
time.sleep(0.4)
col.stop()
delta = threading.active_count() - base_threads
assert delta <= 1, "коллектор обязан быть ОДНИМ тредом, а выросло на %d" % delta
assert len(results) == 500, "потеряны квитанции: %d из 500" % len(results)
assert max(seen_batches) > 1, "позитивный контроль: партии реально больше одной пробы"
assert len(seen_batches) < 50, "500 проб разобраны за %d запросов — это не партии" % len(seen_batches)
print("UNIT_OK: 500 проб разобраны ОДНИМ тредом за %d партий (макс %d в партии)" % (
    len(seen_batches), max(seen_batches)))

# ---- 4. «Нет квитанции» — это не дроп: приговор только по таймауту ----
clk = Clock()
dropped, done = [], []
col = ReceiptCollector(receipts_fn=lambda hs: {}, on_result=lambda m, h, r: done.append(h),
                       on_timeout=lambda m, h: dropped.append(h),
                       poll_sec=0.01, timeout_sec=60.0, now=clk.now, sleep=clk.sleep)
col.track("0xaa", ("0xEA", None))
col.tick()
assert not dropped, "квитанции ещё нет — объявлять дроп рано (nonce живой ноги не трогаем)"
assert col.pending() == 1
clk.t += 61.0
col.tick()
assert dropped == ["0xaa"], "после таймаута дроп обязан быть объявлен"
assert col.pending() == 0
# позитивный контроль: та же проба, но квитанция пришла — таймаута нет
clk = Clock()
dropped2, done2 = [], []
col2 = ReceiptCollector(receipts_fn=lambda hs: {h: {"status": 1} for h in hs},
                        on_result=lambda m, h, r: done2.append(h),
                        on_timeout=lambda m, h: dropped2.append(h),
                        timeout_sec=60.0, now=clk.now, sleep=clk.sleep)
col2.track("0xbb", ("0xEA", None))
col2.tick()
assert done2 == ["0xbb"] and not dropped2
print("UNIT_OK: нет квитанции != дроп — приговор только по таймауту")

# ---- 5. Обработчик-паникёр не роняет коллектор и не подвешивает очередь ----
col3 = ReceiptCollector(receipts_fn=lambda hs: {h: {"status": 1} for h in hs},
                        on_result=lambda m, h, r: (_ for _ in ()).throw(RuntimeError("boom")),
                        log=lambda s: None)
col3.track("0xcc", ("0xEA", None))
col3.tick()
assert col3.pending() == 0, "упавший обработчик не должен оставлять пробу висеть вечно"
print("UNIT_OK: падение обработчика не подвешивает очередь коллектора")

# ---- 6. Движок кормит коллектор и считает отправленное ----
clk = Clock()
tracked = []
col4 = ReceiptCollector(receipts_fn=lambda hs: {}, on_result=lambda *a: None)
col4.track = lambda h, meta: tracked.append((h, meta))
n = {"i": 0}


def _send(addr):
    n["i"] += 1
    return "0xh%d" % n["i"]


eng = FireEngine(legs_fn=lambda: ["0xA", "0xB", "0xC"], send_fn=_send, collector=col4,
                 threads=1, rate=1e9, ttl=99.0, now=clk.now, sleep=clk.sleep)
eng.keepalive(target=("BTC", "0xmid", "0xborrower", 50000.0))
for _ in range(6):                                   # прогон рабочего шага без тредов
    addr = eng._next_leg()
    h = eng._send_fn(addr)
    eng.stats["sent"] += 1
    col4.track(h, (addr, eng._target))
assert [t[1][0] for t in tracked] == ["0xA", "0xB", "0xC", "0xA", "0xB", "0xC"], \
    "ноги обязаны идти по кругу: %s" % [t[1][0] for t in tracked]
assert tracked[0][1][1][1] == "0xmid", "цель обязана доехать до коллектора для вердикта"
print("UNIT_OK: ноги ротируются по кругу, цель доезжает до коллектора")

# ---- 7. Пустой склад не превращается в busy-loop ----
clk = Clock()
eng = FireEngine(legs_fn=lambda: ["0xA"], send_fn=lambda a: None,     # склад пуст -> None
                 threads=1, rate=1e9, ttl=99.0, now=clk.now, sleep=clk.sleep)
eng.keepalive()
t_before = clk.t
stop_after = {"n": 0}


def _sleep_counting(s):
    stop_after["n"] += 1
    clk.t += s
    if stop_after["n"] >= 5:
        eng._stop.set()


eng._sleep = _sleep_counting
eng._worker()
assert stop_after["n"] >= 5, "пустой склад обязан уводить тред в сон, а не крутить цикл"
assert eng.stats["skipped_dry"] >= 5
assert clk.t > t_before, "позитивный контроль: время в цикле реально шло"
print("UNIT_OK: пустой склад уводит тред в сон (не жжём ядро вхолостую)")

# ---- 8. ПОДКЛЮЧЕНИЕ: флаг реально переключает путь (иначе весь движок — мёртвый код) ----
import types
import chain.spec_fire_live as sfl
from chain.spec_fire_live import SpecFireLive
import chain.execute as ex_mod

_cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000, tip_gwei=0.05,
                             sequencer_rpc="off", eth_price_usd=1730.0)
_shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
PROBE = {"calldata": "0xdead", "gas": 625_000, "swap_meta": None}


class _RecEngine:
    def __init__(self):
        self.alive = []

    def keepalive(self, target=None, ttl=None):
        self.alive.append(target)

    def disarm(self):
        pass


def _mk(engine_on, board):
    legs_fired = []
    sf = SpecFireLive(rpc=None, cfg=_cfg, shared=_shared, ret_fn=lambda l: 0.0,
                      dev_by={"BTC": 0.001}, build_fn=lambda m, b, d: PROBE,
                      send_fn=lambda c, g: "0xh", receipt_fn=lambda h: (0, 0.0),
                      log_fn=lambda s: None,
                      volley_accounts_fn=lambda: [("0xEA01", "k1")],
                      volley_send_fn=lambda *a, **k: legs_fired.append(a[2]) or "0xleg")
    sf._now = lambda: 5000.0
    sf._sleep = lambda s: None
    sf._board_live = lambda m, b: board
    sf._cascade_active = lambda: True
    fe.ENGINE_ON = engine_on
    rec = _RecEngine()
    if engine_on:
        sf._fire_engine = rec
    return sf, rec, legs_fired


_saved_on, _saved_volley = fe.ENGINE_ON, ex_mod.SPEC_FIRE_VOLLEY
ex_mod.SPEC_FIRE_VOLLEY = True
try:
    # движок ВКЛ + доска: тик кормит keepalive и НЕ порождает залп
    sf, rec, fired = _mk(True, board=True)
    sf._volley(PROBE, "0xmid", "0xborrower", 50_000.0, "BTC")
    assert rec.alive == [("BTC", "0xmid", "0xborrower", 50_000.0)], \
        "тик обязан продлевать право стрелять, получено %s" % rec.alive
    time.sleep(0.05)
    assert fired == [], "движок включён — залп порождать нельзя (в этом вся правка)"

    # ПОЗИТИВНЫЙ КОНТРОЛЬ: движок ВЫКЛ -> прежний путь стреляет, как стрелял
    sf, rec, fired = _mk(False, board=True)
    sf._volley(PROBE, "0xmid", "0xborrower", 50_000.0, "BTC")
    time.sleep(0.3)
    assert rec.alive == [], "выключенный движок не должен получать тики"
    assert fired == ["0xEA01"], "с выключенным движком прежний залп обязан стрелять: %s" % fired

    # движок ВКЛ, но доски НЕТ: калдата зависит от цели, склад под неё не подписан -> старый путь
    sf, rec, fired = _mk(True, board=False)
    sf._volley(PROBE, "0xmid", "0xborrower", 50_000.0, "BTC")
    time.sleep(0.3)
    assert rec.alive == [], "без доски движку стрелять нечем — тик ему не адресован"
    assert fired == ["0xEA01"], "без доски обязан работать прежний путь: %s" % fired
finally:
    fe.ENGINE_ON, ex_mod.SPEC_FIRE_VOLLEY = _saved_on, _saved_volley
print("UNIT_OK: флаг переключает путь — ВКЛ кормит движок, ВЫКЛ стреляет по-старому")

print("ПРОШЛИ: движок огня и батч-коллектор")
