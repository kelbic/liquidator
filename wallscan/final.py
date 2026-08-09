import json, os
from collections import Counter

OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"
heads = json.load(open(os.path.join(OUT,"heads.json")))
def load(blk):
    return {int(k): v for k,v in json.load(open(os.path.join(OUT,f"txs_{blk}.json"))).items()}

WALL = {  # probe-style: tiny input, repeated to
 "0xf1737022bc1f5b5735ee9f29e0ec1212d73f1f3a":"A beef-board (winner x2)",
 "0x4be560e576c49922c7c87830bb99fb7ced4327ae":"B beef-board",
 "0xb1610106e2f9d38dc27023f6c32d3d39a8e30ff8":"E 0B-poke",
 "0x575cce1bb9c46d5cbe303aabbed4f096776a10dd":"F 0B-poke",
 "0xfabb2d4ac6acf663f5648fa1866a91e0b94e3d49":"C 0B-board (winner)",
 "0x455494036a163fe9c99f3cbb6298fac6f5f46b93":"D 36B-probe (winner)",
}
FULLCALL = {
 "0x1f494779091cf1a4137bc92f40760d4aeec6be95":"G 466B",
 "0xb4458d5997f0dea90b681bec7aedc1546f4214b2":"H 940B",
 "0xbc2e95134c56f64d33143a85929ef14712b57334":"I 1002B",
 "0xcbecc9d0a461712c61a15ccb89a711e9f59bdef5":"J 281B",
 "0x48630e5780d5a45a555578cbbc921797ce4f6e7a":"K 1380B",
 "0x703ca95a09b839576d7641a2b8410adeca145ef7":"L 996B",
}
for blk in [49666335, 49666338, 49666340]:
    txs = load(blk); n = heads[str(blk)]["tx_count"]
    print(f"--- {blk} n={n} ---")
    wsum=0
    for grp,name in [(WALL,"WALL"),(FULLCALL,"FULL")]:
        for a,lbl in grp.items():
            sub=[t for t in txs.values() if t['to']==a]
            uf=len(set(t['from'] for t in sub))
            if grp is WALL: wsum+=len(sub)
            print(f"  {name} {lbl:24s} {a[:10]}..: {len(sub):5d} tx  uniq_from={uf}")
    fsum=sum(1 for t in txs.values() if t['to'] in FULLCALL)
    print(f"  probe-wall total: {wsum} ({wsum/2:.0f}/s, {100*wsum/n:.1f}% of block); full-call total: {fsum} ({fsum/2:.0f}/s, {100*fsum/n:.1f}%)")
    print(f"  combined: {wsum+fsum} = {100*(wsum+fsum)/n:.1f}% of block")
