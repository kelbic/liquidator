import json, urllib.request, time, os

RPC = "https://mainnet.base.org"
OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"
HDRS = {"Content-Type":"application/json","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) wallscan/1.0"}

def post(payload, retries=6):
    body = json.dumps(payload).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(RPC, data=body, headers=HDRS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry {i}: {e}", flush=True)
            time.sleep(1.5*(i+1))
    raise RuntimeError("exhausted retries")

# headers for all 8 blocks
heads = {}
for n in range(49666335, 49666343):
    res = post({"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber","params":[hex(n), False]})
    b = res["result"]
    heads[n] = {"number": n, "timestamp": int(b["timestamp"],16), "tx_count": len(b["transactions"]),
                "gasUsed": int(b["gasUsed"],16)}
    print(n, heads[n], flush=True)
with open(os.path.join(OUT,"heads.json"),"w") as f: json.dump(heads,f)
