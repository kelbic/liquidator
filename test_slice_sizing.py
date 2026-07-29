"""Юниты slice-сайзинга (29.07, «го» kelbic): резать repaid до глубины пула вместо отказа
«no profitable chunk» (Katana-урок; USDe-кит $20M при глубине ~$1.15M). Без сети: probe замокан."""
import sys
sys.path.insert(0, ".")
from chain.execute import SLICE_MAX_IMPACT, SLICE_MIN_FRACTION, slice_to_depth

SWAP = {"router": "0xr", "calldata": "0x"}


def probe_linear(k, calls):
    def p(f):
        calls.append(f)
        return k * f, SWAP
    return p


# 1) глубокий пул: полный размер проходит с ПЕРВОЙ котировки (RTT не добавляется)
calls = []
f, swap = slice_to_depth(probe_linear(0.002, calls))
assert f == 1.0 and swap is SWAP and calls == [1.0], (f, calls)
print("UNIT_OK: глубокий пул — f=1.0 с первой котировки")

# 2) линейный крутой профиль (импакт 40% на полном): предел ~f=0.025, идём к нему, не в пол
calls = []
f, swap = slice_to_depth(probe_linear(0.40, calls))
assert swap is SWAP and 0.019 <= f <= 0.026 and 0.40 * f <= SLICE_MAX_IMPACT, (f, calls)
print("UNIT_OK: линейный 40%% — f=%.4f за %d котировок" % (f, len(calls)))

# 3) средний случай (импакт 3% на полном): оптимум f=1/3, сходимся в 0.26-0.34
calls = []
f, swap = slice_to_depth(probe_linear(0.03, calls))
assert swap is SWAP and 0.25 < f < 0.35 and 0.03 * f <= SLICE_MAX_IMPACT, (f, calls)
print("UNIT_OK: импакт 3%% -> f=%.3f (близко к оптимуму 0.333)" % f)

# 3b) ОБРЫВНЫЙ профиль USDe (канарейка 29.07): глубина до f=0.056 бездонная, дальше стена.
# Чистое сжатие падало в пол 0.02 и теряло 2/3 среза — теперь расширяемся к глубине.
def cliff(f):
    return (0.0005 if f <= 0.056 else 0.97, SWAP)


calls = []
f, swap = slice_to_depth(lambda fr: (calls.append(fr) or cliff(fr)))
assert swap is SWAP and f >= 0.04, "обязан дожать к глубине, а не остаться в полу: f=%s %s" % (f, calls)
print("UNIT_OK: обрывный USDe-профиль — f=%.4f (пол был бы 0.02), пробы: %s" %
      (f, ["%.3f" % c for c in calls]))

# 4) глубины нет вообще: импакт высокий даже на полу -> (0, None), цикл конечен
calls = []
f, swap = slice_to_depth(lambda fr: (calls.append(fr) or (0.5, SWAP)))
assert (f, swap) == (0.0, None) and len(calls) <= 4, (f, calls)
print("UNIT_OK: глубины нет — (0, None) за <=%d проб" % len(calls))

# 5) отказ котировки на КРУПНОМ размере = «слишком жирно», а не «глубины нет» (mGLO 29.07:
# Kyber отдавал 400 на 13.5M токенов, первая версия сдавалась и теряла цель $12.3M)
calls = []


def refuse_big(f):
    calls.append(round(f, 4))
    if f > 0.5:
        return None                      # агрегатор не строит маршрут такого размера
    return (0.003 if f <= 0.3 else 0.05, SWAP)


f, swap = slice_to_depth(refuse_big)
assert swap is SWAP and f >= 0.1, "отказ обязан вести к сжатию, а не к капитуляции: %s %s" % (f, calls)
print("UNIT_OK: отказ на жирном -> сжатие -> глубина f=%.3f (пробы %s)" % (f, len(calls)))

# 5b) отказ на ВСЕХ размерах -> (0, None)
assert slice_to_depth(lambda fr: None) == (0.0, None)
print("UNIT_OK: тотальный отказ котировки — (0, None)")

assert SLICE_MIN_FRACTION == 0.02 and SLICE_MAX_IMPACT == 0.01
print("=== ВСЕ SLICE-ЮНИТЫ ПРОШЛИ ===")
