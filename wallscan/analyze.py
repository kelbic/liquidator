import json, os
from collections import Counter, defaultdict

OUT = "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/wallscan"
heads = json.load(open(os.path.join(OUT,"heads.json")))

def load(blk):
    d = json.load(open(os.path.join(OUT, f"txs_{blk}.json")))
    return {int(k): v for k,v in d.items()}

def short(a): return a[:10]+".."+a[-4:] if a else "None"

WINNER_TO = {"0xf1737022bc1f5b5735ee9f29e0ec1212d73f1f3a",
             "0xfabb2d4ac6acf663f5648fa1866a91e0b94e3d49",
             "0x455494036a163fe9c99f3cbb6298fac6f5f46b93"}

hot = load(49666338)
print("=== KEY INDICES in 49666338 ===")
for i in [2170,2171,2172,2173,2174,2175,2176,2177,2178,2179,2180,2181,2182,2183,2184,2185,2186,2187,2188]:
    t = hot[i]
    print(f"idx {i}: to={t['to']} from={short(t['from'])} len={t['input_len']} sel={t['sel']} hash={t['hash'][:14]}")

print("\n=== OUR PROBES check (idx 13,633,1262,2439,2867,3396,3961) ===")
for i in [13,633,1262,2439,2867,3396,3961]:
    t = hot[i]
    print(f"idx {i}: to={t['to']} from={short(t['from'])} len={t['input_len']} sel={t['sel']}")

print("\n=== NEIGHBORHOOD 2150..2260 ===")
neigh = [hot[i] for i in range(2150,2261)]
to_cnt = Counter(t['to'] for t in neigh)
repeated = {a for a,c in to_cnt.items() if c >= 3}
probes = [t for t in neigh if t['input_len'] < 200 or t['to'] in repeated]
print(f"total in window: {len(neigh)}, probe-classified: {len(probes)}")
print(f"uniq from among probes: {len(set(t['from'] for t in probes))}")
print("repeated to (>=3):")
for a,c in to_cnt.most_common(12):
    if c >= 3:
        ufrom = len(set(t['from'] for t in neigh if t['to']==a))
        lens = Counter(t['input_len'] for t in neigh if t['to']==a)
        print(f"  {a}: {c} tx, uniq_from={ufrom}, input_lens={dict(lens.most_common(4))}")
small = [t for t in neigh if t['input_len'] < 200]
print(f"small-input (<200B) in window: {len(small)}, uniq from: {len(set(t['from'] for t in small))}")
print("small-input to breakdown:", dict(Counter(t['to'] for t in small).most_common(8)))

def block_profile(blk, label):
    txs = load(blk)
    n = heads[str(blk)]["tx_count"]
    print(f"\n=== BLOCK {blk} ({label}): {n} tx, ts={heads[str(blk)]['timestamp']} ===")
    to_cnt = Counter(t['to'] for t in txs.values() if t['to'])
    print("top-12 to by tx count:")
    rows = []
    for a,c in to_cnt.most_common(12):
        sub = [t for t in txs.values() if t['to']==a]
        ufrom = len(set(t['from'] for t in sub))
        med_len = sorted(t['input_len'] for t in sub)[len(sub)//2]
        sels = Counter(t['sel'] for t in sub).most_common(2)
        rows.append((a,c,ufrom,med_len,sels))
        print(f"  {a}: {c} tx, uniq_from={ufrom}, med_input={med_len}B, top_sel={sels}")
    small = [t for t in txs.values() if t['input_len'] < 200 and t['to'] and t['type']!='0x7e']
    print(f"small-input (<200B) whole block: {len(small)} ({100*len(small)/n:.1f}%), uniq from={len(set(t['from'] for t in small))}")
    return rows

r38 = block_profile(49666338, "HOT")
r35 = block_profile(49666335, "QUIET pre")
r40 = block_profile(49666340, "post-prize")
