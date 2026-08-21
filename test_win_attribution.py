"""П.3 пакета kelbic 21.08: атрибуция WIN — ярлык keepalive против жертвы из квитанции.

Дефект НАЗВАН в докстринге _probe_win: tgt[0] — ярлык ПОСЛЕДНЕГО тикнувшего keepalive
любого окна каскада (FireEngine._target — одно перезаписываемое поле), 59% проб несут чужой.
Сценарий красного теста: план на доске принадлежит жертве A (окно feed=A), но keepalive
окна B успел перезаписать _target до отправки — проба летит с ярлыком B. Приз берётся ПО
ПЛАНУ ДОСКИ (жертва A, видна в квитанции событием Liquidated). База: гасится окно B —
ЛОЖНЫЙ cooldown не тому фиду. Фикс: жертва из квитанции сверяется с ярлыком; НЕСОВПАДЕНИЕ
НЕ ГАСИТ чужое окно (незнание закрывает гард на гашение, а не открывает); совпадение —
поведение прежнее бит-в-бит. Живые окна для приёмки не нужны."""
import sys
import threading
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from eth_utils import keccak  # noqa: E402
import chain.spec_fire_live as sfl  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


LIQ = "0x" + "ab" * 20                       # наш контракт
B_A = "0x" + "aa" * 20                       # жертва A (план доски)
B_B = "0x" + "bb" * 20                       # жертва B (ярлык keepalive)
LOAN = "0x" + "cc" * 20
SIG = "0x" + keccak(text="Liquidated(address,address,uint256)").hex()


def _receipt(borrower):
    return {"status": 1, "logs": [{
        "address": LIQ,
        "topics": [SIG, "0x" + "00" * 12 + borrower[2:], "0x" + "00" * 12 + LOAN[2:]],
        "data": "0x" + (10**15).to_bytes(32, "big").hex(),
    }]}


class _Eng:
    def __init__(self):
        self.disarmed = 0

    def disarm(self):
        self.disarmed += 1


class _Cfg:
    liquidator_address = LIQ


def _mk():
    o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    o.stats = {}
    o.cfg = _Cfg()
    o._lock = threading.Lock()
    o._windows = {}
    o._fire_engine = _Eng()
    o._cooldowns = []
    o._set_cooldown = lambda feed, src, proven=False: o._cooldowns.append(feed)
    o._accounted = lambda name, h, fn, *a, **kw: None    # учёт сбоку — в стенде глушим
    return o


# ---- 1. ЯДРО (красный на базе): ярлык B, приз по плану A -> окно B гаснуть НЕ должно ----
o = _mk()
stopB = threading.Event()
o._windows["B"] = stopB
meta = ("0x" + "11" * 20, ("B", "0xmidB", B_B, 5000.0))   # ярлык от keepalive окна B
o._engine_result_verdict(meta, "0xhash1", _receipt(B_A), gas_usd=0.01)
check("победа посчитана", o.stats.get("wins") == 1, o.stats)
check("огонь погашен (disarm — глобальный, всегда)", o._fire_engine.disarmed == 1)
check("ЛЖИВЫЙ ярлык: окно B НЕ закрыто (жертва из квитанции — не его)",
      "B" in o._windows and not stopB.is_set(), list(o._windows))
check("ЛЖИВЫЙ ярлык: cooldown фиду B НЕ ставится", o._cooldowns == [], o._cooldowns)

# ---- 2. Честный ярлык: поведение прежнее бит-в-бит ----
o2 = _mk()
stopB2 = threading.Event()
o2._windows["B"] = stopB2
meta2 = ("0x" + "11" * 20, ("B", "0xmidB", B_B, 5000.0))
o2._engine_result_verdict(meta2, "0xhash2", _receipt(B_B), gas_usd=0.01)
check("честный ярлык: окно B закрыто", "B" not in o2._windows and stopB2.is_set())
check("честный ярлык: cooldown поставлен", o2._cooldowns == ["B"], o2._cooldowns)

# ---- 3. Незнание жертвы (квитанция без наших событий разобралась пусто) ----
# _receipt_has_win=False -> не победа, ветка не зовётся; отдельный случай — победа есть,
# но parse пуст (форма уехала): гасить по ярлыку НЕЛЬЗЯ (незнание закрывает гард).
o3 = _mk()
stopB3 = threading.Event()
o3._windows["B"] = stopB3
bad = {"status": 1, "logs": [{"address": LIQ, "topics": [SIG], "data": "0x"}]}   # len(topics)<3
# _receipt_has_win смотрит только topic0 -> победа True; parse_batch_receipt вернёт пусто
o3._engine_result_verdict(meta2, "0xhash3", bad, gas_usd=0.0)
check("жертва неизвестна: окно НЕ гасится по ярлыку", "B" in o3._windows and not stopB3.is_set())

# ---- 4. HIL-шаблон первой победы (ритуал, п.2): уходит человеку, деньги не трогает ----
o4 = _mk()
sent = []
o4._alert = lambda text, key: sent.append((key, text))
o4._windows["B"] = threading.Event()
o4._engine_result_verdict(meta2, "0xhash4", _receipt(B_B), gas_usd=0.02)
check("HIL-шаблон ушёл с ключом first-win", len(sent) == 1 and sent[0][0] == "first-win", sent)
_t = sent[0][1] if sent else ""
for _frag in ("tx:", "рынок/жертва:", "РЕШЕНИЕ:", "Профит УЖЕ у owner"):
    check("шаблон несёт %r" % _frag, _frag in _t)
o5 = _mk()                                   # канал не армирован
o5._alert = None
o5._windows["B"] = threading.Event()
o5._engine_result_verdict(meta2, "0xhash5", _receipt(B_B), gas_usd=0.02)
check("неармированный канал: молчание, не падение", o5.stats.get("wins") == 1)
o6 = _mk()                                   # канал бросает
o6._alert = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TG упал"))
o6._windows["B"] = threading.Event()
o6._engine_result_verdict(meta2, "0xhash6", _receipt(B_B), gas_usd=0.02)
check("отказ канала не отменяет победу и гашение окна",
      o6.stats.get("wins") == 1 and "B" not in o6._windows)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: победа гасит только окно ВЗЯТОЙ жертвы; лживый ярлык не глушит чужой фид"))
sys.exit(1 if FAILS else 0)
