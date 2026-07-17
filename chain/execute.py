"""Execute layer (Phase 2). Builds the on-chain liquidation bundle and gates it with a live
eth_call simulation before any send (no send happens in this module). Aggregator = KyberSwap
(keyless) for the collateral->loan swap; the contract validates the OUTCOME (minProfit gate),
so the route itself is untrusted-but-checked.

stdlib HTTP (urllib) like morpho.py — runs on `python3`; only the eth_call path needs web3 (venv).
"""
from __future__ import annotations
import json
import logging
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


# ⭐ CANARY (замер inclusion-гонки, Вариант B: in-process, ОБЩИЙ кошелёк/nonce). Состояние под
# _INFLIGHT_LOCK. Газ канарейки — в СВОЙ бакет (_CANARY_GAS_USD), НЕ в kill-switch realized_today.
_CANARY_PROBES = 0
_CANARY_GAS_USD = 0.0
_CANARY_LAST_TS = 0.0


def canary_stats() -> tuple:
    with _INFLIGHT_LOCK:
        return (_CANARY_PROBES, _CANARY_GAS_USD)


def canary_record_gas(gas_usd: float):
    global _CANARY_GAS_USD
    with _INFLIGHT_LOCK:
        _CANARY_GAS_USD += max(0.0, gas_usd)


def send_canary(rpc, cfg, log=None, max_fee=None, tip=None, broadcast_raw_urls=None):
    """⭐ Минимальная 0-value self-transfer от БОЕВОГО кошелька под ОБЩИМ _SEND_LOCK/_LOCAL_NONCE
    (единый nonce -> ноль nonce-краха; single-slot). Гейты (кап проб / газ-кап / интервал) проверяются
    АТОМАРНО под локом ПЕРЕД раздачей nonce. Возвращает hash или None (гейт закрыт/ошибка). Позиция
    tx в блоке = прокси позиции ликвидации при равном tip -> замер гонки. НЕ считается в kill-switch.
    Поправка 07-17: канарейка должна лететь БОЕВЫМ send-путём — max_fee/tip из fb-хедера (иначе
    send_tx жжёт 2 Alchemy-RTT на fee) и broadcast_raw_urls=[секвенсер] (SEND-3; иначе только
    Alchemy-прокси-хоп, которого у реальной ликвидации нет) — пробы 1-100 мерили ИМЕННО этот оверхед."""
    global _CANARY_PROBES, _CANARY_LAST_TS
    _now = time.monotonic()
    with _SEND_LOCK:
        with _INFLIGHT_LOCK:
            if (_CANARY_PROBES >= getattr(cfg, "canary_max_probes", 100)
                    or _CANARY_GAS_USD >= getattr(cfg, "canary_max_gas_usd", 25.0)
                    or _now - _CANARY_LAST_TS < getattr(cfg, "canary_min_interval_sec", 120.0)):
                return None
        w3 = rpc._web3()
        bot = account_for(w3, cfg.wallet_key).address
        base_nonce = _LOCAL_NONCE.get(bot)
        if base_nonce is None:
            base_nonce = int(w3.eth.get_transaction_count(bot, "pending"))
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
            _CANARY_PROBES += 1
            _CANARY_LAST_TS = _now
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
_UNIV3_WETH_USDC_FEE = 500
_UNIV3_EXACT_INPUT_SEL = "b858183f"    # exactInput((bytes,address,uint256,uint256)) verified cast sig
_AERO_EXACT_INPUT_SEL = "c04b8d59"     # exactInput((bytes,address,uint256,uint256,uint256)) verified bytecode
_AERO_DEADLINE = 2 ** 63               # v1-форма требует deadline; константа = «не истекает»
UNIV3_PATHS = {  # collateral(lower) -> fee tier of coll/WETH pool (verified factory.getPool)
    "0xcb585250f852c6c6bf90434ab21a00f02833a4af": 3000,   # cbXRP/WETH
    "0xcbd06e5a2b0c65597161de254aa074e489deb510": 3000,   # cbDOGE/WETH
    "0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c": 3000,   # cbADA/WETH
}


