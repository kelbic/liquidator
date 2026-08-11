"""Ш24-А: измеритель ДОМИНИРУЮЩЕГО КЛАССА tip (входного билета в блок).

ЗАЧЕМ. Замер Ш22 показал: во всех четырёх блоках с ликвидациями доминирующий tip был ВЫШЕ
нашего (1 034 000 / 3 000 000 / 3 000 000 / 1 150 000 против нашей константы 1 000 000).
Призовое окно лежит ВНУТРИ доминирующего класса — на 49820471 все двадцать позиций за транзитом
стоят ровно на 3 000 000, и сам транзит тоже. Значит присутствие в блоке (Ш26) необходимо, но
недостаточно: без попадания в класс мы садимся в хвост, позади ~90% блока.

ПОЧЕМУ РОВНО КЛАСС, БЕЗ НАДБАВКИ. Внутри суб-блока порядок по УБЫВАНИЮ tip. Плата ВЫШЕ транзита
кладёт нас ПЕРЕД ним — туда, где цена ещё старая и позиция здорова. Это дефект #91, за который
уже заплачено. Плата НИЖЕ кладёт в хвост за всем классом — это то, что происходит сейчас.
Цель — ровно класс: внутри него нас с победителями разводит порядок прибытия, а не цена.

МЕТОД. `eth_feeHistory` вместо выкачивания блока: сверено на четырёх блоках с известной истиной,
перцентили p25 и p50 совпали с модой блока ДО ПОСЛЕДНЕЙ ЦИФРЫ. Один дешёвый вызов вместо
нескольких мегабайт. p10 на лёгком блоке (628 tx) расходится — берём p25/p50.

ЗАПУСК: python3 analysis/tip_floor_watch.py [--minutes 120] [--every 30]
Пишет JSONL: {ts, block, p25, p50, ours, in_class}
"""
import argparse
import json
import pathlib
import subprocess
import sys
import time
from collections import Counter

RPC = "https://mainnet.base.org"
BLOCK_RPC = "https://base-rpc.publicnode.com"
OUR_TIP = 1_000_000                      # chain/execute.py: _PROBE_TIP_WEI


def rpc(method, params, url=RPC, tries=3):
    for _ in range(tries):
        r = subprocess.run(
            ["curl", "-s", "--max-time", "60", "-X", "POST", url,
             "-H", "content-type: application/json", "-H", "User-Agent: curl/8",
             "-d", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})],
            capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            if "result" in j:
                return j["result"]
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(1.0)
    return None


def self_test():
    """ПОЗИТИВНЫЙ КОНТРОЛЬ: feeHistory обязан воспроизвести моду ИЗВЕСТНОГО блока.

    Без этого «пол такой-то» неотличим от «узел отдаёт что попало» — а на ложном нуле мы уже
    один раз чуть не построили вывод (Ш22)."""
    n = 49820471
    b = rpc("eth_getBlockByNumber", [hex(n), True], url=BLOCK_RPC)
    if not b:
        print("КОНТРОЛЬ: блок %d не отдан — считать нечего" % n)
        return False
    tips = [int(t.get("maxPriorityFeePerGas") or "0x0", 16) for t in b["transactions"]]
    mode = Counter(tips).most_common(1)[0][0]
    fh = rpc("eth_feeHistory", [1, hex(n), [25, 50]])
    if not fh or not fh.get("reward"):
        print("КОНТРОЛЬ: feeHistory не отдал")
        return False
    p25, p50 = (int(x, 16) for x in fh["reward"][0])
    ok = (p25 == mode == p50)
    print("КОНТРОЛЬ блок %d: мода %d, feeHistory p25 %d / p50 %d -> %s"
          % (n, mode, p25, p50, "СОВПАЛО" if ok else "РАСХОЖДЕНИЕ"))
    return ok


def sample(count=20):
    """Доминирующий класс по последним `count` блокам. -> [(block, p25, p50)]"""
    fh = rpc("eth_feeHistory", [count, "latest", [25, 50]])
    if not fh or not fh.get("reward"):
        return []
    first = int(fh["oldestBlock"], 16)
    out = []
    for i, rw in enumerate(fh["reward"]):
        p25, p50 = (int(x, 16) for x in rw)
        out.append((first + i, p25, p50))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=120.0)
    ap.add_argument("--every", type=float, default=30.0)
    ap.add_argument("--out", default="/home/claude-agent/.claude/jobs/c5adabde/tmp/tipfloor.jsonl")
    a = ap.parse_args()

    if not self_test():
        print("контроль не пройден — измеритель не доказал, что видит истину")
        return 2

    f = pathlib.Path(a.out)
    seen = set()
    t_end = time.time() + a.minutes * 60
    vals = []
    while time.time() < t_end:
        for blk, p25, p50 in sample():
            if blk in seen:
                continue
            seen.add(blk)
            vals.append(p50)
            with f.open("a") as fh:
                fh.write(json.dumps({"ts": int(time.time()), "block": blk, "p25": p25,
                                     "p50": p50, "ours": OUR_TIP,
                                     "in_class": OUR_TIP >= p50}) + "\n")
        if vals:
            c = Counter(vals)
            below = sum(1 for v in vals if OUR_TIP < v)
            sys.stderr.write(
                "  блоков %d | доминирующий: %s | НАШИ 1e6 ниже класса в %d (%.0f%%)\r"
                % (len(vals), ", ".join("%d×%d" % (n, v) for v, n in c.most_common(3)),
                   below, 100.0 * below / len(vals)))
        time.sleep(a.every)
    sys.stderr.write("\n")

    c = Counter(vals)
    below = sum(1 for v in vals if OUR_TIP < v)
    print("\n=== ИТОГ за %.0f мин, блоков %d ===" % (a.minutes, len(vals)))
    print("разброс доминирующего класса: min %d, max %d" % (min(vals), max(vals)))
    print("частоты: %s" % ", ".join("%d wei × %d" % (v, n) for v, n in c.most_common(8)))
    print("наш tip 1 000 000 НИЖЕ класса в %d из %d блоков (%.1f%%)  <-- ключевое число"
          % (below, len(vals), 100.0 * below / len(vals)))
    print("файл: %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
