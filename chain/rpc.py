"""Base RPC access. web3 is imported lazily so the module stays import-clean for
the runtime-import check (see docs/WORKFLOW.md). Phase 1 reads only."""
from __future__ import annotations

import re
import threading
import time

# ---- РЕДАКЦИЯ СЕКРЕТОВ В ЛОГЕ (07.08: журнал отдавал полный drpc-URL с ключом в пути ~600
# строк/сутки — текст исключения RPC несёт URL целиком, а его печатали как есть). Режем ТОЛЬКО
# внутри URL и только после authority: хост обязан остаться (без него не понять, какой контур
# отвалился), хэши tx и адреса — тоже (без них вскрытие гонки невозможно).
_URL_RE = re.compile(r"https?://[^\s'\"<>,)]*")
_AUTHORITY_RE = re.compile(r"(https?://[^/?#]*)(.*)", re.S)
_SECRET_SEG_RE = re.compile(r"([/=])([A-Za-z0-9_\-]{20,})")


def _redact_one_url(u: str) -> str:
    m = _AUTHORITY_RE.match(u)
    if not m:
        return u
    return m.group(1) + _SECRET_SEG_RE.sub(lambda x: x.group(1) + "<ключ скрыт>", m.group(2))


# РЕЕСТР ЛИТЕРАЛЬНЫХ СЕКРЕТОВ (09.08, находка ревьюера). Регексп выше режет ключ только ВНУТРИ
# URL, а исключения транспорта его теряют: urllib3/DNS/SSL печатают хост и «голый» путь без
# схемы, socket.gaierror несёт только имя хоста, а requests в ряде ошибок отдаёт строку, где
# «://» нет вовсе — и тогда фильтр журнала пропускал строку целиком (гейт стоял на «://»).
# Поэтому ключи, увиденные при разборе НАШИХ URL, запоминаются как литералы и режутся где угодно.
_SECRETS: set = set()
_SECRETS_LOCK = threading.Lock()


# КОРОТКИЕ КЛЮЧИ (12.08, утечка в боевом журнале). Порог «>=16 символов» пропускал ключи
# короче: dRPC отдаёт `lb.drpc.live/base/<ключ>` длиной 9, и строка «429 ... for url: ...»
# уехала в журнал ЦЕЛИКОМ. Регексп `_SECRET_SEG_RE` (>=20) её тоже не берёт — то есть оба
# рубежа фильтра были слепы к короткому ключу, а фильтр числился «закрытым навсегда».
# Длину снижаем до 8, но чтобы не резать полезный текст, требуем ПРИЗНАК СЛУЧАЙНОСТИ: цифру
# или заглавную букву. Служебные сегменты наших же URL — base, mainnet, ogrpc, network, rpc,
# v2 — их не имеют и в реестр не попадают.
_KEYISH_RE = re.compile(r"^(?=.*[0-9A-Z])[A-Za-z0-9_\-]{8,}$")


def note_secret(value: str):
    """Запомнить литерал секрета (сегмент пути / значение query нашего RPC-URL).

    Источник доверенный: сюда приходят части НАШИХ настроенных URL, поэтому «похоже на ключ»
    достаточно — ложное срабатывание испортит лишь наш же лог, а пропуск отдаёт ключ наружу.
    """
    v = (value or "").strip()
    if len(v) >= 16 or _KEYISH_RE.match(v):
        with _SECRETS_LOCK:
            _SECRETS.add(v)


def note_secrets_from_url(url: str):
    """Разобрать наши URL и запомнить всё, что похоже на ключ: сегменты пути и значения query.

    Принимает и СПИСОК URL в одной строке. Это не удобство, а починка (12.08): переменные вида
    BG_RPC_URL / COMBAT_FAILOVER_URLS / PENDING_RPC_URLS несут несколько адресов через запятую,
    а прежний разбор брал строку как ОДИН URL — `_AUTHORITY_RE` цеплялся за первый хост, и
    ключи остальных адресов в реестр не попадали. Из-за этого ключ второго узла ушёл в боевой
    журнал строкой предиктора, хотя переменная была «зарегистрирована».
    """
    try:
        for one in re.split(r"[,;\s]+", str(url) or ""):
            m = _AUTHORITY_RE.match(one.strip())
            if not m:
                continue
            for part in re.split(r"[/?&#]", m.group(2) or ""):
                note_secret((part.split("=", 1)[1] if "=" in part else part).strip())
    except Exception:
        pass


