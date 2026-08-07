"""Движок огня: постоянный пул тредов, держащий каденс проб по пред-подписанным лестницам (#32).

ПОЧЕМУ ДВИЖОК, А НЕ ГЕЙТ «ЗАЛП В ПОЛЁТЕ». Нынешняя схема порождает залп на КАЖДЫЙ тик окна и
режет ноги на пачки тредов. Наблюдаемая плотность 13 проб/блок рождается именно из ПЕРЕКРЫТИЯ
залпов: предыдущий ещё летит, когда стартует следующий. Поэтому «не начинать залп, пока
предыдущий в полёте» плотность не поднял бы, а срезал — гейт лечит симптом (взрыв тредов),
убивая механизм. Правильная форма — заменить порождение на ПОСТОЯННЫЙ пул: N тредов живут всё
время, а тик окна лишь продлевает им право стрелять (keepalive).

ЧТО ЭТО ДАЁТ ПО ЗАМЕРУ (07.08). Рассылка пред-подписанной пробы в тёплую сессию секвенсера
держит тред 32.5мс (p50) против 60мс у синхронной ноги Alchemy. 16 тредов x 1/0.0325 =
~493 отправки/с = ~986 проб в блок Base против нынешних 267 (насыщение окна победы — 1480).

ПОТОЛОК ЗАДАЁТ НЕ РАССЫЛКА, А ПОДПИСЬ — И ЭТО ДВА РАЗНЫХ ПОТОЛКА:
  ВСПЛЕСК ограничен только рассылкой: лестницы — это СКЛАД, набитый в спокойное время
    (106 ног x 40 проб = 4240 готовых байтов). Пока склад не пуст, подписи в контуре нет вовсе;
  ПОДДЕРЖАНИЕ ограничено CPU подписи: 4мс на пробу, то есть 200 проб/с = 0.8 ядра ПОСТОЯННО,
    сколько лестницу ни углубляй. Pre-sign не уменьшает работу подписи — он уносит её с
    горячего пути (латентность отправки 64мс -> 32.5мс), но суммарный CPU остаётся.
Отсюда конструкция: склад набивается ФОНОМ (пополнение — отдельный тред, уступающий огню), а
окно победы стреляет со склада. Если склад кончился — движок просто идёт медленнее, а не встаёт.

ГАРДЫ (у нас есть боевые инциденты ровно на эти грабли):
  * keepalive с коротким TTL: перестал тикать источник (окно закрыто, петля встала, процесс
    занят) -> огонь гаснет сам за TTL. Отказ в сторону МОЛЧАНИЯ, а не бесконечной стрельбы;
  * потолок темпа (token bucket): безлимитный цикл рассылки — это ровно тот самопальный
    RPC-шторм, который мы себе уже устраивали (109x429);
  * КОЛЛЕКТОР КВИТАНЦИЙ — ОДИН ТРЕД на весь движок. Тред-на-квитанцию при сотнях отправок в
    секунду это тот же взрыв тредов, который здесь и чинится, только с другого конца.

ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ: SPEC_FIRE_ENGINE=0 -> ни одного треда не создаётся, путь залпа
работает бит-в-бит как прежде.
"""
import os
import threading
import time

