"""Ш22: МЕТРИКА ПО ИСТОРИИ — сколько НАШИХ tx попадало в призовое окно [T+1, T+20].

Зачем именно так. Два месяца приёмка шла по МЕХАНИЗМУ («арм сел», «пробы ушли», «тесты
зелёные»), и метрика не двигалась ни разу. С 11.08 действует правило: правка боевого пути не
принята, пока не названо, как она двигает ЭТО число. Значит число надо уметь считать — и не
по одному окну, а по всей истории.

Почему история, а не живое окно. Каскады редки (ждали два месяца), каждое окно — это деньги,
и тратить их на замеры запрещено (решение kelbic 11.08). Форк для вопросов ПОЗИЦИИ непригоден
по устройству (FORK_REHEARSAL.md: не моделирует ни FCFS, ни позицию, ни конкурентов). А вся
нужная фактура лежит в прошлых блоках и стоит только времени.

Что считаем на каждом блоке с ликвидацией:
  * индекс транзита (AnswerUpdated) и его tip;
  * призовое окно [T+1, T+20]: чей tip, кто занял;
  * ПОЛ блока (доминирующий tip) — он уехал 1e6 -> 3e6 между 07.08 и 11.08, и именно поэтому
    константа в _PROBE_TIP_WEI невозможна в принципе;
  * НАШИ tx: индексы, tip, и сколько их попало в окно.

Ходит на ПУБЛИЧНУЮ ноду (боевой ключ не трогаем), троттлит, кэширует блоки на диск.

ЗАПУСК: python3 analysis/history_position.py --days 7 [--out /path/report.json]
"""
import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request

RPC = os.environ.get("RECON_RPC_URL", "https://base-rpc.publicnode.com")
THROTTLE = float(os.environ.get("RECON_THROTTLE_SEC", "0.25"))
CACHE = pathlib.Path(os.environ.get("HP_CACHE", "/home/claude-agent/.claude/jobs/c5adabde/tmp/blkcache"))

MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
# Liquidate(Id indexed, address indexed caller, address indexed borrower, uint256 x5)
LIQUIDATE = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
PRIZE = 20                                   # окно [T+1, T+20]

# НАШИ адреса. Пробы летят В доску, боевые ликвидации — В ликвидатор с главного кошелька.
# Ноги флота по именам не нужны: любая наша проба идёт В доску, кем бы ни была отправлена.
# ВНИМАНИЕ: адреса берутся ТОЛЬКО полностью известные. Раньше в этот список чуть не попал
# «v4» по одному префиксу 0xe83B713d — то есть ВЫДУМАННЫЙ адрес. Пробы летят в доску v5, этого
# для метрики достаточно; недостающие поколения добавлять только с полным адресом из цепи.
OUR_TO = {
    "0x85bade2cdd375ab1b75f5c522b7989a12f939292",   # доска v5 — сюда идут ВСЕ пробы флота
    "0x54730ee13f10c073772f25ee475ee13f2252fb82",   # ликвидатор
}
OUR_FROM = {"0xe2aca8e68ae3b7efcfce4fd82abdfc2a9d7908fe"}   # главный кошелёк


def rpc(method, params, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                RPC, data=body,
                headers={"content-type": "application/json", "User-Agent": "curl/8.5"})
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read())
            if "result" in j:
                return j["result"]
            last = j.get("error")
        except Exception as e:                       # noqa: BLE001
            last = e
        time.sleep(1.2)
    return {"__err__": str(last)}


