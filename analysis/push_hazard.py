"""PUSH-HAZARD (разведка, READ-ONLY): эмпирическое распределение времени прихода оракульного
пуша ОТНОСИТЕЛЬНО момента, когда сработало бы наше правило арма.

ЗАЧЕМ. Спекулятивные пробы стреляются ПЛОСКИМ каденсом (жирный режим 0.12с, пол петли
SPEC_FIRE_POLL_SEC=0.15 -> ~13 проб/блок, кап 600). P(win) = W/tau, W = 0.5-2.2мс. Плоский спам —
минимаксный выбор. Концентрация бюджета в подынтервале высокой плотности — СТАВКА на оценку
плотности: если плотность оценена плохо, концентрация строго хуже равномерной. Поэтому сначала
замер, потом код. Этот скрипт НИЧЕГО не меняет в боевом пути — только читает публичные данные.

ЧТО МЕРЯЕТ (для каждого фида):
  t_push  — время блока с AnswerUpdated на агрегаторе (транзит сел на чейн);
  t_arm   — момент, когда PredictEngine армил бы окно: |ret| >= dev * ARM_RATIO, где
            ret = (binance_mid - anchor)/anchor, anchor = binance_mid на ПРЕДЫДУЩЕМ пуше
            (chain/predict.py: on_mid/on_push, arm_pct = dev * _arm_ratio);
  t_hot   — момент, когда петля окна начала бы СТРЕЛЯТЬ: |ret| >= dev * SPEC_FIRE_THRESHOLD
            (chain/spec_fire_live.py::_window_loop, raw_hot) + peak-owed продление;
  L_arm = t_push - t_arm,  L_hot = t_push - t_hot.
Бюджет проб тратится в горячей фазе, поэтому решение о концентрации считается по L_hot
(L_arm выводится тоже — им живёт сам арм).

ИСТОЧНИКИ (оба публичные, ключей нет, боевые хосты не трогаются):
  * Base RPC (mainnet.base.org / base.drpc.org) — eth_getLogs по AnswerUpdated агрегатора;
  * Binance REST klines interval=1s (data-api.binance.vision) — off-chain путь цены.

ПОЗИТИВНЫЙ КОНТРОЛЬ (канон: «логов нет ⇒ явления нет» действительно лишь после позитивного
контроля НА ТОМ ЖЕ объекте). До любых выводов скрипт доказывает:
  PC1 (RPC/сигнатура/декод): на заведомо активном фиде getLogs отдаёт непустой набор, и цена
      САМОГО СВЕЖЕГО лога совпадает с latestRoundData() того же агрегатора;
  PC2 (Binance/спаривание): klines непусты и медиана цены сходится с ончейн-ответом (<2%) —
      значит серия принадлежит ТОМУ ЖЕ активу, а не случайному символу;
  PC3 (реплей умеет армить): синтетическая рампа через ту же функцию реплея даёт arm/hot в
      ожидаемой секунде (контроль не зависит от данных);
  PC4 (реплей армит НА НАШИХ ДАННЫХ): доля пушей с восстановленным армом > 0.
Провал любого PC -> скрипт падает громко и НЕ печатает выводов.

ЗАПУСК (read-only, ничего не пишет кроме кэша и --out):
    python -m analysis.push_hazard --days 7
    python -m analysis.push_hazard --days 14 --feeds XRP,ADA,SOL,DOGE,LTC --arm-ratio 0.65
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

# ---------------------------------------------------------------- константы боевого пути
# Держим локально, чтобы скрипт работал без .env/конфига (импорт chain.predict тянет config).
FEEDS = {                       # label -> (binance symbol, deviation фида)
    "BTC":  ("BTCUSDT",  0.0010),
    "ETH":  ("ETHUSDT",  0.0015),
    "DOGE": ("DOGEUSDT", 0.0050),
    "ADA":  ("ADAUSDT",  0.0050),
    "LTC":  ("LTCUSDT",  0.0050),
    "XRP":  ("XRPUSDT",  0.0050),
    "SOL":  ("SOLUSDT",  0.0050),
}
# BF1-прокси Chainlink на Base (feed_to_market.json / chain.predict.FEEDS_BASE). Агрегатор
# резолвится ЖИВЬЁМ через proxy.aggregator() — прокси стабильна при ротации агрегаторов.
PROXIES = {
    "BTC":  "0x64c911996D3c6aC71f9b455B1E8E7266BcbD848F",
    "ETH":  "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
    "DOGE": "0x8422f3d3CAFf15Ca682939310d6A5e619AE08e57",
    "ADA":  "0x34cD971a092d5411bD69C10a5F0A7EEF72C69041",
    "LTC":  "0x206a34e47093125fbf4C75b7c7E88b84c6A77a69",
    "XRP":  "0x9f0C1dD78C4CBdF5b9cf923a549A201EdC676D34",
    "SOL":  "0x975043adBb80fc32276CbF9Bbcfd4A601a12462D",
}
ARM_RATIO_DEFAULT = 0.65        # PREDICT_ARM_RATIO_<LABEL> (боевой дефолт слоя 0.9)
DISARM_RATIO = 0.7              # chain/predict.py::_DISARM_RATIO
HOLD_SEC = 600.0                # falsepos-кап армленного окна
SPEC_FIRE_THRESHOLD = 1.0       # ×dev — порог ОГНЯ в петле окна
OWED_HOLD_SEC = 75.0            # peak-owed: заряженный транзит держит огонь
OWED_BAND = 0.3                 # доля dev — серая полоса продления долга
FAT_INTERVAL_SEC = 0.12         # каденс жирного режима
FAT_CAP = 600                   # кап проб окна в жирном режиме
POLL_QUANTUM_SEC = 0.15         # квант петли — фактический пол каденса
SEND_FLOOR_SEC = 0.10           # физический пол интервала отправки (_dense_interval)

ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
SEL_AGGREGATOR = "0x245a7bfc"        # aggregator()
SEL_LATEST_ROUND = "0xfeaf968c"      # latestRoundData()
SEL_DECIMALS = "0x313ce567"          # decimals()

RPCS = ["https://mainnet.base.org", "https://base.drpc.org",
        "https://base.gateway.tenderly.co", "https://developer-access-mainnet.base.org",
        "https://base.lava.build"]
BINANCE = "https://data-api.binance.vision"
LOG_CHUNK = 10000               # оба публичных эндпоинта: жёсткий кап диапазона getLogs
# Кэш ВНЕ репозитория: секундные klines за неделю — сотни МБ, в рабочем дереве им не место
# (одно `git add -A` — и они в истории). Переопределяется PUSH_HAZARD_CACHE.
CACHE_DIR = os.environ.get("PUSH_HAZARD_CACHE") or os.path.expanduser("~/.cache/push_hazard")


def log(msg):
    print(msg, flush=True)


class Fail(Exception):
    """Позитивный контроль/предпосылка не выполнена — падаем громко, выводы не печатаем."""


# ---------------------------------------------------------------- транспорт
class Rpc:
    """Round-robin по публичным эндпоинтам + бэкофф на rate-limit. Только чтения."""

    def __init__(self, urls=None):
        self.urls = list(urls or RPCS)
        self._i = 0
        self._lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({"content-type": "application/json", "user-agent": "Mozilla/5.0 research"})
        self.calls = 0

    def call(self, method, params, tries=14):
        last = None
        for k in range(tries):
            with self._lock:
                url = self.urls[self._i % len(self.urls)]
                self._i += 1
                self.calls += 1
            try:
                r = self.s.post(url, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                      "method": method, "params": params}),
                                timeout=45)
                j = r.json()
                if "error" in j:
                    last = "%s: %s" % (url, str(j["error"])[:140])
                    if "rate" in last.lower() or "limit" in last.lower() or r.status_code == 429:
                        time.sleep(min(0.4 * (2 ** k), 20.0))
                        continue
                    raise Fail("RPC %s -> %s" % (method, last))
                return j["result"]
            except Fail:
                raise
            except Exception as e:
                last = "%s: %s" % (url, type(e).__name__)
                time.sleep(min(0.4 * (2 ** k), 8.0))
        raise Fail("RPC %s не отвечает после %d попыток (%s)" % (method, tries, last))


def _s256(h: str) -> int:
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


# ---------------------------------------------------------------- ончейн: агрегаторы и пуши
def resolve_aggregators(rpc: Rpc, labels):
    out = {}
    for lb in labels:
        px = PROXIES[lb]
        agg = "0x" + rpc.call("eth_call", [{"to": px, "data": SEL_AGGREGATOR}, "latest"])[-40:]
        dec_raw = rpc.call("eth_call", [{"to": px, "data": SEL_DECIMALS}, "latest"])
        dec = int(dec_raw, 16) if dec_raw and dec_raw != "0x" else 8
        out[lb] = {"proxy": px, "agg": agg, "decimals": dec}
        log("  %-5s proxy=%s -> agg=%s dec=%d" % (lb, px, agg, dec))
    return out


class BlockClock:
    """Base тикает ровно 2с. Держим линейную карту block->ts и ВАЛИДИРУЕМ её на выборке
    реальных блоков; расхождение >2с -> честный фолбэк на поблочный запрос (с кэшем)."""

    def __init__(self, rpc: Rpc, head: int):
        self.rpc = rpc
        self.linear = True
        self._cache = {}
        b0 = self.fetch_ts(head)
        b1 = self.fetch_ts(head - 100000)
        self.ref_b, self.ref_t = head, b0
        step = (b0 - b1) / 100000.0
        self.step = step
        log("  block clock: шаг %.4f с/блок (%d..%d)" % (step, head - 100000, head))
        if abs(step - 2.0) > 1e-6:
            self.linear = False

    def fetch_ts(self, b: int) -> int:
        if b in self._cache:
            return self._cache[b]
        r = self.rpc.call("eth_getBlockByNumber", [hex(b), False])
        t = int(r["timestamp"], 16)
        self._cache[b] = t
        return t

    def ts(self, b: int) -> int:
        if not self.linear:
            return self.fetch_ts(b)
        return int(self.ref_t + self.step * (b - self.ref_b))

    def block_at(self, t: float) -> int:
        return int(self.ref_b + (t - self.ref_t) / self.step)

    def validate(self, blocks):
        bad = []
        for b in blocks:
            got = self.fetch_ts(b)
            if abs(got - self.ts(b)) > 2:
                bad.append((b, got, self.ts(b)))
        if bad:
            log("  ! линейная карта времени НЕ валидна на %d/%d выборках -> поблочный режим"
                % (len(bad), len(blocks)))
            self.linear = False
        else:
            log("  block clock валидирован на %d случайных блоках (расхождение <=2с)" % len(blocks))


def fetch_pushes(rpc: Rpc, agg: str, b_from: int, b_to: int, workers: int = 3):
    """Все AnswerUpdated агрегатора в [b_from, b_to]. Чанки по LOG_CHUNK (кап публичных RPC).
    Кэш на диск: публичные эндпоинты душат лимитом, повторный прогон не должен их доить."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, "logs_%s_%d_%d.json" % (agg[-8:], b_from, b_to))
    if os.path.exists(cpath):
        with open(cpath) as f:
            return json.load(f)
    chunks = []
    b = b_from
    while b <= b_to:
        e = min(b + LOG_CHUNK - 1, b_to)
        chunks.append((b, e))
        b = e + 1
    out = []

    def one(rng):
        f, t = rng
        res = rpc.call("eth_getLogs", [{"fromBlock": hex(f), "toBlock": hex(t),
                                        "address": agg, "topics": [ANSWER_UPDATED]}])
        rows = []
        for lg in res:
            tp = lg["topics"]
            rows.append({"block": int(lg["blockNumber"], 16),
                         "tx_index": int(lg.get("transactionIndex", "0x0"), 16),
                         "answer": _s256(tp[1]),
                         "round": int(tp[2], 16),
                         "updated_at": int(lg["data"][2:66], 16) if len(lg["data"]) >= 66 else 0})
        return rows

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(one, chunks):
            out += rows
    out.sort(key=lambda r: (r["block"], r["tx_index"]))
    with open(cpath, "w") as f:
        json.dump(out, f)
    return out


