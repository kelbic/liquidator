"""Сторож квоты CU (агентский, инбокс-адресат): парс, медиана против прогрева, слепота.

Канон: тревоги только через инжектированный notify_fn (боевой канал в тестах недостижим —
транспорт грузится лишь при notify_fn=None, здесь всегда передаём свой)."""
import os
import sys
import tempfile
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

# state-файл — во временный каталог ДО импорта модуля
_tmp = tempfile.mkdtemp()
import analysis.cu_quota_watch as w  # noqa: E402
w.STATE_PATH = os.path.join(_tmp, "cu-quota.state")

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


L = ("Aug 20 16:03:51 host python[1]: 2026-08-20 16:03:51,752 INFO CU %s: 866 CU/окно = "
     "7 CU/s (проекция %sM/мес, скольз.%dмин 50.8M/мес) | eth_call@prearm.py:_read_target×38")


def _journal(host, projs, spans=None):
    spans = spans or [30] * len(projs)
    return "\n".join(L % (host, p, sp) for p, sp in zip(projs, spans))


A = "base-mainnet.g.alchemy.com#3133"
D = "lb.drpc.live#afd4"

# ---- 1. Парс: только alchemy, значения и топ ----
ph = w.parse_cu_windows(_journal(A, [7, 9, 11]) + "\n" + _journal(D, [40, 44]))
check("alchemy распарсен (3 окна)", A in ph and len(ph[A]["projs"]) == 3, ph)
check("dRPC отфильтрован (не квотуем)", D not in ph, list(ph))
check("топ-атрибуция снята", "prearm" in ph[A]["top"], ph[A]["top"])

# ---- 2. Бут-окна выкинуты по полю скольз-спана: жирный прогрев не будит ----
projs = [60.0] * 6 + [12.0] * 24        # прогрев + штиль; со средним ПО ВСЕМ было бы ~21.6M
spans = [2, 4, 6, 9, 11, 14] + [30] * 24   # прогрев виден по спану < 16 мин
ph = w.parse_cu_windows(_journal(A, projs, spans))
check("бут-окна выкинуты (boot=6, осталось 24)", ph[A]["boot"] == 6 and len(ph[A]["projs"]) == 24, ph)
check("среднее без бута = штиль (12M < 26) -> тишина",
      w.verdicts(ph, threshold=26, min_windows=20) == [])
# пилообразный расход НЕ занижается (за это и среднее, не медиана)
saw = [5.0, 50.0] * 15                  # медиана была бы 27.5, но среднее тоже 27.5; острее:
saw = [1.0] * 16 + [80.0] * 14          # медиана 1.0 (проспала бы), среднее 37.9 — будит
ph = w.parse_cu_windows(_journal(A, saw))
v = w.verdicts(ph, threshold=26, min_windows=20)
check("пила (медиана спала бы) будит по среднему", len(v) == 1 and 37 < v[0][1] < 39, v)

# ---- 3. Устойчивый перерасход будит ----
ph = w.parse_cu_windows(_journal(A, [31.0] * 30))
v = w.verdicts(ph, threshold=26, min_windows=20)
check("среднее 31M >= 26 -> тревога", len(v) == 1 and v[0][1] == 31.0, v)

# ---- 4. Мало окон — не судим ----
ph = w.parse_cu_windows(_journal(A, [99.0] * 5))
check("5 окон < 20 -> молчание (не судим по огрызку)",
      w.verdicts(ph, threshold=26, min_windows=20) == [])

# ---- 5. run(): тревога уходит в инжектированный канал ----
sent = []


def _nf(text, *, source, hil, key, dedup_sec):
    sent.append((source, hil, key, text))
    return "inbox"


os.environ["CU_WATCH_PROJ_M"] = "26"
rc = w.run(fetch=lambda: _journal(A, [31.0] * 30), notify_fn=_nf, out=lambda *_: None)
check("run: тревога в инбокс (hil=False)", rc == 0 and len(sent) == 1 and sent[0][1] is False, sent)
check("run: ключ дедупа пер-хост", sent[0][2] == "cu-quota:%s" % A, sent)
check("run: текст зовёт агента, HIL — только новый ключ", "HIL-1" in sent[0][3])

# ---- 6. Тихий прогон — ничего не шлёт ----
sent.clear()
rc = w.run(fetch=lambda: _journal(A, [12.0] * 30), notify_fn=_nf, out=lambda *_: None)
check("run: ниже порога -> тишина", rc == 0 and sent == [])

# ---- 6b. Порог, по которому вынесен приговор, едет В ТЕКСТЕ тревоги ----
# Разбор 20.08: тревоги «жжёт 16M/20M» при боевом пороге 26 были позитивным контролем с
# заниженным CU_WATCH_PROJ_M. По тексту они не отличались от настоящих, и происхождение
# доказывалось археологией по времени коммита. Проверяем НЕ «26 в строке» (совпало бы с
# захардкоженной константой), а что печатается ИМЕННО ДЕЙСТВУЮЩИЙ порог: гоняем два
# прогона с разными порогами и требуем разные числа.
sent.clear()
os.environ["CU_WATCH_PROJ_M"] = "14"
w.run(fetch=lambda: _journal(A, [16.0] * 30), notify_fn=_nf, out=lambda *_: None)
low = sent[0][3] if sent else ""
sent.clear()
os.environ["CU_WATCH_PROJ_M"] = "15"
w.run(fetch=lambda: _journal(A, [16.0] * 30), notify_fn=_nf, out=lambda *_: None)
mid = sent[0][3] if sent else ""
os.environ.pop("CU_WATCH_PROJ_M", None)
check("тревога называет порог, по которому судила", "порог 14M" in low, low[:160])
check("порог в тексте — действующий, а не константа", "порог 15M" in mid and low != mid, mid[:160])

# ---- 7. Слепота: одиночный фейл терпим, серия будит (о САМОМ стороже) ----
sent.clear()


def _boom():
    raise RuntimeError("ssh упал")


for i in range(3):
    rc = w.run(fetch=_boom, notify_fn=_nf, out=lambda *_: None)
check("два фейла молчат, третий будит (streak=3)",
      rc == 1 and len(sent) == 1 and sent[0][2] == "cu-quota:blind", sent)
sent.clear()
rc = w.run(fetch=lambda: _journal(A, [12.0] * 30), notify_fn=_nf, out=lambda *_: None)
check("успех сбрасывает счётчик слепоты", rc == 0 and sent == [])

# ---- 8. Ноль окон = слепота (тест-который-может-вернуть-только-ноль закрыт) ----
sent.clear()
rc = w.run(fetch=lambda: "мусор без CU-строк", notify_fn=_nf, out=lambda *_: None)
check("пустой парс считается слепым прогоном, не тишиной", rc == 1)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS else "OK: сторож квоты — парс/медиана/слепота"))
sys.exit(1 if FAILS else 0)
