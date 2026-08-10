"""Ш10: где НАШИ пробы сели относительно транзита (#97).

Зачем инструмент. Форк не моделирует ни FCFS, ни позицию, ни конкурентов — это записано в
docs/FORK_REHEARSAL.md и подтверждено приёмкой #85. Поэтому единственный способ проверить
политику tip — смотреть на РЕАЛЬНЫЕ блоки. Главную находку проекта (внутри суб-блока порядок по
УБЫВАНИЮ tip; транзит и призы лежат на полу в хвосте) дал РУЧНОЙ разбор одного блока. Здесь тот
же разбор становится инструментом: его можно гнать по каждому блоку, где были наши пробы, и
получать метрику продвижения вместо ежемесячного ручного вскрытия.

Метрика, ради которой всё: ЧИСЛО НАШИХ TX НА ПОЛУ ПРИОРИТЕТА В ОКНЕ [T+1, T+20].
У победителей в разобранном блоке 49666338 там было 8, 8, 3 и 1. У нас — 0.

Стоимость: ходит на ПУБЛИЧНЫЙ узел (квоту Alchemy не трогает), ~150 запросов на блок с
троттлом. Целый блок публичная нода не отдаёт (4139 tx), поэтому читаем окно по индексам.

ЗАПУСК:
    python3 analysis/block_position.py 49666338 [--feed 0x92a7c3a5...] [--win 60]
"""
import json
import os
import sys
import time

import requests

RPC = os.environ.get("RECON_RPC_URL", "https://mainnet.base.org")
THROTTLE = float(os.environ.get("RECON_THROTTLE_SEC", "0.45"))
# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
PRIZE_WINDOW = 20                     # [T+1, T+20] — окно, где забрали все призы в 49666338

_S = requests.Session()


def _rpc(method, params, tries=4):
    for _ in range(tries):
        try:
            r = _S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params}, timeout=25)
            if r.status_code == 200:
                j = r.json()
                if "result" in j:
                    return j["result"]
        except Exception:
            pass
        time.sleep(1.0)
    return None


def find_transit(block, feed=None):
    """Индекс tx, обновившей оракул в этом блоке. None, если пуша не было.

    Ищем по событию агрегатора, а не по отправителю: пушер меняется, событие — нет."""
    flt = {"fromBlock": hex(block), "toBlock": hex(block), "topics": [ANSWER_UPDATED]}
    if feed:
        flt["address"] = feed
    logs = _rpc("eth_getLogs", [flt]) or []
    return sorted(int(l["transactionIndex"], 16) for l in logs) if logs else []


def scan_window(block, center, half):
    """[(idx, from, to, tip_wei)] по окну индексов. Публичная нода целый блок не отдаёт."""
    out = []
    for i in range(max(0, center - half), center + half + 1):
        t = _rpc("eth_getTransactionByBlockNumberAndIndex", [hex(block), hex(i)])
        time.sleep(THROTTLE)
        if not t:
            continue
        out.append((i, (t.get("from") or "").lower(), (t.get("to") or "").lower(),
                    int(t.get("maxPriorityFeePerGas", "0x0"), 16)))
    return out


def segments(rows):
    """Границы суб-блоков = точки РОСТА tip (внутри суб-блока порядок по убыванию).

    Возвращает индексы строк, на которых начинается новый сегмент."""
    starts = []
    prev = None
    for n, (_i, _f, _t, tip) in enumerate(rows):
        if prev is not None and tip > prev:
            starts.append(n)
        prev = tip
    return starts


def analyse(block, ours, feed=None, half=60):
    """Разбор одного блока. ours — множество наших адресов (from) в нижнем регистре."""
    tr = find_transit(block, feed)
    if not tr:
        return {"block": block, "transit": None, "error": "пуша оракула в блоке нет"}
    t_idx = tr[0]
    rows = scan_window(block, t_idx, half)
    by_idx = {r[0]: r for r in rows}
    floor = min((r[3] for r in rows), default=0)
    seg_starts = segments(rows)

    mine = [r for r in rows if r[1] in ours]
    prize = [r for r in mine if t_idx < r[0] <= t_idx + PRIZE_WINDOW]
    prize_floor = [r for r in prize if r[3] == floor]
    # кто ВООБЩЕ стоял в призовом окне — для честного знаменателя «сколько там мест»
    in_prize_all = [r for r in rows if t_idx < r[0] <= t_idx + PRIZE_WINDOW]
    return {
        "block": block, "transit": t_idx, "transit_tip": by_idx.get(t_idx, (0, "", "", 0))[3],
        "floor": floor, "scanned": len(rows), "segment_starts": len(seg_starts),
        "ours_in_window": [(r[0], r[0] - t_idx, r[3]) for r in mine],
        "ours_in_prize": len(prize), "ours_in_prize_at_floor": len(prize_floor),
        "prize_window_total": len(in_prize_all),
        "rivals_in_prize": {},
    }


def _fmt(res):
    if res.get("error"):
        return "блок %d: %s" % (res["block"], res["error"])
    L = ["блок %d: транзит на idx %d (tip %.4f gwei), пол окна %.4f gwei" % (
        res["block"], res["transit"], res["transit_tip"] / 1e9, res["floor"] / 1e9),
        "  просмотрено позиций %d, границ суб-блоков в окне %d" % (
            res["scanned"], res["segment_starts"]),
        "  НАШИХ tx в окне: %d%s" % (
            len(res["ours_in_window"]),
            ("  " + ", ".join("idx %d (%+d, tip %.4f)" % (i, d, t / 1e9)
                              for i, d, t in res["ours_in_window"])
             if res["ours_in_window"] else "")),
        "  В ПРИЗОВОМ ОКНЕ [T+1, T+%d]: %d наших (на полу: %d) из %d занятых мест" % (
            PRIZE_WINDOW, res["ours_in_prize"], res["ours_in_prize_at_floor"],
            res["prize_window_total"])]
    return "\n".join(L)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    block = int(sys.argv[1])
    feed = None
    half = 60
    for i, a in enumerate(sys.argv):
        if a == "--feed" and i + 1 < len(sys.argv):
            feed = sys.argv[i + 1]
        if a == "--win" and i + 1 < len(sys.argv):
            half = int(sys.argv[i + 1])
    ours = {a.strip().lower() for a in
            (os.environ.get("OUR_SENDERS", "") or "").split(",") if a.strip()}
    if not ours:
        print("OUR_SENDERS не задан (список наших from через запятую) — считаю только транзит")
    res = analyse(block, ours, feed=feed, half=half)
    print(_fmt(res))
    if os.environ.get("BLOCK_POSITION_JSON"):
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
