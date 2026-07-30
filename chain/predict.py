"""PREDICT-слой (порт katana bot/predict.py, боевого с 07-17): anchor/return/гистерезис по
Binance-mid против последнего on-chain пуша. За флагом PREDICT_SHADOW (default OFF) — импорт
ничего не открывает, тредов/сети нет.

ПРИНЦИП (SHADOW-фаза): слой НИЧЕГО НЕ СТРЕЛЯЕТ и не трогает fire-путь — только меряет на нашем
живом потоке: lead_s (за сколько секунд ARM опережает транзит), FP-rate, recall. По этим цифрам
принимается решение о pre-arm (пре-подпись флипов на предсказанной цене) и — отдельным решением —
о спекулятивном огне (экономика открыта: tip 0.05 gwei → промах ~$0.02 против приза $600+).

МОДЕЛЬ: фид пушит при |off-chain − last_push| / last_push >= deviation (у Base-фидов 0.1-0.5%)
либо по heartbeat. Держим ANCHOR = цена последнего on-chain пуша, считаем return Binance-mid от
якоря. ARM при |return| >= arm_pct (чуть ниже deviation — покупаем lead), DISARM на ретрейсе ниже
disarm_pct (гистерезис). ARM живёт, пока цена отклонена (пуш может лагать минуты — Katana p90
132-325с), релиз только ретрейсом или HOLD_SEC-капом (falsepos). На подтверждённом пуше anchor →
текущий Binance-mid.

PREDICT-лог (greppable, `PREDICT ` + key=value): event=bootstrap|arm|confirmed|push|disarm|
falsepos|stale|recovered — грамматика 1:1 с Katana (анализаторы переиспользуемы).
FP = (disarm+falsepos)/arm; recall = confirmed/(confirmed+push); lead_s по confirmed."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

# Binance-символ -> {label, dev} (deviation Chainlink-фида на Base; arm=0.9*dev, disarm=0.7*dev).
# ПРОКСИ-адреса фидов резолвятся у чейна (resolve_feed_proxies) — стабильны при ротации агрегаторов.
# ETH/USD задан явно (нога multi-leg; проверен 23.07), cb-фиды резолвятся из money-рынков.
FEEDS_BASE: dict = {
    "BTCUSDT":  {"label": "BTC",  "dev": 0.0010, "collateral": "cbBTC"},
    "ETHUSDT":  {"label": "ETH",  "dev": 0.0015, "proxy": "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70"},
    "DOGEUSDT": {"label": "DOGE", "dev": 0.0050, "collateral": "cbDOGE"},
    "ADAUSDT":  {"label": "ADA",  "dev": 0.0050, "collateral": "cbADA"},
    "LTCUSDT":  {"label": "LTC",  "dev": 0.0050, "collateral": "cbLTC"},
    "XRPUSDT":  {"label": "XRP",  "dev": 0.0050, "collateral": "cbXRP"},
    "SOLUSDT":  {"label": "SOL",  "dev": 0.0050, "collateral": "SOL"},
}
HOLD_SEC = 600.0        # falsepos-кап: релиз таймером только если цена отклонена, а пуша всё нет
MID_STALE_SEC = float(os.environ.get("PREDICT_MID_STALE_SEC", "30.0"))
# бэкофф полла агрегаторов при серии отказов (шторм 30.07): после AFTER подряд — 5с*2^n, кап 30с
POLL_BACKOFF_ON = os.environ.get("PREDICT_POLL_BACKOFF", "1").lower() not in ("0", "false", "off")
POLL_BACKOFF_AFTER = 3
POLL_BACKOFF_BASE_SEC = 5.0
POLL_BACKOFF_CAP_SEC = 30.0
# mid старше -> фид заморожен (обрыв WS не генерит фантомные arm/falsepos). 30с, не 10: bookTicker
# тихих альтов (ADA) легитимно молчит >10с без смены best-bid/ask — 10с давало stale/recovered-чёрн
# (2 эпизода за первые 7 мин shadow). Реальный клин WS ловит транспорт (recv-таймаут 10с -> реконнект).

_LATEST_ROUND_SEL = bytes.fromhex("feaf968c")     # latestRoundData()


def resolve_feed_proxies(rpc, markets) -> dict:
    """{symbol: proxy_lower} — BF1-прокси money-рынков (стабильны при ротации агрегаторов) +
    явные записи из FEEDS_BASE. I/O один раз на старте (лёгкие вызовы)."""
    from chain.feeds import _try_addr
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    by_coll = {}
    for m in markets:
        coll = getattr(m, "collateral_token", None)
        if coll and getattr(m, "loan_token", "") == "USDC" and coll not in by_coll:
            by_coll[coll] = m.market_id
    out = {}
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)
    for sym, cfg_row in FEEDS_BASE.items():
        if "proxy" in cfg_row:
            out[sym] = cfg_row["proxy"].lower()
            continue
        mid = by_coll.get(cfg_row.get("collateral"))
        if not mid:
            continue
        try:
            oracle = morpho.functions.idToMarketParams(rpc.to_bytes32(mid)).call()[2]
        except Exception:
            continue
        proxy = _try_addr(rpc, oracle, "BASE_FEED_1")
        if proxy:
            out[sym] = proxy.lower()
    return out


def make_poll_fn(rpc, proxies: dict):
    """poll_fn() -> {symbol: (updatedAt, price_float)} — ОДИН aggregate3 latestRoundData по всем
    прокси (лёгкий, каждые poll_interval сек на Alchemy). Ошибку транспорта бросает — driver._poll
    её глотает и скипает шаг."""
    from chain.multicall import aggregate3
    syms = sorted(proxies)
    calls = [(proxies[s], _LATEST_ROUND_SEL) for s in syms]

    def _poll():
        out = {}
        for s, (ok, data) in zip(syms, aggregate3(rpc, calls)):
            if ok and data and len(data) >= 160:
                answer = int.from_bytes(data[32:64], "big", signed=True)
                updated = int.from_bytes(data[96:128], "big")
                if answer > 0 and updated > 0:
                    out[s] = (updated, answer / 1e8)
        return out
    return _poll


def format_line(event: dict) -> str:
    def fmt(v):
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            r = round(v, 6)
            return str(int(r)) if r == int(r) else format(r, "f").rstrip("0").rstrip(".")
        return str(v)
    ev = dict(event)
    ev.setdefault("ts", round(time.time(), 3))
    return "PREDICT " + " ".join("%s=%s" % (k, fmt(v)) for k, v in ev.items())


@dataclass
class Feed:
    symbol: str
    label: str
    arm_pct: float
    disarm_pct: float
    anchor: float | None = None
    mid: float | None = None
    armed: bool = False
    suppressed: bool = False          # после falsepos: без ре-арма до ретрейса
    arm_mono: float | None = None
    arm_wall: float | None = None
    arm_return: float | None = None
    peak_return: float = 0.0


class PredictEngine:
    """Anchor/return/гистерезис per feed, ПЕР-ФИДОВЫЕ пороги (deviation Base-фидов различается
    5×: BTC 0.1% vs DOGE 0.5%). Чистая машина состояний: вход push'ится (on_mid/on_push/tick),
    каждый метод возвращает список событий-словарей (не логирует, без I/O). now инжектируем."""

    def __init__(self, feeds_cfg: dict = None, falsepos_window: float = HOLD_SEC,
                 now=time.monotonic):
        self.falsepos_window = falsepos_window
        self._now = now
        self.feeds: dict = {}
        for sym, row in (feeds_cfg or FEEDS_BASE).items():
            dev = float(row["dev"])
            self.feeds[sym.upper()] = Feed(sym.upper(), row["label"],
                                           arm_pct=dev * 0.9, disarm_pct=dev * 0.7)

    def ret(self, symbol: str):
        f = self.feeds.get(symbol.upper())
        if f is None or f.anchor is None or f.mid is None or f.anchor == 0:
            return None
        return (f.mid - f.anchor) / f.anchor

    def armed_symbols(self) -> set:
        return {s for s, f in self.feeds.items() if f.armed}

    def bootstrap(self, symbol: str, anchor: float, source: str = "onchain") -> list:
        f = self.feeds.get(symbol.upper())
        if f is None or f.anchor is not None or anchor is None or anchor <= 0:
            return []
        f.anchor = float(anchor)
        return [{"event": "bootstrap", "feed": f.label, "anchor": f.anchor, "source": source}]

    def on_mid(self, symbol: str, mid: float, wall: float | None = None) -> list:
        f = self.feeds.get(symbol.upper())
        if f is None or mid is None or mid <= 0:
            return []
        f.mid = float(mid)
        if f.anchor is None:                       # ленивый bootstrap, если on-chain чтение запоздало
            f.anchor = f.mid
            return []
        r = self.ret(symbol)
        if r is None:
            return []
        mag = abs(r)
        events = []
        if f.armed and mag > abs(f.peak_return):
            f.peak_return = r
        if not f.armed:
            if f.suppressed and mag < f.disarm_pct:
                f.suppressed = False
            if not f.suppressed and mag >= f.arm_pct:
                f.armed = True
                f.arm_mono = self._now()
                f.arm_wall = wall if wall is not None else time.time()
                f.arm_return = r
                f.peak_return = r
                events.append({"event": "arm", "feed": f.label, "ret_pct": r * 100.0,
                               "ret_bps": int(round(mag * 10000.0)), "anchor": f.anchor,
                               "mid": f.mid, "dir": ("up" if r >= 0 else "down")})
        elif mag < f.disarm_pct:                   # ретрейс ниже гистерезиса без пуша
            events.append(self._end(f, "disarm", r))
        return events

    def on_push(self, symbol: str, wall: float | None = None, price: float | None = None) -> list:
        """Подтверждённый on-chain пуш (updatedAt сдвинулся): confirmed (если armed) либо push
        (recall-мисс), затем ре-якорь на текущий Binance-mid (return ≈ 0)."""
        f = self.feeds.get(symbol.upper())
        if f is None:
            return []
        r = self.ret(symbol)
        events = []
        if f.armed:
            lead = (self._now() - f.arm_mono) if f.arm_mono is not None else None
            events.append({"event": "confirmed", "feed": f.label, "was_armed": True,
                           "lead_s": (None if lead is None else round(lead, 3)),
                           "ret_pct": (None if r is None else r * 100.0),
                           "arm_ret_pct": (None if f.arm_return is None else f.arm_return * 100.0),
                           "anchor": f.anchor, "push_mid": f.mid})
        else:
            events.append({"event": "push", "feed": f.label, "was_armed": False, "lead_s": None,
                           "ret_pct": (None if r is None else r * 100.0),
                           "anchor": f.anchor, "push_mid": f.mid})
        new_anchor = f.mid if f.mid is not None else price
        if new_anchor is not None and new_anchor > 0:
            f.anchor = float(new_anchor)
        f.armed = False
        f.suppressed = False
        f.arm_mono = f.arm_wall = f.arm_return = None
        f.peak_return = 0.0
        return events

    def tick(self, wall: float | None = None, exclude: set | None = None) -> list:
        """Таймерный релиз ПОСЛЕДНЕЙ надежды (falsepos-кап): норма — disarm на ретрейсе, пуш
        может лагать минуты. exclude — фиды с протухшим mid (заморожены)."""
        events = []
        for f in self.feeds.values():
            if exclude and f.symbol in exclude:
                continue
            if f.armed and f.arm_mono is not None and \
                    (self._now() - f.arm_mono) >= self.falsepos_window:
                r = self.ret(f.symbol)
                events.append(self._end(f, "falsepos", r))
                f.suppressed = True
        return events

    def freeze_gap(self, symbol: str, gap_s: float) -> None:
        """Слепой эпизод (stale-mid) кончился: сдвигаем arm-часы на gap — held/lead считают
        только наблюдаемое время (иначе 10-мин обрыв WS мгновенно зреет falsepos-кап)."""
        f = self.feeds.get(symbol.upper())
        if f is not None and f.arm_mono is not None and gap_s > 0:
            f.arm_mono += gap_s

    def _end(self, f: Feed, kind: str, r) -> dict:
        held = (self._now() - f.arm_mono) if f.arm_mono is not None else None
        peak = f.peak_return
        ev = {"event": kind, "feed": f.label,
              "held_s": (None if held is None else round(held, 3)),
              "ret_pct": (None if r is None else r * 100.0),
              "peak_ret_pct": (None if peak == 0 else peak * 100.0)}
        f.armed = False
        f.arm_mono = f.arm_wall = f.arm_return = None
        f.peak_return = 0.0
        return ev


class PredictDriver:
    """Фоновый тред: PriceFeed-mid + poll агрегаторов -> движок -> PREDICT-лог; (LIVE, позже)
    публикует armed-сет через on_arm. Каждый шаг обёрнут — ошибка = «нет предсказания на шаге»,
    не клин. mid_fn(sym)->(mid,ts)|None; poll_fn()->{sym:(updatedAt,price)}; on_arm(set)|None."""

    def __init__(self, engine: PredictEngine, mid_fn, poll_fn, on_arm=None, on_event=None,
                 log=print, interval: float = 0.5, poll_interval: float = 2.0,
                 stale_sec: float = MID_STALE_SEC,
                 now=time.monotonic, sleep=time.sleep, wall=time.time):
        self.engine = engine
        self.mid_fn, self.poll_fn = mid_fn, poll_fn
        self.on_arm = on_arm
        self.on_event = on_event      # шов потребителей событий (spec-канарейка, позже pre-arm)
        self.log = log
        self.interval, self.poll_interval = interval, poll_interval
        self.stale_sec = stale_sec
        self._now, self._sleep, self._wall = now, sleep, wall
        self._stop = threading.Event()
        self._thread = None
        self._last_updated: dict = {}
        self._last_poll = None
        # 30.07: шторм dRPC (429 быстрые, 408 по ~7.4с) — полл без бэкоффа долбил задушенный
        # эндпоинт каждые poll_interval часами, кормя его же троттлер. Серия отказов (>=3) ->
        # экспоненциальная пауза 5с..кап 30с, успех сбрасывает. PREDICT_POLL_BACKOFF=0 = откат.
        self._poll_fail_streak = 0
        self._poll_backoff_until = 0.0
        self._prev_armed: set = set()
        self._stale_since: dict = {}
        self.stats = {"steps": 0, "arms": 0, "confirmed": 0, "falsepos": 0, "poll_fail": 0,
                      "stale_episodes": 0}

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="predict-driver", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:                 # баг драйвера не убивает тред
                self.log("[predict] driver step error: %s" % str(e)[:160])
            self._sleep(self.interval)

    def step(self):
        """Один тик (публичен для офлайн-тестов): классифицировать mid'ы (fresh/stale) -> poll по
        каденсу (пуши stale-фидов отложены) -> скормить свежие mid'ы -> falsepos-таймер (stale
        исключены) -> лог -> publish дельты armed-сета."""
        self.stats["steps"] += 1
        wall = self._wall()
        events = []
        fresh: dict = {}
        stale: set = set()
        for sym in self.engine.feeds:
            v = self.mid_fn(sym)
            if v is None:
                continue
            mid, ts = v if isinstance(v, tuple) else (v, None)
            if ts is not None and (self._now() - ts) > self.stale_sec:
                stale.add(sym)
                if sym not in self._stale_since:
                    self._stale_since[sym] = self._now()
                    self.stats["stale_episodes"] += 1
                    events.append({"event": "stale", "feed": self.engine.feeds[sym].label,
                                   "age_s": round(self._now() - ts, 3)})
                continue
            if sym in self._stale_since:           # эпизод кончился
                gap = self._now() - self._stale_since.pop(sym)
                self.engine.freeze_gap(sym, gap)
                events.append({"event": "recovered", "feed": self.engine.feeds[sym].label,
                               "stale_s": round(gap, 3)})
                events += self.engine.on_mid(sym, mid, wall)   # recovered-mid ДО пуша этого шага
                continue
            fresh[sym] = mid
        if ((self._last_poll is None or (self._now() - self._last_poll) >= self.poll_interval)
                and self._now() >= self._poll_backoff_until):
            self._last_poll = self._now()
            events += self._poll(wall, stale)
        for sym, mid in fresh.items():
            events += self.engine.on_mid(sym, mid, wall)
        events += self.engine.tick(wall, exclude=stale)
        for ev in events:
            self.log(format_line(ev))
            if ev["event"] == "arm":
                self.stats["arms"] += 1
            elif ev["event"] == "confirmed":
                self.stats["confirmed"] += 1
            elif ev["event"] == "falsepos":
                self.stats["falsepos"] += 1
            if self.on_event is not None:
                try:
                    self.on_event(ev)
                except Exception as e:            # потребитель не роняет драйвер
                    self.log("[predict] on_event error: %s" % str(e)[:120])
        self._publish()

    def _poll(self, wall, stale: set | None = None) -> list:
        try:
            reads = self.poll_fn() or {}
            self._poll_fail_streak = 0
            self._poll_backoff_until = 0.0
        except Exception as e:
            self.stats["poll_fail"] += 1
            self._poll_fail_streak += 1
            if POLL_BACKOFF_ON and self._poll_fail_streak >= POLL_BACKOFF_AFTER:
                delay = min(POLL_BACKOFF_BASE_SEC * (2 ** (self._poll_fail_streak - POLL_BACKOFF_AFTER)),
                            POLL_BACKOFF_CAP_SEC)
                self._poll_backoff_until = self._now() + delay
                self.log("[predict] aggregator poll failed #%d — backoff %.0fs: %s"
                         % (self._poll_fail_streak, delay, str(e)[:120]))
            else:
                self.log("[predict] aggregator poll failed (skipped): %s" % str(e)[:120])
            return []
        events = []
        for sym, val in reads.items():
            sym = sym.upper()
            if sym not in self.engine.feeds or not val:
                continue
            if stale and sym in stale:
                # заморожен: пуш ОТЛОЖЕН (updatedAt не съеден) — ре-якорь на замороженный
                # pre-drop mid отравил бы return; обработается первым poll'ом после recovery
                continue
            updated_at, price = val
            f = self.engine.feeds[sym]
            if f.anchor is None:
                events += self.engine.bootstrap(sym, price)
                self._last_updated[sym] = updated_at
            elif sym not in self._last_updated:
                self._last_updated[sym] = updated_at    # первый poll-baseline, НЕ пуш
            elif updated_at != self._last_updated[sym]:
                events += self.engine.on_push(sym, wall, price)
                self._last_updated[sym] = updated_at
        return events

    def _publish(self):
        armed = self.engine.armed_symbols()
        if armed == self._prev_armed:
            return
        self._prev_armed = set(armed)
        if self.on_arm is not None:
            try:
                self.on_arm(armed)
            except Exception as e:
                self.log("[predict] on_arm error: %s" % str(e)[:120])
