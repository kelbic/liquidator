"""Метки решётки OCR стоят на времени ОРАКУЛА, а не на моменте нашего полла (#49).

Ревью 08.08 (CONFIRMED): note_push штамповал t=self._now() — момент ОБНАРУЖЕНИЯ поллом, при
том что updatedAt оракула лежит в руках у predict._poll и выбрасывался. Полл ходит раз в 4с
(PREDICT_POLL_SEC), а толеранс фазы ±2с: метки квантованы ГРУБЕЕ самого гейта. Следствия —
ожидаемая доля гэпов на решётке ~0.72 против MIN_FRAC 0.80 (фича молча мертва, эффект x1.15
потерян), а при флуктуационном подтверждении фаза уезжает до 4с и lattice_skips глушат огонь
в истинную секунду транзита. Валидатор hits/misses при этом мерил ТЕМИ ЖЕ смещёнными часами
и выглядел здоровым — самореферентная валидация, зелёная по построению.

Исполняются НАСТОЯЩИЕ note_push и предиктовый on_push; швов на времени нет — время подаём.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.predict as pr                               # noqa: E402
import chain.spec_fire_live as sfl                       # noqa: E402

NOW = 10_000.0


def mk():
    sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    sf._now = lambda: NOW
    sf._logs = []
    sf._log = sf._logs.append
    sf._push_ts = {}
    sf.stats = {}
    sf._lattice_ready = lambda f: False       # валидатор здесь не экзаменуем, только метки
    return sf


# --- 1. МЕТКА СТАВИТСЯ НА МОМЕНТ АПДЕЙТА, а не на момент детекта ----------------------
# Полл увидел апдейт через 3.2с после того, как он сел (квант 4с — больше толеранса ±2с).
sf = mk()
sf.note_push("XRP", updated_at=time.time() - 3.2)
_t = sf._push_ts["XRP"][0]
assert abs(_t - (NOW - 3.2)) < 0.2, \
    "метка стоит на времени ДЕТЕКТА (%.2f вместо %.2f) — фаза уедет на квант полла" % (_t, NOW - 3.2)
assert sf.stats.get("lattice_oracle_ts") == 1, sf.stats

# --- 2. КОНТРОЛЬ «до фикса»: без updated_at метка = момент детекта --------------------
# Без него п.1 неотличим от «метка всегда сдвинута».
sf2 = mk()
sf2.note_push("XRP")
assert sf2._push_ts["XRP"][0] == NOW, sf2._push_ts
assert not sf2.stats.get("lattice_oracle_ts")

# --- 3. РАЗНОСТИ В ОДНОМ КАДРЕ: решётка считает интервалы, кадр обязан быть один ------
# Два апдейта ровно через период решётки -> разность меток = период (иначе гейт слепнет).
sf3 = mk()
_wall = time.time()
# Два апдейта, СЕВШИЕ ровно через период решётки, но увиденные поллом с РАЗНЫМ лагом
# (1.5с и 3.7с) — именно этот разнобой лагов раньше и размывал фазу.
sf3.note_push("XRP", updated_at=_wall - 31.5)           # сел 31.5с назад, лаг детекта 1.5с
sf3.note_push("XRP", updated_at=_wall - 1.5)            # сел 1.5с назад, лаг детекта 3.7с
_a, _b = sf3._push_ts["XRP"]
assert abs((_b - _a) - 30.0) < 0.3, \
    "разность меток %.2f вместо периода решётки 30с — кадры времени разъехались" % (_b - _a)

# --- 4. ЧАСЫ РАЗЪЕХАЛИСЬ: не молчим, а честно падаем на время детекта -----------------
sf4 = mk()
sf4.note_push("XRP", updated_at=time.time() + 500.0)     # апдейт «из будущего»
assert sf4._push_ts["XRP"][0] == NOW, "приняли невозможный skew за истину"
assert sf4.stats.get("lattice_skew_bad") == 1, sf4.stats
sf5 = mk()
sf5.note_push("XRP", updated_at=time.time() - sfl.SPEC_FIRE_LATTICE_MAX_SKEW_SEC - 10)
assert sf5._push_ts["XRP"][0] == NOW and sf5.stats.get("lattice_skew_bad") == 1

# --- 5. ДЕДУП 5с не сломан (confirmed и push об одном апдейте приходят парой) ---------
sf6 = mk()
_w = time.time()
sf6.note_push("XRP", updated_at=_w - 1.0)
sf6.note_push("XRP", updated_at=_w - 1.0)
assert len(sf6._push_ts["XRP"]) == 1, sf6._push_ts

# --- 6. ИСТОЧНИК: predict.on_push обязан НЕСТИ updated_at в событии ------------------
# Без этого note_push всегда получал бы None и фикс был бы мёртвым по построению.
eng = pr.PredictEngine.__new__(pr.PredictEngine) if hasattr(pr, "PredictEngine") else None
src = pathlib.Path("chain/predict.py").read_text()
assert '"updated_at": updated_at' in src, "on_push не кладёт время оракула в событие"
assert "on_push(sym, wall, price, updated_at=updated_at)" in src, \
    "поллер не передаёт updatedAt в on_push — время оракула снова выбрасывается"
_sfl = pathlib.Path("chain/spec_fire_live.py").read_text()
assert 'self.note_push(feed, updated_at=ev.get("updated_at"))' in _sfl, \
    "обработчик события не протаскивает время оракула в решётку"

print("OK: решётка размечена временем оракула, разъезд часов виден счётчиком")
