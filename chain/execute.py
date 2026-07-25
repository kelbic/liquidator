"""Execute layer (Phase 2). Builds the on-chain liquidation bundle and gates it with a live
eth_call simulation before any send (no send happens in this module). Aggregator = KyberSwap
(keyless) for the collateral->loan swap; the contract validates the OUTCOME (minProfit gate),
so the route itself is untrusted-but-checked.

stdlib HTTP (urllib) like morpho.py — runs on `python3`; only the eth_call path needs web3 (venv).
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
import urllib.request
from urllib.parse import urlencode

log = logging.getLogger(__name__)

_SEND_LOCK = threading.Lock()   # serialize dispatch nonce-fetch+send across the block loop and hot path

# P0-4: локальный nonce-трекер — снимает get_transaction_count (~150-300мс Alchemy) из send-пути.
# Инварианты: читается/пишется ТОЛЬКО под _SEND_LOCK; после успешного батча next=base+len(sent);
# любая send-ошибка -> инвалидация (следующий dispatch пере-читает RPC — самолечение от nonce-дрейфа);
# фоновый reconcile после батча подтягивает max(rpc, local) (внешняя tx с кошелька поднимет base,
# dropped-tx лечится веткой инвалидации, вниз reconcile НЕ опускает — иначе гонка с только что
# отправленным батчем, который RPC ещё не видит).
_LOCAL_NONCE: dict = {}         # bot address -> next unused nonce

# 07-23: холодный nonce-fetch шёл ТОЛЬКО через RPC_URL, без ретраев — burst-429 основной ноды в
# момент залпа терял ВЕСЬ выстрел (fetch до подписи, SEND-3 ещё не в игре). Фолбэк не гоночный
# путь: срабатывает только когда основная нода уже бросила; тёплую сессию не держим.
_NONCE_FALLBACK_RPC = os.environ.get("NONCE_FALLBACK_RPC", "https://mainnet.base.org")


def _pending_nonce(w3, addr: str) -> int:
    try:
        return int(w3.eth.get_transaction_count(addr, "pending"))
    except Exception:
        from web3 import Web3   # локальный импорт: модуль обязан грузиться без web3 (см. док-стринг)
        w3f = Web3(Web3.HTTPProvider(_NONCE_FALLBACK_RPC, request_kwargs={"timeout": 5}))
        n = int(w3f.eth.get_transaction_count(addr, "pending"))
        log.warning("nonce via fallback %s (основная нода бросила)", _NONCE_FALLBACK_RPC)
        return n


def prewarm_local_nonce(rpc, wallet_key: str) -> bool:
    """Однократный best-effort прогрев _LOCAL_NONCE на старте (наших in-flight tx на старте нет):
    убирает обязательный RPC-hop из ПЕРВОГО залпа после рестарта. Ошибку глотает — боевой путь
    имеет собственный фолбэк (_pending_nonce)."""
    try:
        w3 = rpc._web3()
        bot = account_for(w3, wallet_key).address
        with _SEND_LOCK:
            if _LOCAL_NONCE.get(bot) is None:
                _LOCAL_NONCE[bot] = _pending_nonce(w3, bot)
            base = _LOCAL_NONCE[bot]
        log.info("nonce prewarm: base=%d", base)
        return True
    except Exception as e:
        log.warning("nonce prewarm failed (%s) — первый залп возьмёт фолбэк сам", type(e).__name__)
        return False

# Поправка 07-17 (CANARY_0717_CORRECTION п.2): from_key = secp256k1-деривация ~1-3мс на КАЖДЫЙ send
# (send_tx, dispatch, canary, prepare_hot) — чистый CPU в горячем пути. LocalAccount иммутабелен ->
# кэш на всю жизнь процесса. Ключ НЕ логировать (dict живёт только в памяти).
_ACCT_CACHE: dict = {}          # wallet_key -> LocalAccount


def account_for(w3, key: str):
    a = _ACCT_CACHE.get(key)
    if a is None:
        a = w3.eth.account.from_key(key)
        _ACCT_CACHE[key] = a
    return a


# Поправка 07-17: _post_raw_tx через urllib открывал ХОЛОДНОЕ TCP+TLS на КАЖДЫЙ raw-броадкаст в
# секвенсер (~2-3 RTT ≈ 60-90мс из Charlotte) — ровно на пути гонки за включение («sequencer
# warm=30ms» мерился keep-alive'ом). requests.Session (urllib3-пул, web3 его уже тянет) держит
# соединение открытым; фоновый пинг eth_chainId раз в 15с не даёт idle-таймауту его закрыть.
try:
    import requests as _requests
except Exception:               # requests нет -> фолбэк на старый urllib-путь (холодный, но рабочий)
    _requests = None

_SEQ_SESSIONS: dict = {}        # url -> requests.Session (тёплый keep-alive)
_SEQ_LOCK = threading.Lock()


def _seq_keepalive(url: str, s, interval: float = 15.0):
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    while True:
        try:
            s.post(url, json=body, timeout=5)
        except Exception:
            pass                # разрыв лечится пулом на следующем post (одна холодная отправка)
        time.sleep(interval)


def _seq_session(url: str):
    if _requests is None:
        return None
    with _SEQ_LOCK:
        s = _SEQ_SESSIONS.get(url)
        if s is None:
            s = _requests.Session()
            _SEQ_SESSIONS[url] = s
            threading.Thread(target=_seq_keepalive, args=(url, s), daemon=True).start()
        return s


def warm_broadcast(urls: list):
    """Прогреть keep-alive сессии broadcast-endpoints на старте (до первого выстрела/канарейки)."""
    for u in urls or []:
        _seq_session(u)

# Дедуп двойной отправки (страховка под FAST_LANE): один и тот же (mid, borrower) может прийти в
# dispatch из нескольких параллельных _process_transmit (feed×feed / feed×bare / feed×poll на одном
# transmit). Первый успешный ликвидирует; повторные Morpho ревертит (HEALTHY_POSITION/нет долга) =
# лишний газ, записанный как loss. С sim-гейтом это было редко; FAST_LANE шлёт без sim -> каждый
# реальный флип давал бы 2-3 слепые tx. Здесь: (mid,borrower), отправленный за последние TTL сек,
# второй раз НЕ уходит. Пишется/читается ТОЛЬКО под _SEND_LOCK (как _LOCAL_NONCE). TTL с запасом на
# несколько блоков — реальная ПОВТОРНАЯ ликвидация того же заёмщика в это окно неправдоподобна
# (Morpho всё равно ревертнёт), а следующий блок после TTL повтор разрешит.
_RECENT_SENDS: dict = {}        # (mid, borrower) -> monotonic ts последней отправки
_RECENT_SEND_TTL = 12.0

# P0: живой inflight для kill-switch (был хардкод GuardState(inflight=0) -> гейт мёртв).
# +1 на каждый принятый send, -1 при получении receipt/timeout (или сразу при wait_receipts=False).
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = 0


def current_inflight() -> int:
    with _INFLIGHT_LOCK:
        return _INFLIGHT


def _inflight_add(n: int):
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT = max(0, _INFLIGHT + n)


# II.6 circuit breaker: N ПОДРЯД ревертов fast-lane -> отключить ТОЛЬКО fast-lane (фолбэк в sim,
# бот жив). Без него реверт-шторм fast-lane (напр. от stale-flip) выбивает дневной loss-кап и
# останавливает ВЕСЬ бот ровно в каскад (богатейшее окно). Считаем ПОДРЯД: успешный fast-lane send
# сбрасывает серию -> срабатывает только устойчивый шторм, не разовый lost-race. disabled держится
# ДО рестарта (шторм = разбираться, не авто-переармить). Состояние под _INFLIGHT_LOCK.
_FL_CONSEC_REVERTS = 0
_FL_DISABLED = False
_FL_TRIP_MSG = None        # one-shot TG-сообщение в момент срабатывания (забирает caller с alerter'ом)


def fast_lane_disabled() -> bool:
    with _INFLIGHT_LOCK:
        return _FL_DISABLED


def pop_fast_lane_trip() -> str | None:
    """One-shot: сообщение о срабатывании breaker'а для TG, затем очистка. Зовёт caller с alerter'ом."""
    global _FL_TRIP_MSG
    with _INFLIGHT_LOCK:
        m = _FL_TRIP_MSG
        _FL_TRIP_MSG = None
        return m


def _fl_record(is_fast: bool, status, threshold: int, log=None):
    """Учёт исхода ЗАМАЙНЕННОЙ fast-lane tx для breaker'а. status==1 -> сброс серии; status!=1 (реверт)
    -> инкремент; при достижении порога -> disable + one-shot TG-msg. status is None (timeout, исход
    неизвестен) НЕ считаем. Idempotent после disable (флаг уже стоит)."""
    global _FL_CONSEC_REVERTS, _FL_DISABLED, _FL_TRIP_MSG
    if not is_fast or status is None:
        return
    with _INFLIGHT_LOCK:
        if status == 1:
            _FL_CONSEC_REVERTS = 0
            return
        _FL_CONSEC_REVERTS += 1
        if _FL_CONSEC_REVERTS >= threshold and not _FL_DISABLED:
            _FL_DISABLED = True
            _FL_TRIP_MSG = ("\U0001F6D1 CIRCUIT BREAKER: fast-lane ОТКЛЮЧЁН после %d подряд ревертов "
                            "-> фолбэк в sim-путь (бот жив). Разобрать причину, вернуть рестартом."
                            % _FL_CONSEC_REVERTS)
            if log:
                log.error("II.6 breaker: fast-lane DISABLED after %d consecutive reverts -> sim fallback",
                          _FL_CONSEC_REVERTS)


# Мультибатч-предохранитель: батч с nOk=0 (ВСЕ items провалились) — tx УСПЕШНА (status==1), потому
# невидим realized-loss-гейту как реверт. Отдельный счётчик серии пустых батчей -> one-shot TG-пейдж
# (как fast-lane trip). Состояние под _INFLIGHT_LOCK.
_BATCH_EMPTY_STREAK = 0
_BATCH_TRIP_MSG = None
_BATCH_EMPTY_THRESHOLD = 3


def pop_batch_empty_trip() -> str | None:
    """One-shot: сообщение о серии пустых батчей для TG. Зовёт caller с alerter'ом."""
    global _BATCH_TRIP_MSG
    with _INFLIGHT_LOCK:
        m = _BATCH_TRIP_MSG
        _BATCH_TRIP_MSG = None
        return m


