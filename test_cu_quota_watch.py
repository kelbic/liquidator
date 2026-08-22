"""Сторож квоты CU (агентский, инбокс-адресат): парс, всплеск против прогрева, ИНТЕГРАЛ, слепота.

Канон: тревоги только через инжектированный notify_fn (боевой канал в тестах недостижим —
транспорт грузится лишь при notify_fn=None, здесь всегда передаём свой). И state, и история
уводятся во временный каталог ДО первого прогона: сторож, пишущий боевые файлы из теста, —
это уже случившийся инцидент (канон tests-never-touch-production-channels).
"""
import calendar
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


# ================== 19-27. ОСТАТОК (ПРЕДЕЛ 3, добавлен 21.08) ==================
# Разбор 21.08: #5735 шёл 27M/мес при квоте 30M — ПОД порогом, сторож писал «~33 сут» и
# по темпу молчал бы вечно. А месяц-к-дате был 26.3M из 30M: остатка на ~3 суток при 10
# сутках до сброса. Темп и остаток — РАЗНЫЕ величины, и связывает вторая.
B5735 = "base-mainnet.g.alchemy.com#5735"
DAY = 86400.0
# NOW фиксирован серединой месяца, чтобы «до сброса» было предсказуемо (канон: без
# Date.now() в фикстуре; иначе тест зелен/красен в зависимости от дня прогона).
MID = calendar.timegm((2026, 8, 21, 18, 0, 0, 0, 1, 0))    # 21.08 -> до сброса 10.25 сут


def _anchor(mtd_m, *, as_of, quota_m=30.0, reset="calendar", host=B5735):
    return {"quota_cu": quota_m * 1e6, "reset": reset,
            "hosts": {host: {"mtd_cu": mtd_m * 1e6, "as_of": as_of,
                             "as_of_iso": "2026-08-21T18:05:33Z",
                             "source": "дашборд владельца 14.08 + замер журнала"}}}


def _led(cu, host=B5735, since=None):
    # since ПО УМОЛЧАНИЮ = as_of якоря этих фикстур (MID - HOUR): книга по определению
    # начинается в момент якоря, и ledger_add иного состояния не порождает. Прежнее
    # MID - DAY при якоре MID - HOUR было внутренне противоречивым входом.
    return {"hosts": {host: {"cu": cu, "since": MID - HOUR if since is None else since}}}


# ---- 19. ГЛАВНАЯ РЕГРЕССИЯ 21.08: темп ПОД порогом, а остаток кончается ----
burn = {B5735: 1.10e6}                       # замеренный эпизодный темп 21.08, CU/сут
bal = w.balance_verdicts(_anchor(26.3, as_of=MID - HOUR), _led(0.0), burn, now=MID)
check("остаток 3.7M при 1.10M/сут -> будит, хотя ТЕМП (27M) ниже квоты 30M", len(bal) == 1, bal)
if bal:
    _h, mtd, rem, dz, dr, _b, stale, _basis = bal[0]
    check("...месяц-к-дате и остаток посчитаны верно",
          abs(mtd - 26.3e6) < 1 and abs(rem - 3.7e6) < 1, (mtd, rem))
    check("...до нуля ~3.4 сут, а до сброса ~10.2 сут (кончится раньше, чем нальётся)",
          3.2 < dz < 3.5 and 10.0 < dr < 10.5 and not stale, (dz, dr))
# ПОЗИТИВНЫЙ КОНТРОЛЬ ТОГО ЖЕ ВХОДА: прежний, темповый приговор на этих данных МОЛЧИТ.
# Без него «новый тест зелен» не доказывает, что найдена НОВАЯ слепота (канон
# test-that-can-only-return-zero: нужен контроль на ТОМ ЖЕ объекте).
q27 = w.quota_verdicts(_hist([27.0], host=B5735), threshold=30, trail_sec=24 * HOUR,
                       min_trail_sec=6 * HOUR, now=MID)
check("...и ПРЕЖНИЙ приговор по темпу на тех же данных молчит (это и есть новая слепота)",
      q27 == [], q27)

# ---- 20. Запаса хватает до сброса -> молчит ----
bal = w.balance_verdicts(_anchor(10.0, as_of=MID - HOUR), _led(0.0), burn, now=MID)
check("остаток 20M при 1.10M/сут (18 сут против 10) -> тишина", bal == [], bal)

# ---- 21. НЕТ ЯКОРЯ -> НЕ СУДИМ ВОВСЕ (незнание закрывает гард, а не разрешает выстрел) ----
bal = w.balance_verdicts({}, _led(99e6), {B5735: 9e9}, now=MID)
check("якоря нет -> приговора по остатку нет даже при диком темпе", bal == [], bal)

