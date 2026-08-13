"""#110 ПОТОЛОК ГОЛОВЫ: индексу подают PENDING-номер, а он обязан читать цепь.

ЗАЧЕМ ЭТОТ ФАЙЛ. В боевой проводке индекс кормится из recv-петли flashblocks
(`chain_index.offer_block(bn)` в hotpath и main), а flashblocks несёт номер ФОРМИРУЕМОГО
блока. Узел такого блока ещё не отдаёт. Живой замер 13.08, по одному запросу на узел,
User-Agent curl/8.5.0:

    mainnet.base.org        head 49912247 -> eth_getLogs(head+1):
        -32602 «block range extends beyond current head block: requested 49912248, head 49912247»
    base-rpc.publicnode.com head 49912252 -> то же -32602; eth_getBlockByNumber(head+1) -> null

Прежняя редакция `_catch_up` при пустом `_last_bn` ставила start=head (то есть на pending),
падала на первом же скане и делала `return`. `_last_bn` оставался None НАВСЕГДА: живой тред,
ноль прочитанных блоков, поток ERROR о здоровом узле. Приёмка «блоков>0» была недостижима
ПО ПОСТРОЕНИЮ — ровно тот класс, которым нам обошлись три пакета прошлого раунда.

ДРЕЛЬ НИЖЕ ДЁРГАЕТ НАСТОЯЩИЙ `_catch_up` — через настоящий тред (`start()`), настоящий
`_loop`, настоящий `scan_block`. Подменён ровно один стык: сокет. И у дрели есть ПОЗИТИВНЫЙ
КОНТРОЛЬ: та же дрель на ПРЕЖНЕЙ логике догона обязана дать blocks==0 — иначе она не умеет
падать и ничего не доказывает.
"""
import logging
import pathlib
import sys
import threading
import time as _time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.chain_index as ci                                # noqa: E402

HEAD0 = 50000000


class Node:
    """Узел с ЧЕСТНОЙ головой: всё, что выше неё, отвергается так же, как это делает Base."""

    def __init__(self, head=HEAD0, clamp=False):
        self.head = head
        self.clamp = clamp          # True = «тихий» узел: на будущий блок отдаёт [] вместо ошибки
        self.calls = []
        self.asked_above = []

    def __call__(self, url, payload, timeout):
        m = payload["method"]
        self.calls.append(m)
        if m == "eth_blockNumber":
            return {"result": hex(self.head)}
        if m == "eth_getLogs":
            bn = int(payload["params"][0]["fromBlock"], 16)
            if bn > self.head:
                self.asked_above.append(bn)
                if self.clamp:
                    return {"result": []}          # тихий клампинг — самый опасный вариант
                return {"error": {"code": -32602,
                                  "message": "block range extends beyond current head block: "
                                             "requested %d, head %d" % (bn, self.head)}}
            return {"result": []}
        if m == "eth_getBlockByNumber":
            bn = int(payload["params"][0], 16)
            if bn > self.head:
                self.asked_above.append(bn)
                return {"result": None}
            return {"result": {"number": hex(bn), "timestamp": "0x1", "baseFeePerGas": "0x1",
                               "transactions": []}}
        raise AssertionError("индекс не имеет права звать %s" % m)


def _spin(cond, sec=6.0):
    t0 = _time.monotonic()
    while _time.monotonic() - t0 < sec:
        if cond():
            return True
        _time.sleep(0.02)
    return False


# ---------------------------------------------------------------- 1. ДРЕЛЬ (обязательная)
errs = []


class _Cap(logging.Handler):
    def emit(self, rec):
        if rec.levelno >= logging.ERROR:
            errs.append(rec.getMessage())


_h = _Cap()
logging.getLogger("liquidator").addHandler(_h)

node = Node()
idx = ci.ChainIndex("https://node.example/KEY", watch_to=["0x" + "11" * 20], http=node,
                    log_fn=lambda m: None)
