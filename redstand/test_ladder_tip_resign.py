"""B2: смена тира tip обязана ДОЕЗЖАТЬ до подписанных байтов лестницы.

ДЕФЕКТ (ревью 12.08). В `presign_ladder` годность лестницы решала строка
    compat = st["gas"] == gas and st["max_fee"] >= max_fee
tip в сравнении не участвовал ВООБЩЕ, а `max_fee = base_fee*MULT + tip`, поэтому смена тира на
10 000 wei тонула в шуме базы (база множится на 20). Следствие: ручка тира не двигала уже
подписанные лестницы НИ ВВЕРХ, НИ ВНИЗ — то есть весь Ш24/Ш24b жил бы только на свежих ногах, а
«откат» тира не снимал бы механизм. Тот же слепой к tip вердикт стоял и в аудите цены.

ПРИЁМКА ЗДЕСЬ — ПРО ЭФФЕКТ: tip читается ИЗ ПОДПИСАННЫХ БАЙТОВ (декод типизированной tx), а не
из нашей же бухгалтерии, и сверяется с ИЗМЕРЕННЫМ классом транзита. Подменён только сетевой стык
(web3-объект ноды) — НИЖЕ дефекта; подпись, сборка лестницы, сверка тира и планировщик
пересборки исполняются настоящие.

ПОЗИТИВНЫЙ КОНТРОЛЬ прогоняется машинно В ЭТОМ ЖЕ ФАЙЛЕ: исходник execute.py с ПРЕЖНЕЙ строкой
compat грузится как отдельный модуль и проходит тот же стенд — лестница остаётся на старом tip.
"""
import pathlib
import sys
import threading
import types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import chain.execute as ex                                      # noqa: E402
import chain.fire_engine as fe                                  # noqa: E402
from eth_account import Account                                 # noqa: E402
from eth_account.typed_transactions import TypedTransaction     # noqa: E402
from hexbytes import HexBytes                                   # noqa: E402

KEY = "0x" + "42" * 32
ACCT = Account.from_key(KEY)
ADDR = ACCT.address
GAS = 3_000_000
BASEFEE = int(0.005e9)
CLASS_A = 1_000_000                       # измеренный класс транзита (49834406 и ещё три блока)
CLASS_B = 1_500_000                       # класс уехал


class _Eth:
    account = Account                     # шов НИЖЕ дефекта: подпись НАСТОЯЩАЯ, eth_account

    def __init__(self, outer):
        self._o = outer

    def get_block(self, _b):
        return {"baseFeePerGas": self._o.base_fee}

    def get_balance(self, _a):
        return self._o.balance_wei

    def get_transaction_count(self, _a, _blk="pending"):
        return self._o.pending_nonce

    def send_raw_transaction(self, raw):
        self._o.sent.append(raw)
        return b"\x00"


class _W3:
    def __init__(self, outer):
        self.eth = _Eth(outer)


class _Rpc:
    chain_id = 8453

    def __init__(self):
        self.sent = []
        self.pending_nonce = 0
        self.base_fee = BASEFEE
        self.balance_wei = int(0.008 * 10**18)

    def _web3(self):
        return _W3(self)


cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)


def tip_of_bytes(raw_hex) -> int:
    """maxPriorityFeePerGas ИЗ ПОДПИСАННЫХ БАЙТОВ — именно он решает позицию в суб-блоке."""
    d = TypedTransaction.from_bytes(HexBytes(raw_hex)).as_dict()
    v = d["maxPriorityFeePerGas"]
    return int(v, 16) if isinstance(v, str) else int(v)


def fresh(mod=ex):
    mod._VOLLEY_LOCKS[ADDR] = threading.Lock()
    mod.invalidate_ladder(ADDR)
    mod._VOLLEY_NONCE.pop(ADDR, None)
    mod._VOLLEY_BAL.pop(ADDR, None)
    mod._VOLLEY_GAS_USD = 0.0
    mod._INFLIGHT = 0
    return _Rpc()


def latch_class(wei, mod=ex, t0=0.0):
    """Довести ИЗМЕРЕННЫЙ класс до защёлки: час замеров под настоящим гистерезисом."""
    mod._TIP_CLASS_WIN.clear()
    mod._TIP_CLASS_LATCHED = 0
    mod._TIP_CLASS_SWITCH_TS = 0.0
    mod._TIP_TIERS_LIVE = tuple(mod._PROBE_TIP_TIERS)
    t = t0
    for _ in range(400):
        mod.refresh_tip_tiers(now=t, dom_wei=wei)
        t += 10.0
    return t


