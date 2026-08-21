"""Feed map + sub-block oracle-update reader (rotation-aware). HOT-BUILD STAGE 1: DETECT ONLY.
resolve_feeds(rpc, markets) -> {watch_key_lower: [market_id]} via idToMarketParams(id).oracle ->
все Chainlink-ноги оракула (BASE_FEED_1 + живые BASE_FEED_2/QUOTE_FEED_1/QUOTE_FEED_2) ->
proxy.aggregator(); rebuilt from CURRENT covered (follows rotation).
watch_feeds(...) subscribes to Flashblocks, parses diff/transactions for `transmit` to those
aggregators, calls on_update(market_ids, info) — NOTHING ELSE. No price, no assess, no send.
Import-clean: websockets/brotli/Morpho ABIs imported lazily.
"""
from __future__ import annotations
import logging
import os
import time
from collections import defaultdict

log = logging.getLogger("liquidator")

FB_URL = "wss://mainnet.flashblocks.base.org/ws"

# --- 20.08: ЧТЕНИЯ WS С ВЫДЕЛЕННОГО ИСХОДЯЩЕГО АДРЕСА ------------------------------------------
# Замер трёх точек (13:13-13:18, вотчеры Шарлотт-IP1 / Шарлотт-IP2 / Вена) поймал троттлинг
# flashblocks ПО АДРЕСУ: с основного IP 26-78 сообщений/мин и 1-2 обрыва КАЖДУЮ минуту, со
# ВТОРОГО адреса ТОЙ ЖЕ МАШИНЫ в те же минуты — ровно 330/мин и ноль обрывов (Вена так же 330).
# Значит ключ — не машина и не маршрут, а вход. Лечение: держать ЧТЕНИЯ суб-блоков на чистом
# адресе, отдельном от того, с которого летят пробы (пробы = 110/с в секвенсер той же
# инфраструктуры base.org, и это самый вероятный источник репутации адреса).
# Пусто -> прежнее поведение бит-в-бит (адрес выбирает ядро).
WS_READ_SRC_IP = (os.environ.get("WS_READ_SRC_IP", "") or "").strip()


def ws_bound_socket(url: str, src_ip: str = None, timeout: float = 20.0):
    """TCP-сокет к хосту url, привязанный к src_ip. None/пусто -> None (обычный путь)."""
    ip = (src_ip if src_ip is not None else WS_READ_SRC_IP) or ""
    if not ip:
        return None
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    host, port = u.hostname, (u.port or (443 if u.scheme in ("wss", "https") else 80))
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    fam, typ, proto, _c, sa = infos[0]
    s = socket.socket(fam, typ, proto)
    s.bind((ip, 0))                      # ошибка привязки НЕ глотается: канал чтения обязан
    s.settimeout(timeout)                # быть предсказуемым, а тихий откат на грязный адрес
    s.connect(sa)                        # вернул бы нас в троттлинг молча
    s.settimeout(None)
    return s

ZERO = "0x0000000000000000000000000000000000000000"


def _addr_abi(n):
    return [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "address"}]}]


def _uint_abi(n):
    return [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "uint256"}]}]


_TRANSPORT_ERRS = ("HTTPError", "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
                   "SSLError", "RequestException", "ChunkedEncodingError", "ProtocolError")


def _is_transport(e):
    """Транспортная ошибка (429/сеть/таймаут) vs легитимный ответ ноды (revert «нет функции» =
    ContractLogicError, пустой return = BadFunctionCallOutput). По имени типа — без импорта
    requests в import-clean модуль; ретраить через фолбэк имеет смысл ТОЛЬКО транспорт: ретрай
    легитимных ревертов (~60-80 отсутствующих ног на резолв) на медленном публичном фолбэке
    раздувал резолв с ~80с до 6+ мин (наблюдалось 17:56-18:02 23.07)."""
    return type(e).__name__ in _TRANSPORT_ERRS


def _try_addr(rpc, addr, getter, fallback_rpc=None):
    """Адрес-геттер с проглотом ошибок; ТРАНСПОРТНОЕ исключение ретраится один раз через
    fallback_rpc — иначе стартовый залп бота жжёт rate-бакет основного RPC ровно во время
    резолва, пер-ножные 429 глотались МОЛЧА и ноги выпадали из карты (23↔25 ключей между
    рестартами 07-23). Легитимный revert/пустой return -> None сразу, без фолбэка."""
    try:
        v = getattr(rpc.contract(addr, _addr_abi(getter)).functions, getter)().call()
    except Exception as e:
        if fallback_rpc is not None and _is_transport(e):
            return _try_addr(fallback_rpc, addr, getter)
        return None
    return v if v and v != ZERO else None