idx.start()
try:
    # Подаём ИМЕННО pending — так и делает боевая проводка (flashblocks).
    idx.offer_block(node.head + 1)
    assert _spin(lambda: idx.stats["blocks"] > 0), \
        ("ВЕЧНЫЙ ЛАТЧ: индексу подали pending-номер, и он не прочитал НИ ОДНОГО блока "
         "(blocks=%d, last=%s, fails=%d)" % (idx.stats["blocks"], idx._last_bn, idx.stats["fails"]))
    assert idx._last_bn == node.head, ("сканировать надо до ПОДТВЕРЖДЁННОЙ головы: last=%s "
                                       "head=%d" % (idx._last_bn, node.head))
    # ...и через N блоков он идёт дальше, а не залипает на первом.
    for k in range(1, 4):
        node.head += 1
        idx.offer_block(node.head + 1)
        assert _spin(lambda k=k: idx._last_bn == HEAD0 + k), \
            "индекс обязан следовать за головой: last=%s ждали %d" % (idx._last_bn, HEAD0 + k)
    assert idx.stats["blocks"] >= 4, idx.stats
    assert idx.stats["fails"] == 0, ("«блока ещё нет» — НЕ отказ узла: fails=%d" % idx.stats["fails"])
    assert not node.asked_above, ("выше подтверждённой головы ходить незачем: спрашивали %s"
                                  % node.asked_above[:3])
    assert not errs, "здоровый узел не имеет права давать ERROR: %s" % errs[:2]
finally:
    idx.stop()
print("UNIT_OK: ДРЕЛЬ — индексу подан номер, которого у узла НЕТ; настоящий _catch_up прочитал "
      "%d блоков, дошёл до подтверждённой головы, отказов 0, ERROR 0" % idx.stats["blocks"])

# ---------------------------------------------------------------- 2. позитивный контроль дрели
class _Old(ci.ChainIndex):
    """ПРЕЖНЯЯ логика догона (до фикса), слово в слово. Если дрель зелёная и на ней — она
    ничего не проверяет (класс «тест, который может вернуть только ноль»)."""

    def _catch_up(self, head):
        start = (self._last_bn + 1) if self._last_bn is not None else head
        if head - start >= ci.CHAIN_INDEX_MAX_CATCHUP:
            start = head
        for bn in range(start, head + 1):
            try:
                self.scan_block(bn)
            except Exception as e:
                self._note_fail(e, bn)
                return


node2 = Node()
old = _Old("https://node.example/KEY", watch_to=[], http=node2, log_fn=lambda m: None)
old._head = node2.head + 1
old._catch_up(old._head)                 # ровно то, что делала боевая петля
old._catch_up(old._head)
assert old.stats["blocks"] == 0 and old._last_bn is None, \
    ("позитивный контроль сломан: прежняя логика обязана давать ВЕЧНЫЙ ЛАТЧ, а дала "
     "blocks=%d last=%s" % (old.stats["blocks"], old._last_bn))
assert old.stats["fails"] >= 2, old.stats
print("UNIT_OK: позитивный контроль — ПРЕЖНЯЯ логика на той же дрели даёт blocks=0, last=None и "
      "копит «отказы» здорового узла (дрель умеет падать)")

# ---------------------------------------------------------------- 3. тихий узел (клампинг в [])
node3 = Node(clamp=True)
idx3 = ci.ChainIndex("https://node.example/KEY", watch_to=[], http=node3, log_fn=lambda m: None)
idx3.start()
try:
    idx3.offer_block(node3.head + 5)
    assert _spin(lambda: idx3.stats["blocks"] > 0)
    _time.sleep(0.2)
    assert idx3._last_bn == node3.head, idx3._last_bn
    assert idx3.block_covered(node3.head + 1) is False and idx3._seen.get(node3.head + 1) is None, \
        ("ТИХАЯ ВЕТКА: узел ответил пустым списком на несуществующий блок, и он оказался "
         "помечен прочитанным — покрытие для «absent» стало бы враньём")
finally:
    idx3.stop()
print("UNIT_OK: узел, тихо клампящий будущий блок в [], не может пометить его прочитанным — "
      "выше подтверждённой головы индекс просто не ходит")

# ---------------------------------------------------------------- 4. отказ узла про голову
class _NoHead(Node):
    def __call__(self, url, payload, timeout):
        if payload["method"] == "eth_blockNumber":
            raise ci.IndexUnavailable("транспорт Timeout", "fixture")
        return Node.__call__(self, url, payload, timeout)