def _batch_record(n_win: int, n_items: int, tx_hash, log=None):
    """Серия пустых батчей: n_win>0 -> сброс; n_win==0 -> инкремент; при пороге -> one-shot TG."""
    global _BATCH_EMPTY_STREAK, _BATCH_TRIP_MSG
    with _INFLIGHT_LOCK:
        if n_win > 0:
            _BATCH_EMPTY_STREAK = 0
            return
        _BATCH_EMPTY_STREAK += 1
        if log:
            log.warning("batch ПУСТОЙ (nOk=0, tx успешна) n=%d streak=%d tx=%s", n_items, _BATCH_EMPTY_STREAK, tx_hash)
        if _BATCH_EMPTY_STREAK >= _BATCH_EMPTY_THRESHOLD and _BATCH_TRIP_MSG is None:
            _BATCH_TRIP_MSG = ("\U0001F6A8 %d подряд ПУСТЫХ батчей (nOk=0, tx успешны, 0 взято) — "
                               "проверить маршрут/minProfit/газ: батч льёт газ без добычи." % _BATCH_EMPTY_STREAK)


# ⭐ CANARY (замер inclusion-гонки, Вариант B: in-process, ОБЩИЙ кошелёк/nonce). Состояние под
# _INFLIGHT_LOCK. Газ канарейки — в СВОЙ бакет (_CANARY_GAS_USD), НЕ в kill-switch realized_today.
_CANARY_PROBES = 0
_CANARY_GAS_USD = 0.0
_CANARY_LAST_TS = 0.0
_SPEC_PROBES = 0          # spec-канарейка (predict-окна): свой счётчик/интервал, ОБЩИЙ газ-кап
_SPEC_LAST_TS = 0.0


def canary_stats() -> tuple:
    with _INFLIGHT_LOCK:
        return (_CANARY_PROBES, _CANARY_GAS_USD)


def canary_record_gas(gas_usd: float):
    global _CANARY_GAS_USD
    with _INFLIGHT_LOCK:
        _CANARY_GAS_USD += max(0.0, gas_usd)


def send_canary(rpc, cfg, log=None, max_fee=None, tip=None, broadcast_raw_urls=None,
                spec_interval_sec=None, spec_max_probes=None):
    """⭐ Минимальная 0-value self-transfer от БОЕВОГО кошелька под ОБЩИМ _SEND_LOCK/_LOCAL_NONCE
    (единый nonce -> ноль nonce-краха; single-slot). Гейты (кап проб / газ-кап / интервал) проверяются
    АТОМАРНО под локом ПЕРЕД раздачей nonce. Возвращает hash или None (гейт закрыт/ошибка). Позиция
    tx в блоке = прокси позиции ликвидации при равном tip -> замер гонки. НЕ считается в kill-switch.
    Поправка 07-17: канарейка должна лететь БОЕВЫМ send-путём — max_fee/tip из fb-хедера (иначе
    send_tx жжёт 2 Alchemy-RTT на fee) и broadcast_raw_urls=[секвенсер] (SEND-3; иначе только
    Alchemy-прокси-хоп, которого у реальной ликвидации нет) — пробы 1-100 мерили ИМЕННО этот оверхед."""
    global _CANARY_PROBES, _CANARY_LAST_TS, _SPEC_PROBES, _SPEC_LAST_TS
    _now = time.monotonic()
    _spec = spec_interval_sec is not None
    with _SEND_LOCK:
        with _INFLIGHT_LOCK:
            if _spec:
                # spec-режим (predict-окна): свой интервал/кап проб — НЕ двигает гейты реактивной
                # кампании; газ-кап ОБЩИЙ (один бюджет на все пробы, включая spec-спам).
                if (_SPEC_PROBES >= (spec_max_probes if spec_max_probes is not None else 500)
                        or _CANARY_GAS_USD >= getattr(cfg, "canary_max_gas_usd", 25.0)
                        or _now - _SPEC_LAST_TS < float(spec_interval_sec)):
                    return None
            elif (_CANARY_PROBES >= getattr(cfg, "canary_max_probes", 100)
                    or _CANARY_GAS_USD >= getattr(cfg, "canary_max_gas_usd", 25.0)
                    or _now - _CANARY_LAST_TS < getattr(cfg, "canary_min_interval_sec", 120.0)):
                return None
        _bot_r = account_for(rpc._web3(), cfg.wallet_key).address   # патронный резерв (см. выше)
        if not probe_allowed_by_reserve(cached_balance_wei(rpc, _bot_r),
                                        21000 * int(cfg.tip_gwei * 1e9) * 3):
            return None
        w3 = rpc._web3()
        bot = account_for(w3, cfg.wallet_key).address
        base_nonce = _LOCAL_NONCE.get(bot)
        if base_nonce is None:
            base_nonce = _pending_nonce(w3, bot)
        tip_wei = int(cfg.tip_gwei * 1e9)
        _tx = {"to": bot, "value": 0, "nonce": base_nonce, "gas": 21000}
        if max_fee is not None:                  # боевой fee-путь (fb-хедер) -> send_tx без fee-RTT
            _tx["maxFeePerGas"] = int(max_fee)
            _tx["maxPriorityFeePerGas"] = int(tip) if tip else tip_wei
        try:
            res = send_tx(rpc, cfg.wallet_key, _tx,
                          wait=False, min_tip_wei=tip_wei, broadcast_raw_urls=broadcast_raw_urls)
        except Exception as e:
            _LOCAL_NONCE.pop(bot, None)   # не знаем, съелся ли nonce -> следующий dispatch пере-читает RPC
            if log:
                log.info("DIAG canary send fail: %s", type(e).__name__)
            return None
        _LOCAL_NONCE[bot] = base_nonce + 1
        with _INFLIGHT_LOCK:
            if _spec:
                _SPEC_PROBES += 1
                _SPEC_LAST_TS = _now
            else:
                _CANARY_PROBES += 1
                _CANARY_LAST_TS = _now
        return res["hash"]


# ---- SPEC-FIRE live (Stage B, 24.07): боевые пробы-ликвидации в predict-окнах ----
# Свой газ-бакет (НЕ kill-switch: реверты проб = замер, как канарейка; WIN попадает в обычный
# учёт при желании владельца). Гейты атомарно под _SEND_LOCK. Пробы НЕ трогают _RECENT_SENDS
# (иначе проба глушила бы БОЕВОЙ реактивный выстрел по той же жертве — дедуп-ловушка Stage B);
# WIN маркируется отдельно (mark_recent_send) сборщиком receipts.
_SPECFIRE_PROBES = 0
_SPECFIRE_GAS_USD = 0.0
_SPECFIRE_LAST_TS = 0.0
_SPECFIRE_DAY = int(time.time() // 86400)   # UTC-день бакета: суточный сброс (был lifetime —
                                            # к вечернему каскаду волатильного дня бакет пуст)
_SPECFIRE_MAX_GAS_USD = float(os.environ.get("SPEC_FIRE_MAX_GAS_USD", "10"))
_SPECFIRE_MIN_INTERVAL = float(os.environ.get("SPEC_FIRE_MIN_INTERVAL_SEC", "0.25"))


def _specfire_roll_day_locked():
    """Вызывать ТОЛЬКО под _INFLIGHT_LOCK: сброс бакета на границе UTC-суток."""
    global _SPECFIRE_DAY, _SPECFIRE_GAS_USD
    d = int(time.time() // 86400)
    if d != _SPECFIRE_DAY:
        _SPECFIRE_DAY = d
        _SPECFIRE_GAS_USD = 0.0


def specfire_stats():
    with _INFLIGHT_LOCK:
        _specfire_roll_day_locked()
        return (_SPECFIRE_PROBES, _SPECFIRE_GAS_USD)


def specfire_gas_today() -> float:
    """Спек-газ за текущие UTC-сутки — слагаемое в общий kill-switch (гард видит СУММАРНЫЙ
    дневной расход: боевой + пробный; спек-кап $10 = максимум 10% от дневного лимита)."""
    with _INFLIGHT_LOCK:
        _specfire_roll_day_locked()
        return _SPECFIRE_GAS_USD


def specfire_gas_add(gas_usd: float):
    global _SPECFIRE_GAS_USD
    with _INFLIGHT_LOCK:
        _specfire_roll_day_locked()
        _SPECFIRE_GAS_USD += max(0.0, gas_usd)


def mark_recent_send(market_id: str, borrower: str):
    """WIN спек-пробы: жертва взята — гасим последующий реактивный дубль (дешевле $0.01 реверта)."""
    with _SEND_LOCK:
        _RECENT_SENDS[(market_id, borrower)] = time.monotonic()


def send_spec_fire_probe(rpc, cfg, calldata: str, gas: int, log=None,
                         max_fee=None, tip=None, broadcast_raw_urls=None):
    """Боевая спек-проба: НАСТОЯЩИЙ liquidate-calldata ДО транзита (реверт 'healthy' ~$0.01-0.02,
    проба в блоке транзита = предочередь = WIN). Зеркало send_canary: общий _SEND_LOCK/_LOCAL_NONCE
    (канареечная машинерия, 1500+ проб без nonce-инцидентов), fb-fee, SEND-3. Гейты атомарно:
    газ-бакет / мин-интервал / _INFLIGHT==0 (боевая tx главнее). Возвращает hash или None."""
    global _SPECFIRE_PROBES, _SPECFIRE_LAST_TS
    _now = time.monotonic()
    with _SEND_LOCK:
        with _INFLIGHT_LOCK:
            _specfire_roll_day_locked()
            if (_SPECFIRE_GAS_USD >= _SPECFIRE_MAX_GAS_USD
                    or _now - _SPECFIRE_LAST_TS < _SPECFIRE_MIN_INTERVAL
                    or _INFLIGHT > 0):
                return None
        # патронный резерв: расходники не грызут выстрел (см. BULLET_RESERVE_WEI)
        _bot_addr = account_for(rpc._web3(), cfg.wallet_key).address
        _cost = int(gas) * int(max_fee if max_fee else int(cfg.tip_gwei * 1e9) * 3)
        if not probe_allowed_by_reserve(cached_balance_wei(rpc, _bot_addr), _cost):
            if log:
                log.info("SPECFIRE проба молчит: баланс не покрывает патрон %.3f ETH + пробу",
                         BULLET_RESERVE_WEI / 1e18)
            return None
        w3 = rpc._web3()
        bot = account_for(w3, cfg.wallet_key).address
        base_nonce = _LOCAL_NONCE.get(bot)
        if base_nonce is None:
            base_nonce = _pending_nonce(w3, bot)
        tip_wei = int(cfg.tip_gwei * 1e9)
        _tx = {"to": cfg.liquidator_address, "data": calldata, "nonce": base_nonce, "gas": int(gas)}
        if max_fee is not None:
            _tx["maxFeePerGas"] = int(max_fee)
            _tx["maxPriorityFeePerGas"] = int(tip) if tip else tip_wei
        try:
            res = send_tx(rpc, cfg.wallet_key, _tx,
                          wait=False, min_tip_wei=tip_wei, broadcast_raw_urls=broadcast_raw_urls)
        except Exception as e:
            _LOCAL_NONCE.pop(bot, None)
            if log:
                log.info("SPECFIRE probe send fail: %s", type(e).__name__)
            return None
        _LOCAL_NONCE[bot] = base_nonce + 1
        with _INFLIGHT_LOCK:
            _SPECFIRE_PROBES += 1
            _SPECFIRE_LAST_TS = _now
        return res["hash"]


def canary_invalidate_nonce(rpc, cfg):
    """Канарейка не замайнилась за timeout (возможно дропнута) -> _LOCAL_NONCE впереди чейна ->
    следующая tx уйдёт в дыру. Инвалидируем (как timeout-ветка dispatch, II нонс-фикс)."""
    try:
        bot = account_for(rpc._web3(), cfg.wallet_key).address
        with _SEND_LOCK:
            _LOCAL_NONCE.pop(bot, None)
    except Exception:
        pass


def _post_raw_tx(url: str, raw_hex: str, timeout: int = 5):
    """SEND-3: eth_sendRawTransaction напрямую в секвенсер Base ПАРАЛЛЕЛЬНО с Alchemy-путём.
    Best-effort в daemon-треде: дубль безвреден (второй приём = 'already known'), ошибка здесь
    НЕ роняет send-путь. Смысл: убрать Alchemy-прокси-хоп из гонки за включение.
    Поправка 07-17: тёплая keep-alive сессия (_seq_session) вместо холодного TCP+TLS на каждый вызов."""
    try:
        _ss = time.perf_counter()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_hex]}
        s = _seq_session(url)
        if s is not None:
            resp = s.post(url, json=payload, timeout=timeout).json()
        else:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        err = resp.get("error")
        if err and "already known" not in str(err.get("message", "")).lower():
            log.info("DIAG seq-send %s err=%s", url, str(err)[:120])
        else:
            # ГОНОЧНАЯ метрика: ack секвенсера = момент, когда наша raw В ОЧЕРЕДИ ордерера.
            # «dispatch: sent» в логе — это Alchemy-нога ПОСЛЕ, гонку она не отражает (23.07).
            log.info("DIAG seq-send ack=%.0fms %s", (time.perf_counter() - _ss) * 1000.0, url.split("/")[2])
    except Exception as e:
        log.info("DIAG seq-send %s fail=%s", url, type(e).__name__)


