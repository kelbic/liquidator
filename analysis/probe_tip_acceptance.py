#!/usr/bin/env python3
"""
ACCEPTANCE #104 — probe tip mirroring vs transit tip.

Independent chain-only instrument. Reads NOTHING from the bot (no logs, no
counters, no index). Public Base RPC only.

Metric: share of our probes carrying a tip EXACTLY equal (in wei) to the tip of
the transit tx in the SAME block. Denominator = probes living in blocks that
contain BOTH a probe and a transit.

Probe      := tx with to == CONTRACT and input == "0x01"
Transit    := tx that emitted a Chainlink AnswerUpdated log in that block
tip(1559)  := min(maxPriorityFeePerGas, maxFeePerGas - baseFeePerGas)
tip(legacy):= gasPrice - baseFeePerGas

Both windows go through the SAME code path (full scan of every block in range),
so BEFORE and AFTER are directly comparable.

Sub-commands:
  scan      <lo> <hi>          full-block scan, caches a compact extract
  legs      <lo> <hi>          list distinct probe senders in a scanned range
  firstseen <addr> [lo] [hi]   binary search on nonce: when did this leg start
  report    <lo> <hi> [...]    metric + distributions + positive control
"""
import json, os, sys, time, itertools, threading, collections
import datetime as dt
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CONTRACT = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
PROBE_INPUT = "0x01"
ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
CACHE = "/home/claude-agent/.claude/jobs/c5adabde/tmp/blkcache"

# Bot restarts (UTC) reported by the coordinator; mirror ring is empty after each.
RESTARTS = ["2026-08-15 07:11:42", "2026-08-15 07:28:09", "2026-08-15 07:45:56"]
WARMUP_SEC = 300

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
ENDPOINTS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.gateway.tenderly.co",
    "https://base-mainnet.public.blastapi.io",
    "https://1rpc.io/base",
    "https://base.meowrpc.com",
]
_ctr = itertools.count()
_lock = threading.Lock()
STATS = collections.Counter()


def utc(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=dt.timezone.utc).timestamp())


def hhmm(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%m-%d %H:%M:%S")


def rpc(method, params, tries=7):
    last = None
    for i in range(tries):
        with _lock:
            url = ENDPOINTS[next(_ctr) % len(ENDPOINTS)]
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=45) as r:
                j = json.loads(r.read())
            if "error" in j or j.get("result") is None:
                raise RuntimeError(j.get("error", "null result"))
            STATS["rpc_ok"] += 1
            return j["result"]
        except Exception as e:
            last = e
            STATS["rpc_retry"] += 1
            time.sleep(0.25 * (i + 1))
    STATS["rpc_fail"] += 1
    raise last


def head():
    return int(rpc("eth_getBlockByNumber", ["latest", False])["number"], 16)


def block_at_time(ts_target):
    lo, hi = 1, head()
    while lo < hi:
        mid = (lo + hi) // 2
        if int(rpc("eth_getBlockByNumber", [hex(mid), False])["timestamp"], 16) < ts_target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def tip_of(tx, base_fee):
    """Effective priority fee in wei, or None for non-priced (deposit) txs."""
    if int(tx.get("type", "0x0"), 16) == 0x7E:      # OP-stack system deposit
        return None
    if tx.get("maxPriorityFeePerGas") is not None:
        return min(int(tx["maxPriorityFeePerGas"], 16),
                   int(tx["maxFeePerGas"], 16) - base_fee)
    return int(tx["gasPrice"], 16) - base_fee


def transit_hashes(lo, hi, chunk=500):
    out = collections.defaultdict(set)
    b = lo
    while b <= hi:
        e = min(b + chunk - 1, hi)
        for lg in rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e),
                                       "topics": [ANSWER_UPDATED]}]):
            out[int(lg["blockNumber"], 16)].add(lg["transactionHash"].lower())
        b = e + 1
        time.sleep(0.05)
    return out


# ---------------------------------------------------------------- scan + cache

def _cache_path(lo, hi):
    os.makedirs(CACHE, exist_ok=True)
    return f"{CACHE}/{lo}_{hi}.json"


