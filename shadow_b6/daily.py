"""Суточная строка отчёта тени (запускается на Charlotte). Печатает markdown-строку и блок
деталей; расход RPC — из heartbeat (свой счётчик) + сырой подсчёт по логам.

python daily.py [YYYY-MM-DD]   (по умолчанию — предыдущие сутки UTC)
"""
from __future__ import annotations
import json, os, sys, time, datetime as dt, collections
HERE = os.path.dirname(os.path.abspath(__file__))
D = f"{HERE}/data"


def load(name):
    p = f"{D}/{name}"
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    t0 = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp()
    t1 = t0 + 86400
    rounds = [r for r in load("rounds.jsonl") if t0 <= r.get("t", 0) < t1]
    outs = load("outcomes.jsonl")
    bnd = load("boundaries.jsonl")
    hb = {}
    if os.path.exists(f"{D}/heartbeat.json"):
        hb = json.load(open(f"{D}/heartbeat.json"))
    # исходы: привязка по времени недоступна (в строке нет t) -> по блокам раундов суток
    blks = {r["b_rel"] for r in rounds}
    ev = [o for o in outs if o["b_rel"] in blks] if blks else []
    outside = [o for o in ev if o["cls"] == "C_OUTSIDE"]
    ours = [o for o in outside if o.get("ours")]
    m4 = [o for o in ours if (o["ours"] or {}).get("m4_in_block")]
    m5 = [o for o in ours if (o["ours"] or {}).get("m5_strict")]
    named_rounds = [r for r in rounds if r.get("named")]
    shots = sum(r.get("named") or 0 for r in rounds)
    lat = sorted(r["lat_ms"] for r in rounds if r.get("lat_ms") is not None)
    bnd_ok = sum(1 for b in bnd if b.get("in_fb0"))
    rpc = hb.get("rpc") or {}
    rpc_tot = sum(v.get("req", 0) for v in rpc.values())
    wins = collections.Counter(o["tx"][:10] for o in outside)
    print(f"| {day} | {len(outside)} | ${sum((o['repaid'] or 0)/1e6 for o in outside):,.0f} | "
          f"{len(ours)} | {len(m4)} | {len(m5)} | {len(rounds)} | {shots} | "
          f"{(lat[len(lat)//2] if lat else 0):.0f}мс | {bnd_ok}/{len(bnd)} | {rpc_tot} |")
    print()
    print(f"детали {day}: раундов {len(rounds)} (с названными позициями {len(named_rounds)}), "
          f"выстрелов-в-тени {shots}; событий на целевых блоках {len(ev)}, вне аукциона {len(outside)}")
    if outside:
        for o in sorted(outside, key=lambda x: -(x['repaid'] or 0))[:8]:
            ou = o.get("ours") or {}
            print(f"   blk {o['b_rel']} {o['pair']:>13} ${(o['repaid'] or 0)/1e6:>9.0f} победитель tx {o['tx'][:12]} "
                  f"txi {o['txi']:>4} fb#{o['winner_fb_idx']} tip {o['tip_gwei']:.4f} gwei | "
                  f"наш tip по политике {ou.get('tip_policy_gwei')} M4={ou.get('m4_in_block')} "
                  f"M5={ou.get('m5_strict')} (время {ou.get('m5_time_ok')}, ставка {ou.get('m5_tip_ok')})")
    if bnd:
        gaps = sorted(b["gap_ms"] for b in bnd)
        print(f"   границы блоков: n={len(bnd)}, попадание в fb#0 {bnd_ok/len(bnd)*100:.1f}%, "
              f"зазор между последним fb блока N-1 и fb#0 блока N: p10 {gaps[len(gaps)//10]:.0f} "
              f"p50 {gaps[len(gaps)//2]:.0f} p90 {gaps[int(len(gaps)*0.9)]:.0f} мс")
    if rpc:
        print(f"   RPC по эндпоинтам: " + ", ".join(f"{k}={v.get('req',0)}(err {v.get('err',0)})" for k, v in sorted(rpc.items())))
    cnt = hb.get("cnt") or {}
    print(f"   сторож: up {hb.get('up_sec',0)/3600:.1f} ч, fb_msg {cnt.get('fb_msg')}, обрывов WS {cnt.get('fb_err')}, "
          f"позиций с долгом {cnt.get('pos_live')}, ошибок решения {cnt.get('decide_err',0)}")


if __name__ == "__main__":
    main()
