import json, urllib.request, time, sys, os

RPC = "https://mainnet.base.org"
OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"

def rpc(method, params, retries=6):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) wallscan/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            if "error" in res:
                raise RuntimeError(res["error"])
            return res["result"]
        except Exception as e:
            print(f"  retry {i}: {e}", flush=True)
            time.sleep(2*(i+1))
    raise RuntimeError("exhausted retries")

for n in range(49666335, 49666343):
    path = os.path.join(OUT, f"block_{n}.json")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        print(f"{n}: cached", flush=True)
        continue
    b = rpc("eth_getBlockByNumber", [hex(n), True])
    if b is None:
        print(f"{n}: NULL", flush=True); continue
    with open(path, "w") as f:
        json.dump(b, f)
    print(f"{n}: ts={int(b['timestamp'],16)} txs={len(b['transactions'])} size={os.path.getsize(path)}", flush=True)
    time.sleep(0.5)
