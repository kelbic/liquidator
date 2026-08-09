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
        except Exception as e:
            time.sleep(1+i)
    raise RuntimeError("exhausted")

def load(blk):
    return {int(k): v for k,v in json.load(open(os.path.join(OUT,f"txs_{blk}.json"))).items()}

WINNERS = ["0xf1737022bc1f5b5735ee9f29e0ec1212d73f1f3a",
           "0xfabb2d4ac6acf663f5648fa1866a91e0b94e3d49",
           "0x455494036a163fe9c99f3cbb6298fac6f5f46b93"]

print("=== winner-contract counts per block ===")
for blk in [49666335, 49666338, 49666340]:
    txs = load(blk)
    for w in WINNERS:
        sub = [t for t in txs.values() if t['to']==w]
        print(f"{blk} {w[:10]}..: {len(sub)} tx, uniq_from={len(set(t['from'] for t in sub))}")

print("\n=== from-overlap between beef-contracts (hot block) ===")
hot = load(49666338)
f1 = set(t['from'] for t in hot.values() if t['to']=='0xf1737022bc1f5b5735ee9f29e0ec1212d73f1f3a')
f2 = set(t['from'] for t in hot.values() if t['to']=='0x4be560e576c49922c7c87830bb99fb7ced4327ae')
f3 = set(t['from'] for t in hot.values() if t['to']=='0xfabb2d4ac6acf663f5648fa1866a91e0b94e3d49')
f4 = set(t['from'] for t in hot.values() if t['to']=='0x455494036a163fe9c99f3cbb6298fac6f5f46b93')
f5 = set(t['from'] for t in hot.values() if t['to']=='0xb1610106e2f9d38dc27023f6c32d3d39a8e30ff8')
f6 = set(t['from'] for t in hot.values() if t['to']=='0x575cce1bb9c46d5cbe303aabbed4f096776a10dd')
print(f"f1737(beef) n={len(f1)}, 4be56(beef) n={len(f2)}, overlap={len(f1&f2)}")
print(f"fabb n={len(f3)}, overlap with f1737={len(f3&f1)}, with 4be56={len(f3&f2)}")
print(f"45549 n={len(f4)}, b1610 n={len(f5)}, 575cc n={len(f6)}")
print(f"b1610&575cc={len(f5&f6)}, b1610&fabb={len(f5&f3)}, 575cc&fabb={len(f6&f3)}")

print("\n=== receipts: claimed winners + controls + transit ===")
targets = {2170:"transit? fwd", 2175:"probe pre", 2176:"winner-claim", 2179:"winner-claim",
           2180:"control beef", 2181:"winner-claim", 2187:"winner-claim", 2188:"control 0B"}
batch=[]
for i,(idx,lbl) in enumerate(targets.items()):
    batch.append({"jsonrpc":"2.0","id":idx,"method":"eth_getTransactionReceipt","params":[hot[idx]["hash"]]})
res = post(batch)
for item in sorted(res, key=lambda x: x["id"]):
    r = item["result"]; idx=item["id"]
    ntop = len(r["logs"])
    topics0 = Counter(l["topics"][0][:10] for l in r["logs"] if l["topics"])
    print(f"idx {idx} ({targets[idx]}): status={r['status']} gasUsed={int(r['gasUsed'],16)} logs={ntop} topics0={dict(topics0.most_common(5))}")
    if targets[idx].startswith("winner") or idx==2170:
        addrs = Counter(l["address"] for l in r["logs"])
        print(f"    log addrs: {dict(addrs.most_common(6))}")
