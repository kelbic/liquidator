"""Диагностика pending-детекта (06.08, «го на пендинг детект»): с Шарлотты меряем
1) отдаёт ли eth_call tag="pending" у провайдеров слоя состояние ФОРМИРУЕМОГО блока
   (pending != latest до запечатывания) или это алиас latest;
2) лаг каждого провайдера до транзита (первое появление нового roundId, pending и latest)
   относительно самого раннего наблюдателя (preconf = flashblocks);
3) реальную латентность запросов (почему цикл 4 Гц живёт на ~0.5 Гц).
READ-ONLY, nice -19, без WALLET_KEY. Ключ dRPC не печатаем (маска host-only).
"""
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, "/root/liquidator")
from eth_utils import keccak
from eth_abi import encode as abi_encode, decode as abi_decode

DUR_SEC = 780
HZ_SLEEP = 1.0          # 1 итерация/с на провайдера, в итерации 2 запроса (pending+latest)
TIMEOUT = 2.0
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
_HDRS = {"Content-Type": "application/json",
         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}

SEL_LRD = keccak(text="latestRoundData()")[:4]
SEL_ID2MP = keccak(text="idToMarketParams(bytes32)")[:4]
SEL_BF1 = keccak(text="BASE_FEED_1()")[:4]
SEL_AGG3 = keccak(text="aggregate3((address,bool,bytes)[])")[:4]

def rpc_call(url, method, params, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=_HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(str(out["error"])[:120])
    return out["result"]

def eth_call(url, to, data_hex, tag):
    return rpc_call(url, "eth_call", [{"to": to, "data": data_hex}, tag])

# --- провайдеры: слой + preconf как референс ---
urls = {"base.org": "https://mainnet.base.org",
        "nodies": "https://base-pokt.nodies.app",
        "preconf": "https://mainnet-preconf.base.org"}
for line in open("/root/liquidator/.env"):
    if line.startswith("PENDING_RPC_URLS="):
        for u in line.split("=", 1)[1].strip().split(","):
            if "drpc" in u:
                urls["drpc"] = u.strip()
print("providers:", sorted(urls))

# --- резолв прокси: ETH явный, cbBTC/cbXRP через idToMarketParams -> BASE_FEED_1 ---
proxies = {"ETH": "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70"}
cov = json.load(open("/root/liquidator/covered_markets.json"))
want = {"cbBTC": "BTC", "cbXRP": "XRP"}
for m in cov:
    lbl = want.get(m.get("collateral_token"))
    if lbl and m.get("loan_token") == "USDC" and lbl not in proxies:
        mid = m["market_id"]
        raw = eth_call(urls["base.org"], MORPHO,
                       "0x" + SEL_ID2MP.hex() + mid[2:], "latest")
        oracle = "0x" + raw[2:][64 * 2:64 * 3][-40:]
        try:
            bf1 = eth_call(urls["base.org"], oracle, "0x" + SEL_BF1.hex(), "latest")
            proxies[lbl] = "0x" + bf1[-40:]
        except Exception as e:
            print("BF1 fail", lbl, e)
print("proxies:", proxies)

FEEDS = sorted(proxies)
calls = [(proxies[f], True, SEL_LRD) for f in FEEDS]
AGG_DATA = "0x" + SEL_AGG3.hex() + abi_encode(
    ["(address,bool,bytes)[]"], [calls]).hex()

def read_all(url, tag):
    raw = eth_call(url, MULTICALL3, AGG_DATA, tag)
    res = abi_decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))[0]
    out = {}
    for f, (ok, data) in zip(FEEDS, res):
        if ok and len(data) >= 160:
            out[f] = (int.from_bytes(data[0:32], "big"),
                      int.from_bytes(data[96:128], "big"))
    return out

rows = []          # (ts, prov, tag, rtt_ms, {feed: (round, upd)})
errs = {}
lock = threading.Lock()
stop_at = time.time() + DUR_SEC

def worker(name, url):
    while time.time() < stop_at:
        it0 = time.time()
        for tag in ("pending", "latest"):
            t0 = time.time()
            try:
                snap = read_all(url, tag)
                rtt = (time.time() - t0) * 1000
                with lock:
                    rows.append((t0, name, tag, rtt, snap))
            except Exception as e:
                with lock:
                    k = (name, type(e).__name__, str(e)[:40])
                    errs[k] = errs.get(k, 0) + 1
        time.sleep(max(0.05, HZ_SLEEP - (time.time() - it0)))

ts = [threading.Thread(target=worker, args=(n, u), daemon=True)
      for n, u in urls.items()]
[t.start() for t in ts]
[t.join() for t in ts]

# --- анализ ---
print("\n=== errors ===")
for k, v in sorted(errs.items()):
    print(v, k)

print("\n=== rtt ms per provider ===")
for name in urls:
    r = sorted(x[3] for x in rows if x[1] == name)
    if r:
        print("%-8s n=%d p50=%.0f p95=%.0f" % (
            name, len(r), r[len(r) // 2], r[int(len(r) * 0.95)]))

# транзиты: по каждому фиду каждое новое (round, upd); первый ts наблюдения на
# (prov, tag). Базлайн (первое наблюдение вообще) выкидываем.
first_seen = {}    # (feed, round, upd) -> {(prov, tag): ts}
order = {}         # (feed, round, upd) -> min ts
for t0, prov, tag, rtt, snap in sorted(rows):
    for f, cur in snap.items():
        key = (f, cur[0], cur[1])
        d = first_seen.setdefault(key, {})
        if (prov, tag) not in d:
            d[(prov, tag)] = t0
        order[key] = min(order.get(key, 1e18), t0)

print("\n=== transits (skip baseline) ===")
by_feed = {}
for key, t in sorted(order.items(), key=lambda kv: kv[1]):
    by_feed.setdefault(key[0], []).append(key)
for f, keys in by_feed.items():
    for key in keys[1:]:                      # первый = базлайн
        t_min = order[key]
        seen = first_seen[key]
        det = " ".join("%s/%s=%+.2fs" % (p, tag[0], seen[(p, tag)] - t_min)
                       for (p, tag) in sorted(seen))
        missing = [n for n in urls
                   if (n, "pending") not in seen and (n, "latest") not in seen]
        print("%s round=%d upd=%s | %s%s" % (
            f, key[1], time.strftime("%H:%M:%S", time.gmtime(key[2])), det,
            (" | MISS:" + ",".join(missing)) if missing else ""))

# pending vs latest: на одном провайдере в одной итерации — расходятся ли вообще
print("\n=== pending!=latest events per provider (same iteration) ===")
by_it = {}
for t0, prov, tag, rtt, snap in rows:
    by_it.setdefault((prov, round(t0)), {})[tag] = snap
div = {}
for (prov, _), d in by_it.items():
    if "pending" in d and "latest" in d:
        n_cmp = div.setdefault(prov, [0, 0])
        n_cmp[1] += 1
        if any(d["pending"].get(f) != d["latest"].get(f)
               for f in FEEDS if f in d["pending"] and f in d["latest"]):
            n_cmp[0] += 1
print({p: "%d/%d" % (v[0], v[1]) for p, v in div.items()})