def _reconcile_nonce(w3, bot: str):
    """Фоновая сверка локального nonce с RPC после батча (см. инварианты _LOCAL_NONCE)."""
    try:
        n = int(w3.eth.get_transaction_count(bot, "pending"))
        with _SEND_LOCK:
            cur = _LOCAL_NONCE.get(bot)
            if cur is not None:
                _LOCAL_NONCE[bot] = max(n, cur)
    except Exception:
        pass

ORACLE_PRICE_SCALE = 10 ** 36
WAD = 10 ** 18
KYBER_HOST = "https://aggregator-api.kyberswap.com"
_CLIENT_ID = "kelbic-liquidator"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"   # KyberSwap CF blocks default urllib UA


def _get(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "x-client-id": _CLIENT_ID, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-client-id": _CLIENT_ID, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def kyber_swap(token_in: str, token_out: str, amount_in: int, sender: str, recipient: str,
               slippage_bps: int = 100, chain: str = "base", timeout: int = 15) -> dict:
    """KyberSwap aggregator (keyless). Two steps: GET /routes -> POST /route/build.
    Returns {router, calldata, amount_in, amount_out, min_out}. The encoded swap pulls token_in
    from `sender` (our contract, which approved the router) and sends token_out to `recipient`
    (our contract). slippage_bps caps the route's own min-out (10000 = 100%); we ALSO gate on
    minProfit in the contract, so this is a secondary guard."""
    # PRE-ARM (Stage A, 24.07): predict-окно уже прогрело квоту этой пары -> отдаём из кэша
    # (0мс вместо 2 HTTP RTT 0.4-0.9с В ГОНКЕ). Валидация внутри prearm_get: возраст <= TTL,
    # cached_amount_in в [BAND..1.0]*amount_in (сайзинг при arm-цене <= seized при флип-цене
    # по построению — цена идёт в сторону флипа). Экономика не обходится: sim/minProfit ниже
    # по потоку работают как обычно; недобор <= 3% остаётся пылью в контракте.
    try:
        from chain.prearm import prearm_get
        _hit = prearm_get(token_in, token_out, int(amount_in))
    except Exception:
        _hit = None
    if _hit is not None:
        log.info("PREARM hit %s->%s req=%d cached=%d age=%.1fs (Kyber RTT снят)",
                 token_in[:10], token_out[:10], int(amount_in),
                 _hit["swap"].get("amount_in", 0), _hit["age_sec"])
        return _hit["swap"]
    api = f"{KYBER_HOST}/{chain}/api/v1"
    q = urlencode({"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(int(amount_in))})
    routes = _get(f"{api}/routes?{q}", timeout)
    if routes.get("code") not in (0, None):
        raise RuntimeError(f"kyber routes error: {routes.get('message')}")
    rd = routes["data"]
    built = _post(f"{api}/route/build",
                  {"routeSummary": rd["routeSummary"], "sender": sender, "recipient": recipient,
                   "slippageTolerance": int(slippage_bps)}, timeout)
    if built.get("code") not in (0, None):
        raise RuntimeError(f"kyber build error: {built.get('message')}")
    bd = built["data"]
    return {
        "router": rd["routerAddress"],
        "calldata": bd["data"],
        "amount_in": int(bd["amountIn"]),
        "amount_out": int(bd["amountOut"]),
        "min_out": int(bd["amountOut"]) * (10000 - int(slippage_bps)) // 10000,  # kyber bakes this into calldata
    }


# ---- direct-swap (variant A): прекомпьютед-маршруты, НОЛЬ серверных RTT в гонке ----
# Два роутера с одной path-механикой (token + 3 байта + token ...):
#  - Uniswap SwapRouter02: 3 байта = fee tier; exactInput((bytes,address,uint256,uint256)).
#  - Aerodrome Slipstream SwapRouter: 3 байта = tickSpacing; exactInput v1-форма С deadline
#    ((bytes,address,uint256,uint256,uint256)) — селекторы/factory верифицированы bytecode 07-08.
# Пулы верифицированы 07-08 (factory.getPool + liquidity()>0 живым RPC):
#  cbXRP/cbDOGE/cbADA -> WETH(3000) -> USDC(500) UniV3 (как было);
#  cbBTC -> EURC напрямую Slipstream ts=100 (UniV3 WETH/EURC и Aerodrome-basic ПУСТЫЕ — 07-08);
#  cbLTC -> WETH ts=200 -> USDC ts=100 Slipstream (на UniV3 у cbLTC ликвидности НЕТ вообще).
UNIV3_SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"  # Uniswap SwapRouter02 (Base)
AERO_CL_ROUTER = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"     # Aerodrome Slipstream SwapRouter (Base)
_UNIV3_WETH = "0x4200000000000000000000000000000000000006"
_UNIV3_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_EURC = "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42"
_CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
_CBLTC = "0xcb17c9db87b595717c857a08468793f5bab6445f"
_LBTC = "0xecac9c5f704e954931349da37f60e39f515c11c1"   # из Morpho idToMarketParams(0x4944a116) 07-12
_WSTETH = "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452" # из idToMarketParams(wstETH/WETH) 07-14
_WEETH = "0x04c0599ae5a44757c0af6f9ec3b93da8976c150a"  # из idToMarketParams(weETH/WETH 0x78d11c03) 24.07
_CBETH = "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22"  # Coinbase Wrapped Staked ETH (Base)
_UNIV3_WETH_USDC_FEE = 500
_UNIV3_EXACT_INPUT_SEL = "b858183f"    # exactInput((bytes,address,uint256,uint256)) verified cast sig
_AERO_EXACT_INPUT_SEL = "c04b8d59"     # exactInput((bytes,address,uint256,uint256,uint256)) verified bytecode
_AERO_DEADLINE = 2 ** 63               # v1-форма требует deadline; константа = «не истекает»
# Ре-верификация 24.07 (живой пропуск cbDOGE 13:08 — победитель вышел Slipstream ts=2000,
# наш UniV3 fee=3000 дал underflow): ликвидность мем-ног УЕХАЛА с проверки 07-08.
# QuoterV2/Slipstream-quoter 24.07: cbXRP fee=3000 МЁРТВ -> fee=10000 (линейно x10);
# cbDOGE UniV3 3000 полупуст (x10 обрыв), Slipstream ts=2000 даёт 2.1x больше и линеен
# ($152/$1514 на x1/x10); cbADA UniV3 мёртв ВСЕ тиры -> Slipstream ts=200 ($31/$305/$1525
# линейно). cbDOGE/cbADA -> полные Slipstream-пути (вторая нога WETH->USDC ts=100, как cbLTC).
UNIV3_PATHS = {  # collateral(lower) -> fee tier of coll/WETH pool (re-verified 24.07)
    "0xcb585250f852c6c6bf90434ab21a00f02833a4af": 10000,  # cbXRP/WETH (3000 умер, 1% живой)
}
_CBDOGE = "0xcbd06e5a2b0c65597161de254aa074e489deb510"
_CBADA = "0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c"


def _pack_path(*hops) -> bytes:
    """UniV3/Slipstream path: token (+ 3-байтовый fee/tickSpacing + token)*. Чистая (юнит)."""
    out = bytes.fromhex(hops[0][2:])
    for i in range(1, len(hops), 2):
        out += int(hops[i]).to_bytes(3, "big") + bytes.fromhex(hops[i + 1][2:])
    return out


_SWAP_ROUTES = {  # (collateral_lower, loan_lower) -> {router, style, path}
    # max_in (24.07 ре-верификация): пул просел с 07-08 — ratio 1.0 cbBTC=0.9967,
    # 2.0=0.9696 (плавная деградация, не обрыв). Кап 1.5 (~$97k, imp ~1.2%), сверх — Kyber.
    (_CBBTC, _EURC): {"router": AERO_CL_ROUTER, "style": "v1",
                      "path": _pack_path(_CBBTC, 100, _EURC),
                      "max_in": 15 * 10**7},
    (_CBLTC, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBLTC, 200, _UNIV3_WETH, 100, _UNIV3_USDC)},
    # cbBTC/USDC: ПРЯМОЙ пул fee=500 (0xfbb6eed8, liq 2.1e12) доминирует 2-hop через WETH на ВСЕХ
    # размерах — QuoterV2 23.07: $30k imp 0.01% vs 0.10%, $500k 0.95% vs 1.68%, $1M 2.02% vs 3.48%
    # (при LIF 4.4% профит жив и на $1M). fee=100/3000 хуже на порядок/в разы.
    # max_in (24.07, ревью мега-сайза): QuoterV2 imp $1.6M=3.39%, $2.5M=5.78% > бонуса 4.38% —
    # сверх ~$2M директ гарантированно ревертит по minProfit-floor. Кап 30 cbBTC (~$1.9M):
    # ниже — скорость директа (профит жив), выше — Kyber-агрегат (PREARM греет в окнах).
    (_CBBTC, _UNIV3_USDC): {"router": UNIV3_SWAP_ROUTER, "style": "02",
                            "path": _pack_path(_CBBTC, 500, _UNIV3_USDC),
                            "max_in": 30 * 10**8},
    # LBTC/cbBTC 07-12 (нож $350.8k долга, BTC-пег пара — профиль «мгновенный флип на ноже»):
    # UniV3 ПУСТ на всех тирах (QuoterV2-проба impact~99.6%, analysis/lbtc_route_probe.py);
    # Slipstream ts=1 ЖИВОЙ (router.factory()->getPool, liquidity=125.6B, 07-12); Kyber-глубина
    # $380k ratio=1.00038 (паритет) — эконом чистая, вопрос был в скорости (Kyber RTT ~0.4-0.9с
    # vs fast-lane ~0.1с). Маршрут даёт fast-lane + кэш слоя 2 (ROUTE_COLLATERALS авто).
    (_LBTC, _CBBTC): {"router": AERO_CL_ROUTER, "style": "v1",
                      "path": _pack_path(_LBTC, 1, _CBBTC)},
    # wstETH/WETH 07-14 (нож $180.2k @ HF 1.0056, LST-пег — профиль «мгновенного флипа на тике»):
    # UniV3 тонкий (fee=100 imp@60wstETH=57%, fee=500 пусто), Slipstream ts=1 живой+глубокий.
    # loan=WETH -> single-hop wstETH->WETH; fast-lane + кэш слоя-2 (ROUTE_COLLATERALS авто).
    # max_in (24.07): QuoterV2 показал ликвидность ts=1 подвижна — 200 wstETH imp 0.002%, 500 imp
    # 6.8% (за ночь просело). Кап 300 wstETH (~$700k) < обрыва -> сверх него direct=None ->
    # Kyber-агрегат (глубже, но серверный RTT). Кап в ЕДИНИЦАХ token_in (18 dec).
    (_WSTETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                             "path": _pack_path(_WSTETH, 1, _UNIV3_WETH),
                             "max_in": 300 * 10**18},
    # weETH/WETH 24.07 (кандидат $452k @ HF 1.0113 — direction по ETH? нет: weETH↔WETH оба ETH-нога,
    # но ликвидатору всё равно нужен ВЫХОД weETH->WETH при взятии). Раньше роута НЕ было -> Kyber
    # block-loop 0.4-0.9с. Slipstream ts=1 живой (pool 0xbd3cd0, liq 2.23e24): плоско до 180 weETH
    # (imp 0.012%), обрыв 210 (0.83%). Кап 160 weETH (~$330k) < обрыва. loan=WETH single-hop.
    (_WEETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_WEETH, 1, _UNIV3_WETH),
                            "max_in": 160 * 10**18},
    # cbETH/WETH 24.07 (пег-нож, кандидаты HF 1.01-1.02; выхода НЕ БЫЛО вовсе — Kyber
    # block-loop, та же дыра, что у weETH до 24.07): UniV3 fee=500 обрывается после ~7 cbETH
    # (28 -> ratio 0.59), Slipstream ts=1 БЕЗ обрыва до 500 cbETH (ratio 1.13531 плоско).
    # Кап 300 по уроку wstETH (ликвидность ts=1 подвижна). fast-lane + кэш слоя-2 авто.
    (_CBETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBETH, 1, _UNIV3_WETH),
                            "max_in": 300 * 10**18},
    # cbDOGE/cbADA -> USDC: полные Slipstream-пути (ре-верификация 24.07, см. коммент выше).
    # max_in (25.07): пул cbDOGE чист до 90k ток. (imp 0.9%), обрыв на 135k (out каппится $8.2k);
    # cbADA чист до 60k (imp 2.3%), обрыв на 120k. Сверх капа -> Kyber. NB: Kyber на ките $80k
    # cbDOGE отдаёт лишь $17.3k (21.5% номинала) — ВСЯ DOGE-ликвидность Base мелкая; жирный
    # cbDOGE-кандидат экономически несобираем целиком, minProfit-floor отсечёт сам.
    (_CBDOGE, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                             "path": _pack_path(_CBDOGE, 2000, _UNIV3_WETH, 100, _UNIV3_USDC),
                             "max_in": 100_000 * 10**8},
    (_CBADA, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBADA, 200, _UNIV3_WETH, 100, _UNIV3_USDC),
                            "max_in": 60_000 * 10**6},
}
for _coll, _fee in UNIV3_PATHS.items():   # существующие UniV3-пути -> та же таблица (loan=USDC)
    _SWAP_ROUTES[(_coll, _UNIV3_USDC)] = {"router": UNIV3_SWAP_ROUTER, "style": "02",
                                          "path": _pack_path(_coll, _fee, _UNIV3_WETH,
                                                             _UNIV3_WETH_USDC_FEE, _UNIV3_USDC)}
