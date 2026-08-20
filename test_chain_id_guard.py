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
        self.chain_id = 8453                 # ОЖИДАЕМАЯ цепь (как у BaseRpc: только память)

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


# ---- 5. FAILOVER-КАНДИДАТЫ: здесь гард нужнее всего ----
# head-guard failover'а (`bn >= self._last_head`) кандидата с ЧУЖОЙ цепи НЕ отсеет: у OP
# (~130M) и Arbitrum (~260M) головы ВЫШЕ, чем у Base (~34M). Без гарда бой переключился бы на
# чужую цепь ровно в отказ боевого узла — когда смотреть некому.
import chain.failover as _fo  # noqa: E402

_sv = {k: getattr(_fo, k) for k in ("COMBAT_FAILOVER", "COMBAT_FAILOVER_URLS", "BaseRpc")}
_chains = {"https://good.example": 8453, "https://wrongnet.example": 10,
           "https://dead.example": None}
try:
    _fo.COMBAT_FAILOVER = True
    _fo.BaseRpc = lambda u, cid, request_timeout=None: _Rpc(_chains[u], u)
    _primary = _Rpc(8453, "https://primary.example")

    _fo.COMBAT_FAILOVER_URLS = ["https://good.example", "https://wrongnet.example"]
    _res = _fo.build_failover(_primary, types.SimpleNamespace(bg_rpc_url=""), alerter=None)
    _urls = [p.rpc_url for p in getattr(_res, "providers", [])][1:]
    check("failover: кандидат с ЧУЖОЙ цепи исключён, годный остался",
          _urls == ["https://good.example"], _urls)

    _fo.COMBAT_FAILOVER_URLS = ["https://good.example", "https://dead.example"]
    _res = _fo.build_failover(_primary, types.SimpleNamespace(bg_rpc_url=""), alerter=None)
    _urls = [p.rpc_url for p in getattr(_res, "providers", [])][1:]
    check("failover: МОЛЧАЩИЙ кандидат сохранён (резерв, выброшенный на буте, — не резерв)",
          len(_urls) == 2, _urls)

    _fo.COMBAT_FAILOVER_URLS = ["https://wrongnet.example"]
    _res = _fo.build_failover(_primary, types.SimpleNamespace(bg_rpc_url=""), alerter=None)
    check("failover: годных не осталось -> указатель прибит к primary (не к чужой цепи)",
          _res is _primary, _res)
finally:
    for k, v in _sv.items():
        setattr(_fo, k, v)
    _fo._ACTIVE = None


# ---- 6. px_confirmed: гард НЕ должен возвращаться на ДЕНЕЖНЫЙ путь ----
# px_confirmed_rpc() строит хэндл лениво ПОД ЛОКОМ, и туда добавлен chainId-гард = сетевой
# вызов с таймаутом 8с. Зовут её из read_price_hedged — реактивного пути. Поэтому: (1) прогрев
# делается на старте (main.py), (2) синглтон ОБЯЗАН быть выставлен после любого исхода прогрева.
# Если он останется None, горячий путь будет ПОВТОРЯТЬ восьмисекундную пробу на каждом чтении
# цены — то есть дефект воскреснет ровно в деградацию, когда он дороже всего.
import os as _os  # noqa: E402
import chain.hotpath as _hp  # noqa: E402
import chain.rpc as _rp  # noqa: E402

_prev_url = _os.environ.get("PX_CONFIRMED_RPC_URL")
_os.environ["PX_CONFIRMED_RPC_URL"] = "https://reserve.example/BKEY"
_combat = types.SimpleNamespace(rpc_url="https://combat.example")
_orig_BR = _rp.BaseRpc
try:
    for _label, _cid, _ctor_bad in (("узел здоров", 8453, False),
                                    ("узел на ЧУЖОЙ цепи", 10, False),
                                    ("узел МОЛЧИТ на буте", None, False),
                                    ("конструктор упал", 8453, True)):
        _net = []

        class _FakeBR(_orig_BR):
            def __init__(s, u, c, request_timeout=None):
                if _ctor_bad:
                    raise ValueError("bad url")
                _orig_BR.__init__(s, u, c, request_timeout)

            def _web3(s, _c=_cid):
                _net.append(1)
                if _c is None:
                    raise RuntimeError("узел лежит")
                return types.SimpleNamespace(eth=types.SimpleNamespace(chain_id=_c))

        _rp.BaseRpc = _FakeBR
        _hp._PX_CONFIRMED_RPC = None
        _hp.px_confirmed_rpc(_combat)                      # прогрев на старте
        _warm, _sent = len(_net), _hp._PX_CONFIRMED_RPC
        for _ in range(3):
            _hp.px_confirmed_rpc(_combat)                  # горячий путь
        check("px_confirmed [%s]: синглтон выставлен (не None => нет повтора пробы)" % _label,
              _sent is not None, _sent)
        check("px_confirmed [%s]: горячий путь БЕЗ сети" % _label, len(_net) - _warm == 0,
              len(_net) - _warm)
        # Какой ИМЕННО сентинел ожидаем, зависит от исхода — проверяем его, а не «что-нибудь».
        # False = вечный откат на боевой; хэндл = резервный ключ принят (в т.ч. непроверенным,
        # когда узел молчал: незнание не хоронит резерв вечным решением).
        _want_false = (_cid == 10) or _ctor_bad
        check("px_confirmed [%s]: сентинел = %s" % (_label, "False" if _want_false else "хэндл"),
              (_sent is False) if _want_false else (_sent is not False), _sent)
finally:
    _rp.BaseRpc = _orig_BR
    _hp._PX_CONFIRMED_RPC = None
    _os.environ.pop("PX_CONFIRMED_RPC_URL", None)
    if _prev_url is not None:
        _os.environ["PX_CONFIRMED_RPC_URL"] = _prev_url


print()
if FAILS:
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("chainId-гард: все проверки прошли")
