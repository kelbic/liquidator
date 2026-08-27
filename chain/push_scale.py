"""РАЗМЕР ТОЛЧКА ЦЕНЫ — замеренное распределение, пер-фид (27.08).

ЗАЧЕМ. Гейт арма стоял на `SPEC_FIRE_ARM_MAX_DIST = 0.03` (3%), а замер 920 подтверждённых пушей
за 3 суток даёт МАКСИМУМ 0.601% и p99 0.510% по всему полю. То есть окно арма было в пять раз
шире самого большого толчка, который вообще случался, и внутри него ранг шёл «чист по долгу»
(spec_fire_live._top_fat). Следствие — доска систематически заряжалась САМОЙ ЖИРНОЙ целью вместо
ДОСЯГАЕМОЙ: в каскаде 26.08 15:05 у нас стоял заёмщик с HF 1.02319 (2.27% от кромки, долг $174k),
а все десять победителей были в пределах 0.51%, победивший — 1.00096 (0.096%). Цель на 2.27% не
флипает НИ ПРИ ОДНОМ наблюдавшемся толчке: 7 391 проба за 9.8 суток, все «position is healthy»,
ноль исполнений.

ЧТО ЗДЕСЬ. Кольцо |ret_pct| подтверждённых пушей ПО ФИДУ и две величины из него:
  * `reach(feed)`  — предел досягаемости (квантиль, по умолчанию p99): дальше цель брать незачем;
  * `p_flip(feed, dist)` — доля толчков, которые перекрывают это расстояние. Ею взвешивается
    ранг: ожидание = долг x p_flip, а не долг.

ПОЧЕМУ ПЕР-ФИД. Разброс втрое: BTC p99 = 0.192%, XRP p99 = 0.601%. Общий порог неверен обоим —
для BTC он втрое щедр, для XRP втрое скуп.

НЕЗНАНИЕ. Пустое/тонкое кольцо не открывает гейт шире: до `MIN_N` замеров действует СЕВОДНЯ
ЗАМЕРЕННЫЙ полевой запас `_SEED` (p99 поля 0.510%, максимум 0.601%), а не прежние 3%. Семя —
не самоотчёт механизма, а число из журнала PREDICT за 3 суток; оно ЗАМЕНЯЕТСЯ живым замером,
как только фид набирает MIN_N.

ОТКАТ: PUSH_SCALE=0 -> reach() отдаёт None, и вызывающий обязан вернуться к прежней константе.
"""
import json
import os
import threading
import time

PUSH_SCALE_ON = os.environ.get("PUSH_SCALE", "1").lower() not in ("0", "off", "false", "no")
RING = int(os.environ.get("PUSH_SCALE_RING", "400"))
MIN_N = int(os.environ.get("PUSH_SCALE_MIN_N", "20"))
TTL_SEC = float(os.environ.get("PUSH_SCALE_TTL_SEC", "259200"))     # 3 суток — окно замера
Q = float(os.environ.get("PUSH_SCALE_Q", "0.99"))
# Порог мусора: наблюдённый максимум поля 6.738%, берём с запасом. Прежние 50% пропускали
# выброс, который на n<=100 (там квантиль вырождается в МАКСИМУМ) пиннил величину на весь TTL.
MAX_SANE_PCT = float(os.environ.get("PUSH_SCALE_MAX_SANE_PCT", "12.0"))
FLOOR_PCT = float(os.environ.get("PUSH_SCALE_FLOOR_PCT", "0.05"))   # пол на случай штиля
CAP_PCT = float(os.environ.get("PUSH_SCALE_CAP_PCT", "7.00"))       # выше наблюдённого max 6.738%

# ПЕРЕМЕР 27.08 (ПОСЛЕ АДВЕРСАРНОЙ ЛИНЗЫ). Первый замер был сделан НЕ ТЕМ ПОЛЕМ: регексп
# `.*ret_pct=` жадный, и в строке `... ret_pct=-0.40327 arm_ret_pct=-0.095318 ...` он
# захватывал ARM_RET_PCT — доходность в момент взведения арм-порога, ограниченную по
# построению. Кольцо же кормится `ret_pct` (predict.py, событие confirmed). Числа разошлись
# в 2.3-5.4 раза, и вывод «цель на 2.27% не флипает НИ ПРИ ОДНОМ толчке» был ЛОЖЕН.
# Правильное поле, 7 суток, n=3150 подтверждённых пушей, |ret_pct| в ПРОЦЕНТАХ:
#   BTC  n=884 p50 0.139 p90 0.242 p99 0.530 | ETH n=737 p50 0.201 p90 0.351 p99 0.808
#   XRP  n=434 p50 0.594 p90 0.970 p99 2.772 | ADA n=380 p50 0.576 p90 0.904 p99 3.034
#   DOGE n=290 p50 0.575 p90 0.921 p99 2.495 | SOL n=233 p50 0.578 p90 0.922 p99 2.768
#   LTC  n=192 p50 0.556 p90 0.827 p99 1.602 | ВСЕ p50 0.347 p90 0.754 p99 1.820 max 6.738
_SEED = {"BTC": 0.530, "ETH": 0.808, "XRP": 2.772, "ADA": 3.034,
         "SOL": 2.768, "DOGE": 2.495, "LTC": 1.602}
SEED_DEFAULT_PCT = float(os.environ.get("PUSH_SCALE_SEED_PCT", "1.820"))   # p99 всего поля

