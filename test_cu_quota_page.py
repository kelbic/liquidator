"""#117: маркер квоты CU обязан будить ЧЕЛОВЕКА, а не только журнал.

Замер владельца 19.08: маркер «CU-квота» сработал 44 раза за 7 суток и ни разу не дошёл до
человека. Прибор исправен — сломан канал «журнал→человек». Класс «мёртвый сторож хуже
отсутствующего»: строка, которую никто не читает, неотличима от молчания.

Решение 02.08 («CU — в журнал, не в TG») принималось против ПРЕДВЕСТНИКА и тогда было верным.
Здесь другое: исчерпание квоты бот сам не отрабатывает, оно требует действия человека
(ротация/страйп/докупка) — один из четырёх законных случаев канона «алерты только там, где
нужен человек».

Стенд гоняет НАСТОЯЩИЙ блок решения (вынесен один-в-один в _decide ниже — если боевой блок
разъедется, тест это НЕ поймает; поэтому здесь же проверяется, что боевые константы и латчи
существуют и читаются оттуда, а не из копии). Проверяется:
  * порог + span соблюдены -> пейдж уходит; недобор любого из двух -> молчание;
  * ЛАТЧ НА КЛЮЧ И НА СУТКИ: второй пейдж по тому же ключу в те же сутки не уходит...
  * ...а по ДРУГОМУ ключу — уходит (прежний общий latch глушил соседний ключ на 6ч);
  * латч МОДУЛЬНЫЙ: переживает self-heal-перезапуск hot_loop (был локальным => обнулялся);
  * не-alchemy хост не будит;
  * CU_ALERT_TG=0 -> прежнее поведение (только журнал);
  * отсутствующий alerter -> молчание, а не падение (канон notify_fn);
  * в текст пейджа не попадает URL/ключ — только host#crc.
"""
import sys
import types

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.hotpath as hp  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


class _Alerter:
    def __init__(self):
        self.sent = []

    def send_async(self, text, key="default"):
        self.sent.append((key, text))


class _Log:
    def __init__(self):
        self.lines = []

    def _add(self, m, *a):
        self.lines.append(str(m) % a if a else str(m))
    info = warning = error = exception = _add


def _decide(host, roll_m, span, now, alerter, log, top3="eth_call×100"):
    """Копия боевого условия из hot_loop (см. шапку про предел этого стенда)."""
    over = ("alchemy" in host and roll_m >= hp.CU_ALERT_PROJ_M and span >= hp.CU_ALERT_MIN_SPAN_SEC)
    if over and now - hp._CU_MARK_TS.get(host, -1e9) > hp.CU_MARK_SEC:
        hp._CU_MARK_TS[host] = now
        log.warning("CU-квота (журнальный маркер): %s", host)
    if (over and hp.CU_ALERT_TG and alerter is not None
            and now - hp._CU_PAGE_TS.get(host, -1e9) > hp.CU_ALERT_PAGE_SEC):
        hp._CU_PAGE_TS[host] = now
        days = (hp.CU_QUOTA_M / roll_m * 30.0) if roll_m > 0 else 0.0
        alerter.send_async(
            "\U0001F4B8 КВОТА RPC: %s жжёт %.0fM CU/мес при квоте %.0fM — при таком темпе ключ "
            "кончится за ~%.0f сут.\nТоп: %s\nНужен ЧЕЛОВЕК: ротация/страйп/докупка ключа."
            % (host, roll_m, hp.CU_QUOTA_M, days, top3), key="cu-quota-%s" % host)
    return alerter.sent if alerter is not None else []


def reset():
    hp._CU_MARK_TS.clear()
    hp._CU_PAGE_TS.clear()


# ---- 0. Боевые ручки и латчи существуют (иначе стенд меряет собственную копию) ----
for nm in ("CU_ALERT_TG", "CU_ALERT_PAGE_SEC", "CU_QUOTA_M", "CU_MARK_SEC",
           "_CU_MARK_TS", "_CU_PAGE_TS"):
    check("боевая константа/латч %s существует" % nm, hasattr(hp, nm))
check("латч пейджа — СЛОВАРЬ по ключу (не одно число на все хосты)",
      isinstance(hp._CU_PAGE_TS, dict) and isinstance(hp._CU_MARK_TS, dict))
check("пейдж по умолчанию ВКЛЮЧЁН (сторож не должен требовать включения)", hp.CU_ALERT_TG is True)
check("латч пейджа — сутки", hp.CU_ALERT_PAGE_SEC == 86400.0, hp.CU_ALERT_PAGE_SEC)
_SRC = open(hp.__file__.replace(".pyc", ".py")).read()
check("last_cu_alert из hot_loop убран (был локальным => глох от self-heal)",
      "last_cu_alert = 0.0" not in _SRC)

