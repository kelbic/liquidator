"""#139: Thread-на-блок в recv-петле flashblocks роняет hot_loop.

Факт боя 18.08 02:56:36: `RuntimeError: can't start new thread` на chain/hotpath.py:2662 —
recv-петля спавнила threading.Thread(target=_poll_prices, ...).start() НА КАЖДЫЙ блок. В тишине
это ~56 тредов; в шторм кап TasksMax=256 пробивается и hot_loop падает в самый дорогой момент.

Правка — паттерн _chain_index.offer_block: recv только КЛАДЁТ (bn, конфиг) и будит ЕДИНСТВЕННЫЙ
стоячий daemon-тред (latest-wins: занятой воркер увидит лишь последний bn, промежуточные
перезатрутся — очереди НЕТ). Обратная сторона размена: Thread-на-блок терпел зависший опрос
(следующий блок спавнил свежий тред), одиночный воркер — нет, поэтому сетевые вызовы опроса
обязаны иметь ЖЁСТКИЙ таймаут (2×таймаут заметно ниже времени блока ~2с).

Стенд гоняет НАСТОЯЩИЙ поблочный воркер (транспорт read_preconf_prices подменён monkeypatch-ем):
  * число живых тредов НЕ растёт с числом блоков (200 блоков -> максимум +2 к базе);
  * latest-wins доказан (занятой воркер обрабатывает ПОСЛЕДНИЙ bn, промежуточные пропущены);
  * self-heal (перезапуск hot_loop) НЕ плодит второй воркер И не залипает на старом конфиге;
  * сетевые клоны опроса несут жёсткий таймаут, 2×таймаут < 2с.

Красный-на-базе: на HEAD `offer_poll_block`/`_poll_rpc` не существуют, а recv-репродукция
(Thread-на-блок) на 200 блоках создаёт >=200 живых тредов ИЛИ падает по капу (.start()).
"""
import logging
import sys
import threading
import time

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.hotpath as hp   # noqa: E402

logging.basicConfig(level=logging.CRITICAL)
LOG = logging.getLogger("hot-poll-test")

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.005)
    return False


# Настоящая функция опроса — снимаем ссылку ДО любых подмен (её и восстанавливаем).
REAL_POLL = getattr(hp, "_poll_prices", None)

# Минимальный конфиг опроса: feeds/meta пусты -> oracle2agg пуст -> _poll_prices всё равно зовёт
# read_preconf_prices (наш шов), затем _poll_changed на пустом -> ни одного _process_transmit.
POLL_KW_BASE = dict(preconf_rpc=None, rpc_fallback=None, feeds={},
                    poll_seen={}, stats=None, spawn_kwargs={}, log=LOG, feed_handled=set())


def kw(meta=None):
    d = dict(POLL_KW_BASE)
    d["meta"] = {} if meta is None else meta
    return d


# ---- CHECK 4: жёсткий таймаут сетевых клонов опроса (пункт приёмки #3) -------------------------
try:
    class _FakeBase:
        rpc_url = "https://preconf.example/base/segsegsegseg"
        chain_id = 8453

    clone = hp._poll_rpc(_FakeBase())
    tmo = getattr(hp, "_POLL_RPC_TIMEOUT_SEC", None)
    check("timeout: клон опроса несёт request_timeout",
          clone is not None and getattr(clone, "request_timeout", None) is not None,
          "clone=%r" % (clone,))
    check("timeout: 2×таймаут заметно ниже времени блока (<2с)",
          tmo is not None and 2.0 * float(tmo) < 2.0, "timeout=%r" % (tmo,))
    check("timeout: клон сохраняет chain_id/url боевого rpc",
          clone.chain_id == 8453 and clone.rpc_url.startswith("https://preconf.example"),
          "%r/%r" % (getattr(clone, "chain_id", None), getattr(clone, "rpc_url", None)))
except Exception as e:
    check("timeout: клон опроса несёт request_timeout", False, "%s: %s" % (type(e).__name__, e))
    check("timeout: 2×таймаут заметно ниже времени блока (<2с)", False, "нет _POLL_RPC_TIMEOUT_SEC")
    check("timeout: клон сохраняет chain_id/url боевого rpc", False, "нет _poll_rpc")


# ---- CHECK 2: latest-wins (занятой воркер берёт последний bn, промежуточные пропущены) ---------
try:
    if REAL_POLL is not None:
        hp._poll_prices = REAL_POLL
    processed2 = []
    entered_first = threading.Event()
    hold = threading.Event()

    def _rec2(*, bn, **k):
        processed2.append(bn)
        if len(processed2) == 1:          # блокируем воркер на ПЕРВОМ обработанном
            entered_first.set()
            hold.wait(10)

    hp._poll_prices = _rec2
    hp.offer_poll_block(2001, **kw())     # воркер стартует, входит в _rec2(2001), висит
    got_first = entered_first.wait(5)
    for bn in range(2002, 2011):          # 9 блоков подряд, пока воркер занят
        hp.offer_poll_block(bn, **kw())
    hold.set()                            # отпускаем
    ok_last = wait_for(lambda: 2010 in processed2, 5.0)
    time.sleep(0.05)                      # дать шанс лишним (их быть не должно) проявиться
    check("latest-wins: воркер вошёл в первый опрос", got_first, "entered_first не выставлен")
    check("latest-wins: обработаны РОВНО первый и последний bn",
          processed2 == [2001, 2010], processed2)
    check("latest-wins: промежуточные bn пропущены (очереди нет)",
          not (set(range(2002, 2010)) & set(processed2)), processed2)