# Read-time композит-оракулы: BASE_FEED_1 без aggregator() — сам композит НИКОГДА не получает tx
# (значение он форвардит из стороннего push-адаптера при чтении), т.е. как watch-ключ он мёртв и
# trig=feed для рынка невозможен. Вместо адреса ставим hex-ПОДСТРОКУ update-tx: bare-match в hotpath
# ищет agg[2:] по всему raw флешблока, так что подстрока calldata работает как и адрес. Ключ такого
# паттерна короче 42 символов — по этому признаку hotpath его отличает в DIAG. Пары ниже проверены
# on-chain (accessList latestRoundData -> адаптер; calldata апдейта содержит feedId ASCII).
WATCH_PATTERN_OVERRIDES = {
    # Moonwell Bounded Composite (LBTC/cbBTC 0x4944a116…): форвардит RedStone LBTC_FUNDAMENTAL из
    # MultiFeedAdapter 0xb81131b6…; ловим ASCII-hex feedId — он есть ТОЛЬКО в апдейтах этого фида.
    "0x31d099c106cd73e731972fdf1390cab77f59dade": "0x" + "LBTC_FUNDAMENTAL".encode().hex(),
    # Тот же RedStone-фид НАПРЯМУЮ как BASE_FEED_1 (LBTC/cbBTC lltv 0.945, 0x30767836…, найден
    # классификацией 07-23): значение из того же адаптера — тот же паттерн; рынки группируются
    # под одним ключом и ассессятся одним триггером.
    "0x5c4c8d6f6bf79b718f3e8399aabdfed01cb7e48f": "0x" + "LBTC_FUNDAMENTAL".encode().hex(),
}


# Оракулы-обёртки БЕЗ Chainlink-геттеров (no-BF1 «фантомы» классификации 07-23): резолв по
# геттерам даёт (None, []) и рынок МОЛЧА выпадает из watch. 28.07 это стоило двух ликвидаций:
# JitoSOL/USDC (0x09276541…) был невидим реактивному пути с деплоя — SOL-дамп, spec-проба
# предсказала жертву за 20с, а стрелять было нечем (рынок вне watch, prearm его не грел).
# Ноги ниже найдены eth_createAccessList на price() и проверены on-chain (description/возраст).
# {oracle_lower: [watch-ключи]} — первый ключ primary. Медленные ноги (staking rate hb 24ч,
# SGD/USD hb 24ч) сюда НЕ идут — та же философия, что возраст-фильтр extra-ног.
ORACLE_LEG_OVERRIDES = {
    # JitoSOL/USDC: обёртка читает SOL/USD OCR-агрегатор (hb ~10мин, деньги) + JITOSOL-SOL
    # Calculated (staking rate, hb 24ч — вне watch). Свип 28.07 остальных безногих рынков:
    # rate-класс/стейблы/hardcoded — суб-блочная фора им не нужна, не добавляем.
    "0x78f772f5fcc03256260cc1165e34da9bb3f9be1c": ["0x43151d66d653ad888bf909f006a01792d5f33243"],
    # wsuperOETHb/WETH + /msETH (один оракул): price() = wsuperOETHb.convertToAssets, курс
    # меняется ТОЛЬКО ребейзом superOETHb — слешинг Origin = негативный ребейз тем же tx.
    # Ключ = адрес superOETHb: bare-match ловит ребейз суб-блочно (~1 tx/5мин, throttle 0.25с).
    # «Детонатор»-класс (kelbic 28.07): LST-стресс флипает пачку ножей cbETH/weETH/wsuperOETHb
    # разом; cbETH и weETH уже в watch своими Chainlink-ногами, этот был полл-only.
    "0x28c964c985fe84736fadc7cf0bbd58b54bc7cf93": ["0xdbfefd2e8460a6ee4955a68582f85708baea60a3"],
}


# Не-primary ноги оракула (двуногая база и quote-сторона): транзит ЛЮБОЙ ноги меняет price()
# композита, но до 07-23 watch держал только BASE_FEED_1 — апдейт ETH/USD-ноги cbBTC/WETH не
# армил рынок суб-блочно (детекцию давал только per-block poll, без форы). Ноги ниже идут в watch
# через возраст-фильтр _leg_live: медленные стейбл-ноги (USDC/USD hb 24ч, age ~23ч — шум) и
# мёртвые/hardcoded фантомы классификации 07-23 в hot-set не попадают.
EXTRA_LEG_GETTERS = ("BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2")
EXTRA_LEG_MAX_AGE_SEC = 3 * 3600.0   # market-ноги (ETH/USD hb 20мин) всегда моложе; стейблы (hb 24ч) — старше

