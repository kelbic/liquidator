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


# #67: ТЁПЛАЯ сессия фолбэка + ХЕДЖ.
#
# ЗАМЕР 11.08 (разбор гонки 14:51 и трассы окна): боевая латентность ready->провод 2.129с, из
# них ~2.093с (98%) съел ОДИН вызов — этот. Alchemy отдал 429, и прежняя редакция строила НОВЫЙ
# Web3-провайдер прямо здесь: свежее TCP+TLS рукопожатие в самой горячей точке. Комментарий
# выше это честно признавал («тёплую сессию не держим») — держим теперь.
#
# Хедж вместо «сначала основная, потом запасная»: последовательная схема платит полный таймаут
# основной ноды ПЕРЕД тем, как начать вторую попытку, и именно так набежали две секунды. Обе
# ноги отвечают ОДНО И ТО ЖЕ значение из цепи, поэтому взять первый успешный ответ безопасно.
_NONCE_FB_W3 = None
_NONCE_FB_W3_LOCK = threading.Lock()


def _nonce_fb_w3():
    """Переиспользуемый клиент запасной ноды. Строится ОДИН раз, живёт весь процесс."""
    global _NONCE_FB_W3
    with _NONCE_FB_W3_LOCK:
        if _NONCE_FB_W3 is None:
            from web3 import Web3   # локальный импорт: модуль обязан грузиться без web3
            _NONCE_FB_W3 = Web3(Web3.HTTPProvider(
                _NONCE_FALLBACK_RPC, request_kwargs={"timeout": 4}))
        return _NONCE_FB_W3


def _pending_nonce(w3, addr: str) -> int:
    """Nonce с ТЁПЛОЙ запасной сессией. БЕЗ тредов.

    #67 дал 2.09с из 2.13с боевой латентности: Alchemy отвечал 429, и прежняя редакция строила
    НОВЫЙ Web3-провайдер прямо здесь — свежее TCP+TLS рукопожатие в самой горячей точке.
    Тёплая переиспользуемая сессия убирает именно это.

    ОТКАТ ХЕДЖА (11.08 15:41, боевой инцидент). Первая редакция этой правки пускала обе ноги
    ПАРАЛЛЕЛЬНО, по треду на каждую. Функция стоит на горячем пути, а при зависшей ноде тред
    живёт до её таймаута — треды копились и пробили cgroup-кап юнита:
    `RuntimeError: can't start new thread` шесть раз, падение hot_loop, self-heal перезапуск.
    Скорость, купленная тредами на горячем пути, стоит дороже, чем экономит: отказ пришёлся бы
    на каскад — то есть ровно на деньги. Возвращаемся к последовательной схеме, сохраняя
    тёплую сессию (это и был главный выигрыш).
    """
    try:
        return int(w3.eth.get_transaction_count(addr, "pending"))
    except Exception:
        n = int(_nonce_fb_w3().eth.get_transaction_count(addr, "pending"))
        log.warning("nonce via fallback %s (основная нода бросила, сессия тёплая)",
                    _NONCE_FALLBACK_RPC)
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

_SEQ_SESSIONS: dict = {}        # ключ канала -> requests.Session (тёплый keep-alive)
_SEQ_LOCK = threading.Lock()

# ---- ВТОРОЙ EGRESS-IP (20.08). Канал рассылки = ПАРА (url, source_ip), а не просто url.
#
# ЗАЧЕМ. Замер: pacer_dry 8758 проб/сутки при transport_reject=0 — то есть нас связывает НАШ
# ЖЕ пейсер на 110/с (замеренная квота приёма секвенсера НА ОДИН IP), а не отказы секвенсера.
# Флот удвоен до 220 ног, значит давление вырастет вдвое в тот же потолок. Второй адрес на том
# же интерфейсе net0 Шарлотт даёт ВТОРУЮ КОРЗИНУ лимита.
#
# Почему именно второй IP, а не Вена/прокси: замер с машины — connect к mainnet-sequencer
# 0.0157/0.0146/0.0158с с нового адреса против 0.0158/0.0149с с основного, то есть канал
# БЕСПЛАТЕН ПО ВРЕМЕНИ. Вена (RTT 107мс) и proxy6 закрыты именно замером RTT, а не вкусом.
#
# ГИПОТЕЗА, НА КОТОРОЙ ВСЁ ДЕРЖИТСЯ (named limit, приёмка боем): вторая корзина существует
# ТОЛЬКО если секвенсер лимитирует ПО IP. Это вывод из формы отказов (110/с на адрес), а не
# документированный контракт. Если он режет по ключу/подсети/аккаунту — второй канал не даст
# ничего, и это будет видно: pacer_dry обязан УПАСТЬ при том же боезапасе. Не упал => гипотеза
# неверна, ручку в пусто.
#
# ЦЕНА (named limit): по TLS-сессии и по keepalive-треду НА КАНАЛ. Число каналов задаётся
# КОНФИГОМ (len(ips) на первичный url + прочие url), не трафиком, поэтому потолок известен
# заранее и мал — но помнить про cgroup-кап стоит: тред-на-вызов мы уже однажды пробили.
# Плюс DNS резолвится отдельно на каждый пул (тот же хост, лишний резолв на старте).
VOLLEY_EGRESS_IPS = [x.strip() for x in
                     (os.environ.get("VOLLEY_EGRESS_IPS", "") or "").split(",") if x.strip()]


def _chan_key(url: str, ip: str = None) -> str:
    """Имя канала для сессий/вёдер/счётчиков. IP печатать МОЖНО — это не секрет (в отличие от
    ключа в пути URL, который режет фильтр журнала).

    ПУСТОЙ ip ДАЁТ РОВНО url — и это не косметика, а способ получить поведение HEAD бит-в-бит:
    при пустой ручке все ключи вёдер, счётчиков и сессий совпадают с прежними посимвольно, то
    есть ни одно ведро не делится, ни один счётчик не переименовывается."""
    return url if not ip else "%s|%s" % (url, ip)


def egress_bindable(ip: str) -> bool:
    """Можно ли ПРИВЯЗАТЬСЯ к адресу. Локальная операция: bind без connect, сети не касается.

    Ловит ошибку КОНФИГА (адрес снят с интерфейса / опечатка / чужой адрес) на старте, где она
    стоит одну строку в журнале. Ошибку ВРЕМЕНИ БОЯ (адрес убрали на живой машине) не ловит —
    её ловит spill: отказ канала уводит пробу на следующий, как и «over rate limit»."""
    import socket
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((ip, 0))
        return True
    except OSError:
        return False                # EADDRNOTAVAIL и родня
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def _make_session(url: str, ip: str = None):
    """Фабрика тёплой сессии канала. Единственное место, где рождается транспорт рассылки —
    и единственный шов, который подменяют стенды (сети в тестах быть не должно).

    ip=None -> сессия РОВНО как раньше (тот же адаптер, те же размеры пула).
    ip задан -> тот же адаптер, но PoolManager с source_address=(ip, 0): расширяем существующий
    транспорт, а не заводим второй."""
    if _requests is None:
        return None
    # Дефолтный пул urllib3 = 10 коннектов, block=False: сверх десятого одновременного
    # поста соединение создаётся ХОЛОДНЫМ и выбрасывается. Пик каскада: 8 тредов движка
    # + до 3 боевых _post_raw_tx + канарейка/keepalive > 10 — и холод падал бы ровно на
    # боевой броадкаст. 32 держит пик с запасом.
    s = _requests.Session()
    if ip:
        class _BoundAdapter(_requests.adapters.HTTPAdapter):
            """HTTPAdapter, чей PoolManager открывает сокеты с ЗАДАННОГО адреса."""

            def init_poolmanager(self, connections, maxsize, block=False, **kw):
                kw["source_address"] = (ip, 0)
                return super().init_poolmanager(connections, maxsize, block=block, **kw)

        _ad = _BoundAdapter(pool_connections=4, pool_maxsize=32)
    else:
        _ad = _requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32)
    s.mount("https://", _ad)
    s.mount("http://", _ad)
    return s


def _seq_keepalive(url: str, s, interval: float = 15.0):
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    while True:
        try:
            s.post(url, json=body, timeout=5)
        except Exception:
            pass                # разрыв лечится пулом на следующем post (одна холодная отправка)
        time.sleep(interval)


def _seq_session(url: str, ip: str = None):
    """Тёплая сессия КАНАЛА (url, ip). ip=None -> прежняя сессия по url, ключ прежний (см.
    _chan_key), поведение бит-в-бит."""
    if _requests is None:
        return None
    _k = _chan_key(url, ip)
    with _SEQ_LOCK:
        s = _SEQ_SESSIONS.get(_k)
        if s is None:
            s = _make_session(url, ip)
            if s is None:
                return None
            _SEQ_SESSIONS[_k] = s
            # Один keepalive-тред НА КАНАЛ (15с). Число каналов задаётся конфигом, не трафиком.
            threading.Thread(target=_seq_keepalive, args=(url, s), daemon=True).start()
        return s


_CHANNELS_CACHE: dict = {}         # tuple(urls) -> [(url, ip|None), ...]
_CHANNELS_LOCK = threading.Lock()


def broadcast_channels(urls: list) -> list:
    """[(url, ip|None), ...] в порядке обхода рассылки. РЕЗУЛЬТАТ КЭШИРУЕТСЯ.

    Кэш здесь не оптимизация, а требование горячего пути: функция зовётся на КАЖДУЮ пробу
    (8/с в окне), а проверка привязки — это syscall bind. Держать сисколл на денежном пути
    ради статической конфигурации незачем; список каналов не меняется без рестарта, ровно как
    и сама ручка. Сброс — очистка _CHANNELS_CACHE (стенды так и делают).

    Пустая VOLLEY_EGRESS_IPS -> [(u, None) для каждого url] = СЕГОДНЯШНЕЕ ПОВЕДЕНИЕ: ни одной
    привязки, ни одного лишнего пула, ключи каналов совпадают с url.

    Ручка задана -> ПЕРВИЧНЫЙ url размножается по адресам (это и есть вторая корзина лимита:
    тот же эндпоинт, другой источник), прочие url остаются одним непривязанным каналом каждый.
    Порядок: (url0, ip0) -> (url0, ip1) -> ... -> прочие url.

    Непривязываемый адрес выпадает ЗДЕСЬ и ГРОМКО (egress_bindable): канал, который не может
    открыть сокет, не имеет права молча съедать место в обходе."""
    _u = [u for u in (urls or []) if u]
    if not _u or not VOLLEY_EGRESS_IPS:
        return [(u, None) for u in _u]
    _ckey = tuple(_u)
    with _CHANNELS_LOCK:
        _hit = _CHANNELS_CACHE.get(_ckey)
    if _hit is not None:
        return list(_hit)
    _ok = []
    for ip in VOLLEY_EGRESS_IPS:
        if egress_bindable(ip):
            _ok.append(ip)
        else:
            log.error("EGRESS: адрес %s НЕ привязывается (снят с интерфейса?) — канал исключён "
                      "из рассылки. Отправка идёт остальными каналами, огонь не затронут", ip)
    if not _ok:
        log.error("EGRESS: ни один адрес из VOLLEY_EGRESS_IPS не привязывается — рассылка идёт "
                  "БЕЗ привязки (сегодняшнее поведение). Незнание не закрывает основной путь")
        _res = [(u, None) for u in _u]
    else:
        _res = [(_u[0], ip) for ip in _ok] + [(u, None) for u in _u[1:]]
        log.warning("EGRESS: каналов рассылки %d — %s (второй адрес = вторая корзина лимита "
                    "секвенсера; гипотеза «лимит по IP», приёмка — падение pacer_dry)",
                    len(_res), ", ".join(_chan_key(u, i) for u, i in _res))
    with _CHANNELS_LOCK:
        _CHANNELS_CACHE[_ckey] = list(_res)
    return list(_res)


def warm_broadcast(urls: list):
    """Прогреть keep-alive сессии КАНАЛОВ на старте (до первого выстрела/канарейки).

    Сессии обязаны быть тёплыми ЗАРАНЕЕ: путь замерен в 32.5мс на тред, и лишний холодный
    коннект (TCP+TLS ~2-3 RTT) в горячем окне недопустим. Ручку читаем здесь, а не у
    вызывателя, — call-site в hotpath не меняется, и при пустой ручке греется ровно то же,
    что грелось раньше."""
    for u, ip in broadcast_channels(list(urls or [])):
        _seq_session(u, ip)

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
# ПЕР-ПАРНЫЙ БРЕЙКЕР (20.08). Глобальный счётчик гасил fast-lane ЦЕЛИКОМ от трёх ревертов ОДНОЙ
# пары: 07.08 три проигранные гонки по $0.01-0.11 выключили путь, когда в том же блоке стояли
# цели на $30 890 и $16 489. Дефект пары (тонкий пул, битый роут, сломанный оракул) — свойство
# ПАРЫ, и гасить он обязан пару. Глобальный рубеж остаётся страховкой сверху: деградация
# каскада не должна требовать 3×числа пар ревертов, чтобы быть замеченной.
# Откат: FL_BREAKER_SCOPE=global возвращает прежнее поведение БИТ-В-БИТ (пер-парный слой мёртв,
# глобальный порог = тот же threshold вызывающего).
FL_BREAKER_SCOPE = (os.environ.get("FL_BREAKER_SCOPE", "pair") or "pair").strip().lower()
FL_BREAKER_GLOBAL_N = int(os.environ.get("FL_BREAKER_GLOBAL_N", "6"))
_FL_PAIR_REVERTS: dict = {}    # (coll,loan) -> серия ПОДРЯД не-race ревертов этой пары
_FL_PAIR_OFF: set = set()      # пары, для которых fast-lane погашен (до рестарта, как и глобально)


def _fl_pair_key(pair):
    """(coll, loan) -> нормализованный ключ либо None (пара неизвестна -> только глобальный слой)."""
    try:
        return (str(pair[0]).lower(), str(pair[1]).lower())
    except Exception:                                   # noqa: BLE001
        return None


def fl_global_disabled() -> bool:
    """ТОЛЬКО глобальный рубеж. Отдельная дверь для наблюдателей (стенды, отчёты): вопрос
    «выключен ли путь вообще» и вопрос «можно ли стрелять по этой паре» — разные, и мешать их
    в одной функции значит однажды ответить не на тот."""
    with _INFLIGHT_LOCK:
        return _FL_DISABLED


def fast_lane_disabled(pair=None) -> bool:
    """Можно ли идти fast-lane. True = НЕЛЬЗЯ.

    ПАРА ОБЯЗАТЕЛЬНА в pair-режиме: спрашивать «выключен ли путь вообще», не назвав пару,
    значит проверять не тот гард — погашенная пара при пустом кэше параметров не проверялась
    бы ВОВСЕ, и защита держалась бы на чужом предусловии (что кэш всегда полон), а не на себе.
    Незнание пары = закрытый гард. Боевой путь этим не задет: там, где fast-lane вообще
    применим, параметры рынка уже в кэше (иначе prepare_fast сам отказывает «params not
    cached»). Глобальный флаг спрашивают через `fl_global_disabled`."""
    k = _fl_pair_key(pair) if pair is not None else None
    with _INFLIGHT_LOCK:
        if _FL_DISABLED:
            return True
        if FL_BREAKER_SCOPE != "pair":
            return False
        if k is None:
            return True                    # пара не названа -> считаем путь закрытым
        return k in _FL_PAIR_OFF


# Ш16: ДЕНЬГИ ПРОШЛИ МИМО — проигранная гонка обязана СООБЩАТЬ О СЕБЕ.
#
# 11.08: за сутки мимо прошло 79 ликвидаций в 9 блоках, включая каскад на $52 320, и НИ ОДНОГО
# сообщения владельцу не ушло — потери находились вручную в журнале постфактум. `_submit_xray`
# уже отличает «нас обогнали» (race) от «позиция была здорова» (not-race), вердикт вычислялся и
# выбрасывался.
#
# ДВЕ ЗАЩИТЫ, обе из наших же канонов:
#  * ПОРОГ ПО ДЕНЬГАМ — мелочь не будит. Пол берём от нашего же порога огня, а не с потолка.
#  * ОКНО АНТИСПАМА — каскад из 44 ликвидаций не должен дать 44 сообщения. Первая потеря в окне
#    уходит сразу, остальные КОПЯТСЯ и приезжают итогом со следующей. Счёт не теряется: в
#    сообщении всегда видно, сколько гонок и на какую сумму накопилось.
#
# Транспорта здесь нет по устройству модуля: кладём one-shot сообщение, забирает вызывающий с
# алертером (тот же образец, что у брейкера).
_LOST_RACE_MSG = None
_LOST_RACE_TS = 0.0
_LOST_RACE_ACC = [0, 0.0]        # [гонок накоплено, сумма долга $]
LOST_RACE_ALERT_USD = float(os.environ.get("LOST_RACE_ALERT_USD", "500"))
LOST_RACE_WINDOW_SEC = float(os.environ.get("LOST_RACE_WINDOW_SEC", "300"))


def note_lost_race(borrower, debt_usd, winner, our_blocks_behind, win_tip_gwei=None,
                   our_tip_gwei=None, market=None, now=None):
    """Учесть проигранную гонку и, если пора, приготовить сообщение владельцу.

    Возвращает True, если сообщение положено (для теста и диагностики)."""
    global _LOST_RACE_MSG, _LOST_RACE_TS
    try:
        d = float(debt_usd or 0.0)
    except Exception:                                   # noqa: BLE001
        d = 0.0
    if d < LOST_RACE_ALERT_USD:
        return False                                    # ниже нашего же пола огня — не будим
    n = float(now if now is not None else time.time())
    with _INFLIGHT_LOCK:
        _LOST_RACE_ACC[0] += 1
        _LOST_RACE_ACC[1] += d
        if (n - _LOST_RACE_TS) < LOST_RACE_WINDOW_SEC and _LOST_RACE_TS > 0:
            return False                                # копим, сообщение придёт следующим
        cnt, total = _LOST_RACE_ACC[0], _LOST_RACE_ACC[1]
        _LOST_RACE_ACC[0], _LOST_RACE_ACC[1] = 0, 0.0
        _LOST_RACE_TS = n
        tips = ""
        if win_tip_gwei is not None and our_tip_gwei is not None:
            tips = " | tip победителя %.4f против нашего %.4f gwei" % (
                float(win_tip_gwei), float(our_tip_gwei))
        _LOST_RACE_MSG = (
            "💸 ДЕНЬГИ МИМО: проиграна гонка, долг $%.0f, отстали блоков %s\n"
            "жертва %s%s\nпобедитель %s%s\n"
            "за окно накоплено гонок: %d на $%.0f" % (
                d, our_blocks_behind, str(borrower)[:12],
                (" рынок %s" % str(market)[:10]) if market else "",
                str(winner)[:12], tips, cnt, total))
    return True


def pop_lost_race() -> str | None:
    """One-shot: сообщение о проигранной гонке. Зовёт caller с алертером."""
    global _LOST_RACE_MSG
    with _INFLIGHT_LOCK:
        m = _LOST_RACE_MSG
        _LOST_RACE_MSG = None
        return m


def pop_fast_lane_trip() -> str | None:
    """One-shot: сообщение о срабатывании breaker'а для TG, затем очистка. Зовёт caller с alerter'ом."""
    global _FL_TRIP_MSG
    with _INFLIGHT_LOCK:
        m = _FL_TRIP_MSG
        _FL_TRIP_MSG = None
        return m


def _fl_record(is_fast: bool, status, threshold: int, log=None, xray=None, pair=None):
    """Учёт исхода ЗАМАЙНЕННОЙ fast-lane tx для breaker'а. status==1 -> сброс серии; status!=1 (реверт)
    -> инкремент; при достижении порога -> disable + one-shot TG-msg. status is None (timeout, исход
    неизвестен) НЕ считаем. Idempotent после disable (флаг уже стоит).

    `xray` — вердикт `_submit_xray`. ПРОИГРАННУЮ ГОНКУ («race»: жертву забрал конкурент, его tx
    видна в логах Morpho) не считаем вовсе: замысел брейкера с самого начала был «устойчивый
    шторм, не разовый lost-race», но отличить одно от другого код не умел, хотя вердикт уже
    вычислялся рядом и выбрасывался. Цена ошибки была измерена 07.08 в 17:00: три подряд
    проигранные гонки в каскаде (реверты по $0.01-0.11) выключили fast-lane ровно в тот момент,
    когда в ТОМ ЖЕ блоке оставались цели на $30890 и $16489. Реверты в каскаде — норма для
    проигравшего, а не признак поломки.

    Серию при этом НЕ сбрасываем: смесь проигранных гонок и настоящих дефектов обязана
    накапливаться, иначе брейкер перестанет срабатывать вообще. `not-race`/`unknown` считаем —
    консервативно, это и есть тот шторм, ради которого брейкер сделан.
    """
    global _FL_CONSEC_REVERTS, _FL_DISABLED, _FL_TRIP_MSG
    if not is_fast or status is None:
        return
    _pk = _fl_pair_key(pair) if pair is not None else None
    if status != 1 and xray in ("race", "self-win"):
        # #44: self-win — жертву взял НАШ ЖЕ тракт (проба доски/движка/ноги). Для брейкера это
        # тот же класс «не шторм», но называем вещи своими именами: иначе собственная победа
        # копится как проигранная гонка.
        if log:
            log.info("II.6 breaker: реверт = %s — серия не растёт (%d/%d)",
                     "НАША победа (взял свой же тракт)" if xray == "self-win"
                     else "проигранная гонка (xray нашёл победителя)",
                     _FL_CONSEC_REVERTS, threshold)
        return
    _pair_tripped = None
    with _INFLIGHT_LOCK:
        if status == 1:
            # УСПЕХ СБРАСЫВАЕТ ОБА СЛОЯ: серия — это «подряд», и удачный выстрел её обрывает.
            # Погашенную пару успех НЕ воскрешает (как и глобальный флаг): шторм разбирают, а
            # не переармливают автоматически.
            _FL_CONSEC_REVERTS = 0
            if _pk is not None:
                _FL_PAIR_REVERTS.pop(_pk, None)
            return
        _FL_CONSEC_REVERTS += 1
        # ПЕР-ПАРНЫЙ СЛОЙ: свой счётчик, ТОТ ЖЕ порог, гасит ТОЛЬКО свою пару.
        if FL_BREAKER_SCOPE == "pair" and _pk is not None:
            _n = _FL_PAIR_REVERTS.get(_pk, 0) + 1
            _FL_PAIR_REVERTS[_pk] = _n
            if _n >= threshold and _pk not in _FL_PAIR_OFF:
                _FL_PAIR_OFF.add(_pk)
                _pair_tripped = (_pk, _n)
        # ГЛОБАЛЬНЫЙ РУБЕЖ: в pair-режиме он выше (FL_BREAKER_GLOBAL_N) и ловит ДЕГРАДАЦИЮ
        # КАСКАДА, размазанную по парам. НО ТОЛЬКО ЕСЛИ ПАРА ИЗВЕСТНА: реверт без пары (батч,
        # старый вызыватель) пер-парным слоем не покрыт вовсе, и поднимать для него планку
        # значило бы ОСЛАБИТЬ защиту — незнание обязано оставлять прежний, более строгий порог.
        _gth = FL_BREAKER_GLOBAL_N if (FL_BREAKER_SCOPE == "pair" and _pk is not None) else threshold
        if _FL_CONSEC_REVERTS >= _gth and not _FL_DISABLED:
            _FL_DISABLED = True
            _FL_TRIP_MSG = ("\U0001F6D1 CIRCUIT BREAKER: fast-lane ОТКЛЮЧЁН после %d подряд ревертов "
                            "-> фолбэк в sim-путь (бот жив). Разобрать причину, вернуть рестартом."
                            % _FL_CONSEC_REVERTS)
            if log:
                log.error("II.6 breaker: fast-lane DISABLED after %d consecutive reverts -> sim fallback",
                          _FL_CONSEC_REVERTS)
    if _pair_tripped:
        if log:
            log.error("II.6 breaker: fast-lane выключен ДЛЯ ПАРЫ %s->%s (%d ревертов подряд); "
                      "остальные пары работают", _pair_tripped[0][0][:10],
                      _pair_tripped[0][1][:10], _pair_tripped[1])
        # ЗАЩЁЛКА ДО РЕСТАРТА БЕЗ КАНАЛА ТРЕВОГИ = состояние, о котором узнать НЕОТКУДА.
        # Кладём в тот же one-shot, что и глобальный трип (его забирает горячий путь и уводит
        # в TG). Глобальное сообщение важнее и перезаписывает; пер-парное занимает пустой слот.
        with _INFLIGHT_LOCK:
            if _FL_TRIP_MSG is None:
                _FL_TRIP_MSG = (
                    "\U0001F6D1 CIRCUIT BREAKER (ПАРА): fast-lane выключен для %s->%s после %d "
                    "ревертов подряд. Остальные пары работают. Снятие — ТОЛЬКО рестарт: "
                    "разобрать причину (пул/роут/оракул этой пары)."
                    % (_pair_tripped[0][0][:12], _pair_tripped[0][1][:12], _pair_tripped[1]))


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


# #91 (10.08) — ЦЕНА ПРИОРИТЕТА, а не плотность, держала нас вне призового окна.
# Блок Base не плоский FCFS: это склейка суб-блоков (флэшблоки 200мс), и ВНУТРИ каждого порядок
# идёт по УБЫВАНИЮ tip. Обновление оракула приходит на ПОЛУ рынка (0.001 gwei) и потому лежит
# глубоко в хвосте своего суб-блока; все призы — там же. Замер блока 49666338: позиции 2164..2191
# (включая транзит 2170 и взятые призы 2176/2179/2181/2182/2187) — ВСЕ на 0.001 gwei, ни одной
# точки роста на 27 подряд позициях.
# Значит плата ВЫШЕ пола не помогает, а ДИСКВАЛИФИЦИРУЕТ: сортировка выносит пробу в голову
# суб-блока, на сотни позиций РАНЬШЕ транзита, где жертва ещё здорова (наши «position is
# healthy»). Мы платили cfg.tip_gwei = 0.05 (50 полов), а в каскаде dynamic_tip_gwei поднимал
# до 0.5 — максимум платы ровно там, где призы. В призовом окне [T+1, T+20] у победителей было
# 8/8/3/1 tx, у нас 0.
# ПОЛИТИКА ДВУХРЕЖИМНАЯ, и режимы уже разведены по путям: ПРОБЫ флота (здесь) идут на полу и
# садятся в хвост рядом с транзитом; БОЕВОЙ dispatch с главного кошелька остаётся на высоком
# tip — в суб-блоках зачистки правило переворачивается (приз на idx 2860 взяла tx с 24.65 gwei).
# Прежний канон «tip места не покупает» верен МЕЖДУ суб-блоками и ложен ВНУТРИ одного.
_PROBE_TIP_WEI = int(os.environ.get("SPEC_FIRE_PROBE_TIP_WEI", "1000000"))   # 0.001 gwei = пол

# Ш24: ТИРЫ tip ПО ФЛОТУ вместо одной константы.
#
# ЗАМЕР 11.08, 5407 блоков за 3 часа: доминирующий класс блока (он же tip транзита и всего
# призового окна) МЕНЯЕТСЯ между соседними блоками в 33% случаев, медианная длина серии одного
# значения — ОДИН блок, то есть две секунды. Лестницы подписываются за секунды-минуты до огня,
# поэтому «подписать по свежему замеру» не успеет никогда: величина живёт короче подписи.
#
# Частоты за те же 3 часа: 1 000 000 — 63.8%, 1 100 000 — 13.0%, 1 775 000 — 7.0%.
# Три уровня покрывают 83.8% блоков, четыре — 87.1%.
#
# ЧЕТВЁРТЫЙ, каскадный (3 000 000), взят НЕ из частот: в штиле он почти не встречается. Это
# единственное наблюдение из каскада 11.08 06:38 (блоки 49820471/49820472, 30 ликвидаций) —
# одна точка, а не статистика. Стоит как СТРАХОВКА: каскад поднимает загрузку, и именно там
# деньги, а вес этого режима по спокойным данным оценить нельзя.
#
# ПОЧЕМУ РОВНО КЛАСС, А НЕ ВЫШЕ: внутри суб-блока порядок по УБЫВАНИЮ tip. Плата выше транзита
# кладёт нас ПЕРЕД ним, где цена ещё старая (дефект #91, оплачен ревертами «position is
# healthy»); ниже — в хвост за всем классом. Замер: с 1 000 000 мы ниже класса в 56% блоков,
# с 3 000 000 были бы ВЫШЕ класса в 99.9% — то есть единая тройка не лечит, а переворачивает
# промах и делает его почти стопроцентным.
_PROBE_TIP_TIERS = tuple(int(x) for x in os.environ.get(
    "SPEC_FIRE_PROBE_TIP_TIERS", "1000000,1100000,1775000,3000000").split(",") if x.strip())

