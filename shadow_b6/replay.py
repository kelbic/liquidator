"""ОФФЛАЙН-РЕПЛЕЙ: смог бы детектор назвать вне-аукционные события B+6 до блока отпирания.

Гейт запуска тени (§4 протокола). Требования, зашитые в код:
  * вселенная заёмщиков не зависит от исхода (universe.py: снимок бота 31.08 + логи Borrow
    до 15.09 00:00Z) — иначе это тест, который может вернуть только правильный ответ;
  * состояние читается на блоке B_rel-1, то есть ДО того, как цена стала видна Morpho;
  * новая цена считается моделью price_new = price_old × answer_new / answer_old и
    СВЕРЯЕТСЯ с фактическим oracle.price() на B_rel (позитивный контроль модели цены);
  * промахи делятся на «пропуск вселенной» и «пропуск математики»;
  * контрольный набор раундов без ликвидаций даёт число ложных выстрелов.

Запуск (Charlotte): python replay.py detect   — основной прогон recall
                    python replay.py control N — контрольный набор из N раундов
"""
from __future__ import annotations
import json, os, sys, time, random, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpcx, core

HERE = os.path.dirname(os.path.abspath(__file__))
RCHK = os.environ.get("RCHK", "/home/claude-agent/work/liquidator/_recheck0917")
SHD = os.environ.get("SHD", "/home/claude-agent/work/liquidator/_shadow")
ARCH = ["https://base.drpc.org", "https://base.gateway.tenderly.co", "https://base-pokt.nodies.app"]
PUB = ["https://mainnet.base.org"]
T_AU = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
T_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
S = None


def sels():
    global S
    if S is None:
        S = rpcx.selectors()
    return S


def read_market_ctx(mid: str, m: dict, block: int):
    """market() + oracle.price() + latestRoundData каждой ноги — ОДНИМ multicall на блоке."""
    s = sels()
    calls = [(rpcx.MORPHO, rpcx.enc_bytes32(s["market"], mid)),
             (m["oracle"], "0x" + s["price"].hex())]
    legs = sorted(m["legs"].items())
    for _name, leg in legs:
        calls.append((leg["feed"], "0x" + s["latestRoundData"].hex()))
    rows = rpcx.multicall(calls, ARCH, block=block)
    ok0, d0 = rows[0]
    ok1, d1 = rows[1]
    if not ok0 or not ok1:
        raise RuntimeError(f"market/price не прочитались на {block}")
    tsa, tss, tba, tbs, last_update, fee = (rpcx.u256(d0, i) for i in range(6))
    price = rpcx.u256(d1)
    answers = {}
    for (name, leg), (ok, d) in zip(legs, rows[2:]):
        answers[name] = rpcx.i256(d, 1) if ok and len(d) >= 64 else None
    return {"tba": tba, "tbs": tbs, "last_update": last_update, "price": price, "answers": answers}


def read_positions(mid: str, borrowers, block: int):
    s = sels()
    calls = [(rpcx.MORPHO, rpcx.enc_position(mid, b)) for b in borrowers]
    rows = rpcx.multicall(calls, ARCH, block=block, chunk=250)
    out = []
    for b, (ok, d) in zip(borrowers, rows):
        if not ok or len(d) < 96:
            continue
        borrow_shares = rpcx.u256(d, 1)
        collateral = rpcx.u256(d, 2)
        if borrow_shares > 0 and collateral > 0:
            out.append((b, collateral, borrow_shares))
    return out