ROUTE_COLLATERALS = {c for (c, _l) in _SWAP_ROUTES}   # scope слоя 2 (кэш позиций) и fast-lane


def direct_swap(token_in, token_out, amount_in, sender, recipient, *, repaid_assets, slippage_bps=100):
    """Drop-in for kyber_swap на прямых маршрутах, NO in-race quote. P1-8 fix: token_out ЧЕСТНЫЙ
    ключ маршрута (раньше univ3_swap хардкодил USDC-выход — для EURC-рынка контракт получил бы
    USDC и упал CannotRepay). amountOutMinimum=repaid_assets (debt floor); profit protected by
    preconf sim + contract minProfit. Returns {router, calldata} или None (caller -> kyber_swap)."""
    from eth_abi import encode
    r = _SWAP_ROUTES.get((token_in.lower(), token_out.lower()))
    if r is None:
        return None
    # пер-роутный кап (24.07): выше ёмкости одиночного пула (обрыв слиппажа) отдаём None ->
    # caller падает на Kyber-агрегат (глубже, но серверный RTT — уже НЕ fast-lane). Защищает
    # wstETH/weETH от исполнения мега-сайза в тонкий пул. Кап в единицах token_in.
    _cap = r.get("max_in")
    if _cap is not None and int(amount_in) > int(_cap):
        return None
    if r["style"] == "v1":     # Slipstream: v1-форма с deadline
        params = encode(["(bytes,address,uint256,uint256,uint256)"],
                        [(r["path"], recipient, _AERO_DEADLINE, int(amount_in), int(repaid_assets))])
        return {"router": r["router"], "calldata": "0x" + _AERO_EXACT_INPUT_SEL + params.hex()}
    params = encode(["(bytes,address,uint256,uint256)"],
                    [(r["path"], recipient, int(amount_in), int(repaid_assets))])
    return {"router": r["router"], "calldata": "0x" + _UNIV3_EXACT_INPUT_SEL + params.hex()}


def univ3_swap(token_in, token_out, amount_in, sender, recipient, *, repaid_assets, slippage_bps=100):
    """Обратная совместимость (пробники/скрипты): теперь тонкая обёртка direct_swap."""
    return direct_swap(token_in, token_out, amount_in, sender, recipient,
                       repaid_assets=repaid_assets, slippage_bps=slippage_bps)


def expected_seized(repaid_assets: int, lif: float, price: int) -> int:
    """Collateral (wei) Morpho seizes for repaying `repaid_assets` (loan wei), per _liquidate:
    seized = mulDivDown(wMulDown(repaidAssets, LIF), ORACLE_PRICE_SCALE, price)."""
    lif_wad = int(round(lif * WAD))
    incentivized = repaid_assets * lif_wad // WAD
    return incentivized * ORACLE_PRICE_SCALE // price


# --- liquidate() bundle + pre-send eth_call gate ---

_LIQUIDATE_SIG = ("liquidate((address,address,address,address,uint256),"
                  "address,uint256,address,bytes,uint256)")


def encode_liquidate(mp: dict, borrower: str, repaid_shares: int, swap_target: str,
                     swap_calldata: str, min_profit: int) -> str:
    """ABI-encode Liquidator.liquidate(mp, borrower, repaidShares, swapTarget, swapData, minProfit)."""
    from eth_abi import encode
    from eth_utils import keccak
    selector = keccak(text=_LIQUIDATE_SIG)[:4]
    cd = bytes.fromhex(swap_calldata[2:] if swap_calldata.startswith("0x") else swap_calldata)
    args = encode(
        ["(address,address,address,address,uint256)", "address", "uint256", "address", "bytes", "uint256"],
        [(mp["loanToken"], mp["collateralToken"], mp["oracle"], mp["irm"], int(mp["lltv"])),
         borrower, int(repaid_shares), swap_target, cd, int(min_profit)],
    )
    return "0x" + (selector + args).hex()


# --- batch: N targets in ONE tx (cascade capture). Контракт: batchLiquidate(LiquidationItem[]) ---
_BATCH_LIQUIDATE_SIG = ("batchLiquidate(((address,address,address,address,uint256),"
                        "address,uint256,address,bytes,uint256)[])")
_ITEM_TYPE = "((address,address,address,address,uint256),address,uint256,address,bytes,uint256)"


def _prep_to_item_tuple(prep: dict):
    """Из prep (сырые компоненты, положенные prepare_hot/fast) -> ABI-tuple LiquidationItem."""
    mp = prep["mp"]
    cd = prep["swap_calldata"]
    cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if isinstance(cd, str) else bytes(cd)
    return ((mp["loanToken"], mp["collateralToken"], mp["oracle"], mp["irm"], int(mp["lltv"])),
            prep["borrower"], int(prep["repaid_shares"]), prep["swap_target"], cd, int(prep["min_profit"]))


# ---- ПАТРОННЫЙ РЕЗЕРВ (25.07, баланс долит до 0.1938 ETH) ----
# Один полный выстрел на каскадном tip стоит РЕЗЕРВА limit×maxFee ≈ 4M × 30 gwei = 0.12 ETH.
# Расходники (канареечные/спек-пробы по 0.05 gwei) не должны его отгрызть: пока баланс не
# покрывает патрон + стоимость пробы, ПРОБЫ МОЛЧАТ. Боевой путь резервом НЕ гейтится — он и есть
# патрон. Баланс кэшируем (19 CU/чтение, квота дорога): пробы редки, 60с свежести достаточно.
BULLET_RESERVE_WEI = int(float(os.environ.get("GAS_BULLET_RESERVE_ETH", "0.12")) * 10 ** 18)
_BAL_CACHE = {"wei": None, "ts": 0.0}
_BAL_TTL = float(os.environ.get("GAS_BALANCE_TTL_SEC", "60"))


