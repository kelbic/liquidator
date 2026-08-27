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
import os
import threading
import time

PUSH_SCALE_ON = os.environ.get("PUSH_SCALE", "1").lower() not in ("0", "off", "false", "no")
RING = int(os.environ.get("PUSH_SCALE_RING", "400"))
MIN_N = int(os.environ.get("PUSH_SCALE_MIN_N", "20"))
TTL_SEC = float(os.environ.get("PUSH_SCALE_TTL_SEC", "259200"))     # 3 суток — окно замера
Q = float(os.environ.get("PUSH_SCALE_Q", "0.99"))
FLOOR_PCT = float(os.environ.get("PUSH_SCALE_FLOOR_PCT", "0.05"))   # ниже этого гейт не сжимаем
CAP_PCT = float(os.environ.get("PUSH_SCALE_CAP_PCT", "1.50"))       # и шире этого не открываем

# Замер 27.08 (журнал PREDICT, 3 суток, 920 подтверждённых пушей), |ret_pct| в ПРОЦЕНТАХ:
#   BTC n=288 p99=0.192 max=0.226 | ETH n=256 p99=0.245 max=0.255 | XRP n=90 p99=0.601 max=0.601
#   ADA n=95 p99=0.586 | SOL n=79 p99=0.515 | DOGE n=64 p99=0.520 | LTC n=48 p99=0.563
#   ВСЕ n=920 p50=0.141 p90=0.346 p99=0.510 max=0.601
_SEED = {"BTC": 0.226, "ETH": 0.255, "XRP": 0.601, "ADA": 0.586,
         "SOL": 0.515, "DOGE": 0.520, "LTC": 0.563}
SEED_DEFAULT_PCT = float(os.environ.get("PUSH_SCALE_SEED_PCT", "0.601"))   # максимум поля

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
    if not (v > 0.0) or v > 50.0:                       # мусор/битый анкер отбрасываем
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
    if len(v) >= MIN_N:
        return sum(1 for x in v if x >= d) / float(len(v))
    seed = _SEED.get(str(feed or "?").upper(), SEED_DEFAULT_PCT)
    return 1.0 if d <= seed else 0.0


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