def leg_for(cls, mod=ex):
    """Нога, чей тир РОВНО равен классу транзита (портфель тиров: одна из полос — сам класс)."""
    for i in range(4096):
        a = Account.from_key("0x" + ("%064x" % (i + 1))).address
        if mod.probe_tip_for(a) == cls:
            return a, "0x" + ("%064x" % (i + 1))
    raise AssertionError("в наборе %s нет полосы, равной классу %d" % (mod.probe_tip_tiers(), cls))


# --- 1. ЭФФЕКТ: подписанный tip = ИЗМЕРЕННЫЙ класс транзита ------------------------------
latch_class(CLASS_A)
assert CLASS_A in ex.probe_tip_tiers(), ex.probe_tip_tiers()
LEG, LKEY = leg_for(CLASS_A)
ex._VOLLEY_LOCKS[LEG] = threading.Lock()
ex.invalidate_ladder(LEG)
ex._VOLLEY_NONCE.pop(LEG, None)
ex._VOLLEY_BAL.pop(LEG, None)
rpc = _Rpc()
n = ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=6)
assert n == 6, n
tips = {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]}
assert tips == {CLASS_A}, "в байтах не класс транзита: %s" % tips
print("OK 1/6: 6 проб подписаны tip=%d wei — РОВНО измеренный класс транзита" % CLASS_A)

# --- 2. КЛАСС УЕХАЛ -> уехали и БАЙТЫ (то, чего не делал прежний compat) ----------------
old_q = ex._VOLLEY_LADDER[LEG]["q"]
latch_class(CLASS_B, t0=100_000.0)
assert CLASS_B in ex.probe_tip_tiers(), ex.probe_tip_tiers()
new_tip = ex.probe_tip_for(LEG)              # тир ЭТОЙ ноги в новом наборе, боевой путь
assert new_tip == CLASS_B, ("нога не встала в измеренный класс: %d при классе %d (портфель %s)"
                            % (new_tip, CLASS_B, ex.probe_tip_tiers()))
reason = ex.ladder_resign_reason(LEG, BASEFEE, new_tip)
assert reason == "tip", "смена тира не признана поводом к пересборке: %r" % reason
# ДОТОЧКА СКЛАДА (боевой вызов refill-петли движка: без tip=, без права переезда) обязана
# оставить байты на СТАРОМ тире — иначе потолок переезда обходится вторым путём (блокер 12.08).
ex._VOLLEY_LADDER[LEG]["q"] = ex._VOLLEY_LADDER[LEG]["q"][:2]      # склад просел ниже порога
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=6)
held = {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]}
assert held == {CLASS_A}, "доточка склада увезла тир мимо потолка: %s" % held
assert ex.ladder_depth(LEG) == 6, "склад не доточен вовсе: %d" % ex.ladder_depth(LEG)
# А ПЕРЕЕЗД делает аудит — он же и держит потолок (invalidate + refill с правом).
ex.invalidate_ladder(LEG)
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=6, allow_tip_resign=True)
got = {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]}
assert got == {CLASS_B}, "БАЙТЫ остались на старом tip: %s (это и был дефект)" % got
assert ex._VOLLEY_LADDER[LEG]["q"] is not old_q, "лестница не пересобрана — вернулись старые байты"
assert ex.ladder_resign_reason(LEG, BASEFEE, new_tip) is None, "после пересборки всё ещё протухшая"

# 2b. КЛАСС УЕХАЛ ВНИЗ — случай, где прежний compat был слеп ПО ПОСТРОЕНИЮ: max_fee подписанной
# лестницы остаётся >= нового, значит «годна», и нога навсегда стоит ВЫШЕ класса, то есть в
# голове суб-блока, где цена ещё старая (дефект #91).
latch_class(CLASS_A, t0=1_000_000.0)
down_tip = ex.probe_tip_for(LEG)
assert down_tip == CLASS_A, down_tip
assert ex.ladder_resign_reason(LEG, BASEFEE, down_tip) == "tip", "снижение класса не замечено"
ex.invalidate_ladder(LEG)
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=6, allow_tip_resign=True)
assert {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]} == {CLASS_A}, \
    "лестница осталась ВЫШЕ класса — нас снова выносит в голову суб-блока"