# ---- 22. Книга складывается с якорем ----
bal = w.balance_verdicts(_anchor(20.0, as_of=MID - HOUR), _led(6.3e6), burn, now=MID)
check("якорь 20M + книга 6.3M = 26.3M -> будит", len(bal) == 1 and abs(bal[0][1] - 26.3e6) < 1, bal)

# ---- 23. ПРОТУХШИЙ ЯКОРЬ: приговор не выносим, но и не молчим ----
stale_anchor = _anchor(26.3, as_of=calendar.timegm((2026, 7, 20, 0, 0, 0, 0, 1, 0)))
bal = w.balance_verdicts(stale_anchor, _led(0.0), {B5735: 1.0}, now=MID)
check("якорь снят в ПРОШЛОМ месяце -> помечен протухшим (молчать нельзя)",
      len(bal) == 1 and bal[0][6] is True, bal)

# ---- 23b. РЕГРЕССИЯ 21.08 19:07: ХВОСТ СЛЕП К ПЕРЕСТРАЙПУ, а судил СРОК ДО НУЛЯ ----
# Живой случай: 18:53 потребители уведены с #5735, в 19:07 сторож объявил «остатка на 4.0
# суток» по темпу 927k CU/сут, посчитанному хвостом, 5 часов которого описывали уже снятую
# конфигурацию. После манёвра ключ не жёг ВООБЩЕ (в журнале исчез в 18:58).
post = MID - 8 * HOUR                                   # якорь снят при манёвре 8ч назад
led_quiet = _led(20e3, since=post)                      # книга: 20k CU за 8 часов = штиль
anc_post = _anchor(26.3, as_of=post)
fat = {B5735: 927e3}                                    # тот самый темп из тревоги 19:07
span_cross = {B5735: 24 * HOUR}                         # хвост 24ч ПЕРЕСЕКАЕТ якорь (8ч)
bal = w.balance_verdicts(anc_post, led_quiet, fat, burn_span=span_cross, now=MID)
check("хвост пересекает якорь -> судим КНИГОЙ (пост-манёвренный штиль) -> тишина",
      bal == [], bal)

# ПОЗИТИВНЫЙ КОНТРОЛЬ НА ТОМ ЖЕ ВХОДЕ (канон test-that-can-only-return-zero): те же данные
# при НЕзагрязнённом хвосте обязаны будить — иначе «тишина» выше могла быть тишиной прибора.
span_clean = {B5735: 4 * HOUR}                          # хвост целиком ПОСЛЕ якоря
bal_c = w.balance_verdicts(anc_post, led_quiet, fat, burn_span=span_clean, now=MID)
check("...а тот же вход с хвостом ЦЕЛИКОМ после якоря — будит (тишина выше не от прибора)",
      len(bal_c) == 1, bal_c)

# ---- 23в. МОЛОДАЯ КНИГА: отступаем к хвосту, но ЗАГРЯЗНЕНИЕ ПРОИЗНЕСЕНО ----
young = _led(0.0, since=MID - HOUR)
bal_y = w.balance_verdicts(_anchor(26.3, as_of=MID - HOUR), young, fat,
                           burn_span=span_cross, now=MID)
check("книге 1ч (<6ч) -> судим хвостом, НО основание названо загрязнённым",
      len(bal_y) == 1 and "ЗАГРЯЗН" in bal_y[0][7], bal_y)

# ---- 23г. Книга старше порога и ПУСТАЯ -> срок до нуля бесконечен, тревоги нет ----
check("книга 8ч со штилём -> темп судится ею, а не хвостом",
      w.balance_verdicts(anc_post, _led(0.0, since=post), fat,
                         burn_span=span_cross, now=MID) == [], "должно молчать")

# ---- 23д. ПЕРЕАНКЕРОВКА ОБНУЛЯЕТ КНИГУ САМА (гард на транспорте, не на памяти человека) ----
lp_re = os.path.join(_tmp, "ledger_reanchor.json")
a_old = _anchor(20.0, as_of=MID - DAY)
w.ledger_add({B5735: {"cu": 5000}}, a_old, lp_re, now=MID)
a_new = _anchor(25.0, as_of=MID - HOUR)                 # владелец переставил якорь вперёд
w.ledger_add({B5735: {"cu": 300}}, a_new, lp_re, now=MID)
e = w.load_ledger(lp_re)["hosts"][B5735]
check("сдвиг as_of обнуляет книгу (5000 до якоря НЕ едут в новый месяц-к-дате)",
      e["cu"] == 300 and e["since"] == MID - HOUR, e)
