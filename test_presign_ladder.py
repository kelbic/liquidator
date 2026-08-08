"""Юниты пред-подписанной лестницы проб (#32, 07.08).

ЗАЧЕМ ОНА. Замер боевого send-пути: подпись p50=4мс/p99=36мс, а сам вызов держит тред 60мс
(p50). Восемь тредов => потолок ~133 отправки/с = эквивалент ~20 стволов: лестница плотности
упиралась в НАШ процесс, а не в сеть и не в число ключей. Проба доски всегда один и тот же
байт (план живёт в контракте), поэтому её можно подписать заранее.

ГЛАВНОЕ, ЧТО ТУТ ПРОВЕРЯЕТСЯ:
  1. в горячем пути НЕТ подписи (позитивный контроль: подпись роняет тест, если её позвали);
  2. nonce'ы расходуются СТРОГО ПОДРЯД — пропуск это дыра, вешающая всю ногу до её закрытия;
  3. неудачная рассылка ВОЗВРАЩАЕТ запись на место, а «already known» — нет (байты у секвенсера);
  4. гейт баланса по ХУДШЕМУ случаю, списание по факту (иначе нога глушит себя, см. 09a6630).
Без сети и без web3.
"""
import os
import sys
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.execute as ex
from eth_account import Account

KEY = "0x" + "42" * 32
ACCT = Account.from_key(KEY)
ADDR = ACCT.address
GAS = 3_000_000
BASEFEE = int(0.005e9)


class _FakeEth:
    account = Account

    def __init__(self, outer):
        self._outer = outer

    def get_block(self, _b):
        return {"baseFeePerGas": BASEFEE}

    def get_balance(self, _a):
        # #45: патрон обязан быть ИЗВЕСТЕН, иначе гейт закрыт (незнание закрывает гард).
        # presign_ladder греет кэш вне пути огня — здесь это и проверяется по факту.
        self._outer.bal_reads += 1
        return self._outer.balance_wei

    def get_transaction_count(self, _a, _blk="pending"):
        # ПОДТЕКАЮЩИЙ pending: узел ещё не увидел разосланные пробы. Ровно эта отсталость
        # и делает пересборку лестницы от pending дырой в nonce (см. тест доточки).
        return self._outer.pending_nonce

    def send_raw_transaction(self, raw):
        self._outer.sent.append(raw)
        if self._outer.fail_next:
            self._outer.fail_next = False
            raise RuntimeError(self._outer.fail_msg)
        return b"\x00"


class _FakeW3:
    def __init__(self, outer):
        self.eth = _FakeEth(outer)


class _FakeRpc:
    chain_id = 8453

    def __init__(self):
        self.sent = []
        self.fail_next = False
        self.fail_msg = "connection reset"
        self.pending_nonce = 0
        self.balance_wei = int(0.008 * 10**18)     # заправка ноги флота
        self.bal_reads = 0

    def _web3(self):
        return _FakeW3(self)


cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)


SEQ = "https://seq.test/invalid"


def fresh():
    import threading
    ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
    ex.invalidate_ladder(ADDR)
    ex._VOLLEY_NONCE.pop(ADDR, None)
    ex._VOLLEY_BAL.pop(ADDR, None)
    ex._VOLLEY_GAS_USD = 0.0
    ex._INFLIGHT = 0
    return _FakeRpc()


# ---- 1. Лестница строится: подряд, тем самым байтом, с запасом по цене ----
rpc = fresh()
n = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=8, start_nonce=100, log=None)
assert n == 8, n
st = ex._VOLLEY_LADDER[ADDR]
assert [e[0] for e in st["q"]] == list(range(100, 108)), "nonce'ы обязаны идти подряд"
assert st["gas"] == GAS
assert st["max_fee"] == BASEFEE * ex._VOLLEY_PRESIGN_MAXFEE_MULT + int(cfg.tip_gwei * 1e9)
# подписанные байты обязаны нести РОВНО пробу доски и наш адрес назначения
_decoded = Account.recover_transaction(st["q"][0][1])
assert _decoded == ADDR, "подпись обязана принадлежать ноге"
assert ex.ladder_depth(ADDR) == 8
print("UNIT_OK: лестница из %d проб, nonce подряд, maxFee %.3f gwei (запас x%d к basefee)" % (
    n, st["max_fee"] / 1e9, ex._VOLLEY_PRESIGN_MAXFEE_MULT))