node4 = _NoHead()
idx4 = ci.ChainIndex("https://node.example/KEY", watch_to=[], http=node4, log_fn=lambda m: None)
idx4._head = node4.head + 1
idx4._catch_up(idx4._head)               # НАСТОЯЩИЙ догон, без головы от узла
assert idx4._last_bn == node4.head, \
    ("узел не ответил про голову -> потолок берём как hint-1 (подача несёт формируемый блок): "
     "last=%s" % idx4._last_bn)
assert idx4.stats["blocks"] == 1, idx4.stats
print("UNIT_OK: узел молчит про голову — потолок берётся как «подача минус один», индекс "
      "продолжает читать (а не встаёт)")

# ---------------------------------------------------------------- 5. уступка ключа — СВОЕМУ url
import chain.failover as fo                                    # noqa: E402


class _Ptr:
    def __init__(self, u):
        self.rpc_url = u


fo._ACTIVE = _Ptr("https://node.example/KEY/")
idx5 = ci.ChainIndex("https://node.example/KEY", watch_to=[], http=Node(), log_fn=lambda m: None)
assert idx5._shed() is True, "бой встал на НАШ ключ — фон обязан уступить"
fo._ACTIVE = _Ptr("https://combat.example/OTHER")
assert idx5._shed() is False, "бой на ЧУЖОМ ключе нас не касается (прежний bg_shed сравнивал не тот адрес)"
fo._ACTIVE = None
print("UNIT_OK: уступка ключа считается по СВОЕМУ url против текущего боевого указателя")

# ---------------------------------------------------------------- 6. гард боевого ключа
class _Cfg:
    rpc_url = "https://combat.example/COMBAT"
    liquidator_address = "0x" + "22" * 20


ci.stop_index()
_saved = ci.CHAIN_INDEX_RPC_URL
try:
    ci.CHAIN_INDEX_RPC_URL = "https://combat.example/COMBAT/"
    got = ci.start_index(cfg=_Cfg())
    assert got is None and ci.get_index() is None, \
        "индекс на БОЕВОМ ключе чтений подниматься не имеет права (#100)"
finally:
    ci.CHAIN_INDEX_RPC_URL = _saved
    ci.stop_index()
print("UNIT_OK: гард «URL индекса != боевой rpc_url» — фон на боевом ключе не поднимается")

# ------------------------------------------------- 7. выбор узла НЕ СМЕЕТ занять ключ №1
# БЛОКЕР Р2 №2. BG_RPC_URL — триггерная нога (predict-полл, preconf-цены, штиль-кэш позиций).
# Прежний выбор при типовой боевой конфигурации (оба bg-ключа заданы, коллектор без своей
# ручки) отдавал индексу ИМЕННО ЕЁ — «первый свободный от коллектора» — и сажал туда ~91k
# запросов в сутки под надписью «разведён». Это повтор #33: 29 минут слепоты предиктора за
# сутки стоили нам оси предвидения.
_ENV_COMBAT = {"BG_RPC_URL": "https://trigger/k1", "BG_RPC_URL_2": "https://mass/k2"}


def _old_choice(e):
    """ПРЕЖНЯЯ редакция дословно — позитивный контроль того, что дрель умеет падать."""
    col = (e.get("SPEC_FIRE_COLLECTOR_RPC_URL", "")
           or e.get("BG_RPC_URL_2", "") or e.get("BG_RPC_URL", ""))
    for name in ("BG_RPC_URL_2", "BG_RPC_URL"):
        u = e.get(name, "")
        if u and u != col:
            return (u, name)
    return ("", "")


assert _old_choice(_ENV_COMBAT) == ("https://trigger/k1", "BG_RPC_URL"), \
    "позитивный контроль: прежняя редакция и правда садилась на триггерный ключ"
u, src, note = ci.index_url_choice(_ENV_COMBAT)
assert u != "https://trigger/k1", "на триггерный ключ индекс садиться НЕ ИМЕЕТ ПРАВА: %s" % note
assert (u, src) == ("https://mass/k2", "BG_RPC_URL_2"), (u, src, note)

# Массовой ноги нет — не «сядем на что осталось», а ОТКАЗ с названной причиной.
u2, s2, n2 = ci.index_url_choice({"BG_RPC_URL": "https://trigger/k1"})
assert u2 == "" and "ОТКАЗ" in n2 and "CHAIN_INDEX_RPC_URL" in n2, (u2, n2)
u2b, _s, n2b = ci.index_url_choice({"BG_RPC_URL": "https://trigger/k1",
                                    "BG_RPC_URL_2": "https://trigger/k1"})
