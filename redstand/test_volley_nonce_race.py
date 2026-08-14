"""ОКНО КОЛЛИЗИИ NONCE между ИЗЪЯТИЕМ пробы и завершением её отправки (блокер ревью 12.08).

ЧТО ОБЕЩАЛ КОММИТ. «Коллизия снята КОНСТРУКЦИЕЙ: полная пересборка стартует с
max(локальный счётчик, pending), а не с чистого pending».

ЧЕМ ОН БЫЛ НЕВЕРЕН. Локальный счётчик двигался ТОЛЬКО ПОСЛЕ успешной рассылки. Между
`st["q"].pop(0)` и ответом секвенсера (тёплая сессия p50=32.5мс, хвост — секунды) номер
считался свободным И локально, И у узла: pending его ещё не видит. Полная пересборка, попавшая
в это окно (а её теперь запускает не только цена, но и переезд по тиру у ЖИВОЙ, стреляющей
ноги), бралась за тот же номер — «replacement transaction underpriced». 11.08 таких коллизий
было 85 за 10 минут.

ШОВ ЗДЕСЬ — ТОЛЬКО СЕТЕВОЙ СТЫК: подменяются `chain.execute._seq_session` (HTTP-сессия до
секвенсера) и web3-объект узла. НАСТОЯЩИМИ исполняются `send_presigned_probe` целиком (гейты
патрона, лок ноги, изъятие записи, возврат записи на отказе) и `presign_ladder` целиком.

ПОЗИТИВНЫЙ КОНТРОЛЬ машинный: прежняя редакция (сдвиг счётчика ПОСЛЕ отправки) собирается из
того же исходника отдельным модулем и проходит тот же стенд — она обязана ПОВТОРИТЬ занятый
номер.
"""
import pathlib
import sys
import threading
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import chain.execute as ex                                      # noqa: E402
from eth_account import Account                                 # noqa: E402

KEY = "0x" + "77" * 32
ACCT = Account.from_key(KEY)
ADDR = ACCT.address
GAS = 900_000
BASEFEE = int(0.005e9)
SEQ = "https://sequencer.example/KEYPATH"

cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)


class _Eth:
    account = Account

    def __init__(self, outer):
        self._o = outer

    def get_block(self, _b):
        return {"baseFeePerGas": self._o.base_fee}

    def get_balance(self, _a):
        return self._o.balance_wei

    def get_transaction_count(self, _a, _blk="pending"):
        # УЗЕЛ ОТСТАЁТ: он ещё не увидел разосланную пробу — ровно то окно, о котором блокер
        self._o.pending_reads += 1
        return self._o.pending_nonce


class _W3:
    def __init__(self, outer):
        self.eth = _Eth(outer)


class _Rpc:
    chain_id = 8453

    def __init__(self):
        self.base_fee = BASEFEE
        self.balance_wei = int(0.05 * 10**18)
        self.pending_nonce = 0
        self.pending_reads = 0

    def _web3(self):
        return _W3(self)


class _Resp:
    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


class _BlockingSess:
    """Сетевой стык: пост УХОДИТ, но НЕ ЗАВЕРШАЕТСЯ, пока тест не отпустит."""

    def __init__(self, gate, entered, fail=False):
        self.gate, self.entered, self.fail = gate, entered, fail
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json["params"][0])
        self.entered.set()
        self.gate.wait(5.0)
        if self.fail:
            return _Resp({"error": {"message": "replacement transaction underpriced"}})
        return _Resp({"result": "0x" + "ab" * 32})


def stand(mod, start_nonce=100, depth=4):
    """Нога с НАСТОЯЩЕЙ лестницей; узел на этой ноге стоит на том же nonce."""
    rpc = _Rpc()
    rpc.pending_nonce = start_nonce
    mod._VOLLEY_LOCKS[ADDR] = threading.Lock()
    mod.invalidate_ladder(ADDR)
    mod._VOLLEY_NONCE.pop(ADDR, None)
    mod._VOLLEY_BAL.pop(ADDR, None)
    mod._VOLLEY_GAS_USD = 0.0
    mod._VOLLEY_GAS_RESERVED = 0.0
    mod._INFLIGHT = 0
    mod.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=depth, base_fee=BASEFEE)
    assert mod.ladder_depth(ADDR) == depth
    assert [e[0] for e in mod._VOLLEY_LADDER[ADDR]["q"]][0] == start_nonce
    return rpc


def race(mod, fail=False, start_nonce=100):
    """Проба ИЗЪЯТА и ещё летит; параллельно идёт ПОЛНАЯ пересборка (путь аудита).

    -> (первый nonce новой лестницы, хэш пробы, сессия)
    """
    rpc = stand(mod, start_nonce=start_nonce)
    gate, entered = threading.Event(), threading.Event()
    sess = _BlockingSess(gate, entered, fail=fail)
    _orig = mod._seq_session
    mod._seq_session = lambda url: sess
    out = {}

    def _fire():
        out["h"] = mod.send_presigned_probe(rpc, cfg, ADDR, broadcast_raw_urls=[SEQ])

    t = threading.Thread(target=_fire, name="fire-test")
    try:
        t.start()
        assert entered.wait(5.0), "проба не дошла до сетевого стыка — стенд не воспроизводит окно"
        # ОКНО: запись изъята, байты у секвенсера, ответа ещё нет. Узел про них не знает.
        assert rpc.pending_nonce == start_nonce, "стенд подсказал пересборке ответ через узел"
        mod.invalidate_ladder(ADDR)                       # ровно как аудит перед пересборкой
        # tip здесь не двигаем ВООБЩЕ (право переезда не нужно): проверяется ТОЧКА СТАРТА
        # номеров, и стенд обязан оставаться совместимым с прежней редакцией — иначе «красный»
        # был бы про несуществующий аргумент, а не про коллизию.
        mod.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=3, base_fee=BASEFEE)
        first = mod._VOLLEY_LADDER[ADDR]["q"][0][0]
    finally:
        gate.set()
        t.join(5.0)
        mod._seq_session = _orig
    return first, out.get("h"), sess


