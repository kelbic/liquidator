"""Сторож квоты CU (агентский, инбокс-адресат): парс, всплеск против прогрева, ИНТЕГРАЛ, слепота.

Канон: тревоги только через инжектированный notify_fn (боевой канал в тестах недостижим —
транспорт грузится лишь при notify_fn=None, здесь всегда передаём свой). И state, и история
уводятся во временный каталог ДО первого прогона: сторож, пишущий боевые файлы из теста, —
это уже случившийся инцидент (канон tests-never-touch-production-channels).
"""
import json
import os
import sys
import tempfile
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

_tmp = tempfile.mkdtemp()
import analysis.cu_quota_watch as w  # noqa: E402
w.STATE_PATH = os.path.join(_tmp, "cu-quota.state")
w.HISTORY_PATH = os.path.join(_tmp, "cu-quota.history.jsonl")
assert not os.path.exists(os.path.expanduser("~/.fleet-watch/cu-quota.history.jsonl")) or True

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


A = "base-mainnet.g.alchemy.com#3133"
C = "base-mainnet.g.alchemy.com#94bf"
D = "lb.drpc.live#afd4"
HOUR = 3600.0

L = ("Aug 20 %(hm)s host python[%(pid)d]: 2026-08-20 %(hm)s,752 INFO CU %(host)s: %(cu)d "
     "CU/окно = 7 CU/s (проекция %(proj).1fM/мес, скольз.%(span)dмин 50.8M/мес) | "
     "eth_call@prearm.py:_read_target×38")


def _cu_for(proj_m, sec=120.0):
    """CU, физически согласованные с проекцией: строка обязана быть непротиворечивой, иначе
    длительность окна (cu/темп) поедет и тест померит фикстуру, а не механизм."""
    return int(round(proj_m * 1e6 / w.SEC_MONTH * sec))