assert u2b == "" and "ОТКАЗ" in n2b, (u2b, n2b)

# PREDICT_RPC_URL — тоже триггерная нога (#33 её и вводил).
u2c, _s, n2c = ci.index_url_choice({"PREDICT_RPC_URL": "https://pred/p",
                                    "BG_RPC_URL_2": "https://pred/p"})
assert u2c == "" and "ОТКАЗ" in n2c, (u2c, n2c)

# Явная ручка сильнее всех — включая осознанный выбор триггерного ключа, но ВСЛУХ.
u3, s3, _ = ci.index_url_choice({"CHAIN_INDEX_RPC_URL": "https://own/x",
                                 "BG_RPC_URL": "https://trigger/k1"})
assert (u3, s3) == ("https://own/x", "CHAIN_INDEX_RPC_URL")
u4, _s4, n4 = ci.index_url_choice({"CHAIN_INDEX_RPC_URL": "https://trigger/k1",
                                   "BG_RPC_URL": "https://trigger/k1"})
assert u4 == "https://trigger/k1" and "ТРИГГЕРНОМ" in n4, (u4, n4)

# И ОТКАЗ обязан быть слышен в start_index, а не выражен отсутствием индекса.
_errs = []


class _CapErr(logging.Handler):
    def emit(self, rec):
        if rec.levelno >= logging.ERROR:
            _errs.append(rec.getMessage())


_he = _CapErr()
logging.getLogger("liquidator").addHandler(_he)
ci.stop_index()
_sv_u, _sv_n = ci.CHAIN_INDEX_RPC_URL, ci.CHAIN_INDEX_URL_NOTE
try:
    ci.CHAIN_INDEX_RPC_URL, ci.CHAIN_INDEX_URL_NOTE = ci.index_url_choice(
        {"BG_RPC_URL": "https://trigger/k1"})[0], ci.index_url_choice(
        {"BG_RPC_URL": "https://trigger/k1"})[2]
    assert ci.start_index(cfg=_Cfg()) is None
finally:
    ci.CHAIN_INDEX_RPC_URL, ci.CHAIN_INDEX_URL_NOTE = _sv_u, _sv_n
    logging.getLogger("liquidator").removeHandler(_he)
    ci.stop_index()
assert any("ОТКАЗ" in m and "CHAIN_INDEX_RPC_URL" in m for m in _errs), _errs
print("UNIT_OK: индекс не садится на триггерный ключ (ключ №1) — берёт массовую ногу либо "
      "ОТКАЗЫВАЕТСЯ вслух; явная ручка сильнее всех, но названа")

# ---------------------------------------------------------------- 8. «не отказ» не значит «молча»
warns = []


class _CapW(logging.Handler):
    def emit(self, rec):
        if rec.levelno >= logging.WARNING:
            warns.append(rec.getMessage())


_hw = _CapW()
logging.getLogger("liquidator").addHandler(_hw)
try:
    node8 = Node()
    idx8 = ci.ChainIndex("https://node.example/KEY", watch_to=[], http=node8,
                         log_fn=lambda m: None)
    idx8._head_conf, idx8._head_conf_ts = node8.head, 1e18   # потолок «свежий» и завышен
    idx8._head_conf = node8.head + 1
    idx8._last_bn = node8.head
    for _ in range(ci.CHAIN_INDEX_BEYOND_LOUD_N + 1):
        idx8._head_conf_ts = 1e18                            # не переспрашивать голову
        idx8._catch_up(node8.head + 2)
    assert idx8.stats["beyond_head"] >= ci.CHAIN_INDEX_BEYOND_LOUD_N, idx8.stats
    assert any("СТОИТ на этом номере" in m for m in warns), \
        ("залипание на «вечно будущем» номере обязано становиться ГРОМКИМ: %s" % warns[-2:])
    assert idx8.stats["fails"] == 0, "и при этом отказом оно так и не становится"
finally:
    logging.getLogger("liquidator").removeHandler(_hw)
print("UNIT_OK: «блока ещё нет» не отказ — но и не тишина навсегда: залипание на одном номере "
      "объявляется вслух (мёртвый сторож хуже отсутствующего)")