# Ш24b/#104: ЗЕРКАЛО ТРАНЗИТА (доказано боем 49978436, 14.08 22:23).
#
# Статические тиры выше калиброваны «классом БЛОКА» (feeHistory), но приз живёт в другом
# распределении: победитель transit+1/+2 несёт tip = TIP ТРАНЗИТА ДО WEI (10/10 денежных
# блоков), и в живом бою из наших 62 проб рабочими были лишь 21 — те, что случайно совпали
# с транзитом (0.0010); всё выше сортируется ПЕРЕД ним, в старую цену. Класс блока живёт
# 2 секунды (потому тиры и были портфелем), но tip ТРАНЗИТА — свойство ОПЕРАТОРА фида и
# стабилен (0.0010-0.0012 неделями). Его теперь меряет индекс (#110: f["transit"][..]["tip"])
# и кормит сюда через set_on_transit (арм ЯВНЫЙ в main.py, канон notify_fn).
#
# Зеркало = мода последних K транзитов при доле >=MIN_SHARE; тогда 3 ноги из 4 несут моду,
# 4-я — вторую моду (хедж). Незнание/разброд/протухание -> статический портфель БИТ-В-БИТ
# (незнание закрывает гард). Границы здравого смысла как у _DOM: вне [0.5e6, 2e7] отбрасываем.
# Откат SPEC_FIRE_PROBE_TIP_MIRROR=0. Ограничение v1 (названо): мода ГЛОБАЛЬНАЯ по всем
# агрегаторам цепи, не пер-фидная — гард доли защищает от разброда операторов.
# Ш24b v2 (19.08, вскрытие потери 50191667 — $3905): МОДА СТАЛА ПЕР-АГГРЕГАТОРНОЙ.
#
# ЧТО СЛОМАЛОСЬ В v1. Мода считалась ГЛОБАЛЬНО по всем агрегаторам цепи. Транзиты cbADA идут с
# полом 1.0-1.2 mgwei и их МНОГО; транзиты 0x1e0b2c38/0x0e3dc8a6 в режиме затора несут 31-33
# mgwei и их МАЛО. Глобальная выборка топится полами: доля 50% не собирается ЛИБО собирается на
# поле — и в блоке 50191666 наши 22 пробы стояли с tip 1.0-3.0 против транзита 31.2, tip-match=0.
# Приз ушёл целиком. Кольцо ПЕР-АГГ разводит эти два распределения, и каждое даёт свой слот.
#
# ВРЕМЯЗАВИСИМОСТЬ. Высокий tip — не свойство оператора, а РЕЖИМ ЗАТОРА: 19.08 в 20:51 ОБА
# агрегатора несли 31-33 mgwei три судимых блока подряд, а в моде того же оператора за неделю
# стоит 1.0-1.15. Поэтому кольцо по СВЕЖЕСТИ (K последних + TTL) адаптивно само — если его
# кормить. Кормить и чинится: разводка main.py передавала только tip, а "agg" в транзит-дикте
# индекса лежал с самого начала (chain_index.py:645,1060).
#
# ГРАНИЦЫ ВХОДА. Верх поднят 2e7 -> 3.5e7. Прежние 2e7 отбрасывали ВЕСЬ режим 21.2-34.6 mgwei
# ПРЯМО НА ВХОДЕ — то есть первичная слепота была не в статистике, а в фильтре. Граница здесь
# не «здравый смысл», а ЭКОНОМИЧЕСКИЙ КЭП: это максимальная цена пробы, которую чужая мода
# вправе нам навязать. 3.5e7 — чуть выше максимума, НАБЛЮДЁННОГО за 7 суток (34 597 197 wei);
# 5e7 (первая редакция) — это уже 10x базы и 8.2x дневного бакета проб $150 на одно окно, то
# есть класс «в такой гонке не участвуем», а не «платим сколько скажут».
PROBE_TIP_MIRROR = os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR", "1").lower() not in ("0", "false", "no")
_MIRROR_K = int(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_K", "12"))
# TTL 15 МИНУТ, а не 6 часов (замер линзы). Одна пачка затора из 4 сэмплов при TTL=6ч диктует
# повышенный тариф четверти флота на 99.8% следующих шести часов, тогда как польза зеркала —
# только ПОВТОРНЫЕ заторы в ближайшие минуты. Цена определённая, польза условная: короткий TTL
# режет хвост цены, не трогая полезную часть (в самом заторе сэмплы идут блок за блоком).
_MIRROR_TTL_SEC = float(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_TTL_SEC", "900"))
_MIRROR_MIN_SHARE = float(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_MIN_SHARE", "0.5"))
_MIRROR_MIN_N = 4
_MIRROR_TIP_MIN = int(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_MIN", "500000"))
_MIRROR_TIP_MAX = int(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_MAX", "35000000"))
_MIRROR_SLOTS = 4                        # слотов портфеля = длине статического портфеля
# СКЛЕЙКА: два tip в пределах ±10% — ОДИН класс, а не два кандидата в слоты. 10.0 и 10.1 mgwei
# у одного оператора это дрейф, а не два режима; 1.0/1.1/1.15/1.2 — один пол (склейка цепная,
# по возрастанию: каждый следующий в пределах шага от предыдущего). Без склейки «четыре
# различные моды» набираются из соседних значений одного и того же класса.
_MIRROR_GLUE_STEP = 1.0 + float(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_GLUE_PCT", "10")) / 100.0
# ПОЛ — ПРИЗОВОЙ КЛАСС, И ЕМУ ГАРАНТИРОВАН СЛОТ. Замер вскрытия 49834406: 20 призовых tx все
# на tip РОВНО 1 000 000. Отбор по цене (v2 ред.1: sorted(modes)[:4]) выбрасывал пол из
# портфеля, как только набиралось 4 различные дорогие моды — то есть ровно в штиле, где пол и
# выигрывает. Гарантия ниже стоит ОТДЕЛЬНОЙ строкой кода, а не подразумевается комментарием.
_MIRROR_FLOOR_MAX = int(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_FLOOR_MAX", "1200000"))
# ГИСТЕРЕЗИС ВХОДА: новый класс занимает слот только когда он ПОДТВЕРЖДЁН — 8 свежих сэмплов
# либо возраст первого >= 60с. Пачка из 4 транзитов затора не имеет права дёрнуть пересборку
# 110 лестниц: приз живёт в transit+1 (~2с), а сходимость аудита — 22 прохода (~24с в окне).
# То есть на ПЕРВОЙ пачке зеркало физически не успевает, и платить за неё пересборкой нечем.
_MIRROR_ENTRY_N = int(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_ENTRY_N", "8"))
_MIRROR_ENTRY_AGE_SEC = float(os.environ.get("SPEC_FIRE_PROBE_TIP_MIRROR_ENTRY_AGE_SEC", "60"))
# ЗАМЕР 7 СУТОК ЖУРНАЛА (analysis/transit_tips_7d.txt, 129 транзитов, 5.7 суток) — числа,
# ради которых поднят верх и введена склейка:
#   0x0e3dc8a6 (74 сэмпла): мода 1.0-1.15 mgwei, режимные выбросы 21.2 и 33.4;
#   0x9491aedf (21): мода 1.0-1.2, выброс 10.1;   0x1e0b2c38 (21): 10.0-10.1, 19.08 — 31.2;
#   0x92a7c3a5 (7) / 0x5bf848b4 (4) / 0xf0f5eebe (2): полы 1.0-2.5, выбросы до 10.5;
#   максимум наблюдённый — 34 597 197 wei. Приёмка потока: analysis/mirror_v2_replay.py.
_TRANSIT_TIPS: dict = {}                 # agg -> [(ts, tip_wei)]: кольцо K НА КАЖДЫЙ агрегатор
_TRANSIT_TIPS_LOCK = threading.Lock()
_MIRROR_UNKNOWN = "unknown"              # agg не назван -> СВОЁ кольцо, не подмешивать к чужим
_MIRROR_LAST_SLOTS: tuple = ()           # прошлый состав портфеля (только для строки журнала)
_MIRROR_APPLIED: tuple = ()              # состав, ДЕЙСТВУЮЩИЙ сейчас (пустой = статика)
_MIRROR_APPLIED_MODES: frozenset = frozenset()   # какие классы в нём заняли слоты (гистерезис)
_MIRROR_HOT_UNTIL = 0.0                  # до этого момента состав ЗАМОРОЖЕН (горячая фаза)


def note_transit_tip(tip_wei, agg=None, now=None) -> bool:
    """#104/Ш24b v2: записать tip живого транзита В КОЛЬЦО ЕГО АГРЕГАТОРА.

    agg=None не смешивается с именованными: неизвестное происхождение — это отдельное
    распределение, а не «чьё-то». Вне границ [_MIRROR_TIP_MIN, _MIRROR_TIP_MAX] — отброс.
    """
    try:
        w = int(tip_wei)
    except Exception:                                   # noqa: BLE001
        return False
    if not (_MIRROR_TIP_MIN <= w <= _MIRROR_TIP_MAX):
        return False
    key = str(agg).lower() if agg else _MIRROR_UNKNOWN
    n = float(now if now is not None else time.time())
    with _TRANSIT_TIPS_LOCK:
        ring = _TRANSIT_TIPS.setdefault(key, [])
        ring.append((n, w))
        del ring[:-_MIRROR_K]
    return True


def _agg_mode(fresh):
    """(мода, вес, ts первого сэмпла моды) ОДНОГО кольца при доле >= MIN_SHARE и n >= MIN_N.
    None = мало данных либо разброд. Разброд внутри агрегатора — законное «не знаю»: у
    оператора либо есть tip-класс, либо он меняет его от блока к блоку, и угадывать нечего.

    ВЕС — число свежих сэмплов ИМЕННО этой моды. Это «улика», и по ней (а не по величине tip)
    идёт отбор в слоты: иначе отбор смещён в дорогое по построению."""
    if len(fresh) < _MIRROR_MIN_N:
        return None
    from collections import Counter
    vals = [w for _ts, w in fresh]
    m, cnt = Counter(vals).most_common(1)[0]
    if cnt / len(vals) < _MIRROR_MIN_SHARE:
        return None
    return m, cnt, min(ts for ts, w in fresh if w == m)


def _glue_modes(cands):
    """Склейка кандидатов в КЛАССЫ: цепная, по возрастанию, шаг <= _MIRROR_GLUE_STEP.

    Вход: [(мода, вес, ts первого)]. Выход: [(представитель, суммарный вес, ранний ts)].
    Представитель класса — мода с НАИБОЛЬШИМ весом улики (а не средняя и не крайняя): именно
    её несут ноги, и она обязана быть значением, реально виденным в цепи.
    """
    out = []
    for m, w, ts in sorted(cands):
        if out and m <= out[-1]["last"] * _MIRROR_GLUE_STEP:
            g = out[-1]
            g["last"] = m
            g["w"] += w
            g["ts"] = min(g["ts"], ts)
            g["members"].append((m, w))
        else:
            out.append({"last": m, "w": w, "ts": ts, "members": [(m, w)]})
    return [(max(g["members"], key=lambda mw: (mw[1], -mw[0]))[0], g["w"], g["ts"]) for g in out]


def transit_tip_portfolio(now=None) -> tuple:
    """ПОРТФЕЛЬ ИЗ 4 СЛОТОВ или () при полном незнании (-> статические тиры бит-в-бит).

    ТРИ РЕШЕНИЯ, каждое оплачено разбором:
      * ОТБОР — ПО ВЕСУ УЛИКИ, НЕ ПО ЦЕНЕ. Первая редакция v2 брала `sorted(модy)[:4]`, и это
        был не порядок, а ОТБОР: при четырёх живых классах статика не добавлялась вовсе, а
        выживали четыре САМЫЕ ДОРОГИЕ — то есть призовой пол 1.0 mgwei вылетал из портфеля
        ровно в штиле, где он и выигрывает (замер: 20 призовых tx вскрытия 49834406 все на
        tip ровно 1 000 000). Отбор по весу этого не делает по построению.
      * ПОЛУ СЛОТ ГАРАНТИРОВАН ЯВНО (см. _MIRROR_FLOOR_MAX): живой класс <= 1.2 mgwei
        вытесняет худший по весу из дорогих. Комментарий больше не обещает того, чего нет.
      * ПОРЯДОК ОТОБРАННЫХ — по величине убыв., и только он. Порядок решает, какая нога какой
        tip несёт (h%4), а смена tip ноги = пересборка её лестницы (#127, 28-32с на флот);
        сортировка по свежести переставляла бы слоты на каждом транзите.

    ЗАМОРОЗКА В ГОРЯЧЕЙ ФАЗЕ: пока движок в окне (см. note_hot_phase), возвращается ДЕЙСТВУЮЩИЙ
    состав, каким бы ни был свежий расчёт — пересигнация 110 лестниц в горячей фазе стоит
    дороже любого выигрыша от смены тарифа, а успеть к призу (transit+1, ~2с) она не может.
    """
    global _MIRROR_APPLIED, _MIRROR_APPLIED_MODES
    n = float(now if now is not None else time.time())
    if n < _MIRROR_HOT_UNTIL:
        return _MIRROR_APPLIED
    with _TRANSIT_TIPS_LOCK:
        # п.7: кольцо, где ВСЕ сэмплы протухли, удаляется целиком — иначе словарь растёт
        # именами агрегаторов, которых давно нет, и каждый проход платит за их фильтрацию.
        for k in [k for k, v in _TRANSIT_TIPS.items()
                  if not any(n - ts <= _MIRROR_TTL_SEC for ts, _w in v)]:
            del _TRANSIT_TIPS[k]
        rings = {k: [(ts, w) for ts, w in v if n - ts <= _MIRROR_TTL_SEC]
                 for k, v in _TRANSIT_TIPS.items()}
    cands = [c for c in (_agg_mode(f) for f in rings.values()) if c is not None]
    glued = _glue_modes(cands)
    # ГИСТЕРЕЗИС ВХОДА: класс, которого в действующем составе НЕТ, обязан быть подтверждён.
    # Класс, УЖЕ занимающий слот, переподтверждать не надо — иначе портфель дёргался бы туда-сюда.
    ready = [g for g in glued
             if g[0] in _MIRROR_APPLIED_MODES or g[1] >= _MIRROR_ENTRY_N
             or (n - g[2]) >= _MIRROR_ENTRY_AGE_SEC]
    sel = sorted(ready, key=lambda g: (-g[1], -g[0]))[:_MIRROR_SLOTS]
    floors = [g for g in ready if g[0] <= _MIRROR_FLOOR_MAX]
    if floors and not any(g[0] <= _MIRROR_FLOOR_MAX for g in sel):
        # ГАРАНТИЯ ПОЛА: худший по весу из отобранных уступает место лучшему по весу полу.
        sel = sel[:_MIRROR_SLOTS - 1] + [max(floors, key=lambda g: g[1])]
    if not sel:
        _MIRROR_APPLIED, _MIRROR_APPLIED_MODES = (), frozenset()
        return ()
    modes = sorted({g[0] for g in sel}, reverse=True)
    slots = list(modes)
    for t in _PROBE_TIP_TIERS:                          # добивка статикой, без дублей
        if len(slots) >= _MIRROR_SLOTS:
            break
        if t not in slots:
            slots.append(int(t))
    while len(slots) < _MIRROR_SLOTS:                   # вырожденный список тиров
        slots.append(slots[-1])
    _MIRROR_APPLIED, _MIRROR_APPLIED_MODES = tuple(slots), frozenset(modes)
    return _MIRROR_APPLIED


def note_hot_phase(in_window, now=None, hold_sec=5.0) -> None:
    """Движок сообщает, что он В ОКНЕ (armed/удержание). Прибор СБОКУ от денежного пути: он
    ничего не считает и ничего не гасит, а лишь замораживает СОСТАВ портфеля до угасания окна.
    Пересигнации по СТАТИЧЕСКИМ причинам (полоса maxFee, байт пробы) не затронуты — они живут
    в ladder_resign_needed и от состава не зависят.

    hold_sec — переживание одного каденса аудита (в окне ~1с): без него заморозка снималась бы
    в промежутке между проходами и состав менялся бы посреди окна.
    """
    global _MIRROR_HOT_UNTIL
    if not in_window:
        return
    _MIRROR_HOT_UNTIL = float(now if now is not None else time.time()) + float(hold_sec)


# ЗАМЕР ГАЗА ПРОБЫ — С ЦЕПИ (STATE:652), а не «примерно 52k»: 1-байтовая проба в промах стоит
# 54 710. Число входит в оценку цены портфеля напрямую, поэтому берётся замеренное.
_PROBE_GAS_MEASURED = 54_710
_MIRROR_WINDOW_SEC = 90.0                 # верхняя оценка длины окна (spec-fire), для объёма проб


def mirror_window_probes() -> float:
    """Верхняя оценка числа проб окна ИЗ ЖИВОГО КОНФИГА: темп пейсера × длина окна. Константа
    здесь врала бы при любой смене VOLLEY_PACER_RATE — а именно она и определяет счёт."""
    return max(1.0, float(_PACER_RATE) * _MIRROR_WINDOW_SEC)


def _mirror_cost_usd(slots, eth_usd, probes=None) -> float:
    """Ожидаемая цена tip-проб ОДНОГО окна при этом составе, $. ОЦЕНКА ДЛЯ ЖУРНАЛА, не гейт.

    Ноги делятся по слотам поровну (h%4), поэтому цена = средний tip слотов × объём проб × газ.
    МАСШТАБ ПО ЭТОЙ ЖЕ ФОРМУЛЕ (ETH $1730, газ 54 710, прогон analysis/mirror_v2_replay.py):
    на боевом темпе 110/с (9 900 проб окна) статика ≈ $1.61, затор-портфель [33.4|1.0|1.1|1.775]
    ≈ $8.74; на дефолте пейсера 200/с — $2.93 и $15.89. То есть дневной бакет проб $150 это
    ~93 жирных окна на статике против ~17 на заторе. Цена зеркала не в разах, но она ЕСТЬ, и
    решение «поднять тариф» обязано печататься с ней рядом. (Оценка линзы $8.2/$13.7 считалась
    по иному объёму проб; сюда переносится не число, а формула — считать умеет код.)
    """
    if not slots:
        return 0.0
    v = mirror_window_probes() if probes is None else float(probes)
    return sum((v / len(slots)) * int(t) * _PROBE_GAS_MEASURED * float(eth_usd) / 1e18
               for t in slots)


def transit_tip_mirror(now=None):
    """СОВМЕСТИМОСТЬ v1: (мода, вторая_мода) | None. Читатели-наблюдатели (стенды, rev-скрипты)
    смотрят сюда; денежный путь ходит через `transit_tip_portfolio`."""
    slots = transit_tip_portfolio(now=now)
    if not slots:
        return None
    return (slots[0], slots[1] if len(slots) > 1 else slots[0])


def probe_tip_for(addr) -> int:
    """Тир tip ЭТОЙ ноги. Детерминированно по адресу, без состояния и без сети.

    Флот делится на тиры ровно, и какой бы ни оказалась загрузка решающего блока, один из
    тиров стоит ТОЧНО в классе транзита. Предсказывать величину, живущую две секунды, не
    требуется — вместо угадывания держим портфель.

    Пустой список тиров возвращает прежнюю константу — откат одной переменной окружения.

    Ш24b v2: при живом зеркале слоты — РАЗЛИЧНЫЕ пер-агг моды плюс статические тиры
    (transit_tip_portfolio); зеркало мертво/выключено -> статический портфель БИТ-В-БИТ.
    Контекста окна здесь нет и быть не может: лестницы подписываются ЗАРАНЕЕ, а тир обязан
    быть функцией одного адреса — иначе подписанная лестница и «свежий тир» разъедутся (#127)."""
    if not _PROBE_TIP_TIERS:
        return _PROBE_TIP_WEI
    try:
        h = int(str(addr)[-8:], 16)
    except Exception:                                   # noqa: BLE001
        return _PROBE_TIP_TIERS[0]
    if PROBE_TIP_MIRROR:
        slots = transit_tip_portfolio()
        if slots:
            return slots[h % len(slots)]
    return _PROBE_TIP_TIERS[h % len(_PROBE_TIP_TIERS)]


def mirror_slots_changed(now=None):
    """СМЕНА СОСТАВА портфеля (для строки журнала экономики). Возврат: (слоты, было) либо None,
    если состав тот же. Прибор стоит СБОКУ от денежного пути: probe_tip_for сюда не заходит,
    а зовёт эту функцию наблюдатель (аудит цены), и её отказ огня не касается."""
    global _MIRROR_LAST_SLOTS
    slots = transit_tip_portfolio(now=now) if PROBE_TIP_MIRROR else ()
    with _TRANSIT_TIPS_LOCK:
        prev = _MIRROR_LAST_SLOTS
        if slots == prev:
            return None
        _MIRROR_LAST_SLOTS = slots
    return slots, prev


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
        probe_note_spend(21000 * int(cfg.tip_gwei * 1e9) * 3)     # пессимистичный гейт
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
# 31.07, решение владельца: сэкономленное на сплющенной tip-лестнице — в бюджет проб
# ($10 -> $20 = ~9 плотных окон/сутки; всё ещё 4% дневного газ-лимита $500)
# 06.08 EV-плотность («го» kelbic): жирное окно (каденс 0.12с, кап 600) жжёт до ~$9 —
# $40 держит 4 жирных + штатные, 8% дневного газ-лимита. Откат: SPEC_FIRE_MAX_GAS_USD=20.
_SPECFIRE_MAX_GAS_USD = float(os.environ.get("SPEC_FIRE_MAX_GAS_USD", "40"))
# КАСКАДНЫЙ ПРОТОКОЛ БАКЕТА (ревью 31.07): в каскадный день armed-окна идут сериями и штатный
# бакет сгорает к полудню — вечером самого дорогого дня слой стоит без патронов. При детекте
# каскада (спам армов, см. spec_fire_live) бакет поднимается до CASCADE_GAS_USD на TTL; TTL
# продлевается новыми армами, каскад кончился -> бакет сам возвращается к штатному.
# 0 = протокол НЕ подписан владельцем -> бустер мёртв (капитал — только решением владельца).
_SPECFIRE_CASCADE_GAS_USD = float(os.environ.get("SPEC_FIRE_CASCADE_GAS_USD", "0"))
_SPECFIRE_CASCADE_UNTIL = 0.0
# Пол — анти-раннауэй, не бюджет: расход держит бакет выше. 0.25 -> 0.10 (06.08):
# EV-каденс жирных окон 0.12с обязан проходить. Откат: SPEC_FIRE_MIN_INTERVAL_SEC=0.25.
_SPECFIRE_MIN_INTERVAL = float(os.environ.get("SPEC_FIRE_MIN_INTERVAL_SEC", "0.10"))


def specfire_cascade_boost(ttl_sec: float = 3600.0, now=None):
    """Детектор каскада дёргает бустер. True = буст активен (протокол подписан и TTL заведён)."""
    global _SPECFIRE_CASCADE_UNTIL
    if _SPECFIRE_CASCADE_GAS_USD <= 0:
        return False
    n = time.monotonic() if now is None else now
    with _INFLIGHT_LOCK:
        _SPECFIRE_CASCADE_UNTIL = max(_SPECFIRE_CASCADE_UNTIL, n + ttl_sec)
    return True


def _specfire_cap(now=None):
    """Действующий кап бакета: каскадный при живом TTL, иначе штатный."""
    n = time.monotonic() if now is None else now
    if _SPECFIRE_CASCADE_GAS_USD > 0 and n < _SPECFIRE_CASCADE_UNTIL:
        return _SPECFIRE_CASCADE_GAS_USD
    return _SPECFIRE_MAX_GAS_USD


def _specfire_roll_day_locked():
    """Вызывать ТОЛЬКО под _INFLIGHT_LOCK: сброс бакета на границе UTC-суток."""
    global _SPECFIRE_DAY, _SPECFIRE_GAS_USD
    d = int(time.time() // 86400)
    if d != _SPECFIRE_DAY:
        _SPECFIRE_DAY = d
        _SPECFIRE_GAS_USD = 0.0


def _is_ours(waddr: str, wto: str, cfg=None) -> bool:
    """Победитель — это МЫ? Сверяем и получателя (наш контракт-ликвидатор), и отправителя
    (главный кошелёк или любая нога флота). Тихо False при любой неопределённости."""
    try:
        _mine = set()
        for a in (getattr(cfg, "liquidator_address", None), getattr(cfg, "wallet_address", None)):
            if a:
                _mine.add(str(a).lower())
        try:
            for a, _k in volley_accounts():
                _mine.add(str(a).lower())
        except Exception:
            pass
        return str(waddr).lower() in _mine or str(wto).lower() in _mine
    except Exception:
        return False


def probe_budget_total() -> float:
    """Сумма бакетов ПРОБНЫХ слоёв ($ в сутки). Нужна для перекрёстной проверки на старте."""
    return float(_SPECFIRE_MAX_GAS_USD) + float(CASCADE_TIER_GAS_USD)


def check_gas_budget_sane(max_daily_gas_usd: float, log=None) -> bool:
    """#53: дневной кап kill-switch обязан ПЕРЕКРЫВАТЬ санкционированный расход проб.

    Дефолты разъехались (кап $100 против $40+$150), и выпадение MAX_DAILY_GAS_USD из .env
    превращало САНКЦИОНИРОВАННЫЙ расход тира в остановку ВСЕГО боевого огня посреди каскада.
    Правим не значение, а отсутствие сверки: несогласованность обязана быть громкой на старте,
    когда её ещё можно исправить, а не в каскаде, когда уже поздно."""
    need = probe_budget_total()
    ok = float(max_daily_gas_usd or 0.0) > need
    if not ok and log:
        log.error("КОНФИГ: дневной газ-кап $%.0f НЕ перекрывает бакеты проб $%.0f "
                  "(спек $%.0f + тир $%.0f) — расход проб остановит БОЕВОЙ огонь. "
                  "Подними MAX_DAILY_GAS_USD либо опусти SPEC_FIRE_MAX_GAS_USD/CASCADE_TIER_GAS_USD",
                  float(max_daily_gas_usd or 0.0), need, _SPECFIRE_MAX_GAS_USD, CASCADE_TIER_GAS_USD)
    return ok


def sign_backend_probe(warmup: int = 32, log=None) -> dict:
    """СТАРТОВАЯ ПРОБА ПОДПИСИ: какой ECC-бэкенд РЕАЛЬНО подписывает и почём (#COIN).

    Зачем строка в журнале. Бот подписывает лестницы проб чистым Python, если в venv нет
    coincurve: eth_keys молча берёт NativeECCBackend. Разница замерена — x6.6 по всему
    sign_transaction на боевой форме пробы. «coincurve добавлен в requirements.txt» НЕ значит
    «coincurve стоит на Шарлотте»: пакет доезжает только пересборкой venv, а её легко забыть
    (класс «живой источник против копии в репо»). Поэтому факт обязан быть В ЖУРНАЛЕ БОТА.

    Почему не читаем import coincurve. Наличие модуля не доказывает, что подписывает он:
    ECC_BACKEND_CLASS может пинить native, а eth_account берёт бэкенд не из объекта ключа
    (проверено запуском: acct._key_obj.backend — ДРУГОЙ экземпляр, туда вызов не идёт).
    Поэтому позитивный контроль: на время прогрева оборачиваем ecdsa_sign НА КЛАССЕ и считаем
    вызовы. Ноль вызовов = бэкенд НЕ подтверждён, и строка обязана это сказать («хэш tx — не
    эффект»: имя класса — это хэш, счётчик вызовов — эффект). Обёртка снимается в finally.

    Ключ СЛУЧАЙНЫЙ, одноразовый: боевой .env для замера не нужен и не читается.
    Форма tx — ровно боевая проба доски (V2_PROBE_CALLDATA, EIP-1559, chainId цепи).

    ЦЕНА (замерена, а не оценена): вся проба стоит 348мс на native и 241мс с coincurve —
    один раз на старте, ДО арма. Львиная доля не подписи, а вывод публичного ключа в
    Account.from_key и импорт бэкенда, которые всё равно будут оплачены первой боевой
    подписью. Рестарт вооружённого бота и так стоит секунды простоя."""
    import statistics
    out = {"backend": "?", "p50_ms": None, "n": 0, "confirmed": False, "fast": False,
           "coincurve": None}
    try:
        from eth_keys import keys as _keys
        from eth_account import Account as _Account
        cls = type(_keys.backend)
        out["backend"] = cls.__module__ + "." + cls.__name__
        out["fast"] = "coincurve" in cls.__module__.lower() or "coincurve" in cls.__name__.lower()
        try:
            import coincurve as _cc                     # noqa: F401  (нужен сам факт импорта)
            # ВЕРСИЮ БЕРЁМ ИЗ МЕТАДАННЫХ, А НЕ ИЗ АТРИБУТА: у coincurve 20.0.0 — той самой,
            # что собрана на Шарлотте под Python 3.14 — атрибута __version__ НЕТ вовсе
            # (у 21.0.0 есть). getattr(..., "?") молча печатал бы «coincurve=?» ровно на
            # боевой версии: диагност, врущий именно там, где его читают.
            import importlib.metadata as _md
            try:
                out["coincurve"] = _md.version("coincurve")
            except Exception:
                out["coincurve"] = getattr(_cc, "__version__", "установлен, версия неизвестна")
        except Exception as e:
            out["coincurve"] = "НЕТ (%s)" % type(e).__name__
        acct = _Account.from_key(os.urandom(32))
        tx = {"from": acct.address, "to": acct.address, "data": V2_PROBE_CALLDATA,
              "nonce": 0, "gas": 1_000_000, "chainId": 8453,
              "maxFeePerGas": 100_000_000, "maxPriorityFeePerGas": 1_000_000}
        hits = [0]
        _orig = cls.ecdsa_sign

        def _counting(self, msg_hash, private_key):
            hits[0] += 1
            return _orig(self, msg_hash, private_key)

        cls.ecdsa_sign = _counting
        try:
            samples = []
            for i in range(max(4, int(warmup))):
                tx["nonce"] = i
                _t = time.perf_counter()
                acct.sign_transaction(tx)
                samples.append((time.perf_counter() - _t) * 1e3)
        finally:
            cls.ecdsa_sign = _orig
        out["n"] = len(samples)
        out["p50_ms"] = statistics.median(samples)
        out["confirmed"] = hits[0] >= len(samples)
    except Exception as e:                      # проба ДИАГНОСТИЧЕСКАЯ: не имеет права ронять старт
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    if log:
        _p = "%.3f" % out["p50_ms"] if out["p50_ms"] is not None else "?"
        _line = ("SIGN бэкенд=%s p50=%sмс (%d подписей прогрева) coincurve=%s подтверждён=%s"
                 % (out["backend"], _p, out["n"], out["coincurve"],
                    "да" if out["confirmed"] else "НЕТ"))
        if out["fast"] and out["confirmed"]:
            log.info(_line)
        else:
            # МОЛЧАЛИВАЯ ДЕГРАДАЦИЯ ЗАПРЕЩЕНА. Не собрался coincurve — бот работает (это
            # допустимо), но чистый Python в подписи обязан быть виден с первой строки журнала.
            log.warning("%s — МЕДЛЕННЫЙ ПУТЬ (чистый Python). Лечится пересборкой venv: "
                        "venv/bin/pip install -r requirements.txt", _line)
    return out


def specfire_stats():
    with _INFLIGHT_LOCK:
        _specfire_roll_day_locked()
        return (_SPECFIRE_PROBES, _SPECFIRE_GAS_USD)


def specfire_gas_today() -> float:
    """Спек-газ за текущие UTC-сутки — слагаемое в общий kill-switch (гард видит СУММАРНЫЙ
    дневной расход: боевой + пробный; спек-кап $10 = максимум 10% от дневного лимита).
    Каскадный тир (07.08): + залповый газ всех EOA — kill-switch видит расход всех отправителей."""
    with _INFLIGHT_LOCK:
        _specfire_roll_day_locked()
        _volley_roll_day_locked()
        return _SPECFIRE_GAS_USD + _VOLLEY_GAS_USD


def specfire_gas_add(gas_usd: float):
    global _SPECFIRE_GAS_USD
    with _INFLIGHT_LOCK:
        _specfire_roll_day_locked()
        _SPECFIRE_GAS_USD += max(0.0, gas_usd)


_COMBAT_WIN_HOOK = None


def set_combat_win_hook(fn):
    """#58: точка, куда диспатч сообщает о БОЕВОЙ победе с подтверждённым захватом.

    Кулдаун (#46) по решению владельца взводит только боевая победа, но вызов не был подключён —
    механизм оставался вакуумным, а вакуумный механизм со временем читается как работающий."""
    global _COMBAT_WIN_HOOK
    _COMBAT_WIN_HOOK = fn


def _note_combat_win(mid, borrower, proven=True, log=None):
    fn = _COMBAT_WIN_HOOK
    if fn is None:
        return False
    try:
        return bool(fn(mid, borrower, proven=proven))
    except Exception as e:
        if log:
            log.info("combat-win hook fail: %s", type(e).__name__)
        return False


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
    # #51 (ревью 09.08): ЧТЕНИЯ СЕТИ — ВНЕ _SEND_LOCK. Раньше под локом сидели get_balance (TTL
    # 60с) и _pending_nonce при холодном кэше, то есть НЕ-боевой отправитель держал критическую
    # секцию боевого raw на ~60-75мс ровно в момент транзита. Лок обязан покрывать только
    # раздачу nonce и саму отправку — то, где важен ПОРЯДОК.
    with _INFLIGHT_LOCK:                       # дешёвый предгейт: без I/O
        _specfire_roll_day_locked()
        if (_SPECFIRE_GAS_USD >= _specfire_cap(_now)
                or _now - _SPECFIRE_LAST_TS < _SPECFIRE_MIN_INTERVAL
                or _INFLIGHT > 0):
            return None
    w3 = rpc._web3()
    bot = account_for(w3, cfg.wallet_key).address
    # патронный резерв: расходники не грызут выстрел (см. BULLET_RESERVE_WEI)
    _cost = int(gas) * int(max_fee if max_fee else int(cfg.tip_gwei * 1e9) * 3)
    if not probe_allowed_by_reserve(cached_balance_wei(rpc, bot), _cost):
        if log:
            log.info("SPECFIRE проба молчит: баланс не покрывает патрон %.3f ETH + пробу",
                     BULLET_RESERVE_WEI / 1e18)
        return None
    _pre_nonce = None
    if _LOCAL_NONCE.get(bot) is None:          # холодный кэш: RTT платим ДО лока
        try:
            _pre_nonce = _pending_nonce(w3, bot)
        except Exception:
            return None
    with _SEND_LOCK:
        with _INFLIGHT_LOCK:                   # пере-проверка под локом: гейты те же, I/O нет
            if (_SPECFIRE_GAS_USD >= _specfire_cap(time.monotonic())
                    or _INFLIGHT > 0):
                return None
        base_nonce = _LOCAL_NONCE.get(bot)
        if base_nonce is None:
            base_nonce = _pre_nonce if _pre_nonce is not None else _pending_nonce(w3, bot)
        # tip-гард плотного режима (31.07): переданный tip капнут вызывающим слоем
        # (SPEC_FIRE_TIP_CAP_GWEI) — min_tip_wei обязан уважать кап, иначе send-слой
        # поднимет пробу обратно до боевого cfg.tip_gwei (FCFS: tip места не покупает).
        tip_wei = int(tip) if tip else int(cfg.tip_gwei * 1e9)
        _tx = {"to": cfg.liquidator_address, "data": calldata, "nonce": base_nonce, "gas": int(gas)}
        if max_fee is not None:
            _tx["maxFeePerGas"] = int(max_fee)
            _tx["maxPriorityFeePerGas"] = tip_wei
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
        probe_note_spend(_cost)                   # пессимистичный гейт: см. cached_balance_wei
        return res["hash"]


def send_board_tx(rpc, cfg, calldata: str, gas: int, log=None, tag="board"):
    """v5 stateful board: УПРАВЛЯЮЩАЯ tx владельца (setPlan/clearPlan) с главного кошелька.

    Не проба и не выстрел: своих $-бакетов не трогает, но обязана делить _SEND_LOCK/_LOCAL_NONCE
    с боевым путём — иначе разъедется nonce главного кошелька (тот же инвариант, что у канарейки).
    Гейт _INFLIGHT>0: боевая tx главнее — план подождёт, у него TTL секунды-минуты, а выстрел
    ждать не может. Тип-2 fee по свежему basefee: доска армится ЗАРАНЕЕ, торопиться нечем,
    поэтому tip обычный боевой, а не капнутый пробный."""
    with _SEND_LOCK:
        with _INFLIGHT_LOCK:
            if _INFLIGHT > 0:
                return None
        w3 = rpc._web3()
        bot = account_for(w3, cfg.wallet_key).address
        base_nonce = _LOCAL_NONCE.get(bot)
        if base_nonce is None:
            base_nonce = _pending_nonce(w3, bot)
        tip_wei = int(cfg.tip_gwei * 1e9)
        from chain.hotpath import _fresh_basefee   # локально: hotpath тянет execute обратно
        fb = _fresh_basefee()
        _tx = {"to": cfg.liquidator_address, "data": calldata, "nonce": base_nonce, "gas": int(gas)}
        if fb is not None:
            _tx["maxFeePerGas"] = int(fb * 2 + tip_wei)
            _tx["maxPriorityFeePerGas"] = tip_wei
        try:
            res = send_tx(rpc, cfg.wallet_key, _tx, wait=False, min_tip_wei=tip_wei)
        except Exception as e:
            _LOCAL_NONCE.pop(bot, None)
            if log:
                log.info("BOARD %s send fail: %s", tag, type(e).__name__)
            return None
        _LOCAL_NONCE[bot] = base_nonce + 1
        return res["hash"]


# --- v6-D4 (#135): owner-tx мультислота с ОБЩЕГО с ботом кошелька ---------------------------
# Признаки КОЛЛИЗИИ NONCE у разных узлов/клиентов. Список — по НИЖНЕМУ регистру подстроки.
# «replacement transaction underpriced» ловилось руками 16.08 при ручных tx владельца: бот
# держит в мемпуле свою tx на том же nonce, и наша приходит ВТОРОЙ.
_V6_NONCE_COLLISION = (
    "replacement transaction underpriced",   # geth/reth: тот же nonce, цена не выше
    "replacement gas too low",               # anvil/hardhat
    "nonce too low",                         # номер уже израсходован
    "already known",                         # тот же хэш уже в пуле
    "known transaction",
    "already imported",
    "nonce has already been used",
    "tx already in mempool",
)


def _is_nonce_collision(err) -> bool:
    s = str(err).lower()
    return any(p in s for p in _V6_NONCE_COLLISION)


def send_owner_tx_v6(rpc, cfg, calldata: str, gas: int, log=None, tag="v6",
                     tries: int = 8, receipt_timeout: float = 180.0, poll_sec: float = 0.4,
                     wait: bool = True):
    """v6-D4: управляющая tx владельца (setGroup / setHot / clearTarget / clearHot).

    ДВА отличия от `send_board_tx` (v5), и оба — про то, что кошелёк владельца ОБЩИЙ с ботом:

      1. **NONCE БЕРЁТСЯ PENDING С ЦЕПИ** (а не только из локального кэша): арм v6 идёт из
         фонового треда прогрева и может разъехаться с боевым путём, который тем же ключом
         шлёт выстрелы. Берём max(pending с цепи, локальный счётчик) — локальный знает про
         наши уже разосланные, цепь знает про всё остальное.
      2. **КОЛЛИЗИЯ ЧИНИТСЯ СЛЕДУЮЩИМ NONCE, А НИКОГДА ЦЕНОЙ.** Соблазн «подними tip и
         перебей» здесь ЗАПРЕЩЁН: на том же nonce у нас может стоять СВОЯ БОЕВАЯ tx, и
         перебить её значит выбросить выстрел ради арма. Приз дороже доски. Поэтому
         maxFee/tip фиксируются ОДИН раз до цикла и в ретраях не трогаются вовсе.

    Возврат: КВИТАНЦИЯ (dict с 'status'/'blockNumber'), а не хэш — приёмка §3 читается по
    квитанции и по цепи («хэш tx — не эффект»). None = не ушла / квитанции нет.
    """
    with _INFLIGHT_LOCK:
        if _INFLIGHT > 0:
            if log:
                log.info("V6 %s: боевая tx в полёте — арм подождёт (у него TTL минуты)", tag)
            return None
    w3 = rpc._web3()
    bot = account_for(w3, cfg.wallet_key).address
    tip_wei = int(cfg.tip_gwei * 1e9)
    from chain.hotpath import _fresh_basefee   # локально: hotpath тянет execute обратно
    fb = _fresh_basefee()
    if fb is None:
        try:
            fb = int(w3.eth.get_block("latest").get("baseFeePerGas") or 0)
        except Exception:
            fb = None
    # ЦЕНА СЧИТАЕТСЯ ОДИН РАЗ И БОЛЬШЕ НЕ МЕНЯЕТСЯ (см. пункт 2 докстринга).
    _fees = {} if fb is None else {"maxFeePerGas": int(fb * 2 + tip_wei),
                                   "maxPriorityFeePerGas": tip_wei}
    h = None
    with _SEND_LOCK:
        try:
            n = max(int(_pending_nonce(w3, bot)), int(_LOCAL_NONCE.get(bot) or 0))
        except Exception as e:
            if log:
                log.info("V6 %s: pending-nonce не прочитан (%s) — tx не отправлена",
                         tag, type(e).__name__)
            return None
        for _k in range(max(1, int(tries))):
            _tx = {"to": cfg.liquidator_address, "data": calldata, "nonce": n, "gas": int(gas)}
            _tx.update(_fees)
            try:
                res = send_tx(rpc, cfg.wallet_key, _tx, wait=False, min_tip_wei=tip_wei)
            except Exception as e:
                if _is_nonce_collision(e):
                    # Номер занят ЧУЖОЙ (нашей боевой) tx. Отходим на следующий свободный —
                    # цену НЕ трогаем.
                    if log:
                        log.info("V6 %s: nonce %d занят (%s) -> пробуем %d, цена НЕ поднимается",
                                 tag, n, type(e).__name__, n + 1)
                    n += 1
                    continue
                _LOCAL_NONCE.pop(bot, None)   # неизвестно, съелся ли nonce -> ресинк с цепи
                if log:
                    log.info("V6 %s send fail: %s", tag, type(e).__name__)
                return None
            h = res.get("hash")
            _LOCAL_NONCE[bot] = n + 1
            break
    if h is None:
        if log:
            log.info("V6 %s: свободный nonce не найден за %d попыток — tx НЕ отправлена "
                     "(цену не поднимаем принципиально)", tag, tries)
        return None
    if not wait:
        return {"transactionHash": h, "status": None, "blockNumber": None}
    # Квитанция — ВНЕ _SEND_LOCK: ждать её под локом значило бы держать боевой путь.
    _t0 = time.monotonic()
    while time.monotonic() - _t0 < float(receipt_timeout):
        try:
            r = w3.eth.get_transaction_receipt(h)
        except Exception:
            r = None
        if r:
            return dict(r) if not isinstance(r, dict) else r
        time.sleep(float(poll_sec))
    if log:
        log.info("V6 %s: квитанция не пришла за %.0fс — состояние НЕИЗВЕСТНО (не «не сел»)",
                 tag, receipt_timeout)
    return None


def canary_invalidate_nonce(rpc, cfg):
    """Канарейка не замайнилась за timeout (возможно дропнута) -> _LOCAL_NONCE впереди чейна ->
    следующая tx уйдёт в дыру. Инвалидируем (как timeout-ветка dispatch, II нонс-фикс)."""
    try:
        bot = account_for(rpc._web3(), cfg.wallet_key).address
        with _SEND_LOCK:
            _LOCAL_NONCE.pop(bot, None)
    except Exception:
        pass


# ---- КАСКАДНЫЙ ТИР («го» kelbic 07.08; контракт v4 authorized-set задеплоен, 4 EOA): залп ----
# Замер окна победы (CASCADE_TIER_DESIGN): P(win) ~ W/τ, W=0.5-2.2мс; один EOA упирается в
# nonce-очередь (~13 проб/блок, τ~120мс = 0.4-1.8% на контестед-событие). Залп кладёт ту же
# пробу от N залповых EOA — РАЗНЫЕ nonce-потоки заполняют разные слоты блока, τ -> τ/(1+N).
# Здесь только механика отправки; активацию (fat_mode И каскад-детект) решает spec_fire_live.
# Ключи SPEC_FIRE_EOA_KEY_2.. живут ТОЛЬКО в .env Шарлотты; профит v4 шлёт owner'у
# безусловно — компрометация залпового ключа = чужой газ, не касса (testCompromisedVolleyCannotSteal).
# Свой $-бакет CASCADE_TIER_GAS_USD; kill-switch видит сумму через specfire_gas_today().
# Свой nonce-поток на EOA (_VOLLEY_NONCE + пер-EOA лок) — _SEND_LOCK главного кошелька залп
# НЕ трогает: боевой путь не тормозится и не толкается. Откат: SPEC_FIRE_VOLLEY=0.
SPEC_FIRE_VOLLEY = os.environ.get("SPEC_FIRE_VOLLEY", "1").lower() not in ("0", "false", "no")
CASCADE_TIER_GAS_USD = float(os.environ.get("CASCADE_TIER_GAS_USD", "150"))
_VOLLEY_GAS_USD = 0.0
_VOLLEY_GAS_RESERVED = 0.0        # #93: газ отправленных проб, чьи квитанции ещё в пути
_VOLLEY_RESERVE_BY_HASH = {}      # hash -> $ брони: освобождаем РОВНО то, что заняли
# Худший случай пробы ПОСЛЕ срабатывания триггера: копия KB-плана + вызов Morpho.
# Замер на форке (#70): 445-710k. Бронируем по верхней границе — недобронировать
# опаснее, чем перебронировать: бронь живёт секунды до квитанции.
_VOLLEY_PROBE_GAS_WORST = int(os.environ.get("SPEC_FIRE_PROBE_GAS_WORST", "710000"))
_VOLLEY_PROBES = 0
_VOLLEY_DAY = int(time.time() // 86400)
_VOLLEY_ACCTS = None            # lazy: [(addr, key)] из env; [] = ключей нет -> тир молчит
_VOLLEY_NONCE: dict = {}        # addr -> следующий nonce (пер-EOA, инварианты как _LOCAL_NONCE)
_VOLLEY_LOCKS: dict = {}        # addr -> Lock
_VOLLEY_BAL: dict = {}          # addr -> [balance_wei, monotonic_ts] (кэш патрона, TTL 60с)
_VOLLEY_BAL_TTL = 60.0
# Пол патрона на ногу: ниже него нога молчит (долив — решение владельца). Настраиваемый, потому
# что для 4 жирных ног 0.003 ETH разумно, а для флота из десятков это мёртвый капитал БОЛЬШЕ
# самого флота: 70 ног x 0.003 = 0.21 ETH заперто, при том что проба стоит ~0.0000024 ETH.
_VOLLEY_BAL_FLOOR_WEI = int(float(os.environ.get("SPEC_FIRE_VOLLEY_FLOOR_ETH", "0.003")) * 10**18)
# ГЕЙТ И РАСХОД — РАЗНЫЕ ВЕЛИЧИНЫ (замер 07.08, #32). Лимит газа пробы доски считается от
# ВЫИГРЫША (3M: ликвидация кита 2.47M + копия плана из холодного стораджа 215k), но реальная
# проба — тихий no-op на ~40k. Если списывать с кэша баланса весь лимит x maxFee, нога при
# 3M замолкает после ~28 проб (0.005 ETH запаса / 0.00018) — это ДВА блока, то есть флот
# глушил бы сам себя ровно в каскаде, ради которого и строится. Поэтому:
#   гейт     — по ХУДШЕМУ случаю: нога обязана быть способна оплатить полный выигрыш;
#   декремент— по ОЖИДАЕМОМУ: no-op, а не лимит (истину всё равно вернёт refresh кэша).
_VOLLEY_EXPECTED_GAS = int(os.environ.get("SPEC_FIRE_VOLLEY_EXPECTED_GAS", "45000"))
# ПУТЬ ДВИЖКА (#45, ревью 08.08): гейт патрона там был сломан в ОБЕ стороны — при пустом кэше
# молча пропускался (незнание как разрешение — канон, экземпляр katana 08.08), а сам кэш на
# этом пути никогда не обновлялся, и пессимистичный декремент по ЭСТИМЕЙТУ гнал его к полу:
# нога самоглушилась за ~135-155 проб при реальном расходе no-op 0.00000009 ETH (завышение
# ~250x). Решение владельца: декремент — по ФАКТИЧЕСКОЙ квитанции, пустой кэш — пропуск с
# АСИНХРОННЫМ дозором (синхронный get_balance на пути огня стоил бы RTT в горячем окне),
# отказы — счётчиком в экспорт (невидимый отказ = отказ, который никто не чинит).
_VOLLEY_BAL_MAX_AGE = float(os.environ.get("SPEC_FIRE_VOLLEY_BAL_MAX_AGE_SEC", "600"))
_VOLLEY_BAL_REFRESHING: set = set()      # addr с дозором в полёте (без него 8 тредов = шторм)
_VOLLEY_REFRESH_MAX = int(os.environ.get("SPEC_FIRE_VOLLEY_REFRESH_MAX", "4"))   # потолок на флот
_VOLLEY_REFRESH_TS: dict = {}            # addr -> ts последнего дозора (ограничение ТЕМПА)
_VOLLEY_REFRESH_MIN_GAP = float(os.environ.get("SPEC_FIRE_VOLLEY_REFRESH_GAP_SEC", "15"))
# Во сколько раз записанный max_fee должен превышать нынешний, чтобы лестницу пересобрать
# ВНИЗ (разрыв защёлки). 3x: обычную рябь basefee не трогаем, вздутие всплеска — снимаем.
_VOLLEY_MAXFEE_RESIGN_MULT = float(os.environ.get("SPEC_FIRE_PRESIGN_RESIGN_MULT", "3"))
_VOLLEY_FLOOR_SKIPS = 0                  # отказов «патрон на поле»
_VOLLEY_UNKNOWN_SKIPS = 0                # отказов «баланс неизвестен/протух» (fail-closed)
# Г2д-3/#132: отказ по ОБЩЕМУ бакету «суточный факт + бронь в полёте» был НЕМЫМ — ни счётчика,
# ни лога, и в stats движка он тонул в skipped_dry вперемешку со «склад пуст». Замер линзы
# 16.08: при базе 0.02 gwei и $50 суточных флот немеет после 117 проб (1.1 обхода из 110), при
# $100 — после 59, то есть ПОЛОВИНА флота молчит; каскад = всплеск базы, значит потолок
# сжимается ровно в решающий момент. Сначала ВИДИМОСТЬ (этот счётчик), размер брони — только
# по замеренной частоте (канон: не менять денежный гейт вслепую).
_VOLLEY_RESERVE_SKIPS = 0                # отказов «суточный факт + бронь >= кап тира»
_VOLLEY_INFLIGHT_SKIPS = 0               # отказов «боевая tx в полёте» (тот же гейт, вторая причина)
_VOLLEY_MAXFEE_MUTE: set = set()         # ноги, отключённые ПОЛОМ ПАТРОНА (gas x max_fee):
                                         # это состояние требует человека — долив или
                                         # ручка множителя, поэтому оно кричит, а не молчит
# Пробы движка мимо гейта _INFLIGHT (решение kelbic 08.08 по замеру немоты: p50=1.8с/tx,
# в каскаде 17:00 три слепых пятна съели 75% горячего окна, а окно фида закрылось ВНУТРИ
# первой немоты). Касается ТОЛЬКО send_presigned_probe (1-байт со склада, свои nonce-потоки);
# кадансовые пробы главного кошелька и V1-залп ног остаются под гейтом — у них общий
# nonce/фиатный вес. Откат: SPEC_FIRE_ENGINE_IGNORE_INFLIGHT=0.
_ENGINE_PROBE_IGNORE_INFLIGHT = os.environ.get(
    "SPEC_FIRE_ENGINE_IGNORE_INFLIGHT", "1").lower() not in ("0", "false", "no")


def volley_probe_skips():
    """(floor_skips, unknown_skips) — отказы пути движка. В экспорт: отказ пола БЕЗ счётчика
    неотличим от «окно было пустым», и нога, замолчавшая на 135-й пробе, выглядела бы штилем."""
    with _INFLIGHT_LOCK:
        return (_VOLLEY_FLOOR_SKIPS, _VOLLEY_UNKNOWN_SKIPS)


def volley_budget_skips():
    """#132: (reserve_skips, inflight_skips) — отказы ОБЩЕГО бакета тира, разведённые по причине.

    `reserve_skips` растёт, когда «суточный факт + бронь в полёте» выбрали `CASCADE_TIER_GAS_USD`:
    это НЕМОТА ФЛОТА ПО БЮДЖЕТУ, и она приходит тем раньше, чем выше база — то есть ровно в
    каскаде. Пока числа не было, отказ тонул в `skipped_dry` вместе со «склад пуст».
    Размер брони по этому числу и пересматривать: сначала частота, потом ручка."""
    with _INFLIGHT_LOCK:
        return (_VOLLEY_RESERVE_SKIPS, _VOLLEY_INFLIGHT_SKIPS)


def volley_maxfee_muted():
    """Ноги, отключённые полом патрона (резерв gas x max_fee съел бюджет). Состояние
    требует ЧЕЛОВЕКА: долив ноги либо ручка MAXFEE_RESERVE_MULT — само не рассосётся."""
    with _INFLIGHT_LOCK:
        return set(_VOLLEY_MAXFEE_MUTE)


_MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_MC3_GETBAL_SEL = "0x4d2301cc"           # getEthBalance(address)


def volley_bal_warm_fleet(rpc, addrs, log=None) -> int:
    """Освежить патрон ВСЕГО флота ОДНИМ мультиколлом. Возврат: сколько ног обновлено.

    КВОТА (напоминание владельца 09.08 и урок rpc-key-striping-drift): пер-ноговое освежение
    по возрасту стоило бы 110 x (get_balance + get_block) каждые полминуты — ~7 запросов/с
    круглосуточно в БОЕВОЙ ключ, то есть сотни миллионов CU в месяц ради величины, которая
    меняется только когда нога стреляет (а расход мы и так книжим по квитанции) или когда её
    доливают. Один aggregate3 на весь флот стоит один запрос."""
    addrs = [a for a in (addrs or []) if a]
    if not addrs:
        return 0
    try:
        from chain.multicall import aggregate3
        # aggregate3 принимает (target, calldata) и сам ставит allowFailure — трёхкортежи
        # роняли его ValueError'ом НА КАЖДОМ вызове (мой дефект 09.08, поймало ревью)
        calls = [(_MC3, bytes.fromhex(_MC3_GETBAL_SEL[2:]) + bytes(12) +
                  bytes.fromhex(str(a)[2:])) for a in addrs]
        res = aggregate3(rpc, calls)
    except Exception as e:
        if log:
            log.info("VOLLEY патрон флота: мультиколл не удался (%s) — ноги под fail-closed гейтом",
                     type(e).__name__)
        return 0
    n = 0
    now = time.monotonic()
    for a, r in zip(addrs, res or []):
        try:
            ok, data = (r[0], r[1]) if isinstance(r, (list, tuple)) else (False, None)
            if not ok or not data:
                continue
            _VOLLEY_BAL[a] = [int.from_bytes(bytes(data)[-32:], "big"), now]
            n += 1
        except Exception:
            continue
    return n


def volley_bal_stale(addr: str) -> bool:
    """Патрон ноги протух и подлежит освежению ФОНОМ. Гейт огня fail-closed по возрасту, поэтому
    единственный прогрев в момент СОЗДАНИЯ лестницы флот не спасает: между fat-окнами проходят
    недели, склад всё это время полон, пополнение не зовётся — и на старте окна немыми оказались
    бы все ноги разом. Порог освежения — половина TTL, чтобы фон успевал до огня."""
    bal = _VOLLEY_BAL.get(addr)
    return bal is None or (time.monotonic() - bal[1]) > (_VOLLEY_BAL_TTL * 0.5)


def volley_bal_warm(rpc, addr: str, log=None) -> bool:
    """Синхронно засеять кэш патрона ВНЕ пути огня (пресайн лестницы, прогрев доски).
    Возврат True, если кэш теперь живой. Здесь RTT допустим — путь холодный."""
    try:
        w3 = rpc._web3()
        b = int(w3.eth.get_balance(addr))
    except Exception as e:
        # Молчаливая осечка = лестница на 40 проб при пустом кэше, то есть нога, немая до
        # следующего освежения. Говорим вслух.
        if log:
            log.info("VOLLEY %s: прогрев патрона не удался (%s) — нога под fail-closed гейтом",
                     addr[:10], type(e).__name__)
        return False
    _VOLLEY_BAL[addr] = [b, time.monotonic()]
    return True


def volley_bal_refresh_async(rpc, addr: str):
    """Дозор патрона мимо пути огня: один в полёте на ногу И не более _VOLLEY_REFRESH_MAX на
    весь флот. Без общего потолка 110 стухших ног за один круг round-robin дали бы 110 тредов
    и ~3000 CU/с в боевой ключ — самоподдерживающийся 429-шторм ровно в горячем окне."""
    _now = time.monotonic()
    with _INFLIGHT_LOCK:
        if addr in _VOLLEY_BAL_REFRESHING or len(_VOLLEY_BAL_REFRESHING) >= _VOLLEY_REFRESH_MAX:
            return
        # #65: потолок держит ЧИСЛО одновременных дозоров, но не ТЕМП — без этого интервала
        # нога, у которой дозор ничего не меняет, перезапускала бы его каждый RTT.
        if _now - _VOLLEY_REFRESH_TS.get(addr, -1e9) < _VOLLEY_REFRESH_MIN_GAP:
            return
        _VOLLEY_REFRESH_TS[addr] = _now
        _VOLLEY_BAL_REFRESHING.add(addr)

    def _run():
        try:
            volley_bal_warm(rpc, addr)
        finally:
            with _INFLIGHT_LOCK:
                _VOLLEY_BAL_REFRESHING.discard(addr)

    threading.Thread(target=_run, daemon=True).start()


def volley_bal_charge(addr: str, wei: int):
    """Списать с кэша патрона ФАКТИЧЕСКИЙ расход по квитанции (#45). Эстимейт на пути огня
    завышал расход в ~250 раз и глушил ногу за ~135 проб; истина приходит от коллектора."""
    if not addr or wei <= 0:
        return
    bal = _VOLLEY_BAL.get(addr)
    if bal is None:
        return
    bal[0] = max(0, int(bal[0]) - int(wei))


def volley_accounts():
    """Залповые EOA: сперва именованные SPEC_FIRE_EOA_KEY_2.. (подряд до первого пропуска),
    затем ФЛОТ из SPEC_FIRE_EOA_KEYFILE — по одной приватке на строку.

    Файл, а не env-имена, потому что плотность упирается в ЧИСЛО ног: замер 07.08 показал у
    конкурента 2592 tx от 2592 EOA в одном блоке (2590 холостых зондов по 27260 газа + один
    исполнитель на 709k). Восемь env-переменных такую стену не опишут.

    Файл обязан быть 0600 и жить ТОЛЬКО на боевой машине. Ключи НЕ логируются нигде — ни при
    ошибке, ни при отладке; наружу идут только адреса и счётчики.
    """
    global _VOLLEY_ACCTS
    if _VOLLEY_ACCTS is None:
        accs = []
        seen = set()
        try:
            from eth_account import Account

            def _add(k):
                k = (k or "").strip()
                if not k or k.startswith("#"):
                    return
                a = Account.from_key(k)
                if a.address in seen:
                    return                          # дубль ключа = один nonce-поток на две ноги
                seen.add(a.address)
                accs.append((a.address, k))
                _VOLLEY_LOCKS.setdefault(a.address, threading.Lock())

            for i in range(2, 10):
                k = os.environ.get("SPEC_FIRE_EOA_KEY_%d" % i)
                if not k:
                    break
                _add(k)
            kf = os.environ.get("SPEC_FIRE_EOA_KEYFILE", "").strip()
            if kf and not os.path.exists(kf):
                # #48: путь указан, файла нет — флот исчезает ЦЕЛИКОМ, а движок выглядит живым.
                # Прежде это был тихий скип: ни строки в журнале, ноль проб, и читается как
                # «не было целей». Отказ громкий и виден деадману.
                log.error("VOLLEY keyfile %s НЕ НАЙДЕН — ФЛОТ НЕ ЗАГРУЖЕН (остались только "
                          "именованные ключи: %d)", kf, len(accs))
            if kf and os.path.exists(kf):
                mode = os.stat(kf).st_mode & 0o077
                if mode:
                    log.warning("VOLLEY keyfile %s доступен группе/миру (mode %o) — "
                                "ФЛОТ НЕ ЗАГРУЖЕН, поправь права на 0600", kf, mode)
                else:
                    n0 = len(accs)
                    for line in open(kf):
                        _add(line)
                    _added = len(accs) - n0
                    if _added == 0:
                        log.error("VOLLEY keyfile %s ПУСТ или не дал ни одного ключа — "
                                  "флот не загружен", kf)
                    else:
                        log.info("VOLLEY флот: +%d ключей из keyfile", _added)
        except Exception as e:
            log.warning("VOLLEY реестр ключей: %s — тир молчит", type(e).__name__)
            accs = []
        # СТУПЕНЬ включения флота. Авторизация ноги на цепи — разовое и дорогое по хлопотам
        # действие (setAuthorized по одному адресу, owner-only, бот на это время стоит), поэтому
        # на цепи авторизуем ВЕСЬ флот сразу, а сколько ног реально стреляет — решает эта
        # переменная. Так ступень 20 -> 106 меняется правкой .env и рестартом, без единой tx.
        # 0/пусто = без ограничения. Срез с начала списка детерминирован: одни и те же ноги на
        # каждом рестарте, значит и nonce-потоки продолжаются, а не скачут по флоту.
        try:
            cap = int(os.environ.get("SPEC_FIRE_EOA_MAX", "0") or 0)
        except ValueError:
            cap = 0
        if cap > 0 and len(accs) > cap:
            log.info("VOLLEY ступень: %d ног из %d (SPEC_FIRE_EOA_MAX)", cap, len(accs))
            accs = accs[:cap]
        _VOLLEY_ACCTS = accs
        if accs:
            # адреса печатаем только у малого набора: строка на 70 адресов бесполезна в журнале
            tail = (", ".join(a for a, _ in accs) if len(accs) <= 8
                    else ", ".join(a for a, _ in accs[:4]) + " ... (+%d)" % (len(accs) - 4))
            log.info("VOLLEY тир: %d залповых EOA: %s", len(accs), tail)
    return _VOLLEY_ACCTS


def _volley_roll_day_locked():
    """Только под _INFLIGHT_LOCK: суточный сброс залпового бакета (как _specfire_roll_day)."""
    global _VOLLEY_DAY, _VOLLEY_GAS_USD
    d = int(time.time() // 86400)
    if d != _VOLLEY_DAY:
        _VOLLEY_DAY = d
        _VOLLEY_GAS_USD = 0.0
        globals()["_VOLLEY_GAS_RESERVED"] = 0.0
        _VOLLEY_RESERVE_BY_HASH.clear()


def volley_gas_reserve(txh: str, usd: float):
    """#93: ЗАБРОНИРОВАТЬ газ в момент ОТПРАВКИ, не дожидаясь квитанции.

    Прежде расход рос только в `_engine_result`, то есть по пришедшей квитанции. Коллектор
    разбирает 64 квитанции/с, а движок настроен на 400 отправок/с — учёт отставал ВШЕСТЕРО.
    Пока триггер не сработал, проба стоит 23.6k и отставание безобидно; но ПОСЛЕ пересечения
    триггера проба копирует KB-план и зовёт Morpho — 445-710k. Полный магазин 110x40 = 4400 проб
    это ~$290 против бакета $150 и суточного выключателя $500, а `check_gas_budget_sane`
    перелёта не моделирует вовсе. То есть проигранная гонка могла сжечь половину суточного
    выключателя раньше, чем тормоз это увидит, — а сработавший выключатель глушит И БОЕВОЙ путь.

    Бронь ВРЕМЕННАЯ и держится ПО ХЭШУ: квитанция освобождает ровно ту сумму, что заняла эта
    же проба. Поэтому суточный ФАКТ от брони не растёт — она ограничивает мгновенную экспозицию."""
    global _VOLLEY_GAS_RESERVED
    if not txh:
        return
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        u = max(0.0, float(usd or 0.0))
        _VOLLEY_RESERVE_BY_HASH[txh] = u
        _VOLLEY_GAS_RESERVED += u


def volley_gas_settle(txh: str, gas_usd: float):
    """Записать ФАКТ по квитанции и снять бронь ИМЕННО этой пробы."""
    global _VOLLEY_GAS_USD, _VOLLEY_GAS_RESERVED
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        _VOLLEY_GAS_USD += max(0.0, float(gas_usd or 0.0))
        u = _VOLLEY_RESERVE_BY_HASH.pop(txh, 0.0)
        if u:
            _VOLLEY_GAS_RESERVED = max(0.0, _VOLLEY_GAS_RESERVED - u)


def volley_gas_add(gas_usd: float):
    """Совместимость: только ФАКТ, без снятия брони (путь V1 брони не ставит)."""
    global _VOLLEY_GAS_USD
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        _VOLLEY_GAS_USD += max(0.0, gas_usd)


def volley_gas_today() -> float:
    """ФАКТ за сутки (без брони) — для отчётов и дневного kill-switch."""
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        return _VOLLEY_GAS_USD


def volley_gas_committed() -> float:
    """ФАКТ + бронь в полёте — величина, по которой обязаны решать ГЕЙТЫ огня."""
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        return _VOLLEY_GAS_USD + _VOLLEY_GAS_RESERVED


def volley_leg_landed(rpc, addr: str) -> bool:
    """#102: села ли ВСЯ отправленная очередь этой ноги — сверка nonce с ЦЕПЬЮ.

    «Квитанция не пришла за таймаут» НЕ равно «нога сломана»: 10.08 сборщик квитанций
    захлебнулся под плотным огнём, выдал 33 ложных NO-RECEIPT подряд, и каждый из них сбросил
    лестницу — весь флот ушёл в пересборку на 28-32с. Окно молчания пришлось РОВНО на транзит
    ценой $600-900. При этом транзакции САДИЛИСЬ: за 16 блоков момента отправки с главного
    кошелька село 20 tx, с одной ноги 9. То есть код принял незнание за факт поломки — тот же
    класс, что мы уже ловили дважды.

    Возврат True = на цепи nonce догнал наш локальный, значит дыр нет и сбрасывать нечего.
    Ошибка чтения -> False (незнание закрывает гард: лучше лишняя пересборка, чем дыра nonce)."""
    try:
        local = _VOLLEY_NONCE.get(addr)
        if local is None:
            return False
        on_chain = int(rpc._web3().eth.get_transaction_count(addr, "pending"))
        return on_chain >= int(local)
    except Exception:
        return False


def volley_invalidate_nonce(addr: str):
    """NO-RECEIPT ноги залпа: nonce EOA возможно впереди чейна — пере-читать на следующей."""
    lk = _VOLLEY_LOCKS.get(addr)
    if lk is None:
        return
    with lk:
        _VOLLEY_NONCE.pop(addr, None)


def send_volley_probe(rpc, cfg, addr: str, key: str, calldata: str, gas: int, log=None,
                      max_fee=None, tip=None, broadcast_raw_urls=None):
    """Одна нога залпа: ТА ЖЕ проба (calldata на v4), но от залпового EOA — свой nonce-поток.
    Гейты до любого RPC: $-бакет тира и _INFLIGHT>0 (боевая tx главнее — как у проб).
    Под пер-EOA локом: патронный пол по кэшу баланса (TTL 60с, пессимистичный декремент),
    затем send_tx тем же SEND-3 путём. Возврат hash|None."""
    global _VOLLEY_PROBES
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        if (_VOLLEY_GAS_USD + _VOLLEY_GAS_RESERVED) >= CASCADE_TIER_GAS_USD or _INFLIGHT > 0:
            return None
    lk = _VOLLEY_LOCKS.get(addr)
    if lk is None:
        return None
    w3 = rpc._web3()
    with lk:
        _n = time.monotonic()
        bal = _VOLLEY_BAL.get(addr)
        if bal is None or _n - bal[1] > _VOLLEY_BAL_TTL:
            try:
                bal = [int(w3.eth.get_balance(addr)), _n]
                _VOLLEY_BAL[addr] = bal
            except Exception:
                pass                      # чтение упало: живём старым кэшем; без кэша молчим
        if bal is None:
            return None
        _px = int(max_fee if max_fee else int(cfg.tip_gwei * 1e9) * 3)
        _worst = int(gas) * _px              # гейт: нога обязана потянуть ПОЛНЫЙ выигрыш
        _spend = _VOLLEY_EXPECTED_GAS * _px  # декремент: реальный расход тихой пробы
        if bal[0] - _worst < _VOLLEY_BAL_FLOOR_WEI:
            if log:
                log.info("VOLLEY %s молчит: патрон на поле %.4f ETH (долив — решение владельца)",
                         addr[:10], bal[0] / 1e18)
            return None
        nonce = _VOLLEY_NONCE.get(addr)
        if nonce is None:
            nonce = _pending_nonce(w3, addr)
        tip_wei = int(tip) if tip else _PROBE_TIP_WEI   # #91: V1-проба залпа — на ПОЛУ рынка
        _tx = {"to": cfg.liquidator_address, "data": calldata, "nonce": nonce, "gas": int(gas)}
        if max_fee is not None:
            _tx["maxFeePerGas"] = int(max_fee)
            _tx["maxPriorityFeePerGas"] = tip_wei
        try:
            res = send_tx(rpc, key, _tx, wait=False, min_tip_wei=tip_wei,
                          broadcast_raw_urls=broadcast_raw_urls)
        except Exception as e:
            _VOLLEY_NONCE.pop(addr, None)
            if log:
                log.info("VOLLEY send fail %s: %s", addr[:10], type(e).__name__)
            return None
        _VOLLEY_NONCE[addr] = nonce + 1
        bal[0] -= _spend                 # по ФАКТУ no-op'а, не по лимиту; истину вернёт refresh
        with _INFLIGHT_LOCK:
            _VOLLEY_PROBES += 1
        return res["hash"]


# --- ПРЕД-ПОДПИСАННАЯ ЛЕСТНИЦА ПРОБ (#32) --------------------------------------------------
# Замер боевого send-пути (07.08, НА NativeECCBackend — coincurve в venv не было, вскрыто
# 13.08): подпись p50=4мс/p99=36мс + вызов держит тред 60мс (p50). Подпись с coincurve стоит
# 0.557мс (замер на этой же форме), поэтому вклад ПОДПИСИ в 60мс удержания треда — шум; сами
# 60мс это сеть, их пакет COIN не трогает. При 8
# тредах это потолок ~133 отправки/с, то есть эквивалент ~20 стволов — лестница «×22» упиралась
# в НАШ процесс, а не в сеть. Проба доски всегда одна и та же (ровно байт 0x01, план живёт в
# контракте), поэтому её можно подписать ЗАРАНЕЕ и в момент огня только разослать готовые байты.
#
# Почему это безопасно по цене (замер 07.08): basefee Base прижат к полу 0.005 gwei; даже в
# июньском каскаде выборка дала максимум 0.00562 gwei = 1.1x пола. Предел замера назван: сетка
# редкая (раз в 800 блоков), короткий всплеск она бы пропустила — поэтому maxFee берётся с
# запасом MAXFEE_MULT, а платим всё равно по факту.
#
# ГЛАВНЫЙ ИНВАРИАНТ: nonce'ы лестницы рассылаются СТРОГО ПОДРЯД. Пропуск = дыра в nonce, и
# все последующие пробы ноги повиснут до её закрытия. Поэтому неудачная рассылка возвращает
# запись НА МЕСТО, а не пропускает её.
_VOLLEY_LADDER: dict = {}       # addr -> {"q": [(nonce, raw_hex, hash)], "gas":, "max_fee":, "ts":}
_VOLLEY_PRESIGN_DEPTH = int(os.environ.get("SPEC_FIRE_PRESIGN_DEPTH", "40"))
# МНОЖИТЕЛЬ РЕЗЕРВА (решение kelbic 09.08, 100 -> 20). Он задаёт maxFee пред-подписанной
# пробы, а через неё — резерв гейта патрона (gas x max_fee). На Base порядок в блоке FCFS:
# maxFee НЕ покупает позицию, она нужна лишь для ВАЛИДНОСТИ (tx включаема, пока maxFee
# покрывает basefee). Значит ×100 не давал ничего, кроме запертого капитала: при заправке
# 0.008 ETH и поле 0.003 нога немела, если лестница подписана при basefee > 0.0162 gwei —
# запас всего 3.2x над полом Base 0.005, а каскад и есть всплеск загрузки (замер по блоку
# 49666338: 4 блока подряд на 99.9-100% заполнения, basefee растёт максимальным шагом).
# Откат: MAXFEE_RESERVE_MULT=20 (прежнее значение) или =100 (до #60).
_VOLLEY_PRESIGN_MAXFEE_MULT = int(os.environ.get(
    "MAXFEE_RESERVE_MULT",
    # #89 (10.08): множитель ОСТАВЛЕН 20 осознанно. Соблазн был опустить его до 5 ради порога
    # немоты, но размен считается в пользу текущего режима, а не гипотетического: порог немоты
    # СЕГОДНЯ не связывает вовсе (за 30 суток максимум базы по ВСЕМ 1 296 000 блокам = 0.0614
    # gwei, ни один блок порога не достиг), тогда как maxFee лестницы при множителе 5 упал бы
    # до 0.026 gwei — запас всего 5x над базой, и любой обычный всплеск делал бы пред-подписанные
    # пробы невключаемыми до пересборки в 28-32с. Резерв снижен там, где это бесплатно, —
    # через gas_limit (3M -> 900k, см. SPEC_FIRE_V2_FIRE_GAS): порог уехал 0.083 -> 0.278 gwei
    # без потери запаса валидности. Откат к прежней страховке: MAXFEE_RESERVE_MULT=5.
    os.environ.get("SPEC_FIRE_PRESIGN_MAXFEE_MULT", "20")))


# #103/Ш19: ПОЛ ПЕРЕСБОРКИ — отдельно от резерва подписи.
#
# Множитель резерва (20) отвечает на вопрос «какой запас закладываем, КОГДА подписываем».
# Этот — на вопрос «когда подпись становится опасной». Раньше обе роли играла одна константа,
# и полоса годности выходила ОДНОСТОРОННЕЙ: лестница, подписанная при базе b0, получает
# maxFee = b0*20 + tip, поэтому требование b*20 нарушается, едва база станет ВЫШЕ b0 — хоть на
# один wei. Вниз запас тройной, вверх нулевой.
#
# БОЕВОЕ ПОДТВЕРЖДЕНИЕ 11.08 14:28-14:52: аудит пересобрал ВСЕ 110 ног ТРИЖДЫ за 24 минуты,
# и переподписка дала 31 коллизию nonce («replacement transaction underpriced») — флот
# перетряхивался вместо того, чтобы стоять. Утром этих коллизий было НОЛЬ.
#
# Включаемость требует maxFee >= base, а не base*20. При поле 3 лестница с резервом 20 годна,
# пока база не вырастет в 6.7 раза — это смена режима, а не рябь.
_VOLLEY_RESIGN_FLOOR_MULT = int(os.environ.get("SPEC_FIRE_RESIGN_FLOOR_MULT", "3"))
if _VOLLEY_RESIGN_FLOOR_MULT >= _VOLLEY_PRESIGN_MAXFEE_MULT:
    # ИНВАРИАНТ (замечание ревьюера): пол пересборки обязан быть НИЖЕ резерва подписи. Иначе
    # свежеподписанная лестница протухшая СРАЗУ и флот уходит в вечную пересборку. Ручка
    # MAXFEE_RESERVE_MULT уже крутилась владельцем (100 -> 20), поэтому отказ ГРОМКИЙ.
    raise RuntimeError(
        "SPEC_FIRE_RESIGN_FLOOR_MULT=%d >= MAXFEE_RESERVE_MULT=%d — свежая лестница будет "
        "протухшей сразу, флот уйдёт в вечную пересборку"
        % (_VOLLEY_RESIGN_FLOOR_MULT, _VOLLEY_PRESIGN_MAXFEE_MULT))


def ladder_resign_needed(addr: str, base_fee: int, tip_wei: int) -> bool:
    """Нужна ли лестнице ПЕРЕСБОРКА по цене — независимо от того, сколько в ней записей (#63).

    Пересборка висела на ЕДИНСТВЕННОМ триггере «глубина склада < порога», а обе формы
    протухания лестницы САМИ останавливают огонь: (1) записанный max_fee вздут (гейт патрона по
    худшему случаю закрыт — отправок нет); (2) записанный max_fee ниже нынешней базы (tx вообще
    не включаема). В обоих случаях очередь не расходуется, глубина не падает, пополнение не
    зовётся — и лестница не пересобирается НИКОГДА. Разрыв защёлки из #60 по той же причине был
    недостижим: его триггер стоял за тем же гейтом глубины.

    Считается БЕЗ сети: базу вызывающий читает ОДИН раз на весь флот."""
    st = _VOLLEY_LADDER.get(addr)
    if not st or not st.get("q"):
        return False                      # пустую лестницу поднимет обычный триггер глубины
    # #127: tip вшит в raw-подпись ОДИН раз (presign_ladder), а полоса max_fee ниже его НЕ
    # видит: разница тиров ±2M wei тонет в base*MULT (~100M wei при базе 0.005 gwei). Из-за
    # этого зеркало #104 не доезжало до отправки НИКОГДА: лестницы подписаны на прогреве при
    # пустом кольце (статика), и пересборки по смене tip не наступало (замер по цепи: у всех
    # 110 ног ровно один tip за все пробы). Расхождение записанного tip с тиром ноги — теперь
    # САМОСТОЯТЕЛЬНЫЙ триггер. Отсутствие поля (лестница от кода до #127 после деплоя) = tip
    # неизвестен = протухла (незнание закрывает гард). Шторм пересборок капает существующий
    # SPEC_FIRE_AUDIT_MAX_RESIGN: сходимость флота = 110 ног / 5 за проход = 22 прохода
    # аудита, т.е. ~22с в окне (ENGINE_PRICE_AUDIT_SEC=1.0) и ~44 мин в штиле (120с).
    _st_tip = st.get("tip")
    if _st_tip is None or int(_st_tip) != int(tip_wei):
        return True
    # #135/§5: БАЙТ ПРОБЫ тоже запечён в подпись, и его расхождение с нынешним флагом —
    # САМОСТОЯТЕЛЬНЫЙ триггер. Иначе смена SPEC_FIRE_V6 оставляла бы флот с лестницами под
    # чужой контракт: 110 ног стреляют, ни одна не попадает, счётчик проб при этом РАСТЁТ —
    # ровно класс «мёртвый прибор с живым числом». Отсутствие поля = байт неизвестен =
    # протухла (незнание закрывает гард; в живом процессе поле ставится всегда).
    _st_cd = st.get("cd")
    if _st_cd is None or _st_cd != probe_calldata():
        return True
    want = int(base_fee) * _VOLLEY_PRESIGN_MAXFEE_MULT + int(tip_wei)
    have = int(st.get("max_fee") or 0)
    if want <= 0:
        return False
    # НИЖНЯЯ граница — по полу ВКЛЮЧАЕМОСТИ, а не по резерву подписи (иначе верхняя граница
    # годности совпадает с базой момента подписи и любой рост базы обнуляет весь флот разом).
    # Верхняя (вздутый maxFee запирает гейт патрона, #60) — прежняя.
    floor = int(base_fee) * _VOLLEY_RESIGN_FLOOR_MULT + int(tip_wei)
    return have < floor or have > want * _VOLLEY_MAXFEE_RESIGN_MULT


def _send_fail_diag(e) -> str:
    """Класс отказа рассылки + его ТЕКСТ, отредактированный от ключей провайдера.

    Ш19: печатался голый type(e).__name__, поэтому ВСЕ отказы рассылки выглядели одинаково —
    «RuntimeError». По такой строке неразличимы минимум три причины: лимит темпа секвенсера,
    «replacement transaction underpriced» от коллизии nonce и отказ транспорта. Из-за этого
    всплеск 644 отказов за минуту в каскаде 10.08 не был ПРОЧИТАН из журнала — причину ему
    назначили переносом цифры с отдельной дрели #69. Ровно тот урок, что дала сама дрель:
    цифра без ПРИЧИНЫ читается как что угодно.

    redact обязателен: текст сетевой ошибки несёт ключ в URL (урок 07.08). Проверено, что
    режется и в URL, и без схемы — литеральный реестр ловит второе."""
    try:
        from chain.rpc import redact
        return "%s: %s" % (type(e).__name__, redact(e)[:200])
    except Exception:
        return type(e).__name__


def ladder_depth(addr: str) -> int:
    """Сколько готовых проб осталось у ноги (0 = лестницы нет)."""
    st = _VOLLEY_LADDER.get(addr)
    return len(st["q"]) if st else 0


def ladder_tip(addr: str):
    """#127: tip, которым подписана лестница ноги. None = лестницы нет или поле отсутствует
    (лестница от кода до #127). Чтение для НАБЛЮДАЕМОСТИ (ветка аудита) — денег не трогает."""
    st = _VOLLEY_LADDER.get(addr)
    return st.get("tip") if st else None


# #127: счётчик «пересборка по tip» — прибор СБОКУ (урок 13.08: счётчик атрибуции внутри
# _prearm_board молча гасил арм). Зовётся ТОЛЬКО из ветки аудита spec_fire_live под её
# собственным except; ни подписи, ни отправки, ни лестницы не трогает.
_TIP_RESIGN_COUNT = 0
_TIP_RESIGN_LAST = None


def note_tip_resign(addr, old_tip, new_tip):
    """Учёт ноги, чью лестницу аудит пересобирает из-за смены tip (старый -> новый)."""
    global _TIP_RESIGN_COUNT, _TIP_RESIGN_LAST
    _TIP_RESIGN_COUNT += 1
    _TIP_RESIGN_LAST = (str(addr), old_tip, int(new_tip))


def invalidate_ladder(addr: str = None):
    """Сбросить лестницу (смена nonce вне лестницы, NO-RECEIPT, пересборка)."""
    if addr is None:
        _VOLLEY_LADDER.clear()
    else:
        _VOLLEY_LADDER.pop(addr, None)


def presign_ladder(rpc, cfg, addr: str, key: str, gas: int, depth: int = None,
                   base_fee: int = None, tip: int = None, log=None,
                   start_nonce: int = None) -> int:
    """Подписать подряд `depth` проб ноги (calldata = V2_PROBE_CALLDATA) начиная с её
    текущего pending-nonce. Возврат: сколько записей в лестнице ПОСЛЕ вызова.

    Зовётся ФОНОМ (взвод окна), не из горячего пути: подпись — это CPU, и весь смысл в том,
    чтобы её там не было. Идемпотентна: лестница той же формы не пересобирается.

    Здесь же греется кэш патрона (#45): путь холодный, RTT допустим, и нога выходит в окно
    с ИЗВЕСТНЫМ балансом — иначе первый же выстрел уткнулся бы в fail-closed гейт.

    ПОДПИСЬ ВНЕ ЛОКА НОГИ (#112, 14.08 — ПРАВКА-4 REVIEW_ROUND3). Раньше ВЕСЬ проход (чтение
    базы, подпись `depth−d` проб, установка) шёл под `with lk`. Подпись — единственная дорогая
    часть: на native 4.27мс/проба × (40−12) = ~120мс немоты ноги за долив, и всё это время
    огонь ЭТОЙ ноги (send_presigned_probe берёт тот же lk на pop) заблокирован. Теперь три фазы:
      1) под локом — снять снимок и решить план (compat/пересборка, старт nonce, сколько
         подписать). Сети под локом НЕТ;
      2) ВНЕ лока — собственно подпись (CPU). Огонь ноги в это время идёт;
      3) под локом — сверить, что снимок не устарел за время подписи, и установить. При любом
         расхождении подписанное отбрасывается, лог, следующий проход пересоберёт (незнание
         закрывает гард — не ставим сомнительное, чтобы не открыть дыру в nonce).
    Дисциплина порядка (пополнение не лезет в лестницу, из которой стреляют) СОХРАНЕНА: установка
    под локом и валидируется; фаза подписи чужой лестницы не трогает.
    """
    if volley_bal_stale(addr):
        volley_bal_warm(rpc, addr, log=log)
    lk = _VOLLEY_LOCKS.get(addr)
    if lk is None:
        return 0
    depth = int(depth or _VOLLEY_PRESIGN_DEPTH)
    w3 = rpc._web3()
    # Ш24: у каждой ноги СВОЙ тир (см. probe_tip_for). Раньше здесь стояла одна константа на
    # весь флот, и мы были ниже класса в 56% блоков — то есть в хвосте за всем полем.
    tip_wei = int(tip) if tip else probe_tip_for(addr)
    acct = account_for(w3, key)          # чистая крипта (from_key, кэш) — вне лока
    # СЕТЬ ВНЕ ЛОКА (#112): держать lk на RTT get_block значило бы немоту огня ноги на RTT.
    if base_fee is None:
        base_fee = int(w3.eth.get_block("latest").get("baseFeePerGas") or 0)
    max_fee = base_fee * _VOLLEY_PRESIGN_MAXFEE_MULT + tip_wei
    # #135/§5: байт пробы — ЧАСТЬ ФОРМЫ лестницы (он запечён в подпись). Снимается ОДИН раз на
    # проход: смена флага в середине подписи дала бы лестницу из двух разных байтов.
    _cd_byte = probe_calldata()

    def _sign_run(n0, count):
        out = []
        for i in range(count):
            t = {"from": addr, "to": cfg.liquidator_address, "data": _cd_byte,
                 "nonce": n0 + i, "gas": int(gas), "chainId": rpc.chain_id,
                 "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip_wei}
            signed = acct.sign_transaction(t)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = getattr(signed, "hash", None)
            out.append((n0 + i, "0x" + bytes(raw).hex(), h.hex() if h is not None else None))
        return out

    # ---- ФАЗА 1: снимок + решение (под локом, без сети, без подписи) ----
    with lk:
        st = _VOLLEY_LADDER.get(addr)
        # ЗАЩЁЛКА max_fee (ревью 08.08, CONFIRMED). Лестница, переподписанная на всплеске
        # basefee, несёт вздутый max_fee НАВСЕГДА: гейт по худшему случаю (gas x max_fee) при
        # нём закрыт -> отправок нет -> склад остаётся полным -> пополнение ногу не трогает ->
        # max_fee никогда не пересчитывается ВНИЗ. Нога немела до рестарта даже после того, как
        # сеть успокоилась. Поэтому «годной» считаем лестницу, чей max_fee не только НЕ НИЖЕ
        # текущего, но и не абсурдно выше: сильно подорожавшую пересобираем по нынешней цене.
        _latched = bool(st and max_fee > 0
                        and int(st["max_fee"]) > int(max_fee) * _VOLLEY_MAXFEE_RESIGN_MULT)
        # #135: БАЙТ — часть формы. Лестница, подписанная под 0x01, на контракте v6 — это 110
        # ног, стреляющих в никуда (v6 читает 0x01 как «явная группа 0», а при флаге мы целим
        # спан через _hot). Долив такой лестницы смешал бы два байта в одном складе.
        compat = bool(st and st["gas"] == int(gas) and st["max_fee"] >= max_fee and not _latched
                      and st.get("cd") == _cd_byte)
        if _latched and log:
            log.info("VOLLEY лестница %s: max_fee %.4f gwei против нынешних %.4f — пересборка "
                     "(иначе гейт патрона держал бы ногу немой до рестарта)",
                     addr[:10], int(st["max_fee"]) / 1e9, max_fee / 1e9)
        if compat and len(st["q"]) >= depth:
            return len(st["q"])          # годная лестница уже есть — подпись не жжём
        _n_snap = int(_VOLLEY_NONCE.get(addr, 0))   # для ловли выстрела во время подписи
        if compat and start_nonce is None:
            # ДОТОЧКА СКЛАДА, а не пересборка. Две причины, и вторая важнее первой:
            #  1) уже подписанное не переподписываем — подпись это CPU на пробу (0.473мс COIN),
            #     а фоновое пополнение уступает огню;
            #  2) продолжать надо с ХВОСТА очереди (или с локального счётчика, если склад
            #     выбран досуха). Пересборка от pending-nonce здесь была бы прямой ДЫРОЙ:
            #     pending отстаёт от уже разосланных проб, и новая лестница начиналась бы
            #     с занятых номеров — вся нога повисла бы до закрытия дыры.
            _mode = "topup"
            _st_id = id(st)
            _snap_gas = st["gas"]
            _snap_max_fee = st["max_fee"]
            # #127: долив подписывается tip'ом ЛЕСТНИЦЫ, а не свежим probe_tip_for — иначе в
            # одной лестнице мешаются два tip и поле "tip" лжёт. Смена tip идёт ТОЛЬКО через
            # пересборку (её триггерит аудит цены: ladder_resign_needed). Поле отсутствует
            # (лестница от кода до #127) => tip неизвестен => НЕ доливаем, возвращаем текущую
            # глубину — пересборку сделает аудит (незнание закрывает гард). Явный параметр
            # tip= ведёт себя как раньше (контракт вызова не тронут).
            if tip is None:
                _snap_tip = st.get("tip")
                if _snap_tip is None:
                    if log:
                        log.info("VOLLEY долив %s пропущен: лестница без поля tip (код до "
                                 "#127) — пересборку сделает аудит", addr[:10])
                    return len(st["q"])
            else:
                _snap_tip = None
            _tail = st["q"][-1][0] if st["q"] else None
            nxt = (_tail + 1) if _tail is not None else None   # пустой склад: pending вне лока
            count = depth - len(st["q"])
        else:
            _mode = "rebuild"
            if start_nonce is not None:
                nxt = int(start_nonce)
            elif addr in _VOLLEY_NONCE:
                # #112 R2 (стенка 110×RTT): локальный счётчик АВТОРИТЕТЕН, ЕСЛИ он присутствует.
                # Он поднимается на КАЖДЫЙ send и СНИМАЕТСЯ (pop) на КАЖДОМ событии рассинхрона
                # с цепью — NO-RECEIPT (volley_invalidate_nonce) и отказе рассылки (см. ~1247).
                # invalidate_ladder счётчик НЕ трогает. Значит присутствие ключа = все наши
                # отправки учтены = старт пересборки известен БЕЗ серийного get_transaction_count.
                # Прежняя редакция читала ЦЕПЬ на КАЖДОЙ протухшей ноге: на каскаде база прыгает,
                # 110 лестниц протухают ПО ЦЕНЕ (nonce НЕ менялся), и аудит делал 110 серийных
                # _pending_nonce = стенка 110×RTT (~2.2-8.8с) > окна каскада 2-4с — флот нем в
                # решающем окне + RTT-бёрст в боевой ключ. Локальный старт снимает стенку. Он и
                # КОРРЕКТНЕЕ chain-pending (тот же аргумент, что у ветки topup ниже: pending
                # отстаёт от уже разосланных проб — дыра занятых номеров). Отсутствие
                # ключа = рассинхрон был => ресинк С ЦЕПИ ниже (незнание закрывает:
                # слепо не стреляем, читаем цепь). _n_snap выше снят из ТОГО ЖЕ счётчика:
                # nxt == _n_snap, и фаза-3 отбросит установку, если выстрел во время подписи
                # сдвинул счётчик (дрель A4).
                nxt = _n_snap
            else:
                nxt = None
            count = depth

    if count <= 0:
        return ladder_depth(addr)
    if _mode == "topup" and _snap_tip is not None:
        # #127: долив несёт tip лестницы (см. фазу 1) — переопределяем ДО подписи. max_fee
        # пересчитывается тем же tip, чтобы подписанные байты не разъезжались с полем.
        tip_wei = int(_snap_tip)
        max_fee = base_fee * _VOLLEY_PRESIGN_MAXFEE_MULT + tip_wei
    # pending читаем ВНЕ лока — только если план его требует (пустая доточка / пересборка с
    # рассинхронённым счётчиком). Обычная пересборка по ЦЕНЕ берёт локальный старт выше (0 RTT).
    if nxt is None:
        # доточка досуха продолжает с ХВОСТА (локальный счётчик), пересборка БЕЗ локального
        # счётчика (его снял NO-RECEIPT/отказ рассылки) — ресинк С ЦЕПИ.
        if _mode == "topup":
            nxt = max(int(_VOLLEY_NONCE.get(addr, 0)), _pending_nonce(w3, addr))
        else:
            nxt = _pending_nonce(w3, addr)

    # ---- ФАЗА 2: ПОДПИСЬ ВНЕ ЛОКА (весь CPU; огонь ноги НЕ заблокирован) ----
    signed = _sign_run(nxt, count)

    # ---- ФАЗА 3: сверить снимок и установить (под локом) ----
    with lk:
        st = _VOLLEY_LADDER.get(addr)
        if _mode == "topup":
            if (st is None or id(st) != _st_id or st["gas"] != _snap_gas
                    or int(st["max_fee"]) != int(_snap_max_fee)):
                if log:
                    log.info("VOLLEY доточка %s отменена: лестница пересобрана параллельно",
                             addr[:10])
                return ladder_depth(addr)
            if st["q"]:
                if st["q"][-1][0] + 1 != nxt:      # хвост уехал (чужая доточка) — сброс
                    if log:
                        log.info("VOLLEY доточка %s отменена: хвост уехал (ждали nonce %d)",
                                 addr[:10], nxt)
                    return len(st["q"])
                st["q"].extend(signed)
            else:
                # склад слили ДОСУХА, пока подписывали. nxt = old_tail+1: номера этой лестницы
                # кончались на old_tail, поэтому _VOLLEY_NONCE не мог обогнать nxt из НЕЁ; если
                # обогнал — была иная пересборка, сброс.
                if int(_VOLLEY_NONCE.get(addr, 0)) > nxt:
                    if log:
                        log.info("VOLLEY доточка %s отменена: счётчик обогнал (nonce %d)",
                                 addr[:10], nxt)
                    return 0
                st["q"] = list(signed)
            st["ts"] = time.monotonic()
            if log:
                log.info("VOLLEY склад %s доточен до %d проб (с nonce %d)",
                         addr[:10], len(st["q"]), nxt)
            return len(st["q"])
        # пересборка. Выстрел, прошедший за время подписи ВНЕ лока, поднял бы _VOLLEY_NONCE —
        # и установка с нашего (уже занятого) старта дала бы «replacement underpriced» (серия
        # 85 за 10 мин, инцидент 11.08). Явный start_nonce задаёт вызывающий — ему и решать.
        if start_nonce is None and int(_VOLLEY_NONCE.get(addr, 0)) != _n_snap:
            if log:
                log.info("VOLLEY пересборка %s отменена: выстрел во время подписи сдвинул "
                         "счётчик (%d -> %d), старт %d занят",
                         addr[:10], _n_snap, int(_VOLLEY_NONCE.get(addr, 0)), nxt)
            return ladder_depth(addr)
        # #127: "tip" — часть формы лестницы. Аудит цены сверяет его с тиром ноги и триггерит
        # пересборку при расхождении (полоса max_fee разницу тиров ±2M wei не видит).
        _VOLLEY_LADDER[addr] = {"q": list(signed), "gas": int(gas), "max_fee": max_fee,
                                "tip": tip_wei, "cd": _cd_byte, "ts": time.monotonic()}
        _VOLLEY_NONCE[addr] = nxt     # локальный счётчик и лестница выходят из одной точки
        if log:
            log.info("VOLLEY лестница %s: %d проб с nonce %d, maxFee %.4f gwei",
                     addr[:10], depth, nxt, max_fee / 1e9)
        return len(signed)


def send_presigned_probe(rpc, cfg, addr: str, log=None, broadcast_raw_urls=None):
    """Огонь из лестницы: НИ подписи, НИ чтения nonce — только рассылка готовых байтов.
    Гейты: $-бакет тира и пол патрона — как у обычной ноги. Гейт «боевая tx в полёте» с 08.08
    по умолчанию НЕ действует (решение kelbic по замеру: немота p50=1.8с/tx съедала 75%
    горячего окна каскада 17:00; у 1-байт проб свои nonce-потоки и цена ~$0.0004, худший
    случай — копеечный реверт-дубль по жертве, которую уже берёт боевая tx).
    Откат: SPEC_FIRE_ENGINE_IGNORE_INFLIGHT=0 — движок снова молчит при боевой tx в полёте.
    Возврат hash|None. При неудаче запись возвращается В НАЧАЛО очереди (инвариант подряд).
    """
    global _VOLLEY_PROBES, _VOLLEY_FLOOR_SKIPS, _VOLLEY_UNKNOWN_SKIPS, _VOLLEY_GAS_RESERVED
    global _VOLLEY_RESERVE_SKIPS, _VOLLEY_INFLIGHT_SKIPS
    with _INFLIGHT_LOCK:
        _volley_roll_day_locked()
        # #132: РАЗВЕДЁННЫЕ причины отказа. Раньше обе возвращали немой None, и «флот молчит,
        # потому что бюджет тира выбран бронью» было неотличимо от «склад пуст» — то есть
        # самый опасный отказ (немота ровно в каскаде) не имел ни одного числа.
        if (_VOLLEY_GAS_USD + _VOLLEY_GAS_RESERVED) >= CASCADE_TIER_GAS_USD:
            _VOLLEY_RESERVE_SKIPS += 1
            return None
        if _INFLIGHT > 0 and not _ENGINE_PROBE_IGNORE_INFLIGHT:
            _VOLLEY_INFLIGHT_SKIPS += 1
            return None
    lk = _VOLLEY_LOCKS.get(addr)
    if lk is None:
        return None
    with lk:
        st = _VOLLEY_LADDER.get(addr)
        if not st or not st["q"]:
            return None
        # ГЕЙТ ПАТРОНА (#45): незнание ЗАКРЫВАЕТ гард. Кэша нет или он старше предельного
        # возраста -> пробу НЕ шлём и будим дозор вне пути огня (синхронный get_balance здесь
        # стоил бы RTT в горячем окне). Кэш свежий-но-протухший по TTL: стреляем по известному
        # числу и обновляем фоном — молчать весь RTT в горячем окне дороже, чем ошибиться на
        # расход одной пробы (~$0.0004 при гейте по ХУДШЕМУ случаю).
        bal = _VOLLEY_BAL.get(addr)
        _age = (time.monotonic() - bal[1]) if bal is not None else None
        if bal is None or _age > _VOLLEY_BAL_MAX_AGE:
            with _INFLIGHT_LOCK:
                _VOLLEY_UNKNOWN_SKIPS += 1
            if rpc is not None:
                volley_bal_refresh_async(rpc, addr)
            if log:
                log.info("VOLLEY %s молчит: патрон НЕИЗВЕСТЕН (кэш %s) — дозор разбужен",
                         addr[:10], "пуст" if bal is None else "возраст %.0fс" % _age)
            return None
        if _age > _VOLLEY_BAL_TTL and rpc is not None:
            volley_bal_refresh_async(rpc, addr)       # фоном, огонь не ждёт
        # гейт по ХУДШЕМУ случаю: нога обязана быть способна оплатить полный выигрыш
        if bal[0] - int(st["gas"]) * int(st["max_fee"]) < _VOLLEY_BAL_FLOOR_WEI:
            with _INFLIGHT_LOCK:
                _VOLLEY_FLOOR_SKIPS += 1
                _VOLLEY_MAXFEE_MUTE.add(addr)   # У2: немота по комиссии обязана КРИЧАТЬ, иначе
                                                # в горячий каскад флот молча встанет, а мы
                                                # прочтём это как «не было целей»
            # #65 (ревью 09.08): дозор здесь НЕ будим. Немоту по полу определяет РЕЗЕРВ
            # (gas x max_fee), а не свежесть баланса — обновление ничего не меняет, зато на
            # армированном окне даёт устойчивый поток get_balance в боевой ключ (потолок
            # ограничивал ЧИСЛО одновременных дозоров, но не ТЕМП: они завершаются за RTT и
            # спавнятся снова). Долив ноги увидит штатный флотский прогрев раз в 60с.
            if log:
                log.info("VOLLEY %s молчит: патрон на поле %.4f ETH", addr[:10], bal[0] / 1e18)
            return None
        nonce, raw_hex, h = st["q"].pop(0)
    # ПУТЬ РАССЫЛКИ ВЫБРАН ЗАМЕРОМ (07.08): тёплая сессия до СЕКВЕНСЕРА держит тред 32.5мс
    # (p50, 25 проб), синхронная нога Alchemy — 60мс. Секвенсер и есть ордерер Base, а hash
    # пред-подписанной пробы известен из подписи, поэтому ответ Alchemy не нужен ВООБЩЕ.
    # Отсюда потолок: 16 тредов x 1/0.0325 = ~493 отправки/с = ~986 проб/блок против прежних
    # 267 (насыщение окна победы — 1480).
    # #136: обход ВСЕХ каналов по порядку. HEAD брал только broadcast_raw_urls[0]; теперь на
    # каждом канале try-take из его ведра -> отправка, spill на следующий И при pacer_dry, И при
    # «over rate limit» (повтор тех же подписанных байт на другом канале идемпотентен). Каждый
    # канал пробуется максимум один раз. Все отказали -> запись В НАЧАЛО очереди (как HEAD).
    if not _VOLLEY_PACER_ON:
        # ---- ОТКАТ (VOLLEY_PACER=off): путь HEAD БИТ-В-БИТ (первый url, одна попытка, без вёдер).
        # СЦЕПКА РУЧЕК, названная вслух (20.08): здесь рассылка идёт НЕПРИВЯЗАННОЙ, то есть
        # VOLLEY_PACER=off отключает и второй egress-IP. Это не упущение: второй адрес нужен
        # ровно ради ВТОРОГО ВЕДРА, а вёдер в этой ветке нет — облегчать нечего. Помнить об
        # этом надо при откате пейсера: он молча снимает и выигрыш второго канала.
        try:
            _seq = None
            for _u in (broadcast_raw_urls or []):
                _seq = _u
                break
            if _seq is not None:
                ok, err = _post_raw_sync(_seq, raw_hex)
                if not ok:
                    raise RuntimeError(err or "sequencer post failed")
            else:
                rpc._web3().eth.send_raw_transaction(raw_hex)
        except Exception as e:
            _msg = str(e).lower()
            if "already known" not in _msg and "nonce too low" not in _msg:
                with lk:                      # НЕ пропускаем nonce: дыра повесит всю ногу
                    st = _VOLLEY_LADDER.get(addr)
                    if st is not None:
                        st["q"].insert(0, (nonce, raw_hex, h))
                if log:
                    # #104: контекст ОТКАЗА, чтобы боевое окно САМО было замером и дрель не
                    # требовалась. Питоновский логгер уже даёт миллисекунды, поэтому связка
                    # (мс, тред, нога, nonce) позволяет ПОСЧИТАТЬ мгновенную интенсивность и
                    # форму трафика: пачка это или поток, сколько ног в пиковом окне, растёт ли
                    # очередь будущих nonce. Без этого мы считали отказы посекундными вёдрами и
                    # чуть не приняли пик 193/с за истину — при том что 193 отказа за 50мс это
                    # 3860/с. Секвенсер режет по ПИКУ, а среднее за секунду его не описывает.
                    log.info("VOLLEY лестница %s: рассылка не удалась (%s), запись возвращена"
                             " [nonce=%d тред=%s глубина=%d]",
                             addr[:10], _send_fail_diag(e), int(nonce),
                             threading.current_thread().name, ladder_depth(addr))
                return None
            # «already known»/«nonce too low» — байты УЖЕ у секвенсера (SEND-3 обогнал):
            # запись потрачена по назначению, возвращать её нельзя.
    else:
        # ---- ПЕЙСЕР+SPILL: каждый канал ровно раз; первый — первичное ведро, остальные — запасное.
        # 20.08: обходим КАНАЛЫ (url, ip), а не url. Первичный эндпоинт размножен по egress-
        # адресам — у каждого СВОЁ ведро, потому что квота секвенсера меряется на IP.
        _chans = broadcast_channels(list(broadcast_raw_urls or []))
        _sent = False
        _terminal = False           # байты уже у ордерера (already known/nonce too low): потрачено
        _last_err = None
        if _chans:
            _head_url = _chans[0][0]
            for _i, (_u, _ip) in enumerate(_chans):
                _ck = _chan_key(_u, _ip)
                if _i == 0 and _ip is None:
                    # первичный канал БЕЗ привязки: вызов БИТ-В-БИТ как в HEAD
                    # (_post_raw_sync(url, raw_hex)), чтобы транспортные стенды с сигнатурой
                    # (url, raw_hex, timeout) не ломались.
                    ok, err = _post_raw_sync(_u, raw_hex)
                elif _u == _head_url:
                    # ТОТ ЖЕ эндпоинт с другого адреса — это НЕ запасной канал, а вторая полоса
                    # первичного: полоса определяется эндпоинтом, а не позицией в списке.
                    ok, err = _post_raw_sync(_u, raw_hex, paced=True, rate=_PACER_RATE,
                                             burst=_PACER_BURST, source_ip=_ip)
                else:
                    ok, err = _post_raw_sync(_u, raw_hex, paced=True,
                                             rate=_SPILL_RATE, burst=_SPILL_BURST, source_ip=_ip)
                if ok:
                    _sent = True
                    if _i > 0:
                        _pacer_note_spill(_ck)     # доставлено НЕ первым каналом
                    break
                _e = (err or "").lower()
                if "already known" in _e or "nonce too low" in _e:
                    _terminal = True               # потрачено по назначению — не возвращаем
                    break
                if err == "pacer_dry":
                    _pacer_note_dry(_ck)           # сухо: спилл без сети (причина РАЗДЕЛЬНО от reject)
                else:
                    _pacer_note_reject(_ck)        # «over rate limit» / мёртвый адрес — сетевой отказ
                _last_err = err
                # spill на следующий канал (та же подписанная tx идемпотентна)
        else:
            try:
                rpc._web3().eth.send_raw_transaction(raw_hex)
                _sent = True
            except Exception as e:
                _e = str(e).lower()
                if "already known" in _e or "nonce too low" in _e:
                    _terminal = True
                else:
                    _last_err = str(e)
        if not _sent and not _terminal:
            with lk:                      # НЕ пропускаем nonce: дыра повесит всю ногу
                st = _VOLLEY_LADDER.get(addr)
                if st is not None:
                    st["q"].insert(0, (nonce, raw_hex, h))
            if log:
                # #104: тот же контекст (мс/тред/нога/nonce/глубина) — боевое окно САМО замер.
                log.info("VOLLEY лестница %s: рассылка не удалась (%s), запись возвращена"
                         " [nonce=%d тред=%s глубина=%d]",
                         addr[:10], _send_fail_diag(RuntimeError(_last_err or "все каналы сухи/отказ")),
                         int(nonce), threading.current_thread().name, ladder_depth(addr))
            return None
    with lk:
        _VOLLEY_NONCE[addr] = nonce + 1
        if addr in _VOLLEY_MAXFEE_MUTE:
            with _INFLIGHT_LOCK:
                _VOLLEY_MAXFEE_MUTE.discard(addr)   # ожила (долив/пересборка лестницы)
        # ДЕКРЕМЕНТ ПО ФАКТУ (#45): эстимейт здесь завышал расход no-op пробы в ~250 раз и
        # глушил ногу за ~135 проб. Истину приносит коллектор квитанций (volley_bal_charge),
        # а верхнюю границу дрейфа держит дозор по возрасту кэша.
    with _INFLIGHT_LOCK:
        _VOLLEY_PROBES += 1
        # #93: БРОНЬ В МОМЕНТ ОТПРАВКИ. Гейт огня решает по ФАКТ+БРОНЬ, поэтому магазин
        # не может уйти в перелёт быстрее, чем коллектор успевает считать. Бронируем по
        # ХУДШЕМУ случаю (проба после срабатывания триггера), квитанция её освободит и
        # запишет истину — суточный ФАКТ от этого не растёт.
        try:
            _res = (_VOLLEY_PROBE_GAS_WORST * int(st["max_fee"])
                    * float(getattr(cfg, "eth_price_usd", 0.0) or 0.0) / 1e18)
            _VOLLEY_RESERVE_BY_HASH[h] = max(0.0, _res)
            _VOLLEY_GAS_RESERVED += max(0.0, _res)
        except Exception:
            pass
    return h


# --- #136: token-bucket ПО ЭНДПОИНТУ (proxyd секвенсера режет «over rate limit» ПО ПИКУ: лимитер
# фикс-окна ~2с = блок Base; пачка в голове блока выжигает квоту за ~0.7с). Ведро — монотонные часы,
# пополнение ЛЕНИВОЕ при try-take, O(1). Никаких тредов, никакого sleep под локом. Дефолты:
# секвенсер 200/с burst 50; запасные каналы 50/с burst 10. Всё настраиваемо env; VOLLEY_PACER=off
# снимает МЕХАНИЗМ целиком (поведение HEAD бит-в-бит). Причины отказа РАЗДЕЛЬНО (дисциплина #132):
# pacer_dry (нет токенов, без сети) vs transport_reject («over rate limit» и прочие сетевые).
_VOLLEY_PACER_ON = (os.environ.get("VOLLEY_PACER", "on") or "on").lower() not in ("0", "off", "false", "no")
_PACER_RATE = float(os.environ.get("VOLLEY_PACER_RATE", "200"))     # секвенсер (первичный канал)
_PACER_BURST = float(os.environ.get("VOLLEY_PACER_BURST", "50"))
_SPILL_RATE = float(os.environ.get("VOLLEY_SPILL_RATE", "50"))      # запасные каналы
_SPILL_BURST = float(os.environ.get("VOLLEY_SPILL_BURST", "10"))
_PACER_LOCK = threading.Lock()
# 20.08: ключ — КАНАЛ (url|ip), а не url. Иначе оба адреса делили бы одно ведро и второй
# egress-IP не дал бы ничего: смысл второй корзины ровно в том, что она ОТДЕЛЬНАЯ.
# Без VOLLEY_EGRESS_IPS ключ канала == url (см. _chan_key) => ключи прежние посимвольно.
_PACER_BUCKETS: dict = {}          # канал -> [tokens, last_monotonic]
_PACER_DRY: dict = {}              # канал -> сколько раз ведро было сухим (спилл без сети)
_PACER_SPILL_SENT: dict = {}       # канал -> сколько проб доставлено ЗАПАСНЫМ каналом
# transport_reject тоже ПЕР-КАНАЛЬНЫЙ: суммарное число не отвечало на вопрос «какой канал
# отказывает». С двумя IP это тот самый вопрос — умер ли адрес или режет секвенсер.
_PACER_TRANSPORT_REJECT: dict = {}  # канал -> сетевых отказов (over rate limit и пр.)


def _pacer_take(url: str, rate: float, burst: float) -> bool:
    """O(1) try-take: ленивое пополнение по монотонным часам. Токен есть -> True (списываем)."""
    now = time.monotonic()
    with _PACER_LOCK:
        tok, last = _PACER_BUCKETS.get(url, (float(burst), now))
        tok = min(float(burst), tok + max(0.0, now - last) * float(rate))
        if tok >= 1.0:
            _PACER_BUCKETS[url] = [tok - 1.0, now]
            return True
        _PACER_BUCKETS[url] = [tok, now]
        return False


def _pacer_note_dry(url: str):
    with _PACER_LOCK:
        _PACER_DRY[url] = _PACER_DRY.get(url, 0) + 1


def _pacer_note_spill(url: str):
    with _PACER_LOCK:
        _PACER_SPILL_SENT[url] = _PACER_SPILL_SENT.get(url, 0) + 1


def _pacer_note_reject(chan: str = ""):
    with _PACER_LOCK:
        _PACER_TRANSPORT_REJECT[chan or "?"] = _PACER_TRANSPORT_REJECT.get(chan or "?", 0) + 1


def volley_pacer_stats():
    """#136: (pacer_dry, spill_sent, transport_reject) — ВСЕ ТРИ по каналам, с момента старта.
    Как volley_budget_skips (#132): без числа спилл и немота эндпоинта неотличимы от «склад
    пуст». Причины РАЗДЕЛЬНО — pacer_dry (без сети) vs transport_reject (сетевой отказ).

    20.08: третий элемент стал СЛОВАРЁМ (был int). Суммарное число не отвечало на вопрос,
    который появляется вместе со вторым egress-IP: отказывает КАНАЛ (адрес умер) или режет
    секвенсер. sum(значений) даёт прежнюю величину, если она понадобится."""
    with _PACER_LOCK:
        return (dict(_PACER_DRY), dict(_PACER_SPILL_SENT), dict(_PACER_TRANSPORT_REJECT))


def _post_raw_sync(url: str, raw_hex: str, timeout: int = 5,
                   paced: bool = True, rate: float = None, burst: float = None,
                   source_ip: str = None):
    """Синхронная рассылка raw в секвенсер тёплой сессией. Возврат (ok, err_msg).
    В отличие от _post_raw_tx (best-effort в треде, глотает всё) ЗДЕСЬ исход нужен вызывающему:
    от него зависит, вернуть ли запись лестницы на место или считать её потраченной.

    #136 ПЕЙСЕР: paced=True и VOLLEY_PACER=on -> сначала token-bucket ПО url. Нет токенов ->
    (False, 'pacer_dry') БЕЗ сетевого похода, вызывающий мгновенно спиллит на следующий канал.
    paced=False — ПРИОРИТЕТНАЯ ПОЛОСА: боевая tx не пейсится НИКОГДА (в этот чокпоинт она и не
    приходит — бой идёт через _post_raw_tx, — но флаг делает инвариант явным и проверяемым)."""
    _ck = _chan_key(url, source_ip)            # ведро/счётчики — НА КАНАЛ; без ip ключ == url
    if paced and _VOLLEY_PACER_ON:
        _r = _PACER_RATE if rate is None else rate
        _b = _PACER_BURST if burst is None else burst
        if not _pacer_take(_ck, _r, _b):
            return False, "pacer_dry"          # сухо: отдельный сигнал вызывающему, без RTT
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                   "params": [raw_hex]}
        # Непривязанный канал зовёт шов ОДНИМ аргументом — ровно как раньше: стенды подменяют
        # _seq_session лямбдой от одного url, и расширение сигнатуры не имеет права их ломать.
        s = _seq_session(url) if source_ip is None else _seq_session(url, source_ip)
        if s is None:
            return False, "no session"
        resp = s.post(url, json=payload, timeout=timeout).json()
        err = resp.get("error")
        if err:
            return False, str(err.get("message", ""))
        return True, None
    except Exception as e:
        return False, type(e).__name__


class ReceiptsUnavailable(RuntimeError):
    """Узел НЕ ответил на партию квитанций. Это «не знаю», а НЕ «ещё не намайнено».

    Существует потому, что различать эти два исхода пустым словарём НЕЛЬЗЯ (#101): прежняя
    редакция клала в словарь только пришедшие квитанции, поэтому «узел молчит» и «ни одна из
    64 проб ещё не в блоке» давали одинаковый {} — и страховка боевым Alchemy срабатывала на
    КАЖДОМ полле. Отличие обязано быть в типе исхода, а не в соглашении о размере словаря.

    Секрет наружу не выносим: в сообщении только ХОСТ и причина, ключ живёт в пути URL.
    """

    def __init__(self, reason: str, host: str = ""):
        self.reason = str(reason)[:120]
        self.host = host or "?"
        super().__init__("%s: %s" % (self.host, self.reason))


def _url_host(url: str) -> str:
    """Хост без пути. В пути URL живёт ключ провайдера — в текст исключения он не попадает."""
    try:
        return str(url).split("//")[-1].split("/")[0] or "?"
    except Exception:
        return "?"


def _note_batch_cu(url: str, n: int):
    """Батч под общий учёт CU. У Alchemy КАЖДЫЙ подзапрос батча тарифицируется отдельно
    (64 квитанции = 64 x 15 CU), поэтому счётчику передаём число подзапросов, а не постов.
    Считаем ДО отправки: недосчёт прячет дыру, а ради этого счётчик и заводится."""
    try:
        from chain import rpc as _rpc
        _rpc.rpc_usage_note("eth_getTransactionReceipt", url, n)
    except Exception:
        pass


def batch_receipts(url: str, hashes, timeout: int = 5, chunk: int = 0):
    """Партия eth_getTransactionReceipt (одним HTTP-запросом либо нарезкой по `chunk`).

    НАРЕЗКА (#101-b, замер 12.08). Узел-коллектор `lb.drpc.live` принимает JSON-RPC-массив
    длиной 1-3 и отдаёт HTTP 500 начиная с ЧЕТЫРЁХ (граница проверена повтором: n=3 принят
    дважды, n=4 отказ). Наш COLLECT_BATCH=64, поэтому увод #99 отказывал на 100% поллов с
    самого деплоя и молча уходил на платный Alchemy: collector_fallback РАВЕН polls в каждом
    из 134 окон. Это не «узел лёг» — это несовместимость размера партии, и лечится она
    нарезкой, а не страховкой.

    `chunk<=0` — прежнее поведение (весь список одним постом; так ходим в Alchemy, он держит
    большие батчи). `chunk>0` — режем на куски такой длины. Отказ ЛЮБОГО куска = отказ всей
    партии (ReceiptsUnavailable): полупустой результат хуже честного «не знаю», потому что
    ненайденный хэш коллектор досчитывает до таймаута и инвалидирует nonce ноги.

    КОНТРАКТ (#101): возвращается словарь, где КАЖДЫЙ запрошенный хэш присутствует ключом;
    значение — квитанция либо None («запрошен, но ещё не намайнен»). Отказ транспорта
    словарём НЕ выражается вовсе — он поднимает ReceiptsUnavailable.

    Зачем так. Вызывающий (коллектор движка) на «пусто» уводит партию на боевой Alchemy, и
    ровно на этом различии держится весь увод нагрузки #99. Прежняя редакция обещала в
    комментарии «{hash: None}», но клала в словарь только `if r is not None` — обещанного
    ключа не появлялось НИКОГДА, и «не намайнено» было неотличимо от «узел лёг».

    При сотнях проб в секунду поштучный полл — это и есть тот самый шторм, который мы себе
    уже устраивали; батч стоит один round-trip, но тарифицируется поштучно (см. _note_batch_cu).
    """
    hs = list(hashes or [])
    if not hs:
        return {}
    host = _url_host(url)
    chunk = int(chunk or 0)
    if chunk > 0 and len(hs) > chunk:
        # Нарезка: каждый кусок — отдельный пост через ЭТУ ЖЕ функцию (chunk=0), поэтому
        # разбор ответа, учёт CU и классификация отказа остаются в одном месте. Отказ куска
        # летит наружу как есть — партия целиком объявляется неизвестной.
        out = {}
        for i in range(0, len(hs), chunk):
            out.update(batch_receipts(url, hs[i:i + chunk], timeout=timeout, chunk=0))
        return out
    out = {h: None for h in hs}                    # контракт: ключ есть у КАЖДОГО запрошенного
    s = _seq_session(url)
    if s is None:
        raise ReceiptsUnavailable("нет http-сессии (requests недоступен)", host)
    payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionReceipt",
                "params": [h]} for i, h in enumerate(hs)]
    _note_batch_cu(url, len(hs))
    try:
        resp = s.post(url, json=payload, timeout=timeout).json()
    except Exception as e:
        raise ReceiptsUnavailable("транспорт %s" % type(e).__name__, host) from None
    if not isinstance(resp, list):
        # Узел ответил одиночной ошибкой на батч (или отдал не-JSON-RPC тело). Батч не принят —
        # это отказ узла, а не «квитанций нет». Именно так молчал dRPC-коллектор (12.08).
        _msg = ""
        try:
            _msg = str((resp or {}).get("error", {}).get("message", ""))[:80]
        except Exception:
            pass
        raise ReceiptsUnavailable("батч не принят %s" % (_msg or type(resp).__name__), host)
    seen = 0
    errs = 0
    for item in resp:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("id"))
        except Exception:
            continue
        if not (0 <= i < len(hs)):
            continue
        seen += 1
        if item.get("error"):
            errs += 1                              # поэлементный отказ узла: НЕ «не намайнено»
            continue
        out[hs[i]] = item.get("result")            # None = запрошен, но ещё не в блоке
    if seen == 0 or errs == seen:
        raise ReceiptsUnavailable("ни одного пригодного ответа (%d ошибок из %d)" % (errs, seen),
                                  host)
    return out


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


# ПОСТ-КОНТРОЛЬ UNCERTAIN («го» kelbic 07.08, дефект №2 вскрытия каскада 06.08): SEND-3
# uncertain = «raw в секвенсере, МОЖЕТ сесть», но до сих пор посадка считалась свершившейся
# без положительного доказательства (канон мёртвого сторожа) — потерянный tx оставлял жертву
# под дедупом _RECENT_SENDS на весь TTL 12с = всё каскадное окно, и никто не перестреливал.
# Теперь: не сел за ~5 блоков -> это ДРОП, дедуп снимается, цель снова открыта живому конвейеру
# (он перестроит с fresh sim; ре-бродкаст протухшей калдаты не делаем). Откат: SEND3_UNCERTAIN_WATCH=0.
SEND3_UNCERTAIN_WATCH = os.environ.get("SEND3_UNCERTAIN_WATCH", "1").lower() not in ("0", "false", "no")
_UNCERTAIN_DROPPED = {"n": 0}   # телеметрия (DIAG/юниты)


def _watch_uncertain(w3, bot: str, tx_nonce: int, tx_hash: str, keys, log=None,
                     checks=(3.0, 6.0, 9.5), sleep_fn=time.sleep):
    """Ждёт консьюм nonce/receipt до ~5 блоков; не сел -> снимает дедуп жертв юнита
    (только если метку не освежила более новая отправка). Возвращает "landed"/"dropped"."""
    marked = {}
    with _SEND_LOCK:
        for k in keys:
            if k in _RECENT_SENDS:
                marked[k] = _RECENT_SENDS[k]
    prev = 0.0
    for at in checks:
        sleep_fn(at - prev); prev = at
        try:
            if w3.eth.get_transaction_receipt(tx_hash) is not None:
                return "landed"
        except Exception:
            pass                                   # не найден/RPC-икота -> решает nonce ниже
        try:
            if int(w3.eth.get_transaction_count(bot)) > int(tx_nonce):
                return "landed"                    # nonce съеден = tx с этим nonce замайнен
        except Exception:
            continue
    _UNCERTAIN_DROPPED["n"] += 1
    freed = []
    with _SEND_LOCK:
        for k, ts in marked.items():
            if _RECENT_SENDS.get(k) == ts:         # более новую отправку не трогаем
                _RECENT_SENDS.pop(k, None)
                freed.append(k)
    if log:
        log.warning("SEND-3 uncertain DROPPED nonce=%d tx=%s: nonce не съеден за ~5 блоков — "
                    "дедуп снят с %d жертв (%s), цель снова открыта огню", tx_nonce, tx_hash,
                    len(freed), ",".join("%s/%s" % (m[:8], b[:8]) for m, b in freed) or "-")
    return "dropped"


def _spawn_uncertain_watch(w3, bot: str, tx_nonce: int, tx_hash: str, unit, log=None):
    """Спавн вотчера по юниту (daemon; keys снимаются под _SEND_LOCK -> гонки с метками нет)."""
    if not SEND3_UNCERTAIN_WATCH:
        return
    threading.Thread(target=_watch_uncertain,   # log позиционно: SyncThread юнитов без kwargs
                     args=(w3, bot, tx_nonce, tx_hash, [(m, b) for m, b, _p in unit], log),
                     daemon=True).start()

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
_CBXRP = "0xcb585250f852c6c6bf90434ab21a00f02833a4af"
_CBDOGE = "0xcbd06e5a2b0c65597161de254aa074e489deb510"
_CBADA = "0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c"


def _pack_path(*hops) -> bytes:
    """UniV3/Slipstream path: token (+ 3-байтовый fee/tickSpacing + token)*. Чистая (юнит)."""
    out = bytes.fromhex(hops[0][2:])
    for i in range(1, len(hops), 2):
        out += int(hops[i]).to_bytes(3, "big") + bytes.fromhex(hops[i + 1][2:])
    return out


# ПРАВИЛО КАПА (#75, замер 09.08 на блоках 49753974/49754165/49754310, публичный узел):
#   cap = min( S_budget , S_обрыв / 2 ),
#   S_budget — размер с импактом <= половины бонуса рынка (LIVE_CAP_BONUS_SHARE),
#   S_обрыв  — размер, на котором импакт достигает ПОЛНОГО бонуса (убыток по построению),
#   делитель 2 — ЗАМЕРЕННЫЙ дрейф самого обрыва (weETH 210->100 за день; cbDOGE 135k->65k
#   за две недели; cbADA 116k ток. дал 5.84% и 8.14% на блоках в 5 минутах друг от друга).
# Бонус берётся у ТЕСНЕЙШЕГО рынка пары (chain/route_depth.py::route_bonus_pct, lltv с цепи).
# Капы двигаем ВНИЗ по одному замеру (безопасная сторона), ВВЕРХ — только живым слоем
# (route_depth, потолок LIVE_CAP_GROW_MAX=2x статика): один снимок не отличает форму пула
# от состояния блока.
_SWAP_ROUTES = {  # (collateral_lower, loan_lower) -> {router, style, path}
    # max_in (24.07 ре-верификация): пул просел с 07-08 — ratio 1.0 cbBTC=0.9967,
    # 2.0=0.9696 (плавная деградация, не обрыв). Кап 1.5 (~$97k, imp ~1.2%), сверх — Kyber.
    # 09.08: бонус 4.3841% (lltv 0.86, рынок 0x67ebd84b). На капе imp 1.25%, S_обрыв 3.63
    # cbBTC ($237k) -> правило даёт 1.82; кап 1.5 уже под правилом, НЕ трогаем.
    (_CBBTC, _EURC): {"router": AERO_CL_ROUTER, "style": "v1",
                      "path": _pack_path(_CBBTC, 100, _EURC),
                      "max_in": 15 * 10**7},
    # cbLTC/USDC 09.08 (#75): роут был БЕЗ КАПА — а значит и без живого слоя (route_depth
    # меряет только капнутые). Пул при этом крошечный: $10k = 1.6%, $25k = 7.8%, $29k = 23%.
    # Бонус 12.6761% (lltv 0.625, рынок 0x9125d0fa). S_обрыв 565 cbLTC ($26.1k) -> кап 280
    # (~$12.9k). Без капа сейз на $50k шёл бы прямым свопом при impact 55%.
    (_CBLTC, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBLTC, 200, _UNIV3_WETH, 100, _UNIV3_USDC),
                            "max_in": 280 * 10**8},
    # cbXRP/USDC 09.08 (замер на форке 49666337, #69): прямого роута НЕ БЫЛО — путь собирался
    # по фи-карте в UniV3 fee=10000, и это ПРОТУХЛО ровно как предупреждал урок 24.07: на
    # seized кита 18 333 cbXRP UniV3 отдаёт 0.0176 WETH против 4.7342 у Slipstream ts=2000
    # (269x). Глубина уехала в Slipstream, как у cbDOGE (ts=2000) и cbADA (ts=200).
    # ПРОФИЛЬ ts=2000 (WETH за 1 cbXRP): 100 шт 0.000530204 (референс), 500 -0.41%,
    # 1000 -0.92%, 2000 -1.93%, 5000 -4.84%, 10000 -11.4%, 18333 -51.3% (пул исчерпан).
    # #76 (09.08): ПРЕЖНИЙ КОММЕНТАРИЙ ЗДЕСЬ ВРАЛ ПРО ЗАПАС. Он гласил «impact 4.84% против
    # LIF 12.7% — тот же запас, что у cbBTC». Две ошибки в одной фразе:
    #  (1) 12.7% — бонус ЖИРНОГО рынка пары (0xd4a903dc, lltv 0.625). На паре ДВА рынка, второй
    #      0xfdfecf85 имеет lltv 0.770 -> бонус 7.4114% (idToMarketParams, блок 49753919).
    #      Маршрут один на оба, кап один, связывает ТЕСНЕЙШИЙ: настоящий запас был 1.53x, не 2.6x.
    #  (2) «тот же запас, что у cbBTC» неверно даже по собственным числам: 12.7/4.84 = 2.6x
    #      против 4.38/2.59 = 1.69x у cbBTC.
    # Замер 09.08 (три блока): 5000 -> imp 4.00%, 7361 -> 6.59%, 8664 -> 8.28%, 12000 -> 19.1%.
    # S_обрыв (imp = бонус 7.41%) = 7996 cbXRP ($8.3k) -> кап 3900 (~$4.05k, imp ~2.9%).
    # Сверх капа — Kyber (в доску не помещается: она quote-free, см. #69 п.2 про частичный план).
    (_CBXRP, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBXRP, 2000, _UNIV3_WETH, 100, _UNIV3_USDC),
                            "max_in": 3900 * 10**6},
    # cbBTC/USDC 28.07: UniV3 fee=500 -> Aerodrome Slipstream ts=100 (пул 0x4e962bb3, liq 3.7e12
    # против 2.1e12, fee динамический 30-162 = 0.003-0.016% против наших 0.05%). Урок разбора
    # победителя cbBTC $65.6k (27.07 23:04): его РЕАЛЬНЫЙ выход через этот пул стоил 0.52% против
    # нашей модели 1% — нетто $2,840 при нашей оценке $1,486. Бенч квотами 28.07 (Slipstream
    # QuoterV2 0x254cF9E1): Aero >= Uni на ВСЕХ размерах — $68k +$84, $500k +$2.2k (imp 0.56%
    # vs 0.99%), $1M +$8.6k (1.14% vs 1.99%), $1.9M +$27.8k (2.59% vs 4.05%). Кап 30 cbBTC
    # ОСТАВЛЕН: на капе impact 2.59% < LIF 4.38% с бОльшим запасом, чем было; сверх — Kyber
    # (PREARM греет в окнах). Старый роут для отката: UNIV3_SWAP_ROUTER style 02 fee=500.
    # 09.08 ре-замер: пул стал ГЛУБЖЕ — на капе 30 imp 1.70% (было 2.59%), 60 -> 4.16%,
    # 80 -> 7.07%; кривая ПОЛОГАЯ, обрыва нет. Бонус 4.3841% (lltv 0.86) -> S_обрыв 62.1 cbBTC
    # ($4.05M), правило min(S_budget 38, S_обрыв/2 31.1) = 31.1 — кап 30 под ним, оставляем.
    (_CBBTC, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBBTC, 100, _UNIV3_USDC),
                            "max_in": 30 * 10**8},
    # cbBTC/WETH 19.08 — ЦЕНА ОТСУТСТВИЯ РОУТА ИЗМЕРЕНА ПОТЕРЕЙ: блок 50191667, рынок
    # 0x5dffffc7 (cbBTC залог / WETH долг, lltv 0.915), приз $3905 забрал 0x218172c1 с idx 1
    # головы блока. Пары в карте НЕ БЫЛО ВООБЩЕ: fast-lane невозможен, план жил на Kyber, и
    # эта цель была НЕДОСТУПНА группе §9 (#133) по построению — пул Kyber-плана неизвестен,
    # запас худшего порядка считать не из чего. Прямой роут закрывает обе дыры разом.
    # РАЗВЕДКА 19.08 (analysis/cbbtc_weth_route_probe.py): живых пулов шесть, ГОДНЫХ два.
    #   UniV3 fee=100 и fee=10000 — пыль (разрыв 50% на 0.1 cbBTC), Slipstream ts=1 тонкий.
    #   Спор идёт между UniV3 fee=500 (0x7aea2e8a) и Slipstream ts=100 (0x70acdf2a).
    # ДВА ЗАМЕРА В ДВУХ РЕЖИМАХ ГЛУБИНЫ — и они разошлись ВДЕСЯТЕРО. Это не разброс прибора,
    # это свойство пары, и кап обязан считаться по ТОНКОМУ режиму (живой слой поднимет сам,
    # потолок 2x статики; поднять статику с одного снимка = поставить кап под обрыв).
    #   ГОЛОВА цепи (20.08, разрыв к базе 0.05 cbBTC): uni500 плоский до 14 cbBTC (0.32%),
    #     ломается 15->0.48%, 18->1.73%, 20->3.23%; на классе целей дороже Slipstream на 0.214%.
    #   БЛОК ЭПИЗОДА 50191666 (разрыв к ОРАКУЛУ рынка 0x10b95702, price/1e46 = 31.3141 —
    #     честная база, а не собственная малая ступень): uni500 0.06->0.0925%, 0.35->0.374%,
    #     1.0->1.05%, 2.0->2.17%, 3.0->3.36%, 4.5->5.29%, 9.0->12.80%(!);
    #     Slipstream 0.06->0.525%, 1.0->0.640%, 3.0->0.884%, 4.5->1.067%.
    #   То есть в тонком режиме uni500 выигрывает ТОЛЬКО до ~0.6 cbBTC, а на 9 cbBTC (кап,
    #   посчитанный по голове) он отдал бы 12.8% — реверт по minOut с гарантией.
    # ВЫБОР ВЕНЬЮ: uni500. Не потому что глубже (он мельче), а потому что на РАЗМЕРЕ ЦЕЛЕЙ
    #   ЭТОГО РЫНКА (0.06-0.35 cbBTC, максимум замеренной позиции $21k) он дешевле в ОБОИХ
    #   режимах: 0.374% против 0.561% в тонком, −0.214% в глубоком. Кап ниже точки разворота.
    # КАП по правилу карты (24.07), ПО ТОНКОМУ РЕЖИМУ: бонус пары 2.6167% (lif_bonus_pct(0.915),
    #   рынок ровно один) -> S_обрыв (разрыв = весь бонус) = 2.38 cbBTC -> min(S_budget 30,
    #   S_обрыв/2 1.19) = 1 cbBTC (~$69k). Запас на капе: разрыв 1.05% против порога реверта.
    #   Порог считается ЧЕСТНО из minProfit-гарда контракта: profit>=0.003*repaid и
    #   seized*oracle = repaid*LIF дают допустимый разрыв g <= 1 - 1.003/1.026167 = 2.257%,
    #   то есть на капе запас 2.15x, на классе целей (0.374%) — 6.0x. По консервативной
    #   половинной полке линзы (1.2751%) те же числа дают 1.21x и 3.4x.
    #   ДРЕЙФ ГЛУБИНЫ ЭТОЙ ПАРЫ — САМЫЙ БЫСТРЫЙ ВО ФЛОТЕ (x10 за сутки против x2 у wstETH),
    #   и он же причина, по которой статика стоит по тонкому замеру: в глубоком режиме
    #   live-cap поднимет кап сам до 2 cbBTC. Сверх капа — Kyber, как и везде.
    (_CBBTC, _UNIV3_WETH): {"router": UNIV3_SWAP_ROUTER, "style": "02",
                            "path": _pack_path(_CBBTC, 500, _UNIV3_WETH),
                            "max_in": 1 * 10**8},
    # LBTC/cbBTC 07-12 (нож $350.8k долга, BTC-пег пара — профиль «мгновенный флип на ноже»):
    # UniV3 ПУСТ на всех тирах (QuoterV2-проба impact~99.6%, analysis/lbtc_route_probe.py);
    # Slipstream ts=1 ЖИВОЙ (router.factory()->getPool, liquidity=125.6B, 07-12); Kyber-глубина
    # $380k ratio=1.00038 (паритет) — эконом чистая, вопрос был в скорости (Kyber RTT ~0.4-0.9с
    # vs fast-lane ~0.1с). Маршрут даёт fast-lane + кэш слоя 2 (ROUTE_COLLATERALS авто).
    # 09.08 (#75): роут был БЕЗ КАПА при САМОМ ТЕСНОМ бонусе флота — 1.6777% (lltv 0.945,
    # рынок 0x30767836; второй рынок 0x4944a116 lltv 0.915). Профиль: плоско до 54 LBTC
    # (0.553%), 57.5 -> 6.49%, 61 -> 11.86% — обрыв ровно на 54.7 ($3.57M). Кап 27 LBTC
    # (~$1.76M) = S_обрыв/2. Заодно роут стал видим живому слою (route_depth меряет капнутые).
    (_LBTC, _CBBTC): {"router": AERO_CL_ROUTER, "style": "v1",
                      "path": _pack_path(_LBTC, 1, _CBBTC),
                      "max_in": 27 * 10**8},
    # wstETH/WETH 07-14 (нож $180.2k @ HF 1.0056, LST-пег — профиль «мгновенного флипа на тике»):
    # UniV3 тонкий (fee=100 imp@60wstETH=57%, fee=500 пусто), Slipstream ts=1 живой+глубокий.
    # loan=WETH -> single-hop wstETH->WETH; fast-lane + кэш слоя-2 (ROUTE_COLLATERALS авто).
    # max_in (24.07): QuoterV2 показал ликвидность ts=1 подвижна — 200 wstETH imp 0.002%, 500 imp
    # 6.8% (за ночь просело). Кап 300 wstETH (~$700k) < обрыва -> сверх него direct=None ->
    # Kyber-агрегат (глубже, но серверный RTT). Кап в ЕДИНИЦАХ token_in (18 dec).
    # 09.08 (#75): «300 < обрыва» ПЕРЕСТАЛО БЫТЬ ПРАВДОЙ — обрыв приехал вплотную к капу.
    # Три блока подряд: 295 -> 0.003%, 305 -> 0.023%, 315 -> 0.29-0.35%, 326 -> 3.45%,
    # 340 -> 7.44%. Бонус пары самый тесный из всех — 1.0611% (lltv 0.965, рынок 0x6aa81f51;
    # второй 0x3a4048c6 lltv 0.945), поэтому S_обрыв = 317.7 wstETH ($755k): кап стоял в 6%
    # от размера, на котором выход съедает ВЕСЬ бонус. Кап 150 (~$356k) = S_обрыв/2.
    (_WSTETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                             "path": _pack_path(_WSTETH, 1, _UNIV3_WETH),
                             "max_in": 150 * 10**18},
    # weETH/WETH 24.07 (кандидат $452k @ HF 1.0113 — direction по ETH? нет: weETH↔WETH оба ETH-нога,
    # но ликвидатору всё равно нужен ВЫХОД weETH->WETH при взятии). Раньше роута НЕ было -> Kyber
    # block-loop 0.4-0.9с. Slipstream ts=1 живой (pool 0xbd3cd0, liq 2.23e24): плоско до 180 weETH
    # (imp 0.012%), обрыв 210 (0.83%). Кап 160 weETH (~$330k) < обрыва. loan=WETH single-hop.
    # 09.08 (#75): обрыв сегодня на 287 weETH (281 -> 0.02%, 292 -> 3.2%, 302 -> 6.4%), бонус
    # 1.6777% (lltv 0.945, рынок 0xfd0895ba) -> S_обрыв/2 = 143.5. Кап 140 (~$295k).
    # Дрейф обрыва этого самого пула (210 -> ~100 за один день 24.07, ~300 04.08, ~287 сейчас)
    # и есть эмпирика делителя 2 в правиле.
    # 19.08 (разбор тревоги канарейки #72, кап 140 -> 55): ОБРЫВ ПРИЕХАЛ ПОД КАП и стоит там
    # ВТОРЫЕ СУТКИ — не дрейф на снимке, а состояние. Первая тревога 17.08 23:06 (imp 23.20%
    # на капе), дальше каждые 6ч: 16.75 / 16.56 / 16.95 / 18.59 / 22.44%. Разобрана НЕ БЫЛА:
    # обе побудки агента упёрлись в недельный лимит ('hit your weekly limit'), инбокс честно
    # эскалировал их владельцу в TG (outcome=escalated-to-human + hil-сводки agent-wake 18.08
    # 02:20 и 19.08 02:00) — КАНАЛ СРАБОТАЛ, разбора не было у АГЕНТА.
    # Замер (Multicall3, публичный узел, ЛЕСТНИЦА В ОДНОМ БЛОКЕ, три блока подряд + джиттер
    # размеров, 50196398/50196414/50196429): плоско до 105 weETH (<=0.05%), колено 110
    # (1.25% / 2.90% / 0.08%), 115 -> 3.90-7.12%, 120 -> 7.91-9.47%, 140 -> 21.06-23.70%.
    # Выход НАСЫЩАЕТСЯ на ~119.7 WETH при любом входе >=140 — это стена, а не наклон
    # (та же форма, что 05.08, когда стена стояла на 88 weETH).
    # Архив-квоты назад по 30 мин (9 точек за 4.5ч): imp на 140 >= бонуса в 9/9, на 110 в 0/9,
    # на 55 в 0/9 (0.00% везде). Бонус пары 1.6777% (lltv 0.945, рынок 0xfd0895ba…cfea)
    # достигается на ~110 weETH -> S_обрыв/2 = 55.
    # 20.08 НЕЗАВИСИМАЯ ПЕРЕПРОВЕРКА перед коммитом (другая сессия, свой скрипт, лестница в
    # одном блоке x3 блока 50198061/50198073/50198079, джиттер размеров +-2%): 40/55/70/90 ->
    # <=0.02% ВЕЗДЕ, 110 -> 5.00 / 1.20 / 0.02% (колено флапает), 140 -> 19.14 / 23.89 /
    # 20.79%. Стена подтверждена ЧУЖИМ замером, кап 55 чист 3/3.
    # ЧТО ЭТОТ КАП НЕ ЧИНИТ: обе жирные цели рынка живут ВЫШЕ обрыва — 0x13B3080A (HF 1.0123,
    # полный сейз 222 weETH) и 0x79320415 (HF 1.0214, 166 weETH). Их прямой выход сегодня не
    # существует ни на каком капе: кап 55 переводит их в слайс+Kyber ЧЕСТНО, а кап 140 гнал
    # 140-слайс в стену (реверт по minOut = потеря гонки и газа, не денег).
    (_WEETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_WEETH, 1, _UNIV3_WETH),
                            "max_in": 55 * 10**18},
    # cbETH/WETH 24.07 (пег-нож, кандидаты HF 1.01-1.02; выхода НЕ БЫЛО вовсе — Kyber
    # block-loop, та же дыра, что у weETH до 24.07): UniV3 fee=500 обрывается после ~7 cbETH
    # (28 -> ratio 0.59), Slipstream ts=1 БЕЗ обрыва до 500 cbETH (ratio 1.13531 плоско).
    # Кап 300 по уроку wstETH (ликвидность ts=1 подвижна). fast-lane + кэш слоя-2 авто.
    # 09.08 (#75): пул СИЛЬНО глубже капа — плоско до 1385 cbETH (0.012%), обрыв 1476 (4.01%).
    # Бонус 1.6777% (lltv 0.945) -> правило даёт 711 cbETH, то есть кап занижен в 2.4x, а
    # крупнейшая замеренная позиция рынка ($704k ≈ 323 cbETH) не помещается в него ВООБЩЕ.
    # Вверх со ОДНОГО снимка не поднимаем: это сделает живой слой (потолок 2x статика = 600).
    (_CBETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBETH, 1, _UNIV3_WETH),
                            "max_in": 300 * 10**18},
    # WETH/USDC 27.07 (новый рынок $70.2M, 199 ликвидаций/90д): ЗАМЕР ОПРОКИНУЛ ОЖИДАНИЕ —
    # канонический fee=500 ТОНКИЙ (1200 ETH imp 5.25% > бонуса 4.38%), а fee=3000 глубокий:
    # 1200 ETH imp 0.20%, 8000 ETH ($15.7M) 1.05%, 16000 ($31M) 2.08%. Крупнейшие позиции рынка
    # ~545 ETH ⇒ запас 15×. Кап 8000 ETH. Скопировать паттерн cbBTC (fee=500) было бы ошибкой.
    # 09.08 подтверждено: 8000 -> imp 1.04%, 20000 -> 2.60%, кривая линейная, обрыва нет в
    # пределах $38M. Бонус 4.3841% (lltv 0.86, рынок 0x8793cf30 — он в INCLUDE_MARKETS, но
    # НЕ в covered_markets.json, из-за чего канарейка судила его дефолтом 10%, см. #76).
    (_UNIV3_WETH, _UNIV3_USDC): {"router": UNIV3_SWAP_ROUTER, "style": "02",
                                 "path": _pack_path(_UNIV3_WETH, 3000, _UNIV3_USDC),
                                 "max_in": 8000 * 10**18},
    # cbDOGE/cbADA -> USDC: полные Slipstream-пути (ре-верификация 24.07, см. коммент выше).
    # max_in (25.07): пул cbDOGE чист до 90k ток. (imp 0.9%), обрыв на 135k (out каппится $8.2k);
    # cbADA чист до 60k (imp 2.3%), обрыв на 120k. Сверх капа -> Kyber. NB: Kyber на ките $80k
    # cbDOGE отдаёт лишь $17.3k (21.5% номинала) — ВСЯ DOGE-ликвидность Base мелкая; жирный
    # cbDOGE-кандидат экономически несобираем целиком, minProfit-floor отсечёт сам.
    # cbDOGE кап 09.08 (канарейка выходов #71): у пула не деградация, а ОБРЫВ. Профиль impact
    # по долям прежнего капа 100k: 1% -> 0.01%, 5% -> 0.08%, 10% -> 0.17%, 25% -> 0.44%,
    # 50% -> 0.94%, 100% -> 43.50%. То есть кап стоял РОВНО НА КРАЮ обрыва: на нём выход съедал
    # 43.5% при бонусе 12.7% — гарантированный минус либо реверт по minOut. Ставили 50k.
    # #71 ЗАКРЫТ ЗАМЕРОМ 09.08 (три блока): 50k подтверждён как «до обрыва» (imp 0.69%), но
    # обрыв НЕ там, где его искали, и 50k НЕ достаточен. Сетка между 48k и 74k: 50k -> 0.69%,
    # 52k -> 0.99%, 54k -> 1.52%, 56k -> 2.28%, 58.4k -> 3.33%, 60.8k -> 6.82%, 63.2k -> 10.4%,
    # 65.8k -> 13.87%. Бонус 12.6761% (lltv 0.625) достигается на 64.9k — это ВСЕГО в 1.3x от
    # капа. Прежний вердикт «запас к LIF больше, чем у cbBTC» мерил НЕ ТО: на обрывном профиле
    # запас в процентах импакта ничего не значит, значение имеет расстояние в РАЗМЕРЕ до обрыва
    # (у cbBTC кривая пологая, у cbDOGE — колено). Кап 32k (~$2.24k) = S_обрыв/2.
    (_CBDOGE, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                             "path": _pack_path(_CBDOGE, 2000, _UNIV3_WETH, 100, _UNIV3_USDC),
                             "max_in": 32_000 * 10**8},
    # cbADA 09.08: бонус пары 7.4114%, а НЕ 12.68% — на паре ДВА рынка (0xd7520ad1 lltv 0.625
    # и 0xf2a59ad1 lltv 0.770), связывает тесный. Профиль: 60k -> 1.07-1.15%, 116k -> 5.84%
    # (блок 49754165) и 8.14% (49754310, +5 минут) — этот пул шевельнулся за пять минут на
    # 39% относительных, живая иллюстрация делителя 2. S_обрыв 114k -> правило 57.1k против
    # капа 60k = 1.05x: в пределах шума при уже заложенном двукратном запасе, не трогаем.
    # 19.08 (тревога канарейки imp 16.03% на капе; кап 60k -> 15k): у ЭТОГО пула правило
    # S_обрыв/2 не имеет одного ответа — ts=200 cbADA/WETH (0xb862f23b) МЕРЦАЕТ. Реквота на
    # блоке тревоги 50195708 дала 9403 USDC на 60k — бит-в-бит цифру канарейки, то есть замер
    # честен; Swap-логи пула в окне 50195588..50195768 показывают L 8.4e16 -> 1.1e16 -> 5.9e16
    # (уход и возврат LP за ~40 минут), сейчас L 1.7e17.
    # Распределение (архив-квоты назад по 30 мин, 30 точек за 14.5ч): выход на капе 60k
    # съедает >= бонуса 7.41% в 5/30 выборок (4 из них — в последние 2.5ч: 45.80 / 36.04 /
    # 65.75 / 86.73%), на 30k в 2/30, на 15k в 1/30, на 6k в 0/30. В здоровом состоянии
    # обрыв стоит на ~190k (120k -> 0.51%, 180k -> 5.69%), в осушенном даже 6k даёт 3.66%:
    # ОДНО правило даёт ответы 95k и <6k, поэтому статик берётся НЕ по снимку, а по
    # РАСПРЕДЕЛЕНИЮ — 15k безопасен в 29/30 состояний, а быстрая динамика остаётся за живым
    # слоем (route_depth, TTL 600с; потолок роста 2x статика = 30k).
    # 20.08 перепроверка: пул СЕЙЧАС здоров (60k -> 0.20% на трёх блоках подряд), то есть
    # снимок этот статик не подтверждает и не опровергает — он и выбран по РАСПРЕДЕЛЕНИЮ, а
    # не по состоянию дня. Перетюнивать 15k по здоровому снимку НЕЛЬЗЯ: это ровно та ошибка,
    # из-за которой кап 60k пережил мерцание.
    # Цена понижения ~нулевая: живые cbADA-цели — либо пыль (единицы тысяч токенов), либо
    # киты с HF 1.5-3.7 и сейзом в МИЛЛИОНЫ токенов, которые режутся слайсами в любом случае.
    (_CBADA, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBADA, 200, _UNIV3_WETH, 100, _UNIV3_USDC),
                            "max_in": 15_000 * 10**6},
}
# UniV3-пути из фи-карты -> та же таблица (loan=USDC). setdefault, А НЕ ПРИСВАИВАНИЕ (#69,
# 09.08): цикл идёт ПОСЛЕ литерала и раньше молча ЗАТИРАЛ явные записи. Из-за этого cbXRP не
# мог иметь осознанного маршрута вовсе — он порождался здесь и всегда получался UniV3, а когда
# глубина ушла в Slipstream (как у cbDOGE/cbADA), выразить это в фи-карте стало нечем: она
# умеет только тир UniV3. Замер 09.08 на форке: UniV3 fee=10000 отдавал 0.0176 WETH там, где
# Slipstream ts=2000 отдаёт 4.7342 — 269x, и план молча ревертил по minOut.
# Класс дефекта тот же, что у защёлки в #63: механизм, который нельзя переопределить снаружи.
for _coll, _fee in UNIV3_PATHS.items():
    _SWAP_ROUTES.setdefault((_coll, _UNIV3_USDC),
                            {"router": UNIV3_SWAP_ROUTER, "style": "02",
                             "path": _pack_path(_coll, _fee, _UNIV3_WETH,
                                                _UNIV3_WETH_USDC_FEE, _UNIV3_USDC)})
ROUTE_COLLATERALS = {c for (c, _l) in _SWAP_ROUTES}   # scope слоя 2 (кэш позиций) и fast-lane


# --- LIVE-CAP (04.08, weETH tail): ёмкость тонких CL-пулов ПОДВИЖНА (ts=1 weETH/WETH:
# 24.07 обрыв 210 weETH, утром того же дня ~100, 04.08 обрыв ~300 при liq 5.66e24 vs
# 2.23e24). Статик max_in ошибается в ОБЕ стороны: занижен — жир уходит Kyber-block-loop'ом
# (0.4-0.9с RTT в гонке; сегодня seize 168 weETH > статик 160 при плоском пуле до 260),
# завышен — сайз бьёт в осушенный пул. Живой замер (chain/route_depth) кладёт сюда
# переопределение с TTL; протухло/выключено — статик. Откат ROUTE_LIVE_CAP=0. ---
ROUTE_LIVE_CAP = os.environ.get("ROUTE_LIVE_CAP", "1").lower() not in ("0", "false", "no")
LIVE_CAP_TTL_SEC = float(os.environ.get("LIVE_CAP_TTL_SEC", "600"))
LIVE_CAP_GROW_MAX = float(os.environ.get("LIVE_CAP_GROW_MAX", "2.0"))  # рост капа: x от статик
_LIVE_CAPS_LOCK = threading.Lock()
_LIVE_CAPS: dict = {}    # (tin_lower, tout_lower) -> (cap_wei, expires_monotonic)


def set_live_cap(token_in, token_out, cap_wei, now=None):
    """Живой кап от замера глубины. Рост ограничен LIVE_CAP_GROW_MAX*статик (кривая квота не
    надует кап), спад не ограничен вплоть до 0 (осушенный пул -> Kyber). Роут без статик-капа
    замеру не подлежит — вызов игнорируется. Возвращает применённый кап или None."""
    key = (token_in.lower(), token_out.lower())
    r = _SWAP_ROUTES.get(key)
    if r is None or r.get("max_in") is None:
        return None
    cap = min(int(cap_wei), int(r["max_in"] * LIVE_CAP_GROW_MAX))
    with _LIVE_CAPS_LOCK:
        _LIVE_CAPS[key] = (cap, (time.monotonic() if now is None else now) + LIVE_CAP_TTL_SEC)
    return cap


def _effective_cap(key, now=None):
    """Кап роута с учётом live-переопределения. Чтение без лока (GIL-атомарный dict.get —
    горячий путь проб/fast-lane не должен спорить за лок вотчера)."""
    r = _SWAP_ROUTES.get(key)
    if r is None:
        return None
    static = r.get("max_in")
    if static is None or not ROUTE_LIVE_CAP:
        return static
    e = _LIVE_CAPS.get(key)
    if e is not None and (time.monotonic() if now is None else now) < e[1]:
        return e[0]
    return static


def direct_swap(token_in, token_out, amount_in, sender, recipient, *, repaid_assets,
                slippage_bps=100, min_out=None):
    """Drop-in for kyber_swap на прямых маршрутах, NO in-race quote. P1-8 fix: token_out ЧЕСТНЫЙ
    ключ маршрута (раньше univ3_swap хардкодил USDC-выход — для EURC-рынка контракт получил бы
    USDC и упал CannotRepay). amountOutMinimum=repaid_assets (debt floor); profit protected by
    preconf sim + contract minProfit. Returns {router, calldata} или None (caller -> kyber_swap).

    min_out (§9/#133): ЯВНЫЙ amountOutMinimum. None = прежнее поведение бит-в-бит (пол = долг).
    Нужен мультицелевой группе v6: цели одного пула исполняются ОДНОЙ tx подряд, и вторая
    свопает уже по сдвинутой цене — её пол обязан считаться от ХУДШЕГО порядка (все n−1 раньше),
    а не от одиночной котировки. Отдельный параметр, а не переиспользование repaid_assets:
    repaid_assets — это ДОЛГ (величина Morpho), min_out — ТРЕБОВАНИЕ К СВОПУ (величина пула);
    смешать их в одном имени значит потерять, какая из двух ролей поехала при разборе.
    """
    from eth_abi import encode
    r = _SWAP_ROUTES.get((token_in.lower(), token_out.lower()))
    if r is None:
        return None
    # пер-роутный кап (24.07): выше ёмкости одиночного пула (обрыв слиппажа) отдаём None ->
    # caller падает на Kyber-агрегат (глубже, но серверный RTT — уже НЕ fast-lane). Защищает
    # wstETH/weETH от исполнения мега-сайза в тонкий пул. Кап в единицах token_in.
    # 04.08: кап живой (_effective_cap) — свежий замер глубины важнее статик-константы.
    _cap = _effective_cap((token_in.lower(), token_out.lower()))
    if _cap is not None and int(amount_in) > int(_cap):
        return None
    # Пол свопа: явный min_out либо (дефолт) долг. Считается ОДИН раз на обе ветки стилей —
    # разъехавшиеся полы у v1 и UniV3 были бы дефектом, невидимым в тестах одной ветки.
    _min_out = int(repaid_assets) if min_out is None else int(min_out)
    if r["style"] == "v1":     # Slipstream: v1-форма с deadline
        params = encode(["(bytes,address,uint256,uint256,uint256)"],
                        [(r["path"], recipient, _AERO_DEADLINE, int(amount_in), _min_out)])
        return {"router": r["router"], "calldata": "0x" + _AERO_EXACT_INPUT_SEL + params.hex()}
    params = encode(["(bytes,address,uint256,uint256)"],
                    [(r["path"], recipient, int(amount_in), _min_out)])
    return {"router": r["router"], "calldata": "0x" + _UNIV3_EXACT_INPUT_SEL + params.hex()}


def univ3_swap(token_in, token_out, amount_in, sender, recipient, *, repaid_assets, slippage_bps=100):
    """Обратная совместимость (пробники/скрипты): теперь тонкая обёртка direct_swap."""
    return direct_swap(token_in, token_out, amount_in, sender, recipient,
                       repaid_assets=repaid_assets, slippage_bps=slippage_bps)


def route_max_in(token_in, token_out):
    """Ёмкость прямого роута в единицах token_in: живой кап (замер глубины, TTL) либо статик
    max_in, либо None (нет роута/роут без капа). Потребители — дробление spec-fire и fast-lane
    (01.08): сейз сверх капа режется до пула; prearm cap-aware (04.08)."""
    return _effective_cap((token_in.lower(), token_out.lower()))


# consume-хук (05.08): списание на ОТПРАВКЕ, не на стройке — пробы строятся спекулятивно
# на кэше 2.5с. Сим каскада 04.08: A=222.4 и B=167.9 оба строились по полному капу 240 ->
# tx B ревертит по minOut в осушенном A пуле (в батче = реверт юнита). Реверт нашей tx
# недооценивает кап до ближайшего замера вотчера (<=120с на капнутых парах с долгом) или
# TTL — консервативно. Откат LIVE_CAP_CONSUME=0.
LIVE_CAP_CONSUME = os.environ.get("LIVE_CAP_CONSUME", "1").lower() not in ("0", "false", "no")


def consume_live_cap(token_in, token_out, amount_in, now=None):
    """Списать отправленный direct-сайз из живого капа: следующая стройка видит остаток пула.
    База = текущий эффективный кап (живой либо статик); срок — существующий TTL живой записи
    либо новый LIVE_CAP_TTL_SEC. Возвращает новый кап или None (нет капнутого роута/выключено)."""
    if not (ROUTE_LIVE_CAP and LIVE_CAP_CONSUME):
        return None
    key = (token_in.lower(), token_out.lower())
    r = _SWAP_ROUTES.get(key)
    if r is None or r.get("max_in") is None:
        return None
    _now = time.monotonic() if now is None else now
    with _LIVE_CAPS_LOCK:
        cur = _effective_cap(key, now=_now)
        e = _LIVE_CAPS.get(key)
        exp = e[1] if (e is not None and e[1] > _now) else _now + LIVE_CAP_TTL_SEC
        new = max(0, int(cur) - int(amount_in))
        _LIVE_CAPS[key] = (new, exp)
    return new


def release_live_cap(token_in, token_out, amount_in, now=None):
    """Вернуть СВОЙ резерв перед пересборкой той же цели (spec-fire: healthy-реверты пул не
    трогают — свой расход не должен резать свой же сайз до партиала). Ограничен той же
    границей LIVE_CAP_GROW_MAX*статик, что и замер; протухшая/перезаписанная замером запись
    возврату не подлежит (замер = истина). Возвращает новый кап или None (no-op)."""
    if not (ROUTE_LIVE_CAP and LIVE_CAP_CONSUME):
        return None
    key = (token_in.lower(), token_out.lower())
    r = _SWAP_ROUTES.get(key)
    if r is None or r.get("max_in") is None:
        return None
    _now = time.monotonic() if now is None else now
    with _LIVE_CAPS_LOCK:
        e = _LIVE_CAPS.get(key)
        if e is None or e[1] <= _now:
            return None
        new = min(int(e[0]) + int(amount_in), int(r["max_in"] * LIVE_CAP_GROW_MAX))
        _LIVE_CAPS[key] = (new, e[1])
    return new


_PARTIAL_HEADROOM_PCT = int(os.environ.get("SPEC_FIRE_PARTIAL_HEADROOM_PCT", "95"))


def partial_shares_for_cap(shares, seized, cap, headroom_pct=None):
    """Сколько shares погасить, чтобы сейз влез в кап роута с запасом. 0 = дробить нечего
    (сейз и так помещается) или нельзя (нулевые входы). Целочисленно, без float-дрейфа.
    Канон дробления (урок cbDOGE 01.08): конкурент монетизирует кита ломтями по ёмкости
    выхода — единственная тактика на тонких пулах (Kyber на таком ките отдаёт 21.5%)."""
    hp = _PARTIAL_HEADROOM_PCT if headroom_pct is None else int(headroom_pct)
    shares, seized, cap = int(shares), int(seized), int(cap)
    if shares <= 0 or seized <= 0 or cap <= 0 or seized <= cap:
        return 0
    return shares * (cap * hp // 100) // seized


def expected_seized(repaid_assets: int, lif: float, price: int) -> int:
    """Collateral (wei) Morpho seizes for repaying `repaid_assets` (loan wei), per _liquidate:
    seized = mulDivDown(wMulDown(repaidAssets, LIF), ORACLE_PRICE_SCALE, price)."""
    lif_wad = int(round(lif * WAD))
    incentivized = repaid_assets * lif_wad // WAD
    return incentivized * ORACLE_PRICE_SCALE // price


# --- liquidate() bundle + pre-send eth_call gate ---

ZERO_ADDR = "0x" + "00" * 20

# 29.07 unwrap-хоп (контракт v3, «го» kelbic): обёртка без DEX-ликвидности -> redeem -> своп
# ПОДЛЕЖАЩИМ. Пары проверены on-chain (asset()) и лестницей Kyber: обёртка мертва (~0.9 WETH
# при любом входе), подлежащий бездонный (impact 0.1% на $400k). {wrapper_lower: underlying}.
UNWRAP_COLLATERALS = {
    "0x7fcd174e80f264448ebee8c88a7c4476aaf58ea6":       # wsuperOETHb (ERC4626)
        "0xdbfefd2e8460a6ee4955a68582f85708baea60a3",    # superOETHb
}
_UNWRAP_RATE_TTL = 3600.0     # convertToAssets дрейфует ребейзом ~раз в сутки — часа хватает
_UNWRAP_HAIRCUT = 0.997       # котируем ЧУТЬ меньше ожидаемого redeem: недобор -> SwapFailed,
_unwrap_rate_cache: dict = {}  # излишек — пыль в контракте (sweep), экономику держит minProfit


def _unwrap_rate_wad(rpc, wrapper: str) -> int:
    """convertToAssets(1e18) обёртки с TTL-кэшем (ставка меняется только ребейзом). I/O."""
    key = wrapper.lower()
    ent = _unwrap_rate_cache.get(key)
    now = time.monotonic()
    if ent and now - ent[1] < _UNWRAP_RATE_TTL:
        return ent[0]
    sel = "0x07a2d13a"                                    # convertToAssets(uint256)
    data = sel + hex(WAD)[2:].rjust(64, "0")
    raw = rpc.eth_call({"to": wrapper, "data": data})
    rate = int.from_bytes(bytes(raw)[:32], "big")
    _unwrap_rate_cache[key] = (rate, now)
    return rate


def swap_plan(rpc, coll: str, seized: int):
    """(token_in, amount_in, unwrap_target) для свопа выхода: обёртки из UNWRAP_COLLATERALS
    котируются ПОДЛЕЖАЩИМ (redeem в контракте до свопа), остальное — как есть. I/O при unwrap."""
    under = UNWRAP_COLLATERALS.get(coll.lower())
    if under is None:
        return coll, seized, ZERO_ADDR
    amount = int(seized) * _unwrap_rate_wad(rpc, coll) // WAD
    return under, int(amount * _UNWRAP_HAIRCUT), coll


# 29.07 SLICE («го» kelbic, USDe ETA сжалась 62->23 дн): умный частичный сайзинг до глубины
# пула — Katana-урок «no profitable chunk» + срез USDe-кита ($20M долга при глубине ~$1.15M).
# Если своп ПОЛНОГО seized продавливает рынок сильнее SLICE_MAX_IMPACT, режем repaidShares до
# доли, чей своп укладывается в импакт. Частичная ликвидация ЛЕЧИТ позицию (LIF*lltv < 1) —
# остаток дозревает и режется снова по мере наполнения пула арбитражем. f=1 проходит с ПЕРВОЙ
# котировки => для обычных целей поведение и число RTT не меняются.
SLICE_MAX_IMPACT = float(os.environ.get("SLICE_MAX_IMPACT", "0.01"))
SLICE_MIN_FRACTION = float(os.environ.get("SLICE_MIN_FRACTION", "0.02"))
SLICE_MAX_ITERS = int(os.environ.get("SLICE_MAX_ITERS", "6"))


def slice_to_depth(probe_fn):
    """probe_fn(fraction) -> (impact_float, swap) | None. Ищет НАИБОЛЬШУЮ долю с импактом
    <= SLICE_MAX_IMPACT. Вниз — пропорционально перебору (демпфер 0.8, пол SLICE_MIN_FRACTION);
    после первого проходного размера — расширение к глубине (x2.5, капнутое серединой до
    известной непроходной границы): канарейка 29.07 показала, что на обрывном профиле USDe
    (импакт 97% на полном) чистое сжатие перепрыгивает оптимум в пол и оставляет ~2/3 среза.
    f=1 проходит с первой котировки. -> (fraction, swap) или (0.0, None) — глубины нет. Чистая."""
    f, best, hi = 1.0, None, None
    for _ in range(SLICE_MAX_ITERS):
        res = probe_fn(f)
        if res is None:
            # отказ котировки (mGLO 29.07: Kyber 400 на 13.5M токенов) = «импакт за капом»,
            # НЕ «глубины нет»: сжимаемся и пробуем меньше; сдаёмся только на полу
            res = (1.0, None)
        impact, swap = res
        if swap is None:
            impact = max(impact, 1.0)
        if impact <= SLICE_MAX_IMPACT:
            if f >= 0.999:
                return 1.0, swap
            best = (f, swap)
            nxt = f * 2.5 if hi is None else min(f * 2.5, (f + hi) / 2)
            if nxt <= f * 1.05:
                return best
            f = min(nxt, 1.0)
        else:
            hi = f
            if best is not None:
                nxt = (best[0] + hi) / 2
                if nxt <= best[0] * 1.05:
                    return best
                f = nxt
            else:
                if f <= SLICE_MIN_FRACTION:
                    return 0.0, None
                f = max(SLICE_MIN_FRACTION, f * SLICE_MAX_IMPACT / max(impact, 1e-9) * 0.8)
    return best if best is not None else (0.0, None)


_LIQUIDATE_SIG = ("liquidate((address,address,address,address,uint256),"
                  "address,uint256,address,bytes,uint256,address)")


def encode_liquidate(mp: dict, borrower: str, repaid_shares: int, swap_target: str,
                     swap_calldata: str, min_profit: int, unwrap_target: str = ZERO_ADDR) -> str:
    """ABI-encode Liquidator.liquidate(mp, borrower, repaidShares, swapTarget, swapData,
    minProfit, unwrapTarget). unwrap_target=0 = прежний путь (без redeem)."""
    from eth_abi import encode
    from eth_utils import keccak
    selector = keccak(text=_LIQUIDATE_SIG)[:4]
    cd = bytes.fromhex(swap_calldata[2:] if swap_calldata.startswith("0x") else swap_calldata)
    args = encode(
        ["(address,address,address,address,uint256)", "address", "uint256", "address", "bytes",
         "uint256", "address"],
        [(mp["loanToken"], mp["collateralToken"], mp["oracle"], mp["irm"], int(mp["lltv"])),
         borrower, int(repaid_shares), swap_target, cd, int(min_profit), unwrap_target],
    )
    return "0x" + (selector + args).hex()


# --- v5 stateful board (07.08): owner армит план ДО окна, залповые ноги пробят 1 байтом ---
_SET_PLAN_SIG = ("setPlan((address,address,address,address,uint256),"
                 "address,uint256,address,bytes,uint256,address,uint64,uint256)")
_CLEAR_PLAN_SIG = "clearPlan()"
_PLAN_INFO_SIG = "planInfo()"
# Проба-доска: <=1 байта калдаты падает в fallback() контракта. Один байт, не ноль — нулевая
# калдата у многих кошельков/релеев трактуется как простой перевод ETH.
V2_PROBE_CALLDATA = "0x01"
# v6-D4 §5: СЕНТИНЕЛ ГОРЯЧЕГО УКАЗАТЕЛЯ. У v6 байт 0x01..0x08 — ЯВНАЯ группа (дрели, прямой
# огонь), а 0xFF означает «группу возьми из _hot». Дефолт боевых лестниц — сентинел: статическое
# разбиение ног по группам делило бы плотность на 8 ровно в том звене, которое сейчас связывает.
V6_PROBE_CALLDATA = "0xFF"


def probe_calldata() -> str:
    """Байт пробы ПРЕДПОДПИСАННОЙ ноги: 0x01 (v5) либо 0xFF (v6, §5).

    Флаг читается ДИНАМИЧЕСКИ (атрибут модуля board_v6), а не копируется в константу при
    импорте: стенд обязан уметь поставить ОБЕ полярности в одном процессе и сравнить их
    ИСПОЛНЕНИЕМ. В бою флаг берётся из окружения один раз при импорте board_v6.

    БАЙТ ЗАПЕЧЁН В ПОДПИСЬ. Поэтому смена флага в живом процессе сама по себе лестницы не
    меняет — их протухание ловит `ladder_resign_needed` по полю "cd" (см. там). Штатный путь
    смены — рестарт в тишине (§0, шаг 4).
    """
    try:
        import chain.board_v6 as _b6
        return V6_PROBE_CALLDATA if _b6.SPEC_FIRE_V6 else V2_PROBE_CALLDATA
    except Exception:
        # Обвязка v6 не импортируется => флаг взять неоткуда => v6 не армится ничем =>
        # единственный БЕЗОПАСНЫЙ байт — прежний (незнание закрывает гард).
        return V2_PROBE_CALLDATA


def encode_set_plan(mp: dict, borrower: str, repaid_shares: int, swap_target: str,
                    swap_calldata: str, min_profit: int, deadline: int, trigger_price: int,
                    unwrap_target: str = ZERO_ADDR) -> str:
    """ABI-encode Liquidator.setPlan(...). Те же аргументы, что у liquidate(), плюс TTL плана
    и triggerPrice (в юнитах ОРАКУЛА-ОБЁРТКИ рынка, 1e36; 0 = фильтра нет)."""
    from eth_abi import encode
    from eth_utils import keccak
    selector = keccak(text=_SET_PLAN_SIG)[:4]
    cd = bytes.fromhex(swap_calldata[2:] if swap_calldata.startswith("0x") else swap_calldata)
    args = encode(
        ["(address,address,address,address,uint256)", "address", "uint256", "address", "bytes",
         "uint256", "address", "uint64", "uint256"],
        [(mp["loanToken"], mp["collateralToken"], mp["oracle"], mp["irm"], int(mp["lltv"])),
         borrower, int(repaid_shares), swap_target, cd, int(min_profit), unwrap_target,
         int(deadline), int(trigger_price)],
    )
    return "0x" + (selector + args).hex()


def encode_clear_plan() -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=_CLEAR_PLAN_SIG)[:4].hex()


def read_plan_info(rpc, liquidator_address: str):
    """(borrower, deadline, triggerPrice) с контракта. None, если чтение не удалось ИЛИ контракт
    не знает planInfo (v4) — вызывающий обязан трактовать None как «доски нет»."""
    from eth_abi import decode as abi_decode
    from eth_utils import keccak
    try:
        raw = rpc._web3().eth.call({"to": liquidator_address,
                                    "data": "0x" + keccak(text=_PLAN_INFO_SIG)[:4].hex()})
        b, dl, tp = abi_decode(["address", "uint64", "uint256"], bytes(raw))
        return b, int(dl), int(tp)
    except Exception:
        return None


# --- batch: N targets in ONE tx (cascade capture). Контракт: batchLiquidate(LiquidationItem[]) ---
_BATCH_LIQUIDATE_SIG = ("batchLiquidate(((address,address,address,address,uint256),"
                        "address,uint256,address,bytes,uint256,address)[])")
_ITEM_TYPE = ("((address,address,address,address,uint256),address,uint256,address,bytes,"
              "uint256,address)")


def _prep_to_item_tuple(prep: dict):
    """Из prep (сырые компоненты, положенные prepare_hot/fast) -> ABI-tuple LiquidationItem."""
    mp = prep["mp"]
    cd = prep["swap_calldata"]
    cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if isinstance(cd, str) else bytes(cd)
    return ((mp["loanToken"], mp["collateralToken"], mp["oracle"], mp["irm"], int(mp["lltv"])),
            prep["borrower"], int(prep["repaid_shares"]), prep["swap_target"], cd,
            int(prep["min_profit"]), prep.get("unwrap_target", ZERO_ADDR))


# ---- ПАТРОННЫЙ РЕЗЕРВ (25.07, баланс долит до 0.1938 ETH) ----
# Один полный выстрел на каскадном tip стоит РЕЗЕРВА limit×maxFee ≈ 4M × 30 gwei = 0.12 ETH.
# Расходники (канареечные/спек-пробы по 0.05 gwei) не должны его отгрызть: пока баланс не
# покрывает патрон + стоимость пробы, ПРОБЫ МОЛЧАТ. Боевой путь резервом НЕ гейтится — он и есть
# патрон. Баланс кэшируем (19 CU/чтение, квота дорога): пробы редки, 60с свежести достаточно.
BULLET_RESERVE_WEI = int(float(os.environ.get("GAS_BULLET_RESERVE_ETH", "0.12")) * 10 ** 18)
_BAL_CACHE = {"wei": None, "ts": 0.0, "spent": 0}
_BAL_TTL = float(os.environ.get("GAS_BALANCE_TTL_SEC", "60"))


def probe_note_spend(cost_wei: int):
    """Учесть ОЦЕНКУ стоимости отправленной пробы между чтениями баланса (см. cached_balance_wei):
    в длинный RPC-аут гейт деградирует в КОНСЕРВАТИВНЫЙ, а не в открытый."""
    with _INFLIGHT_LOCK:
        _BAL_CACHE["spent"] = int(_BAL_CACHE["spent"]) + max(0, int(cost_wei))


def cached_balance_wei(rpc, address: str, now=None):
    """Баланс с TTL-кэшем, ПЕССИМИСТИЧНО: возвращает last_balance − Σ(оценок проб с последнего
    успешного чтения). None только если баланс не читался НИ РАЗУ (тогда вызыватель fail-open —
    глушить измерительный слой на пустом кэше нельзя). Пока чтение падает, знаменатель тает сам,
    и пробы упираются в патронный резерв, а не грызут его по копейке."""
    t = time.monotonic() if now is None else now
    fresh = _BAL_CACHE["wei"] is not None and t - _BAL_CACHE["ts"] < _BAL_TTL
    if not fresh:
        try:
            w = int(rpc._web3().eth.get_balance(address))
            with _INFLIGHT_LOCK:
                _BAL_CACHE["wei"], _BAL_CACHE["ts"], _BAL_CACHE["spent"] = w, t, 0
        except Exception:
            pass                                  # читаем дальше по кэшу МИНУС потраченное
    if _BAL_CACHE["wei"] is None:
        return None
    return max(0, int(_BAL_CACHE["wei"]) - int(_BAL_CACHE["spent"]))


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


def tip_ladder_wei(cfg) -> list:
    """Лестница tip-ярусов (base, mid, big) в wei, по убыванию. Чистая."""
    return sorted({int(float(cfg.tip_gwei) * 1e9),
                   int(float(getattr(cfg, "tip_mid_gwei", 2.0)) * 1e9),
                   int(float(getattr(cfg, "tip_big_gwei", 30.0)) * 1e9)}, reverse=True)


def marginal_item_cost_usd(prep, cfg, tip_wei: int) -> float:
    """Маржинальная ЦЕНА добавления позиции в батч: её вклад в batch_gas_limit × tip. basefee
    сознательно не в формуле — на Base он мгвеи против ярусов 0.05-30 gwei и покрыт запасом
    batch_ev_margin. gas-фолбэк = floor (пере-, не недо-оценка: реальная пылинка ~0.5-1M). Чистая."""
    marg_gas = (int(prep.get("gas") or gas_floor_of(cfg)) + 30_000) * 1.25
    return marg_gas * tip_wei / 1e18 * float(cfg.eth_price_usd)


def assign_tier(prep, cfg, base_tip_wei: int, margin: float):
    """EV-ярус позиции: идём от её собственного tip ВНИЗ по лестнице до первого яруса, где
    net >= margin × маржинальный газ. (None, cost) = не тянет даже базовый (EV-дроп). Чистая."""
    own = int(prep.get("tip") or base_tip_wei)
    net = float(prep.get("net_usd") or 0.0)
    for t in [c for c in sorted(set(tip_ladder_wei(cfg) + [own]), reverse=True) if c <= own]:
        cost = marginal_item_cost_usd(prep, cfg, t)
        if net >= margin * cost:
            return t, cost
    return None, marginal_item_cost_usd(prep, cfg, base_tip_wei)


def assemble_units(deduped, cfg, max_inflight: int, log=None):
    """Тир-чистые юниты + EV-отсечка (27.07 «пылевой догруз», из разбора каскадов 06.25):
    (1) юнит НЕ смешивает tip-ярусы: батч платит fee своего жирнейшего члена, и пылинка в юните
        кита ехала бы на tip 30 → маржинальный газ ~$200+ против приза $10-20 (глубокий минус);
    (2) позиция, чей net не тянет свой ярус (margin × маржинальный газ), ДЕМОТИРУЕТСЯ вниз по
        лестнице до проходного (fee переписывается: max_fee − old_tip + new_tip);
    (3) не тянет даже base — skip 'ev-cutoff' (при текущем конфиге не срабатывает: min_profit $5
        выше порога ~$0.9; это защита от будущей смены tip/цен, не новый гейт);
    (4) юниты уходят дорогим ярусом вперёд (нонс = позиция в блоке): киты гонятся первыми,
        пыле-батчи хвостом на tip 0.05 — пыль скорости не требует (41% пыли поле берёт вообще
        без транзита в блоке). Внутри яруса — net desc, чанки по batch_max_items (для чисто
        базового входа блок-лупа порядок бит-в-бит со старой сборкой).
    Возвращает (units[:max_inflight], dropped — skip-строки для results). Чистая (юниты)."""
    base_tip = int(float(cfg.tip_gwei) * 1e9)
    margin = float(getattr(cfg, "batch_ev_margin", 2.0))
    bmax = max(1, int(getattr(cfg, "batch_max_items", 5)))
    tiers, dropped = {}, []
    for mid, b, prep in deduped:
        t, cost = assign_tier(prep, cfg, base_tip, margin)
        net = float(prep.get("net_usd") or 0.0)
        if t is None:
            dropped.append({"market_id": mid, "borrower": b, "sent": False,
                            "reason": f"ev-cutoff: net ${net:.2f} < {margin:g}x газ ${cost:.2f} @base-tip",
                            "net_usd": net})
            if log:
                log.info("batch-tier: EV-дроп %s/%s net$%.2f < %gx маржинальный газ $%.2f",
                         mid[:10], b[:10], net, margin, cost)
            continue
        own = int(prep.get("tip") or base_tip)
        if t != own and prep.get("max_fee"):
            prep = dict(prep)                                 # копия: prep расшарен с вызывающим
            prep["max_fee"] = max(t, int(prep["max_fee"]) - own + t)
            prep["tip"] = t
            if log:
                log.info("batch-tier: демоция %s/%s net$%.2f tip %.3g→%.3g gwei (свой ярус EV не отбит)",
                         mid[:10], b[:10], net, own / 1e9, t / 1e9)
        tiers.setdefault(t, []).append((mid, b, prep))
    units = []
    for t in sorted(tiers, reverse=True):
        rows = sorted(tiers[t], key=lambda r: r[2].get("net_usd", 0.0), reverse=True)
        units += [rows[i:i + bmax] for i in range(0, len(rows), bmax)]
    return units[:max_inflight], dropped


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

        from chain.hotpath import _shave_ask      # #121: заявка строго меньше долга
        repaid_shares = _shave_ask(int(borrow_shares))
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
        if seized == 0:
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        swap_token, swap_amount_full, unwrap = swap_plan(rpc, coll, seized)

        def _probe(f):
            amt = max(1, int(swap_amount_full * f))
            try:
                s = kyber_swap(swap_token, loan, amt, liq, liq, slippage_bps=slippage_bps)
            except Exception:
                return None
            out = int(s.get("amount_out") or 0)
            # референс нулевого импакта: цена оракула (loan за коллатерал, 1e36); для unwrap
            # amt уже в подлежащем с haircut'ом — переводим обратно через seized-эквивалент
            seized_eq = int(seized * f)
            ref = seized_eq * int(price) // ORACLE_PRICE_SCALE
            return (1.0 - out / ref) if ref > 0 else 1.0, s

        fraction, swap = slice_to_depth(_probe)
        if swap is None:
            return {"ok": False, "reason": "нет глубины даже для среза (SLICE_MIN_FRACTION)"}
        if fraction < 1.0:
            repaid_shares = max(1, int(int(borrow_shares) * fraction))
            repaid_assets = to_assets_up(repaid_shares, tba, tbs)
            seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
            swap_token, swap_amount, unwrap = swap_plan(rpc, coll, seized)
            swap = kyber_swap(swap_token, loan, swap_amount, liq, liq, slippage_bps=slippage_bps)

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0, unwrap)
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
        cd1 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"],
                               min_profit_final, unwrap)
        return {"ok": True, "calldata": cd1, "net_usd": net_usd, "gas": gas_floor_of(cfg),
                "profit_usd": profit_usd, "cost_usd": cost_usd, "slice_fraction": fraction}
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