def round_answer(agg: str, block: int):
    """Новый answer раунда из лога AnswerUpdated на АГРЕГАТОРЕ в блоке B."""
    logs = rpcx.getlogs(block, block, [T_AU], agg, ARCH + PUB)
    if not logs:
        return None
    t = logs[-1]["topics"]
    v = int(t[1], 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def block_ts(block: int):
    h = rpcx.rpc("eth_getBlockByNumber", [hex(block), False], ARCH + PUB)
    return int(h["timestamp"], 16)


def evaluate(mid, m, uni, b_round, b_rel, agg, leg_name, min_gross_usd=0.0, verify=True):
    """Один (рынок, раунд): что назвал бы детектор на цене раунда к блоку b_rel."""
    ctx = read_market_ctx(mid, m, b_rel - 1)
    a_new = round_answer(agg, b_round)
    a_old = ctx["answers"].get(leg_name)
    if a_new is None or not a_old:
        return None
    price_new = ctx["price"] * a_new // a_old
    ts_rel = block_ts(b_rel)
    poss = read_positions(mid, uni, b_rel - 1)
    mk = {"id": mid, "lltv": m["lltv"], "tba": ctx["tba"], "tbs": ctx["tbs"],
          "rate": 0, "last_update": ctx["last_update"], "ts": ts_rel}
    dec = int(m["loan_dec"])
    named = core.detect(mk, poss, price_new, min_gross_units=int(min_gross_usd * 10 ** dec))
    res = {"mid": mid, "pair": m["pair"], "b_round": b_round, "b_rel": b_rel, "agg": agg,
           "price_old": ctx["price"], "price_new": price_new, "a_old": a_old, "a_new": a_new,
           "n_positions": len(poss), "n_universe": len(uni),
           "named": [{"user": r["user"], "repay": r["repay"], "gross": r["gross"],
                      "hf": round(r["hf"], 6), "repay_usd": r["repay"] / 10 ** dec,
                      "gross_usd": r["gross"] / 10 ** dec} for r in named[:40]],
           "n_named": len(named)}
    if verify:
        s = sels()
        rows = rpcx.multicall([(m["oracle"], "0x" + s["price"].hex())], ARCH, block=b_rel)
        ok, d = rows[0]
        if ok:
            actual = rpcx.u256(d)
            res["price_actual_at_rel"] = actual
            res["price_model_err"] = (price_new - actual) / actual if actual else None
    return res


def main_detect():
    M = json.load(open(f"{HERE}/markets_tracked.json"))
    uni = json.load(open(f"{HERE}/data/universe_replay.json"))
    b6 = json.load(open(f"{SHD}/b6_events.json"))
    AU = json.load(open(f"{RCHK}/answerupd.json"))
    agg_of = {}
    for mid, m in M.items():
        for name, leg in m["legs"].items():
            agg_of.setdefault(leg["agg"], []).append((mid, name))
    # группируем события по (блок отпирания, агрегатор)
    tasks = collections.defaultdict(list)
    for e in b6:
        mid = e["id"].lower()
        if mid not in M:
            print(f"  !! рынок события {mid[:12]} не в конфиге — пропуск", flush=True)
            continue
        legs = sorted(M[mid]["legs"].items())
        agg = legs[0][1]["agg"]
        key = f"{agg}:{e['blk']}"
        blocks = [b for b in (AU.get(key) or []) if b <= e["blk"]]
        if not blocks:
            continue
        tasks[(max(blocks), e["blk"], agg)].append(e)
    print(f"задач (раунд, блок отпирания, агрегатор): {len(tasks)}; событий {sum(len(v) for v in tasks.values())}", flush=True)
    out = []
    for (b_round, b_rel, agg), evs in sorted(tasks.items()):
        mids = sorted({e["id"].lower() for e in evs})
        # рынки, которые тень оценивала бы на этом раунде (все на этом агрегаторе с непустой вселенной)
        cand = sorted({mid for mid, _ in agg_of.get(agg, []) if uni.get(mid)})
        for mid in cand:
            leg_name = [n for m2, n in agg_of[agg] if m2 == mid][0]
            t0 = time.time()
            try:
                r = evaluate(mid, M[mid], uni[mid], b_round, b_rel, agg, leg_name)
            except Exception as ex:
                print(f"  {b_rel} {M[mid]['pair']}: ОШИБКА {str(ex)[:110]}", flush=True)
                continue
            if r is None:
                print(f"  {b_rel} {M[mid]['pair']}: нет раунда/answer — пропуск", flush=True)
                continue
            r["actual"] = [{"user": e["user"], "repay": e["repay"], "usd": e["usd"],
                            "caller": e["caller"], "txi": e["txi"]} for e in evs if e["id"].lower() == mid]
            r["sec"] = round(time.time() - t0, 1)
            out.append(r)
            named_u = {x["user"] for x in r["named"]}
            act_u = {a["user"] for a in r["actual"]}
            hit = len(act_u & named_u)
            print(f"  {b_rel} {M[mid]['pair']:>14} раунд {b_round} поз {r['n_positions']:>5}/{r['n_universe']:>5} "
                  f"назвал {r['n_named']:>3} факт {len(act_u):>2} попал {hit:>2} "
                  f"цена-модель err {r.get('price_model_err')} {r['sec']}с", flush=True)
            json.dump(out, open(f"{HERE}/data/replay_detect.json", "w"))
    json.dump(out, open(f"{HERE}/data/replay_detect.json", "w"))
    print("готово", len(out), flush=True)


def main_control(n=60):
    """Контрольный набор: раунды 15.09 на отслеживаемых агрегаторах, где в B+6 ликвидаций НЕ было."""
    M = json.load(open(f"{HERE}/markets_tracked.json"))
    uni = json.load(open(f"{HERE}/data/universe_replay.json"))
    agg_of = {}
    for mid, m in M.items():
        for name, leg in m["legs"].items():
            agg_of.setdefault(leg["agg"], []).append((mid, name))
    aggs = sorted(agg_of)
    b0, b1 = 51320528, 51363728          # сутки 15.09 (00:00Z..24:00Z, ~43200 блоков)
    cpath = f"{HERE}/data/control_rounds.json"
    if os.path.exists(cpath):
        rounds, liqblocks = json.load(open(cpath))
    else:
        rounds = []
        liqblocks = []
        b = b0
        while b <= b1:
            e = min(b + 1999, b1)
            try:
                lg = rpcx.getlogs(b, e, [T_AU], aggs, PUB, timeout=40)
                rounds += [(int(l["blockNumber"], 16), l["address"].lower()) for l in lg]
                lq = rpcx.getlogs(b, e, [T_LIQ], rpcx.MORPHO, PUB, timeout=40)
                liqblocks += [int(l["blockNumber"], 16) for l in lq]
                b = e + 1
                print(f"  скан {b}: раундов {len(rounds)}, ликвидаций {len(liqblocks)}", flush=True)
            except Exception as ex:
                print(f"  скан {b} ошибка {str(ex)[:70]}", flush=True); time.sleep(1.0)
        json.dump([rounds, liqblocks], open(cpath, "w"))
    liqset = set(liqblocks)
    clean = [(b, a) for b, a in rounds if (b + 6) not in liqset and agg_of.get(a)]
    print(f"раундов за 15.09: {len(rounds)}; из них без ликвидации в B+6: {len(clean)}", flush=True)
    random.seed(1509)
    sample = random.sample(clean, min(n, len(clean)))
    out = []
    for b_round, agg in sample:
        for mid, leg_name in agg_of[agg]:
            if not uni.get(mid):
                continue
            try:
                r = evaluate(mid, M[mid], uni[mid], b_round, b_round + 6, agg, leg_name, verify=False)
            except Exception as ex:
                print(f"  {b_round} {M[mid]['pair']}: ОШИБКА {str(ex)[:90]}", flush=True); continue
            if r is None:
                continue
            out.append(r)
            if r["n_named"]:
                tops = sorted((x["gross_usd"] for x in r["named"]), reverse=True)[:3]
                print(f"  ЛОЖНЫЙ ВЫСТРЕЛ {b_round} {M[mid]['pair']:>14} назвал {r['n_named']} бонус$ {tops}", flush=True)
            json.dump(out, open(f"{HERE}/data/replay_control.json", "w"))
    json.dump(out, open(f"{HERE}/data/replay_control.json", "w"))
    named = sum(r["n_named"] for r in out)
    print(f"контроль: {len(out)} (раунд,рынок), названо позиций {named}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "control":
        main_control(int(sys.argv[2]) if len(sys.argv) > 2 else 60)
    else:
        main_detect()