_PRIMARY_AGG: dict = {}   # {market_id: BF1 watch-key} последнего resolve_feeds (см. primary_map)
_LEGS_AGG: dict = {}      # {market_id: [все watch-ключи рынка]} последнего resolve_feeds
_MAP_TS = [0.0]           # когда карту последний раз ОБНОВЛЯЛ успешный резолв (см. map_age)


def map_age():
    """Возраст карты владения в секундах (inf — не строилась ни разу).

    Возраст обязан быть ЧИТАЕМЫМ снаружи: «карта старая» и «суд вслепую» — разные вещи, и
    путать их значит принять слепоту за факт «владельца нет»."""
    if not _MAP_TS[0]:
        return float("inf")
    return max(0.0, time.time() - _MAP_TS[0])


def legs_map():
    """{market_id: [ВСЕ watch-ноги рынка]} последнего resolve_feeds (primary первой).

    Зачем отдельно от primary_map. Владение транзитом — свойство РЫНКА, а не одной его ноги:
    композит цены двигает ЛЮБАЯ из ног (07-23 multi-leg именно поэтому и вводился), и в
    up-окне рынка cbBTC/WETH транзит приезжает от ETH/USD-ноги, то есть НЕ от BF1. Судить по
    одному BF1 значит объявлять собственный транзит ЧУЖИМ ровно в тех окнах, ради которых
    метрика позиций и снимается.
    """
    return {k: list(v) for k, v in _LEGS_AGG.items()}


def primary_map():
    """{market_id: primary(BF1) watch-key} последнего resolve_feeds. Гард shadow-карты hotpath:
    рынок теперь числится и под extra-ногами, а _SHADOW_SCALE выведен для СВОЕГО BF1-фида —
    посадить его на чужую ногу (cbBTC-scale на USDC/USD, когда стейбл-нога в депег оживает и
    проходит возраст-фильтр) = ложный px_hint в feed-спавн."""
    return dict(_PRIMARY_AGG)


def _leg_agg(rpc, oracle, getter, fallback_rpc=None):
    """Одна нога оракула: getter -> proxy -> aggregator (lower), с подменой watch-паттерна;
    None если ноги нет (getter отсутствует/ZERO). Фолбэк-ретрай на каждом вызове (см. _try_addr):
    без него транспорт-фейл aggregator() подставлял ПРОКСИ как watch-ключ (`or proxy` ниже
    легитимен только для фидов без агрегатора, напр. прямой RedStone) = мёртвый ключ. I/O."""
    proxy = _try_addr(rpc, oracle, getter, fallback_rpc)
    if not proxy:
        return None
    agg = (_try_addr(rpc, proxy, "aggregator", fallback_rpc) or proxy).lower()
    return WATCH_PATTERN_OVERRIDES.get(agg, agg)


def _leg_live(rpc, agg, fallback_rpc=None):
    """Возраст-фильтр extra-ноги: latestTimestamp агрегатора не старше EXTRA_LEG_MAX_AGE_SEC.
    Watch-паттерны (len != 42) на чейне не читаемы — проверены вручную, пропускаем. Нет
    latestTimestamp (hardcoded/не-Chainlink) или старый — нога мёртвая/стейбл, в watch не идёт.
    Исключение (транспорт) — ретрай через fallback_rpc, иначе 429 в старт-залп молча ронял
    живую ногу. I/O."""
    if len(agg) != 42:
        return True
    try:
        ts = rpc.contract(agg, _uint_abi("latestTimestamp")).functions.latestTimestamp().call()
    except Exception as e:
        if fallback_rpc is not None and _is_transport(e):
            return _leg_live(fallback_rpc, agg)
        return False
    return bool(ts) and (time.time() - int(ts)) <= EXTRA_LEG_MAX_AGE_SEC