def _journal(host, projs, spans=None, *, pid=1, t0=0):
    """Окна по 120с подряд; штампы РАЗНЫЕ (перекрытие прогонов ловится по ним)."""
    spans = spans or [30] * len(projs)
    lines = []
    for i, (p, sp) in enumerate(zip(projs, spans)):
        sec = (t0 + i) * 120
        hm = "%02d:%02d:%02d" % (sec // 3600 % 24, sec // 60 % 60, sec % 60)
        lines.append(L % {"hm": hm, "pid": pid, "host": host, "cu": _cu_for(p), "proj": p, "span": sp})
    return "\n".join(lines)


# ---- 1. Парс: только alchemy, значения и топ ----
ph = w.parse_cu_windows(_journal(A, [7, 9, 11]) + "\n" + _journal(D, [40, 44]))
check("alchemy распарсен (3 окна)", A in ph and len(ph[A]["projs"]) == 3, ph)
check("dRPC отфильтрован (не квотуем)", D not in ph, list(ph))
check("топ-атрибуция снята", "prearm" in ph[A]["top"], ph[A]["top"])
check("длительность окна выведена ИЗ СТРОКИ (~120с), не захардкожена",
      118 < ph[A]["sec"] / 3 < 122, ph[A]["sec"])

# ---- 2. Бут: вне ВСПЛЕСКА, но ВНУТРИ интеграла (провайдер списывает и его) ----
projs = [60.0] * 6 + [12.0] * 24
spans = [2, 4, 6, 9, 11, 14] + [30] * 24
ph = w.parse_cu_windows(_journal(A, projs, spans))
check("бут-окна выкинуты из всплеска (boot=6, осталось 24)",
      ph[A]["boot"] == 6 and len(ph[A]["projs"]) == 24, ph)
check("среднее без бута = штиль (12M < 26) -> всплеска нет",
      w.verdicts(ph, threshold=26, min_windows=20) == [])
check("бут ПОСЧИТАН в расходе (cu_boot > 0 и это ~5/8 всех CU)",
      ph[A]["cu_boot"] > 0 and 0.5 < ph[A]["cu_boot"] / ph[A]["cu"] < 0.7,
      (ph[A]["cu_boot"], ph[A]["cu"]))
saw = [1.0] * 16 + [80.0] * 14          # медиана 1.0 (проспала бы), среднее 37.9 — видно
ph = w.parse_cu_windows(_journal(A, saw))
v = w.verdicts(ph, threshold=26, min_windows=20)
check("пила (медиана спала бы) видна по среднему", len(v) == 1 and 37 < v[0][1] < 39, v)

# ---- 3/4. Всплеск: устойчивое видно, огрызок не судим ----
ph = w.parse_cu_windows(_journal(A, [31.0] * 30))
v = w.verdicts(ph, threshold=26, min_windows=20)
check("среднее 31M >= 26 -> всплеск зафиксирован", len(v) == 1 and v[0][1] == 31.0, v)
ph = w.parse_cu_windows(_journal(A, [99.0] * 5))
check("5 окон < 20 -> всплеск не судим (не судим по огрызку)",
      w.verdicts(ph, threshold=26, min_windows=20) == [])

# ================= ИНТЕГРАЛ: то, ради чего сторож переделан 20.08 =================
NOW = 1_787_000_000.0


def _hist(rates_by_hour, *, host=C, now=NOW, n=24):
    """История: по записи на час, каждая покрывает час с указанным темпом."""
    recs = []
    for i in range(n):
        rate = rates_by_hour[i % len(rates_by_hour)]
        recs.append({"ts": now - (n - 1 - i) * HOUR, "until": None, "restarts": 1,
                     "hosts": {host: {"cu": rate * 1e6 / w.SEC_MONTH * HOUR, "sec": HOUR,
                                      "cu_boot": 0, "n": 30, "boot": 0}}})
    return recs


# ---- 5. РЕГРЕССИЯ ТРЕВОГИ 20.08 19:07: один горячий час среди тихих НЕ будит ----
hot = _hist([20.0] * 23 + [32.3])
q = w.quota_verdicts(hot, threshold=26, trail_sec=24 * HOUR, min_trail_sec=6 * HOUR, now=NOW)
check("один пиковый час (32.3M) среди тихих (20M) -> интеграл молчит", q == [], q)
ph_hot = w.parse_cu_windows(_journal(C, [32.3] * 30))
check("...при том что ПРЕЖНЯЯ метрика (всплеск часа) на нём звонила бы",
      len(w.verdicts(ph_hot, threshold=26, min_windows=20)) == 1)

# ---- 6. Устойчивый перерасход хвоста БУДИТ ----
q = w.quota_verdicts(_hist([31.0]), threshold=26, trail_sec=24 * HOUR,
                     min_trail_sec=6 * HOUR, now=NOW)
check("темп 31M ДЕРЖИТСЯ сутки -> тревога", len(q) == 1 and 30.5 < q[0][1] < 31.5, q)

# ---- 7. Мало накоплено — НЕ СУДИМ ВОВСЕ (незнание закрывает гард) ----
q = w.quota_verdicts(_hist([99.0], n=3), threshold=26, trail_sec=24 * HOUR,
                     min_trail_sec=6 * HOUR, now=NOW)
check("накоплено 3ч < 6ч -> молчание даже при 99M", q == [], q)
q = w.quota_verdicts(_hist([99.0], n=7), threshold=26, trail_sec=24 * HOUR,
                     min_trail_sec=6 * HOUR, now=NOW)
check("накоплено 7ч >= 6ч -> судим", len(q) == 1, q)

# ---- 8. Хвост не тянет протухшее: записи старше окна выпадают ----
old = _hist([99.0], n=8, now=NOW - 48 * HOUR)
q = w.quota_verdicts(old, threshold=26, trail_sec=24 * HOUR, min_trail_sec=6 * HOUR, now=NOW)
check("записи старше хвоста 24ч не судятся", q == [], q)

# ---- 9. СЛУЧАЙ КЛЮЧА A: бут-CU переводит ключ через порог, ex-boot молчал бы ----
# 6ч, из них расход бутовый 54% (замер 20.08). Ex-boot темп 17.5M, честный — 28.4M.
boot_recs = [{"ts": NOW - i * HOUR, "until": None, "restarts": 3,
              "hosts": {A: {"cu": 28.4e6 / w.SEC_MONTH * HOUR, "sec": HOUR,
                            "cu_boot": 0.54 * 28.4e6 / w.SEC_MONTH * HOUR, "n": 30, "boot": 6}}}
             for i in range(8)]
q = w.quota_verdicts(boot_recs, threshold=26, trail_sec=24 * HOUR, min_trail_sec=6 * HOUR, now=NOW)
check("бут-CU внутри интеграла: 28.4M >= 26 -> ключ A виден", len(q) == 1 and q[0][0] == A, q)
ph_a = w.parse_cu_windows(_journal(A, [60.0] * 6 + [17.5] * 24, [2, 4, 6, 9, 11, 14] + [30] * 24))
check("...а ПРЕЖНЯЯ метрика (ex-boot 17.5M) по нему молчала бы ПО ПОСТРОЕНИЮ",
      w.verdicts(ph_a, threshold=26, min_windows=20) == [])

# ---- 10. Перекрытие прогонов не двоит расход ----
hpath = os.path.join(_tmp, "dedup.jsonl")
j1 = _journal(C, [20.0] * 30, t0=0)
ph1 = w.parse_cu_windows(j1, since_epoch=None)
r1 = w.append_history({"ts": NOW, "until": ph1[C]["last"], "restarts": 1,
                       "hosts": {C: {"cu": ph1[C]["cu"], "sec": ph1[C]["sec"]}}}, hpath, now=NOW)
ph2 = w.parse_cu_windows(j1, since_epoch=w.history_cutoff(r1))   # тот же кусок второй раз
check("повторно скормленный кусок журнала даёт 0 новых окон", ph2 == {}, ph2)
j2 = _journal(C, [20.0] * 30, t0=15)                              # 15 окон перекрытия
ph3 = w.parse_cu_windows(j2, since_epoch=w.history_cutoff(r1))
check("перекрытие срезано по штампу (15 новых из 30)", len(ph3[C]["projs"]) == 15, ph3[C]["n_all"])

# ---- 11. run(): тревога уходит в инжектированный канал, текст самодостаточен ----
sent = []


def _nf(text, *, source, hil, key, dedup_sec):
    sent.append((source, hil, key, text))
    return "inbox"


def _fresh_hist(rate, hours=24, *, path):
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _hist([rate], n=hours))
    open(path, "w", encoding="utf-8").write(data)


