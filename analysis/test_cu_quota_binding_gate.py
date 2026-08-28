"""НЕГАТИВНЫЙ КОНТРОЛЬ гарда молчания ветки темпа (28.08).

Гоняет НАСТОЯЩИЙ run() (не помощника: канон seam-stubbed-above-the-defect — 21.08 суита
звала помощника напрямую и осталась зелёной со СНЯТЫМ фиксом). Все пути — из history_path,
боевые файлы не трогаются вовсе; канал — обязательный notify_fn-рекордер.
"""
import json, os, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/home/claude-agent/work/liquidator")
from analysis import cu_quota_watch as W

# ФИКСТУРА ЗАМОРОЖЕНА В РЕПО, А НЕ ЧИТАЕТСЯ ИЗ ~/.fleet-watch. Живой каталог сделал бы
# стенд КРАСНЫМ через час по построению: боевой крон дописывает окно в историю каждый час
# в :07, и как только последняя запись станет новее NOW, history_cutoff съест журнал стенда,
# run() уйдёт в слепой прогон и до ветки не дойдёт — ровно тот ноль, который этот стенд и
# ловит. Плюс пере-якорение 01.09 (свежее ведро 30M, горизонт ~месяц) перевернуло бы посылку
# половины А. Снимок несёт только публичные теги #crc, значений ключей в нём нет.
FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "binding_gate")
NOW = 1787909400.0  # 28.08 09:30:00Z — ПОСЛЕ последнего окна истории (09:07:01), иначе
                    # history_cutoff съедает журнал стенда и run() уходит в слепой прогон
HOST = "base-mainnet.g.alchemy.com#94bf"

JOURNAL = "\n".join(
    "авг 28 09:2%d server python[999]: 2026-08-28 09:2%d:00 INFO CU %s: 1200 CU/окно "
    "(проекция 3.0M/мес, скольз.70мин) | eth_call@hotpath.py:read_preconf_price×9" % (i, i, HOST)
    for i in range(1, 4))


def stand(tag, mutate=None):
    d = tempfile.mkdtemp(prefix="cuctl-")
    hp = os.path.join(d, "cu-quota.history.jsonl")
    shutil.copy(os.path.join(FW, "cu-quota.history.jsonl"), hp)
    for name in ("cu-quota.anchor.json", "cu-quota.ledger.json"):
        shutil.copy(os.path.join(FW, name), os.path.join(d, name))
    if mutate:
        mutate(d)
    sent = []
    lines = []
    W.run(fetch=lambda: JOURNAL,
          notify_fn=lambda msg, **kw: (sent.append((kw.get("key"), msg)), "inbox")[1],
          out=lambda s: lines.append(str(s)), now=NOW, history_path=hp)
    keys = [k for k, _ in sent]
    gate = [l for l in lines if "СВЯЗЫВАЮЩАЯ ВЕЛИЧИНА ПРОХОДИТ" in l]
    print("[%s] звонков ветки темпа по %s: %d | строк гарда: %d"
          % (tag, HOST, sum(1 for k in keys if k == "cu-quota:" + HOST), len(gate)))
    for g in gate:
        print("    " + g[:400])
    for k, m in sent:
        print("    ЗВОНОК key=%s :: %s" % (k, m.splitlines()[0][:150]))
    return sum(1 for k in keys if k == "cu-quota:" + HOST)


def drain_bucket(d):
    """ПОЛОВИНА (б): остаток НИЖЕ спроса при худшем часе -> ветка ОБЯЗАНА звонить."""
    p = os.path.join(d, "cu-quota.anchor.json")
    a = json.load(open(p))
    a["hosts"][HOST]["mtd_cu"] = 24_500_000     # остаток ~1.1M против спроса ~17.5M
    json.dump(a, open(p, "w"))



def test_гард_закрывает_звонок_когда_ведро_доживает():
    assert stand("A") == 0


def test_гард_пропускает_звонок_когда_ведро_не_доживает():
    assert stand("B", drain_bucket) >= 1

