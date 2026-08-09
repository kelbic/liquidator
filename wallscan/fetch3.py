import json, urllib.request, time, os, sys

RPC = "https://mainnet.base.org"
OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"
HDRS = {"Content-Type":"application/json","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) wallscan/1.0"}

class TooLarge(Exception): pass

def post(payload, retries=5):
    body = json.dumps(payload).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(RPC, data=body, headers=HDRS)
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.loads(r.read())
            if isinstance(res, dict) and res.get("error"):
                if res["error"].get("code") == -32020: raise TooLarge()
                raise RuntimeError(res["error"])
            return res
        except TooLarge:
            raise
        except Exception as e:
            last = e
            time.sleep(1.2*(i+1))
    raise RuntimeError(f"exhausted: {last}")

def compact(tx):
    if tx is None: return None
    inp = tx.get("input") or "0x"
    return {"idx": int(tx["transactionIndex"],16), "hash": tx["hash"],
            "from": tx["from"], "to": tx.get("to"),
            "input_len": (len(inp)-2)//2, "sel": inp[:10] if len(inp)>=10 else inp,
            "gas": int(tx["gas"],16), "nonce": int(tx["nonce"],16), "type": tx.get("type")}

def fetch_range(blk, lo, hi, out):
    """fetch tx indices [lo,hi) via batch; split on too-large"""
    if lo >= hi: return
    batch = [{"jsonrpc":"2.0","id":i,"method":"eth_getTransactionByBlockNumberAndIndex",
              "params":[hex(blk), hex(i)]} for i in range(lo, hi)]
    try:
        res = post(batch)
        for item in res:
            out[item["id"]] = compact(item.get("result"))
    except TooLarge:
        if hi - lo == 1:
            out[lo] = {"idx": lo, "too_large": True}
            return
        mid = (lo+hi)//2
        fetch_range(blk, lo, mid, out)
        fetch_range(blk, mid, hi, out)

heads = json.load(open(os.path.join(OUT,"heads.json")))
for blk in [49666338, 49666335, 49666340]:
    path = os.path.join(OUT, f"txs_{blk}.json")
    n = heads[str(blk)]["tx_count"]
    out = {}
    if os.path.exists(path):
        out = {int(k): v for k,v in json.load(open(path)).items()}
        if len(out) >= n:
            print(f"{blk}: cached {len(out)}", flush=True); continue
    t0 = time.time()
    step = 10
    for lo in range(0, n, step):
        hi = min(lo+step, n)
        if all(i in out and out[i] for i in range(lo,hi)): continue
        fetch_range(blk, lo, hi, out)
        if lo % 1000 == 0:
            print(f"  {blk}: {hi}/{n} t={time.time()-t0:.0f}s", flush=True)
            with open(path,"w") as f: json.dump(out,f)
        time.sleep(0.04)
    with open(path,"w") as f: json.dump(out,f)
    missing = [i for i in range(n) if i not in out or out[i] is None]
    print(f"{blk}: done {len(out)}/{n} missing={len(missing)} t={time.time()-t0:.0f}s", flush=True)
