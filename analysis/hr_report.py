"""Калибровка эмпирической hr_P (П.1 healthy-race, решение 22.08 вариант (а)).

Читает журнал бидов data/healthy_race_bids.jsonl и для каждого бида спрашивает цепь: взял ли
НАШ контракт (caller в Liquidate-логе Morpho) этого borrower'а в окне после бида. Печатает
hr_P = hits/bids и вердикт триггера снижения пола: при выборке >=20 и P_emp выше
безубыточности низкого класса ($9/приз) пол HR_MIN_PRIZE_USD опускается одной env-строкой.

Честный знаменатель: биды без доступного окна цепи (журнал старше ретенции узла) считаются
ОТДЕЛЬНО (unknown), в P не входят ни числителем, ни знаменателем — но печатаются, чтобы
цензура выборки была видна. Запуск на Шарлотте: venv/bin/python analysis/hr_report.py
"""
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.environ.get("HR_JOURNAL", os.path.join(ROOT, "data", "healthy_race_bids.jsonl"))
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
LIQUIDATE_TOPIC = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
WINDOW_BLOCKS = int(os.environ.get("HR_REPORT_WINDOW_BLOCKS", "30"))   # ~60с после бида
MIN_SAMPLE = 20
MISS_COST = 9.0

env = {}
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env.setdefault(k, v.split("#", 1)[0].strip())
URL = env.get("BG_RPC_URL") or env.get("BG_RPC_URL_2")
OUR = (env.get("LIQUIDATOR_ADDRESS") or "").lower()


def rpc(method, params):
    for _ in range(3):
        try:
            r = requests.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": params}, timeout=15).json()
            if "result" in r:
                return r["result"]
        except Exception:                                # noqa: BLE001
            pass
        time.sleep(1.0)
    return None


def block_at_ts(ts, head_bn, head_ts):
    return int(head_bn - (head_ts - ts) / 2.0)          # Base ~2с/блок


def main():
    try:
        rows = [json.loads(x) for x in open(JOURNAL) if x.strip()]
    except FileNotFoundError:
        print("журнала нет (%s) — бидов ещё не было" % JOURNAL)
        return 0
    # kind=obs_* — ЦЕНЗУРИРОВАННЫЕ наблюдения (напр. отказ по полу приза, забранный чужими):
    # в P не входят ни числителем, ни знаменателем, но копятся для будущего триггера пола.
    bids = [b for b in rows if b.get("kind", "bid") == "bid"]
    obs = [b for b in rows if str(b.get("kind", "")).startswith("obs")]
    if obs:
        print("наблюдений ниже пола (цензурированных): %d — напр. %s"
              % (len(obs), obs[-1].get("borrower", "?")))
    head = rpc("eth_getBlockByNumber", ["latest", False])
    if not head:
        print("узел не отвечает — отчёт не состоялся")
        return 1
    head_bn, head_ts = int(head["number"], 16), int(head["timestamp"], 16)
    hits, misses, unknown = 0, 0, 0
    for b in bids:
        b0 = block_at_ts(float(b["ts"]), head_bn, head_ts)
        if b0 < head_bn - 90000:                        # ~2 суток: ретенция логов не гарантирована
            unknown += 1
            continue
        topic_b = "0x" + b["borrower"][2:].lower().rjust(64, "0")
        logs = rpc("eth_getLogs", [{"fromBlock": hex(max(0, b0 - 2)),
                                    "toBlock": hex(min(head_bn, b0 + WINDOW_BLOCKS)),
                                    "address": MORPHO,
                                    "topics": [LIQUIDATE_TOPIC, None, None, topic_b]}])
        if logs is None:
            unknown += 1
            continue
        ours = [lg for lg in logs
                if OUR and OUR[2:] in str(lg["topics"][2]).lower()]
        if ours:
            hits += 1
        else:
            misses += 1
    n = hits + misses
    print("бидов в журнале: %d | судимо: %d (hits=%d misses=%d) | вне ретенции/узла: %d"
          % (len(bids), n, hits, misses, unknown))
    if n == 0:
        print("P_emp: нет судимой выборки")
        return 0
    p = hits / n
    print("hr_P = %.1f%% (выборка %d%s)" % (100 * p, n,
          "" if n >= MIN_SAMPLE else " — МЕНЬШЕ %d, триггер спит" % MIN_SAMPLE))
    for floor in (300, 100, 50):
        be = MISS_COST / floor
        ok = n >= MIN_SAMPLE and p > be
        print("  пол $%-3d: безубыток P>%.1f%% -> %s" % (floor, 100 * be,
              "МОЖНО ОПУСКАТЬ (HR_MIN_PRIZE_USD=%d)" % floor if ok else "рано"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