def _pack_path(*hops) -> bytes:
    """UniV3/Slipstream path: token (+ 3-байтовый fee/tickSpacing + token)*. Чистая (юнит)."""
    out = bytes.fromhex(hops[0][2:])
    for i in range(1, len(hops), 2):
        out += int(hops[i]).to_bytes(3, "big") + bytes.fromhex(hops[i + 1][2:])
    return out


_SWAP_ROUTES = {  # (collateral_lower, loan_lower) -> {router, style, path}
    (_CBBTC, _EURC): {"router": AERO_CL_ROUTER, "style": "v1",
                      "path": _pack_path(_CBBTC, 100, _EURC)},
    (_CBLTC, _UNIV3_USDC): {"router": AERO_CL_ROUTER, "style": "v1",
                            "path": _pack_path(_CBLTC, 200, _UNIV3_WETH, 100, _UNIV3_USDC)},
    # CBBTC_PLAN 07-09: cbBTC/USDC — QuoterV2 live: $2k->$2009, $30k->$30103 (~0.3% impact); fee 500
    # на обоих хопах чуть лучше 3000 (пулы верифицированы 07-08: cbBTC/WETH 500 liq>0, WETH/USDC 500).
    (_CBBTC, _UNIV3_USDC): {"router": UNIV3_SWAP_ROUTER, "style": "02",
                            "path": _pack_path(_CBBTC, 500, _UNIV3_WETH, 500, _UNIV3_USDC)},
    # LBTC/cbBTC 07-12 (нож $350.8k долга, BTC-пег пара — профиль «мгновенный флип на ноже»):
    # UniV3 ПУСТ на всех тирах (QuoterV2-проба impact~99.6%, analysis/lbtc_route_probe.py);
    # Slipstream ts=1 ЖИВОЙ (router.factory()->getPool, liquidity=125.6B, 07-12); Kyber-глубина
    # $380k ratio=1.00038 (паритет) — эконом чистая, вопрос был в скорости (Kyber RTT ~0.4-0.9с
    # vs fast-lane ~0.1с). Маршрут даёт fast-lane + кэш слоя 2 (ROUTE_COLLATERALS авто).
    (_LBTC, _CBBTC): {"router": AERO_CL_ROUTER, "style": "v1",
                      "path": _pack_path(_LBTC, 1, _CBBTC)},
    # wstETH/WETH 07-14 (нож $180.2k @ HF 1.0056, LST-пег — профиль «мгновенного флипа на тике»):
    # UniV3 тонкий (fee=100 imp@60wstETH=57%, fee=500 пусто), Slipstream ts=1 живой+глубокий
    # (Kyber агрегирует Slipstream: 10/40/80 wstETH ratio 1.2391 flat = 0% слиппажа до ~$360k).
    # loan=WETH -> single-hop wstETH->WETH; fast-lane + кэш слоя-2 (ROUTE_COLLATERALS авто).
    (_WSTETH, _UNIV3_WETH): {"router": AERO_CL_ROUTER, "style": "v1",
                             "path": _pack_path(_WSTETH, 1, _UNIV3_WETH)},
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
        t["nonce"] = w3.eth.get_transaction_count(acct.address, "pending")
    if "maxFeePerGas" not in t and "gasPrice" not in t:
        base = w3.eth.get_block("latest").get("baseFeePerGas") or 0
        tip = max(int(w3.eth.max_priority_fee), int(min_tip_wei))
        t["maxPriorityFeePerGas"] = tip
        t["maxFeePerGas"] = int(base) * 2 + tip
    if "gas" not in t:
        t["gas"] = int(w3.eth.estimate_gas(t) * 12 // 10)
    signed = acct.sign_transaction(t)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    _relayed = False
    if broadcast_raw_urls:
        raw_hex = "0x" + bytes(raw).hex()
        for _u in broadcast_raw_urls:
            threading.Thread(target=_post_raw_tx, args=(_u, raw_hex), daemon=True).start()
        _relayed = True
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
        return {"ok": True, "calldata": cd1, "net_usd": net_usd,
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


def _submit_xray(rpc, borrower, block_number, our_index, log):
    """P2-12 submit-side телеметрия: наш выстрел замайнился — кто взял жертву в ЭТОМ блоке и с
    каким tx-index. Узкий getLogs fromBlock==toBlock (урок 30: обходит range-limit), фильтр по
    topic3=borrower (неоспоримый источник жертвы). Дельта=our-winner: на сколько позиций в блоке
    нас обошли — ЕДИНСТВЕННАЯ честная метрика submit-side проигрыша (gap-запрет не задет: это
    наши собственные выстрелы, не пере-мер чужих). Диагностика, не роняет receipt-путь."""
    global _LIQUIDATE_EVENT_SIG
    try:
        from eth_utils import keccak
        if _LIQUIDATE_EVENT_SIG is None:
            _LIQUIDATE_EVENT_SIG = "0x" + keccak(
                text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)").hex()
        w3 = rpc._web3()
        topic_b = "0x" + borrower[2:].lower().rjust(64, "0")
        logs = w3.eth.get_logs({"fromBlock": block_number, "toBlock": block_number,
                                "address": _MORPHO_BLUE,
                                "topics": [_LIQUIDATE_EVENT_SIG, None, None, topic_b]})
        if not logs:
            if log:
                log.info("submit-xray: borrower %s НЕ ликвидирован в блоке %d (наш idx=%s) — "
                         "реверт НЕ гонка за жертву (смотреть reason)", borrower[:10], block_number, our_index)
            return
        w = logs[0]
        widx = w.get("transactionIndex")
        if log:
            log.info("submit-xray: победитель idx=%s наш idx=%s дельта=%s tx=%s block=%d",
                     widx, our_index,
                     (our_index - widx) if isinstance(our_index, int) and isinstance(widx, int) else "?",
                     w.get("transactionHash").hex() if w.get("transactionHash") is not None else "?",
                     block_number)
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
            base_nonce = int(w3.eth.get_transaction_count(bot, "pending"))
        tip = int(cfg.tip_gwei * 1e9)
        batch = _deduped[:max_inflight]

        sent = []
        _send_failed = False
        for i, (mid, borrower, prep) in enumerate(batch):
            try:
                _txd = {"to": cfg.liquidator_address, "data": prep["calldata"], "nonce": base_nonce + i}
                if prep.get("gas"):                          # explicit gas from prep (hot path: estimated on preconf)
                    _txd["gas"] = prep["gas"]                # -> send_tx skips its stale-Alchemy estimate_gas
                if prep.get("max_fee"):                      # SEND-2: fees from fb-header via prep (hot path)
                    _txd["maxFeePerGas"] = prep["max_fee"]   # -> send_tx skips BOTH Alchemy fee-RTTs
                    _txd["maxPriorityFeePerGas"] = prep["tip"]
                if send_fn is None:                          # SEND-3 только через штатный send_tx (инжектированный
                    res = send(rpc, cfg.wallet_key, _txd, wait=False, min_tip_wei=tip,   # send_fn тестов не знает kwarg)
                               broadcast_raw_urls=_bcast)
                else:
                    res = send(rpc, cfg.wallet_key, _txd, wait=False, min_tip_wei=tip)
                sent.append((mid, borrower, prep, res["hash"]))
                _RECENT_SENDS[(mid, borrower)] = time.monotonic()   # дедуп: этот заёмщик отправлен
                _inflight_add(1)
                if log:
                    log.info("dispatch: sent %s/%s nonce=%d tx=%s net~$%.2f",
                             mid[:10], borrower[:10], base_nonce + i, res["hash"], prep["net_usd"])
                if res.get("uncertain"):     # SEND-3 race: Alchemy-send бросил, raw уже в секвенсере ->
                    if log:                  # tx МОЖЕТ сесть в base_nonce+i. Записан выше как sent (дедуп
                        log.warning("dispatch: SEND-3 uncertain %s/%s nonce=%d — Alchemy бросил, raw в "
                                    "секвенсере; учтён как sent (дедуп+receipt), стоп батча + инвалидация nonce",
                                    mid[:10], borrower[:10], base_nonce + i)
                    _send_failed = True      # + receipt-ждётся -> исход/газ учтётся, дубль исключён).
                    _LOCAL_NONCE.pop(bot, None)   # неизвестно, сел ли base+i -> следующий dispatch пере-читает RPC
                    break                    # не шлём хвост в возможную дыру nonce (как при send-ошибке)
            except Exception as e:
                if log:
                    log.warning("dispatch: send FAILED at nonce %d (%s/%s): %s — stopping batch to avoid gap",
                                base_nonce + i, mid[:10], borrower[:10], type(e).__name__)
                results.append({"market_id": mid, "borrower": borrower, "sent": False,
                                "reason": f"send error: {type(e).__name__}: {str(e)[:80]}",
                                "net_usd": prep["net_usd"]})
                _send_failed = True
                _LOCAL_NONCE.pop(bot, None)  # не знаем, съелся ли nonce -> следующий dispatch пере-читает RPC
                break   # nonce gap -> later txs would be stuck; next block re-derives the base nonce
        if not _send_failed:
            _LOCAL_NONCE[bot] = base_nonce + len(sent)
    if sent:                                                 # фоновая сверка nonce с RPC (вне лока)
        threading.Thread(target=_reconcile_nonce, args=(w3, bot), daemon=True).start()

    if not wait_receipts:
        _inflight_add(-len(sent))    # receipts здесь не отслеживаются -> не копим фантомный inflight
        for mid, borrower, prep, h in sent:
            results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                            "status": None, "gas_used": None, "net_usd": prep["net_usd"]})
        return results

    for mid, borrower, prep, h in sent:
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
            status = rcpt.get("status")
            net_usd, gas_usd = realized_econ(prep["profit_usd"], int(rcpt.get("gasUsed") or 0),
                                             int(rcpt.get("effectiveGasPrice") or 0), status, cfg.eth_price_usd)
            row = {"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                   "status": status, "gas_used": rcpt.get("gasUsed"),
                   "net_usd": net_usd, "gas_usd": gas_usd,
                   "tx_index": rcpt.get("transactionIndex")}   # P2-12: submit-side позиция в блоке
            if status == 1 and log:
                log.info("submit-xray: WIN idx=%s block=%s tx=%s",
                         rcpt.get("transactionIndex"), rcpt.get("blockNumber"), h)
            if status != 1:                                  # diagnose the revert (lost race vs structural)
                reason = _revert_reason(rpc, h, rcpt.get("blockNumber"))
                row["revert_reason"] = reason
                if log:
                    log.warning("liquidate revert %s/%s reason=%s tx=%s",
                                mid[:10], borrower[:10], reason, h)
                _submit_xray(rpc, borrower, rcpt.get("blockNumber"),
                             rcpt.get("transactionIndex"), log)   # P2-12: кто взял и с какой дельтой
            _fl_record(prep.get("fast"), status,               # II.6: учёт серии ревертов fast-lane
                       getattr(cfg, "fast_lane_max_consec_reverts", 3), log)
            results.append(row)
        except Exception as e:
            # outcome unknown -> conservatively book estimated cost as a LOSS, never a phantom profit
            # HIGH-фикс: tx могла быть ДРОПНУТА из мемпула (не замайнена за timeout). Локальный nonce
            # стоит на base+len(sent), но чейн всё ещё ждёт дропнутый nonce -> следующий dispatch слал
            # бы В ДЫРУ (queued-пул навсегда) -> бот молча перестаёт майнить. _reconcile_nonce не спасает
            # (max-only отказывается откатывать вниз). Инвалидируем -> следующий dispatch пере-читает RPC:
            # дропнута -> pending=N -> пере-отправка в N закрывает дыру; просто медленная -> pending=N+1 -> верно.
            with _SEND_LOCK:
                _LOCAL_NONCE.pop(bot, None)
            results.append({"market_id": mid, "borrower": borrower, "sent": True, "hash": h,
                            "status": None, "gas_used": None,
                            "net_usd": -prep["cost_usd"], "gas_usd": prep["cost_usd"],
                            "reason": f"receipt timeout: {type(e).__name__}"})
        finally:
            _inflight_add(-1)        # receipt получен либо timeout -> tx больше не «в полёте»
    return results