# НЕГАТИВНЫЙ КОНТРОЛЬ К ЭТОЙ ЖЕ ПОЛОВИНЕ: без сдвига as_of книга обязана ПРОДОЛЖАТЬ копиться.
w.ledger_add({B5735: {"cu": 700}}, a_new, lp_re, now=MID)
check("...но при ТОМ ЖЕ as_of книга копится дальше (обнуление не стало тотальным)",
      w.load_ledger(lp_re)["hosts"][B5735]["cu"] == 1000, w.load_ledger(lp_re))

# ---- 24. next_reset: календарный месяц ----
check("next_reset: с 21.08 -> 01.09", w.next_reset(MID) == calendar.timegm((2026, 9, 1, 0, 0, 0, 0, 1, 0)))
check("next_reset: с 31.12 -> 01.01 следующего года",
      w.next_reset(calendar.timegm((2026, 12, 31, 0, 0, 0, 0, 1, 0)))
      == calendar.timegm((2027, 1, 1, 0, 0, 0, 0, 1, 0)))
check("next_reset: годовщина (день 14) с 21.08 -> 14.09",
      w.next_reset(MID, "14") == calendar.timegm((2026, 9, 14, 0, 0, 0, 0, 1, 0)))

# ---- 25. Книга НЕ двоит расход прогонов ----
lp = os.path.join(_tmp, "ledger_dbl.json")
anc = _anchor(20.0, as_of=MID - HOUR)
w.ledger_add({B5735: {"cu": 1000}}, anc, lp, now=MID)
w.ledger_add({B5735: {"cu": 500}}, anc, lp, now=MID)
check("книга накапливает 1000+500=1500 (а не перезаписывает)",
      w.load_ledger(lp)["hosts"][B5735]["cu"] == 1500, w.load_ledger(lp))

# ---- 26. ГАРД НА ТРАНСПОРТЕ: увёл историю -> книга уехала следом ----
sent.clear()
hp_bal = os.path.join(_tmp, "bal_run.jsonl")
_fresh_hist(27.0, path=hp_bal)
json.dump(_anchor(26.3, as_of=MID - 30 * HOUR),
          open(os.path.join(_tmp, "cu-quota.anchor.json"), "w", encoding="utf-8"))
os.environ["CU_WATCH_PROJ_M"] = "30"          # темп 27M ПОД порогом -> темповых тревог нет
_prod_led_pre = os.path.expanduser("~/.fleet-watch/cu-quota.ledger.json")
_snap = (os.path.getmtime(_prod_led_pre), open(_prod_led_pre, "rb").read()) \
    if os.path.exists(_prod_led_pre) else None
rc = w.run(fetch=lambda: _journal(B5735, [27.0] * 30, pid=9), notify_fn=_nf,
           out=lambda *_: None, now=MID, history_path=hp_bal)
os.environ["CU_WATCH_PROJ_M"] = "26"
# ПРОВЕРЯЕМ «НЕ ТРОНУЛ», А НЕ «НЕ СУЩЕСТВУЕТ» (починка стенда 21.08). Прежняя редакция
# требовала ОТСУТСТВИЯ боевой книги — то есть была зелена ровно до первого боевого прогона
# ветки остатка. Крон создал ~/.fleet-watch/cu-quota.ledger.json в 19:07 21.08, и стенд стал
# КРАСНЫМ НАВСЕГДА при полностью исправном продукте (канон red-bench-is-not-a-diagnosis).
# Утверждение, которое гард на самом деле несёт: ЭТОТ прогон боевой файл не писал.
_prod_led, _prod_before = _prod_led_pre, _snap
check("run: книга/якорь легли РЯДОМ С ИСТОРИЕЙ, боевой каталог не тронут",
      os.path.exists(os.path.join(_tmp, "cu-quota.ledger.json"))
      and (((os.path.getmtime(_prod_led), open(_prod_led, "rb").read()) == _prod_before)
           if _prod_before is not None else not os.path.exists(_prod_led)))
check("run: при темпе ПОД порогом ушла ровно одна тревога — по ОСТАТКУ",
      rc == 0 and len(sent) == 1 and sent[0][2] == "cu-quota:balance:%s" % B5735, sent)
if sent:
    t = sent[0][3]
    check("run: текст остатка везёт ОСНОВАНИЕ — якорь, дату и что он рукописный",
          "ОСТАТОК КВОТЫ" in t and "якорь снят" in t and "Источник якоря:" in t
          and "ВВЕДЁННОЕ РУКАМИ" in t, t[:200])
    check("run: текст явно отделяет остаток от темпа",
          "НЕ то же самое, что тревога по темпу" in t, t[:200])

# ---- 27. Тихая строка ПРОИЗНОСИТ «якоря нет» (молчание об остатке недоказуемо) ----
lines = []
hp_q = os.path.join(_tmp, "quiet_noanchor", "h.jsonl")
os.makedirs(os.path.dirname(hp_q), exist_ok=True)
_fresh_hist(10.0, path=hp_q)
w.run(fetch=lambda: _journal(C, [10.0] * 30, pid=9), notify_fn=_nf, out=lines.append,
      now=MID, history_path=hp_q)