_LOCK = threading.Lock()
_RINGS: dict = {}


def note_push(feed, ret_pct, now=None):
    """Записать |ret_pct| подтверждённого пуша фида. Зовётся СБОКУ от денежного пути."""
    if not PUSH_SCALE_ON or ret_pct is None:
        return False
    try:
        v = abs(float(ret_pct))
    except Exception:                                   # noqa: BLE001
        return False
    if not (v > 0.0) or v > MAX_SANE_PCT:               # мусор/битый анкер отбрасываем
        return False
    n = time.time() if now is None else now
    k = str(feed or "?").upper()
    with _LOCK:
        r = _RINGS.setdefault(k, [])
        r.append((n, v))
        if len(r) > RING:
            del r[:len(r) - RING]
    return True


def _fresh(feed, now):
    r = _RINGS.get(str(feed or "?").upper()) or []
    return [v for ts, v in r if now - ts <= TTL_SEC]


def samples(feed, now=None):
    """Сколько живых замеров у фида (для приборов и стендов). Чистая."""
    return len(_fresh(feed, time.time() if now is None else now))


def reach(feed, now=None):
    """Предел досягаемости фида в ДОЛЕ (не процентах): дальше цель не флипает.
    None = механизм выключен (вызывающий возвращается к прежней константе)."""
    if not PUSH_SCALE_ON:
        return None
    now = time.time() if now is None else now
    v = sorted(_fresh(feed, now))
    if len(v) >= MIN_N:
        pct = v[min(len(v) - 1, int(len(v) * Q))]
    else:
        pct = _SEED.get(str(feed or "?").upper(), SEED_DEFAULT_PCT)
    pct = max(FLOOR_PCT, min(CAP_PCT, float(pct)))
    return pct / 100.0


def p_flip(feed, dist, now=None):
    """Доля толчков фида, перекрывающих расстояние `dist` (доля, не проценты).
    Живой замер при n >= MIN_N; иначе — консервативная ступень от семени: внутри семени 1.0,
    вне — 0.0. Никогда не возвращает больше 1.0 и меньше 0.0."""
    if dist is None:
        return 0.0
    try:
        d = max(0.0, float(dist)) * 100.0
    except Exception:                                   # noqa: BLE001
        return 0.0
    now = time.time() if now is None else now
    v = _fresh(feed, now)
    if len(v) < MIN_N:
        return None            # ЗАМЕРА НЕТ. Прежняя редакция отдавала ступень 1.0/0.0 от семени:
                               # всё за семенем получало РОВНО 0.0, ключи ранга становились равны,
                               # и устойчивая сортировка молча отдавала top-1 по ПОРЯДКУ ВСТАВКИ.
                               # None заставляет вызывающего вернуться к прежнему рангу по долгу.
    return sum(1 for x in v if x >= d) / float(len(v))


def snapshot(now=None):
    """Срез для приборов: фид -> (n, reach%, p50%). Чистая."""
    now = time.time() if now is None else now
    out = {}
    with _LOCK:
        keys = list(_RINGS)
    for k in keys:
        v = sorted(_fresh(k, now))
        if not v:
            continue
        out[k] = (len(v), round((reach(k, now) or 0.0) * 100.0, 4),
                  round(v[len(v) // 2], 4))
    return out


# --- ПЕРСИСТЕНТНОСТЬ (линза 27.08) ---------------------------------------------------------
# Замер: 63 старта юнита за 10 суток, средний аптайм ~3.8ч. Время набрать MIN_N=20 при живых
# темпах: BTC 3.8ч, XRP 7.6ч, SOL 14.3ч, LTC 17.5ч. Кольцо ТОЛЬКО В ПАМЯТИ означает, что для
# всех фидов кроме BTC живой замер не включается НИКОГДА, а TTL 3 суток не связывает вовсе.
STATE_PATH = os.environ.get("PUSH_SCALE_STATE", "data/push_scale.json")


def save(path=None):
    """Сохранить кольца атомарно (tmp+rename). Возвращает число сохранённых замеров."""
    path = path or STATE_PATH
    with _LOCK:
        snap = {k: [[round(ts, 3), round(v, 6)] for ts, v in r] for k, r in _RINGS.items()}
    n = sum(len(v) for v in snap.values())
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, path)
    except Exception:                                   # noqa: BLE001
        return 0                                        # учёт сбоку: не роняет вызывающего
    return n


def load(path=None, now=None):
    """Поднять кольца с диска, отбросив протухшее. Возвращает число поднятых замеров."""
    path = path or STATE_PATH
    now = time.time() if now is None else now
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:                                   # noqa: BLE001
        return 0
    n = 0
    with _LOCK:
        for k, rows in (raw or {}).items():
            keep = [(float(ts), float(v)) for ts, v in rows
                    if now - float(ts) <= TTL_SEC and 0.0 < float(v) <= MAX_SANE_PCT]
            if keep:
                _RINGS[str(k).upper()] = keep[-RING:]
                n += len(keep)
    return n


# Кольцо поднимается ПРИ ИМПОРТЕ: без этого при аптайме 3.8ч живой замер не включается никогда.
# Сбой чтения — не отказ: load() возвращает 0 и механизм работает на семени, как и раньше.
if PUSH_SCALE_ON:
    try:
        load()
    except Exception:                                   # noqa: BLE001
        pass
