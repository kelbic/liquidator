"""Вселенная для ЖИВОГО прогона: API Morpho (все позиции отслеживаемых рынков, страницами
по borrowShares) ∪ вселенная реплея (снимок бота + логи Borrow). HF из API НЕ используется
ни в одном решении — только адреса; здоровье считается на цепи ([[liq-target-classes-canon]]).

Позитивный контроль: 11 заёмщиков, которых реплей потерял как «пропуск вселенной», должны
найтись здесь — иначе живая вселенная так же слепа, и это надо знать ДО запуска.
"""
from __future__ import annotations
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.morpho.org/graphql"
Q = """
query Positions($keys: [String!], $first: Int!, $skip: Int!) {
  marketPositions(first: $first, skip: $skip, orderBy: BorrowShares, orderDirection: Desc,
                  where: { marketUniqueKey_in: $keys }) {
    items { user { address } market { marketId } state { borrowShares collateral } }
  }
}
"""


def api(keys, first, skip, timeout=40):
    body = json.dumps({"query": Q, "variables": {"keys": keys, "first": first, "skip": skip}}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if d.get("errors"):
        raise RuntimeError(str(d["errors"])[:200])
    return (((d.get("data") or {}).get("marketPositions") or {}).get("items")) or []


def main():
    M = json.load(open(f"{HERE}/markets_tracked.json"))
    keys = sorted(M)
    out = {k.lower(): set() for k in keys}
    got = 0
    for i in range(0, len(keys), 8):                 # ключи пачками, чтобы не упереться в лимит запроса
        part = keys[i:i + 8]
        skip = 0
        while True:
            try:
                items = api(part, 1000, skip)
            except Exception as ex:
                print("  API ошибка:", str(ex)[:140], flush=True); time.sleep(2.0); continue
            if not items:
                break
            for it in items:
                mid = ((it.get("market") or {}).get("marketId") or "").lower()
                u = ((it.get("user") or {}).get("address") or "").lower()
                st = it.get("state") or {}
                if mid in out and u and (int(st.get("borrowShares") or 0) > 0 or int(st.get("collateral") or 0) > 0):
                    out[mid].add(u); got += 1
            skip += len(items)
            if len(items) < 1000:
                break
        print(f"  пачка {i//8+1}: всего записей {got}", flush=True)
    api_tot = sum(len(v) for v in out.values())
    prev = {}
    p = f"{HERE}/data/universe_replay.json"
    if os.path.exists(p):
        prev = json.load(open(p))
    for mid, v in prev.items():
        out.setdefault(mid.lower(), set()).update(x.lower() for x in v)
    res = {k: sorted(v) for k, v in out.items() if v}
    json.dump(res, open(f"{HERE}/data/universe_live.json", "w"))
    tot = sum(len(v) for v in res.values())
    print(f"API дал {api_tot}; объединение с реплеем: {tot} позиций на {len(res)} рынках")
    for mid, v in sorted(res.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"   {M[mid]['pair']:>18} {len(v):>6} (API {len(out[mid]) - len([x for x in prev.get(mid,[])]) if mid in prev else len(out[mid]):>6})")
    # позитивный контроль: промахи вселенной реплея
    MISS = ["0x35bd7a9d7e", "0x98d5654335", "0x3a7b499d80", "0x3535b7d0a1", "0x1e85f593fa",
            "0x7ade53f282", "0x2e2376be2f", "0x6975407ab1", "0x1e905ed62d", "0x0ea863b096", "0x9410374ef4"]
    alladdr = {a for v in res.values() for a in v}
    found = sum(1 for m in MISS if any(a.startswith(m) for a in alladdr))
    print(f"ПОЗИТИВНЫЙ КОНТРОЛЬ: из 11 потерянных реплеем заёмщиков живая вселенная содержит {found}")


if __name__ == "__main__":
    main()