def _submit_xray(rpc, borrower, block_number, our_index, log, lookback=4, cfg=None, our_debt_usd=None,
                 _lag_sleep=2.0, _sleep=time.sleep):
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
        # 31.07: xray бежит через ~0.7с после майна — нода порой ЕЩЁ не отдаёт свежие блоки (тот же
        # лаг, что дал reason='block not found' в реплее), и пустые логи выглядели как «не гонка»,
        # хотя победитель сидел блоком раньше нас (проигрыш cbBTC $3.4k 14:07: победитель blk=-1
        # idx=2928 tip=0.0034 — ложный вердикт скрыл первую боевую FCFS-эмпирику). Голова ноды
        # обязана покрыть окно; не покрыла после ретрая — честный «xray неполон», не вердикт.
        logs, head = [], None
        for attempt in (0, 1):
            try:
                head = int(w3.eth.block_number)
            except Exception:
                head = None
            logs = get_logs_fb(w3, {"fromBlock": frm, "toBlock": block_number,
                                    "address": _MORPHO_BLUE,
                                    "topics": [_LIQUIDATE_EVENT_SIG, None, None, topic_b]})
            if logs or head is None or head >= int(block_number):
                break
            if attempt == 0:
                _sleep(_lag_sleep)
        if not logs:
            if head is not None and head < int(block_number):
                if log:
                    log.info("submit-xray: НЕПОЛОН — нода отстала (head=%s < %s), вердикт о гонке "
                             "не выносим (реверт разбирать по reason)", head, block_number)
                return "unknown"
            if log:
                log.info("submit-xray: borrower %s НЕ ликвидирован в [%d..%d] (наш idx=%s) — "
                         "реверт НЕ гонка за жертву (смотреть reason)",
                         borrower[:10], frm, block_number, our_index)
            return "not-race"
        w = logs[0]                                     # самый ранний Liquidate = победитель
        wblk, widx = w.get("blockNumber"), w.get("transactionIndex")
        wtx = w.get("transactionHash")
        wtx_hex = wtx.hex() if wtx is not None and not isinstance(wtx, str) else (wtx or "?")
        waddr = wto = tip_s = "?"
        try:                                            # обогащение: кто и с каким tip (2 RPC, реверты редки)
            tx = w3.eth.get_transaction(wtx)
            waddr, wto = str(tx.get("from") or "?"), str(tx.get("to") or "?")
            # #44: победителем может быть НАШ СОБСТВЕННЫЙ тракт — проба доски/движка/ноги берёт
            # жертву раньше боевой tx. Без тега такая строка уходит в эмпирическую tip-функцию
            # конкурентов и в счётчик оспоренных гонок как ЧУЖАЯ победа, то есть наша победа
            # засчитывается нам же в поражение.
            _self = _is_ours(waddr, wto, cfg)
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
                     "tx=%s | %s addr=%s to=%s tip_gwei=%s",
                     wblk, widx, block_number, our_index, blk_off, wtx_hex,
                     "self-win" if _self else "race-winner", waddr, wto, tip_s)
        if cfg is not None and tip_s != "?" and not _self:   # своя победа каскад не эскалирует
            try:
                from chain.hotpath import dynamic_tip_gwei, note_race_loss_for_cascade
                our_tip = dynamic_tip_gwei(cfg, our_debt_usd)
                note_race_loss_for_cascade(float(tip_s), our_tip, log=log)
            except Exception:
                pass
        # Ш16: проигранная гонка обязана СООБЩАТЬ О СЕБЕ. Вердикт уже вычислен здесь и раньше
        # просто возвращался наверх — деньги проходили мимо молча (11.08: 79 ликвидаций за
        # сутки, ни одного сообщения). Своя победа сообщением не считается.
        if not _self:
            try:
                _our_tip = None
                if cfg is not None:
                    from chain.hotpath import dynamic_tip_gwei as _dtg
                    _our_tip = _dtg(cfg, our_debt_usd)
                note_lost_race(borrower, our_debt_usd, waddr, blk_off,
                               win_tip_gwei=(float(tip_s) if tip_s != "?" else None),
                               our_tip_gwei=_our_tip)
            except Exception:
                pass                                    # диагностика не роняет receipt-путь
        # #44: своя победа — НЕ проигранная гонка. Для брейкера класс тот же (не шторм), но
        # счётчик оспоренных гонок и tip-функция конкурентов обязаны её отличать.
        return "self-win" if _self else "race"
    except Exception as e:
        if log:
            log.info("submit-xray fail: %s", type(e).__name__)
    return "unknown"


