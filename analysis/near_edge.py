#!/usr/bin/env python3
"""СКОЛЬКО ПОЗИЦИЙ У КРОМКИ ПРЯМО СЕЙЧАС — прибор, независимый от бота.

ЗАЧЕМ. 27.08 флот замолчал после включения пола досягаемости, и вопрос «мы ослепли или рынок
спокоен» нельзя было решить журналом бота: журнал — самоотчёт механизма. Этот скрипт собирает
заёмщиков рынка из Borrow-логов Morpho и считает HF КАЖДОГО прямо с цепи (position / market /
oracle.price), то есть отвечает на вопрос эталоном, а не мнением бота.

Первый прогон 27.08 19:4xZ, 7 рынков, 12 250 живых позиций: НИ ОДНОЙ внутри 3% от кромки.
Ближайшая по всем рынкам — cbBTC 4.066%. То есть молчание флота было честным.

Запуск на боевой машине (только чтение):
    ./venv/bin/python analysis/near_edge.py            # рынки с добычей за 7 суток
    ./venv/bin/python analysis/near_edge.py 0xd4a903dc # конкретный рынок по префиксу
"""
import json
import sys
import urllib.request

MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
LIQ_TOPIC = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
# keccak("Borrow(bytes32,address,address,address,uint256,uint256)") — ПРОВЕРЕНО вычислением,
# а не переписано по памяти: первая редакция этого файла несла неверный литерал и дала бы
# ТИХИЙ НОЛЬ заёмщиков, то есть «у кромки никого» из пустого чтения (канон «нулевой тест»).
BORROW_TOPIC = "0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43"


def _rpc_url():
    for f in (".env", ".env.g2d"):
        try:
            for ln in open(f):
                if ln.startswith("RPC_URL="):
                    return ln.split("=", 1)[1].strip()
        except OSError:
            pass
    raise SystemExit("RPC_URL не найден (запускать из /root/liquidator)")


def rpc(url, method, params):
    req = urllib.request.Request(url, json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=90)).get("result")


def sym(url, a):
    r = rpc(url, "eth_call", [{"to": a, "data": "0x95d89b41"}, "latest"])
    if not r or len(r) < 130:
        return a[:10]
    n = int(r[66:130], 16)
    return bytes.fromhex(r[130:130 + n * 2]).decode(errors="replace")


def near_edge(url, mid, lookback=600000):
    """-> (заёмщиков, живых, [(dist%, долг_raw)] по возрастанию dist) либо None."""
    head = int(rpc(url, "eth_blockNumber", []), 16)
    mp = rpc(url, "eth_call", [{"to": MORPHO, "data": "0x2c3c9157" + mid[2:]}, "latest"])
    if not mp or len(mp) < 322:
        return None
    oracle, lltv = "0x" + mp[154:194], int(mp[258:322], 16)
    px = rpc(url, "eth_call", [{"to": oracle, "data": "0xa035b1fe"}, "latest"])
    mk = rpc(url, "eth_call", [{"to": MORPHO, "data": "0x5c60e39a" + mid[2:]}, "latest"])
    if not px or not mk:
        return None
    px = int(px, 16)
    w = [int(mk[2 + 64 * i:2 + 64 * (i + 1)], 16) for i in range(6)]
    bors, b = set(), head
    while b > head - lookback:
        lo = max(b - 19999, head - lookback)
        for x in rpc(url, "eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(b),
                                           "address": MORPHO,
                                           "topics": [BORROW_TOPIC, mid]}]) or []:
            bors.add("0x" + x["topics"][3][-40:])
        b = lo - 1
    near, alive = [], 0
    for bo in bors:
        po = rpc(url, "eth_call", [{"to": MORPHO,
                                    "data": "0x93c52062" + mid[2:] + bo[2:].rjust(64, "0")},
                                   "latest"])
        if not po or len(po) < 194:
            continue
        p = [int(po[2 + 64 * i:2 + 64 * (i + 1)], 16) for i in range(3)]
        if p[1] == 0:
            continue
        alive += 1
        debt = p[1] * w[2] / w[3] if w[3] else 0.0
        if not debt:
            continue
        hf = (p[2] * px / 1e36 * lltv / 1e18) / debt
        if hf > 1:
            near.append(((1.0 - 1.0 / hf) * 100.0, debt))
    near.sort()
    if not bors:
        # ПОЗИТИВНЫЙ КОНТРОЛЬ: рынок с ликвидациями обязан иметь заёмщиков. Пусто = мы читаем
        # пустоту (не тот топик/окно/узел), а не «никого нет». Отдаём None, чтобы вызывающий
        # напечатал отказ, а не тишину.
        return None
    return len(bors), alive, near


def main():
    url = _rpc_url()
    want = sys.argv[1].lower() if len(sys.argv) > 1 else None
    head = int(rpc(url, "eth_blockNumber", []), 16)
    mids, b = {}, head
    while b > head - 302400:                       # рынки с добычей за 7 суток
        lo = max(b - 9999, head - 302400)
        for x in rpc(url, "eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(b),
                                           "address": MORPHO, "topics": [LIQ_TOPIC]}]) or []:
            mids[x["topics"][1]] = mids.get(x["topics"][1], 0) + 1
        b = lo - 1
    print("рынок       залог/заём          заёмщиков живых  ближайший  <=1%  <=3%")
    for mid, _n in sorted(mids.items(), key=lambda kv: -kv[1]):
        if want and not mid.lower().startswith(want):
            continue
        r = near_edge(url, mid)
        if not r:
            print("  %-11s читается плохо" % mid[:10])
            continue
        tot, alive, near = r
        mp = rpc(url, "eth_call", [{"to": MORPHO, "data": "0x2c3c9157" + mid[2:]}, "latest"])
        lbl = "%s/%s" % (sym(url, "0x" + mp[90:130]), sym(url, "0x" + mp[26:66]))
        print("  %-11s %-18s %6d %6d  %8.3f%%  %4d  %4d" % (
            mid[:10], lbl, tot, alive, near[0][0] if near else -1,
            sum(1 for x in near if x[0] <= 1.0), sum(1 for x in near if x[0] <= 3.0)))


if __name__ == "__main__":
    main()