# ---- 2. Идемпотентность: годная лестница не пересобирается ----
_q_before = st["q"]
n2 = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=8, start_nonce=100)
assert n2 == 8 and ex._VOLLEY_LADDER[ADDR]["q"] is _q_before, "подпись не должна жечься впустую"
print("UNIT_OK: годная лестница не пересобирается (CPU подписи не жжём)")

# ---- 3. ГЛАВНОЕ: в горячем пути НЕТ подписи ----
_real_sign = Account.sign_transaction


def _boom(*a, **k):
    raise AssertionError("подпись в горячем пути — вся затея бессмысленна")


Account.sign_transaction = _boom
try:
    h = ex.send_presigned_probe(rpc, cfg, ADDR)
    assert h is not None, "проба обязана уйти"
finally:
    Account.sign_transaction = _real_sign
assert len(rpc.sent) == 1
assert ex.ladder_depth(ADDR) == 7
print("UNIT_OK: горячий путь только рассылает готовые байты (подпись бы уронила тест)")

# ---- 4. Строгий порядок nonce ----
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, start_nonce=50)
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes


def _nonce_of(raw_hex):
    """nonce ИЗ ПОДПИСАННЫХ БАЙТОВ, а не из нашей же очереди: иначе тест проверял бы
    собственную бухгалтерию, а не то, что реально уходит в сеть."""
    d = TypedTransaction.from_bytes(HexBytes(raw_hex)).as_dict()
    v = d["nonce"]
    return int(v, 16) if isinstance(v, str) else int(v)


_order = []
for _ in range(5):
    ex.send_presigned_probe(rpc, cfg, ADDR)
_order = [_nonce_of(r) for r in rpc.sent]
assert _order == list(range(50, 55)), _order
assert ex.send_presigned_probe(rpc, cfg, ADDR) is None, "пустая лестница обязана молчать"
print("UNIT_OK: nonce расходуются строго подряд %s, пустая лестница молчит" % _order)

# ---- 5. Сбой рассылки ВОЗВРАЩАЕТ запись (дыра в nonce вешает ногу) ----
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=4, start_nonce=10)
rpc.fail_next = True
assert ex.send_presigned_probe(rpc, cfg, ADDR) is None, "сбой обязан вернуть None"
assert ex.ladder_depth(ADDR) == 4, "запись обязана вернуться НА МЕСТО, иначе дыра в nonce"
assert ex._VOLLEY_LADDER[ADDR]["q"][0][0] == 10, "вернуться обязана в НАЧАЛО очереди"
assert ex.send_presigned_probe(rpc, cfg, ADDR) is not None
assert ex._VOLLEY_LADDER[ADDR]["q"][0][0] == 11, "после успеха очередь двигается"
print("UNIT_OK: сбой рассылки возвращает nonce на место — дыры не возникает")

# ---- 6. «already known» — запись ПОТРАЧЕНА (байты уже у секвенсера через SEND-3) ----
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=3, start_nonce=20)
rpc.fail_next = True
rpc.fail_msg = "already known"
h = ex.send_presigned_probe(rpc, cfg, ADDR)
assert ex.ladder_depth(ADDR) == 2, "already known = байты приняты, возвращать запись нельзя"
assert h is not None, "hash известен из подписи — вызывающий обязан ждать квитанцию"
print("UNIT_OK: «already known» тратит запись (SEND-3 обогнал), hash возвращается")

