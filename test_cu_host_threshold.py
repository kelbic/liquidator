"""ПОРОГ ТЕМПА — ПО ВЕДРУ ХОСТА (27.08, крон-триаж ложной тревоги cu-quota #3133 11:07:03Z).

Дефект: плоские 26M/мес — это ДОЛЯ free-ведра 30M, а не физическая величина. Владелец перевёл
ключ A на PAYG в 11:00Z (ведро 1e10), якорь обновлён в 11:09Z — а ветка темпа продолжала бы
звонить по A каждый час на 127M/мес против порога от ведра, которого у ключа нет.

Суита держит ОБЕ половины отдельно (канон seam-stubbed-above-the-defect): негативный контроль
— со снятым threshold_for приговор ОБЯЗАН вернуться; позитивный — ключ с настоящим ведром 30M
из-под порога НЕ уходит (иначе фикс менял бы ложный звон на слепоту по всему флоту).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis.cu_quota_watch import quota_verdicts, host_threshold

FAILS = []
def ok(cond, what):
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)

PAYG = "h#payg"
FREE = "h#free"
ANCHOR = {"quota_cu": 30_000_000,
          "hosts": {PAYG: {"quota_cu": 10_000_000_000}, FREE: {"mtd_cu": 1}}}
NOW = 1_000_000.0
# ~127M/мес у PAYG и ~98M/мес у free — оба выше плоских 26M.
SEC = 24 * 3600.0
recs = [{"ts": NOW - 60, "hosts": {PAYG: {"cu": 127e6 / (30 * 24 * 3600) * SEC,
                                          "sec": SEC},
                                   FREE: {"cu": 98e6 / (30 * 24 * 3600) * SEC, "sec": SEC}}}]
tf = lambda h: host_threshold(ANCHOR, h, 26.0)

hits = {h for h, *_ in quota_verdicts(recs, threshold=26.0, trail_sec=48 * 3600,
                                      min_trail_sec=6 * 3600, now=NOW, threshold_for=tf)}
ok(PAYG not in hits, "PAYG-ключ (ведро 1e10) из-под порога темпа уходит")
ok(FREE in hits, "ПОЗИТИВНЫЙ КОНТРОЛЬ: ключ с ведром 30M под порогом остаётся")

back = {h for h, *_ in quota_verdicts(recs, threshold=26.0, trail_sec=48 * 3600,
                                      min_trail_sec=6 * 3600, now=NOW)}
ok(PAYG in back and FREE in back,
   "НЕГАТИВНЫЙ КОНТРОЛЬ: со снятым threshold_for оба приговора возвращаются")

ok(abs(host_threshold(ANCHOR, PAYG, 26.0) - 26.0 * (10_000_000_000 / 30_000_000)) < 1e-6,
   "порог PAYG = базовый * (ведро/база)")
ok(host_threshold(ANCHOR, "h#нет-в-якоре", 26.0) == 26.0,
   "хост без якоря судится ОБЩИМ ведром — незнание не расширяет порог")

# ---- run(): ВЕТКА ЖУРНАЛА ОБЯЗАНА ИСПОЛНИТЬСЯ, а не «читаться чисто» ----
# Канон seam-stubbed-above-the-defect: чистые функции выше проверены, но строка журнала про
# «предел — деньги» живёт ВНУТРИ run(), и её первое исполнение пришлось бы на крон без надзора.
# Поэтому здесь гоняется настоящий run() — на ИЗОЛИРОВАННОЙ истории во временном каталоге
# (_beside_history уводит туда же якорь/книгу/стейт: боевой стейт стендом не трогается,
# канон 27.08 «у сторожа два выхода — тревога и ЗАЩЁЛКА») и с ОБЯЗАТЕЛЬНЫМ notify_fn.
import json, tempfile
from analysis import cu_quota_watch as _W

_NOW = 1_787_000_000.0
_A, _C = "base-mainnet.g.alchemy.com#3133", "base-mainnet.g.alchemy.com#94bf"
_L = ("Aug 20 %(hm)s host python[%(pid)d]: 2026-08-20 %(hm)s,752 INFO CU %(host)s: %(cu)d "
      "CU/окно = 7 CU/s (проекция %(proj).1fM/мес, скольз.%(span)dмин 50.8M/мес) | %(top)s")
_TOP = "eth_call@prearm.py:_read_target×38"

def _cu(p, sec):
    return p * 1e6 / _W.SEC_MONTH * sec

def _journal(host, proj, n=20):
    out = []
    for i in range(n):
        s = i * 120
        out.append(_L % {"hm": "%02d:%02d:%02d" % (s // 3600 % 24, s // 60 % 60, s % 60),
                         "pid": 7, "host": host, "cu": int(round(_cu(proj, 120))),
                         "proj": proj, "span": 30, "top": _TOP})
    return "\n".join(out)

with tempfile.TemporaryDirectory() as _d:
    _h = os.path.join(_d, "cu-quota.history.jsonl")
    with open(_h, "w") as _f:
        for i in range(25):
            _ts = _NOW - (24 - i) * 3600 - 60
            _f.write(json.dumps({"ts": _ts, "until": _ts - 10, "restarts": 1, "hosts": {
                _A: {"cu": _cu(127, 3600), "sec": 3600, "cu_boot": 0, "n": 29, "boot": 0},
                _C: {"cu": _cu(98, 3600), "sec": 3600, "cu_boot": 0, "n": 29, "boot": 0}}}) + "\n")
    with open(os.path.join(_d, "cu-quota.anchor.json"), "w") as _f:
        json.dump({"quota_cu": 30_000_000, "reset": "calendar", "hosts": {
            _A: {"mtd_cu": 1436, "as_of": _NOW - 7200, "quota_cu": 10_000_000_000},
            _C: {"mtd_cu": 5_817_952, "as_of": _NOW - 7200}}}, _f)

    _notes, _lines = [], []
    _rc = _W.run(fetch=lambda: _journal(_A, 127.0) + "\n" + _journal(_C, 98.0),
                 notify_fn=lambda *a, **k: _notes.append((a, k)),
                 out=lambda *a: _lines.append(" ".join(str(x) for x in a)),
                 now=_NOW, history_path=_h)

_keys = [k.get("key") for _a, k in _notes]
ok(_rc == 0, "run() отработал без исключения (ветка исполнена, а не прочитана)")
ok(any("ПРЕДЕЛ ЭТОГО КЛЮЧА — ДЕНЬГИ" in l and _A in l for l in _lines),
   "PAYG-хост печатает строку журнала: молчание — не слепота")
ok("cu-quota:" + _A not in _keys, "тревоги ТЕМПА по PAYG-хосту нет")
ok("cu-quota:" + _C in _keys, "ПОЗИТИВНЫЙ КОНТРОЛЬ run(): по ключу с ведром 30M тревога есть")

print("\n" + ("OK: порог темпа — по ведру хоста (вкл. run())" if not FAILS else "ПРОВАЛЫ: %s" % FAILS))
sys.exit(1 if FAILS else 0)
