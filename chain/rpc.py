"""Base RPC access. web3 is imported lazily so the module stays import-clean for
the runtime-import check (see docs/WORKFLOW.md). Phase 1 reads only."""
from __future__ import annotations

import re
import threading

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


def redact(s) -> str:
    """Убрать ключи провайдеров из произвольной строки перед печатью."""
    try:
        return _URL_RE.sub(lambda m: _redact_one_url(m.group(0)), str(s))
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
            if isinstance(record.msg, str) and "://" in record.msg:
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (redact(v) if isinstance(v, str) and "://" in v else v)
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) and "://" in a else a
                        for a in record.args)
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


def rpc_usage_note(method: str, url: str = ""):
    import os as _os
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
        d[key] = d.get(key, 0) + 1


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