ENGINE_ON = os.environ.get("SPEC_FIRE_ENGINE", "0").lower() not in ("0", "false", "no")
ENGINE_THREADS = int(os.environ.get("SPEC_FIRE_ENGINE_THREADS", "8"))
# Потолок темпа. Начинаем ЗАМЕТНО ниже модельных 493/с: обкатка идёт на 5 стволах, и цена
# ошибки в темпе — шторм 429 по боевому ключу, а не недобор плотности.
ENGINE_MAX_RATE = float(os.environ.get("SPEC_FIRE_ENGINE_MAX_RATE", "200"))
ENGINE_BURST = float(os.environ.get("SPEC_FIRE_ENGINE_BURST", "40"))
# TTL keepalive: ~блок Base (2с) с запасом. Больше — огонь переживает закрытие окна;
# меньше — обычная задержка тика гасит огонь в середине горячей фазы.
ENGINE_TTL_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_TTL_SEC", "1.5"))
ENGINE_IDLE_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_IDLE_SEC", "0.05"))
# Пополнение склада: ниже какой глубины лестницу дотачивать и как часто смотреть.
ENGINE_REFILL_AT = int(os.environ.get("SPEC_FIRE_ENGINE_REFILL_AT", "12"))
ENGINE_REFILL_POLL_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_REFILL_POLL_SEC", "0.5"))
COLLECT_POLL_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_POLL_SEC", "1.0"))
COLLECT_TIMEOUT_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_TIMEOUT_SEC", "180"))
COLLECT_BATCH = int(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_BATCH", "200"))


class ReceiptCollector:
    """ОДИН тред на все квитанции движка: партиями спрашивает исходы и раздаёт их обработчику.

    Почему не тред-на-квитанцию (как в прежнем залпе): при 200 отправках/с это 200 новых тредов
    в секунду на четырёхъядерной машине — тот самый эффект наблюдателя, на который у нас есть
    задокументированные инциденты. Партия из одного запроса стоит столько же, сколько один
    полл, а покрывает сотни проб.

    Швы: receipts_fn(hashes) -> {hash: receipt|None}; on_result(meta, receipt); on_timeout(meta).
    """

    def __init__(self, receipts_fn, on_result, on_timeout=None, log=None,
                 poll_sec=None, timeout_sec=None, batch=None, now=None, sleep=None):
        self._receipts_fn = receipts_fn
        self._on_result = on_result
        self._on_timeout = on_timeout
        self._log = log
        self._poll = COLLECT_POLL_SEC if poll_sec is None else float(poll_sec)
        self._timeout = COLLECT_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
        self._batch = COLLECT_BATCH if batch is None else int(batch)
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._pending = {}          # hash -> (meta, deadline)
        self._stop = threading.Event()
        self._thr = None
        self.stats = {"tracked": 0, "resolved": 0, "timeouts": 0, "polls": 0}

    def track(self, h, meta):
        with self._lock:
            if h in self._pending:
                return
            self._pending[h] = (meta, self._now() + self._timeout)
            self.stats["tracked"] += 1

    def pending(self):
        with self._lock:
            return len(self._pending)

    def start(self):
        if self._thr is not None:
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._loop, daemon=True, name="fire-collect")
        self._thr.start()

    def stop(self):
        self._stop.set()

    def tick(self):
        """Один проход. Вынесен из петли, чтобы тест мог прогнать его без тредов и без времени."""
        with self._lock:
            if not self._pending:
                return 0
            hashes = list(self._pending.keys())[:self._batch]
        try:
            got = self._receipts_fn(hashes) or {}
        except Exception as e:
            if self._log:
                self._log("FIRE collect batch fail: %s" % type(e).__name__)
            return 0
        self.stats["polls"] += 1
        done = 0
        now = self._now()
        for h in hashes:
            r = got.get(h)
            if r is None:
                with self._lock:
                    ent = self._pending.get(h)
                # Квитанции нет. Это НЕ приговор: на Base блок 2с, а дроп мы объявляем только
                # по истечении таймаута — иначе инвалидировали бы nonce живой ноги на ровном месте.
                if ent is not None and now >= ent[1]:
                    with self._lock:
                        self._pending.pop(h, None)
                    self.stats["timeouts"] += 1
                    if self._on_timeout:
                        try:
                            self._on_timeout(ent[0], h)
                        except Exception:
                            pass
                continue
            with self._lock:
                ent = self._pending.pop(h, None)
            if ent is None:
                continue
            self.stats["resolved"] += 1
            done += 1
            try:
                self._on_result(ent[0], h, r)
            except Exception as e:
                if self._log:
                    self._log("FIRE collect handler fail: %s" % type(e).__name__)
        return done

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._sleep(self._poll)


