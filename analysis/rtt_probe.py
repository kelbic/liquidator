"""P2-11 гео-замер: per-call RTT до эндпоинтов гонки с keep-alive (как web3-session в проде).
Гонять С РАЗНЫХ машин (Aeza vs us-east VM) и сравнивать warm median.
Замер 07-08 с Aeza (justcrimson): sequencer warm=121ms, preconf warm=133ms; TCP connect до
Cloudflare-edge ~20ms => ~100-115ms живёт в хопе CF->origin (US). Ожидание us-east: total 20-40ms,
т.е. −90-100ms НА КАЖДЫЙ вызов горячего пути (read+sim+send => ~−250-300ms сквозняком).
Запуск: python3 analysis/rtt_probe.py   (stdlib, без venv)"""
import http.client, json, statistics, time

HOSTS = ("mainnet-sequencer.base.org", "mainnet-preconf.base.org", "mainnet.base.org")
BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}

for host in HOSTS:
    try:
        c = http.client.HTTPSConnection(host, timeout=10)
        ts = []
        for _ in range(10):
            t0 = time.perf_counter()
            c.request("POST", "/", BODY, HDRS)
            r = c.getresponse(); r.read()
            ts.append((time.perf_counter() - t0) * 1000)
        c.close()
        warm = ts[1:]   # первый запрос несёт TCP+TLS
        print(f"{host}: first={ts[0]:.0f}ms warm_median={statistics.median(warm):.0f}ms "
              f"min={min(warm):.0f} max={max(warm):.0f}")
    except Exception as e:
        print(f"{host}: FAIL {type(e).__name__}: {e}")