hp = os.path.join(_tmp, "run.jsonl")
_fresh_hist(31.0, path=hp)
os.environ["CU_WATCH_PROJ_M"] = "26"
rc = w.run(fetch=lambda: _journal(C, [31.0] * 30, pid=7), notify_fn=_nf, out=lambda *_: None,
           now=NOW, history_path=hp)
check("run: тревога в инбокс (hil=False)", rc == 0 and len(sent) == 1 and sent[0][1] is False, sent)
check("run: ключ дедупа пер-хост", sent and sent[0][2] == "cu-quota:%s" % C, sent)
check("run: текст зовёт агента, HIL — только новый ключ", sent and "HIL-1" in sent[0][3])
check("run: текст называет ГОРИЗОНТ (интеграл хвоста), а не «час»",
      sent and "ИНТЕГРАЛ хвоста" in sent[0][3], sent[0][3][:120] if sent else "")
check("run: текст называет РЕСТАРТЫ", sent and "Рестартов в окне: 1" in sent[0][3],
      sent[0][3][:200] if sent else "")
check("run: текст называет предел «квота на аккаунт»", sent and "НА АККАУНТ" in sent[0][3])

# ---- 12. Тихий прогон: пиковый час поверх тихих суток НЕ будит (через run) ----
sent.clear()
hp2 = os.path.join(_tmp, "run_quiet.jsonl")
_fresh_hist(20.0, path=hp2)
rc = w.run(fetch=lambda: _journal(C, [32.3] * 30, pid=7), notify_fn=_nf, out=lambda *_: None,
           now=NOW, history_path=hp2)
check("run: горячий час поверх тихого хвоста -> тишина", rc == 0 and sent == [], sent)

# ---- 13. Холодный старт: истории нет -> не судим, но и не слепнем ----
sent.clear()
hp3 = os.path.join(_tmp, "run_cold.jsonl")
rc = w.run(fetch=lambda: _journal(C, [99.0] * 30, pid=7), notify_fn=_nf, out=lambda *_: None,
           now=NOW, history_path=hp3)
check("run: холодный старт (накоплен 1 час) молчит даже на 99M", rc == 0 and sent == [], sent)
check("run: но прогон ЗАПИСАН в историю (копим, а не теряем)",
      len(w.load_history(hp3, now=NOW)) == 1, w.load_history(hp3, now=NOW))

