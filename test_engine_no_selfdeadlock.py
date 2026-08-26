"""РЕГРЕССИЯ 23-26.08: САМОДЕДЛОК прогрева коллектора внутри _engine.

_engine() держит нерекурсивный self._lock; прогрев self._rcpt_coll() (правка П.3 по линзе)
брал ТОТ ЖЕ лок повторно -> лок не отпускался НИКОГДА -> predict-driver вставал на нём в
первом же handle_event -> окна не армились 2.5 суток (0 армов при 528 пушах оракула/сутки),
флот молчал, работала одна реактивная лента. Поймано py-spy на живом процессе.

Стенд ИСПОЛНЯЕТ обе функции на настоящем объекте (урок «шов подменён выше дефекта») и
проверяет ГЛАВНОЕ СВОЙСТВО: после _engine() лок СВОБОДЕН, а значит соседние треды живут.
"""
import sys
import threading
import pathlib as _pl
from types import SimpleNamespace

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402

o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o._lock = threading.Lock()
o.stats = {}
o._log = lambda m: None
o._now = lambda: 1000.0
o.rpc = SimpleNamespace(rpc_url="http://node")
o.cfg = SimpleNamespace(eth_price_usd=4000.0, liquidator_address="0xliq")
o._receipts_fn = lambda combat: (lambda hs: {})
o._index_fn = lambda: None

# 1. Прогрев коллектора ПОД ЛОКОМ — тот самый вложенный захват (воспроизведение дефекта)
got = []


def _warm_under_lock():
    with o._lock:
        try:
            o._rcpt_coll()
            got.append("no-deadlock")
        except Exception as e:                                # noqa: BLE001
            got.append("err:%s" % type(e).__name__)


t = threading.Thread(target=_warm_under_lock, daemon=True)
t.start()
t.join(timeout=3.0)
check("ВОСПРОИЗВЕДЕНИЕ: _rcpt_coll ПОД локом вешает тред намертво (класс дефекта реален)",
      t.is_alive() and not got, got)

# 2. ГЛАВНОЕ СВОЙСТВО боевого пути: после сборки движка лок СВОБОДЕН.
#    (проверяем исходником И семантикой: прогрев стоит ВНЕ with self._lock)
_S = open("chain/spec_fire_live.py").read()
_eng = _S[_S.index("    def _engine(self):"):]
_eng = _eng[:_eng.index("\n    def _engine_result")]
_warm_pos = _eng.index("self._rcpt_coll()")
_ret_pos = _eng.index("return eng", _warm_pos - 400)
_indent_ok = "\n        if SPEC_FIRE_RCPT_MODE in (\"shadow\", \"collector\"):" in _eng
check("прогрев коллектора стоит ВНЕ `with self._lock` (отступ 8, не 12)", _indent_ok,
      _eng[max(0, _warm_pos - 120):_warm_pos + 40])
check("после прогрева движок возвращается (return eng на том же уровне)",
      "\n        return eng" in _eng)

# 3. Свободный лок после прогрева вне лока: соседний тред (как predict-driver) не встаёт
o2 = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
o2._lock = threading.Lock()
o2.stats = {}
o2._log = lambda m: None
o2.rpc = o.rpc
o2.cfg = o.cfg
o2._receipts_fn = o._receipts_fn
o2._index_fn = o._index_fn
o2._rcpt_coll()                                              # прогрев БЕЗ удержания лока
neighbour = []


def _neighbour():
    with o2._lock:                                           # как handle_event/predict-driver
        neighbour.append("прошёл")


t2 = threading.Thread(target=_neighbour, daemon=True)
t2.start()
t2.join(timeout=3.0)
check("после прогрева ВНЕ лока сосед (predict-driver) проходит лок", neighbour == ["прошёл"],
      neighbour)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: прогрев коллектора не держит лок — predict-driver жив"))
sys.exit(1 if FAILS else 0)