def _resolve_one(rpc, market_id):
    """market_id -> OCR aggregator (lower), watch-паттерн из WATCH_PATTERN_OVERRIDES для read-time
    композитов, или None (non-Chainlink wrapper). ТОЛЬКО BF1-нога — дешёвый резолв для офлайн-
    аналитики (replay_winners/reachable_money/gap_profile/net_after_fee); боевой multi-leg
    резолв — _resolve_legs. I/O."""
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)
    try:
        oracle = morpho.functions.idToMarketParams(rpc.to_bytes32(market_id)).call()[2]
    except Exception:
        return None
    ov = ORACLE_LEG_OVERRIDES.get(oracle.lower())
    if ov:
        return ov[0]
    return _leg_agg(rpc, oracle, "BASE_FEED_1")


def _resolve_legs(rpc, market_id, fallback_rpc=None):
    """market_id -> (primary, legs): primary = BF1-нога (исторический одноногий watch, БЕЗ
    возраст-фильтра — поведение primary не меняем) или None; legs = все watch-ноги рынка
    (primary первой + живые BF2/QF1/QF2 через _leg_live, без дублей). (None, []) = non-Chainlink
    wrapper ЛИБО транспортный шторм — различает пер-рыночный повтор в resolve_feeds.
    fallback_rpc пробрасывается до КАЖДОГО вызова ноги (пер-ножный ретрай). I/O; patched in tests."""
    from chain.morpho import MORPHO_BLUE_ADDRESS
    from chain.simulate import MORPHO_READ_ABI
    morpho = rpc.contract(MORPHO_BLUE_ADDRESS, MORPHO_READ_ABI)
    try:
        oracle = morpho.functions.idToMarketParams(rpc.to_bytes32(market_id)).call()[2]
    except Exception:
        return None, []
    ov = ORACLE_LEG_OVERRIDES.get(oracle.lower())
    if ov:
        return ov[0], list(ov)
    legs = []
    primary = _leg_agg(rpc, oracle, "BASE_FEED_1", fallback_rpc)
    if primary:
        legs.append(primary)
    for getter in EXTRA_LEG_GETTERS:
        agg = _leg_agg(rpc, oracle, getter, fallback_rpc)
        if agg and agg not in legs and _leg_live(rpc, agg, fallback_rpc):
            legs.append(agg)
    return primary, legs


def resolve_feeds(rpc, markets, fallback_rpc=None):
    """{watch_key_lower: [market_id,...]} for covered markets (grouping over _resolve_legs).
    07-23 multi-leg: рынок числится под КАЖДОЙ своей ногой (BF1 + живые BF2/QF1/QF2) — транзит
    любой ноги меняет price() композита и должен армить рынок; раньше ETH/USD-нога cbBTC/WETH
    была вне watch. Primary-ноги последнего резолва — через primary_map() (гард shadow-карты).
    Non-Chainlink markets (ни одной Chainlink-ноги) omitted — separate reader branch.
    fallback_rpc: _resolve_legs глотает транспортные ошибки (пусто), поэтому шторм 408/429 на
    основном RPC давал МОЛЧА пустую карту — рестарт/ре-резолв в шторм слепил hot-path (инцидент
    07-23: «hot path: 0 aggregators / 40 markets» на 6ч rescan-интервала). Пустоту от основного
    повторяем через запасной эндпоинт; легитимно пустые (не-Chainlink) просто пусты дважды."""
    aggs = defaultdict(list)
    prim = {}
    legs_by_market = {}
    for m in markets:
        primary, legs = _resolve_legs(rpc, m.market_id, fallback_rpc)
        if not legs and fallback_rpc is not None:
            primary, legs = _resolve_legs(fallback_rpc, m.market_id)
        if primary:
            prim[m.market_id] = primary
        if legs:
            legs_by_market[m.market_id] = list(legs)
        for agg in legs:
            aggs[agg].append(m.market_id)
    # ЖИВУЮ КАРТУ ВЛАДЕНИЯ ПУСТОТОЙ НЕ ЗАТИРАЕМ (ревью р-3, условие 4). Прежняя редакция звала
    # clear()+update() ДО того, как узнавала, ЧТО принёс резолв: штормовой (пустой) ре-резолв
    # обнулял и _LEGS_AGG, и запасной _PRIMARY_AGG, а `_agg_of_market` после этого отдавал
    # None — каждая проба судилась ВСЛЕПУЮ, денежные probe_in_band/probe_tip_match были
    # структурно нулями. При rescan_interval_sec=21600 это до 6 ЧАСОВ слепоты, причём карта
    # фидов hot_loop в это время выглядит здоровой: у него свой гард (hotpath «re-resolve пуст
    # — держу старую карту»), а модульную карту не берёг никто. Шторм на обоих эндпоинтах не
    # гипотеза — ради него гард hot_loop и написан (инцидент 07-23 «hot path: 0 aggregators /
    # 40 markets»), и он совпадает по времени с каскадом, то есть с жирным окном.
    # ОБНОВЛЯЕМ ПО РЫНКАМ, а не целиком: частичный шторм (упал резолв части рынков) иначе
    # стирал бы и тех, кто резолвнулся, — `if legs:` не кладёт, а clear() уже стёр.
    _kept = [m.market_id for m in markets if m.market_id not in legs_by_market
             and m.market_id in _LEGS_AGG]
    if legs_by_market or prim:
        _MAP_TS[0] = time.time()
    for _mid, _lg in legs_by_market.items():
        _LEGS_AGG[_mid] = list(_lg)
    for _mid, _p in prim.items():
        _PRIMARY_AGG[_mid] = _p
    if _kept:
        log.warning("КАРТА ВЛАДЕНИЯ не обновлена по %d рынкам из %d (резолв вернул пусто — "
                    "шторм на обоих эндпоинтах?), держу прежние ноги, возраст карты %.0fс; "
                    "стереть их значило бы судить позиции ВСЛЕПУЮ до следующего rescan",
                    len(_kept), len(markets), map_age())
    return dict(aggs)