# ---- 7. Гейт по худшему случаю, списание по факту ----
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=60, start_nonce=0)
_mf = ex._VOLLEY_LADDER[ADDR]["max_fee"]
# нога, которой не хватает на ПОЛНЫЙ выигрыш, обязана молчать
ex._VOLLEY_BAL[ADDR] = [ex._VOLLEY_BAL_FLOOR_WEI + GAS * _mf - 1, 1e9]
assert ex.send_presigned_probe(rpc, cfg, ADDR) is None
print("UNIT_OK: гейт по худшему случаю — нога без денег на выигрыш не стреляет")
# боевая нога (0.008 ETH) обязана выдержать залп, а не десяток проб
ex._VOLLEY_BAL[ADDR] = [int(0.008e18), 1e9]
_fired = 0
for _ in range(60):
    if ex.send_presigned_probe(rpc, cfg, ADDR) is None:
        break
    _fired += 1
assert _fired == 60, "нога замолчала после %d проб — списание считается по лимиту, не по факту" % _fired
# #45: путь огня БОЛЬШЕ не списывает по эстимейту (он завышал расход no-op в ~250 раз и глушил
# ногу за ~135 проб). Кэш двигает только квитанция — вот позитивный контроль, что учёт жив:
assert ex._VOLLEY_BAL[ADDR][0] == int(0.008e18), "путь огня снова списывает по эстимейту"
ex.volley_bal_charge(ADDR, 18600 * _mf)                  # квитанция no-op пробы доски
assert ex._VOLLEY_BAL[ADDR][0] == int(0.008e18) - 18600 * _mf, \
    "позитивный контроль: списание по квитанции не двигает патрон"
print("UNIT_OK: расход по факту — нога отдала все %d проб, патрон списан квитанцией" % _fired)

# ---- 8. Гейты тира действуют и на лестницу ----
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=3, start_nonce=0)
ex._VOLLEY_GAS_USD = ex.CASCADE_TIER_GAS_USD + 1
assert ex.send_presigned_probe(rpc, cfg, ADDR) is None
ex._VOLLEY_GAS_USD = 0.0
# 08.08 (решение kelbic): боевая tx в полёте больше НЕ глушит лестницу по умолчанию —
# немота съедала 75% горячего окна каскада. Прежнее поведение живо под флагом отката.
ex._INFLIGHT = 1
_sv = ex._ENGINE_PROBE_IGNORE_INFLIGHT
try:
    assert ex.send_presigned_probe(rpc, cfg, ADDR) is not None, \
        "проба лестницы обязана лететь при боевой tx в полёте (дефолт 08.08)"
    ex._ENGINE_PROBE_IGNORE_INFLIGHT = False           # контроль отката
    assert ex.send_presigned_probe(rpc, cfg, ADDR) is None
finally:
    ex._ENGINE_PROBE_IGNORE_INFLIGHT = _sv
    ex._INFLIGHT = 0
assert ex.ladder_depth(ADDR) == 2, "дефолт тратит одну запись, откат — ни одной"
print("UNIT_OK: $-бакет глушит лестницу; inflight глушит только под флагом отката")

# ---- 9. БОЕВОЙ ПУТЬ = СЕКВЕНСЕР, а не синхронная нога Alchemy ----
# Замер 07.08: тёплая сессия до секвенсера держит тред 32.5мс (p50), Alchemy — 60мс.
# Секвенсер и есть ордерер Base, а hash пред-подписанной пробы известен из подписи, поэтому
# ответ Alchemy не нужен вовсе. Тест закрепляет это: при заданном секвенсере путь через w3
# не зовётся ВООБЩЕ (иначе тред снова держат лишние 27мс, и весь замер бессмысленен).
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=4, start_nonce=0)
_posts = []
_real_post = ex._post_raw_sync


