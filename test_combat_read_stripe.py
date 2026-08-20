"""#117: страйп окно-массы на ТРЕТИЙ ключ (COMBAT_READ_RPC_URL).

Замер боевого ключа A 19.08: 1.4M CU/сутки = 42M/мес при квоте 30M (скользящая проекция
самого бота на пике показывала 67-73M/мес). Разложение eth_call: PREARM-прогрев (>=16 930
вызовов/сут — `_read_target` жжёт ДВА aggregate3 на цель) и буст-циклы кэша позиций
(<=10 560/сут). Оба слоя обслуживают predict-ОКНО (~2 мин), а не гонку одного блока, поэтому
уезжают на ключ C; денежный путь остаётся на A.

Стенд гоняет НАСТОЯЩИЕ build_combat_read_rpc / PreArmWorker._read_target /
_spawn_cache_refresh. Проверяется не наличие функции, а ФАКТ, что чтения ушли на другой
хэндл: «шов подменён выше дефекта» — это когда тест доволен, а боевой путь не исполнялся.

  * пусто / тот же URL / чужая цепь / мёртвый узел / битый конструктор -> БОЕВОЙ (бит-в-бит);
  * рабочий C -> отдельный хэндл именно с этим URL;
  * PREARM: оба aggregate3 `_read_target` летят в переданный хэндл, боевой не тронут;
  * кэш позиций: в БУСТ-окне цикл уходит на combat_rpc (=C), в штиль — на cache_rpc (bg);
  * ключ C в журнал не попадает (только хост).

Красный-на-базе: на HEAD нет ни поля cfg.combat_read_rpc_url, ни build_combat_read_rpc.
"""
import logging
import sys
import threading
import time
import types

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from chain.rpc import build_combat_read_rpc  # noqa: E402
import chain.hotpath as hp                   # noqa: E402
from chain.prearm import PreArmWorker        # noqa: E402

logging.basicConfig(level=logging.CRITICAL)
FAILS = []
SECRET = "CKEYCKEYCKEY0001"
C_URL = "https://base-mainnet.g.alchemy.com/v2/" + SECRET


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


class _Rpc:
    def __init__(self, cid=8453, url="https://combat.example/AKEY"):
        self._cid, self.rpc_url, self.chain_id = cid, url, 8453

    def _web3(self):
        if self._cid is None:
            raise RuntimeError("down")
        return types.SimpleNamespace(eth=types.SimpleNamespace(chain_id=self._cid))

    def to_bytes32(self, h):
        return bytes(32)


class _Log:
    def __init__(self):
        self.lines = []

    def _add(self, m, *a):
        self.lines.append(str(m) % a if a else str(m))
    error = warning = info = exception = _add


def cfg_with(url, rpc_url="https://combat.example/AKEY"):
    return types.SimpleNamespace(combat_read_rpc_url=url, rpc_url=rpc_url, chain_id=8453)


COMBAT = _Rpc()

# ---- 1. Все сомнительные случаи -> боевой (одно прежнее поведение, не третий режим) ----
check("пусто -> боевой", build_combat_read_rpc(cfg_with(""), COMBAT, log=_Log()) is COMBAT)
check("тот же URL, что боевой -> боевой",
      build_combat_read_rpc(cfg_with("https://combat.example/AKEY"), COMBAT, log=_Log()) is COMBAT)
check("None в поле -> боевой", build_combat_read_rpc(cfg_with(None), COMBAT, log=_Log()) is COMBAT)
check("пробелы -> боевой", build_combat_read_rpc(cfg_with("   "), COMBAT, log=_Log()) is COMBAT)

# Поля вообще нет (старый конфиг / чужой объект) — не падаем, отдаём боевой.
check("поля нет в cfg -> боевой",
      build_combat_read_rpc(types.SimpleNamespace(rpc_url="x", chain_id=8453), COMBAT,
                            log=_Log()) is COMBAT)

# ---- 2. Рабочий ключ C -> отдельный хэндл ИМЕННО с этим URL ----
import chain.rpc as _rpcmod  # noqa: E402

_orig_assert = _rpcmod.assert_chain_id
_rpcmod.assert_chain_id = lambda h, e, what="", log=None: True   # сеть подтверждена
try:
    _rpcmod._COMBAT_READ_LOGGED = False
    lg = _Log()
    h = build_combat_read_rpc(cfg_with(C_URL), COMBAT, log=lg)
    check("рабочий C -> НЕ боевой хэндл", h is not COMBAT)
    check("рабочий C -> хэндл несёт URL ключа C", getattr(h, "rpc_url", None) == C_URL, h)
    check("ключ C в журнал НЕ уехал (только хост)",
          all(SECRET not in s for s in lg.lines) and any("alchemy" in s for s in lg.lines),
          lg.lines)
finally:
    _rpcmod.assert_chain_id = _orig_assert

# ---- 3. Сеть не подтверждена -> страйп отменён (у C политика строже общей) ----
for verdict, label in ((False, "чужая цепь"), (None, "узел молчит")):
    _rpcmod.assert_chain_id = (lambda v: (lambda h, e, what="", log=None: v))(verdict)
    try:
        check("C: %s -> страйп ОТМЕНЁН, остаёмся на боевом" % label,
              build_combat_read_rpc(cfg_with(C_URL), COMBAT, log=_Log()) is COMBAT)
    finally:
        _rpcmod.assert_chain_id = _orig_assert