_TRANSPORT_SIGNS = ("block not found", "-32001", "too many requests", "request timeout",
                    "timed out", "timeout", "connection", "unavailable")


def _revert_reason(rpc, tx_hash, block_number, _retry_sleep=1.5, _sleep=time.sleep):
    """Best-effort revert reason for a mined-but-reverted tx: replay it as eth_call at the mined block
    and surface the revert string/selector via _revert_bucket. DIAGNOSTIC ONLY (block end-state may
    differ from what the tx saw mid-block) -> use as a bucket, not proof. Never raises.
    31.07: реплей идёт через ~0.7с после майна — нода порой ещё НЕ видит блок, и транспортный отказ
    (-32001 'block not found') печатался КАК причина реверта (проигрыш cbBTC 14:07). Транспортный
    класс -> один ретрай после паузы (нода догоняет за секунды); не догнала — честный тег."""
    last = ""
    for attempt in (0, 1):
        try:
            w3 = rpc._web3()
            tx = w3.eth.get_transaction(tx_hash)
            rpc.eth_call({"to": tx["to"], "from": tx["from"], "data": tx["input"]},
                         block=block_number if block_number is not None else "latest")
            return "clean-on-replay (state moved / lost race)"
        except Exception as e:
            s = str(e)
            if any(t in s.lower() for t in _TRANSPORT_SIGNS):
                last = s
                if attempt == 0:
                    _sleep(_retry_sleep)
                    continue
                return "reason недоступен (нода отстала/транспорт: %s)" % s[:60]
            return _revert_bucket(s)
    return "reason недоступен (%s)" % last[:60]


