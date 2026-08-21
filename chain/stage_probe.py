"""П.7 (kelbic 21.08): ПЕР-СТАДИЙНЫЙ ПРОФИЛЬ ЗАДЕРЖКИ до нашей первой пробы на классе.

Зачем. Главная цифра проекта — медиана delta ≈ +140 позиций на верном классе (замер
50262420). Это не сеть (RTT до секвенсера 30мс) и не класс (tip-match есть) — это 200-500мс
лишнего пути ВНУТРИ конвейера. Резать конвейер вслепую запрещено (kelbic): сначала прибор,
который называет СТАДИЮ-ВКЛАДЧИКА.

Стадии (метки времени на путь ОДНОЙ пробы):
    frame     — кадр суб-блока с транзитом принят recv-петлёй
    detect    — транзит опознан как наш (флип/агрегатор наш)
    handoff   — окну отдан pending_fire (право огня по факту транзита)
    sign      — эта проба была ПОДПИСАНА (лестница; обычно СИЛЬНО раньше — тем и важна:
                класс вшивается при подписи, а транзит приходит позже)
    pacer     — пейсер выдал токен именно этой пробе
    ack       — секвенсер принял байты

Устройство. Кольцо меток по хэшу (cap ~4000 = 100 ног × лестница 40). Пишут — денежные
пути, поэтому каждая запись O(1) под одним локом и НИКОГДА не бросает: прибор висит СБОКУ,
его отказ не имеет права трогать огонь (канон «учёт не стоит перед деньгами»).
Откат: STAGE_PROBE=0 — метки не пишутся, читатель молчит.
"""
from __future__ import annotations

import os
import threading
import time

STAGE_ON = (os.environ.get("STAGE_PROBE", "1") or "1").lower() not in ("0", "false", "no")
_CAP = int(os.environ.get("STAGE_PROBE_CAP", "4000"))
_LOCK = threading.Lock()
_MARKS: dict = {}          # tx_hash -> {stage: ts}
_ORDER: list = []          # порядок вставки для дешёвой обрезки кольца
_FEED: dict = {}           # feed -> {stage: ts}: стадии ДО подписи живут по фиду, не по хэшу
_BLOCK: dict = {}          # bn -> ts кадра


def mark_frame(bn, ts=None):
    """Кадр суб-блока принят (recv-петля). Дешевле некуда: одна запись + обрезка."""
    if not STAGE_ON:
        return
    try:
        with _LOCK:
            _BLOCK[int(bn)] = float(ts if ts is not None else time.time())
            if len(_BLOCK) > 64:
                for k in sorted(_BLOCK)[:32]:
                    _BLOCK.pop(k, None)
    except Exception:                                        # noqa: BLE001
        pass


def mark_feed(feed, stage, ts=None):
    """Стадия ДО подписи (detect/handoff) — по фиду: конкретной пробы ещё не существует."""
    if not STAGE_ON or not feed:
        return
    try:
        with _LOCK:
            _FEED.setdefault(str(feed), {})[str(stage)] = float(
                ts if ts is not None else time.time())
    except Exception:                                        # noqa: BLE001
        pass


def stamp(h, stage, ts=None):
    """Стадия КОНКРЕТНОЙ пробы (sign/pacer/ack)."""
    if not STAGE_ON or not h:
        return
    try:
        k = str(h).lower()
        with _LOCK:
            d = _MARKS.get(k)
            if d is None:
                d = _MARKS[k] = {}
                _ORDER.append(k)
                if len(_ORDER) > _CAP:
                    for old in _ORDER[:len(_ORDER) - _CAP]:
                        _MARKS.pop(old, None)
                    del _ORDER[:len(_ORDER) - _CAP]
            d[str(stage)] = float(ts if ts is not None else time.time())
    except Exception:                                        # noqa: BLE001
        pass


def profile(h, feed=None, bn=None):
    """Профиль пробы: {stage: ts} по хэшу + стадии фида/блока. Пусто -> {}."""
    if not STAGE_ON or not h:
        return {}
    try:
        out = {}
        with _LOCK:
            if bn is not None and int(bn) in _BLOCK:
                out["frame"] = _BLOCK[int(bn)]
            if feed:
                out.update(_FEED.get(str(feed), {}))
            out.update(_MARKS.get(str(h).lower(), {}))
        return out
    except Exception:                                        # noqa: BLE001
        return {}


# ЦЕПОЧКА реактивного пути. `sign` в неё НЕ ВХОДИТ намеренно: лестница подписывается
# АСИНХРОННО и заранее (обычно за десятки секунд до кадра), это не звено задержки, а ВОЗРАСТ
# заготовки — и он важен отдельно: класс tip вшивается при подписи, поэтому старая лестница
# едет старым классом. Первая редакция считала sign звеном и назвала вкладчиком «sign→pacer
# 90 315мс» — прибор соврал бы ровно там, где должен называть виновного.
_CHAIN_STAGES = ("frame", "detect", "handoff", "pacer", "ack")


def format_profile(prof):
    """(строка, (топ-стадия, вклад в мс)). Возраст лестницы — отдельным полем, не звеном."""
    if not prof:
        return "—", (None, None)
    known = [(s, prof[s]) for s in _CHAIN_STAGES if s in prof]
    age_s = None
    if "sign" in prof and "frame" in prof:
        age_s = prof["frame"] - prof["sign"]
    if len(known) < 2:
        head = " ".join("%s=%.3f" % (s, t) for s, t in known) or "—"
        return (head + ("" if age_s is None else " | возраст_лестницы %.0fс" % age_s),
                (None, None))
    parts, worst, worst_ms = [], None, None
    for (s0, t0), (s1, t1) in zip(known, known[1:]):
        ms = (t1 - t0) * 1000.0
        parts.append("%s→%s %.0fмс" % (s0, s1, ms))
        if ms > 0 and (worst_ms is None or ms > worst_ms):
            worst, worst_ms = "%s→%s" % (s0, s1), ms
    total = (known[-1][1] - known[0][1]) * 1000.0
    tail = "" if age_s is None else " | возраст_лестницы %.0fс" % age_s
    return "%s | всего %.0fмс%s" % (" ".join(parts), total, tail), (worst, worst_ms)