# Конструктор упал -> боевой, наружу не бросает
_orig_baserpc = _rpcmod.BaseRpc
_rpcmod.BaseRpc = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad url"))
try:
    check("конструктор упал -> боевой, исключение не наружу",
          build_combat_read_rpc(cfg_with(C_URL), COMBAT, log=_Log()) is COMBAT)
finally:
    _rpcmod.BaseRpc = _orig_baserpc


# ---- 4. ШОВ PREARM: оба aggregate3 летят в ПЕРЕДАННЫЙ хэндл, боевой не тронут ----
# Это и есть проверка «исполнялась ли настоящая функция»: считаем вызовы ПО ХЭНДЛУ.
SEEN = []


def _fake_aggregate3(rpc, calls, block_identifier=None):
    SEEN.append(getattr(rpc, "rpc_url", "?"))
    if len(calls) == 3:                       # idToMarketParams + market + position
        return [(True, b"mp"), (True, b"mk"), (True, b"ps")]
    return [(True, b"px")]                    # price()


import chain.multicall as _mc  # noqa: E402

_saved = {k: getattr(_mc, k) for k in
          ("aggregate3", "decode_id_to_market_params", "decode_market", "decode_position",
           "decode_price")}
_mc.aggregate3 = _fake_aggregate3
_mc.decode_id_to_market_params = lambda d: ("0xloan", "0xcoll", "0xoracle", "0xirm", 86 * 10**16)
_mc.decode_market = lambda d: (0, 0, 10**6, 10**6)
_mc.decode_position = lambda d: (10**6, 10**6)
_mc.decode_price = lambda d: 10**36
try:
    C_HANDLE = _Rpc(url=C_URL)
    shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
    w = PreArmWorker(C_HANDLE, types.SimpleNamespace(liquidator_address="0xL",
                                                     fast_lane_max_repaid_usd=5e5), shared)
    SEEN.clear()
    w._read_target("0x" + "11" * 32, "0x" + "22" * 20)
    check("PREARM: _read_target делает ДВА aggregate3 (замер >=2/цель подтверждён)",
          len(SEEN) == 2, SEEN)
    check("PREARM: ОБА чтения ушли на ключ C, боевой не тронут",
          SEEN and all(u == C_URL for u in SEEN), SEEN)
finally:
    for k, v in _saved.items():
        setattr(_mc, k, v)


# ---- 5. ШОВ КЭША: буст-окно -> combat_rpc (=C), штиль -> cache_rpc (bg) ----
USED = []
_real_refresh = hp._refresh_position_caches
hp._refresh_position_caches = lambda **kw: USED.append(getattr(kw.get("cache_rpc"), "rpc_url", "?"))
_real_shed = None
try:
    import chain.failover as _fo
    _real_shed = _fo.bg_shed
    _fo.bg_shed = lambda: False

    BG = _Rpc(url="https://bg.example")
    CC = _Rpc(url=C_URL)
    _shared = types.SimpleNamespace(lock=threading.Lock(), groups={})

    def _spawn_once():
        hp._CACHE_REFRESH_THREAD = None
        hp._CACHE_REFRESH_STARTED = 0.0
        hp._CACHE_COMBAT_SPEND.clear()
        USED.clear()
        hp._spawn_cache_refresh(cache_rpc=BG, shared=_shared, bn=1, combat_rpc=CC)
        for _ in range(200):                  # цикл уходит в тред — ждём факт, не спим наугад
            if USED:
                break
            time.sleep(0.005)
        return USED[0] if USED else None

    hp._CACHE_BOOST_UNTIL = time.monotonic() + 60.0        # predict-окно живо
    check("кэш: в БУСТ-окне цикл уходит на ключ C", _spawn_once() == C_URL, USED)

    hp._CACHE_BOOST_UNTIL = 0.0                            # штиль
    check("кэш: в штиль цикл остаётся на bg (ключ C не тратится зря)",
          _spawn_once() == "https://bg.example", USED)

    # Откат ручкой: POSITION_CACHE_COMBAT_RPC=0 -> всё на bg, как до пакета 05.08
    hp._CACHE_BOOST_UNTIL = time.monotonic() + 60.0
    _sv = hp._CACHE_COMBAT_BOOST
    hp._CACHE_COMBAT_BOOST = False
    try:
        check("кэш: откат POSITION_CACHE_COMBAT_RPC=0 -> bg даже в окне",
              _spawn_once() == "https://bg.example", USED)
    finally:
        hp._CACHE_COMBAT_BOOST = _sv
finally:
    hp._refresh_position_caches = _real_refresh
    hp._CACHE_BOOST_UNTIL = 0.0
    if _real_shed is not None:
        import chain.failover as _fo
        _fo.bg_shed = _real_shed


# ---- 6. Поле конфига реально читается из env ----
import os  # noqa: E402
from config import Config  # noqa: E402

_prev = os.environ.get("COMBAT_READ_RPC_URL")
try:
    os.environ["COMBAT_READ_RPC_URL"] = C_URL
    check("COMBAT_READ_RPC_URL -> cfg.combat_read_rpc_url",
          Config.from_env().combat_read_rpc_url == C_URL)
    os.environ["COMBAT_READ_RPC_URL"] = ""
    check("пустой env == не задано (дефолт пуст)", Config.from_env().combat_read_rpc_url == "")
finally:
    os.environ.pop("COMBAT_READ_RPC_URL", None)
    if _prev is not None:
        os.environ["COMBAT_READ_RPC_URL"] = _prev


print()
if FAILS:
    print("ПРОВАЛЕНО: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("страйп на ключ C: все проверки прошли")