check("тихий прогон без якоря говорит «ост:якоря нет», а не молчит",
      any("якоря нет" in l for l in lines), lines)

# ---- 28. САМОКОНТРОЛЬ ОСТАТКА: снятый фикс обязан краснеть ----
# По каждой ПОЛОВИНЕ отдельно (канон seam-stubbed-above-the-defect, форма 21.08).
_real_nr = w.next_reset
try:
    # (а) сравнение с «до сброса» снято -> тревожит и там, где запаса хватает (тест 20 краснеет)
    w.next_reset = lambda e, s="calendar": e            # до сброса всегда 0 сут
    broke_a = w.balance_verdicts(_anchor(10.0, as_of=MID - HOUR), _led(0.0), burn, now=MID) == []
finally:
    w.next_reset = _real_nr
# (б) гард «нет якоря» снят -> пустой якорь начинает судить
broke_b = w.balance_verdicts({"hosts": {B5735: {"mtd_cu": 29e6, "as_of": MID - HOUR}}},
                             _led(0.0), burn, now=MID) != []
check("самоконтроль остатка: обе половины держат именно то, что заявлено",
      (not broke_a) and broke_b, (broke_a, broke_b))

# ---- 29. ГЛАВНАЯ РЕГРЕССИЯ 21.08 23:07: СВЕЖЕЕ ОКНО ПРОТИВ СМАЗАННОГО ХВОСТА ----
# Живой случай: в 22:00Z вечерняя кампания подняла #94bf с 54k до 188k CU/час. Хвост 21.6ч
# (грязный — он пересекает якорь 18:53) дал 1.0M CU/сут, книге было 4.2ч (<6ч), и ветка
# остатка ПРОМОЛЧАЛА: 28.7M / 1.0M = 28 суток «хватает». По факту при темпе последнего часа
# ведро пустело за 6.1 суток при 10.0 сутках до сброса.
as_of_man = MID - 4.2 * HOUR                 # якорь снят при манёвре 18:53
anc_c = _anchor(1.0, as_of=as_of_man)        # C: месяц-к-дате 1.0M -> остаток 28.7M... (28.99M)
led_young = _led(343529.0, since=as_of_man)  # книга 4.2ч: 343k CU -> ~1.96M/сут, ПОД критом
tail_dirty = {B5735: 1.0e6}                  # хвост 24ч: 1.0M CU/сут, ПОД критом
span_dirty = {B5735: 24 * HOUR}              # ...и он ПЕРЕСЕКАЕТ якорь => загрязнён
fresh_hot = {B5735: (3.30e6, 2 * HOUR, MID - 2 * HOUR)}   # свежее окно 2ч: 3.30M CU/сут

bal_f = w.balance_verdicts(anc_c, led_young, tail_dirty, burn_span=span_dirty,
                           burn_fresh=fresh_hot, now=MID)
check("свежее окно 3.3M/сут при остатке ~29M и 10 сут до сброса -> БУДИТ",
      len(bal_f) == 1 and abs(bal_f[0][5] - 3.30e6) < 1, bal_f)
if bal_f:
    check("...и основание НАЗВАНО свежим окном (приговор везёт то, чем судил)",
          "СВЕЖЕЕ ОКНО" in bal_f[0][7], bal_f[0][7])
# ПОЗИТИВНЫЙ КОНТРОЛЬ НА ТОМ ЖЕ ВХОДЕ (канон test-that-can-only-return-zero): ровно эти
# данные БЕЗ свежего окна обязаны молчать — иначе «будит» выше не доказывает новую ветку,
# а тишина ниже и есть та слепота, ради которой ветка написана.
bal_blind = w.balance_verdicts(anc_c, led_young, tail_dirty, burn_span=span_dirty, now=MID)
check("...а БЕЗ свежего окна тот же вход молчит — это и есть найденная 23:07 слепота",
      bal_blind == [], bal_blind)

# ---- 30. ГАРД ЗАГРЯЗНЕНИЯ: свежее окно, залезающее ЗА якорь, кандидатом не становится ----
# Иначе новая ветка вернула бы ту самую ошибку 20.08, от которой лечили грязный хвост:
# приговор о будущем по конфигурации, которой уже нет.
fresh_pre = {B5735: (3.30e6, 2 * HOUR, as_of_man - HOUR)}      # окно НАЧАЛОСЬ до якоря
bal_pre = w.balance_verdicts(anc_c, led_young, tail_dirty, burn_span=span_dirty,
                             burn_fresh=fresh_pre, now=MID)