# 2c. МИКРО-СМЕНА (1.0e6 -> 1.01e6, ровно наблюдение блоков 49880361/49887640) на фоне дрейфа
# базы: 10 000 wei тира против 20 000 wei шума базы×20 — прежний вердикт тонул именно здесь.
rpc.base_fee = BASEFEE - 1_000
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=6, tip=1_010_000)
assert {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]} == {1_010_000}, \
    "микро-смена тира утонула в шуме базы — ручка тира снова ничего не двигает"
rpc.base_fee = BASEFEE
print("OK 2/6: класс %d -> %d -> %d и микро-шаг до 1.01e6 доехали до ПОДПИСАННЫХ БАЙТОВ "
      "(compat больше не глотает смену тира ни вверх, ни вниз)" % (CLASS_A, CLASS_B, CLASS_A))

# --- 3. ПЕРЕСБОРКА НЕ РОЖДАЕТ ДЫР И КОЛЛИЗИЙ nonce --------------------------------------
# Нога стреляла: локальный счётчик ушёл вперёд, а pending узла ОТСТАЁТ (узел ещё не увидел
# разосланное). Пересборка обязана начинаться с максимума, иначе новая лестница садится на
# ЗАНЯТЫЕ номера — это и есть «replacement transaction underpriced».
rpc.pending_nonce = 10
ex.invalidate_ladder(LEG)
ex._VOLLEY_NONCE[LEG] = 17                                   # 7 проб уже в полёте
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=5, tip=CLASS_A)
q = ex._VOLLEY_LADDER[LEG]["q"]
assert [e[0] for e in q] == list(range(17, 22)), \
    "пересборка села на занятые номера: %s (pending узла %d)" % ([e[0] for e in q], rpc.pending_nonce)
# а без локального счётчика (после NO-RECEIPT его снимают) — прежнее поведение, с цепи
ex.invalidate_ladder(LEG)
ex.volley_invalidate_nonce(LEG)
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=3, tip=CLASS_A)
assert [e[0] for e in ex._VOLLEY_LADDER[LEG]["q"]] == [10, 11, 12], "после NO-RECEIPT nonce не с цепи"
print("OK 3/6: пересборка стартует с max(локальный, pending) — занятые номера не переиспользуются")

# --- 4. ПОЗИТИВНЫЙ КОНТРОЛЬ: прежняя редакция compat на ТОМ ЖЕ стенде теряет смену тира --
SRC = (ROOT / "chain/execute.py").read_text()
NEW = ('        _tip_moved = bool(st and int(st.get("tip", -1)) != int(tip_wei))\n'
       '        compat = bool(st and st["gas"] == int(gas) and st["max_fee"] >= max_fee\n'
       '                      and not _tip_moved and not _latched)')
OLD = ('        _tip_moved = False\n'
       '        compat = bool(st and st["gas"] == int(gas) and st["max_fee"] >= max_fee\n'
       '                      and not _latched)')
assert SRC.count(NEW) == 1, "якорь контроля разошёлся с продуктом — контроль был бы фикцией"
legacy = types.ModuleType("chain._execute_legacy_compat")
legacy.__file__ = str(ROOT / "chain/execute.py")
sys.modules[legacy.__name__] = legacy
exec(compile(SRC.replace(NEW, OLD), legacy.__file__, "exec"), legacy.__dict__)


def legacy_bytes(first_tip, second_tip, base2=BASEFEE):
    """Прежняя редакция на ТОМ ЖЕ стенде: подписать первым tip, затем заказать второй."""
    lrpc = fresh(legacy)
    legacy._VOLLEY_LOCKS[LEG] = threading.Lock()
    legacy.invalidate_ladder(LEG)
    legacy._VOLLEY_NONCE.pop(LEG, None)
    legacy._VOLLEY_BAL.pop(LEG, None)
    legacy.presign_ladder(lrpc, cfg, LEG, LKEY, GAS, depth=4, tip=first_tip)
    assert {tip_of_bytes(e[1]) for e in legacy._VOLLEY_LADDER[LEG]["q"]} == {first_tip}
    lrpc.base_fee = base2
    legacy.presign_ladder(lrpc, cfg, LEG, LKEY, GAS, depth=4, tip=second_tip)
    return {tip_of_bytes(e[1]) for e in legacy._VOLLEY_LADDER[LEG]["q"]}