# ГАРД ДРЕЙФА КОПИИ. _decide выше — копия боевого условия, и сама по себе она ничего о бое не
# доказывает (канон «шов подменён выше дефекта»). Полноценно исполнить боевой блок нельзя: он
# сидит внутри recv-петли ws. Поэтому проверяем, что боевой текст всё ещё несёт те же несущие
# элементы: пер-хостовый латч пейджа, ручку отката и вызов канала. Это НЕ проверка поведения —
# это защёлка против молчаливого расхождения копии с оригиналом.
for _frag in ("_CU_PAGE_TS.get(_host", "CU_ALERT_TG", "alerter.send_async(", "_CU_MARK_TS[_host]"):
    check("боевой блок всё ещё содержит %r" % _frag, _frag in _SRC)

A = "base-mainnet.g.alchemy.com#3133"
B = "base-mainnet.g.alchemy.com#5735"

# ---- 1. Порог и span ----
reset()
al = _Alerter()
_decide(A, 70.0, 3600.0, 1000.0, al, _Log())
check("проекция выше порога + span набран -> пейдж ушёл", len(al.sent) == 1, al.sent)

reset()
al = _Alerter()
_decide(A, 10.0, 3600.0, 1000.0, al, _Log())
check("проекция НИЖЕ порога -> молчание", al.sent == [])

reset()
al = _Alerter()
_decide(A, 70.0, 60.0, 1000.0, al, _Log())
check("span не набран (бут-спайк) -> молчание", al.sent == [])

# ---- 2. Латч: один пейдж в сутки НА КЛЮЧ ----
reset()
al = _Alerter()
_decide(A, 70.0, 3600.0, 1000.0, al, _Log())
_decide(A, 70.0, 3600.0, 1000.0 + 3600.0, al, _Log())      # час спустя
_decide(A, 70.0, 3600.0, 1000.0 + 43200.0, al, _Log())     # полсуток спустя
check("повторы в те же сутки по тому же ключу -> ОДИН пейдж", len(al.sent) == 1, al.sent)
_decide(A, 70.0, 3600.0, 1000.0 + 86401.0, al, _Log())     # сутки прошли
check("через сутки пейдж повторяется (беда не исчезла)", len(al.sent) == 2, al.sent)

# ---- 3. Латч ПЕР-КЛЮЧЕВОЙ: шумный ключ не глушит соседний ----
reset()
al = _Alerter()
_decide(A, 70.0, 3600.0, 1000.0, al, _Log())
_decide(B, 45.0, 3600.0, 1000.0 + 1.0, al, _Log())
check("другой ключ в те же сутки -> СВОЙ пейдж (прежний общий latch его глушил)",
      len(al.sent) == 2 and al.sent[0][0] != al.sent[1][0], al.sent)

# ---- 4. Латч переживает self-heal-перезапуск hot_loop ----
reset()
al = _Alerter()
_decide(A, 70.0, 3600.0, 1000.0, al, _Log())
# self-heal: hot_loop падает и поднимается заново. Модульный латч обязан ПОМНИТЬ.
check("после self-heal латч помнит (модульный, не локальный)",
      hp._CU_PAGE_TS.get(A) == 1000.0, dict(hp._CU_PAGE_TS))
_decide(A, 70.0, 3600.0, 1000.0 + 600.0, al, _Log())
check("после self-heal повторный пейдж НЕ уходит", len(al.sent) == 1, al.sent)

# ---- 5. Не-alchemy хост не будит (у публичных узлов квоты этого вида нет) ----
reset()
al = _Alerter()
_decide("lb.drpc.live#afd4", 90.0, 3600.0, 1000.0, al, _Log())
check("не-alchemy хост -> молчание", al.sent == [])

# ---- 6. Откат ручкой и отсутствие канала ----
reset()
al = _Alerter()
_sv = hp.CU_ALERT_TG
hp.CU_ALERT_TG = False
try:
    lg = _Log()
    _decide(A, 70.0, 3600.0, 1000.0, al, lg)
    check("CU_ALERT_TG=0 -> пейджа нет, журнальный маркер ОСТАЁТСЯ",
          al.sent == [] and any("журнальный маркер" in s for s in lg.lines), lg.lines)
finally:
    hp.CU_ALERT_TG = _sv

reset()
lg = _Log()
try:
    _decide(A, 70.0, 3600.0, 1000.0, None, lg)
    check("alerter не прокинут -> молчание, а не падение (канон notify_fn)",
          any("журнальный маркер" in s for s in lg.lines), lg.lines)
except Exception as e:
    check("alerter не прокинут -> молчание, а не падение (канон notify_fn)", False, repr(e))

# ---- 7. Гигиена: в пейдже host#crc, не URL с ключом ----
reset()
al = _Alerter()
_decide(A, 70.0, 3600.0, 1000.0, al, _Log())
_txt = al.sent[0][1]
check("в пейдже нет схемы URL (значит и ключа)", "://" not in _txt, _txt)
check("в пейдже есть host#crc и порядок действий",
      A in _txt and "ЧЕЛОВЕК" in _txt, _txt)
check("в пейдже назван срок исчерпания (иначе непонятно, насколько срочно)",
      "кончится за" in _txt, _txt)

reset()
print()
if FAILS:
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("CU-квота -> человек: все проверки прошли")
