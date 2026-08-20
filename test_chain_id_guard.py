"""#117: ключ, выданный НЕ НА ТУ СЕТЬ, принимался молча.

Проверок сети в боте не осталось нигде. `strip_validation_middleware` (chain/rpc.py) снял
web3-middleware `validation` ради -45мс на чтение, а он был ЕДИНСТВЕННЫМ потребителем
eth_chainId. `BaseRpc.__init__` chain_id только ЗАПОМИНАЕТ и с узлом не сверяет. При этом
Multicall3 задеплоен по ОДНОМУ адресу на всех цепях, то есть aggregate3 ответит валидно и с
чужой цепи: PREARM грел бы Kyber на несуществующих позициях, а кэш раздавал бы чужие
borrow_shares — без единой строки в журнале.

Стенд гоняет НАСТОЯЩИЕ assert_chain_id/guard_chain_id (подменён только транспорт `_web3`):
  * три исхода различимы: True / False (чужая цепь) / None (узел не ответил);
  * ЧУЖАЯ ЦЕПЬ -> хэндл отклонён, потребители уходят на fallback;
  * УЗЕЛ МОЛЧИТ -> хэндл СОХРАНЁН. Это не мягкость: бут идёт под 429-штормом прогрева, и
    трактовка молчания как отказа сослала бы всю фоновую массу на боевой ключ — класс #100
    («фон, залезший в боевой ключ»), за который уже заплачено. Гард, который лечит редкий
    риск ценой гарантированной регрессии, — не гард;
  * fallback НЕ ЗАДАН (сам боевой узел, падать некуда) -> хэндл сохранён, но строка ERROR;
  * расхождение и молчание ОБА громкие (ни один не уходит тише ERROR).

Красный-на-базе: на HEAD `assert_chain_id`/`guard_chain_id` не существуют вовсе (ImportError),
а чужая цепь не порождает ни строки.
"""
import logging
import sys
import types

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from chain.rpc import assert_chain_id, guard_chain_id  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


class _Rpc:
    """Двойник хэндла: тот же шов, что у боевого кода — rpc._web3().eth.chain_id."""

    def __init__(self, cid, url="https://node.example/KEYKEYKEYKEY"):
        self._cid = cid                      # None => узел бросает (не ответил)
        self.rpc_url = url

    def _web3(self):
        if self._cid is None:
            raise RuntimeError("transport down")
        return types.SimpleNamespace(eth=types.SimpleNamespace(chain_id=self._cid))


class _CapturingLog:
    """Считаем строки по уровням: «громко» — проверяемое свойство, а не намерение."""

    def __init__(self):
        self.err, self.notes = [], []          # НЕ `self.info`: затёрло бы метод info() ниже

    def error(self, msg, *a):
        self.err.append(str(msg) % a if a else str(msg))

    def info(self, msg, *a):
        self.notes.append(str(msg) % a if a else str(msg))

    def warning(self, msg, *a):
        self.err.append(str(msg) % a if a else str(msg))

    def exception(self, msg, *a):
        self.err.append(str(msg) % a if a else str(msg))


# ---- 1. Три исхода различимы (иначе политика ниже неотличима от угадывания) ----
lg = _CapturingLog()
check("здоровый узел на нашей цепи -> True",
      assert_chain_id(_Rpc(8453), 8453, what="t", log=lg) is True)
check("узел на ЧУЖОЙ цепи -> False (не None)",
      assert_chain_id(_Rpc(10), 8453, what="t", log=lg) is False)
check("узел не ответил -> None (не False)",
      assert_chain_id(_Rpc(None), 8453, what="t", log=lg) is None)

# Обе беды обязаны быть слышны. Проверяем ФАКТ строк, а не то, что мы их «собирались» писать.
lg2 = _CapturingLog()
assert_chain_id(_Rpc(10), 8453, what="wrong", log=lg2)
check("чужая цепь -> строка ERROR", len(lg2.err) == 1, lg2.err)
lg3 = _CapturingLog()
assert_chain_id(_Rpc(None), 8453, what="dead", log=lg3)
check("узел молчит -> строка ERROR", len(lg3.err) == 1, lg3.err)

# ---- 2. Ключ в журнал не уезжает (гигиена: URL несёт секрет) ----
lg4 = _CapturingLog()
assert_chain_id(_Rpc(10, "https://base-mainnet.g.alchemy.com/v2/SECRETKEY123456"), 8453, log=lg4)
check("в строку гарда попадает ХОСТ, не ключ",
      all("SECRETKEY123456" not in s for s in lg4.err), lg4.err)

# ---- 3. Политика guard_chain_id ----
FALLBACK = _Rpc(8453, "https://combat.example")

good = _Rpc(8453)
check("True -> отдаём тот же хэндл",
      guard_chain_id(good, 8453, "t", fallback=FALLBACK, log=_CapturingLog()) is good)

wrong = _Rpc(10)
check("ЧУЖАЯ ЦЕПЬ -> хэндл отклонён, отдаём fallback",
      guard_chain_id(wrong, 8453, "t", fallback=FALLBACK, log=_CapturingLog()) is FALLBACK)

dead = _Rpc(None)
check("УЗЕЛ МОЛЧИТ -> хэндл СОХРАНЁН (не сослан на боевой: класс #100)",
      guard_chain_id(dead, 8453, "t", fallback=FALLBACK, log=_CapturingLog()) is dead)

# Боевой узел: подменить нечем. Тихо проглотить нельзя, но и отказаться работать — нельзя.
lg5 = _CapturingLog()
combat_wrong = _Rpc(10)
check("нет fallback + чужая цепь -> хэндл сохранён (падать некуда)",
      guard_chain_id(combat_wrong, 8453, "боевой", fallback=None, log=lg5) is combat_wrong)
check("нет fallback + чужая цепь -> ДВЕ строки ERROR (факт + «подменить нечем»)",
      len(lg5.err) == 2, lg5.err)

# fallback, совпадающий с самим хэндлом, — не подмена; молча вернуть его же нельзя тихо
lg6 = _CapturingLog()
same = _Rpc(10)
check("fallback is handle -> считается «подменить нечем», строка ERROR остаётся",
      guard_chain_id(same, 8453, "t", fallback=same, log=lg6) is same and len(lg6.err) == 2,
      lg6.err)

# ---- 4. Отказ транспорта любой формы -> None, гард наружу не бросает ----
# rpc_url=None проверяет заодно, что вычисление ярлыка для лога не падает само.
class _Weird:
    rpc_url = None

    def _web3(self):
        raise ValueError("экзотика транспорта")


try:
    r = assert_chain_id(_Weird(), 8453, log=_CapturingLog())
    check("экзотический отказ транспорта -> None, гард не бросает", r is None)
except Exception as e:
    check("экзотический отказ транспорта -> None, гард не бросает", False, type(e).__name__)


# ...но KeyboardInterrupt/SystemExit ПРОХОДЯТ НАСКВОЗЬ. Это не придирка: широкий `except
# BaseException` в гарде превратил бы Ctrl-C и штатное завершение юнита в «узел не ответил»,
# и systemd-стоп молча деградировал бы конфигурацию вместо остановки.
class _Interrupted:
    rpc_url = "https://x.example"

    def _web3(self):
        raise KeyboardInterrupt


try:
    assert_chain_id(_Interrupted(), 8453, log=_CapturingLog())
    check("KeyboardInterrupt НЕ проглочен гардом", False, "вернулся вместо проброса")
except KeyboardInterrupt:
    check("KeyboardInterrupt НЕ проглочен гардом", True)


print()
if FAILS:
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("chainId-гард: все проверки прошли")