class FireEngine:
    """Постоянный пул тредов огня. Тик окна лишь ПРОДЛЕВАЕТ право стрелять (keepalive)."""

    def __init__(self, legs_fn, send_fn, collector=None, refill_fn=None, depth_fn=None,
                 log=None, threads=None, rate=None, ttl=None, now=None, sleep=None):
        """legs_fn() -> [addr,...] (ноги со склада); send_fn(addr) -> hash|None (рассылка пробы);
        refill_fn(addr) -> None (фоновое пополнение склада); depth_fn(addr) -> int (остаток)."""
        self._legs_fn = legs_fn
        self._send_fn = send_fn
        self._refill_fn = refill_fn
        self._depth_fn = depth_fn
        self._collector = collector
        self._log = log
        self._nthreads = ENGINE_THREADS if threads is None else int(threads)
        self._rate = ENGINE_MAX_RATE if rate is None else float(rate)
        self._ttl = ENGINE_TTL_SEC if ttl is None else float(ttl)
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._until = 0.0
        self._target = None        # (feed, mid, borrower, debt_usd) — чей план сейчас на доске
        self._rr = 0
        self._tokens = ENGINE_BURST
        self._tok_ts = self._now()
        self._stop = threading.Event()
        self._threads = []
        self.stats = {"sent": 0, "skipped_rate": 0, "skipped_dry": 0, "armed_sec": 0.0}

    # ---- управление ----
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for i in range(max(1, self._nthreads)):
            t = threading.Thread(target=self._worker, daemon=True, name="fire-%d" % (i + 1))
            t.start()
            self._threads.append(t)
        if self._refill_fn is not None:
            t = threading.Thread(target=self._refill_loop, daemon=True, name="fire-refill")
            t.start()
            self._threads.append(t)
        if self._collector is not None:
            self._collector.start()
        if self._log:
            self._log("FIRE движок поднят: %d тредов, потолок %.0f отправок/с, TTL %.1fс" % (
                self._nthreads, self._rate, self._ttl))

    def stop(self):
        self._stop.set()
        with self._lock:
            self._until = 0.0
        if self._collector is not None:
            self._collector.stop()

    def keepalive(self, target=None, ttl=None):
        """Тик окна: продлить право стрелять. Перестали звать — огонь гаснет сам за TTL."""
        with self._lock:
            self._until = self._now() + (self._ttl if ttl is None else float(ttl))
            if target is not None:
                self._target = target

    def disarm(self):
        with self._lock:
            self._until = 0.0
            self._target = None

    def armed(self) -> bool:
        with self._lock:
            return self._now() < self._until

    # ---- внутреннее ----
    def _take_token(self) -> bool:
        """Token bucket: общий на все треды потолок отправок/с."""
        with self._lock:
            n = self._now()
            self._tokens = min(ENGINE_BURST, self._tokens + (n - self._tok_ts) * self._rate)
            self._tok_ts = n
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    def _next_leg(self):
        legs = self._legs_fn() or []
        if not legs:
            return None
        with self._lock:
            i = self._rr % len(legs)
            self._rr += 1
        return legs[i]

    def _worker(self):
        while not self._stop.is_set():
            if not self.armed():
                self._sleep(ENGINE_IDLE_SEC)
                continue
            if not self._take_token():
                self.stats["skipped_rate"] += 1
                self._sleep(ENGINE_IDLE_SEC)
                continue
            addr = self._next_leg()
            if addr is None:
                self.stats["skipped_dry"] += 1
                self._sleep(ENGINE_IDLE_SEC)
                continue
            with self._lock:
                tgt = self._target
            try:
                h = self._send_fn(addr)
            except Exception as e:
                if self._log:
                    self._log("FIRE отправка ноги %s: %s" % (addr[:10], type(e).__name__))
                self._sleep(ENGINE_IDLE_SEC)
                continue
            if h is None:
                # склад пуст либо гейт закрыт — не крутим пустой цикл на полной скорости
                self.stats["skipped_dry"] += 1
                self._sleep(ENGINE_IDLE_SEC)
                continue
            self.stats["sent"] += 1
            if self._collector is not None:
                self._collector.track(h, (addr, tgt))

    def _refill_loop(self):
        """Склад набивается ФОНОМ и УСТУПАЕТ огню: подпись это CPU (4мс/проба), и в горячей
        фазе она нужна не нам. Пополняем только те ноги, у которых остаток просел."""
        while not self._stop.is_set():
            try:
                for addr in (self._legs_fn() or []):
                    if self._stop.is_set():
                        break
                    d = self._depth_fn(addr) if self._depth_fn else 0
                    if d < ENGINE_REFILL_AT:
                        self._refill_fn(addr)
            except Exception as e:
                if self._log:
                    self._log("FIRE пополнение склада: %s" % type(e).__name__)
            self._sleep(ENGINE_REFILL_POLL_SEC)