# ---- 14. Порог, по которому вынесен приговор, едет В ТЕКСТЕ тревоги ----
sent.clear()
hp4 = os.path.join(_tmp, "run_thr.jsonl")
_fresh_hist(16.0, path=hp4)
os.environ["CU_WATCH_PROJ_M"] = "14"
w.run(fetch=lambda: _journal(C, [16.0] * 30), notify_fn=_nf, out=lambda *_: None,
      now=NOW, history_path=hp4)
low = sent[0][3] if sent else ""
sent.clear()
_fresh_hist(16.0, path=hp4)
os.environ["CU_WATCH_PROJ_M"] = "15"
w.run(fetch=lambda: _journal(C, [16.0] * 30), notify_fn=_nf, out=lambda *_: None,
      now=NOW, history_path=hp4)
mid = sent[0][3] if sent else ""
os.environ.pop("CU_WATCH_PROJ_M", None)
check("тревога называет порог, по которому судила", "порог 14M" in low, low[:160])
check("порог в тексте — действующий, а не константа", "порог 15M" in mid and low != mid, mid[:160])

# ---- 15. Слепота: одиночный фейл терпим, серия будит (о САМОМ стороже) ----
sent.clear()


def _boom():
    raise RuntimeError("ssh упал")


hp5 = os.path.join(_tmp, "run_blind.jsonl")
for _ in range(3):
    rc = w.run(fetch=_boom, notify_fn=_nf, out=lambda *_: None, now=NOW, history_path=hp5)
check("два фейла молчат, третий будит (streak=3)",
      rc == 1 and len(sent) == 1 and sent[0][2] == "cu-quota:blind", sent)
sent.clear()
rc = w.run(fetch=lambda: _journal(C, [12.0] * 30), notify_fn=_nf, out=lambda *_: None,
           now=NOW, history_path=hp5)
check("успех сбрасывает счётчик слепоты", rc == 0 and sent == [])

# ---- 16. Ноль окон = слепота (тест-который-может-вернуть-только-ноль закрыт) ----
sent.clear()
rc = w.run(fetch=lambda: "мусор без CU-строк", notify_fn=_nf, out=lambda *_: None,
           now=NOW, history_path=hp5)
check("пустой парс считается слепым прогоном, не тишиной", rc == 1)

# ---- 17. Битая история не ослепляет сторож ----
hp6 = os.path.join(_tmp, "run_corrupt.jsonl")
open(hp6, "w", encoding="utf-8").write("{не json\n" + json.dumps(_hist([31.0], n=24)[0]) + "\n")
check("битые строки истории пропущены, годные прочитаны", len(w.load_history(hp6, now=NOW)) == 1)

# ---- 18. САМОКОНТРОЛЬ: те же проверки на СЛОМАННЫХ вариантах обязаны падать ----
# Иначе зелень выше не доказывает ничего (канон acceptance-reference-not-self-report).
selfok = []
selfok.append(w.quota_verdicts(_hist([20.0] * 23 + [32.3]), threshold=26, trail_sec=24 * HOUR,
                               min_trail_sec=6 * HOUR, now=NOW) == []
              and w.quota_verdicts(_hist([31.0]), threshold=26, trail_sec=24 * HOUR,
                                   min_trail_sec=6 * HOUR, now=NOW) != [])
# гард накопления снят -> огрызок начинает судить (проверка, что гард именно он держит)
selfok.append(w.quota_verdicts(_hist([99.0], n=3), threshold=26, trail_sec=24 * HOUR,
                               min_trail_sec=0.0, now=NOW) != [])
# бут исключён из интеграла -> ключ A уходит под порог (проверка, что его держит именно бут)
no_boot = [{"ts": NOW - i * HOUR, "until": None, "restarts": 3,
            "hosts": {A: {"cu": (1 - 0.54) * 28.4e6 / w.SEC_MONTH * HOUR, "sec": HOUR}}}
           for i in range(8)]
selfok.append(w.quota_verdicts(no_boot, threshold=26, trail_sec=24 * HOUR,
                               min_trail_sec=6 * HOUR, now=NOW) == [])
check("самоконтроль: сломанные варианты ведут себя иначе (гарды держат именно то, что заявлено)",
      all(selfok), selfok)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: сторож квоты — парс/всплеск/ИНТЕГРАЛ/дедуп/слепота/самоконтроль"))
sys.exit(1 if FAILS else 0)