def scan(lo, hi, workers=6, force=False):
    """Full-block scan -> compact per-block extract. Cached on disk."""
    cp = _cache_path(lo, hi)
    if os.path.exists(cp) and not force:
        with open(cp) as f:
            return json.load(f)

    t0 = time.time()
    th = transit_hashes(lo, hi)
    sys.stderr.write(f"[scan {lo}..{hi}] transit blocks: {len(th)}\n")
    blocks = {}

    def grab(n):
        blocks[n] = rpc("eth_getBlockByNumber", [hex(n), True])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, _ in enumerate(ex.map(grab, range(lo, hi + 1))):
            if i and i % 400 == 0:
                sys.stderr.write(f"  {i}/{hi-lo+1} ({time.time()-t0:.0f}s)\n")

    out = {"lo": lo, "hi": hi, "blocks": []}
    for n in range(lo, hi + 1):
        blk = blocks[n]
        base = int(blk["baseFeePerGas"], 16)
        probes, transits, to_contract = [], [], collections.Counter()
        for tx in blk["transactions"]:
            to = (tx.get("to") or "").lower()
            inp = (tx.get("input", "") or "").lower()
            if to == CONTRACT:
                to_contract[inp[:10]] += 1
                if inp == PROBE_INPUT:
                    probes.append({"h": tx["hash"], "i": int(tx["transactionIndex"], 16),
                                   "tip": tip_of(tx, base), "from": tx["from"].lower()})
            if tx["hash"].lower() in th.get(n, ()):
                transits.append({"h": tx["hash"], "i": int(tx["transactionIndex"], 16),
                                 "tip": tip_of(tx, base), "to": to})
        out["blocks"].append({"n": n, "ts": int(blk["timestamp"], 16), "base": base,
                              "p": probes, "t": transits, "tc": dict(to_contract)})
    out["secs"] = time.time() - t0
    with open(cp, "w") as f:
        json.dump(out, f)
    sys.stderr.write(f"[scan {lo}..{hi}] done in {out['secs']:.0f}s -> {cp}\n")
    return out


# ------------------------------------------------------------------- reporting

def warm_flags(ts, restarts, warm=WARMUP_SEC):
    for r in restarts:
        if r <= ts < r + warm:
            return True
    return False


def analyse(sc, restarts_ts, label):
    R = {"label": label, "lo": sc["lo"], "hi": sc["hi"], "nblocks": sc["hi"] - sc["lo"] + 1}
    R["t_first"] = sc["blocks"][0]["ts"]
    R["t_last"] = sc["blocks"][-1]["ts"]
    buckets = {"ALL": [], "WARM": [], "STEADY": []}
    transit_blocks = probe_blocks = both_blocks = 0
    probes_no_transit = probes_total = 0
    to_contract = collections.Counter()
    transit_tips_all = collections.Counter()

    for b in sc["blocks"]:
        for k, v in b["tc"].items():
            to_contract[k] += v
        if b["t"]:
            transit_blocks += 1
            for t in b["t"]:
                if t["tip"] is not None:
                    transit_tips_all[t["tip"]] += 1
        if b["p"]:
            probe_blocks += 1
            probes_total += len(b["p"])
        if b["p"] and b["t"]:
            both_blocks += 1
            ttips = {t["tip"] for t in b["t"] if t["tip"] is not None}
            primary = min(b["t"], key=lambda t: t["i"])["tip"]
            warm = warm_flags(b["ts"], restarts_ts)
            for p in b["p"]:
                rec = {"blk": b["n"], "ts": b["ts"], "tip": p["tip"], "i": p["i"],
                       "from": p["from"], "hit": p["tip"] in ttips,
                       "hit1": p["tip"] == primary, "warm": warm,
                       "ttips": sorted(ttips), "h": p["h"],
                       "tidx": [t["i"] for t in b["t"]], "base": b["base"]}
                buckets["ALL"].append(rec)
                buckets["WARM" if warm else "STEADY"].append(rec)
        elif b["p"]:
            probes_no_transit += len(b["p"])

    R.update(transit_blocks=transit_blocks, probe_blocks=probe_blocks,
             both_blocks=both_blocks, probes_total=probes_total,
             probes_no_transit=probes_no_transit,
             to_contract=dict(to_contract), transit_tips=dict(transit_tips_all),
             buckets=buckets)
    return R


def bucket_line(name, recs):
    n = len(recs)
    if not n:
        return f"    {name:<7} n=0"
    h = sum(1 for r in recs if r["hit"])
    blks = len({r["blk"] for r in recs})
    tips = collections.Counter(r["tip"] for r in recs)
    dist = "  ".join(f"{t:,}x{c}" for t, c in sorted(tips.items()))
    return (f"    {name:<7} n={n:<4} blocks={blks:<3} match={h:<4} "
            f"({100*h/n:5.1f}%)   tips: {dist}")