# ---------------------------------------------------------------- off-chain: Binance 1s klines
_bs = requests.Session()
_bs.headers.update({"user-agent": "Mozilla/5.0 research"})


def _klines(symbol, start_ms, limit=1000, interval="1s"):
    for k in range(8):
        try:
            r = _bs.get(BINANCE + "/api/v3/klines",
                        params={"symbol": symbol, "interval": interval,
                                "startTime": start_ms, "limit": limit}, timeout=40)
            if r.status_code == 429 or r.status_code == 418:
                time.sleep(min(1.0 * (2 ** k), 30.0))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(min(0.5 * (2 ** k), 15.0))
    raise Fail("Binance klines %s не отвечает" % symbol)


def fetch_series(symbol, t0: int, t1: int, workers: int = 6):
    """Непрерывная секундная серия (close, close_time) на [t0, t1]. Кэш на диск: повторный
    прогон не бьёт по API. Возвращает (times[list int сек], prices[list float])."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, "%s_%d_%d.json" % (symbol, t0, t1))
    if os.path.exists(cpath):
        with open(cpath) as f:
            d = json.load(f)
        return d["t"], d["p"]
    starts = list(range(t0, t1, 1000))

    def one(st):
        return _klines(symbol, st * 1000, limit=1000)

    rows = []
    done = [0]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(one, starts):
            rows += part
            done[0] += 1
            if done[0] % 100 == 0:
                log("    %s klines %d/%d" % (symbol, done[0], len(starts)))
    seen = {}
    for r in rows:
        ot = int(r[0]) // 1000
        if t0 <= ot <= t1:
            seen[ot] = float(r[4])          # close
    ts = sorted(seen)
    if not ts:
        raise Fail("Binance: пустая серия для %s" % symbol)
    t, p = [], []
    last = seen[ts[0]]
    for sec in range(ts[0], ts[-1] + 1):    # forward-fill секунд без сделок
        if sec in seen:
            last = seen[sec]
        t.append(sec)
        p.append(last)
    with open(cpath, "w") as f:
        json.dump({"t": t, "p": p}, f)
    return t, p


# ---------------------------------------------------------------- реплей правила арма
def replay(times, prices, pushes, dev, arm_ratio):
    """ТОЧНЫЙ порт семантики chain/predict.py::PredictEngine + горячая фаза
    chain/spec_fire_live.py::_window_loop, прогнанный по секундной серии.

    times/prices — off-chain путь (сек, цена); pushes — [(t_push_sec, ...)] по возрастанию.
    Возвращает список записей по каждому пушу (кроме первого — он даёт только якорь)."""
    arm_pct = dev * arm_ratio
    disarm_pct = min(dev * DISARM_RATIO, arm_pct * 0.85)
    thr_hot = dev * SPEC_FIRE_THRESHOLD
    band = dev * OWED_BAND
    idx = {t: i for i, t in enumerate(times)}
    n = len(times)

    def price_at(t):
        i = idx.get(int(t))
        if i is None:
            i = min(range(n), key=lambda j: abs(times[j] - t)) if n else None
            if i is None or abs(times[i] - t) > 30:
                return None
        return prices[i]

    out = []
    for k in range(1, len(pushes)):
        t_prev, t_now = pushes[k - 1], pushes[k]
        anchor = price_at(t_prev)
        if anchor is None or anchor <= 0:
            out.append({"t_push": t_now, "gap": t_now - t_prev, "state": "no-anchor"})
            continue
        i0 = idx.get(int(t_prev))
        i1 = idx.get(int(t_now))
        if i0 is None or i1 is None or i1 <= i0:
            out.append({"t_push": t_now, "gap": t_now - t_prev, "state": "no-series"})
            continue
        armed = False
        suppressed = False
        t_arm = None
        arm_ret = None
        t_hot = None            # первый raw-hot ТЕКУЩЕГО армленного эпизода
        owed_until = 0.0
        owed_ret = 0.0
        peak = 0.0
        hot_secs = 0            # сколько секунд эпизода петля реально стреляла бы
        for i in range(i0 + 1, i1 + 1):
            t = times[i]
            r = (prices[i] - anchor) / anchor
            mag = abs(r)
            if not armed:
                if suppressed and mag < disarm_pct:
                    suppressed = False
                if not suppressed and mag >= arm_pct:
                    armed, t_arm, arm_ret, peak = True, t, r, r
                    t_hot, owed_until, owed_ret, hot_secs = None, 0.0, 0.0, 0
            else:
                if mag > abs(peak):
                    peak = r
                if mag < disarm_pct:
                    # ретрейс: sticky — при заряженном транзите окно НЕ умирает (peak-owed)
                    if owed_until > t:
                        pass
                    else:
                        armed, t_arm, t_hot = False, None, None
                        owed_ret, peak = 0.0, 0.0
                        continue
                if t_arm is not None and (t - t_arm) >= HOLD_SEC and owed_until <= t:
                    armed, suppressed, t_arm, t_hot = False, True, None, None   # falsepos-кап
                    owed_ret, peak = 0.0, 0.0
                    continue
            if armed:
                # направление окна фиксируется армом (dir), огонь — по знаку арма
                dirn = 1.0 if (arm_ret or 0.0) >= 0 else -1.0
                signed = r * dirn
                raw_hot = signed >= thr_hot
                if raw_hot:
                    owed_ret = max(owed_ret, mag)
                    owed_until = t + OWED_HOLD_SEC
                elif owed_until > t and signed >= (thr_hot - band):
                    owed_until = t + OWED_HOLD_SEC
                fires = raw_hot or (owed_until > t and owed_ret > 0.0)
                if fires:
                    if t_hot is None:
                        t_hot = t
                    hot_secs += 1
        # S_hot = СЕКУНДЫ ОГНЯ, накопленные к моменту пуша. Это и есть часы расхода бюджета:
        # кап 600 проб при каденсе POLL_QUANTUM тратится ровно за 600*0.15 = 90 таких секунд,
        # а не за 90с настенного времени (в эпизоде бывают холодные провалы).
        rec = {"t_push": t_now, "t_prev": t_prev, "gap": t_now - t_prev,
               "state": "armed" if armed else "unarmed",
               "ret_at_push": (prices[i1] - anchor) / anchor,
               "S_hot": hot_secs}
        if armed and t_arm is not None:
            rec["L_arm"] = t_now - t_arm
            rec["arm_ret_pct"] = (arm_ret or 0.0) * 100.0
            rec["peak_ret_pct"] = peak * 100.0
        if armed and t_hot is not None:
            rec["L_hot"] = t_now - t_hot
        out.append(rec)
    return out


# ---------------------------------------------------------------- статистика
def quantiles(xs, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    if not xs:
        return {}
    s = sorted(xs)
    out = {}
    for q in qs:
        i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        out["p%d" % int(q * 100)] = s[i]
    out["min"], out["max"], out["n"] = s[0], s[-1], len(s)
    out["mean"] = sum(s) / len(s)
    return out


def histogram(xs, edges):
    h = [0] * (len(edges) - 1)
    for x in xs:
        for i in range(len(edges) - 1):
            if edges[i] <= x < edges[i + 1]:
                h[i] += 1
                break
    return h


def best_interval(samples, horizon, min_width, grid=1.0):
    """max_[a,b] (масса в [a,b]) / (b-a) при b-a >= min_width. Возвращает (gain, a, b, mass).
    gain = плотность выбранного интервала, делённая на равномерную 1/horizon."""
    if not samples:
        return (0.0, 0.0, 0.0, 0.0)
    n = len(samples)
    nb = int(math.ceil(horizon / grid))
    cnt = [0] * (nb + 1)
    for x in samples:
        if 0 <= x < horizon:
            cnt[int(x / grid)] += 1
    cum = [0] * (nb + 2)
    for i in range(nb + 1):
        cum[i + 1] = cum[i] + cnt[i]
    best = (0.0, 0.0, horizon, 0.0)
    wmin = max(1, int(math.ceil(min_width / grid)))
    for ia in range(0, nb):
        for w in range(wmin, nb - ia + 1):
            mass = (cum[ia + w] - cum[ia]) / n
            dens = mass / (w * grid)
            g = dens * horizon
            if g > best[0]:
                best = (g, ia * grid, (ia + w) * grid, mass)
    return best


def shift_gain(samples, cover, horizon, grid=1.0):
    """Реализуемый выигрыш при УЖЕ предельной плотности: сдвинуть окно покрытия шириной
    `cover` в лучшее место против старта с нуля. Возвращает (gain, a, mass_best, mass_at0)."""
    if not samples:
        return (0.0, 0.0, 0.0, 0.0)
    n = len(samples)
    nb = int(math.ceil(horizon / grid))
    cnt = [0] * (nb + 1)
    for x in samples:
        if 0 <= x < horizon:
            cnt[int(x / grid)] += 1
    cum = [0] * (nb + 2)
    for i in range(nb + 1):
        cum[i + 1] = cum[i] + cnt[i]
    w = max(1, int(round(cover / grid)))
    m0 = (cum[min(nb + 1, w)] - cum[0]) / n
    best_a, best_m = 0.0, m0
    for ia in range(0, max(1, nb - w + 1)):
        m = (cum[min(nb + 1, ia + w)] - cum[ia]) / n
        if m > best_m:
            best_m, best_a = m, ia * grid
    return ((best_m / m0) if m0 > 0 else float("inf"), best_a, best_m, m0)


def waterfill(samples, horizon, budget, rate_cap, rate_now, cover_now, grid=1.0):
    """ФИЗИЧНАЯ постановка: probes/сек lambda(t) ограничена сверху rate_cap (пол интервала
    отправки), интеграл lambda = budget. P(win) ~ integral f(t)*lambda(t). Оптимум — залить
    бюджет по убыванию плотности (water-filling), СЕЙЧАС бюджет льётся ровно rate_now на
    [0, cover_now]. Возвращает коэффициент роста P(win) и накрытые корзины."""
    if not samples:
        return {}
    n = len(samples)
    nb = int(math.ceil(horizon / grid))
    cnt = [0] * (nb + 1)
    for x in samples:
        if 0 <= x < horizon:
            cnt[int(x / grid)] += 1
    # текущая политика: rate_now на [0, cover_now]
    now_mass = sum(cnt[i] for i in range(0, min(nb + 1, int(cover_now / grid)))) / n
    score_now = rate_now * now_mass
    # water-filling: bins по убыванию плотности, в каждый льём rate_cap, пока хватает бюджета
    width = budget / rate_cap                     # сколько секунд можно накрыть на пределе
    order = sorted(range(nb + 1), key=lambda i: -cnt[i])
    take, left = [], width
    for i in order:
        if left <= 0:
            break
        w = min(grid, left)
        take.append((i, w))
        left -= w
    opt_mass = sum((cnt[i] / n) * (w / grid) for i, w in take)
    score_opt = rate_cap * opt_mass
    # то же, но НЕПРЕРЫВНЫМ интервалом (реализуемо одним сдвигом старта огня)
    wc = max(1, int(round(width / grid)))
    cum = [0] * (nb + 2)
    for i in range(nb + 1):
        cum[i + 1] = cum[i] + cnt[i]
    best_m, best_a = -1.0, 0.0
    for ia in range(0, max(1, nb - wc + 2)):
        m = (cum[min(nb + 1, ia + wc)] - cum[ia]) / n
        if m > best_m:
            best_m, best_a = m, ia * grid
    score_cont = rate_cap * best_m
    return {"score_now": score_now, "mass_now": now_mass, "rate_now": rate_now,
            "cover_now_s": cover_now, "rate_cap": rate_cap, "opt_width_s": width,
            "gain_waterfill": (score_opt / score_now) if score_now > 0 else float("inf"),
            "mass_waterfill": opt_mass,
            "gain_contiguous": (score_cont / score_now) if score_now > 0 else float("inf"),
            "contiguous_a": best_a, "mass_contiguous": best_m,
            "gain_retime_only": (best_m / now_mass) if now_mass > 0 else float("inf")}


def oos_gain(samples, horizon, min_width, reps=200, seed=7):
    """Честная проверка ставки: интервал выбирается на ПОЛОВИНЕ выборки, выигрыш меряется на
    другой половине. Плюс placebo: та же процедура на равномерном шуме того же размера."""
    rnd = random.Random(seed)
    real, plac = [], []
    for _ in range(reps):
        s = list(samples)
        rnd.shuffle(s)
        h = len(s) // 2
        tr, te = s[:h], s[h:]
        if not tr or not te:
            continue
        g, a, b, _ = best_interval(tr, horizon, min_width)
        mass = sum(1 for x in te if a <= x < b) / len(te)
        real.append(mass / (b - a) * horizon if b > a else 0.0)
        u = [rnd.uniform(0, horizon) for _ in samples]
        rnd.shuffle(u)
        gt, ga, gb, _ = best_interval(u[:h], horizon, min_width)
        um = sum(1 for x in u[h:] if ga <= x < gb) / max(1, len(u) - h)
        plac.append(um / (gb - ga) * horizon if gb > ga else 0.0)
    return quantiles(real, (0.1, 0.5, 0.9)), quantiles(plac, (0.1, 0.5, 0.9))


def lattice_scan(gaps, periods=range(4, 181), tols=(0, 2, 4, 6, 10)):
    """ФАЗОВАЯ РЕШЁТКА: OCR-раунд тикает с внутренним периодом, поэтому пуш может сесть только
    на кратном ему моменте от ПРЕДЫДУЩЕГО пуша. Ищем период P, при котором остаток gap mod P
    липнет к нулю сильнее случайного. lift = доля попаданий / (доля при равномерности) —
    это и есть проверка «структура или совпадение»."""
    out = []
    n = len(gaps)
    if n < 10:
        return out
    for P in periods:
        row = {"P": P}
        for tol in tols:
            hit = sum(1 for g in gaps if min(g % P, P - (g % P)) <= tol)
            frac = hit / n
            chance = min(1.0, (2 * tol + 1) / P)
            row["f%d" % tol] = frac
            row["lift%d" % tol] = frac / chance if chance > 0 else 0.0
        out.append(row)
    return out


def lattice_pick(scan, tol_key="f2", min_frac=0.9, min_P=8):
    """Выбор ФУНДАМЕНТАЛЬНОГО периода. lift сам по себе не различает P и 2P (у обоих он равен),
    а покрытие f — различает: при истинном P=30 доля попаданий у P=30 высокая, у P=60 падает
    вдвое, у P=15 остаётся высокой но зря дробит. Значит истинный период = САМЫЙ БОЛЬШОЙ P,
    сохраняющий покрытие. Малые P (<=6) покрывают всё тривиально (сетка блоков 2с) — это НЕ
    решётка, поэтому min_P отсекает вырожденный ответ."""
    ok = [r for r in scan if r[tol_key] >= min_frac and r["P"] >= min_P]
    if not ok:
        return None
    return max(ok, key=lambda r: r["P"])


def rayleigh_R(vals, period):
    """Круговая концентрация значений по модулю period. R ~ 1/sqrt(n) при равномерности."""
    if not vals:
        return 0.0
    sx = sy = 0.0
    for v in vals:
        a = 2 * math.pi * ((v % period) / period)
        sx += math.cos(a)
        sy += math.sin(a)
    return math.hypot(sx, sy) / len(vals)


# ---------------------------------------------------------------- позитивные контроли
def pc3_synthetic_replay():
    """PC3: реплей ОБЯЗАН уметь вернуть непустой результат. Синтетическая рампа: цена ползёт
    вниз на 1%/300с при dev=0.5%, arm_ratio=0.65 -> арм на -0.325% (t≈97с), горячая фаза на
    -0.5% (t≈150с), пуш на t=200с. Ждём L_arm≈103, L_hot≈50."""
    dev, ratio = 0.005, 0.65
    t0 = 1700000000
    times = list(range(t0, t0 + 401))
    prices = [100.0 * (1.0 - 0.01 * max(0, (t - t0)) / 300.0) for t in times]
    recs = replay(times, prices, [t0, t0 + 200], dev, ratio)
    if not recs or recs[0].get("state") != "armed":
        raise Fail("PC3 ПРОВАЛЕН: синтетическая рампа не армит — реплей не способен вернуть "
                   "непустой результат (%r)" % recs)
    L_arm, L_hot = recs[0].get("L_arm"), recs[0].get("L_hot")
    if L_arm is None or L_hot is None or not (95 <= L_arm <= 112) or not (42 <= L_hot <= 58):
        raise Fail("PC3 ПРОВАЛЕН: рампа армит не там, где обязана (L_arm=%s ждали ~103, "
                   "L_hot=%s ждали ~50)" % (L_arm, L_hot))
    log("  PC3 OK: синтетическая рампа -> arm за %ds до пуша, hot за %ds (ожидание ~103/~50)"
        % (L_arm, L_hot))
    # контроль ЗНАМЕНАТЕЛЯ: плоская цена НЕ должна армить (иначе реплей армит что угодно)
    flat = replay(times, [100.0] * len(times), [t0, t0 + 200], dev, ratio)
    if flat and flat[0].get("state") == "armed":
        raise Fail("PC3 ПРОВАЛЕН: плоская цена армит окно — правило не различает движение")
    log("  PC3 OK: плоская цена НЕ армит (отрицательный контроль реплея)")


def pc1_pc2(rpc, clock, label, meta, pushes, times, prices):
    """PC1: свежайший AnswerUpdated == latestRoundData() того же агрегатора (сигнатура+декод).
    PC2: медиана Binance-серии сходится с ончейн-ценой (серия принадлежит тому же активу)."""
    if not pushes:
        raise Fail("PC1 ПРОВАЛЕН для %s: getLogs не вернул НИ ОДНОГО AnswerUpdated" % label)
    raw = rpc.call("eth_call", [{"to": meta["agg"], "data": SEL_LATEST_ROUND}, "latest"])
    body = raw[2:]
    on_answer = _s256(body[64:128])
    on_updated = int(body[192:256], 16)
    last = pushes[-1]
    if last["answer"] != on_answer or last["updated_at"] != on_updated:
        # допускаем ОДИН новый пуш между getLogs и этим чтением
        if not (last["updated_at"] <= on_updated):
            raise Fail("PC1 ПРОВАЛЕН для %s: последний лог (ans=%d upd=%d) не сходится с "
                       "latestRoundData (ans=%d upd=%d)"
                       % (label, last["answer"], last["updated_at"], on_answer, on_updated))
        log("  PC1 OK(*) %s: последний лог упд=%d <= latestRoundData упд=%d (пуш прилетел "
            "после выборки логов)" % (label, last["updated_at"], on_updated))
    else:
        log("  PC1 OK %s: последний лог == latestRoundData (ans=%.6f, round=%d) — сигнатура и "
            "декод доказаны" % (label, on_answer / 10 ** meta["decimals"], last["round"]))
    # PC2: цена ПУША против Binance В ТОТ ЖЕ МОМЕНТ. Сравнивать 7-дневную медиану с текущим
    # спотом бессмысленно (цена за неделю уходит на проценты) — это проверяло бы не то.
    # Здесь проверяется И актив, И ВРЕМЕННОЕ ВЫРАВНИВАНИЕ: оракул пушит близко к рынку.
    idx = {t: i for i, t in enumerate(times)}
    scale = 10 ** meta["decimals"]
    errs, errs_shift = [], []
    SHIFT = 86400                              # placebo: та же серия, сдвинутая на сутки
    for r in pushes:
        i = idx.get(int(r["ts"]))
        j = idx.get(int(r["ts"]) - SHIFT)
        px = r["answer"] / scale
        if i is not None and px > 0:
            errs.append(abs(prices[i] - px) / px)
        if j is not None and px > 0:
            errs_shift.append(abs(prices[j] - px) / px)
    if not errs:
        raise Fail("PC2 ПРОВАЛЕН для %s: ни один пуш не лёг на секундную серию (выравнивание "
                   "времени сломано)" % label)
    med = sorted(errs)[len(errs) // 2]
    med_sh = sorted(errs_shift)[len(errs_shift) // 2] if errs_shift else None
    if med > 0.005:
        raise Fail("PC2 ПРОВАЛЕН для %s: медианное расхождение оракул↔Binance в момент пуша "
                   "%.3f%% (>0.5%%) — не тот актив либо съехало время" % (label, med * 100))
    if med_sh is not None and med_sh <= med * 2:
        raise Fail("PC2 ПРОВАЛЕН для %s: PLACEBO (серия, сдвинутая на сутки) даёт %.3f%% против "
                   "%.3f%% — выравнивание времени НЕ несёт информации" % (label, med_sh * 100, med * 100))
    log("  PC2 OK %s: медиана |оракул − Binance| в момент пуша %.3f%% на %d пушах "
        "(placebo со сдвигом на сутки: %.3f%% — в %.0f раз хуже, выравнивание доказано)"
        % (label, med * 100, len(errs), (med_sh or 0) * 100,
           (med_sh / med) if (med_sh and med) else 0))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--feeds", default="XRP,ADA,SOL,DOGE,LTC,BTC,ETH")
    ap.add_argument("--arm-ratio", type=float, default=ARM_RATIO_DEFAULT)
    ap.add_argument("--horizon", type=float, default=0.0, help="0 = p95 выборки")
    ap.add_argument("--out", default="out/push_hazard.json")
    ap.add_argument("--pc-only", action="store_true", help="только позитивные контроли")
    args = ap.parse_args()

    labels = [x.strip().upper() for x in args.feeds.split(",") if x.strip()]
    for lb in labels:
        if lb not in FEEDS:
            raise Fail("неизвестный фид %s (есть: %s)" % (lb, ",".join(FEEDS)))

    log("==== PUSH-HAZARD: %s, %.1f дн, arm_ratio=%.2f ====" % (",".join(labels), args.days, args.arm_ratio))
    log("[0] ПОЗИТИВНЫЙ КОНТРОЛЬ реплея (не зависит от данных)")
    pc3_synthetic_replay()
    if args.pc_only:
        pass

    rpc = Rpc()
    head = int(rpc.call("eth_blockNumber", []), 16)
    log("\n[1] Резолв агрегаторов (proxy.aggregator(), живьём) — head=%d" % head)
    meta = resolve_aggregators(rpc, labels)
    clock = BlockClock(rpc, head)
    span_blocks = int(args.days * 86400 / clock.step)
    # окно выравнено по сетке LOG_CHUNK: повторный прогон попадает в тот же дисковый кэш и
    # не доит публичные эндпоинты (их лимит — узкое место, а не наша логика)
    b_to = ((head - 5) // LOG_CHUNK) * LOG_CHUNK
    b_from = b_to - span_blocks
    clock.validate([b_from, b_from + span_blocks // 3, b_from + 2 * span_blocks // 3, b_to])
    t_from, t_to = clock.ts(b_from), clock.ts(b_to)
    log("  окно: блоки %d..%d, время %s .. %s" % (b_from, b_to, time.ctime(t_from), time.ctime(t_to)))

    log("\n[2] Сбор пушей (AnswerUpdated) — %d чанков×%d блоков на фид"
        % (math.ceil(span_blocks / LOG_CHUNK), LOG_CHUNK))
    pushes = {}
    for lb in labels:
        rows = fetch_pushes(rpc, meta[lb]["agg"], b_from, b_to)
        for r in rows:
            r["ts"] = clock.ts(r["block"])
        pushes[lb] = rows
        log("  %-5s пушей=%-5d  (%.1f/сут)  round %d..%d"
            % (lb, len(rows), len(rows) / args.days,
               rows[0]["round"] if rows else -1, rows[-1]["round"] if rows else -1))

    log("\n[3] Binance 1s klines (кэш: %s)" % CACHE_DIR)
    series = {}
    for lb in labels:
        sym = FEEDS[lb][0]
        t, p = fetch_series(sym, t_from - 60, t_to + 60)
        series[lb] = (t, p)
        log("  %-5s %s: точек=%d (покрытие %.1f%%)"
            % (lb, sym, len(t), 100.0 * len(t) / max(1, (t_to - t_from + 120))))

    log("\n[4] ПОЗИТИВНЫЙ КОНТРОЛЬ данных (PC1 ончейн/декод, PC2 спаривание Binance)")
    for lb in labels:
        pc1_pc2(rpc, clock, lb, meta[lb], pushes[lb], *series[lb])

    log("\n[5] Реплей правила арма по каждому пушу")
    result = {"window": {"days": args.days, "b_from": b_from, "b_to": b_to,
                         "t_from": t_from, "t_to": t_to, "arm_ratio": args.arm_ratio},
              "feeds": {}}
    all_Larm, all_Lhot, all_S = [], [], []
    per_feed, recs_by_feed = {}, {}
    budget_cover = FAT_CAP * POLL_QUANTUM_SEC       # 600×0.15 = 90 секунд ОГНЯ на весь кап
    rate_now_sim = 1.0 / POLL_QUANTUM_SEC           # проб/сек сейчас (~6.7 = 13 проб/блок)
    for lb in labels:
        dev = FEEDS[lb][1]
        t, p = series[lb]
        recs = replay(t, p, [r["ts"] for r in pushes[lb]], dev, args.arm_ratio)
        armed = [r for r in recs if r["state"] == "armed"]
        Larm = [r["L_arm"] for r in armed if "L_arm" in r]
        Lhot = [r["L_hot"] for r in armed if "L_hot" in r]
        Shot = [r["S_hot"] for r in armed if "L_hot" in r]
        gaps = [r["gap"] for r in recs]
        covered = sum(1 for s in Shot if s <= budget_cover)
        recs_by_feed[lb] = recs
        res = {"dev_pct": dev * 100.0, "arm_pct": dev * args.arm_ratio * 100.0,
               "pushes": len(pushes[lb]), "replayed": len(recs),
               "armed": len(armed), "recall": (len(armed) / len(recs)) if recs else 0.0,
               "hot": len(Lhot), "hot_share": (len(Lhot) / len(recs)) if recs else 0.0,
               "L_arm": quantiles(Larm), "L_hot": quantiles(Lhot), "S_hot": quantiles(Shot),
               "covered_by_cap": covered,
               "covered_share_of_hot": (covered / len(Shot)) if Shot else 0.0,
               "covered_share_of_pushes": (covered / len(recs)) if recs else 0.0,
               "gap": quantiles(gaps, (0.1, 0.25, 0.5, 0.75, 0.9))}
        result["feeds"][lb] = res
        per_feed[lb] = {"L_hot": Lhot, "L_arm": Larm, "S_hot": Shot}
        all_Larm += Larm
        all_Lhot += Lhot
        all_S += Shot
        q, qs = res["L_hot"], res["S_hot"]
        log("  %-5s пушей=%-4d реплей=%-4d armed=%-4d (recall %.0f%%) hot=%-4d (%.0f%%)  "
            "L_hot p10/50/90 = %s/%s/%s c | S_hot(секунды огня) p50=%s p90=%s | "
            "накрыто капом 600 проб: %.0f%% горячих"
            % (lb, res["pushes"], res["replayed"], res["armed"], res["recall"] * 100.0,
               res["hot"], res["hot_share"] * 100.0,
               q.get("p10", "-"), q.get("p50", "-"), q.get("p90", "-"),
               qs.get("p50", "-"), qs.get("p90", "-"), res["covered_share_of_hot"] * 100.0))

    if not all_Lhot:
        raise Fail("PC4 ПРОВАЛЕН: ни один пуш не восстановил горячую фазу — недоказанный ноль, "
                   "выводы не печатаем")
    log("  PC4 OK: реплей армил на РЕАЛЬНЫХ данных (%d армов, %d с горячей фазой)"
        % (len(all_Larm), len(all_Lhot)))

    log("\n[6] Квантование раундов")
    quant = {}
    for lb in labels:
        rows = pushes[lb]
        if len(rows) < 10:
            continue
        d_round = [rows[i]["round"] - rows[i - 1]["round"] for i in range(1, len(rows))]
        d_ts = [rows[i]["ts"] - rows[i - 1]["ts"] for i in range(1, len(rows))]
        lag = [r["ts"] - r["updated_at"] for r in rows]
        small = [g for g in d_ts if g <= 300]
        # период 2с ИСКЛЮЧЁН: Base тикает ровно 2с и updatedAt == block.timestamp (см. lag),
        # поэтому R(2)=1 тривиально и структурой оракула НЕ является.
        rr, rr_gap = {}, {}
        chance = 1.0 / math.sqrt(len(rows))
        for per in (4, 6, 8, 10, 12, 16, 20, 30, 40, 60, 90, 120, 300, 600):
            rr[per] = round(rayleigh_R([r["updated_at"] for r in rows], per), 4)
            rr_gap[per] = round(rayleigh_R(d_ts, per), 4)
        quant[lb] = {"d_round": quantiles(d_round, (0.5,)), "gap": quantiles(d_ts),
                     "gap_small_n": len(small), "gap_small": quantiles(small),
                     "block_lag_s": quantiles(lag), "rayleigh_push_ts": rr,
                     "rayleigh_gap": rr_gap, "rayleigh_chance": chance,
                     "d_round_not_1": sum(1 for d in d_round if d != 1)}
        top = sorted(rr.items(), key=lambda kv: -kv[1])[:3]
        topg = sorted(rr_gap.items(), key=lambda kv: -kv[1])[:3]
        log("  %-5s dround p50=%s (не-1: %d)  gap p50=%ss  lag(block-updatedAt) p50=%ss"
            % (lb, quant[lb]["d_round"].get("p50"), quant[lb]["d_round_not_1"],
               quant[lb]["gap"].get("p50"), quant[lb]["block_lag_s"].get("p50")))
        log("        R(ts mod p) top: %s | R(gap mod p) top: %s | шум %.3f"
            % (", ".join("%ds=%.3f" % (k, v) for k, v in top),
               ", ".join("%ds=%.3f" % (k, v) for k, v in topg), chance))
        if small:
            log("        малые гэпы (<=300c): n=%d p10/50/90=%s/%s/%s"
                % (len(small), quant[lb]["gap_small"].get("p10"),
                   quant[lb]["gap_small"].get("p50"), quant[lb]["gap_small"].get("p90")))
    result["quantization"] = quant

    log("\n[6b] ФАЗОВАЯ РЕШЁТКА (пуш может сесть только на кратном OCR-периоду моменте?)")
    lat = {}
    for lb in labels:
        rows = pushes[lb]
        if len(rows) < 15:
            log("  %-5s мало пушей (%d) — решётку не заявляем" % (lb, len(rows)))
            continue
        gaps = [rows[i]["ts"] - rows[i - 1]["ts"] for i in range(1, len(rows))]
        sc = lattice_scan(gaps)
        best = lattice_pick(sc)
        by_p = {r["P"]: r for r in sc}
        if best is None:
            lat[lb] = {"n_gaps": len(gaps), "best": None, "P30": by_p.get(30)}
            log("  %-5s n=%-4d решётки НЕТ (ни один период >=8с не держит 90%% попаданий в ±2с)"
                % (lb, len(gaps)))
            continue
        lat[lb] = {"n_gaps": len(gaps), "best": best, "P30": by_p.get(30),
                   "scan_hi": [r for r in sc if r["f2"] >= 0.8]}
        log("  %-5s n=%-4d ФУНДАМЕНТАЛЬНЫЙ период P=%ds: ±0с %.0f%% (lift ×%.1f), ±2с %.0f%% "
            "(×%.1f), ±4с %.0f%% (×%.1f)"
            % (lb, len(gaps), best["P"], best["f0"] * 100, best["lift0"],
               best["f2"] * 100, best["lift2"], best["f4"] * 100, best["lift4"]))
        if best["P"] != 30 and by_p.get(30):
            r = by_p[30]
            log("        (P=30с для сравнения: ±2с %.0f%% ×%.1f, ±4с %.0f%% ×%.1f)"
                % (r["f2"] * 100, r["lift2"], r["f4"] * 100, r["lift4"]))
    result["lattice"] = lat
    # ВЫГОДА РЕШЁТКИ: если пуш садится только у границ раунда, огонь между границами — чистый
    # холостой расход. Гейт по фазе увеличивает ЧИСЛО накрытых границ на том же бюджете.
    lat_ok = {k: v for k, v in lat.items() if v.get("best")}
    if lat_ok:
        Ps = [v["best"]["P"] for v in lat_ok.values()]
        P = max(set(Ps), key=Ps.count)
        cover_w = 6.0                                  # окно у границы: ±2с промаха + блок
        probes_per_border = cover_w / POLL_QUANTUM_SEC
        borders_now = budget_cover / P
        borders_gated = FAT_CAP / probes_per_border
        log("  ИТОГ: модальный период P=%dс; окно у границы %.0fс = %.0f проб; сейчас бюджет "
            "накрывает %.1f границ, с фазовым гейтом — %.0f границ (×%.1f по числу границ, "
            "то есть по НАКРЫТОМУ времени огня %.0fс -> %.0fс)"
            % (P, cover_w, probes_per_border, borders_now, borders_gated,
               borders_gated / max(borders_now, 1e-9), budget_cover, borders_gated * P))
        lat["_summary"] = {"P": P, "cover_w": cover_w, "borders_now": borders_now,
                           "borders_gated": borders_gated,
                           "hot_secs_covered_now": budget_cover,
                           "hot_secs_covered_gated": borders_gated * P}
        # честная цена: сколько МАССЫ пушей попадает в накрытое время огня в обоих режимах
        for nm, samples in (("ALL", all_S),):
            m_now = sum(1 for s in samples if s <= budget_cover) / len(samples)
            m_gate = sum(1 for s in samples if s <= borders_gated * P) / len(samples)
            log("  масса пушей в накрытом времени: сейчас %.1f%% -> с гейтом %.1f%% "
                "(P(win) ×%.2f при том же кванте каденса)"
                % (m_now * 100, m_gate * 100, (m_gate / m_now) if m_now else float("inf")))
            lat["_summary"]["mass_now"] = m_now
            lat["_summary"]["mass_gated"] = m_gate
            lat["_summary"]["gain_phase_gate"] = (m_gate / m_now) if m_now else None

    log("\n[6c] СИМУЛЯЦИЯ ПОЛИТИК ПО КАЖДОМУ ПУШУ (без модельных допущений о плотности)")
    log("     Правило победы: пуш накрыт, если бюджет ещё не выгорел на момент S_hot И проба")
    log("     стреляет в этот момент. P(win) ~ (проб/сек) × доля накрытых пушей.")
    LAT_P = 30                       # период решётки (см. [6b])
    LAT_TOL = 2                      # ±2с = 3 блока Base -> окно огня 6с у границы
    LAT_WIN = 2 * LAT_TOL + 2        # 6с
    LAT_HORIZON = 1800               # дальше фаза уплывает (замер: доля попаданий падает)
    sim = {}
    for mode_name, r_gate in (("гейт @ текущий квант", 1.0 / POLL_QUANTUM_SEC),
                              ("гейт @ пол отправки", 1.0 / SEND_FLOOR_SEC)):
        tot_now = tot_gate = n_tot = 0.0
        borders = FAT_CAP / (LAT_WIN * r_gate)          # сколько границ накрывает бюджет
        reach_gate = borders * LAT_P                    # секунд огня под гейтом
        for lb in labels:
            for rec in recs_by_feed[lb]:
                if "L_hot" not in rec:
                    continue
                n_tot += 1
                S, gap = rec["S_hot"], rec["gap"]
                tot_now += rate_now_sim if (S <= budget_cover) else 0.0
                if gap <= LAT_HORIZON:
                    resid = min(gap % LAT_P, LAT_P - (gap % LAT_P))
                    on_lattice = resid <= LAT_TOL
                    tot_gate += r_gate if (on_lattice and S <= reach_gate) else 0.0
                else:                                    # вне горизонта решётки — прежний плоский огонь
                    tot_gate += rate_now_sim if (S <= budget_cover) else 0.0
        sim[mode_name] = {"borders": borders, "reach_gate_s": reach_gate,
                          "score_now": tot_now / n_tot, "score_gate": tot_gate / n_tot,
                          "gain": (tot_gate / tot_now) if tot_now else None, "n": int(n_tot)}
        log("  [%s] бюджет накрывает %.0f границ = %.0fс огня (сейчас %.0fс); "
            "P(win) ×%.2f на n=%d пушах"
            % (mode_name, borders, reach_gate, budget_cover, sim[mode_name]["gain"], int(n_tot)))
    # то же по фидам, где решётка доказана (BTC/ETH/ADA) — там и живёт весь эффект
    for lb in labels:
        recs_l = [r for r in recs_by_feed[lb] if "L_hot" in r]
        if len(recs_l) < 30:
            continue
        r_gate = 1.0 / SEND_FLOOR_SEC
        borders = FAT_CAP / (LAT_WIN * r_gate)
        reach = borders * LAT_P
        a = sum(rate_now_sim for r in recs_l if r["S_hot"] <= budget_cover)
        b_ = 0.0
        for r in recs_l:
            if r["gap"] <= LAT_HORIZON:
                res_ = min(r["gap"] % LAT_P, LAT_P - (r["gap"] % LAT_P))
                b_ += r_gate if (res_ <= LAT_TOL and r["S_hot"] <= reach) else 0.0
            else:
                b_ += rate_now_sim if r["S_hot"] <= budget_cover else 0.0
        near = sum(1 for r in recs_l if r["gap"] <= LAT_HORIZON) / len(recs_l)
        log("    %-5s n=%-4d в горизонте решётки %.0f%% пушей -> P(win) ×%.2f"
            % (lb, len(recs_l), near * 100, (b_ / a) if a else float("nan")))
        sim.setdefault("by_feed", {})[lb] = {"n": len(recs_l), "near_share": near,
                                             "gain": (b_ / a) if a else None}
    result["policy_sim"] = sim

    log("\n[7] ВЫИГРЫШ ОТ КОНЦЕНТРАЦИИ (бюджет N=%d проб)" % FAT_CAP)
    log("    часы бюджета — СЕКУНДЫ ОГНЯ S_hot: при кванте петли %.2fс кап %d тратится за %.0f "
        "таких секунд; пол отправки %.2fс дал бы %.0fс на том же бюджете"
        % (POLL_QUANTUM_SEC, FAT_CAP, budget_cover, SEND_FLOOR_SEC, FAT_CAP * SEND_FLOOR_SEC))
    conc = {}
    rate_now = 1.0 / POLL_QUANTUM_SEC        # проб/сек сейчас (~6.7 = 13 проб/блок)
    rate_cap = 1.0 / SEND_FLOOR_SEC          # физический потолок каденса (10 проб/сек)
    wmin_send = FAT_CAP * SEND_FLOOR_SEC     # минимальная ширина концентрации: 600×0.10 = 60с
    cases = [("ALL_budget_S", all_S), ("ALL_hot_wall", all_Lhot), ("ALL_arm_wall", all_Larm)]
    cases += [("%s_budget_S" % lb, v["S_hot"]) for lb, v in per_feed.items() if len(v["S_hot"]) >= 30]
    for name, samples in cases:
        if not samples:
            continue
        H = args.horizon or float(quantiles(samples, (0.95,))["p95"])
        H = max(H, budget_cover * 2)
        g, a, b, mass = best_interval(samples, H, wmin_send)
        sg, sa, sm, m0 = shift_gain(samples, budget_cover, H)
        real, plac = oos_gain(samples, H, wmin_send, reps=120)
        wf = waterfill(samples, H, FAT_CAP, rate_cap, rate_now, budget_cover)
        edges = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600, 100000]
        conc[name] = {"n": len(samples), "horizon_s": H,
                      "insample": {"gain": g, "a": a, "b": b, "mass": mass},
                      "shift": {"gain": sg, "a": sa, "mass_best": sm, "mass_at0": m0,
                                "cover_s": budget_cover},
                      "waterfill": wf, "oos_gain": real, "placebo_gain": plac,
                      "hist_edges": edges, "hist": histogram(samples, edges),
                      "q": quantiles(samples)}
        log("  [%s] n=%d горизонт T=%.0fс" % (name, len(samples), H))
        log("     квантили: %s" % {k: round(v, 1) if isinstance(v, float) else v
                                   for k, v in conc[name]["q"].items()})
        log("     гистограмма %s" % list(zip([("%s-%s" % (edges[i], edges[i + 1]))
                                              for i in range(len(edges) - 1)], conc[name]["hist"])))
        log("     (A) формула задачи, IN-SAMPLE: [%.0f, %.0f]с, масса %.1f%% -> плотность ×%.2f "
            "против равномерного по всему T" % (a, b, mass * 100.0, g))
        log("     (A') OOS (интервал на половине, замер на другой): медиана ×%.2f, p10 ×%.2f | "
            "PLACEBO (равномерный шум) медиана ×%.2f"
            % (real.get("p50", 0.0), real.get("p10", 0.0), plac.get("p50", 0.0)))
        log("     (B) СДВИГ текущего покрытия %.0fс без смены каденса: лучший старт %.0fс, "
            "масса %.1f%% против %.1f%% при старте с 0 -> ×%.2f"
            % (budget_cover, sa, sm * 100.0, m0 * 100.0, sg))
        log("     (C) WATER-FILL при потолке каденса %.1f проб/с (сейчас %.1f): непрерывный "
            "интервал [%.0f, %.0f]с масса %.1f%% -> P(win) ×%.2f; несвязный оптимум ×%.2f"
            % (rate_cap, rate_now, wf["contiguous_a"], wf["contiguous_a"] + wf["opt_width_s"],
               wf["mass_contiguous"] * 100.0, wf["gain_contiguous"], wf["gain_waterfill"]))
        log("     (D) сейчас накрыто массы: %.1f%% (остальное — пуши ПОСЛЕ выгорания капа)"
            % (wf["mass_now"] * 100.0))
    result["concentration"] = conc

    outp = args.out
    os.makedirs(os.path.dirname(outp) or ".", exist_ok=True)
    with open(outp, "w") as f:
        json.dump(result, f, indent=2, default=str)
    log("\n[=] записан %s (RPC-вызовов: %d)" % (outp, rpc.calls))


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    try:
        main()
    except Fail as e:
        log("\nПРОВАЛ: %s" % e)
        sys.exit(2)