# 4a. класс уехал ВНИЗ: прежний вердикт «max_fee >= нужного» держит лестницу годной вечно
stuck = legacy_bytes(CLASS_B, CLASS_A)
assert stuck == {CLASS_B}, \
    ("КОНТРОЛЬ ПРОВАЛЕН: прежняя редакция на этом стенде тоже уехала вниз (%s) — значит раздел 2 "
     "не доказывает ничего" % stuck)
# 4b. микро-шаг вверх на фоне дрейфа базы: 10 000 wei тира тонут в 20 000 wei базы×20
stuck2 = legacy_bytes(CLASS_A, 1_010_000, base2=BASEFEE - 1_000)
assert stuck2 == {CLASS_A}, \
    "КОНТРОЛЬ ПРОВАЛЕН: прежняя редакция поймала микро-шаг тира (%s)" % stuck2
print("OK 4/6: КОНТРОЛЬ — прежний compat на том же стенде оставляет %d wei и при снижении класса,"
      " и при шаге +10 000 wei (обе регрессии ловятся)" % CLASS_B)

# --- 5. И ЭТО ЖЕ ЛОВИТ АУДИТ: прежний вердикт слеп к тиру -------------------------------
legacy._VOLLEY_LADDER[LEG]["tip"] = CLASS_A                  # как записала прежняя редакция
assert legacy.ladder_resign_needed(LEG, BASEFEE, CLASS_B) is True, \
    "в контрольном модуле вердикт уже новый — подмена сделана не там"
_saved_state = dict(ex._VOLLEY_LADDER.get(LEG) or {})
ex._VOLLEY_LADDER[LEG] = {"q": [(0, "0x", None)], "gas": GAS,
                          "max_fee": BASEFEE * ex._VOLLEY_PRESIGN_MAXFEE_MULT + CLASS_A,
                          "ts": 0.0}                          # ЛЕСТНИЦА БЕЗ ПОЛЯ tip (до правки)
assert ex.ladder_resign_reason(LEG, BASEFEE, CLASS_A) == "tip", \
    "лестница из прежнего формата (без записанного tip) не пересобирается — тир останется слепым"
ex._VOLLEY_LADDER[LEG] = _saved_state or {"q": [], "gas": GAS, "max_fee": 1, "tip": CLASS_A,
                                          "ts": 0.0}
print("OK 5/6: аудит видит смену тира, а лестница СТАРОГО формата (без поля tip) объявляется "
      "протухшей — переезд не застревает на рестарте")

# --- 6. ОТКАТ СНИМАЕТ МЕХАНИЗМ ------------------------------------------------------------
# Ручка динамики возвращает статический набор, и это доезжает до БАЙТОВ: нога пересобирается
# обратно в статическую полосу. Не «параметр подкрутили» — механизм снят.
latch_class(CLASS_B, t0=2_000_000.0)                          # набор УЕХАЛ на измеренный класс
ex.invalidate_ladder(LEG)
ex.volley_invalidate_nonce(LEG)
ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=3, allow_tip_resign=True)
assert {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]} == {CLASS_B}
_sv = ex._TIP_CLASS_DYNAMIC
try:
    ex._TIP_CLASS_DYNAMIC = False
    static_tip = ex.probe_tip_for(LEG)
    assert static_tip != CLASS_B, "стенд не различает динамический и статический набор"
    assert static_tip in ex._PROBE_TIP_TIERS, static_tip
    ex.invalidate_ladder(LEG)
    ex.volley_invalidate_nonce(LEG)
    ex.presign_ladder(rpc, cfg, LEG, LKEY, GAS, depth=3, allow_tip_resign=True)
    assert {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG]["q"]} == {static_tip}, \
        "с выключённой динамикой байты всё ещё несут измеренный класс"
finally:
    ex._TIP_CLASS_DYNAMIC = _sv
print("OK 6/6: SPEC_FIRE_TIP_CLASS_DYNAMIC=0 -> байты снова несут статический тир %d wei"
      % static_tip)


