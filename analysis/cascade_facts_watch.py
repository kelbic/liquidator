"""Вотчер факт-сигнала каскада (Вена, крон 10 мин; калибровка 31.07).

Считает ликвидации НАШИХ рынков за скользящий час: blue-api (один GraphQL-count), при
ошибке — ОДИН фолбэк-замер прямым get_logs через BG_RPC_URL (drpc держит диапазон <=2k
блоков; час = 1800). Оба умерли -> флаг НЕ трогаем и падаем громко (fail-closed: бустер
на Шарлотте сам погаснет по TTL 15 мин; слепой подъём хуже пропуска — API теряет ~8%
событий и таймаутит именно в каскад). count >= CASCADE_FACTS_MIN (3) -> touch флага на
Шарлотте через command-restricted ключ (sink принимает только ids|flag).

Ids наших рынков: с Шарлотты через тот же sink (covered_markets.json живёт ТОЛЬКО там),
кэш сутки — не дёргать ssh каждые 10 мин."""
import json
import os
import subprocess
import sys
import time
import urllib.request

API_URL = "https://api.morpho.org/graphql"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
LIQUIDATE_TOPIC = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
CASCADE_FACTS_MIN = int(os.environ.get("CASCADE_FACTS_MIN", "3"))
CHARLOTTE = os.environ.get("CASCADE_SINK_SSH", "root@185.173.146.134")
SINK_KEY = os.environ.get("CASCADE_SINK_KEY", "/root/.ssh/id_cascade")
IDS_CACHE = os.environ.get("CASCADE_IDS_CACHE", "/root/liquidator/state/covered_ids.cache")
IDS_CACHE_TTL = 86400.0

_Q = """query($ids: [String!], $ts: Int!) {
  marketTransactions(first: 1,
    where: { chainId_in: [8453], type_in: [Liquidation],
             marketUniqueKey_in: $ids, timestamp_gte: $ts }) {
    pageInfo { countTotal }
  }
}"""


def _ssh_sink(op, _run=subprocess.run):
    r = _run(["ssh", "-i", SINK_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
              CHARLOTTE, op], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("sink %s: rc=%d %s" % (op, r.returncode, (r.stderr or "")[:120]))
    return r.stdout


def covered_ids(now=None, _ssh=_ssh_sink):
    now = time.time() if now is None else now
    try:
        if now - os.path.getmtime(IDS_CACHE) <= IDS_CACHE_TTL:
            ids = [ln.strip().lower() for ln in open(IDS_CACHE) if ln.strip()]
            if ids:
                return ids
    except OSError:
        pass
    out = _ssh("ids")
    ids = sorted({ln.strip().lower() for ln in out.splitlines() if ln.strip().startswith("0x")})
    if not ids:
        raise RuntimeError("sink ids: пустой список")
    tmp = IDS_CACHE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(ids))
    os.replace(tmp, IDS_CACHE)
    return ids


def count_api(ids, now=None, _urlopen=urllib.request.urlopen):
    """Ликвидации наших рынков за час по blue-api. Бросает на любой ошибке."""
    now = int(time.time() if now is None else now)
    body = json.dumps({"query": _Q, "variables": {"ids": ids, "ts": now - 3600}}).encode()
    req = urllib.request.Request(API_URL, body, {"Content-Type": "application/json",
                                                 "User-Agent": "Mozilla/5.0 (cascade-facts)"})
    with _urlopen(req, timeout=20) as r:
        out = json.load(r)
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:150])
    return int(out["data"]["marketTransactions"]["pageInfo"]["countTotal"])


def _rpc(url, method, params, _urlopen=urllib.request.urlopen):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                             "User-Agent": "Mozilla/5.0 (cascade-facts)"})
    with _urlopen(req, timeout=25) as r:
        out = json.load(r)
    if "error" in out:
        raise RuntimeError(str(out["error"])[:150])
    return out["result"]


def count_logs(ids, rpc_url, _rpc_fn=_rpc):
    """Фолбэк: один get_logs за последние 1800 блоков (час Base; drpc держит <=2k)."""
    head = int(_rpc_fn(rpc_url, "eth_blockNumber", []), 16)
    logs = _rpc_fn(rpc_url, "eth_getLogs", [{
        "address": MORPHO, "topics": [LIQUIDATE_TOPIC],
        "fromBlock": hex(head - 1800), "toBlock": hex(head)}])
    idset = set(ids)
    return sum(1 for lg in logs
               if len(lg.get("topics") or []) > 1 and str(lg["topics"][1]).lower() in idset)


def main():
    ids = covered_ids()
    src = "api"
    try:
        n = count_api(ids)
    except Exception as e:
        print("[cascade-facts] api fail (%s) -> фолбэк get_logs" % type(e).__name__, flush=True)
        rpc_url = os.environ.get("BG_RPC_URL") or os.environ.get("RPC_URL")
        if not rpc_url:
            print("[cascade-facts] нет RPC для фолбэка — fail-closed, флаг не трогаем")
            return 1
        try:
            n = count_logs(ids, rpc_url)
            src = "logs"
        except Exception as e2:
            print("[cascade-facts] фолбэк тоже умер (%s) — fail-closed, флаг не трогаем"
                  % type(e2).__name__)
            return 1
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if n >= CASCADE_FACTS_MIN:
        _ssh_sink("flag")
        print("%s КАСКАД-ФАКТЫ: %d ликвидаций/час (>= %d, src=%s) — флаг поднят" % (
            stamp, n, CASCADE_FACTS_MIN, src), flush=True)
    else:
        print("%s ok: %d ликвидаций/час (< %d, src=%s) — флаг не трогаем (TTL сам погасит)" % (
            stamp, n, CASCADE_FACTS_MIN, src), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