def get_block(n):
    """Полный блок с транзакциями, с кэшем на диск (блоки неизменны — кэш безопасен)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / ("%d.json" % n)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:                            # noqa: BLE001
            pass
    b = rpc("eth_getBlockByNumber", [hex(n), True])
    if isinstance(b, dict) and "__err__" not in b and b.get("transactions") is not None:
        f.write_text(json.dumps(b))
        time.sleep(THROTTLE)
        return b
    return None


def tip_of(t):
    return int(t.get("maxPriorityFeePerGas") or "0x0", 16)


def liquidate_blocks(lo, hi, step=2000):
    """Блоки с событиями Liquidate. Публичная нода режет диапазон — идём окнами."""
    out = {}
    n = lo
    while n <= hi:
        end = min(n + step - 1, hi)
        logs = rpc("eth_getLogs", [{"fromBlock": hex(n), "toBlock": hex(end),
                                    "address": MORPHO, "topics": [LIQUIDATE]}])
        if isinstance(logs, dict) and "__err__" in logs:
            if step > 100:                            # сузить окно и повторить
                step //= 4
                continue
            sys.stderr.write("окно %d..%d не отдалось: %s\n" % (n, end, logs["__err__"]))
            n = end + 1
            continue
        for lg in logs or []:
            out.setdefault(int(lg["blockNumber"], 16), []).append(lg)
        sys.stderr.write("  сканировано до %d, блоков с ликвидацией %d\r" % (end, len(out)))
        n = end + 1
        time.sleep(THROTTLE)
    sys.stderr.write("\n")
    return out


def analyse_block(n):
    b = get_block(n)
    if not b:
        return {"block": n, "error": "блок не отдан"}
    txs = b["transactions"]
    tips = [tip_of(t) for t in txs]
    if not tips:
        return {"block": n, "error": "пустой блок"}
    floor = min(tips)
    from collections import Counter
    dominant, dom_n = Counter(tips).most_common(1)[0]

    # транзит: AnswerUpdated в этом блоке (любой фид)
    lg = rpc("eth_getLogs", [{"fromBlock": hex(n), "toBlock": hex(n),
                              "topics": [ANSWER_UPDATED]}])
    time.sleep(THROTTLE)
    tr = sorted(int(x["transactionIndex"], 16) for x in (lg or [])) \
        if isinstance(lg, list) else []

    ours = [(int(t["transactionIndex"], 16), tip_of(t)) for t in txs
            if (t.get("to") or "").lower() in OUR_TO
            or (t.get("from") or "").lower() in OUR_FROM]

    res = {"block": n, "n_tx": len(txs), "floor": floor,
           "dominant_tip": dominant, "dominant_share": round(dom_n / len(txs), 4),
           "transit_idx": tr[0] if tr else None, "n_transits": len(tr),
           "ours": ours, "ours_n": len(ours), "in_prize": 0, "prize_tips": []}
    if tr:
        t0 = tr[0]
        res["transit_tip"] = tips[t0] if t0 < len(tips) else None
        res["in_prize"] = sum(1 for i, _ in ours if t0 < i <= t0 + PRIZE)
        res["prize_tips"] = sorted({tips[i] for i in range(t0 + 1, min(t0 + PRIZE + 1, len(tips)))})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--out", default="/home/claude-agent/.claude/jobs/c5adabde/tmp/hp_report.json")
    ap.add_argument("--max-blocks", type=int, default=200)
    a = ap.parse_args()

    head = int(rpc("eth_blockNumber", []), 16)
    span = int(a.days * 24 * 3600 / 2)               # блок Base = 2с
    lo = head - span
    print("голова %d, окно %d..%d (%.1f суток)" % (head, lo, head, a.days))

    blocks = liquidate_blocks(lo, head)
    order = sorted(blocks)
    print("блоков с ликвидацией: %d" % len(order))
    # ПОЗИТИВНЫЙ КОНТРОЛЬ: пустой результат обязан отличаться от «топик не тот».
    # Канон проекта: «логов нет ⇒ явления нет» верно ЛИШЬ после контроля на том же объекте.
    if not order:
        probe = rpc("eth_getLogs", [{"fromBlock": hex(head - 2000), "toBlock": hex(head),
                                     "address": MORPHO}])
        n_any = len(probe) if isinstance(probe, list) else -1
        print("КОНТРОЛЬ: логов Morpho без фильтра топика за 2000 блоков: %d" % n_any)
        print("  0 -> нода/адрес; >0 -> ликвидаций правда не было; -1 -> ошибка запроса")
        return 1
    if len(order) > a.max_blocks:
        order = order[-a.max_blocks:]
        print("берём последние %d (кап --max-blocks)" % len(order))

    rows = []
    for k, n in enumerate(order, 1):
        r = analyse_block(n)
        r["n_liq"] = len(blocks[n])
        rows.append(r)
        sys.stderr.write("  разобрано %d/%d (блок %d)\r" % (k, len(order), n))
    sys.stderr.write("\n")

    pathlib.Path(a.out).write_text(json.dumps(rows, ensure_ascii=False))
    ok = [r for r in rows if not r.get("error")]
    with_tr = [r for r in ok if r.get("transit_idx") is not None]
    total_prize = sum(r["in_prize"] for r in ok)
    print("\n=== ИТОГ ===")
    print("разобрано блоков: %d (с транзитом: %d)" % (len(ok), len(with_tr)))
    print("ликвидаций всего: %d" % sum(r["n_liq"] for r in ok))
    print("блоков, где были НАШИ tx: %d" % sum(1 for r in ok if r["ours_n"]))
    print("НАШИХ tx в призовом окне [T+1,T+20]: %d   <-- МЕТРИКА" % total_prize)
    print("отчёт: %s" % a.out)


if __name__ == "__main__":
    sys.exit(main())