# --- 7. ПОРЯДОК ПРИЧИН: немая нога лечится ПЕРВЫМ проходом, а не по остаточному принципу --
# Блокер ревью 12.08. Причины «price» и «tip» обязаны быть НЕЗАВИСИМЫ: у ноги, чья лестница
# уже не включаема (или запирает гейт патрона), одновременная смена класса не имеет права
# перевести её в повод «tip» — он стоит ПОД потолком, и немота растягивается на ~28 минут.
# Стенд ровно тот, что гонял ревьюер: 110 ног, три из них немые, класс уехал в тот же момент.
def stand_110(mod=ex, mute_lo=(3, 17), mute_hi=(59,)):
    """110 ног с НАСТОЯЩИМИ лестницами (depth=1, подпись честная). -> (адреса, немые, ключи)."""
    keys = ["0x" + ("%064x" % (i + 1000)) for i in range(110)]
    addrs = [Account.from_key(k).address for k in keys]
    r = _Rpc()
    for a, k in zip(addrs, keys):
        mod._VOLLEY_LOCKS[a] = threading.Lock()
        mod.invalidate_ladder(a)
        mod._VOLLEY_NONCE.pop(a, None)
        mod._VOLLEY_BAL.pop(a, None)
        mod.presign_ladder(r, cfg, a, k, GAS, depth=1)
    mute = []
    for i in mute_lo:                       # ниже пола включаемости: tx не включается вовсе
        mod._VOLLEY_LADDER[addrs[i]]["max_fee"] = BASEFEE // 2
        mute.append(addrs[i])
    for i in mute_hi:                       # вздут: гейт патрона по худшему случаю закрыт
        stx = mod._VOLLEY_LADDER[addrs[i]]
        stx["max_fee"] = BASEFEE * mod._VOLLEY_PRESIGN_MAXFEE_MULT * 10 + int(stx["tip"])
        mute.append(addrs[i])
    return addrs, mute, keys


def legacy_module(name, subs):
    """Модуль из ТОГО ЖЕ исходника с точечной подменой — машинный позитивный контроль."""
    src = (ROOT / "chain/execute.py").read_text()
    for old, new in subs:
        assert src.count(old) == 1, "якорь контроля разошёлся с продуктом: %r" % old[:60]
        src = src.replace(old, new)
    m = types.ModuleType(name)
    m.__file__ = str(ROOT / "chain/execute.py")
    sys.modules[name] = m
    exec(compile(src, m.__file__, "exec"), m.__dict__)
    return m


latch_class(CLASS_A, t0=3_000_000.0)
ADDRS, MUTE, KEYS = stand_110()
latch_class(CLASS_B, t0=4_000_000.0)                 # класс уехал В ТОТ ЖЕ МОМЕНТ
stale, price, moved, deferred = ex.plan_ladder_resigns(ADDRS, BASEFEE, ex.probe_tip_for)
assert len(price) == 3, "немые ноги растворились в поводе «tip»: price=%d" % len(price)
assert sorted(price) == sorted(MUTE), "в price попали не те ноги: %s" % price
assert all(a not in moved for a in MUTE), "немая нога уехала ПОД потолок: %s" % moved
assert len(moved) == ex.tip_resign_cap(), "потолок переезда не сработал: %d" % len(moved)
assert deferred == 110 - 3 - len(moved), deferred
assert ex.TIP_CLASS_STATS["resign_capped"] == deferred, \
    "счётчик отложенных — сумма, а не гейдж: %r" % ex.TIP_CLASS_STATS["resign_capped"]
print("OK 7/8: на стенде 110 ног три немые лечатся ПЕРВЫМ проходом (price=3, вне потолка), "
      "переезд по тиру ограничен %d ногами" % len(moved))

# 7-КОНТРОЛЬ: прежний порядок причин на ТОМ ЖЕ стенде даёт price=0 ------------------------
ORDER_NEW = ('    st_tip = int(st.get("tip", -1))\n'
             '    want = int(base_fee) * _VOLLEY_PRESIGN_MAXFEE_MULT + st_tip\n'
             '    have = int(st.get("max_fee") or 0)\n'
             '    floor = int(base_fee) * _VOLLEY_RESIGN_FLOOR_MULT + st_tip\n'
             '    if want > 0 and (have < floor or have > want * _VOLLEY_MAXFEE_RESIGN_MULT):\n'
             '        return "price"')