def redact_clip(x, n: int) -> str:
    """Текст исключения для журнала: СНАЧАЛА редакция, ПОТОМ обрезка. -> строка длиной <= n.

    Порядок здесь — вся суть (12.08). Вызовы вида `str(e)[:80]` режут строку ДО того, как её
    увидит фильтр журнала, и ключ приезжает в лог ОБРУБКОМ: боевой случай — 44-символьный ключ
    dRPC, от которого в строке осталось ровно 9 символов («...for url: https://host/base/» = 71
    символ, плюс 9 = ровно 80). Фильтр ищет литерал целиком и обрубок сматчить не может, а
    эвристика по длине сегмента тем более. Утечка при этом частичная (9 символов из 44 не
    восстанавливают ключ), но канал реальный: другой лимит обрезки — другая доля ключа.
    """
    try:
        return redact(x)[:int(n)]
    except Exception:
        return "<строка не поддалась редакции>"


def note_secrets_from_env(environ=None) -> int:
    """Зарегистрировать ключи ВСЕХ провайдерских URL из окружения. -> сколько URL разобрано.

    Зачем, если есть регистрация в конструкторе BaseRpc. Рядом с ней написано «ЕДИНСТВЕННАЯ
    дверь, через которую URL входит в бота» — это НЕПРОВЕРЕННЫЙ ИНВАРИАНТ, и 12.08 он дал
    утечку: строка предиктора «основной узел отказал (429 ... for url: ...)» унесла в журнал
    ключ целиком. Опираться на «все контуры строят BaseRpc» нельзя — контуров много, а цена
    ошибки односторонняя: лишняя регистрация портит только наш лог, пропуск отдаёт ключ.

    Берём из окружения ВСЁ, что похоже на http(s)-URL, независимо от имени переменной: реестр
    имён протух бы при первом новом провайдере.
    """
    import os as _os
    env = _os.environ if environ is None else environ
    n = 0
    for v in list(env.values()):
        try:
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                note_secrets_from_url(v)
                n += 1
        except Exception:
            continue
    return n


def redact(s) -> str:
    """Убрать ключи провайдеров из произвольной строки перед печатью.

    Два рубежа: (1) известные литералы — ловят ключ ДАЖЕ там, где URL распался (DNS/SSL/таймаут
    транспорта); (2) регексп по URL — ловит то, чего мы ещё не видели."""
    try:
        out = str(s)
        if _SECRETS:
            with _SECRETS_LOCK:
                _known = tuple(_SECRETS)
            for k in _known:
                if k in out:
                    out = out.replace(k, "<ключ скрыт>")
        return _URL_RE.sub(lambda m: _redact_one_url(m.group(0)), out)
    except Exception:
        return "<строка не поддалась редакции>"


class RedactingFilter:
    """Фильтр логгера: последний рубеж, а не замена аккуратности на местах.

    Ставится на корневой хендлер, поэтому закрывает и те строки, которых сегодня ещё нет.
    Правит record.msg/args, а не готовую строку: хендлер форматирует запись сам, и подмена
    результата format() до него бы не дошла.
    """

    def filter(self, record) -> bool:
        try:
            # Гейт «строка содержит ://» был ДЫРОЙ: ошибки транспорта (DNS, SSL, таймаут)
            # несут ключ без схемы. Редактируем безусловно — цена одна str.replace на литерал.
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (redact(v) if isinstance(v, str) else v)
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args)
            if record.exc_info:
                # traceback хендлер рендерит отдельно от msg и кэширует в exc_text; рендерим
                # сами и отдаём уже отредактированным, иначе log.exception() пронесёт URL мимо.
                if not record.exc_text:
                    import traceback
                    record.exc_text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redact(record.exc_text)
        except Exception:
            pass
        return True


