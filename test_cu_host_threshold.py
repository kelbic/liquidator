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

print("\n" + ("OK: порог темпа — по ведру хоста" if not FAILS else "ПРОВАЛЫ: %s" % FAILS))
sys.exit(1 if FAILS else 0)
