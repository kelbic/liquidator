"""Слияние вселенных (глубокий скан 120 суток ∪ реплей) в universe_live.json.
Позитивный контроль: 11 заёмщиков, потерянных реплеем, обязаны найтись — это прямая
проверка, что живой прогон не унаследовал ту же слепоту."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
D = f"{HERE}/data"
M = json.load(open(f"{HERE}/markets_tracked.json"))
acc = {}
for name in ("universe_replay.json", "universe_deep.json"):
    p = f"{D}/{name}"
    if not os.path.exists(p):
        print(f"  {name}: НЕТ"); continue
    d = json.load(open(p))
    for mid, v in d.items():
        acc.setdefault(mid.lower(), set()).update(x.lower() for x in v)
    print(f"  {name}: {sum(len(v) for v in d.values())} записей, {len(d)} рынков")
res = {k: sorted(v) for k, v in acc.items() if v}
json.dump(res, open(f"{D}/universe_live.json", "w"))
tot = sum(len(v) for v in res.values())
print(f"ИТОГО живая вселенная: {tot} позиций на {len(res)} рынках")
for mid, v in sorted(res.items(), key=lambda kv: -len(kv[1]))[:8]:
    print(f"   {M[mid]['pair']:>18} {len(v):>6}")
MISS = ["0x35bd7a9d7e", "0x98d5654335", "0x3a7b499d80", "0x3535b7d0a1", "0x1e85f593fa",
        "0x7ade53f282", "0x2e2376be2f", "0x6975407ab1", "0x1e905ed62d", "0x0ea863b096", "0x9410374ef4"]
alladdr = {a for v in res.values() for a in v}
found = [m for m in MISS if any(a.startswith(m) for a in alladdr)]
print(f"ПОЗИТИВНЫЙ КОНТРОЛЬ: из 11 промахов вселенной реплея живая вселенная содержит {len(found)}")
if len(found) < len(MISS):
    print("   НЕ НАЙДЕНЫ:", [m for m in MISS if m not in found])
