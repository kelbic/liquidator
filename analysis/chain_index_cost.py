"""ЦЕНА ФОНОВОГО ИНДЕКСА (#110, блокер р-2 №6) и КОНТРАКТ КОРМЛЕНИЯ А1v3 — ЗАМЕРОМ.

Прежний замер цены индекса был снят на блоках ШТИЛЯ (55-109 КБ по проводу) и на холостой
ветке: он занижал стоимость полного чтения ЗАТОРНОГО блока в 5-20 раз и вовсе не мерил GIL
против живой заявки. Здесь всё считается на настоящих заторных блоках и на ОДНОМ ядре.

Запуск (сеть — публичные узлы Base, боевые ключи НЕ используются):
    taskset -c 0 python3 analysis/chain_index_cost.py parse   # цена разбора и выжимки
    taskset -c 0 python3 analysis/chain_index_cost.py gil     # GIL против живой заявки
    python3 analysis/chain_index_cost.py feed                 # feeHistory против ИСТИНЫ B-1
    python3 analysis/chain_index_cost.py ntx                  # суточная выборка загрузки

taskset обязателен для parse/gil: боевая машина одноядерная, а на многоядерной GIL-цена
разбора прячется — получится замер «в моменте», а не в боевых условиях.
"""
import gzip
import json
import os
import pathlib
import re
import statistics
import sys
import threading
import time
import urllib.request
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import chain.chain_index as ci                                        # noqa: E402

NODES = ["https://mainnet.base.org", "https://base-rpc.publicnode.com"]
CACHE = pathlib.Path(os.environ.get("COST_CACHE", "/tmp/chain_index_cost"))
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
# Решающие блоки денежного окна 10-13.08 (docs/STATE.md): у каждого меряем B-1 — именно он
# несёт tip-корпус, из которого А1v3 собирается предсказывать tip решающего транзита.
DECISIVE = [49805093, 49820471, 49831600, 49834406, 49835262,
            49835463, 49836628, 49841955, 49880361, 49890266]
CONGESTED = 49836628          # n_tx 5725 — самый заторный блок окна
_RE_TIP = re.compile(rb'"maxPriorityFeePerGas":"(0x[0-9a-f]+)"')
_MP, _TO = b'"maxPriorityFeePerGas":"', b'"to":"'