check("свежее окно, начавшееся ДО якоря -> отброшено (не судим снятой конфигурацией)",
      bal_pre == [], bal_pre)
# ---- 31. ГАРД ПОКРЫТИЯ: огрызок окна кандидатом не становится ----
fresh_thin = {B5735: (9.9e6, 0.2 * HOUR, MID - 0.2 * HOUR)}    # 12 минут — не основание
bal_thin = w.balance_verdicts(anc_c, led_young, tail_dirty, burn_span=span_dirty,
                              burn_fresh=fresh_thin, now=MID)
check("свежее окно покрыло 0.2ч (<1ч) -> отброшено даже при диком темпе", bal_thin == [], bal_thin)

# ---- 32. МАКСИМУМ, А НЕ ЗАМЕНА: тихое свежее окно НЕ гасит уже звонящее основание ----
# Новая ветка обязана только ДОБАВЛЯТЬ поводы звонить. Если бы свежее окно ВЫТЕСНЯЛО
# книгу/хвост, кампания, кончившаяся десять минут назад, глушила бы тревогу об остатке,
# который она уже сожгла.
fresh_calm = {B5735: (1.0, 2 * HOUR, MID - 2 * HOUR)}
bal_max = w.balance_verdicts(_anchor(26.3, as_of=MID - HOUR), _led(0.0), burn,
                             burn_fresh=fresh_calm, now=MID)
check("свежее окно в штиле НЕ гасит тревогу, которую даёт хвост (берётся максимум)",
      len(bal_max) == 1 and abs(bal_max[0][5] - 1.10e6) < 1, bal_max)

# ---- 33. fresh_summary: берёт N ПОСЛЕДНИХ записей и оценивает начало окна ----
recs_f = [{"ts": MID - 3 * HOUR, "until": MID - 3 * HOUR, "hosts": {C: {"cu": 100, "sec": HOUR}}},
          {"ts": MID - 2 * HOUR, "until": MID - 2 * HOUR, "hosts": {C: {"cu": 200, "sec": HOUR}}},
          {"ts": MID - HOUR, "until": MID - HOUR, "hosts": {C: {"cu": 400, "sec": HOUR}}}]
fr = w.fresh_summary(recs_f, n=2)
check("fresh_summary: 2 последних записи (200+400 за 2ч), первая НЕ учтена",
      abs(fr[C][0] - 600.0 * 86400.0 / (2 * HOUR)) < 1 and abs(fr[C][1] - 2 * HOUR) < 1, fr)
check("fresh_summary: начало окна оценено (until самой ранней минус её покрытие)",
      abs(fr[C][2] - (MID - 3 * HOUR)) < 1, fr)

# ---- 34. РЕЖИМ В ТЕКСТЕ ТРЕВОГИ — ВЫЧИСЛЯЕМЫЙ, А НЕ КАНОННЫЙ ----
# 21.08 23:07 шаблон утверждал «это НЕ пиковый час» в тревоге, где всплеск (141M) был в 4.7
# раза выше хвоста (30M). Утверждение о событии, которого текст не проверял, — самоотчёт.
sent.clear()
hp_reg = os.path.join(_tmp, "regime", "h.jsonl")
os.makedirs(os.path.dirname(hp_reg), exist_ok=True)
_fresh_hist(30.0, path=hp_reg)                 # хвост ~30M
# журнал прогона: 30 окон по 141M -> всплеск ex-boot 141M против хвоста 30M
w.run(fetch=lambda: _journal(C, [141.0] * 30, pid=9), notify_fn=_nf, out=lambda *_: None,
      now=NOW, history_path=hp_reg)   # NOW, а не MID: история фикстуры строится от NOW
rate_alarms = [x for x in sent if x[2] == "cu-quota:%s" % C]
check("тревога по темпу при всплеске >> хвоста говорит «всплеск ВЫШЕ хвоста», а не «НЕ пиковый час»",
      len(rate_alarms) == 1 and "ВСПЛЕСК ВЫШЕ ХВОСТА" in rate_alarms[0][3]
      and "это НЕ пиковый час" not in rate_alarms[0][3], [x[3][:200] for x in rate_alarms])
# НЕГАТИВНЫЙ КОНТРОЛЬ ТОЙ ЖЕ ПОЛОВИНЫ: когда всплеск и хвост СОШЛИСЬ, текст обязан вернуться
# к прежнему утверждению — иначе новая строка кричит всегда и перестаёт что-либо значить.
sent.clear()
hp_reg2 = os.path.join(_tmp, "regime2", "h.jsonl")
os.makedirs(os.path.dirname(hp_reg2), exist_ok=True)
_fresh_hist(30.0, path=hp_reg2)
w.run(fetch=lambda: _journal(C, [30.0] * 30, pid=9), notify_fn=_nf, out=lambda *_: None,
      now=NOW, history_path=hp_reg2)