# --------------------------- 9. СПОСОБНОСТЬ УЗЛА НАЗВАНА (предусловие приёмки «в»)
# Без этой пробы `index_hits=0` нечитаем: и «узел не умеет eth_getBlockReceipts», и «мы ни разу
# не спросили», и «наших tx в блоках не было» дают ОДИН И ТОТ ЖЕ ноль. Живой замер 13.08:
# mainnet.base.org -> «-32601 rpc method is unsupported», base-rpc.publicnode.com -> список.
_T = [500.0]


def _cap_drill(mode):
    """(индекс, строки журнала). mode: ok | unsupported | transport."""
    seen = []

    def _http(url, payload, timeout):
        m = payload["method"]
        seen.append(m)
        if m == "eth_blockNumber":
            return {"result": hex(49890266)}
        if m == "eth_getBlockReceipts":
            if mode == "unsupported":
                return {"error": {"code": -32601, "message": "rpc method is unsupported"}}
            if mode == "transport":
                raise ci.IndexUnavailable("транспорт ConnectionError", "node")
            return {"result": [{"transactionHash": "0x" + "11" * 32, "status": "0x1",
                                "gasUsed": "0x1", "effectiveGasPrice": "0x1", "logs": []}]}
        if m == "eth_getLogs":
            return {"result": []}
        return {"result": None}

    ix = ci.ChainIndex("https://node.test/KEY", watch_to=["0x" + "ab" * 20], http=_http,
                       now=lambda: _T[0])
    msgs = []

    class _C(logging.Handler):
        def emit(self, rec):
            msgs.append(rec.getMessage())

    _hc = _C()
    logging.getLogger("liquidator").addHandler(_hc)
    try:
        ix._catch_up(49890267)
        ix._catch_up(49890267)          # второй оборот: проба НЕ имеет права повторяться
    finally:
        logging.getLogger("liquidator").removeHandler(_hc)
    return ix, msgs, seen


ix_ok, msgs_ok, seen_ok = _cap_drill("ok")
assert ix_ok._rcpt_probe == "поддержан" and ix_ok._receipts_ok is True
assert any("метод ПОДДЕРЖАН" in m for m in msgs_ok), msgs_ok
assert seen_ok.count("eth_getBlockReceipts") == 1,     "способность выясняется ОДНИМ запросом, а не каждый оборот: %s" % seen_ok
assert ix_ok.snapshot()["rcpt_probe"] == "поддержан"

ix_no, msgs_no, seen_no = _cap_drill("unsupported")
assert ix_no._rcpt_probe == "не поддержан" and ix_no._receipts_ok is False
assert any("НЕ ПОДДЕРЖАН" in m for m in msgs_no), msgs_no
assert seen_no.count("eth_getBlockReceipts") == 1, seen_no

# ТРАНСПОРТ — НЕ ПРИГОВОР СПОСОБНОСТИ: «не смогли спросить» это не «не умеет».
ix_tr, msgs_tr, seen_tr = _cap_drill("transport")
assert ix_tr._rcpt_probe == "не выяснено" and ix_tr._receipts_ok is True,     "транспортный отказ не имеет права ГАСИТЬ способность навсегда: %s" % ix_tr._rcpt_probe
assert seen_tr.count("eth_getBlockReceipts") == 1, "и не имеет права долбить узел каждый оборот"
_T[0] += ci.CHAIN_INDEX_RCPT_PROBE_RETRY_SEC + 1        # ВО ВРЕМЕНИ: окно переспроса прошло
ix_tr._catch_up(49890267)
assert seen_tr.count("eth_getBlockReceipts") == 2, \
    ("после окна переспроса проба обязана ПОВТОРИТЬСЯ — иначе «не выяснено» становится вечным "
     "и неотличимым от «не умеет»: %s" % seen_tr.count("eth_getBlockReceipts"))
assert ix_tr._rcpt_probe == "не выяснено" and ix_tr._receipts_ok is True
print("UNIT_OK: способность узла к eth_getBlockReceipts выясняется ОДНИМ запросом и называется "
      "вслух; -32601 гасит её навсегда, транспортный отказ — только откладывает (index_hits=0 "
      "стал читаемым)")

logging.getLogger("liquidator").removeHandler(_h)
print("ПРОШЛИ: потолок головы — pending-подача больше не запирает индекс")