# прежняя редакция: сперва tip, цена — только если по тиру лестница годна
ORDER_OLD = ('    st_tip = int(st.get("tip", -1))\n'
             '    if st_tip != int(tip_wei):\n'
             '        return "tip"\n'
             '    want = int(base_fee) * _VOLLEY_PRESIGN_MAXFEE_MULT + st_tip\n'
             '    have = int(st.get("max_fee") or 0)\n'
             '    floor = int(base_fee) * _VOLLEY_RESIGN_FLOOR_MULT + st_tip\n'
             '    if want > 0 and (have < floor or have > want * _VOLLEY_MAXFEE_RESIGN_MULT):\n'
             '        return "price"')
old_order = legacy_module("chain._execute_legacy_order", [(ORDER_NEW, ORDER_OLD)])
latch_class(CLASS_A, t0=3_000_000.0, mod=old_order)
A2, M2, _K2 = stand_110(mod=old_order)
latch_class(CLASS_B, t0=4_000_000.0, mod=old_order)
_st0, price0, moved0, _d0 = old_order.plan_ladder_resigns(A2, BASEFEE, old_order.probe_tip_for)
assert len(price0) == 0 and len(moved0) == old_order.tip_resign_cap(), \
    ("КОНТРОЛЬ ПРОВАЛЕН: прежний порядок на том же стенде уже лечит немоту (price=%d, moved=%d) "
     "— значит раздел 7 ничего не доказывает" % (len(price0), len(moved0)))
print("OK 7-КОНТРОЛЬ: прежний порядок причин даёт price=0 / moved=%d / отложено %d — три немые "
      "ноги ждали переезда по остаточному принципу" % (len(moved0), _d0))

# --- 8. ПОТОЛОК ДЕЙСТВУЕТ НА ВСЕХ ПУТЯХ ПЕРЕСБОРКИ ---------------------------------------
# Блокер ревью 12.08: смену тира умеет запускать не только аудит (у него потолок), но и фоновая
# refill-петля движка (fire_engine._refill_loop, порог глубины склада) — а она потолка не
# спрашивает. Пока это так, SPEC_FIRE_TIP_CLASS_MAX_RESIGN=0 не запрещает НИЧЕГО: нога уедет
# при первой же доточке склада. «Откат, который не откатывает, — не откат».
_sv_cap = ex._TIP_CLASS_MAX_RESIGN
try:
    ex._TIP_CLASS_MAX_RESIGN = 0                      # ОТКАТ: переезд по тиру запрещён
    latch_class(CLASS_A, t0=5_000_000.0)
    LEG2, LKEY2 = leg_for(CLASS_A)
    ex._VOLLEY_LOCKS[LEG2] = threading.Lock()
    ex.invalidate_ladder(LEG2)
    ex._VOLLEY_NONCE.pop(LEG2, None)
    ex._VOLLEY_BAL.pop(LEG2, None)
    r2 = _Rpc()
    ex.presign_ladder(r2, cfg, LEG2, LKEY2, GAS, depth=6)
    assert {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG2]["q"]} == {CLASS_A}
    latch_class(CLASS_B, t0=6_000_000.0)              # набор УЕХАЛ
    assert ex.probe_tip_for(LEG2) != CLASS_A, "стенд вырожден: тир ноги не изменился"
    # ВЫЗОВ РОВНО КАК В _refill: без tip=, без права переезда, склад просажен ниже порога
    for _ in range(5):
        ex._VOLLEY_LADDER[LEG2]["q"] = ex._VOLLEY_LADDER[LEG2]["q"][:fe.ENGINE_REFILL_AT - 1]
        ex.presign_ladder(r2, cfg, LEG2, LKEY2, GAS, base_fee=BASEFEE, log=None)
    held = {tip_of_bytes(e[1]) for e in ex._VOLLEY_LADDER[LEG2]["q"]}
    assert held == {CLASS_A}, "при MAX_RESIGN=0 доточка склада УВЕЗЛА тир: %s" % held
    assert ex.ladder_depth(LEG2) >= fe.ENGINE_REFILL_AT, "склад не доточен — огонь заперт"
    _st1, _pr1, mv1, df1 = ex.plan_ladder_resigns([LEG2], BASEFEE, ex.probe_tip_for)
    assert mv1 == [] and df1 == 1, (mv1, df1)
    print("OK 8/8: SPEC_FIRE_TIP_CLASS_MAX_RESIGN=0 -> подписанные байты остаются на %d wei "
          "после 5 доточек склада (потолок действует на ОБОИХ путях)" % CLASS_A)

    # 8-КОНТРОЛЬ: та же редакция БЕЗ пина (как было до правки) на том же стенде — байты уедут
    PIN = ('        if (st and tip is None and not allow_tip_resign\n'
           '                and int(st.get("tip", 0)) > 0 and int(st["tip"]) != int(tip_wei)):\n'
           '            TIP_CLASS_STATS["resign_pinned"] = TIP_CLASS_STATS.get("resign_pinned", 0) + 1\n'
           '            tip_wei = int(st["tip"])\n')
    nopin = legacy_module("chain._execute_legacy_nopin", [(PIN, "")])
    nopin._TIP_CLASS_MAX_RESIGN = 0
    latch_class(CLASS_A, t0=5_000_000.0, mod=nopin)
    nopin._VOLLEY_LOCKS[LEG2] = threading.Lock()
    nopin.invalidate_ladder(LEG2)
    nopin._VOLLEY_NONCE.pop(LEG2, None)
    nopin._VOLLEY_BAL.pop(LEG2, None)
    r3 = _Rpc()
    nopin.presign_ladder(r3, cfg, LEG2, LKEY2, GAS, depth=6)
    latch_class(CLASS_B, t0=6_000_000.0, mod=nopin)
    nopin._VOLLEY_LADDER[LEG2]["q"] = nopin._VOLLEY_LADDER[LEG2]["q"][:fe.ENGINE_REFILL_AT - 1]
    nopin.presign_ladder(r3, cfg, LEG2, LKEY2, GAS, base_fee=BASEFEE, log=None)
    ran = {tip_of_bytes(e[1]) for e in nopin._VOLLEY_LADDER[LEG2]["q"]}
    assert ran != {CLASS_A}, ("КОНТРОЛЬ ПРОВАЛЕН: прежняя редакция на этом стенде тоже удержала "
                              "тир (%s) — значит раздел 8 ничего не доказывает" % ran)
    print("OK 8-КОНТРОЛЬ: без пина та же доточка при MAX_RESIGN=0 увозит байты %d -> %s "
          "(ручка отката была косметикой)" % (CLASS_A, sorted(ran)))