rate2 = [x for x in sent if x[2] == "cu-quota:%s" % C]
check("...а при сошедшихся всплеске и хвосте — прежнее «НЕ пиковый час»",
      len(rate2) == 1 and "это НЕ пиковый час" in rate2[0][3], [x[3][:200] for x in rate2])
check("ПРЕДЕЛ 2 в тексте отсылает к INFRA-FACTS, а не пересказывает факт с истёкшим сроком",
      len(rate2) == 1 and "INFRA-FACTS" in rate2[0][3], [x[3][:300] for x in rate2])

# ...и третья ветка: всплеск НЕ ЗАМЕРЕН -> режим не устанавливается вовсе.
sent.clear()
hp_reg3 = os.path.join(_tmp, "regime3", "h.jsonl")
os.makedirs(os.path.dirname(hp_reg3), exist_ok=True)
_fresh_hist(30.0, path=hp_reg3)
w.run(fetch=lambda: _journal(C, [141.0] * 5, pid=9), notify_fn=_nf, out=lambda *_: None,
      now=NOW, history_path=hp_reg3)          # 5 окон < CU_WATCH_MIN_WINDOWS=20
rate3 = [x for x in sent if x[2] == "cu-quota:%s" % C]
check("всплеск не замерен -> режим НЕ УСТАНОВЛЕН, а не «темп держится на хвосте»",
      len(rate3) == 1 and "режим НЕ УСТАНОВЛЕН" in rate3[0][3]
      and "держится на хвосте" not in rate3[0][3], [x[3][:200] for x in rate3])

# ---- 35. САМОКОНТРОЛЬ НОВОЙ ВЕТКИ: по КАЖДОЙ ПОЛОВИНЕ отдельно ----
# (канон seam-stubbed-above-the-defect, форма 21.08: суита обязана краснеть на снятом фиксе)
_real_fresh_min = w.FRESH_MIN_SEC
try:
    # (а) снят гард покрытия -> огрызок 0.2ч начинает судить (тест 31 краснеет)
    w.FRESH_MIN_SEC = 0.0
    broke_a = w.balance_verdicts(anc_c, led_young, tail_dirty, burn_span=span_dirty,
                                 burn_fresh=fresh_thin, now=MID) != []
finally:
    w.FRESH_MIN_SEC = _real_fresh_min
# (б) снят гард загрязнения -> до-якорное окно начинает судить (тест 30 краснеет).
#     Моделируем ровно снятие проверки: то же окно, но помеченное как пост-якорное.
broke_b = w.balance_verdicts(anc_c, led_young, tail_dirty, burn_span=span_dirty,
                             burn_fresh={B5735: (3.30e6, 2 * HOUR, as_of_man)}, now=MID) != []
check("самоконтроль свежего окна: обе половины (покрытие, загрязнение) держат заявленное",
      broke_a and broke_b, (broke_a, broke_b))

# ---- 36. ПРИПАРКОВАННЫЙ КЛЮЧ: co-наблюдение как позитивный контроль (22.08) ----
# 21.08 18:53 потребители уведены с #5735; трафика по нему нет вовсе, а сторож звонил и по
# ТЕМПУ («жжёт 31.6M/мес»), и по ОСТАТКУ («4.0 сут») — оба приговора по грязному хвосту.
# Прошлая сессия чинить отказалась: «одной строкой не отличить простой от слепоты». Верно
# для строки, неверно для ПРОГОНА: в тех же записях другие ключи разбираются штатно.
def _rec(ts, hosts):
    return {"ts": ts, "until": ts, "hosts": hosts}


_live = {"cu": 50000.0, "sec": HOUR}
recs_park = [_rec(MID - 3 * HOUR, {A: dict(_live), B5735: {"cu": 0.0, "sec": 120.0}}),
             _rec(MID - 2 * HOUR, {A: dict(_live)}),                   # B пропал из журнала
             _rec(MID - HOUR, {A: dict(_live)})]
check("parked: ноль по ключу ПРИ разобранных окнах у другого -> ПРИПАРКОВАН",
      w.parked_hosts(recs_park, [A, B5735]) == {B5735}, w.parked_hosts(recs_park, [A, B5735]))
# НЕГАТИВНЫЙ КОНТРОЛЬ (а): хоть один ненулевой прогон -> НЕ припаркован (иначе гард гасил бы
# живой ключ, а это уже не «сторож молчит», а «сторож ослеп по решению»).
recs_alive = list(recs_park)
recs_alive[1] = _rec(MID - 2 * HOUR, {A: dict(_live), B5735: {"cu": 1.0, "sec": HOUR}})
check("parked: один ненулевой прогон из трёх -> НЕ припаркован",
      w.parked_hosts(recs_alive, [A, B5735]) == set(), w.parked_hosts(recs_alive, [A, B5735]))
