"""Binance WS price feed для PREDICT-слоя (chain/predict.py; за флагом PREDICT_SHADOW,
default-OFF — импорт модуля ничего не открывает).

ЗАЧЕМ: Chainlink-фиды Base пушат on-chain при отклонении off-chain цены на deviation-порог
(BTC 0.1% … DOGE/XRP 0.5%) либо по heartbeat. Off-chain цена (прокси = Binance spot) пересекает
порог за десятки секунд ДО транзита (Katana-замер: медиана lead 31-46с) — этого хватает, чтобы
пре-армить флипы и пре-подписать tx. Разбор гонки 23.07 показал: победители стоят на idx+2 от
транзита в очереди секвенсера — реакцией такое не берётся, только предсказанием. Этот модуль —
глаза; chain/predict.py — anchor/return/гистерезис-мозг. САМ НИЧЕГО НЕ СТРЕЛЯЕТ.

Порт katana bot/pricefeed.py (боевой с 07-17): транспорт заменён с самописного WsConn на
websockets.sync (уже в зависимостях — hot_loop живёт на websockets). Тредовый контракт тот же:
ОДИН фоновый daemon-тред владеет своим сокетом; главный цикл никогда не блокируется — читает
lock-guarded снапшот mid. Обрыв → unhealthy → реконнект с капнутым backoff. Транспорт
инжектируем (тесты без сети)."""
from __future__ import annotations

import json
import os
import threading
import time

WS_URL = os.environ.get("PREDICT_WS_URL", "wss://data-stream.binance.vision/ws")
# data-stream.binance.vision = публичный market-data домен Binance БЕЗ гео-блока: основной
# stream.binance.com отдаёт HTTP 451 с US-IP (Charlotte, живой отказ 23.07 19:01). Протокол
# и стримы идентичны. Фолбэк при недоступности vision: Coinbase/OKX (другой формат — адаптер).
# bookTicker идёт много раз в секунду; такая тишина = поток заклинил = реконнект.
STALE_SEC = float(os.environ.get("PREDICT_WS_STALE_SEC", "10.0"))
BACKOFF_MAX_SEC = float(os.environ.get("PREDICT_WS_BACKOFF_MAX", "30.0"))

# 29.07 ротация edge-IP: у vision 8 A-записей, ДВА edge гнилые (пер-IP замер: залипание 12с и
# keepalive 1011 — ровно сигнатуры дневного шторма, 10 обрывов/30мин), DNS-рулетка ловила их
# ~25% реконнектов. Коннектимся к выбранному IP своим TCP-сокетом (URI сохраняет hostname =>
# SNI и Host корректны), упавший IP — в карантин. Пустой резолв = прежний коннект по hostname.
EDGE_QUARANTINE_SEC = float(os.environ.get("PREDICT_WS_EDGE_QUARANTINE_SEC", "600"))
_edge_bad: dict = {}          # ip -> monotonic последнего фейла
_edge_last = {"ip": None}     # ip текущего/последнего коннекта (для маркировки из _run)


def _resolve_edges(host: str):
    import socket
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET)})
    except Exception:
        return []


def _pick_edge(host: str, now=time.monotonic):
    """Случайный edge вне карантина; все в карантине = карантин обнуляется (хуже не станет)."""
    import random
    ips = _resolve_edges(host)
    if not ips:
        return None
    ok = [ip for ip in ips if now() - _edge_bad.get(ip, -1e9) >= EDGE_QUARANTINE_SEC]
    if not ok:
        _edge_bad.clear()
        ok = ips
    return random.choice(ok)


def _mark_last_edge_bad(now=time.monotonic):
    """Зовётся из PriceFeed._run на обрыве: текущий edge — в карантин. No-op без ротации
    (тестовые/кастомные connect не трогают _edge_last)."""
    ip = _edge_last.get("ip")
    if ip:
        _edge_bad[ip] = now()


def _default_connect(url: str = WS_URL):
    import socket
    from urllib.parse import urlparse
    from websockets.sync.client import connect
    host = urlparse(url).hostname
    ip = _pick_edge(host) if host else None
    _edge_last["ip"] = ip
    if ip is None:
        return connect(url, open_timeout=15, close_timeout=3)
    raw = socket.create_connection((ip, 443), timeout=15)
    return connect(url, sock=raw, open_timeout=15, close_timeout=3)