finally:
    ex._TIP_CLASS_MAX_RESIGN = _sv_cap

# 8-ПРОВОДКА: боевой триггер доточки зовёт refill БЕЗ права переезда, аудит — С правом.
# Предел стенда назван честно: само замыкание `_refill` живёт внутри `_engine`, который поднимает
# треды, поэтому здесь проверяется (а) АРНОСТЬ боевого вызова в НАСТОЯЩЕЙ петле fire_engine и
# (б) проводка права в исходнике аудита.
_calls = []


def _rec_refill(*a, **kw):
    _calls.append((a, kw))
    _eng._stop.set()


_eng = fe.FireEngine(legs_fn=lambda: ["0xleg"], send_fn=lambda a: None,
                     refill_fn=_rec_refill, depth_fn=lambda a: 0, audit_fn=None,
                     sleep=lambda *_: None)
_eng._refill_loop()
assert _calls and _calls[0] == (("0xleg",), {}), \
    "refill-петля движка передаёт право переезда: %r" % (_calls,)
_ablk = (ROOT / "chain/spec_fire_live.py").read_text()
_i = _ablk.index("def _refill(addr")
_sig = _ablk[_i:_ablk.index("def _audit(", _i)]
assert "allow_tip_resign=False" in _sig, "у _refill право переезда включено по умолчанию"
_aud = _ablk[_ablk.index("def _audit(", _i):_ablk.index("def _combat_url(", _i)]
assert "allow_tip_resign=True" in _aud, "аудит не передаёт право переезда — флот не переедет НИКОГДА"
print("ПРОВОДКА: refill-петля движка зовёт _refill(addr) без права, аудит — с правом; "
      "потолок спрашивается ровно там, где он есть")

print("=== B2 ЗАКРЫТ: ручка тира двигает ПОДПИСАННЫЕ БАЙТЫ, пересборка не трогает занятые nonce ===")