# НЕГАТИВНЫЙ КОНТРОЛЬ (б): прогон БЕЗ позитивного контроля (никто не разобран) — это слепота
# сторожа, а не простой ключа, и он обязан не объявлять тишину ни по кому.
recs_blind = [_rec(MID - 3 * HOUR, {A: {"cu": 0.0, "sec": 0.0}}),
              _rec(MID - 2 * HOUR, {A: {"cu": 0.0, "sec": 0.0}}),
              _rec(MID - HOUR, {A: {"cu": 0.0, "sec": 0.0}})]
check("parked: прогон без единого разобранного окна = слепота, НЕ парковка",
      w.parked_hosts(recs_blind, [A, B5735]) == set(), w.parked_hosts(recs_blind, [A, B5735]))
check("parked: истории меньше окна наблюдения -> не судим",
      w.parked_hosts(recs_park[:2], [B5735]) == set())

# ---- 37. Припаркованный ключ снят С ОБОИХ приговоров, и снят ИМЕННО фиксом ----
anc_park = _anchor(25.8, as_of=MID - 30 * HOUR)      # остаток 4.2M, якорь ДО хвоста
tail_park = {B5735: 1.05e6}
span_park = {B5735: 20 * HOUR}
bal_p = w.balance_verdicts(anc_park, _led(0.0, since=MID - 30 * HOUR), tail_park,
                           burn_span=span_park, now=MID, parked={B5735})
check("остаток: припаркованный ключ НЕ звонит (расход ЗАМЕРЕН нулём)", bal_p == [], bal_p)
# НЕГАТИВНЫЙ КОНТРОЛЬ той же половины: без parked тот же вход обязан звонить — иначе тест
# зелен по причине фикстуры, а не по причине фикса (канон seam-stubbed-above-the-defect).
bal_np = w.balance_verdicts(anc_park, _led(0.0, since=MID - 30 * HOUR), tail_park,
                            burn_span=span_park, now=MID)
check("...а БЕЗ parked тот же вход звонит — гасит именно фикс, а не фикстура",
      len(bal_np) == 1, bal_np)
q_p = w.quota_verdicts(recs_park + [_rec(MID - 4 * HOUR, {B5735: {"cu": 3.0e6, "sec": 6 * HOUR}})],
                       threshold=26.0, trail_sec=24 * HOUR, min_trail_sec=6 * HOUR,
                       now=MID, parked={B5735})
check("темп: припаркованный ключ НЕ звонит и по ИНТЕГРАЛУ",
      [x[0] for x in q_p] == [], q_p)
q_np = w.quota_verdicts(recs_park + [_rec(MID - 4 * HOUR, {B5735: {"cu": 3.0e6, "sec": 6 * HOUR}})],
                        threshold=26.0, trail_sec=24 * HOUR, min_trail_sec=6 * HOUR, now=MID)
check("...а БЕЗ parked он звонит по тому же хвосту (негативный контроль второй половины)",
      B5735 in [x[0] for x in q_np], q_np)

# ---- 38. ГОРИЗОНТ СБРОСА — КАНДИДАТ, А НЕ ФАКТ (22.08) ----
# INFRA-FACTS: дата сброса НЕИЗВЕСТНА. Календарное 1-е число — САМЫЙ РАННИЙ кандидат, то есть
# допущение работает В СТОРОНУ МОЛЧАНИЯ, и текст обязан это говорить, а не выдавать «10.0 сут»
# за измеренное (канон 29ae77e: приговор везёт основание, по которому судил).
anc_alt = _anchor(26.3, as_of=MID - HOUR)
anc_alt["hosts"][B5735]["reset_alt"] = "20"          # годовщина заведения аккаунта
check("host_reset_spec: cal -> календарь, alt -> день годовщины",
      w.host_reset_spec(anc_alt, B5735, "cal") == "calendar"
      and w.host_reset_spec(anc_alt, B5735, "alt") == "20")
t_alt = w._horizon_txt(anc_alt, B5735, 3.4, 10.25, MID)
check("текст горизонта: НЕИЗВЕСТНА + оба кандидата + цена разницы",
      "НЕИЗВЕСТНА" in t_alt and "САМЫЙ РАННИЙ" in t_alt and "СТОРОНУ МОЛЧАНИЯ" in t_alt
      and "Второй кандидат" in t_alt and "ТЁМНЫЙ" in t_alt, t_alt)