# ---- учёт CU (25.07: Alchemy №2 сжёг 7.1M/30M за ~2 суток => ~8 дней до нуля; пик 558 CU/s
# против лимита 500). Считаем ВЫЗОВЫ ПО МЕТОДАМ на каждом URL, чтобы резать по факту, а не на
# ощупь. Веса — прайс Alchemy (CU/вызов); неизвестный метод = 20 (средняя). Только счётчики,
# сетевого поведения не меняем. Снимок: rpc_usage_take() (в hot stats раз в окно).
CU_COST = {"eth_call": 26, "eth_getBalance": 19, "eth_blockNumber": 10, "eth_chainId": 0,
           "eth_getBlockByNumber": 16, "eth_getBlockByHash": 21, "eth_getTransactionReceipt": 15,
           "eth_getTransactionByHash": 17, "eth_getTransactionCount": 26, "eth_estimateGas": 87,
           "eth_getLogs": 75, "eth_sendRawTransaction": 250, "eth_feeHistory": 10,
           "eth_maxPriorityFeePerGas": 10, "eth_getCode": 19, "eth_getStorageAt": 17,
           "eth_gasPrice": 19, "eth_syncing": 10, "net_version": 0, "web3_clientVersion": 0}
_USAGE: dict = {}
_USAGE_LOCK = threading.Lock()

# ---- скользящий ФАКТ расхода (07.08). Тревогу по квоте вешали на проекцию ОДНОГО окна, а она
# спайкует: max расходится с фактическим расходом суток в 6.7-40 раз, тогда как СРЕДНЕЕ проекций
# совпадает с фактом на 0.1-1.5%. Копим измеренные CU и измеренное время, проекцию считаем с
# накопленного — так порог означает «мы столько ТРАТИМ», а не «в одном окне был всплеск».
_CU_ROLL: dict = {}
_CU_ROLL_LOCK = threading.Lock()
CU_ROLL_WINDOW_SEC = 3600.0


def cu_rolling_add(host: str, cu: float, win_sec: float):
    """Добавить замер и вернуть (проекция M CU/мес по накопленному, покрытые секунды).

    Покрытые секунды возвращаются наружу не для красоты: по ним вызывающий решает, накоплено ли
    достаточно, чтобы порогу вообще можно было верить. Бут-спайк резолва отсекается требованием
    span, а НЕ временем аптайма — сторож, который слепнет от рестарта, сторожит рестарты, а не
    квоту (07.08: 21 рестарт за сутки => шестичасовая защёлка не сработала ни разу именно в те
    сутки, когда расход был максимальным за неделю).
    """
    now = time.monotonic()
    with _CU_ROLL_LOCK:
        q = _CU_ROLL.setdefault(host, [])
        q.append((now, float(cu), float(win_sec)))
        cut = now - CU_ROLL_WINDOW_SEC
        while q and q[0][0] < cut:
            q.pop(0)
        total_cu = sum(x[1] for x in q)
        span = sum(x[2] for x in q)
    if span <= 0:
        return (0.0, 0.0)
    return (total_cu / span * 86400 * 30 / 1e6, span)


def _caller_tag() -> str:
    """Первый кадр ВНЕ транспортных модулей — кто заказал вызов. Только под CU_ATTRIBUTION=1."""
    import sys as _sys
    f = _sys._getframe(2)
    skip = ("/chain/rpc.py", "/chain/multicall.py", "/web3/", "/eth_", "/requests/")
    for _ in range(14):
        if f is None:
            break
        name = f.f_code.co_filename
        if not any(s in name for s in skip):
            return "%s:%s" % (name.rsplit("/", 1)[-1], f.f_code.co_name)
        f = f.f_back
    return "?"


