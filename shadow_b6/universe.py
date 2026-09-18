"""Вселенная заёмщиков отслеживаемых рынков — НЕ ЗАВИСЯЩАЯ ОТ ИСХОДА.

Два источника, оба слепы к тому, кого ликвидировали:
  1) снимок позиций боевого бота (`liquidator.db`, 14 699 пар на 31.08) — бесплатно;
  2) логи `Borrow` / `SupplyCollateral` Morpho по id отслеживаемых рынков за заданный
     диапазон блоков (чанки 2000 на mainnet.base.org — единственный публичный источник
     широких логов, RECHECK §0).

Промах детектора и промах ВСЕЛЕННОЙ — разные строки отчёта: первый чинится кодом, второй
означает, что живой тени нужен более длинный бутстрап.
"""
from __future__ import annotations
import json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpcx

HERE = os.path.dirname(os.path.abspath(__file__))
PUB = ["https://mainnet.base.org"]
T_BORROW = None
T_SUPCOLL = None


def topics():
    global T_BORROW, T_SUPCOLL
    if T_BORROW is None:
        T_BORROW = "0x" + rpcx.keccak("Borrow(bytes32,address,address,address,uint256,uint256)").hex()
        T_SUPCOLL = "0x" + rpcx.keccak("SupplyCollateral(bytes32,address,address,uint256)").hex()
    return T_BORROW, T_SUPCOLL


def from_db(db_path: str, ids):
    out = {i: set() for i in ids}
    if not os.path.exists(db_path):
        print(f"  DB {db_path}: нет файла", flush=True)
        return out
    c = sqlite3.connect(db_path)
    n = 0
    for mid, bo in c.execute("select market_id, borrower from positions"):
        m = (mid or "").lower()
        if m in out:
            out[m].add((bo or "").lower()); n += 1
    print(f"  DB: {n} пар на отслеживаемых рынках из {c.execute('select count(*) from positions').fetchone()[0]}", flush=True)
    return out


def from_logs(frm: int, to: int, ids, ck_path: str, out: dict, chunk: int = 2000):
    tb, ts_ = topics()
    ids_t = [i for i in ids]
    done = 0
    ck = json.load(open(ck_path)) if os.path.exists(ck_path) else {"next": frm}
    b = max(frm, int(ck.get("next", frm)))
    t0 = time.time()
    while b <= to:
        e = min(b + chunk - 1, to)
        try:
            logs = rpcx.getlogs(b, e, [[tb, ts_], ids_t], rpcx.MORPHO, PUB, timeout=40)
        except Exception as ex:
            print(f"  чанк {b}-{e} ошибка: {str(ex)[:90]}", flush=True)
            time.sleep(1.0)
            continue
        for l in logs:
            t = l["topics"]
            mid = t[1].lower()
            if mid not in out:
                continue
            who = ("0x" + t[2][-40:]).lower() if t[0].lower() == tb else ("0x" + t[3][-40:]).lower()
            out[mid].add(who)
        b = e + 1
        done += 1
        if done % 25 == 0:
            json.dump({"next": b}, open(ck_path, "w"))
            tot = sum(len(v) for v in out.values())
            print(f"  чанк {done}: блок {b}, заёмщиков {tot}, {time.time()-t0:.0f}с", flush=True)
    json.dump({"next": b}, open(ck_path, "w"))
    return out


def main():
    frm, to = int(sys.argv[1]), int(sys.argv[2])
    db = sys.argv[3] if len(sys.argv) > 3 else "/root/liquidator/liquidator.db"
    tag = sys.argv[4] if len(sys.argv) > 4 else "replay"
    M = json.load(open(f"{HERE}/markets_tracked.json"))
    ids = [i.lower() for i in M]
    print(f"вселенная: {len(ids)} рынков, блоки {frm}..{to} ({(to-frm)//2000+1} чанков)", flush=True)
    out = from_db(db, ids)
    out = from_logs(frm, to, ids, f"{HERE}/data/uni_{tag}.ckpt", out)
    res = {k: sorted(v) for k, v in out.items() if v}
    json.dump(res, open(f"{HERE}/data/universe_{tag}.json", "w"))
    tot = sum(len(v) for v in res.values())
    print(f"ИТОГО вселенная: {tot} позиций на {len(res)} рынках", flush=True)
    for mid, v in sorted(res.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"   {M[mid]['pair']:>18} {len(v):>6}")


if __name__ == "__main__":
    main()