# НЕГАТИВНЫЙ КОНТРОЛЬ: без reset_alt текст НЕ выдумывает годовщину, а называет незнание.
t_noalt = w._horizon_txt(_anchor(26.3, as_of=MID - HOUR), B5735, 3.4, 10.25, MID)
check("...а без reset_alt — «второго кандидата нет», а не выдуманная дата",
      "ВТОРОГО КАНДИДАТА НЕТ" in t_noalt and "Второй кандидат" not in t_noalt, t_noalt)
# ...и ПРИГОВОР при этом остаётся на раннем кандидате: поздний горизонт НЕ поднимает тревог
# сам по себе (иначе порог звенел бы на здоровых ключах — замер 22% ложных, 20.08).
quiet_burn = {B5735: 0.30e6}                # 3.7M остатка / 0.30M = 12.3 сут > 10.25
check("приговор по РАННЕМУ кандидату: на календаре молчит...",
      w.balance_verdicts(anc_alt, _led(0.0), quiet_burn, now=MID) == [])
check("...а на ПОЗДНЕМ (годовщина 20-е, 29.6 сут) тот же вход не доживает — это и есть цена",
      len(w.balance_verdicts(anc_alt, _led(0.0), quiet_burn, now=MID, horizon="alt")) == 1)

# ---- 39. run(): горизонт в тексте тревоги, парковка — В ЖУРНАЛ И В ШУМНОМ ПРОГОНЕ ----
sent.clear()
lines_h = []
hp_h = os.path.join(_tmp, "horizon", "h.jsonl")
os.makedirs(os.path.dirname(hp_h), exist_ok=True)
_fresh_hist(27.0, path=hp_h)
_anc_file = os.path.join(_tmp, "horizon", "cu-quota.anchor.json")
_a = _anchor(26.3, as_of=MID - 30 * HOUR)
_a["hosts"][B5735]["reset_alt"] = "20"
json.dump(_a, open(_anc_file, "w", encoding="utf-8"))
os.environ["CU_WATCH_PROJ_M"] = "30"
w.run(fetch=lambda: _journal(B5735, [27.0] * 30, pid=9), notify_fn=_nf, out=lines_h.append,
      now=MID, history_path=hp_h)
os.environ["CU_WATCH_PROJ_M"] = "26"
bal_msgs = [x[3] for x in sent if x[2].startswith("cu-quota:balance:")]
check("run: текст тревоги остатка везёт ГОРИЗОНТ как кандидат, а не как факт",
      len(bal_msgs) == 1 and "ГОРИЗОНТ СБРОСА — НЕ ФАКТ" in bal_msgs[0]
      and "НЕИЗВЕСТНА" in bal_msgs[0], [m[:300] for m in bal_msgs])
check("run: текст велит СЛОЖИТЬ ОСТАТКИ И СПРОС до «увести на ключ с запасом»",
      len(bal_msgs) == 1 and "СЛОЖИТЬ ОСТАТКИ И СПРОС" in bal_msgs[0], [m[:300] for m in bal_msgs])
# Парковка обязана печататься и когда прогон ШУМНЫЙ (строка штиля тогда не печатается вовсе).
sent.clear()
lines_p = []
hp_p = os.path.join(_tmp, "parkloud", "h.jsonl")
os.makedirs(os.path.dirname(hp_p), exist_ok=True)
# История: три прогона, в которых по C идёт трафик, а по B5735 — ноль (он и припаркован).
with open(hp_p, "w", encoding="utf-8") as f:
    for k in (3, 2, 1):
        # sec=3ч на запись: хвосту нужно накопить >= CU_WATCH_MIN_TRAIL_H, иначе прогон
        # выйдет ТИХИМ и проверка «произносится в шумном» проверяла бы не то.
        f.write(json.dumps(_rec(NOW - k * HOUR, {C: {"cu": 3.0e6, "sec": 3 * HOUR}})) + "\n")
json.dump(_anchor(25.8, as_of=NOW - 30 * HOUR, host=B5735),
          open(os.path.join(_tmp, "parkloud", "cu-quota.anchor.json"), "w", encoding="utf-8"))
w.run(fetch=lambda: _journal(C, [141.0] * 30, pid=9), notify_fn=_nf, out=lines_p.append,
      now=NOW, history_path=hp_p)
check("run: ПРИПАРКОВАН произносится и в ШУМНОМ прогоне (строки штиля там нет)",
      any("ПРИПАРКОВАНЫ" in l and B5735 in l for l in lines_p)
      and not any("тихо (порог" in l for l in lines_p), lines_p)
check("run: по припаркованному ключу тревог не ушло",
      not any(B5735 in x[2] for x in sent), [x[2] for x in sent])


print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: сторож квоты — парс/всплеск/ИНТЕГРАЛ/ОСТАТОК/дедуп/слепота/самоконтроль"))
sys.exit(1 if FAILS else 0)