def rpc_usage_note(method: str, url: str = "", n: int = 1):
    """n>1 — ОДИН HTTP-пост, но НЕСКОЛЬКО тарифицируемых подзапросов.

    У Alchemy батч JSON-RPC стоит по подзапросам: 64 квитанции в одном посте = 64 x 15 CU.
    Пока счётчик умел только «+1 за вызов», батчевые пути было невозможно завести под учёт
    честно, и они уходили мимо (12.08: eth_getTransactionReceipt в панели Alchemy = 59%
    суточного расхода, а учёт бота показывал 7 — расход коллектора не считался ВООБЩЕ,
    и по этому нулю был вынесен вердикт «трафик не наш»).
    """
    import os as _os
    try:
        n = int(n)
    except Exception:
        n = 1
    if n <= 0:
        return
    host = (url.split("//")[-1].split("/")[0] or "?") if url else "?"
    # 05.08 страйп bg-ключей: два ключа на одном хосте (lb.drpc.live) сливались в одну CU-строку —
    # различаем стабильным crc-тегом пути (сам ключ в лог НЕ попадает). Alchemy-алерт ниже матчится
    # подстрокой ("alchemy" in host) — тег его не ломает.
    if url:
        _path = url.split("//")[-1].split("/", 1)
        if len(_path) > 1 and _path[1].strip("/"):
            import zlib as _zlib
            host = "%s#%04x" % (host, _zlib.crc32(_path[1].encode()) & 0xffff)
    key = method
    if (_os.environ.get("CU_ATTRIBUTION") or "").lower() in ("1", "true", "yes", "on"):
        try:
            key = "%s@%s" % (method, _caller_tag())
        except Exception:
            pass
    with _USAGE_LOCK:
        d = _USAGE.setdefault(host, {})
        d[key] = d.get(key, 0) + n


def rpc_usage_take():
    """{host: {method: n}} за окно + сброс. Пустой словарь = вызовов не было."""
    global _USAGE
    with _USAGE_LOCK:
        out, _USAGE = _USAGE, {}
        return out


def rpc_usage_cu(snapshot: dict) -> int:
    return sum(CU_COST.get(str(m).split("@", 1)[0], 20) * n
               for d in snapshot.values() for m, n in d.items())


def wrap_provider_counting(w3, url: str):
    """Оборачивает provider.make_request счётчиком. НЕ middleware: в web3 v7 middleware —
    классы Web3Middleware (v6-фабрика молча отваливалась, 25.07 счётчик не завёлся), а
    provider.make_request стабилен в обеих версиях. Идемпотентно."""
    prov = getattr(w3, "provider", None)
    if prov is None or getattr(prov, "_cu_counted", False):
        return
    _orig = prov.make_request

    def _counted(method, params):
        try:
            rpc_usage_note(str(method), url)
        except Exception:
            pass
        return _orig(method, params)

    prov.make_request = _counted
    prov._cu_counted = True


_STRIP_LOGGED = False


def strip_validation_middleware(w3):
    """Снять web3-middleware `validation`. Он дергает eth_chainId ПЕРЕД КАЖДЫМ запросом (замер
    25.07 счётчиком CU: eth_chainId×1150 на eth_call×575 = ровно 2 лишних round-trip на чтение).
    A/B на Alchemy: медиана aggregate3 **71мс -> 26мс (-45мс, -63%)** — оверхед сидел в НАШЕМ
    процессе, не в сети. Безопасно: chainId мы ставим явно (`t.setdefault("chainId", ...)` в
    send_tx и в залпе), подписываем локально и шлём raw. Откат: WEB3_STRIP_VALIDATION=0."""
    global _STRIP_LOGGED
    import os
    if (os.environ.get("WEB3_STRIP_VALIDATION", "1") or "1").lower() in ("0", "false", "no", "off"):
        return
    try:
        w3.middleware_onion.remove("validation")
        if not _STRIP_LOGGED:
            _STRIP_LOGGED = True
            import logging
            logging.getLogger(__name__).info(
                "web3: validation-middleware снят — минус 2 eth_chainId RTT на каждое чтение (-45мс)")
    except Exception:
        pass                                      # уже снят / другой набор middleware