def _fake_post(url, raw_hex, timeout=5):
    _posts.append((url, raw_hex))
    return (True, None)


ex._post_raw_sync = _fake_post
try:
    h = ex.send_presigned_probe(rpc, cfg, ADDR, broadcast_raw_urls=[SEQ])
    assert h is not None
    assert len(_posts) == 1 and _posts[0][0] == SEQ, _posts
    assert rpc.sent == [], "нога Alchemy обязана быть НЕ на пути: 60мс против 32.5мс"

    # отказ секвенсера обязан вернуть запись (инвариант «подряд» важнее скорости)
    ex._post_raw_sync = lambda u, r, timeout=5: (False, "connection reset")
    _d = ex.ladder_depth(ADDR)
    assert ex.send_presigned_probe(rpc, cfg, ADDR, broadcast_raw_urls=[SEQ]) is None
    assert ex.ladder_depth(ADDR) == _d, "отказ обязан вернуть запись — иначе дыра в nonce"

    # «already known» на пути секвенсера = байты приняты, запись потрачена
    ex._post_raw_sync = lambda u, r, timeout=5: (False, "already known")
    assert ex.send_presigned_probe(rpc, cfg, ADDR, broadcast_raw_urls=[SEQ]) is not None
    assert ex.ladder_depth(ADDR) == _d - 1
finally:
    ex._post_raw_sync = _real_post
print("UNIT_OK: боевая рассылка идёт в секвенсер (32.5мс), нога Alchemy с пути убрана")

# ---- 10. ДОТОЧКА СКЛАДА: nonce продолжается с ХВОСТА, а не от подтекающего pending ----
# Движок огня пополняет склад ФОНОМ. Если пополнение пересоберёт лестницу от pending-nonce,
# оно выдаст УЖЕ ЗАНЯТЫЕ номера: узел не видел разосланных проб, его счётчик отстал. Такая
# лестница — прямая дыра, вешающая ногу целиком.
rpc = fresh()
rpc.pending_nonce = 50                       # узел «знает» только про 50 — он отстал
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, start_nonce=50)
for _ in range(3):
    ex.send_presigned_probe(rpc, cfg, ADDR)  # 50,51,52 ушли; узел всё ещё думает, что nonce=50
_kept = list(ex._VOLLEY_LADDER[ADDR]["q"])
assert [e[0] for e in _kept] == [53, 54]
n = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5)   # без start_nonce = боевой вызов фона
_q = ex._VOLLEY_LADDER[ADDR]["q"]
assert [e[0] for e in _q] == [53, 54, 55, 56, 57], "доточка обязана продолжать с хвоста: %s" % [
    e[0] for e in _q]
assert _q[0] is _kept[0] and _q[1] is _kept[1], "уже подписанное переподписывать нельзя (CPU)"
assert [_nonce_of(e[1]) for e in _q] == [53, 54, 55, 56, 57], "и в САМИХ БАЙТАХ тоже подряд"
print("UNIT_OK: доточка склада продолжает с хвоста %s — pending отстал, дыры нет" % (
    [e[0] for e in _q],))

# позитивный контроль: склад выбран ДОСУХА — продолжаем с локального счётчика, а не с pending
for _ in range(5):
    ex.send_presigned_probe(rpc, cfg, ADDR)
assert ex.ladder_depth(ADDR) == 0
assert rpc.pending_nonce == 50, "узел по-прежнему отстал — в этом весь смысл проверки"
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=3)
_q = ex._VOLLEY_LADDER[ADDR]["q"]
assert [e[0] for e in _q] == [58, 59, 60], "с пустого склада — от локального счётчика: %s" % [
    e[0] for e in _q]
print("UNIT_OK: пустой склад продолжает с локального счётчика (58..60), не с отставшего pending")

ex.invalidate_ladder(ADDR)
assert ex.ladder_depth(ADDR) == 0
print("ПРОШЛИ: пред-подписанная лестница проб")
