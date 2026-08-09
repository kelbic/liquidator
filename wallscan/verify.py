import json, urllib.request, time, os
from collections import Counter

RPC = "https://mainnet.base.org"
OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"
HDRS = {"Content-Type":"application/json","User-Agent":"Mozilla/5.0 (X11; Linux x86_64) wallscan/1.0"}

def post(payload, tries=8):
    body = json.dumps(payload).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(RPC, data=body, headers=HDRS)
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            if isinstance(res, dict) and res.get("error"): raise RuntimeError(res["error"])
            return res
        except Exception:
            time.sleep(1+i)
    raise RuntimeError("exhausted")

heads = json.load(open(os.path.join(OUT,"heads.json")))
def load(blk):
    return {int(k): v for k,v in json.load(open(os.path.join(OUT,f"txs_{blk}.json"))).items()}

# 1) integrity
for blk in [49666335, 49666338, 49666340]:
    txs = load(blk); n = heads[str(blk)]["tx_count"]
    assert len(txs)==n, (blk,"len",len(txs),n)
    bad = [i for i in range(n) if txs[i] is None or txs[i]["idx"]!=i]
    hashes = [txs[i]["hash"] for i in range(n)]
    assert not bad, (blk,"idx-mismatch",bad[:5])
    assert len(set(hashes))==n, (blk,"dup hashes", n-len(set(hashes)))
    print(f"{blk}: integrity OK ({n} tx, idx aligned, hashes unique)")

# 2) classify FULLCALL via one receipt each
hot = load(49666338)
samples = {}
for a,lbl in [("0x1f494779091cf1a4137bc92f40760d4aeec6be95","G"),
              ("0xb4458d5997f0dea90b681bec7aedc1546f4214b2","H"),
              ("0xbc2e95134c56f64d33143a85929ef14712b57334","I"),
              ("0xcbecc9d0a461712c61a15ccb89a711e9f59bdef5","J"),
              ("0x48630e5780d5a45a555578cbbc921797ce4f6e7a","K"),
              ("0x703ca95a09b839576d7641a2b8410adeca145ef7","L")]:
    t = next(t for t in hot.values() if t["to"]==a)
    samples[lbl]=(a,t["hash"],t["idx"])
batch = [{"jsonrpc":"2.0","id":lbl,"method":"eth_getTransactionReceipt","params":[h]} for lbl,(a,h,i) in samples.items()]
res = post(batch)
for item in res:
    lbl=item["id"]; r=item["result"]
    addrs = Counter(l["address"][:10] for l in r["logs"])
    t0 = Counter(l["topics"][0][:10] for l in r["logs"] if l["topics"])
    print(f"{lbl} {samples[lbl][0][:10]}.. idx={samples[lbl][2]}: status={r['status']} gas={int(r['gasUsed'],16)} logs={len(r['logs'])} addrs={dict(addrs.most_common(4))} topics={dict(t0.most_common(4))}")
