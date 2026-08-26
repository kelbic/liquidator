# -*- coding: utf-8 -*-
"""ШОВ водораздела полос: гоняем НАСТОЯЩИЙ run(), а не помощника (канон seam-stubbed-above-the-defect).

Класс, ради которого тест есть: 26.08 фикс d75268e внёс полосу в ключ дедупа и был зелен
юнитами, а в бою через час пропустил УЛУЧШЕНИЕ (sev2 11:07Z -> sev1 12:07Z) как ухудшение.
Юнит на чистой функции такое не ловит — ловит только прогон всей run() до notify_fn.
КРАСНЫЙ-НА-БАЗЕ: со снятым severity_should_ring падает случай A.
"""
import json, os, shutil, tempfile
import analysis.cu_quota_watch as M

HOST = "base-mainnet.g.alchemy.com#3133"
NOW = 1787749623.0        # 26.08 13:07:03Z — первый прогон ПОСЛЕ разбираемой тревоги
PREV = 1787746023.0       # 12:07:03Z — момент разбираемой тревоги
W = os.path.join(os.path.expanduser("~"), ".fleet-watch")


def _журнал(n=30, step=120):
    """n окон бота подряд на темпе 200M/мес — заведомо дефицитных для остатка A#3133."""
    строки = []
    for i in range(n):
        t = NOW - (n - i) * step
        import time as _t
        iso = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(t))
        строки.append("Aug 26 %s host python[4242]: %s INFO CU %s: 9259 CU/окно = 77 CU/s "
                      "(проекция 200.0M/мес, скольз.26мин 200.0M/мес) | eth_call@x.py:y×3"
                      % (iso[11:], iso, HOST))
    return "\n".join(строки) + "\n"


def _прогон(seed_band):
    d = tempfile.mkdtemp()
    for f in ("cu-quota.history.jsonl", "cu-quota.anchor.json", "cu-quota.ledger.json"):
        shutil.copy(os.path.join(W, f), os.path.join(d, f))
    st = {"fails": 0, "last_ok": NOW}
    if seed_band is not None:
        st["sev_marks"] = {HOST: {"band": seed_band, "ts": PREV}}
    open(os.path.join(d, "cu-quota.state"), "w").write(json.dumps(st))
    старый, M.STATE_PATH = M.STATE_PATH, os.path.join(d, "cu-quota.state")
    try:
        зовы, строки = [], []
        def notify_fn(msg, **kw):
            зовы.append(kw.get("key")); return "инбокс"
        M.run(fetch=_журнал, notify_fn=notify_fn, out=строки.append, now=NOW,
              history_path=os.path.join(d, "cu-quota.history.jsonl"))
        bal = [k for k in зовы if k and k.startswith("cu-quota:balance:%s" % HOST)]
        return bal, [s for s in строки if "водораздел" in s]
    finally:
        M.STATE_PATH = старый


def test_A_улучшение_не_звонит_и_говорит_почему():
    bal, вод = _прогон(3)          # за окно уже звучала ХУДШАЯ полоса
    assert bal == [], "УЛУЧШЕНИЕ ПРОШЛО В ИНБОКС — шов не подключён: %r" % (bal,)
    assert вод, "молчание без строки в логе неотличимо от сломанного сторожа"


def test_B_без_памяти_звонит():
    bal, _ = _прогон(None)
    assert bal, "потеря водораздела обязана ОТКРЫВАТЬ звонок, а не глушить"


def test_C_ухудшение_проходит_регресс_d75268e_не_вернулся():
    bal, _ = _прогон(0)            # за окно звучала МЯГКАЯ полоса
    assert bal, "УХУДШЕНИЕ ЗАГЛУШЕНО — вернулся дефект, который чинил d75268e"