def cached_balance_wei(rpc, address: str, now=None):
    """Баланс с TTL-кэшем. None = прочитать не удалось (вызыватель обязан fail-open)."""
    t = time.monotonic() if now is None else now
    if _BAL_CACHE["wei"] is not None and t - _BAL_CACHE["ts"] < _BAL_TTL:
        return _BAL_CACHE["wei"]
    try:
        w = int(rpc._web3().eth.get_balance(address))
    except Exception:
        return _BAL_CACHE["wei"]                  # старое значение лучше слепоты
    _BAL_CACHE["wei"], _BAL_CACHE["ts"] = w, t
    return w


def probe_allowed_by_reserve(balance_wei, probe_cost_wei: int, reserve_wei=None) -> bool:
    """Чистая: можно ли тратить на ПРОБУ, не залезая в патронный резерв. Неизвестный баланс
    (None) -> True (fail-open: слепота не должна глушить измерительный слой)."""
    if balance_wei is None:
        return True
    r = BULLET_RESERVE_WEI if reserve_wei is None else int(reserve_wei)
    return int(balance_wei) - int(probe_cost_wei) >= r


def gas_floor_of(cfg) -> int:
    """Лимит-фолбэк (НЕ оценка стоимости). См. config.gas_limit_floor + форк-экзамен 25.07."""
    return int(getattr(cfg, "gas_limit_floor", 3_000_000) or 3_000_000)


def batch_gas_limit(preps: list, cfg) -> int:
    """Лимит батч-tx: Σ(gas каждой позиции) + 30k×n, ×1.25 (63/64-мина C3c — голодный хвост).
    ФОЛБЭК = gas_limit_floor, НЕ gas_limit_est: форк 25.07 показал реальный расход 2.2-2.5M на
    ОДНУ жирную ликвидацию, а prepare_liquidation (блок-луп) поле gas не отдаёт вовсе — с прежним
    фолбэком 500k батч из 3 китов ушёл бы с лимитом ~2M против нужных ~7M = out-of-gas. Чистая."""
    floor = gas_floor_of(cfg)
    return int((sum(int(p.get("gas") or floor) for p in preps) + 30_000 * len(preps)) * 1.25)


def clamp_fee_to_balance(gas_limit: int, max_fee: int, tip: int, balance_wei: int,
                         min_tip_wei: int = 0, reserve_bps: int = 1000):
    """Резерв под tx = gas_limit × max_fee и он должен влезть в БАЛАНС (иначе узел отбивает
    'insufficient funds' и выстрел не уходит вовсе). Лимит НЕ режем — это вернуло бы OOG-дыру;
    режем max_fee/tip: теряем позицию в fee-сорте, но транзакция летит. reserve_bps — подушка
    на уже висящие в полёте tx. -> (max_fee, tip, ok). ok=False: даже базовая цена не влезает.
    Чистая (юнит). Мотив: владелец не доливает ETH (25.07) — код обязан жить на текущем балансе."""
    if balance_wei <= 0 or gas_limit <= 0 or max_fee <= 0:
        return max_fee, tip, False
    budget = balance_wei * (10_000 - reserve_bps) // 10_000
    need = gas_limit * max_fee
    if need <= budget:
        return max_fee, tip, True
    base_part = max(0, max_fee - tip)                  # ~2×basefee из нашей конструкции fee
    affordable = budget // gas_limit
    if affordable <= base_part:                        # не хватает даже на базовую цену
        return max_fee, tip, False
    new_tip = affordable - base_part
    if min_tip_wei and new_tip < min_tip_wei:
        return max_fee, tip, False                     # ниже минимального tip смысла нет
    return int(affordable), int(new_tip), True


def encode_batch_liquidate(preps: list) -> str:
    """ABI-encode Liquidator.batchLiquidate([LiquidationItem...]) из списка prep-словарей, у каждого
    из которых есть сырые компоненты (mp/borrower/repaid_shares/swap_target/swap_calldata/min_profit)."""
    from eth_abi import encode
    from eth_utils import keccak
    selector = keccak(text=_BATCH_LIQUIDATE_SIG)[:4]
    args = encode([_ITEM_TYPE + "[]"], [[_prep_to_item_tuple(p) for p in preps]])
    return "0x" + (selector + args).hex()


# события НАШЕГО контракта для per-item разбора receipt батча (лениво, нужен eth_utils)
_OUR_LIQUIDATED_SIG = None    # Liquidated(address indexed borrower, address indexed loanToken, uint256 profit)
_BATCH_ITEM_FAILED_SIG = None # BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason)


def parse_batch_receipt(receipt, liquidator_addr: str):
    """Per-item исход батча ТОЛЬКО из событий НАШЕГО контракта (фантомная прибыль запрещена: status==1
    НЕ значит «все сели»). Возвращает: {(borrower_lc, loan_lc): profit_wei} по Liquidated + set
    провалившихся borrower_lc по BatchItemFailed. Матч по (borrower,loanToken) — устойчив к двум item
    одного заёмщика в разных рынках. Никогда не бросает."""
    global _OUR_LIQUIDATED_SIG, _BATCH_ITEM_FAILED_SIG
    wins: dict = {}     # (borrower_lc, loan_lc) -> profit_wei
    fails: set = set()  # borrower_lc, у кого был BatchItemFailed
    try:
        from eth_utils import keccak
        if _OUR_LIQUIDATED_SIG is None:
            _OUR_LIQUIDATED_SIG = "0x" + keccak(text="Liquidated(address,address,uint256)").hex()
            _BATCH_ITEM_FAILED_SIG = "0x" + keccak(text="BatchItemFailed(uint256,address,bytes4)").hex()
        liq_lc = (liquidator_addr or "").lower()
        for lg in (receipt.get("logs") or []):
            if (lg.get("address") or "").lower() != liq_lc:
                continue
            topics = [t.hex() if hasattr(t, "hex") else str(t) for t in (lg.get("topics") or [])]
            if not topics:
                continue
            t0 = topics[0] if topics[0].startswith("0x") else "0x" + topics[0]
            if t0.lower() == _OUR_LIQUIDATED_SIG and len(topics) >= 3:
                borrower_lc = "0x" + topics[1][-40:]
                loan_lc = "0x" + topics[2][-40:]
                data = lg.get("data")
                data = bytes.fromhex(data[2:]) if isinstance(data, str) else bytes(data or b"")
                profit = int.from_bytes(data[:32], "big") if len(data) >= 32 else 0
                wins[(borrower_lc.lower(), loan_lc.lower())] = profit
            elif t0.lower() == _BATCH_ITEM_FAILED_SIG and len(topics) >= 3:
                borrower_lc = "0x" + topics[2][-40:]
                fails.add(borrower_lc.lower())
    except Exception:
        pass
    return wins, fails


def simulate_tx(rpc, liquidator_addr: str, from_addr: str, calldata: str,
                state_override: dict | None = None, block: str = "latest") -> dict:
    """eth_call the liquidate bundle (no send). {ok, profit, error}. `block` defaults to 'latest' (the
    confirmed-state gate the block loop uses); the hot path passes a preconf RPC + block='pending' to
    gate on the pre-confirmed price, where a just-transmitted price has already moved the position."""
    tx = {"to": liquidator_addr, "from": from_addr, "data": calldata}
    try:
        ret = bytes(rpc.eth_call(tx, block, state_override))
        profit = int.from_bytes(ret[:32], "big") if len(ret) >= 32 else 0
        return {"ok": True, "profit": profit, "error": None}
    except Exception as e:
        return {"ok": False, "profit": 0, "error": str(e)[:240]}


# --- sign + submit (EIP-1559 on Base) ---