def fmt(R):
    L = [f"=== {R['label']}  blocks {R['lo']}..{R['hi']} (n={R['nblocks']}), "
         f"{hhmm(R['t_first'])} .. {hhmm(R['t_last'])} UTC ==="]
    a = L.append
    a(f"  blocks with transit              : {R['transit_blocks']}")
    a(f"  blocks with probe                : {R['probe_blocks']}")
    a(f"  blocks with BOTH (denominator)   : {R['both_blocks']}")
    a(f"  probes total in range            : {R['probes_total']}")
    a(f"  probes in blocks WITHOUT transit : {R['probes_no_transit']}  (excluded from ratio)")
    a(f"  probes in denominator            : {len(R['buckets']['ALL'])}")
    a("  MATCH share by bucket (warm = first 5 min after a restart, mirror ring empty):")
    for k in ("ALL", "STEADY", "WARM"):
        a(bucket_line(k, R["buckets"][k]))
    tt = R["transit_tips"]
    a(f"  transit tips seen in range (n={sum(tt.values())}, distinct={len(tt)}):")
    for t, c in sorted(tt.items(), key=lambda kv: -kv[1])[:8]:
        a(f"      {int(t):>14,} x{c}")
    a("  DETECTOR CONTROL - txs to our contract by selector:")
    if not R["to_contract"]:
        a("      (none - contract untouched)")
    for sel, c in sorted(R["to_contract"].items(), key=lambda kv: -kv[1])[:8]:
        a(f"      {sel:<12} x{c}" + ("   <-- PROBE form" if sel == PROBE_INPUT else ""))
    return "\n".join(L)


def positive_control(R, limit=4):
    L = ["", f"--- POSITIVE CONTROL / {R['label']} ---"]
    byblk = collections.defaultdict(list)
    for r in R["buckets"]["ALL"]:
        byblk[r["blk"]].append(r)
    ranked = sorted(byblk.items(), key=lambda kv: -sum(1 for r in kv[1] if r["hit"]))
    for blk, recs in ranked[:limit]:
        r0 = recs[0]
        a = f"  block {blk} {hhmm(r0['ts'])}Z base={r0['base']:,} warm={r0['warm']}"
        L.append(a)
        L.append(f"     TRANSIT idx={r0['tidx']} tip={[f'{t:,}' for t in r0['ttips']]}")
        tips = collections.Counter(r["tip"] for r in recs)
        L.append(f"     PROBES  n={len(recs)} idx={min(r['i'] for r in recs)}..{max(r['i'] for r in recs)} "
                 f"tips={ {f'{k:,}':v for k,v in sorted(tips.items())} }")
        for r in recs[:3]:
            L.append(f"       e.g. idx={r['i']:>4} tip={r['tip']:>12,} "
                     f"{'MATCH' if r['hit'] else '  -  '} {r['h']}")
    return "\n".join(L)


# ------------------------------------------------------------------------- cli

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "scan":
        scan(int(sys.argv[2]), int(sys.argv[3]))

    elif cmd == "legs":
        sc = scan(int(sys.argv[2]), int(sys.argv[3]))
        c = collections.Counter()
        tiers = collections.defaultdict(collections.Counter)
        for b in sc["blocks"]:
            for p in b["p"]:
                c[p["from"]] += 1
                tiers[p["from"]][p["tip"]] += 1
        print(f"distinct probe senders: {len(c)}")
        for a, n in c.most_common():
            print(f"  {a} probes={n:<5} tips={dict(sorted(tiers[a].items()))}")

    elif cmd == "firstseen":
        # binary search on nonce -> first block where this leg had sent anything
        addr = sys.argv[2]
        lo = int(sys.argv[3]); hi = int(sys.argv[4])
        n_lo = int(rpc("eth_getTransactionCount", [addr, hex(lo)]), 16)
        n_hi = int(rpc("eth_getTransactionCount", [addr, hex(hi)]), 16)
        print(f"{addr}: nonce@{lo}={n_lo}  nonce@{hi}={n_hi}  (sent {n_hi-n_lo} in range)")
        if n_hi == n_lo:
            print("  no activity in range"); return
        a, b = lo, hi
        while a < b:
            m = (a + b) // 2
            if int(rpc("eth_getTransactionCount", [addr, hex(m)]), 16) > n_lo:
                b = m
            else:
                a = m + 1
        ts = int(rpc("eth_getBlockByNumber", [hex(a), False])["timestamp"], 16)
        print(f"  first tx beyond nonce {n_lo} lands at block {a} ({hhmm(ts)}Z)")

    elif cmd == "report":
        lo, hi, label = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
        rts = [utc(s) for s in RESTARTS]
        R = analyse(scan(lo, hi), rts, label)
        print(fmt(R))
        print(positive_control(R))

    else:
        print(__doc__)
    if STATS:
        sys.stderr.write(f"rpc {dict(STATS)}\n")


if __name__ == "__main__":
    main()
