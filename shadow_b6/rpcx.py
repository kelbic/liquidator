"""Транспорт тени: JSON-RPC со счётчиком расхода по эндпоинту, батчи, Multicall3, логи.

ГАРД: список запрещённых методов проверяется на каждом вызове. В тени физически не может
уйти транзакция — ни по забывчивости, ни через чужой вызов ([[tests-never-touch-production-channels]]:
границу держит транспорт, а не дисциплина вызывающего).
"""
from __future__ import annotations
import json, os, time, threading, urllib.request, urllib.error

FORBIDDEN = ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "eth_signTransaction",
             "personal_", "eth_sendBundle", "mev_sendBundle")
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

_LOCK = threading.Lock()
COUNTS: dict = {}          # host -> {"req": n, "calls": n, "err": n}


def _host(u: str) -> str:
    return u.split("//", 1)[-1].split("/", 1)[0]


def stats() -> dict:
    with _LOCK:
        return json.loads(json.dumps(COUNTS))


def _note(u: str, n_calls: int, err: bool = False):
    h = _host(u)
    with _LOCK:
        d = COUNTS.setdefault(h, {"req": 0, "calls": 0, "err": 0})
        d["req"] += 1
        d["calls"] += n_calls
        if err:
            d["err"] += 1


def _post(url: str, payload, timeout: float):
    body = json.dumps(payload).encode()
    # User-Agent обязателен: без него Cloudflare перед mainnet.base.org отдаёт 403
    # (проверено на Charlotte 18.09; в _recon/lib.py тот же заголовок стоит с 29.08)
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rpc(method: str, params, urls, retries: int = 2, timeout: float = 30.0):
    """Один вызов с перебором эндпоинтов. urls: str или список (порядок = приоритет)."""
    for f in FORBIDDEN:
        if method.startswith(f) or method == f:
            raise RuntimeError(f"ГАРД ТЕНИ: метод {method} запрещён (тень ничего не отправляет)")
    if isinstance(urls, str):
        urls = [urls]
    last = None
    for attempt in range(retries):
        for u in urls:
            try:
                out = _post(u, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout)
                _note(u, 1)
                if isinstance(out, dict) and out.get("error"):
                    last = RuntimeError(f"{_host(u)}: {str(out['error'])[:160]}")
                    continue
                return out["result"]
            except Exception as e:
                _note(u, 1, err=True)
                last = e
        time.sleep(0.2 * (attempt + 1))
    raise last if last else RuntimeError("rpc: нет эндпоинтов")


def batch(reqs, urls, retries: int = 2, timeout: float = 60.0):
    """reqs: [(method, params)]. -> список результатов (Exception на месте ошибки)."""
    for m, _ in reqs:
        for f in FORBIDDEN:
            if m.startswith(f):
                raise RuntimeError(f"ГАРД ТЕНИ: метод {m} запрещён")
    if isinstance(urls, str):
        urls = [urls]
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(reqs)]
    last = None
    for attempt in range(retries):
        for u in urls:
            try:
                out = _post(u, payload, timeout)
                _note(u, len(reqs))
                by = {}
                for o in out:
                    by[o["id"]] = RuntimeError(str(o.get("error"))[:160]) if o.get("error") else o.get("result")
                return [by.get(i, RuntimeError("нет ответа")) for i in range(len(reqs))]
            except Exception as e:
                _note(u, len(reqs), err=True)
                last = e
        time.sleep(0.2 * (attempt + 1))
    raise last if last else RuntimeError("batch: нет эндпоинтов")


# ---------- ABI ----------
def keccak(text: str) -> bytes:
    from eth_utils import keccak as _k
    return _k(text=text)


def sel(sig: str) -> bytes:
    return keccak(sig)[:4]


SEL_POSITION = None  # ленивая инициализация, чтобы модуль импортировался без eth_utils


def selectors() -> dict:
    """Селекторы + ПОЗИТИВНЫЙ КОНТРОЛЬ против чисел, записанных в STATE бота
    (position 0x93c52062, market 0x5c60e39a, idToMarketParams 0x2c3c9157, price 0xa035b1fe,
    latestRoundData 0xfeaf968c). Расхождение = ошибка ABI, а не «нет данных»."""
    s = {"position": sel("position(bytes32,address)"), "market": sel("market(bytes32)"),
         "idToMarketParams": sel("idToMarketParams(bytes32)"), "price": sel("price()"),
         "latestRoundData": sel("latestRoundData()"), "aggregate3": sel("aggregate3((address,bool,bytes)[])"),
         "aggregator": sel("aggregator()"), "SCALE_FACTOR": sel("SCALE_FACTOR()")}
    known = {"position": "0x93c52062", "market": "0x5c60e39a", "idToMarketParams": "0x2c3c9157",
             "price": "0xa035b1fe", "latestRoundData": "0xfeaf968c"}
    for k, v in known.items():
        got = "0x" + s[k].hex()
        assert got == v, f"селектор {k}: получено {got}, в STATE {v}"
    return s


def enc_position(mid_hex: str, borrower: str) -> str:
    s = selectors()["position"]
    return "0x" + (s + bytes.fromhex(mid_hex[2:]) + bytes(12) + bytes.fromhex(borrower[2:].lower())).hex()


def enc_bytes32(sig_sel: bytes, mid_hex: str) -> str:
    return "0x" + (sig_sel + bytes.fromhex(mid_hex[2:])).hex()


def enc_aggregate3(calls) -> str:
    """calls: [(target, calldata_hex)] -> calldata aggregate3 (allowFailure=True всем)."""
    from eth_abi import encode
    data = encode(["(address,bool,bytes)[]"],
                  [[(t, True, bytes.fromhex(c[2:])) for t, c in calls]])
    return "0x" + (selectors()["aggregate3"] + data).hex()


def dec_aggregate3(ret_hex: str):
    from eth_abi import decode
    (rows,) = decode(["(bool,bytes)[]"], bytes.fromhex(ret_hex[2:]))
    return rows


def multicall(calls, urls, block="latest", chunk: int = 250, timeout: float = 60.0):
    """-> [(ok, bytes)] в том же порядке. Один eth_call на chunk вызовов."""
    out = []
    for i in range(0, len(calls), chunk):
        part = calls[i:i + chunk]
        data = enc_aggregate3(part)
        blk = block if isinstance(block, str) else hex(block)
        ret = rpc("eth_call", [{"to": MULTICALL3, "data": data}, blk], urls, timeout=timeout)
        out.extend(dec_aggregate3(ret))
    return out


def u256(b: bytes, i: int = 0) -> int:
    return int.from_bytes(b[i * 32:(i + 1) * 32], "big")


def i256(b: bytes, i: int = 0) -> int:
    v = u256(b, i)
    return v - (1 << 256) if v >= (1 << 255) else v


def getlogs(frm: int, to: int, topics, address, urls, timeout: float = 60.0):
    p = {"fromBlock": hex(frm), "toBlock": hex(to)}
    if address:
        p["address"] = address
    if topics:
        p["topics"] = topics
    return rpc("eth_getLogs", [p], urls, timeout=timeout)


def env_urls(env_path: str, names) -> dict:
    """Читает из .env ТОЛЬКО поимённо указанные переменные. Значения не печатаются
    ([[secrets-never-grep-env]]: выбирать поимённо, маску не применять)."""
    want = set(names)
    got = {}
    if not os.path.exists(env_path):
        return got
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in want:
                got[k.strip()] = v.strip().strip('"').strip("'")
    return got