except Exception as e:
    check("latest-wins: обработаны РОВНО первый и последний bn", False, "%s: %s" % (type(e).__name__, e))
finally:
    if REAL_POLL is not None:
        hp._poll_prices = REAL_POLL


# ---- CHECK 3: self-heal не плодит воркера И не залипает на старом конфиге ----------------------
try:
    seen3 = []

    def _rec3(*, bn, meta=None, **k):
        seen3.append((bn, meta))

    hp._poll_prices = _rec3
    hp.offer_poll_block(3001, **kw(meta={"_v": 1}))
    wait_for(lambda: any(b == 3001 for b, _m in seen3), 5.0)
    workers_1 = [t for t in threading.enumerate() if t.name == "hot-poll"]
    # СИМУЛЯЦИЯ SELF-HEAL: hot_loop перезапустился и подаёт СВЕЖИЙ конфиг (feeds/meta ротация)
    hp.offer_poll_block(3002, **kw(meta={"_v": 2}))
    wait_for(lambda: any(b == 3002 for b, _m in seen3), 5.0)
    workers_2 = [t for t in threading.enumerate() if t.name == "hot-poll"]
    last_v = [m for b, m in seen3 if b == 3002]
    check("self-heal: ровно ОДИН стоячий воркер после перезапуска",
          len(workers_1) == 1 and len(workers_2) == 1 and workers_1[0] is workers_2[0],
          "w1=%d w2=%d" % (len(workers_1), len(workers_2)))
    check("self-heal: воркер получил СВЕЖИЙ конфиг (не залип на старых kwargs)",
          last_v == [{"_v": 2}], last_v)
except Exception as e:
    check("self-heal: ровно ОДИН стоячий воркер после перезапуска", False, "%s: %s" % (type(e).__name__, e))
    check("self-heal: воркер получил СВЕЖИЙ конфиг (не залип на старых kwargs)", False, str(e))
finally:
    if REAL_POLL is not None:
        hp._poll_prices = REAL_POLL


# ---- CHECK 1: число тредов НЕ растёт с числом блоков (красный-на-базе: Thread-на-блок) ---------
GATE = threading.Event()              # закрыт: транспорт опроса виснет, пока держим ворота


def _fake_read(*a, **k):
    GATE.wait(15)
    return {}, 0


hp.read_preconf_prices = _fake_read   # ШОВ ТРАНСПОРТА (не гейт, не логика)

_has_entry = hasattr(hp, "offer_poll_block")


def _drive(bn):
    if _has_entry:
        hp.offer_poll_block(bn, **kw())                 # ветка: одиночный воркер
    else:
        # HEAD-репродукция дефекта #139 ВЕРБАТИМ: Thread-на-блок на настоящий _poll_prices
        hp.threading.Thread(target=hp._poll_prices, daemon=True, kwargs=dict(bn=bn, **kw())).start()


base_ct = threading.active_count()
N = 200
peak = base_ct
start_errs = 0
for bn in range(1, N + 1):
    try:
        _drive(bn)
    except RuntimeError:
        start_errs += 1                                 # кап тредов пробит — ровно дефект боя
    c = threading.active_count()
    if c > peak:
        peak = c
GATE.set()                                              # отпускаем висящие опросы (дренаж)
time.sleep(0.05)
check("thread-count: 200 блоков НЕ растят число тредов (<= база+2)",
      peak <= base_ct + 2 and start_errs == 0,
      "peak=%d base=%d start_errs=%d" % (peak, base_ct, start_errs))


# ---- CHECK 0 (проводка): recv-петля переведена на offer_poll_block, Thread-на-блок снят --------
src = open("chain/hotpath.py").read()
check("проводка: recv-петля зовёт offer_poll_block", "offer_poll_block(" in src)
check("проводка: опрос идёт на клонах с таймаутом (_poll_rpc)", "_poll_rpc(" in src)
check("проводка: Thread(target=_poll_prices) на блок СНЯТ",
      src.count("target=_poll_prices") == 0, "осталось %d" % src.count("target=_poll_prices"))


print("=" * 62)
if FAILS:
    print("ПРОВАЛ: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("OK: recv-петля -> одиночный воркер (latest-wins, таймаут, self-heal), тредов не растёт")