def _send_units_volley(rpc, w3, cfg, units, base_nonce, tip_wei, bcast_urls, log=None):
    """PARALLEL_BROADCAST (залп): подписать ВСЕ юниты -> раздать ВСЕ raw в секвенсер одним залпом ->
    Alchemy-нога журналом ПОСЛЕ. Старый цикл шлёт юниты последовательно: гоночная нога юнита u+1
    стартует только после синхронной Alchemy-ноги юнита u (~60мс RTT) — в каскаде >batch_max_items
    жертв юнит 3 приходит в FCFS-очередь на ~120мс позже юнита 1. Здесь подпись = CPU-мс (fee-контекст
    ОДИН на залп, не RTT×юниты), межюнитная задержка гоночной ноги = спавн треда (~0).
    Sign-фейл юнита u: ничего ещё не в сети -> хвост дропаем (nonce-гэп исключён, семантика старого
    цикла), префикс 0..u-1 уходит залпом. Alchemy-фейл юнита НЕ стопит журнал остальных — их raw уже
    в секвенсере (SEND-3 uncertain семантика: учтён sent, nonce инвалидируется вызывающим).
    Возвращает (sent, err_results, send_failed, unc_watch): sent=[(unit, hash)] как у старого
    цикла; unc_watch=[(nonce, hash, unit)] — вызывающий спавнит пост-контроль ВНЕ _SEND_LOCK."""
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
    unc_watch = []
    for u, (unit, _raw, h) in enumerate(signed_units):    # Alchemy-нога: журнал/дедуп, НЕ гонка
        try:
            w3.eth.send_raw_transaction(_raw)
        except Exception as e:
            n_unc += 1                                    # raw уже в секвенсере -> tx МОЖЕТ сесть: учтён sent
            if log:
                log.warning("volley: Alchemy-нога nonce=%d бросила (%s) — raw уже в секвенсере, "
                            "учтён sent/uncertain, журнал остальных продолжаем",
                            base_nonce + u, type(e).__name__)
            unc_watch.append((base_nonce + u, h, unit))
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
    return sent, err_results, send_failed, unc_watch


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
    _unc_watch = []                # (nonce, hash, unit) uncertain-отправок: вотчеры спавнятся ВНЕ
    with _SEND_LOCK:               # _SEND_LOCK (вотчер сам берёт лок — иначе дедлок при sync-спавне)
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
        # ЮНИТЫ: батч ON и >1 жертвы -> тир-чистая сборка assemble_units (27.07 «пылевой догруз»):
        # ярусы по tip не смешиваются в юните (батч платит fee жирнейшего), EV-демоция вниз по
        # лестнице, киты первыми нонсами, пыле-батчи хвостом; хвост за max_inflight = deferred.
        # Иначе каждая жертва = свой юнит (путь бит-в-бит старый).
        if getattr(cfg, "batch_liquidate", False) and len(_deduped) > 1:
            units, _ev_dropped = assemble_units(_deduped, cfg, max_inflight, log=log)
            results.extend(_ev_dropped)
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
            sent, _verr, _send_failed, _unc_v = _send_units_volley(rpc, w3, cfg, units, base_nonce, tip, _bcast, log)
            _unc_watch.extend(_unc_v)
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
                    _unc_watch.append((base_nonce + u, res["hash"], unit))
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
    for _n2, _h2, _u2 in _unc_watch:                         # пост-контроль uncertain — спавн вне лока
        _spawn_uncertain_watch(w3, bot, _n2, _h2, _u2, log=log)   # (метки _RECENT_SENDS уже стоят)
    if sent:                                                 # фоновая сверка nonce с RPC (вне лока)
        threading.Thread(target=_reconcile_nonce, args=(w3, bot), daemon=True).start()
    for _unit, _h in sent:                                   # consume-хук (05.08): отправленный direct-сайз
        for _mid, _b, _p in _unit:                           # списывает живой кап — следующая стройка видит
            _sm = _p.get("swap_meta")                        # остаток пула, а не полный кап
            if _sm:
                _nc = consume_live_cap(_sm[0], _sm[1], _sm[2])
                if _nc is not None and log:
                    log.info("LIVECAP consume %s->%s: -%d -> %d (dispatch %s/%s)",
                             _sm[0][:8], _sm[1][:8], _sm[2], _nc, _mid[:10], _b[:10])

    if not wait_receipts:
        _inflight_add(-len(sent))    # -1 на tx (юнит); receipts не отслеживаются -> не копим фантомный inflight
        for unit, h in sent:
            for mid, borrower, prep in unit:
                results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                                "status": None, "gas_used": None, "net_usd": prep["net_usd"]})
        return results

    # ЗАМЕР 08.08 (месяц журнала): немота _INFLIGHT p50=1.8с/tx, в каскаде 17:00 объединение
    # 7.5с из 9.9с горячего окна (75%) — и ~половина брекета была НЕ полётом, а пост-диагностикой
    # (_revert_reason: ретрай sleep 1.5с; _submit_xray: sleep 2.0с + get_logs/get_transaction/
    # get_block). «tx в полёте» кончается квитанцией либо таймаутом: ПРОХОД 1 собирает квитанции
    # и отпускает _INFLIGHT (гейты проб открываются сразу), ПРОХОД 2 — диагностика и книжки.
    # Инвалидация nonce на таймауте остаётся в моменте обнаружения (проход 1).
    _got = []                                                 # (unit, hash, rcpt|None, err|None)
    for unit, h in sent:
        _rcpt = None
        _rerr = None
        try:
            _rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
        except Exception as e:
            # HIGH-фикс: tx могла быть ДРОПНУТА из мемпула (не замайнена за timeout) -> инвалидация
            # nonce (следующий dispatch пере-читает RPC и закрывает возможную дыру)
            _rerr = e
            with _SEND_LOCK:
                _LOCAL_NONCE.pop(bot, None)
        finally:
            _inflight_add(-1)    # -1 на tx (юнит): receipt получен либо timeout; диагностика ниже — не полёт
        _got.append((unit, h, _rcpt, _rerr))
        if log:
            # Положительное доказательство конца брекета для замера немоты: терминальные маркеры
            # диагностики уехали в проход 2 и момент декремента больше не видны в журнале.
            log.info("dispatch: inflight released tx=%s outcome=%s left=%d",
                     h, "receipt" if _rerr is None else "timeout", current_inflight())
    for unit, h, rcpt, _rerr in _got:
        try:
            if _rerr is not None:
                raise _rerr                                   # хвостовые строки книжит общий except ниже
            status = rcpt.get("status")
            _blk = rcpt.get("blockNumber"); _txi = rcpt.get("transactionIndex")
            if len(unit) == 1:                                # --- одиночный путь: бит-в-бит старый ---
                mid, borrower, prep = unit[0]
                net_usd, gas_usd = realized_econ(prep["profit_usd"], int(rcpt.get("gasUsed") or 0),
                                                 int(rcpt.get("effectiveGasPrice") or 0), status, cfg.eth_price_usd)
                row = {"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                       "status": status, "gas_used": rcpt.get("gasUsed"),
                       "net_usd": net_usd, "gas_usd": gas_usd, "tx_index": _txi}
                if status == 1:
                    if log:
                        log.info("submit-xray: WIN idx=%s block=%s tx=%s", _txi, _blk, h)
                    # #58: победа боевой tx подтверждена КВИТАНЦИЕЙ (не «uncertain») — только
                    # она взводит WIN-паузу, и только на своём фиде (решение владельца #46)
                    _note_combat_win(mid, borrower, proven=True, log=log)
                    # ОГРЫЗОК: наш кап-выстрел ПОДТВЕРЖДЁН квитанцией — переводим намерение в
                    # факт, и с этого момента остаток залога <=20 bps читается как труп. Только
                    # НАШ выстрел: чужая частичная ликвидация сюда не попадает по построению.
                    try:
                        from chain.hotpath import note_cap_shot_confirmed
                        note_cap_shot_confirmed(mid, borrower)
                    except Exception:                       # noqa: BLE001
                        pass
                if status != 1:
                    reason = _revert_reason(rpc, h, _blk)
                    row["revert_reason"] = reason
                    if log:
                        log.warning("liquidate revert %s/%s reason=%s tx=%s", mid[:10], borrower[:10], reason, h)
                    _xr = _submit_xray(rpc, borrower, _blk, _txi, log, cfg=cfg,
                                       our_debt_usd=prep.get("debt_usd"))
                    row["xray"] = _xr
                _fl_record(prep.get("fast"), status, getattr(cfg, "fast_lane_max_consec_reverts", 3),
                           log, xray=row.get("xray"),
                           pair=((prep.get("mp") or {}).get("collateralToken"),
                                 (prep.get("mp") or {}).get("loanToken")))
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
                    # БАТЧ: пара НЕ передаётся намеренно — в юните могут быть РАЗНЫЕ пары, и
                    # приписать один top-level реверт какой-то одной значило бы гасить невиновную.
                    # Батчевый шторм ловит свой механизм (_batch_record), здесь только глобальный слой.
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
                            _note_combat_win(mid, borrower, proven=True, log=log)
                            # ОГРЫЗОК: КАСКАДНЫЙ путь тоже стреляет кап-планами, и без этой
                            # точки правило было бы мертво ровно там, где кап-ветка встречается
                            # чаще всего. Факт здесь СИЛЬНЫЙ: Liquidated нашего контракта в
                            # квитанции нашей же tx.
                            try:
                                from chain.hotpath import note_cap_shot_confirmed
                                note_cap_shot_confirmed(mid, borrower)
                            except Exception:                 # noqa: BLE001
                                pass
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
            # outcome unknown -> conservatively book estimated cost as a LOSS, never a phantom profit.
            # Сюда падают И перекинутый таймаут квитанции, И осечка самой диагностики. Nonce здесь
            # НЕ трогаем: на таймауте pop уже сделан в проходе 1 в момент детекта, а повторный pop
            # между проходами снёс бы СВЕЖИЙ кэш параллельного dispatch/арма (гейты открыты с
            # прохода 1); при осечке диагностики квитанция на руках — nonce потреблён on-chain,
            # кэш валиден. Timeout-ветка: суммарный est-cost ОДИН раз (на первую строку юнита),
            # остальные строки нулевые -> сумма/день точна. _INFLIGHT уже отпущен в проходе 1.
            for j, (mid, borrower, prep) in enumerate(unit):
                _first = (j == 0)
                results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                                "status": None, "gas_used": None,
                                "net_usd": (-prep["cost_usd"] if _first else 0.0),
                                "gas_usd": (prep["cost_usd"] if _first else 0.0),
                                "reason": f"receipt timeout: {type(e).__name__}"})
    return results