class BaseRpc:
    def __init__(self, rpc_url: str, chain_id: int, request_timeout: float | None = None):
        """request_timeout: пер-запросный timeout для HTTPProvider. None = web3-дефолт (прежнее
        поведение, все существующие вызыватели не задеты). Явный таймаут ставят пути, где
        зависший эндпоинт хуже потерянного вызова (fallback-резолвер фидов: mainnet.base.org
        чёрнодырил 23.07, bg-резолв висел 7+ мин на одном вызове)."""
        self.rpc_url = rpc_url
        # ЕДИНСТВЕННАЯ дверь, через которую URL входит в бота (все контуры строят BaseRpc):
        # здесь же запоминаем ключ как ЛИТЕРАЛ, чтобы фильтр журнала резал его и там, где URL
        # распался — DNS/SSL/таймаут транспорта печатают строку без схемы (находка ревьюера).
        note_secrets_from_url(rpc_url)
        self.chain_id = chain_id
        self.request_timeout = request_timeout
        self._w3 = None

    def _web3(self):
        if self._w3 is None:
            from web3 import Web3                 # lazy: keeps `import main` clean
            kw = {"timeout": self.request_timeout} if self.request_timeout else None
            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs=kw) if kw
                            else Web3.HTTPProvider(self.rpc_url))
            try:                                  # учёт CU: обёртка провайдера, поведение не меняет
                wrap_provider_counting(self._w3, self.rpc_url)
            except Exception:
                pass
            strip_validation_middleware(self._w3)
        return self._w3

    def contract(self, address: str, abi: list):
        """Bound contract for read calls. web3 stays lazy (loaded on first use)."""
        w3 = self._web3()
        return w3.eth.contract(address=w3.to_checksum_address(address), abi=abi)

    def to_bytes32(self, hexstr: str):
        from web3 import Web3
        return Web3.to_bytes(hexstr=hexstr)

    def eth_call(self, tx: dict, block="latest", state_override=None):
        """Low-level eth_call. state_override (optional) injects {addr: {code, stateDiff}} —
        used to simulate the not-yet-deployed liquidator before a real deploy."""
        w3 = self._web3()
        if state_override is not None:
            return w3.eth.call(tx, block, state_override)
        return w3.eth.call(tx, block)

    def block_number(self) -> int:
        return self._web3().eth.block_number


_LOGS_FB: list = []   # ленивые Web3-фолбэки get_logs_fb (создаются на первом отказе primary)


def get_logs_fb(primary_w3, params):
    """eth_getLogs с фолбэком по LOGS_FALLBACK_RPCS (csv, дефолт mainnet.base.org): Alchemy-ключ
    №2 (23.07) отдаёт 400 на ЛЮБОЙ getLogs — диагностические чтения (canary DELTA, submit-xray)
    не должны слепнуть из-за одного провайдера. Порядок: primary -> фолбэки; последняя ошибка
    пробрасывается (вызыватели уже обёрнуты в try)."""
    try:
        return primary_w3.eth.get_logs(params)
    except Exception as e:
        last = e
    global _LOGS_FB
    if not _LOGS_FB:
        import os
        from web3 import Web3
        urls = [u.strip() for u in
                os.environ.get("LOGS_FALLBACK_RPCS", "https://mainnet.base.org").split(",") if u.strip()]
        _LOGS_FB = [Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 10})) for u in urls]
    for w3 in _LOGS_FB:
        try:
            return w3.eth.get_logs(params)
        except Exception as e:
            last = e
    raise last

    # TODO(phase1): subscribe to new blocks / oracle updates (wss),
    #   batch eth_call for health factors, read base fee for tip accounting.