class PriceFeed:
    """Фоновый Binance WS-ридер. Конструктор берёт символы (upper, напр. "BTCUSDT"), start();
    из главного цикла — mid(symbol) -> (mid, ts_mono) и healthy(). on_tick(sym, mid, wall) —
    если задан — зовётся В ЭТОМ треде на каждом апдейте (быстрый + thread-safe).

    Подписка `<symbol lower>@bookTicker`, mid = (bid+ask)/2 — best-bid/ask без трейд-лага."""

    def __init__(self, symbols, on_tick=None, connect=None, ws_url: str = WS_URL,
                 now=time.monotonic, sleep=time.sleep, log=None, wall=time.time,
                 stale_sec: float = STALE_SEC, backoff_max: float = BACKOFF_MAX_SEC):
        self.symbols = tuple(s.upper() for s in symbols)
        self.on_tick = on_tick
        self._connect = connect or (lambda: _default_connect(ws_url))
        self._now, self._sleep, self._wall = now, sleep, wall
        self._log = log or (lambda *_: None)
        self.stale_sec, self.backoff_max = stale_sec, backoff_max
        self._lock = threading.Lock()
        self._mid: dict = {}                       # sym -> (mid, ts по self._now)
        self._connected = False
        self._last_msg = 0.0
        self._stop = threading.Event()
        self._thread = None
        self.stats = {"reconnects": 0, "ticks": 0, "parse_fail": 0}

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="predict-pricefeed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            self._connected = False

    def mid(self, symbol: str):
        """(mid, ts) — ts по фидовым часам (monotonic): потребитель обязан age-гейтить, иначе
        при обрыве WS (backoff до 30с) замороженный mid молча кормил бы движок предсказаний
        протухшей ценой (грязнит shadow-метрики, по которым принимается go-live). None = нет данных."""
        with self._lock:
            return self._mid.get(symbol.upper())

    def healthy(self) -> bool:
        with self._lock:
            return self._connected and (self._now() - self._last_msg) < self.stale_sec

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._connect()
                self._subscribe(conn)
                with self._lock:
                    self._connected = True
                    self._last_msg = self._now()
                self._log("[predict] Binance WS connected+subscribed (%s bookTicker)"
                          % ",".join(self.symbols))
                backoff = 0.5
                self._serve(conn)
            except Exception as e:                 # обрыв/парс — деградация, не смерть
                _mark_last_edge_bad()              # 29.07: гнилой edge — в карантин до ротации
                self._log("[predict] Binance WS dropped (%s: %s, edge=%s) — reconnect in %.1fs"
                          % (type(e).__name__, str(e)[:100], _edge_last.get("ip") or "dns", backoff))
            finally:
                with self._lock:
                    self._connected = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            self.stats["reconnects"] += 1
            self._sleep(backoff)
            backoff = min(self.backoff_max, backoff * 2)

    def _subscribe(self, conn):
        # in-band SUBSCRIBE — все символы на одном коннекте; ответ {"result":null,"id":1}
        params = ["%s@bookTicker" % s.lower() for s in self.symbols]
        conn.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": 1}))

    def _serve(self, conn):
        while not self._stop.is_set():
            msg = conn.recv(timeout=self.stale_sec)   # тишина > stale = заклинило -> TimeoutError -> реконнект
            with self._lock:
                self._last_msg = self._now()
            try:
                j = json.loads(msg)
            except ValueError:
                self.stats["parse_fail"] += 1
                continue
            self._on_message(j)

    def _on_message(self, j):
        if not isinstance(j, dict):
            return
        if "result" in j and "id" in j:            # SUBSCRIBE ack
            return
        data = j.get("data", j)
        sym = (data.get("s") or "").upper()
        bid, ask = data.get("b"), data.get("a")
        if not sym or bid is None or ask is None:
            return
        try:
            mid = (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            self.stats["parse_fail"] += 1
            return
        if mid <= 0:
            return
        with self._lock:
            self._mid[sym] = (mid, self._now())
        self.stats["ticks"] += 1
        if self.on_tick is not None:
            try:
                self.on_tick(sym, mid, self._wall())
            except Exception as e:
                self._log("[predict] on_tick error: %s" % str(e)[:100])
