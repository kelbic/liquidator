import json, urllib.request, time, os

RPC = "https://mainnet.base.org"
OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"
HDRS = {"Content-Type":"application/json","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) wallscan/1.0"}
err_seen = {}

def post(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(RPC, data=body, headers=HDRS)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())

def compact(tx):
    inp = tx.get("input") or "0x"
    return {"idx": int(tx["transactionIndex"],16), "hash": tx["hash"],
            "from": tx["from"], "to": tx.get("to"),
            "input_len": (len(inp)-2)//2, "sel": inp[:10] if len(inp)>=10 else inp,
            "gas": int(tx["gas"],16), "nonce": int(tx["nonce"],16), "type": tx.get("type")}

heads = json.load(open(os.path.join(OUT,"heads.json")))
for blk in [49666338, 49666335, 49666340]:
    path = os.path.join(OUT, f"txs_{blk}.json")
    out = {int(k): v for k,v in json.load(open(path)).items()}
    n = heads[str(blk)]["tx_count"]
    for rnd in range(12):
        missing = [i for i in range(n) if out.get(i) is None or "too_large" in (out.get(i) or {})]
        if not missing:
            break
        print(f"{blk} round {rnd}: missing {len(missing)}", flush=True)
        for j in range(0, len(missing), 10):
            chunk = missing[j:j+10]
            batch = [{"jsonrpc":"2.0","id":i,"method":"eth_getTransactionByBlockNumberAndIndex",
                      "params":[hex(blk), hex(i)]} for i in chunk]
            try:
                res = post(batch)
                if isinstance(res, dict): res = [res]
                for item in res:
                    if item.get("result"):
                        out[item["id"]] = compact(item["result"])
                    elif item.get("error"):
                        c = item["error"].get("code")
                        err_seen[c] = err_seen.get(c,0)+1
            except Exception as e:
                time.sleep(1.0)
            time.sleep(0.12)
        with open(path,"w") as f: json.dump(out,f)
        time.sleep(2.0)
    missing = [i for i in range(n) if out.get(i) is None]
    print(f"{blk}: final missing {len(missing)}", flush=True)
print("errors seen:", err_seen, flush=True)
