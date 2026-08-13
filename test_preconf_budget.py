"""Юниты preconf-бюджета (24.07, инцидент «degraded 18x windows»): полл уступает канал
реактиву, но форы не теряет вне тени. Без сети."""
import sys
import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.hotpath as hp

# 1) чистый штиль: полл идёт на preconf (фора сохранена — главное требование)
hp._PRECONF_LAST_REACTIVE = -1e9
hp._PRECONF_LAST_POLL = -1e9
assert hp.preconf_poll_allowed(now=1000.0) is True
print("UNIT_OK: без реактива полл берёт preconf (фора у poll-only рынков цела)")

# 2) в тени реактивного чтения — уступает (именно тут он и ловил 429)
hp.preconf_mark_reactive(now=1000.0)
assert hp.preconf_poll_allowed(now=1000.5) is False
assert hp.preconf_poll_allowed(now=1001.0) is False
print("UNIT_OK: в тени реактива (<%.1fс) полл уступает канал" % hp.PRECONF_REACTIVE_QUIET)

# 3) тень прошла — фора возвращается сразу, без штрафного простоя
assert hp.preconf_poll_allowed(now=1001.3) is True
print("UNIT_OK: после тени полл сразу снова на preconf (штрафа нет)")

# 4) MIN_GAP по умолчанию выключен: подряд идущие блоки не режутся
hp._PRECONF_LAST_REACTIVE = -1e9
assert hp.PRECONF_POLL_MIN_GAP == 0.0, "gap-резерв должен быть выключен по умолчанию"
assert hp.preconf_poll_allowed(now=2000.0) is True
assert hp.preconf_poll_allowed(now=2002.0) is True, "каждый блок — свой preconf-полл"
print("UNIT_OK: MIN_GAP выключен — поблочный ритм полла сохранён")

# 5) MIN_GAP как аварийный рычаг (env) реально режет частоту
_old = hp.PRECONF_POLL_MIN_GAP
hp.PRECONF_POLL_MIN_GAP = 6.0
hp._PRECONF_LAST_POLL = -1e9
assert hp.preconf_poll_allowed(now=3000.0) is True
assert hp.preconf_poll_allowed(now=3002.0) is False
assert hp.preconf_poll_allowed(now=3006.5) is True
hp.PRECONF_POLL_MIN_GAP = _old
print("UNIT_OK: MIN_GAP-рычаг работает (аварийный резерв через env)")

# 6) датчик реактивной форы: ok/fail считаются и сбрасываются окном
hp.px_preconf_take()
hp.px_preconf_note(True); hp.px_preconf_note(True); hp.px_preconf_note(False)
assert hp.px_preconf_take() == (2, 1)
assert hp.px_preconf_take() == (0, 0), "take обязан сбрасывать окно"
print("UNIT_OK: датчик реактивной форы (ok/fail) считает и сбрасывается")

# 7) read_preconf_prices: при block_identifier=None читает latest БЕЗ preconf-платы
seen = {}


class _FakeRpc:
    def __init__(self, name): self.name = name


def _fake_agg(rpc, calls, block_identifier="pending"):
    seen["rpc"], seen["bid"] = rpc.name, block_identifier
    return [(True, b"\x00" * 32) for _ in calls]


import chain.multicall as mc
_orig = mc.aggregate3
mc.aggregate3 = _fake_agg
try:
    hp.read_preconf_prices(_FakeRpc("latest-rpc"), ["0xoracle"], rpc_fallback=None, block_identifier=None)
    assert seen == {"rpc": "latest-rpc", "bid": None}, seen
    hp.read_preconf_prices(_FakeRpc("preconf"), ["0xoracle"], rpc_fallback=None)
    assert seen == {"rpc": "preconf", "bid": "pending"}, seen
finally:
    mc.aggregate3 = _orig
print("UNIT_OK: уступивший полл читает latest напрямую (без 429-платы), обычный — pending")

print("=== ВСЕ PRECONF-BUDGET ЮНИТЫ ПРОШЛИ ===")