FORWARD_SEL = "6fadcf72"      # forward(address,bytes) — обёртка OCR-transmit (роутеры 0xb277/0x79fc/0x5e46)
FORWARD_ANSWER_OFF = 676      # эмпирический offset answer в payload (STATE Идея 2: полноразрядно доказан)


def decode_forward_answer(raw_hex):
    """raw flashblock tx hex -> (aggregator_lower, answer_int) для forward-обёрнутого OCR-transmit,
    иначе None. Чистый слайсинг по hex: находим селектор forward, arg1 = адрес агрегатора (последние
    20 байт слова 1), answer int256 по эмпирическому offset 676 от начала payload (полноразрядное
    совпадение на НЕокруглённом тике, 4 транзита / 3 коллатерала — STATE 'Идея 2, слой 1'). Без
    RLP-парсинга: смещения относительно позиции селектора в raw. None при любом несовпадении формы
    (нет селектора / короткий payload / знаковый бит) — реальный фильтр от селектора-двойника в RLP
    делает вызывающая сторона по карте известных фидов."""
    if not raw_hex:
        return None
    i = raw_hex.find(FORWARD_SEL)
    if i < 0:
        return None
    need = i + 8 + (FORWARD_ANSWER_OFF + 32) * 2
    if len(raw_hex) < need:
        return None
    agg = "0x" + raw_hex[i + 32 : i + 72]                        # arg1: address = хвост слова 1
    try:
        answer = int(raw_hex[i + 8 + FORWARD_ANSWER_OFF * 2 : need], 16)
    except ValueError:
        return None
    if answer >= 1 << 255:                                       # int256: отрицательных цен не бывает
        return None
    return agg, answer


def tip_from_raw(raw_hex, basefee=None):
    """tip (maxPriorityFeePerGas, wei) из СЫРОЙ tx кадра суб-блока, либо None.

    21.08 (живое кольцо зеркала): класс tip эпизода съезжает за минуты (7.5→3.7 мгвей за
    утро 21.08), а кольцо кормилось только полными сканами индекса (19% блоков, с лагом) —
    tip-match=0 в 4/4 вердиктов эпизода. Кадр суб-блока несёт транзитную tx целиком, tip
    лежит в её RLP: третье поле для type-2/3, gasPrice−basefee для type-0/1 (basefee — из
    того же кадра). Мини-ридер первых трёх RLP-полей: O(десятков байт), без зависимостей.
    Любая несходимость формы -> None (незнание не кормит кольцо)."""
    if not raw_hex:
        return None
    try:
        h = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
        b = bytes.fromhex(h[:220])          # первые 3 поля живут в первых ~40 байтах
        typ = b[0]
        if typ in (1, 2, 3):
            i = 1
        elif typ >= 0xc0:                    # legacy: RLP-список сразу
            i = 0
        else:
            return None

        def _item(pos):
            p = b[pos]
            if p < 0x80:
                return int(p), pos + 1
            if p < 0xb8:
                ln = p - 0x80
                return int.from_bytes(b[pos + 1:pos + 1 + ln], "big"), pos + 1 + ln
            return None, None                # длинное поле в первых трёх не встречается

        # заголовок списка
        p = b[i]
        if p >= 0xf8:
            i += 1 + (p - 0xf7)
        elif p >= 0xc0:
            i += 1
        else:
            return None
        if typ in (1, 2, 3):
            _, i = _item(i)                  # chain_id
            if i is None:
                return None
            _, i = _item(i)                  # nonce
            if i is None:
                return None
            v, _ = _item(i)                  # type-2/3: maxPriorityFee; type-1: gasPrice
            if v is None:
                return None
            if typ == 1:
                return (v - int(basefee)) if basefee else None
            return int(v)
        _, i = _item(i)                      # legacy: nonce
        if i is None:
            return None
        v, _ = _item(i)                      # gasPrice
        if v is None or not basefee:
            return None
        return int(v) - int(basefee)
    except Exception:                        # noqa: BLE001
        return None


