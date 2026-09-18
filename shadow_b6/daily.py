"""Суточная строка отчёта тени (запускается на Charlotte). Печатает markdown-строку и блок
деталей; расход RPC — из heartbeat (свой счётчик) + сырой подсчёт по логам.

python daily.py [YYYY-MM-DD]   (по умолчанию — предыдущие сутки UTC)
"""
from __future__ import annotations
import json, os, sys, time, datetime as dt, collections
HERE = os.path.dirname(os.path.abspath(__file__))
D = f"{HERE}/data"


M0 = json.load(open(f"{HERE}/markets_tracked.json"))


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


def independent_scan(day, urls=None):
    """§4.3 протокола: НЕЗАВИСИМЫЙ скан всех Liquidate за сутки. Без него суточная строка
    считается только по блокам, которые тень сама и выбрала, — то есть тест, способный
    вернуть только зелёное. 22 чанка по 2000 блоков на бесплатном эндпоинте."""
    sys.path.insert(0, HERE)
    import rpcx
    urls = urls or ["https://base-rpc.publicnode.com", "https://base.drpc.org", "https://mainnet.base.org"]
    T_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
    T_SVR = "0x8d530b9ddc4b318d28fdd4c3a21fcfecece54c1a72a824f262985b99afef009b"
    t0 = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp()
    t1 = t0 + 86400
    head = int(rpcx.rpc("eth_blockNumber", [], urls), 16)
    hb = rpcx.rpc("eth_getBlockByNumber", [hex(head), False], urls)
    ts_head = int(hb["timestamp"], 16)
    b0 = max(1, head - int((ts_head - t0) / 2))
    b1 = min(head, head - int((ts_head - t1) / 2))
    M = json.load(open(f"{HERE}/markets_tracked.json"))
    ids = {k.lower() for k in M}
    ev, svr_blocks = [], set()
    b = b0
    while b <= b1:
        e = min(b + 1999, b1)
        try:
            for l in rpcx.getlogs(b, e, [T_LIQ], "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb", urls, timeout=40):
                mid = l["topics"][1].lower()
                if mid in ids:
                    ev.append({"b": int(l["blockNumber"], 16), "mid": mid, "tx": l["transactionHash"].lower(),
                               "user": ("0x" + l["topics"][3][-40:]).lower(),
                               "repaid": int(l["data"][2:66], 16)})
            for l in rpcx.getlogs(b, e, [T_SVR], None, urls, timeout=40):
                svr_blocks.add((int(l["blockNumber"], 16), l["transactionHash"].lower()))
            b = e + 1
        except Exception as ex:
            print("   скан", b, "ошибка", str(ex)[:60], flush=True); time.sleep(1.0)
    svr_tx = {t for _b, t in svr_blocks}
    for x in ev:
        x["cls"] = "A_UNLOCK" if x["tx"] in svr_tx else "C_OUTSIDE_or_B"
    return b0, b1, ev


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
    def usd(o):
        m = M0.get((o.get("mid") or "").lower()) or {}
        return (o.get("repaid") or 0) / 10 ** int(m.get("loan_dec") or 6) if m.get("loan_sym") in ("USDC", "USDbC", "USDS", "EURC", "USR") else 0.0
    print(f"| {day} | {len(outside)} | ${sum(usd(o) for o in outside):,.0f} | "
          f"{len(ours)} | {len(m4)} | {len(m5)} | {len(rounds)} | {shots} | "
          f"{(lat[len(lat)//2] if lat else 0):.0f}мс | {bnd_ok}/{len(bnd)} | {rpc_tot} |")
    print()
    print(f"детали {day}: раундов {len(rounds)} (с названными позициями {len(named_rounds)}), "
          f"выстрелов-в-тени {shots}; событий на целевых блоках {len(ev)}, вне аукциона {len(outside)}")
    if outside:
        for o in sorted(outside, key=lambda x: -(x['repaid'] or 0))[:8]:
            ou = o.get("ours") or {}
            print(f"   blk {o['b_rel']} {o['pair']:>13} ${usd(o):>9.0f} победитель tx {o['tx'][:12]} "
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
    # независимый контроль полноты
    try:
        b0, b1, allev = independent_scan(day)
        ours = {(o["b_rel"], o["mid"], o["user"]) for o in outs}
        named_keys = set()
        for r in rounds:
            for sh in (r.get("shots") or []):
                named_keys.add((r["b_rel"], sh["mid"], sh["user"].lower()))
        outside = [x for x in allev if x["cls"] != "A_UNLOCK"]
        # КЛАССИФИКАЦИЯ ПРОПУСКОВ: событие вне класса B+6 (не было раунда цены в [b-8, b])
        # нельзя считать промахом детектора — иначе строка «0 из 50» пугает и ничего не значит.
        T_AU2 = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
        agg_of_mid = {}
        for mid, m in M0.items():
            legs = sorted((m.get("legs") or {}).items())
            if legs:
                agg_of_mid[mid.lower()] = legs[0][1]["agg"]
        for x in outside:
            a = agg_of_mid.get(x["mid"])
            x["b6"] = None
            if not a:
                continue
            try:
                lg = rpcx.getlogs(x["b"] - 8, x["b"], [T_AU2], a,
                                  ["https://base-rpc.publicnode.com", "https://base.drpc.org"], timeout=20)
                blocks = sorted({int(l["blockNumber"], 16) for l in lg})
                x["b6"] = (x["b"] - max(blocks)) if blocks else None
            except Exception:
                pass
        seen = [x for x in outside if (x["b"], x["mid"], x["user"]) in ours]
        namd = [x for x in outside if (x["b"], x["mid"], x["user"]) in named_keys]
        print(f"   НЕЗАВИСИМЫЙ СКАН блоков {b0}..{b1}: событий на отслеживаемых рынках {len(allev)}, "
              f"вне tx Atlas {len(outside)}, из них тень видела {len(seen)}, назвала {len(namd)}")
        miss = [x for x in outside if (x["b"], x["mid"], x["user"]) not in named_keys]
        m_b6 = [x for x in miss if x.get("b6") == 6]
        m_other = [x for x in miss if x.get("b6") != 6]
        print(f"     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — {len(m_b6)}; "
              f"вне класса (смещение {sorted({x.get('b6') for x in m_other})}) — {len(m_other)}")
        for x in sorted(m_b6, key=lambda z: -(z["repaid"] or 0))[:6]:
            print(f"     ПРОПУЩЕНО B+6 blk {x['b']} {(M0.get(x['mid']) or {}).get('pair','?'):>13} "
                  f"${usd(x):>9.0f} user {x['user'][:12]}")
    except Exception as ex:
        print("   НЕЗАВИСИМЫЙ СКАН: ошибка", str(ex)[:120])
    # КОНТРОЛЬ ГОНКИ ЧТЕНИЯ РАУНДА — каждые сутки, а не один раз при установке:
    # дефект был молчаливым (журнал говорил «раунд принят»), значит проверка обязана жить.
    try:
        sys.path.insert(0, HERE)
        import rpcx
        T_AU = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
        FREE = ["https://base-rpc.publicnode.com", "https://base.drpc.org"]
        sample = [r for r in rounds if r.get("a_new") is not None][-25:]
        okc = badc = nolog = 0
        for r in sample:
            lg = rpcx.getlogs(r["b"], r["b"], [T_AU], r["agg"], FREE, timeout=20)
            if not lg:
                nolog += 1; continue
            v = int(lg[-1]["topics"][1], 16)
            v = v - (1 << 256) if v >= (1 << 255) else v
            okc += (v == r["a_new"]); badc += (v != r["a_new"])
        print(f"   КОНТРОЛЬ ГОНКИ РАУНДА (выборка {len(sample)}): совпало {okc}, РАСХОЖДЕНИЙ {badc}, "
              f"транзитов без лога {nolog}" + ("  <-- ПРИБОР СЛОМАН, замер за сутки недействителен" if badc else ""))
    except Exception as ex:
        print("   КОНТРОЛЬ ГОНКИ: ошибка", str(ex)[:100])
    rel = load("release_check.jsonl")
    if rel:
        ch = sum(1 for r in rel if r.get("changed"))
        offs = collections.Counter(r.get("offset") for r in rel)
        print(f"   §3.2 блок отпирания: проверок {len(rel)}, цена сменилась на B_rel в {ch}; смещения {dict(offs)}")
    at = load("atlas_unlocked.jsonl")
    if at:
        print(f"   отпираний Atlas до B+6 (выстрел отменяется): {len(at)}")
    cnt = hb.get("cnt") or {}
    print(f"   сторож: up {hb.get('up_sec',0)/3600:.1f} ч, fb_msg {cnt.get('fb_msg')}, обрывов WS {cnt.get('fb_err')}, "
          f"позиций с долгом {cnt.get('pos_live')}, ошибок решения {cnt.get('decide_err',0)}")


if __name__ == "__main__":
    main()
