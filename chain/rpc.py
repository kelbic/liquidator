"""Base RPC access. web3 is imported lazily so the module stays import-clean for
the runtime-import check (see docs/WORKFLOW.md). Phase 1 reads only."""
from __future__ import annotations

import threading

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


def rpc_usage_note(method: str, url: str = ""):
    host = (url.split("//")[-1].split("/")[0] or "?") if url else "?"
    with _USAGE_LOCK:
        d = _USAGE.setdefault(host, {})
        d[method] = d.get(method, 0) + 1


def rpc_usage_take():
    """{host: {method: n}} за окно + сброс. Пустой словарь = вызовов не было."""
    global _USAGE
    with _USAGE_LOCK:
        out, _USAGE = _USAGE, {}
        return out


def rpc_usage_cu(snapshot: dict) -> int:
    return sum(CU_COST.get(m, 20) * n for d in snapshot.values() for m, n in d.items())


def _counting_middleware(url: str):
    """web3 v6/v7-совместимая обёртка провайдера: считает методы, ничего не меняет."""
    def factory(make_request, _w3):
        def middleware(method, params):
            try:
                rpc_usage_note(str(method), url)
            except Exception:
                pass
            return make_request(method, params)
        return middleware
    return factory


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
                self._w3.middleware_onion.add(_counting_middleware(self.rpc_url), name="cu_count")
            except Exception:
                pass                              # версия web3 без onion -> просто без учёта
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
