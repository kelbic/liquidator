"""НЕЗАВИСИМАЯ ПРОВЕРКА Б5: потолок переспроса в боевой ключ."""
import pathlib, sys, types
ROOT = pathlib.Path("/home/claude-agent/work/liquidator/.claude/worktrees/wf_73589e1f-2ff-2")
sys.path.insert(0, str(ROOT))
import chain.spec_fire_live as sfl
import chain.execute as ex

H = ["0x%064x" % i for i in range(64)]
DED, COMBAT = "https://ded/k", "https://combat/k"
calls = []

def make_batch(answer_n):
    def batch_fn(url, hs, chunk=0):
        calls.append((url, len(hs)))
        if url == DED:
            got = {h: None for h in hs[:answer_n]}
            return ex.receipt_batch(got, set(hs[:answer_n]))
        return ex.receipt_batch({h: {"status": 1} for h in hs}, set(hs))
    return batch_fn

lines = []
def on_reask(n, why): lines.append((n, why))
def on_fb(why): lines.append(("FB", why))

for answered in (1, 32, 56, 63):
    calls.clear(); lines.clear()
    out = sfl.collect_receipts(H, DED, COMBAT, make_batch(answered),
                               on_fallback=on_fb, on_reask=on_reask, dedicated_chunk=0)
    combat = [c for c in calls if c[0] == COMBAT]
    print("узел ответил про %2d/64 -> обращений к БОЕВОМУ %d (id %s), строк %d: %s"
          % (answered, len(combat), [c[1] for c in combat], len(lines),
             [l[0] for l in lines]))
    for l in lines: print("      ", l[1][:110])