def rpc_raw(method, params, node=0, gz=True, timeout=60):
    """(тело, байт по проводу, RTT мс). Тело — РАСПАКОВАННОЕ, wire — как заплатили."""
    hdr = {"Content-Type": "application/json", "User-Agent": "curl/8.5.0"}
    if gz:
        hdr["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(NODES[node], headers=hdr, data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode())
    t0 = time.perf_counter()
    r = urllib.request.urlopen(req, timeout=timeout)
    body = r.read()
    wire = len(body)
    if r.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return body, wire, (time.perf_counter() - t0) * 1e3


def block_body(bn, gz=True):
    """Сырое тело eth_getBlockByNumber(fullTx). Кэш на диске: замер повторяем без сети."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / ("blk_%d.json.gz" % bn)
    if f.exists():
        return gzip.decompress(f.read_bytes()), f.stat().st_size, 0.0
    body = None
    for node in (0, 1):
        try:
            body, wire, rtt = rpc_raw("eth_getBlockByNumber", [hex(bn), True], node=node, gz=gz)
            break
        except Exception as e:
            print("  узел %d отказал на %d: %s" % (node, bn, e))
            time.sleep(3)
    if body is None:
        raise SystemExit("тело блока %d не получено" % bn)
    f.write_bytes(gzip.compress(body, 6))
    return body, wire, rtt


# ---------------------------------------------------------------- три способа взять tip-корпус
def tips_regex(raw):
    """ЛЁГКАЯ ВЫЖИМКА: только tip-ы. Без json.loads и без N словарей.
    Теряет legacy/deposit-tx (у них нет maxPriorityFeePerGas) — на заторных блоках это 11-12
    системных tx из 5725, в моду они не входят."""
    return [int(m, 16) for m in _RE_TIP.findall(raw)]


def tips_with_to(raw):
    """ВЫЖИМКА С АДРЕСАТОМ: (to, tip) на каждую tx — единственный дешёвый способ ВЫЧЕСТЬ СВОИ
    пробы из моды (самозагрязнение B-1: в 11 блоках из 50 наши tx давали 53-84% моды)."""
    out = []
    for ch in raw.split(b'{"type":"')[1:]:
        i = ch.find(_MP)
        if i < 0:
            continue
        j = ch.find(b'"', i + len(_MP))
        k = ch.find(_TO)
        out.append((ch[k + len(_TO):k + len(_TO) + 42] if k >= 0 else b"", int(ch[i + len(_MP):j], 16)))
    return out


def tips_truth(blk):
    """ИСТИНА: через настоящую chain_index.tx_tip на разобранном блоке."""
    bf = ci._hexint(blk.get("baseFeePerGas"), 0)
    return [ci.tx_tip(tx, bf) for tx in blk["transactions"]]


def med(fn, n=5):
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = fn()
        xs.append((time.perf_counter() - t0) * 1e3)
    return statistics.median(xs), r


def cmd_parse():
    print("=== ЦЕНА РАЗБОРА (ядер доступно: %d) ===" % len(os.sched_getaffinity(0)))
    for bn in (CONGESTED - 1, CONGESTED):
        raw, wire, _ = block_body(bn)
        gz = gzip.compress(raw, 6)
        t_gz, _ = med(lambda: gzip.decompress(gz))
        t_json, d = med(lambda: json.loads(raw))
        blk = d["result"]
        t_facts, _ = med(lambda: ci.build_facts(blk, [], watch_to=[BOARD]))
        t_tip, tips = med(lambda: tips_regex(raw))
        t_pair, pairs = med(lambda: tips_with_to(raw))
        truth = tips_truth(blk)
        print("\n--- блок %d: n_tx=%d, сырой %.2f МБ, по проводу(gzip) %.0f КБ ---"
              % (bn, len(truth), len(raw) / 1e6, len(gz) / 1e3))
        print("  ПОЛНЫЙ конвейер  gzip %.1f + json %.1f + build_facts %.1f = %.1f мс"
              % (t_gz, t_json, t_facts, t_gz + t_json + t_facts))
        print("  ЛЁГКИЙ (tips)    gzip %.1f + regex %.1f = %.1f мс (дешевле в %.1fx)"
              % (t_gz, t_tip, t_gz + t_tip, (t_gz + t_json + t_facts) / (t_gz + t_tip)))
        print("  ЛЁГКИЙ (to,tip)  gzip %.1f + раскол %.1f = %.1f мс (дешевле в %.1fx)"
              % (t_gz, t_pair, t_gz + t_pair, (t_gz + t_json + t_facts) / (t_gz + t_pair)))
        print("  мода: истина %s | tips %s | (to,tip) %s"
              % (Counter(truth).most_common(1)[0], Counter(tips).most_common(1)[0],
                 Counter(v for _t, v in pairs).most_common(1)[0]))


class _Hot(threading.Thread):
    """Горячий тред: тик 2мс (как recv-петля) + сетевая заявка раз в 200мс."""

    def __init__(self):
        super().__init__(daemon=True)
        self.gaps, self.rtts, self.stop = [], [], False

    def run(self):
        nxt = time.perf_counter()
        while not self.stop:
            t0 = time.perf_counter()
            time.sleep(0.002)
            self.gaps.append((time.perf_counter() - t0) * 1e3)
            if time.perf_counter() >= nxt:
                nxt = time.perf_counter() + 0.2
                try:
                    _b, _w, rtt = rpc_raw("eth_blockNumber", [], gz=False, timeout=10)
                    self.rtts.append(rtt)
                except Exception:
                    pass


def _st(xs, name):
    if not xs:
        return "%s: нет данных" % name
    xs = sorted(xs)
    return ("%s p50=%.1f p90=%.1f p99=%.1f max=%.1f мс"
            % (name, statistics.median(xs), xs[int(len(xs) * .9)], xs[int(len(xs) * .99)], xs[-1]))


def cmd_gil(secs=6.0):
    print("=== GIL ПРОТИВ ЖИВОЙ ЗАЯВКИ (ядер: %d) ===" % len(os.sched_getaffinity(0)))
    raw, _w, _r = block_body(CONGESTED)
    gz = gzip.compress(raw, 6)

    def full():
        ci.build_facts(json.loads(gzip.decompress(gz))["result"], [], watch_to=[BOARD])

    def light():
        tips_regex(gzip.decompress(gz))

    for label, work in (("ХОЛОСТОЙ контроль", lambda: time.sleep(0.05)),
                        ("ПОЛНЫЙ разбор", full), ("ЛЁГКАЯ выжимка", light)):
        h = _Hot()
        h.start()
        end = time.perf_counter() + secs
        n = 0
        while time.perf_counter() < end:
            work()
            n += 1
        h.stop = True
        h.join(timeout=3)
        print("%-18s прогонов=%-4d %s | %s"
              % (label, n, _st(h.gaps, "пробуждение горячего треда"), _st(h.rtts, "RTT заявки")))


def cmd_feed():
    """Контракт кормления: eth_feeHistory против ИСТИННОЙ моды B-1 на решающих блоках."""
    print("=== КОРМЛЕНИЕ А1v3: feeHistory против истины (B-1 решающих блоков) ===")
    PCTS = [25, 50, 75, 90]
    rows = []
    for dec in DECISIVE:
        bn = dec - 1
        raw, wire, rtt = block_body(bn)
        blk = json.loads(raw)["result"]
        truth = tips_truth(blk)
        mode, cnt = Counter(truth).most_common(1)[0]
        fh, fwire, frtt = rpc_raw("eth_feeHistory", ["0x1", hex(bn), PCTS], gz=False)
        rew = [int(x, 16) for x in json.loads(fh)["result"]["reward"][0]]
        rows.append((dec, bn, len(truth), mode, cnt, rew, fwire, frtt, wire))
        print("B=%d B-1=%d n_tx=%-4d мода=%-9d(%.0f%%) | тело %4.0fКБ | feeHistory %dБ/%3.0fмс "
              "p50=%d %s" % (dec, bn, len(truth), mode, 100 * cnt / len(truth), wire / 1e3,
                             fwire, frtt, rew[1], "СОВПАЛА" if rew[1] == mode else "разошлась"))
        time.sleep(1.5)
    ok = [r for r in rows if r[5][1] == r[3]]
    gate = [r for r in rows if r[3] > 1_000_000]          # ось HIST: мода B-1 выше пола 1e6
    gate_ok = [r for r in gate if r[5][1] == r[3]]
    print("\nfeeHistory p50 == ИСТИННАЯ мода B-1: %d из %d; на ОТКРЫТОМ гейте (мода>1e6): "
          "%d из %d" % (len(ok), len(rows), len(gate_ok), len(gate)))


def cmd_ntx():
    """Суточная выборка загрузки: 144 блока шагом 10 минут (дешёвый счётчик tx)."""
    head = int(json.loads(rpc_raw("eth_blockNumber", [], gz=False)[0])["result"], 16)
    ns = []
    for i in range(144):
        bn = head - i * 300
        try:
            b = json.loads(rpc_raw("eth_getBlockTransactionCountByNumber", [hex(bn)], gz=False)[0])
            ns.append(int(b["result"], 16))
        except Exception:
            pass
        time.sleep(0.4)
    ns.sort()
    print("суточная выборка %d блоков: p50=%d p90=%d p95=%d max=%d; доля n_tx>=1000: %.1f%%"
          % (len(ns), statistics.median(ns), ns[int(.9 * len(ns))], ns[int(.95 * len(ns))],
             ns[-1], 100 * sum(1 for n in ns if n >= 1000) / len(ns)))


if __name__ == "__main__":
    {"parse": cmd_parse, "gil": cmd_gil, "feed": cmd_feed,
     "ntx": cmd_ntx}[(sys.argv[1:] or ["parse"])[0]]()
