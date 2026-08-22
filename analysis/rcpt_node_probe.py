"""Замер: держит ли узел ПАРТИИ eth_getTransactionReceipt (решение из замера, не из надежды).

22.08, ключ D: прежде чем ставить узел вторым бесплатным в цепочку квитанций, меряем партии
1/3/16/64 на живых хэшах последнего блока. Урок #101-b: lb.drpc.live принимал 1-3 и падал
HTTP 500 с четырёх — граница проверяется ПОВТОРОМ (n=2 на размер), не одиночным тыком.

Запуск на машине, где ключ в .env:  python3 analysis/rcpt_node_probe.py [ИМЯ_ПЕРЕМЕННОЙ]
Печатает ТОЛЬКО хост и вердикты по размерам — URL с ключом в вывод не попадает.
"""
import json
import sys
import time
import urllib.parse

import requests

VAR = sys.argv[1] if len(sys.argv) > 1 else "ALCHEMY_D_URL"
env = {}
for p in ("/root/liquidator/.env", ".env"):
    try:
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k, v.split("#", 1)[0].strip())
    except FileNotFoundError:
        pass
URL = env.get(VAR) or ""
if not URL:
    print("Переменная %s не найдена в .env — замер не состоялся" % VAR)
    sys.exit(1)
HOST = urllib.parse.urlparse(URL).netloc
print("узел: %s (из %s)" % (HOST, VAR))


def rpc_one(method, params):
    r = requests.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=15)
    r.raise_for_status()
    return r.json()["result"]


blk = rpc_one("eth_getBlockByNumber", ["latest", False])
hashes = blk["transactions"]
print("позитивный контроль: блок %d, tx в блоке %d" % (int(blk["number"], 16), len(hashes)))
if len(hashes) < 64:
    print("в блоке <64 tx — беру предыдущий тоже")
    blk2 = rpc_one("eth_getBlockByNumber", [hex(int(blk["number"], 16) - 1), False])
    hashes = hashes + blk2["transactions"]

verdict = {}
for size in (1, 3, 16, 64):
    results = []
    for attempt in range(2):                          # граница проверяется ПОВТОРОМ
        batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionReceipt",
                  "params": [h]} for i, h in enumerate(hashes[:size])]
        try:
            r = requests.post(URL, json=batch, timeout=20)
            body = r.json() if r.status_code == 200 else None
            ok = (isinstance(body, list) and len(body) == size
                  and all("result" in x or "error" not in x for x in body)
                  and sum(1 for x in body if x.get("result")) > 0)
            results.append("OK" if ok else "ОТКАЗ(http=%d,форма=%s)"
                           % (r.status_code, type(body).__name__))
        except Exception as e:                        # noqa: BLE001
            results.append("ОТКАЗ(%s)" % type(e).__name__)
        time.sleep(1.0)
    verdict[size] = results
    print("партия n=%-3d: %s" % (size, " ".join(results)))

full = all(v == ["OK", "OK"] for v in verdict.values())
print("ВЕРДИКТ: %s" % ("держит партии до 64 — ставить БЕЗ нарезки (CHUNK_2=0)" if full else
                       "партии держит не все — CHUNK_2 = наибольший стабильный размер, "
                       "либо одиночные (см. вердикты выше)"))