def send_tx(rpc, key: str, tx: dict, wait: bool = True, timeout: int = 120,
            min_tip_wei: int = 0, broadcast_raw_urls: list | None = None) -> dict:
    """Sign `tx` with `key` and submit to Base. Fills from/chainId/nonce/fees/gas if absent.
    broadcast_raw_urls (SEND-3): доп. endpoints (секвенсер) для параллельного raw-broadcast —
    треды стартуют ДО Alchemy-сенда, hash детерминирован от подписи, дубль безвреден."""
    w3 = rpc._web3()
    acct = account_for(w3, key)
    t = dict(tx)
    t.setdefault("from", acct.address)
    t.setdefault("chainId", rpc.chain_id)
    if "nonce" not in t:
        t["nonce"] = _pending_nonce(w3, acct.address)
    if "maxFeePerGas" not in t and "gasPrice" not in t:
        base = w3.eth.get_block("latest").get("baseFeePerGas") or 0
        tip = max(int(w3.eth.max_priority_fee), int(min_tip_wei))
        t["maxPriorityFeePerGas"] = tip
        t["maxFeePerGas"] = int(base) * 2 + tip
    if "gas" not in t:
        t["gas"] = int(w3.eth.estimate_gas(t) * 12 // 10)
    _t0 = time.perf_counter()
    signed = acct.sign_transaction(t)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    _t_sign = time.perf_counter()
    _relayed = False
    if broadcast_raw_urls:
        raw_hex = "0x" + bytes(raw).hex()
        for _u in broadcast_raw_urls:
            threading.Thread(target=_post_raw_tx, args=(_u, raw_hex), daemon=True).start()
        _relayed = True
    _t_relay = time.perf_counter()
    try:
        h = w3.eth.send_raw_transaction(raw)
    except Exception:
        # SEND-3 race: raw уже роздан в секвенсер (реальный ордерер Base) daemon-тредами ВЫШЕ, ДО
        # этого send'а -> tx МОЖЕТ замайниться, даже если Alchemy-путь бросил. hash детерминирован
        # (keccak подписи == тот же, что вернул бы send_raw_transaction), поэтому возвращаем его +
        # uncertain=True: вызывающий ЗАПИШЕТ отправку (дедуп + ожидание receipt), а не сочтёт «не
        # отправлено» -> не будет ни дубля того же заёмщика, ни неучтённого kill-switch'ем газа.
        # Без SEND-3 (_relayed=False) ничего не ушло -> пробрасываем как раньше (nonce не съеден).
        if not _relayed:
            raise
        _hh = getattr(signed, "hash", None)
        if _hh is None:
            raise
        return {"hash": _hh.hex(), "status": None, "gas_used": None, "receipt": None, "uncertain": True}
    # Разложение send-пути (23.07): подпись CPU-мс; relay-спавн ~0 (треды в секвенсер уже летят);
    # alch = синхронная Alchemy-нога — журнал/дедуп, НЕ гонка (raw в секвенсере с _t_relay).
    log.info("DIAG send-stages sign=%.0fms relay_spawn=%.0fms alch=%.0fms relayed=%s",
             (_t_sign - _t0) * 1000.0, (_t_relay - _t_sign) * 1000.0,
             (time.perf_counter() - _t_relay) * 1000.0, _relayed)
    out = {"hash": h.hex(), "status": None, "gas_used": None, "receipt": None}
    if wait:
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
        out["receipt"] = rcpt; out["status"] = rcpt.get("status"); out["gas_used"] = rcpt.get("gasUsed")
    return out


# --- full execute action for one candidate (read fresh -> gate -> send) ---

_MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"


def prepare_liquidation(rpc, cfg, market_id: str, borrower: str, debt_usd: float,
                        debt_assets: int, slippage_bps: int = 100) -> dict:
    """Read fresh on-chain state, size the seize, fetch the swap, simulate, and gate on the NET
    floor — everything UP TO sending. Returns {ok: True, calldata, net_usd, profit_usd, cost_usd}
    ready to sign+send, or {ok: False, reason, net_usd?}. No send, so many candidates can be
    prepared and then dispatched together (parallel). Catches its own errors."""
    try:
        from chain.multicall import (aggregate3, encode_id_to_market_params_call,
            decode_id_to_market_params, encode_market_call, decode_market,
            encode_price_call, decode_price, encode_position_call, decode_position)
        from chain.simulate import to_assets_up
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = w3.eth.account.from_key(cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        r1 = aggregate3(rpc, [(_MORPHO_BLUE, encode_id_to_market_params_call(mid)),
                              (_MORPHO_BLUE, encode_market_call(mid)),
                              (_MORPHO_BLUE, encode_position_call(mid, borrower))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
        m = decode_market(r1[1][1]); tba, tbs = m[2], m[3]
        borrow_shares, _ = decode_position(r1[2][1])
        if borrow_shares == 0:
            return {"ok": False, "reason": "no debt (cleared)"}
        price = decode_price(aggregate3(rpc, [(oracle, encode_price_call())])[0][1])

        repaid_shares = int(borrow_shares)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
        if seized == 0:
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        swap = kyber_swap(coll, loan, seized, liq, liq, slippage_bps=slippage_bps)

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0)
        sim = simulate_tx(rpc, liq, bot, cd0)
        if not sim["ok"]:
            return {"ok": False, "reason": f"sim revert: {sim['error']}"}
        profit_wei = sim["profit"]
        profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
        cost_usd = cfg.gas_limit_est * cfg.tip_gwei * cfg.eth_price_usd / 1e9
        net_usd = profit_usd - cost_usd
        if net_usd < cfg.min_profit_usd:
            return {"ok": False, "net_usd": net_usd,
                    "reason": f"net ${net_usd:.2f} (profit ${profit_usd:.2f} - cost ${cost_usd:.2f}) < min ${cfg.min_profit_usd:.2f}"}

        min_profit_final = profit_wei * 95 // 100   # revert if realized < 95% of simulated
        cd1 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], min_profit_final)
        return {"ok": True, "calldata": cd1, "net_usd": net_usd, "gas": gas_floor_of(cfg),
                "profit_usd": profit_usd, "cost_usd": cost_usd}
    except Exception as e:
        return {"ok": False, "reason": f"error: {type(e).__name__}: {str(e)[:120]}"}


def try_liquidate(rpc, cfg, market_id: str, borrower: str, debt_usd: float,
                  debt_assets: int, log=None, slippage_bps: int = 100) -> dict:
    """Prepare + send ONE liquidation (blocking) — the sequential path. Returns
    {sent, reason?/hash?/status?/net_usd?/profit_usd?}. The batch path prepares many then sends
    non-blocking with sequential nonces; this wrapper keeps the single-shot behavior identical."""
    prep = prepare_liquidation(rpc, cfg, market_id, borrower, debt_usd, debt_assets, slippage_bps)
    if not prep["ok"]:
        out = {"sent": False, "reason": prep["reason"]}
        if "net_usd" in prep:
            out["net_usd"] = prep["net_usd"]
        return out
    try:
        _seq = str(getattr(cfg, "sequencer_rpc", "") or "")
        res = send_tx(rpc, cfg.wallet_key, {"to": cfg.liquidator_address, "data": prep["calldata"]},
                      min_tip_wei=int(cfg.tip_gwei * 1e9),
                      broadcast_raw_urls=([_seq] if (_seq and _seq.lower() not in ("off", "none", "0")) else None))
    except Exception as e:
        return {"sent": False, "reason": f"send error: {type(e).__name__}: {str(e)[:120]}"}
    if log:
        log.info("LIQUIDATE sent %s/%s tx=%s status=%s net~$%.2f (profit $%.2f - cost $%.2f)",
                 market_id[:10], borrower[:10], res["hash"], res["status"],
                 prep["net_usd"], prep["profit_usd"], prep["cost_usd"])
    return {"sent": True, "hash": res["hash"], "status": res["status"], "gas_used": res["gas_used"],
            "profit_usd": prep["profit_usd"], "net_usd": prep["net_usd"]}


def realized_econ(profit_usd: float, gas_used: int, eff_gas_price: int, status, eth_price_usd: float):
    """Realized (net_usd, gas_usd) from a mined receipt. A revert still burns gas, so gas is ALWAYS a
    cost; net = profit - gas on success (status==1), else just -gas. Pure -> the kill-switch math is
    unit-testable: the loss gate must see a revert as a LOSS, not the simulated expected profit."""
    gas_usd = gas_used * eff_gas_price * eth_price_usd / 1e18
    net_usd = (profit_usd - gas_usd) if status == 1 else -gas_usd
    return net_usd, gas_usd


def _revert_bucket(err_str: str) -> str:
    """Map a revert error string to a short diagnostic bucket. Pure -> unit-testable. The buckets we
    care about: 'position healthy' (lost the race / position recovered -> latency lever) vs structural
    (no-route / slippage / Morpho param error -> a different fix). Falls back to the raw msg/selector."""
    import re
    low = err_str.lower()
    if "healthy" in low:
        return "position healthy (lost race / recovered)"
    if "0x81ceff30" in low:
        return "Morpho 0x81ceff30"
    m = re.search(r"reverted:?\s*(.+)", err_str)        # Error(string): 'execution reverted: <msg>'
    if m:
        return m.group(1).strip()[:80]
    sel = re.search(r"0x[0-9a-fA-F]{8}", err_str)        # custom-error selector
    if sel:
        return "selector " + sel.group(0)
    return err_str[:80]


_LIQUIDATE_EVENT_SIG = None   # keccak сигнатуры события Morpho.Liquidate (лениво, нужен eth_utils)


def _submit_xray(rpc, borrower, block_number, our_index, log, lookback=4, cfg=None, our_debt_usd=None):
    """P2-12 submit-side телеметрия v2. Окно [наш−lookback .. наш]: транзит всегда <= нашего блока
    (реакция летит ПОСЛЕ него), а прежний одноблочный поиск давал ложное «реверт НЕ гонка», когда
    победитель сидел в блоке транзита, а мы приземлялись в +1..+2 (разбор 15:10 23.07). Победитель
    обогащается from/to/tip -> строка `race-winner` копит ЭМПИРИЧЕСКУЮ tip-функцию конкурентов
    (grep 'race-winner' по журналу) вместо слепых ступеней. Если tip победителя ВЫШЕ нашего (fee-
    сортировка была) -> взводит каскадную эскалацию tip. Диагностика, не роняет receipt-путь."""
    global _LIQUIDATE_EVENT_SIG
    try:
        from eth_utils import keccak
        if _LIQUIDATE_EVENT_SIG is None:
            _LIQUIDATE_EVENT_SIG = "0x" + keccak(
                text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)").hex()
        from chain.rpc import get_logs_fb
        w3 = rpc._web3()
        topic_b = "0x" + borrower[2:].lower().rjust(64, "0")
        frm = max(0, int(block_number) - int(lookback))
        logs = get_logs_fb(w3, {"fromBlock": frm, "toBlock": block_number,
                                "address": _MORPHO_BLUE,
                                "topics": [_LIQUIDATE_EVENT_SIG, None, None, topic_b]})
        if not logs:
            if log:
                log.info("submit-xray: borrower %s НЕ ликвидирован в [%d..%d] (наш idx=%s) — "
                         "реверт НЕ гонка за жертву (смотреть reason)",
                         borrower[:10], frm, block_number, our_index)
            return
        w = logs[0]                                     # самый ранний Liquidate = победитель
        wblk, widx = w.get("blockNumber"), w.get("transactionIndex")
        wtx = w.get("transactionHash")
        wtx_hex = wtx.hex() if wtx is not None and not isinstance(wtx, str) else (wtx or "?")
        waddr = wto = tip_s = "?"
        try:                                            # обогащение: кто и с каким tip (2 RPC, реверты редки)
            tx = w3.eth.get_transaction(wtx)
            waddr, wto = str(tx.get("from") or "?"), str(tx.get("to") or "?")
            tip = tx.get("maxPriorityFeePerGas")
            if tip is None:                             # legacy-tx: tip = gasPrice - baseFee блока
                bf = int(w3.eth.get_block(wblk).get("baseFeePerGas") or 0)
                tip = max(0, int(tx.get("gasPrice") or 0) - bf)
            tip_s = "%.4f" % (int(tip) / 1e9)
        except Exception:
            pass
        blk_off = (int(block_number) - int(wblk)) if isinstance(wblk, int) else "?"
        if log:
            log.info("submit-xray: победитель blk=%s idx=%s (наш blk=%s idx=%s, отстали блоков=%s) "
                     "tx=%s | race-winner addr=%s to=%s tip_gwei=%s",
                     wblk, widx, block_number, our_index, blk_off, wtx_hex, waddr, wto, tip_s)
        if cfg is not None and tip_s != "?":            # tip победителя > нашего -> взвести каскад
            try:
                from chain.hotpath import dynamic_tip_gwei, note_race_loss_for_cascade
                our_tip = dynamic_tip_gwei(cfg, our_debt_usd)
                note_race_loss_for_cascade(float(tip_s), our_tip, log=log)
            except Exception:
                pass
    except Exception as e:
        if log:
            log.info("submit-xray fail: %s", type(e).__name__)


def _revert_reason(rpc, tx_hash, block_number):
    """Best-effort revert reason for a mined-but-reverted tx: replay it as eth_call at the mined block
    and surface the revert string/selector via _revert_bucket. DIAGNOSTIC ONLY (block end-state may
    differ from what the tx saw mid-block) -> use as a bucket, not proof. Two RPC calls; reverts are
    infrequent so the cost is negligible. Never raises -> failure to diagnose returns a tag, not an error."""
    try:
        w3 = rpc._web3()
        tx = w3.eth.get_transaction(tx_hash)
        rpc.eth_call({"to": tx["to"], "from": tx["from"], "data": tx["input"]},
                     block=block_number if block_number is not None else "latest")
        return "clean-on-replay (state moved / lost race)"
    except Exception as e:
        return _revert_bucket(str(e))


def _send_units_volley(rpc, w3, cfg, units, base_nonce, tip_wei, bcast_urls, log=None):
    """PARALLEL_BROADCAST (залп): подписать ВСЕ юниты -> раздать ВСЕ raw в секвенсер одним залпом ->
    Alchemy-нога журналом ПОСЛЕ. Старый цикл шлёт юниты последовательно: гоночная нога юнита u+1
    стартует только после синхронной Alchemy-ноги юнита u (~60мс RTT) — в каскаде >batch_max_items
    жертв юнит 3 приходит в FCFS-очередь на ~120мс позже юнита 1. Здесь подпись = CPU-мс (fee-контекст
    ОДИН на залп, не RTT×юниты), межюнитная задержка гоночной ноги = спавн треда (~0).
    Sign-фейл юнита u: ничего ещё не в сети -> хвост дропаем (nonce-гэп исключён, семантика старого
    цикла), префикс 0..u-1 уходит залпом. Alchemy-фейл юнита НЕ стопит журнал остальных — их raw уже
    в секвенсере (SEND-3 uncertain семантика: учтён sent, nonce инвалидируется вызывающим).
    Возвращает (sent, err_results, send_failed): sent=[(unit, hash)] как у старого цикла."""
    acct = account_for(w3, cfg.wallet_key)
    _t0 = time.perf_counter()
    _base_fee = _dyn_tip = None
    signed_units = []                                     # (unit, raw_bytes, hash_hex)
    err_results = []
    send_failed = False
    try:                                                  # тонкий кошелёк (25.07): владелец не доливает
        _bal_wei = int(w3.eth.get_balance(acct.address))  # ETH -> резерв limit×maxFee обязан влезать
    except Exception:
        _bal_wei = None                                   # не прочитали -> ведём себя как раньше
    _reserved = 0
    for u, unit in enumerate(units):
        try:
            if len(unit) == 1:                            # сборка txd бит-в-бит со старым циклом
                _prep = unit[0][2]
                _data = _prep["calldata"]; _gas = _prep.get("gas")
                _mf = _prep.get("max_fee"); _tp = _prep.get("tip")
            else:
                _preps = [p for _, _, p in unit]
                _data = encode_batch_liquidate(_preps)
                _gas = batch_gas_limit(_preps, cfg)
                _fee_src = next((p for p in _preps if p.get("max_fee")), None)
                _mf = _fee_src.get("max_fee") if _fee_src else None
                _tp = _fee_src.get("tip") if _fee_src else None
            t = {"to": cfg.liquidator_address, "data": _data, "nonce": base_nonce + u,
                 "from": acct.address, "chainId": rpc.chain_id}
            if _mf:                                       # SEND-2: fee из fb-хедера
                t["maxFeePerGas"] = _mf; t["maxPriorityFeePerGas"] = _tp
            else:                                         # fee-контекст один на ВЕСЬ залп
                if _base_fee is None:
                    _base_fee = int(w3.eth.get_block("latest").get("baseFeePerGas") or 0)
                    _dyn_tip = max(int(w3.eth.max_priority_fee), int(tip_wei))
                t["maxPriorityFeePerGas"] = _dyn_tip
                t["maxFeePerGas"] = _base_fee * 2 + _dyn_tip
            t["gas"] = _gas if _gas else int(w3.eth.estimate_gas(t) * 12 // 10)
            if _bal_wei is not None and t.get("maxFeePerGas"):      # тонкий кошелёк: см. clamp_fee_to_balance
                _mf2, _tp2, _ok = clamp_fee_to_balance(
                    int(t["gas"]), int(t["maxFeePerGas"]), int(t.get("maxPriorityFeePerGas") or 0),
                    _bal_wei - _reserved, min_tip_wei=int(tip_wei))
                if not _ok:
                    if log:                               # ХВОСТ дропаем, не continue: пропуск юнита
                        log.warning("volley: резерв %d×%d не влезает в баланс (тонкий кошелёк) at "
                                    "nonce %d — хвост дропнут (nonce-гэп исключён)",
                                    int(t["gas"]), int(t["maxFeePerGas"]), base_nonce + u)
                    for _mid, _b, _p in unit:
                        err_results.append({"market_id": _mid, "borrower": _b, "sent": False,
                                            "reason": "insufficient ETH for gas reserve",
                                            "net_usd": _p["net_usd"]})
                    send_failed = True
                    break
                if _mf2 != int(t["maxFeePerGas"]) and log:
                    log.warning("SEND fee-кламп по балансу: maxFee %d->%d, tip %d->%d (лимит %d НЕ тронут)",
                                int(t["maxFeePerGas"]), _mf2, int(t.get("maxPriorityFeePerGas") or 0), _tp2, int(t["gas"]))
                t["maxFeePerGas"], t["maxPriorityFeePerGas"] = _mf2, _tp2
                _reserved += int(t["gas"]) * _mf2                   # следующий юнит видит занятое
            signed = acct.sign_transaction(t)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            signed_units.append((unit, bytes(raw), signed.hash.hex()))
        except Exception as e:
            if log:
                log.warning("volley: sign FAILED at nonce %d (n=%d): %s — хвост дропнут (гэп исключён)",
                            base_nonce + u, len(unit), type(e).__name__)
            for _mid, _b, _p in unit:
                err_results.append({"market_id": _mid, "borrower": _b, "sent": False,
                                    "reason": f"sign error: {type(e).__name__}: {str(e)[:80]}",
                                    "net_usd": _p["net_usd"]})
            send_failed = True
            break
    _t_sign = time.perf_counter()
    for _, _raw, _h in signed_units:                      # ЗАЛП: все raw в секвенсер разом (гоночная нога)
        _raw_hex = "0x" + _raw.hex()
        for _u in bcast_urls:
            threading.Thread(target=_post_raw_tx, args=(_u, _raw_hex), daemon=True).start()
    _t_volley = time.perf_counter()
    sent = []
    n_unc = 0
    for u, (unit, _raw, h) in enumerate(signed_units):    # Alchemy-нога: журнал/дедуп, НЕ гонка
        try:
            w3.eth.send_raw_transaction(_raw)
        except Exception as e:
            n_unc += 1                                    # raw уже в секвенсере -> tx МОЖЕТ сесть: учтён sent
            if log:
                log.warning("volley: Alchemy-нога nonce=%d бросила (%s) — raw уже в секвенсере, "
                            "учтён sent/uncertain, журнал остальных продолжаем",
                            base_nonce + u, type(e).__name__)
        sent.append((unit, h))
        for _mid, _b, _p in unit:                         # дедуп: ВСЕ жертвы юнита помечены отправленными
            _RECENT_SENDS[(_mid, _b)] = time.monotonic()
        _inflight_add(1)                                  # ОДИН inflight на tx (не на item)
        if log:
            log.info("dispatch: sent nonce=%d n=%d %s tx=%s (volley)",
                     base_nonce + u, len(unit), "batch" if len(unit) > 1 else "single", h)
    if n_unc:
        send_failed = True
    if log and signed_units:
        log.info("DIAG send-volley n=%d sign_all=%.0fms volley_spawn=%.0fms alch_leg=%.0fms unc=%d",
                 len(signed_units), (_t_sign - _t0) * 1000.0, (_t_volley - _t_sign) * 1000.0,
                 (time.perf_counter() - _t_volley) * 1000.0, n_unc)
    return sent, err_results, send_failed


def dispatch_liquidations(rpc, cfg, prepared: list, log=None, max_inflight: int = 3,
                          send_fn=None, wait_receipts: bool = True, timeout: int = 120) -> list:
    """Send up to `max_inflight` already-prepared liquidations NON-BLOCKING with SEQUENTIAL nonces,
    then collect receipts. This is the parallel path: in a volatility cascade several positions are
    liquidatable at once, and sending them one-at-a-time (blocking on each receipt) would let a
    competitor take the rest.

    `prepared`: list of (market_id, borrower, prep) where prep is a prepare_liquidation() result
    with ok=True (already passed simulate + the net floor). Returns a result dict per attempted
    send: {market_id, borrower, sent, hash?/status?/gas_used?/net_usd/reason?}.

    Nonce safety: we fetch the base nonce ONCE and assign base+0, base+1, ... so concurrent sends
    don't race on get_transaction_count (which would hand them all the same nonce). Only candidates
    that passed simulate get a nonce (definitely-sending). If a send THROWS mid-batch we STOP (a
    missing nonce would strand every later tx behind the gap); the next block re-derives the base
    nonce and retries. Reverts do NOT break the chain — a reverted tx still consumes its nonce.
    `send_fn` is injectable for tests."""
    send = send_fn or send_tx
    results: list = []
    if not prepared:
        return results
    w3 = rpc._web3()
    bot = account_for(w3, cfg.wallet_key).address
    _seq = str(getattr(cfg, "sequencer_rpc", "") or "")
    _bcast = [_seq] if (_seq and _seq.lower() not in ("off", "none", "0")) else None
    with _SEND_LOCK:                                         # serialize nonce-fetch+send across senders (block loop + hot path)
        # дедуп двойной отправки (см. _RECENT_SENDS): выкидываем (mid,borrower), отправленные за
        # последние TTL сек, ПЕРЕД раздачей nonce — чтобы дубль не съел nonce и газ реверта.
        _now = time.monotonic()
        for _k in [_k for _k, _t in _RECENT_SENDS.items() if _now - _t > _RECENT_SEND_TTL]:
            _RECENT_SENDS.pop(_k, None)
        _deduped = []
        _seen_this_call = set()      # дубли ВНУТРИ одного prepared (два спавна в один батч), не только межвызовные
        for _row in prepared:
            _key = (_row[0], _row[1])
            if _key in _RECENT_SENDS or _key in _seen_this_call:
                results.append({"market_id": _row[0], "borrower": _row[1], "sent": False,
                                "reason": "dedup: recent send (двойной transmit-спавн)",
                                "net_usd": _row[2].get("net_usd", 0.0)})
                if log:
                    log.info("dispatch: dedup-skip %s/%s (уже отправлен <%.0fs назад или дубль в батче)",
                             _row[0][:10], _row[1][:10], _RECENT_SEND_TTL)
            else:
                _seen_this_call.add(_key)
                _deduped.append(_row)
        if not _deduped:
            return results
        base_nonce = _LOCAL_NONCE.get(bot)                   # P0-4: локальный nonce (0 RTT); RPC только на холодном
        if base_nonce is None:                               # кэше/после инвалидации (см. инварианты у _LOCAL_NONCE)
            base_nonce = _pending_nonce(w3, bot)
        tip = int(cfg.tip_gwei * 1e9)
        # ЮНИТЫ: батч ON и >1 жертвы -> группы по batch_max_items (сорт по net desc, ПОСЛЕ дедупа —
        # иначе fee-источник может быть выкинут); хвост = следующими tx в пределах max_inflight. Иначе
        # каждая жертва = свой юнит (путь бит-в-бит старый). Юнит из 1 item шлётся ОДИНОЧНЫМ вызовом.
        if getattr(cfg, "batch_liquidate", False) and len(_deduped) > 1:
            _sorted = sorted(_deduped, key=lambda r: r[2].get("net_usd", 0.0), reverse=True)
            _bmax = max(1, int(getattr(cfg, "batch_max_items", 5)))
            units = [_sorted[i:i + _bmax] for i in range(0, len(_sorted), _bmax)][:max_inflight]
        else:
            units = [[row] for row in _deduped[:max_inflight]]

        sent = []                                             # (unit, hash)
        _send_failed = False
        # PARALLEL_BROADCAST: каскадный залп — все юниты в секвенсер разом (юнит u+1 не ждёт
        # Alchemy-RTT юнита u). Только >1 юнита и при живой SEND-3 ноге; OFF / 1 юнит /
        # инжект-send_fn -> старый цикл ниже бит-в-бит.
        _par = (bool(getattr(cfg, "parallel_broadcast", False)) and send_fn is None
                and _bcast and len(units) > 1)
        if _par:
            sent, _verr, _send_failed = _send_units_volley(rpc, w3, cfg, units, base_nonce, tip, _bcast, log)
            results.extend(_verr)
            if _send_failed:
                _LOCAL_NONCE.pop(bot, None)
            units = []                                        # старый цикл ниже не выполняется
        for u, unit in enumerate(units):
            try:
                if len(unit) == 1:
                    _prep = unit[0][2]
                    _data = _prep["calldata"]; _gas = _prep.get("gas")
                    _mf = _prep.get("max_fee"); _tp = _prep.get("tip")
                else:                                         # батч-tx
                    _preps = [p for _, _, p in unit]
                    _data = encode_batch_liquidate(_preps)
                    # 63/64-мина C3c: голодный хвост молча морится -> запас газа обязателен (×1.25)
                    _gas = batch_gas_limit(_preps, cfg)
                    _fee_src = next((p for p in _preps if p.get("max_fee")), None)   # у блок-лупа max_fee нет
                    _mf = _fee_src.get("max_fee") if _fee_src else None
                    _tp = _fee_src.get("tip") if _fee_src else None
                _txd = {"to": cfg.liquidator_address, "data": _data, "nonce": base_nonce + u}
                if _gas:
                    _txd["gas"] = _gas
                if _mf:                                       # SEND-2: fee из fb-хедера; иначе send_tx доберёт сам
                    _txd["maxFeePerGas"] = _mf; _txd["maxPriorityFeePerGas"] = _tp
                if send_fn is None:                           # SEND-3 только через штатный send_tx
                    res = send(rpc, cfg.wallet_key, _txd, wait=False, min_tip_wei=tip, broadcast_raw_urls=_bcast)
                else:
                    res = send(rpc, cfg.wallet_key, _txd, wait=False, min_tip_wei=tip)
                sent.append((unit, res["hash"]))
                for _mid, _b, _p in unit:                     # дедуп: ВСЕ жертвы юнита помечены отправленными
                    _RECENT_SENDS[(_mid, _b)] = time.monotonic()
                _inflight_add(1)                              # ОДИН inflight на tx (не на item)
                if log:
                    log.info("dispatch: sent nonce=%d n=%d %s tx=%s",
                             base_nonce + u, len(unit), "batch" if len(unit) > 1 else "single", res["hash"])
                if res.get("uncertain"):     # SEND-3 race: raw в секвенсере -> tx МОЖЕТ сесть; стоп хвоста
                    if log:
                        log.warning("dispatch: SEND-3 uncertain nonce=%d — Alchemy бросил, raw в секвенсере; "
                                    "учтён как sent (дедуп+receipt), стоп + инвалидация nonce", base_nonce + u)
                    _send_failed = True
                    _LOCAL_NONCE.pop(bot, None)
                    break
            except Exception as e:
                if log:
                    log.warning("dispatch: send FAILED at nonce %d (n=%d): %s — stopping to avoid gap",
                                base_nonce + u, len(unit), type(e).__name__)
                for _mid, _b, _p in unit:
                    results.append({"market_id": _mid, "borrower": _b, "sent": False,
                                    "reason": f"send error: {type(e).__name__}: {str(e)[:80]}",
                                    "net_usd": _p["net_usd"]})
                _send_failed = True
                _LOCAL_NONCE.pop(bot, None)
                break
        if not _send_failed:
            _LOCAL_NONCE[bot] = base_nonce + len(sent)
    if sent:                                                 # фоновая сверка nonce с RPC (вне лока)
        threading.Thread(target=_reconcile_nonce, args=(w3, bot), daemon=True).start()

    if not wait_receipts:
        _inflight_add(-len(sent))    # -1 на tx (юнит); receipts не отслеживаются -> не копим фантомный inflight
        for unit, h in sent:
            for mid, borrower, prep in unit:
                results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                                "status": None, "gas_used": None, "net_usd": prep["net_usd"]})
        return results

    for unit, h in sent:
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
            status = rcpt.get("status")
            _blk = rcpt.get("blockNumber"); _txi = rcpt.get("transactionIndex")
            if len(unit) == 1:                                # --- одиночный путь: бит-в-бит старый ---
                mid, borrower, prep = unit[0]
                net_usd, gas_usd = realized_econ(prep["profit_usd"], int(rcpt.get("gasUsed") or 0),
                                                 int(rcpt.get("effectiveGasPrice") or 0), status, cfg.eth_price_usd)
                row = {"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                       "status": status, "gas_used": rcpt.get("gasUsed"),
                       "net_usd": net_usd, "gas_usd": gas_usd, "tx_index": _txi}
                if status == 1 and log:
                    log.info("submit-xray: WIN idx=%s block=%s tx=%s", _txi, _blk, h)
                if status != 1:
                    reason = _revert_reason(rpc, h, _blk)
                    row["revert_reason"] = reason
                    if log:
                        log.warning("liquidate revert %s/%s reason=%s tx=%s", mid[:10], borrower[:10], reason, h)
                    _submit_xray(rpc, borrower, _blk, _txi, log, cfg=cfg, our_debt_usd=prep.get("debt_usd"))
                _fl_record(prep.get("fast"), status, getattr(cfg, "fast_lane_max_consec_reverts", 3), log)
                results.append(row)
            else:                                             # --- батч: per-item ТОЛЬКО из событий ---
                gas_total = int(rcpt.get("gasUsed") or 0) * int(rcpt.get("effectiveGasPrice") or 0) * cfg.eth_price_usd / 1e18
                if status != 1:                               # top-level revert (OOG/onlyOwner) -> ВСЕ провалились
                    reason = _revert_reason(rpc, h, _blk)
                    if log:
                        log.warning("batch revert (top-level) n=%d reason=%s tx=%s", len(unit), reason, h)
                    for j, (mid, borrower, prep) in enumerate(unit):
                        _submit_xray(rpc, borrower, _blk, _txi, log, cfg=cfg, our_debt_usd=prep.get("debt_usd"))
                        results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h, "status": 0,
                                        "gas_used": rcpt.get("gasUsed") if j == 0 else 0,     # газ ОДНОЙ строкой (сумма/день точна)
                                        "net_usd": (-gas_total if j == 0 else 0.0),
                                        "gas_usd": (gas_total if j == 0 else 0.0),
                                        "revert_reason": reason, "tx_index": _txi})
                    _fl_record(all(p.get("fast") for _, _, p in unit), 0,
                               getattr(cfg, "fast_lane_max_consec_reverts", 3), log)
                    _batch_record(0, len(unit), h, log)
                else:                                         # status==1: разбор по Liquidated/BatchItemFailed
                    wins, _fails = parse_batch_receipt(rcpt, cfg.liquidator_address)
                    n_win = 0
                    for j, (mid, borrower, prep) in enumerate(unit):
                        _key = (str(borrower).lower(), str(prep["mp"]["loanToken"]).lower())
                        _g = gas_total if j == 0 else 0.0     # газ батча книжим на ПЕРВУЮ строку
                        _gu = rcpt.get("gasUsed") if j == 0 else 0
                        if _key in wins:                      # Liquidated(borrower,loan,profit) = взято
                            n_win += 1
                            profit_usd_i = (wins[_key] * prep["debt_usd"] / prep["debt_assets"]
                                            if prep.get("debt_assets") else 0.0)   # арифметика _net_gate
                            results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                                            "status": 1, "gas_used": _gu, "net_usd": profit_usd_i - _g,
                                            "gas_usd": _g, "tx_index": _txi})
                        else:                                 # BatchItemFailed или просто нет Liquidated
                            _submit_xray(rpc, borrower, _blk, _txi, log, cfg=cfg, our_debt_usd=prep.get("debt_usd"))
                            results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                                            "status": 0, "gas_used": _gu, "net_usd": -_g, "gas_usd": _g,
                                            "tx_index": _txi, "revert_reason": "batch item failed (no Liquidated)"})
                    # весь батч провалился (nOk=0) = как реверт для fast-lane breaker + счётчик пустых батчей
                    _fl_record(all(p.get("fast") for _, _, p in unit), 1 if n_win > 0 else 0,
                               getattr(cfg, "fast_lane_max_consec_reverts", 3), log)
                    _batch_record(n_win, len(unit), h, log)
                    if log:
                        log.info("batch receipt tx=%s n=%d win=%d block=%s idx=%s", h, len(unit), n_win, _blk, _txi)
        except Exception as e:
            # outcome unknown -> conservatively book estimated cost as a LOSS, never a phantom profit
            # HIGH-фикс: tx могла быть ДРОПНУТА из мемпула (не замайнена за timeout) -> инвалидация nonce
            # (следующий dispatch пере-читает RPC и закрывает возможную дыру). Timeout-ветка: суммарный
            # est-cost ОДИН раз (на первую строку юнита), остальные строки нулевые -> сумма/день точна.
            with _SEND_LOCK:
                _LOCAL_NONCE.pop(bot, None)
            for j, (mid, borrower, prep) in enumerate(unit):
                _first = (j == 0)
                results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                                "status": None, "gas_used": None,
                                "net_usd": (-prep["cost_usd"] if _first else 0.0),
                                "gas_usd": (prep["cost_usd"] if _first else 0.0),
                                "reason": f"receipt timeout: {type(e).__name__}"})
        finally:
            _inflight_add(-1)        # -1 на tx (юнит): receipt получен либо timeout
    return results
