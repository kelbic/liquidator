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

# 2) USDe-профиль (импакт 40% на полном): сходится к min-доле, импакт в пределе
calls = []
f, swap = slice_to_depth(probe_linear(0.40, calls))
assert swap is SWAP and f <= 0.021 and 0.40 * f <= SLICE_MAX_IMPACT, (f, calls)
print("UNIT_OK: USDe-профиль — срез до f=%.3f за %d котировки" % (f, len(calls)))

# 3) средний случай (импакт 3% на полном): доля ~0.27, не пол
calls = []
f, swap = slice_to_depth(probe_linear(0.03, calls))
assert swap is SWAP and 0.2 < f < 0.4 and 0.03 * f <= SLICE_MAX_IMPACT, (f, calls)
print("UNIT_OK: импакт 3%% -> f=%.3f (режем пропорционально, не в пол)" % f)

# 4) глубины нет вообще: импакт высокий даже на полу -> (0, None), цикл конечен
calls = []
f, swap = slice_to_depth(lambda fr: (calls.append(fr) or (0.5, SWAP)))
assert (f, swap) == (0.0, None) and len(calls) <= 4, (f, calls)
print("UNIT_OK: глубины нет — (0, None) за <=%d проб" % len(calls))

# 5) отказ котировки -> (0, None) сразу
assert slice_to_depth(lambda fr: None) == (0.0, None)
print("UNIT_OK: отказ котировки — (0, None)")

assert SLICE_MIN_FRACTION == 0.02 and SLICE_MAX_IMPACT == 0.01
print("=== ВСЕ SLICE-ЮНИТЫ ПРОШЛИ ===")