def extract_txs(d):
    diff = d.get("diff") or {}
    txs = diff.get("transactions")
    if txs is None:
        txs = d.get("transactions") or []
    out = []
    for t in txs:
        if isinstance(t, str):
            out.append((t.lower(), None))
        elif isinstance(t, dict):
            raw = t.get("raw") or t.get("rlp") or t.get("input")
            out.append(((raw.lower() if isinstance(raw, str) else None),
                        (t.get("to") or "").lower() or None))
        else:
            out.append((None, None))
    return out


def match_aggs(txs, agg_set):
    hit = set()
    for raw, to in txs:
        if to and to in agg_set:
            hit.add(to)
        elif raw:
            for a in agg_set:
                if a[2:] in raw:
                    hit.add(a)
    return hit


def basefee_of(d):
    """baseFee из index-0 кадра (base-хедер), int wei; None если кадр без base/поля. Имя поля —
    snake_case как block_number; camelCase-фолбэк на случай варианта шлюза (живой DIAG покажет)."""
    base = d.get("base") if isinstance(d.get("base"), dict) else {}
    bf = base.get("base_fee_per_gas") or base.get("baseFeePerGas")
    if bf is None:
        return None
    return int(bf, 16) if isinstance(bf, str) and bf.startswith("0x") else int(bf)


def block_number_of(d):
    base = d.get("base") if isinstance(d.get("base"), dict) else {}
    meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    bn = base.get("block_number") or meta.get("block_number")
    if bn is None:
        return None
    return int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else int(bn)


async def watch_feeds(rpc, markets_loader, on_update, *, duration=None,
                      feed_rescan_sec=3600.0, ws_factory=None, log=None):
    """Subscribe to Flashblocks; on oracle update to one of our aggregators in a sub-block, call
    on_update(market_ids, info). DETECT ONLY. markets_loader re-called every feed_rescan_sec
    (rotation). info={block,index,agg,t}. ws_factory: test seam."""
    import asyncio
    import json
    import websockets
    import brotli

    def _default_ws():
        import ssl as _ssl
        _sock = ws_bound_socket(FB_URL)
        if _sock is None:
            return websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None)
        return websockets.connect(FB_URL, sock=_sock, ssl=_ssl.create_default_context(),
                                  server_hostname="mainnet.flashblocks.base.org",
                                  open_timeout=20, ping_interval=20, max_size=None)

    ws_factory = ws_factory or _default_ws
    feeds = resolve_feeds(rpc, markets_loader())
    agg_set = set(feeds)
    last_resolve = time.monotonic()
    if log:
        log("feed-watch: %d aggregators from %d markets (live resolve)" % (len(agg_set), sum(len(v) for v in feeds.values())))
    t_end = (time.monotonic() + duration) if duration else None

    async with ws_factory() as ws:
        while t_end is None or time.monotonic() < t_end:
            if time.monotonic() - last_resolve >= feed_rescan_sec:
                try:
                    feeds = resolve_feeds(rpc, markets_loader())
                    agg_set = set(feeds)
                    last_resolve = time.monotonic()
                    if log:
                        log("feed-watch: re-resolved -> %d aggregators" % len(agg_set))
                except Exception as e:
                    if log:
                        log("feed-watch: re-resolve failed: %r" % e)
            timeout = 5.0 if t_end is None else max(1.0, t_end - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                if t_end is not None:
                    break
                continue
            try:
                txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                d = json.loads(txt)
            except Exception:
                continue
            bn = block_number_of(d)
            if bn is None:
                continue
            hits = match_aggs(extract_txs(d), agg_set)
            if not hits:
                continue
            idx = d.get("index")
            now = time.monotonic()
            for a in hits:
                on_update(feeds.get(a, []), {"block": bn, "index": idx, "agg": a, "t": now})
