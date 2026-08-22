"""Пост-мортем $128k (22.08): _revert_bucket ел ПРЕФИКС tx-хэша как «selector 0x8edd9824»,
пряча настоящую причину реверта. Селектор = ровно 8 hex с границей."""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from chain.execute import _revert_bucket  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


H = "0x8edd9824a6a9177adbfb136e05b5e37cb5fc643221c815e042324c090ef73b44"
b = _revert_bucket("tx %s status=0" % H)   # боевой err: без слова reverted
check("tx-хэш в тексте ошибки НЕ читается селектором (боевой случай $128k)",
      not b.startswith("selector"), b)
check("настоящий 8-hex селектор по-прежнему ловится (ветка селектора — когда нет "
      "«reverted: msg»)", _revert_bucket("custom error 0x81ceff31 thrown")
      == "selector 0x81ceff31")
check("канонический 0x81ceff30 — прежний бакет",
      _revert_bucket("blah 0x81ceff30 blah") == "Morpho 0x81ceff30")
check("healthy — прежний бакет",
      "healthy" in _revert_bucket("execution reverted: position is healthy"))

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS else "OK: селектор с границей, хэш не селектор"))
sys.exit(1 if FAILS else 0)