# --- 1. ЭФФЕКТ: пересборка В ОКНЕ отправки не переиспользует занятый номер ---------------
first, h, sess = race(ex)
assert h is not None, "проба не отправилась — стенд не про то"
assert len(sess.posts) == 1, sess.posts
assert first == 101, ("новая лестница села на ЗАНЯТЫЙ номер %d (проба 100 в полёте, pending "
                      "узла отстаёт) — это и есть «replacement transaction underpriced»" % first)
assert [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]] == [101, 102, 103]
print("OK 1/4: пересборка в окне отправки стартует со 101 — занятый 100 не переиспользован")

# --- 2. ПОЗИТИВНЫЙ КОНТРОЛЬ: прежняя редакция на том же стенде повторяет номер -----------
SRC = (ROOT / "chain/execute.py").read_text()
NEW = ('        _VOLLEY_NONCE[addr] = max(int(_VOLLEY_NONCE.get(addr, 0)), int(nonce) + 1)\n'
       '    # ПУТЬ РАССЫЛКИ ВЫБРАН ЗАМЕРОМ')
OLD = '    # ПУТЬ РАССЫЛКИ ВЫБРАН ЗАМЕРОМ'
assert SRC.count(NEW) == 1, "якорь контроля разошёлся с продуктом — контроль был бы фикцией"
legacy = types.ModuleType("chain._execute_legacy_nonce")
legacy.__file__ = str(ROOT / "chain/execute.py")
sys.modules[legacy.__name__] = legacy
exec(compile(SRC.replace(NEW, OLD), legacy.__file__, "exec"), legacy.__dict__)

lfirst, lh, lsess = race(legacy)
assert lh is not None and len(lsess.posts) == 1
assert lfirst == 100, ("КОНТРОЛЬ ПРОВАЛЕН: прежняя редакция на этом стенде тоже ушла на %d — "
                       "значит раздел 1 ничего не доказывает" % lfirst)
print("OK 2/4: КОНТРОЛЬ — прежняя редакция (сдвиг счётчика ПОСЛЕ отправки) берёт занятый 100")

# --- 3. СИММЕТРИЯ: отказ рассылки возвращает и запись, И номер --------------------------
# Иначе счётчик остаётся на nonce+1 при НЕОТПРАВЛЕННОЙ пробе, и следующая пересборка оставляет
# ДЫРУ: вся нога виснет до таймаута коллектора (180с).
rpc = stand(ex, start_nonce=200)
gate, entered = threading.Event(), threading.Event()
sess = _BlockingSess(gate, entered, fail=True)
_o = ex._seq_session
ex._seq_session = lambda url: sess
try:
    gate.set()                                   # отказ отдаём сразу
    h2 = ex.send_presigned_probe(rpc, cfg, ADDR, broadcast_raw_urls=[SEQ])
finally:
    ex._seq_session = _o
assert h2 is None, "отказ рассылки выдан за успех"
assert [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]] == [200, 201, 202, 203], \
    "запись не возвращена на место: %s" % [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]]
assert ex._VOLLEY_NONCE[ADDR] == 200, \
    "счётчик не откатан (%d) — пересборка оставит дыру в nonce" % ex._VOLLEY_NONCE[ADDR]
ex.invalidate_ladder(ADDR)
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=2, base_fee=BASEFEE)
assert [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]] == [200, 201], \
    "после отказа пересборка оставила дыру: %s" % [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]]
print("OK 3/4: отказ рассылки возвращает и запись, и счётчик — дыры в nonce не возникает")

# --- 4. ДРЕЛЬ ВО ВРЕМЕНИ: «узел ожил» и «нога перезапущена» ------------------------------
# 4a. Узел ДОГНАЛ (pending уже включает разосланное) — берём максимум, а не локальный.
rpc = stand(ex, start_nonce=300)
ex._VOLLEY_NONCE[ADDR] = 301
rpc.pending_nonce = 305                       # узел увидел больше, чем знаем мы
ex.invalidate_ladder(ADDR)
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=2, base_fee=BASEFEE)
assert [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]] == [305, 306], \
    "узел ожил, а мы сели ниже его pending: %s" % [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]]
# 4b. NO-RECEIPT снял счётчик (ручка volley_invalidate_nonce) — идём с цепи, как прежде.
ex.volley_invalidate_nonce(ADDR)
ex.invalidate_ladder(ADDR)
rpc.pending_nonce = 302
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=2, base_fee=BASEFEE)
assert [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]] == [302, 303], \
    "после NO-RECEIPT нога не вернулась к цепи: %s" % [e[0] for e in ex._VOLLEY_LADDER[ADDR]["q"]]
print("OK 4/4: узел ожил -> берём его pending; NO-RECEIPT снял счётчик -> идём с цепи")

print("=== ОКНО КОЛЛИЗИИ ЗАКРЫТО: счётчик двигается в момент ИЗЪЯТИЯ, откатывается при отказе ===")
