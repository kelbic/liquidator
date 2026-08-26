# -*- coding: utf-8 -*-
"""Свежее окно обязано ВЕЗТИ долю бута вместе с приговором (26.08).

ЭТАЛОН — РЕАЛЬНЫЕ ЧИСЛА ИНЦИДЕНТА, а не подогнанные: два прогона ключа A#3133 из
cu-quota.history.jsonl (10:07Z и 11:07Z 26.08). Их сумма 764133 CU / 6968.5 c = 9474k CU/сут
и есть то число, которым тревога 11:07:03Z вынесла приговор «остатка на 1.6 суток», не сказав,
что 57.5% его — прогрев трёх рестартов деплой-кампании.

КРАСНЫЙ-НА-БАЗЕ: без правки метка кандидата — «СВЕЖЕЕ ОКНО 1.9ч (2 последних прогона)» без
слова БУТ, и оба теста ниже падают.
"""
from analysis.cu_quota_watch import fresh_summary, balance_verdicts

AS_OF = 1787338435          # 21.08 18:53:55Z, якорь A/B/C
HOST = "base-mainnet.g.alchemy.com#3133"
NOW = 1787742422.0          # 26.08 11:07:02Z

RECS = [
    {"ts": 1787738821.99, "until": 1787738770.0, "restarts": 3,
     "hosts": {HOST: {"cu": 648494, "sec": 3364.0, "cu_boot": 439615, "n": 28, "boot": 14}}},
    {"ts": 1787742422.02, "until": 1787742374.0, "restarts": 1,
     "hosts": {HOST: {"cu": 115639, "sec": 3604.5, "cu_boot": 0, "n": 30, "boot": 0}}},
]
ANCHOR = {"quota_cu": 30000000, "reset": "calendar",
          "hosts": {HOST: {"mtd_cu": 11400000, "as_of": AS_OF}}}
LEDGER = {"hosts": {HOST: {"cu": 3158305.0, "since": AS_OF}}}


def _fresh():
    return fresh_summary(RECS, now=NOW, as_of_map={HOST: float(AS_OF)})


def test_fresh_summary_отдаёт_разложение_на_бут():
    fv = _fresh()[HOST]
    assert len(fv) >= 7, "разложение на бут не отдано: %r" % (fv,)
    rate, sec, _since, k, cu_boot, n_sub, n_boot = fv[:7]
    assert round(rate / 1000.0) == 9474, rate      # то самое число из тревоги
    assert round(sec, 1) == 6968.5, sec
    assert k == 2 and cu_boot == 439615.0 and n_sub == 58 and n_boot == 14, fv


def test_приговор_везёт_долю_бута():
    out = balance_verdicts(ANCHOR, LEDGER, {HOST: 675000.0},
                           burn_span={HOST: 112.2 * 3600},
                           burn_fresh=_fresh(), now=NOW)
    assert out, "приговор по остатку не вынесен вовсе"
    basis = out[0][7]
    assert "СВЕЖЕЕ ОКНО" in basis and "максимум из" in basis, basis
    # САМООТЧЁТ ЗАКРЫТ: число, переворачивающее приговор, стоит в самом приговоре.
    assert "БУТ 58%" in basis, basis
    assert "14 из 58 подокон бутовые" in basis, basis


def test_чистое_окно_про_бут_молчит():
    """Отсутствие бута не должно печатать «БУТ 0%» — шум в каждой строке штиля."""
    clean = [dict(RECS[1]), dict(RECS[1])]
    fv = fresh_summary(clean, now=NOW, as_of_map={HOST: float(AS_OF)})
    out = balance_verdicts(ANCHOR, LEDGER, {HOST: 675000.0},
                           burn_span={HOST: 112.2 * 3600}, burn_fresh=fv, now=NOW)
    assert not out or "БУТ" not in out[0][7], out


def test_старая_форма_кортежа_принимается():
    """3-элементный кортеж (внешние вызовы и старые тесты) не должен ломаться."""
    out = balance_verdicts(ANCHOR, LEDGER, {HOST: 675000.0},
                           burn_span={HOST: 112.2 * 3600},
                           burn_fresh={HOST: (9474000.0, 6968.5, float(AS_OF) + 1)}, now=NOW)
    assert out and "БУТ" not in out[0][7], out
