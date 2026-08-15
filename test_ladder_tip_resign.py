"""#127: tip запечён в пресайн-лестницу, аудит цены к нему слеп — зеркало #104 не доезжает.

ДОКАЗАНО НЕЗАВИСИМЫМ ПРОХОДОМ ПО ЦЕПИ (15.08): эффект #104 нулевой (23.8%→23.9% совпадения с
tip транзита), у всех 110 ног ровно ОДИН tip за все пробы, раскладка 27/29/25/29 по статическим
тирам бит-в-бит. Механизм: tip вшивается в raw-подпись ОДИН раз в presign_ladder; лестницы
подписываются на прогреве, когда кольцо зеркала пусто (<_MIRROR_MIN_N) => статика; а аудит цены
(ladder_resign_needed) сверяет ТОЛЬКО max_fee с полосой допуска — разница тиров ±2M wei тонет в
base*MULT (100M wei при базе 0.005 gwei) и НЕВИДИМА. Пересборка по смене tip не наступала никогда.

ЧТО ЗАКРЕПЛЯЕТСЯ:
  1. КРАСНАЯ-НА-БАЗЕ: смена tip при годном max_fee ОБЯЗАНА триггерить пересборку (на коде до
     фикса этот assert падает — полоса max_fee расхождение тиров не видит);
  2. отсутствие поля "tip" (лестница от кода до #127 после деплоя) => пересборка (незнание
     закрывает гард: неизвестный tip = протухла);
  3. равный tip => прежнее поведение полосы max_fee БИТ-В-БИТ (сценарии из test_presign_ladder §13);
  4. поле "tip" пишется в точке установки лестницы (и рантайм, и проводка по исходнику);
  5. ДОЛИВ несёт tip ЛЕСТНИЦЫ, а не свежий probe_tip_for — иначе в одной лестнице два tip и
     поле лжёт; смена tip идёт ТОЛЬКО пересборкой (её триггерит аудит);
  6. долив лестницы БЕЗ поля tip не доливает (пересборку сделает аудит);
  7. счётчик «пересборка по tip» висит СБОКУ: исключение в нём не трогает подпись/установку/огонь
     (мутация) и по исходнику он не стоит в денежных блоках (проводка).
Без сети и без web3.
"""
import sys
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.execute as ex
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

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
        return self._outer.balance_wei

    def get_transaction_count(self, _a, _blk="pending"):
        return self._outer.pending_nonce

    def send_raw_transaction(self, raw):
        self._outer.sent.append(raw)
        return b"\x00"


class _FakeW3:
    def __init__(self, outer):
        self.eth = _FakeEth(outer)


class _FakeRpc:
    chain_id = 8453

    def __init__(self):
        self.sent = []
        self.pending_nonce = 0
        self.balance_wei = int(0.008 * 10**18)

    def _web3(self):
        return _FakeW3(self)


cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)


def fresh():
    import threading
    ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
    ex.invalidate_ladder(ADDR)
    ex._VOLLEY_NONCE.pop(ADDR, None)
    ex._VOLLEY_BAL.pop(ADDR, None)
    ex._VOLLEY_GAS_USD = 0.0
    ex._INFLIGHT = 0
    return _FakeRpc()


def _tx_field(raw_hex, field):
    """Поле ИЗ ПОДПИСАННЫХ БАЙТОВ, а не из нашей бухгалтерии: проверяем то, что уходит в сеть."""
    d = TypedTransaction.from_bytes(HexBytes(raw_hex)).as_dict()
    v = d[field]
    return int(v, 16) if isinstance(v, str) else int(v)


TIP0 = ex.probe_tip_for(ADDR)          # тир этой ноги (зеркало пусто => статика), 1.1M wei
assert TIP0 == 1_100_000, "предпосылка дрели: тир ноги 0x...D025 = 1.1M wei, а вышло %d" % TIP0
TIP2 = 1_775_000                       # соседний тир: ±2M wei — ровно то, что полоса не видит

# ---- 1. КРАСНАЯ-НА-БАЗЕ: смена tip при годном max_fee обязана требовать пересборку ----
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=8, start_nonce=0, log=None)
st = ex._VOLLEY_LADDER[ADDR]
# предпосылка: max_fee лестницы ГОДЕН для полосы при новом tip (иначе тест мерил бы полосу)
_floor = BASEFEE * ex._VOLLEY_RESIGN_FLOOR_MULT + TIP2
_want = BASEFEE * ex._VOLLEY_PRESIGN_MAXFEE_MULT + TIP2
assert _floor <= st["max_fee"] <= _want * ex._VOLLEY_MAXFEE_RESIGN_MULT, \
    "предпосылка сломана: max_fee вне полосы, тест мерит не tip-слепоту"
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP2) is True, \
    ("КРАСНАЯ-НА-БАЗЕ (#127): tip ноги сменился %d -> %d, max_fee в полосе допуска — аудит "
     "слеп, лестница со старым tip не пересобирается НИКОГДА" % (TIP0, TIP2))
print("UNIT_OK: смена tip %d -> %d при годном max_fee триггерит пересборку" % (TIP0, TIP2))

# ---- 2. Отсутствие поля tip (лестница от кода до #127) => пересборка ----
_saved_tip = st.pop("tip")             # KeyError здесь = поле не пишется вовсе (см. п.4)
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is True, \
    "лестница без поля tip (деплой поверх старого кода) обязана протухнуть: незнание закрывает гард"
st["tip"] = _saved_tip
print("UNIT_OK: лестница без поля tip протухает (незнание закрывает гард)")

# ---- 3. Равный tip => полоса max_fee БИТ-В-БИТ (сценарии test_presign_ladder §13) ----
_norm = st["max_fee"]
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is False, "аудит гоняет здоровую ногу"
st["max_fee"] = _norm * 50                                   # вздутая — гейт патрона закрыт
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is True
st["max_fee"] = BASEFEE // 2                                 # ниже базы — tx невключаема
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is True
st["max_fee"] = _norm // 4                                   # включаема, но без резерва — не трогаем
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is False
st["max_fee"] = int(_norm * 2.5)                             # рябь в пределах множителя
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is False
st["max_fee"] = _norm
_q_saved = st["q"]
st["q"] = []                                                 # пустую поднимает триггер глубины
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP0) is False
st["q"] = _q_saved
ex._VOLLEY_LADDER.pop("0x" + "ab" * 20, None)
assert ex.ladder_resign_needed("0x" + "ab" * 20, BASEFEE, TIP0) is False, "нет лестницы — нет аудита"
print("UNIT_OK: при равном tip полоса max_fee ведёт себя бит-в-бит (§13)")

# ---- 4. Поле tip пишется в точке установки: рантайм + проводка по исходнику ----
assert st.get("tip") == TIP0, "точка установки не записала tip: %r" % (st.get("tip"),)
assert _tx_field(st["q"][0][1], "maxPriorityFeePerGas") == TIP0, \
    "поле tip разошлось с БАЙТАМИ подписи — учёт лжёт"
src = _pl.Path("chain/execute.py").read_text()
_ins = src.index("_VOLLEY_LADDER[addr] = {")
_ins_line = src[_ins:src.index("}", _ins) + 1]
assert '"tip"' in _ins_line, "точка установки лестницы не пишет поле tip: %s" % _ins_line
print("UNIT_OK: поле tip пишется в точке установки и равно tip из подписанных байтов")

# ---- 5. ДОЛИВ несёт tip ЛЕСТНИЦЫ, а не свежий probe_tip_for ----
# Лестница подписана явным tip=TIP2 (контракт параметра tip= прежний — п.4 задачи), а фоновый
# долив идёт БЕЗ tip. Свежий probe_tip_for(ADDR)=TIP0 < TIP2, так что compat-гейт долив пускает
# (max_fee лестницы выше свежего) — ровно та щель, где до фикса в одну лестницу мешались два tip.
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, start_nonce=0, tip=TIP2)
st = ex._VOLLEY_LADDER[ADDR]
assert st.get("tip") == TIP2, "явный tip= обязан сесть в поле: %r" % (st.get("tip"),)
assert _tx_field(st["q"][0][1], "maxPriorityFeePerGas") == TIP2, "явный tip= не в байтах"
for _ in range(2):
    assert ex.send_presigned_probe(rpc, cfg, ADDR) is not None
n = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5)     # боевой фоновый долив: без tip
assert n == 5, n
st = ex._VOLLEY_LADDER[ADDR]
assert st.get("tip") == TIP2, "долив сменил tip лестницы: %r" % (st.get("tip"),)
_tips_in_bytes = sorted({_tx_field(r, "maxPriorityFeePerGas") for _, r, _h in st["q"]})
assert _tips_in_bytes == [TIP2], \
    "в одной лестнице смешались tip'ы %s — поле tip лжёт, зеркало снова не доезжает" % _tips_in_bytes
print("UNIT_OK: долив подписан tip'ом лестницы (%d), а не свежим тиром (%d)" % (TIP2, TIP0))

# ---- 6. Долив лестницы БЕЗ поля tip НЕ доливает (пересборку сделает аудит) ----
# Склад обязан быть НЕПОЛНЫМ (иначе presign_ladder выходит на «годная лестница уже есть»
# раньше гарда, и тест вакуумный — «тест, который может вернуть только ноль»). Позитивный
# контроль в духе §3 test_presign_ladder: подпись под запретом — без гарда долив подписал бы
# пробу и умер здесь.
st.pop("tip")
assert ex.send_presigned_probe(rpc, cfg, ADDR) is not None    # 5 -> 4 < depth: долив нужен
_q_id = id(st["q"])
_depth_before = len(st["q"])
_real_sign = Account.sign_transaction


def _no_sign(*a, **k):
    raise AssertionError("долив лестницы без поля tip ПОДПИСЫВАЕТ — tip неизвестен, гард мёртв")


Account.sign_transaction = _no_sign
try:
    n = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5)
finally:
    Account.sign_transaction = _real_sign
assert n == _depth_before and len(st["q"]) == _depth_before and id(st["q"]) == _q_id, \
    "долив лестницы без tip обязан вернуть текущую глубину, не подписывая (tip неизвестен)"
print("UNIT_OK: лестница без поля tip не доливается — её пересоберёт аудит")

# ---- 7. Счётчик «пересборка по tip» висит СБОКУ от денег ----
# (а) мутация: счётчик бросает — подпись, установка и огонь живут (канон 13.08: прибор,
# вставший ПЕРЕД деньгами, уже ронял арм доски).
_real_note = ex.note_tip_resign


def _boom(*a, **k):
    raise AssertionError("счётчик встал перед деньгами")


ex.note_tip_resign = _boom
try:
    rpc = fresh()
    n = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=4, start_nonce=0)
    assert n == 4 and ex._VOLLEY_LADDER[ADDR].get("tip") == TIP0, \
        "исключение в счётчике уронило подпись/установку лестницы"
    assert ex.send_presigned_probe(rpc, cfg, ADDR) is not None, "исключение в счётчике уронило огонь"
finally:
    ex.note_tip_resign = _real_note
# (б) проводка: в денежных блоках execute.py счётчика НЕТ...
for fn in ("def presign_ladder(", "def send_presigned_probe(", "def send_volley_probe("):
    i = src.index(fn)
    blk = src[i:src.index("\ndef ", i + 10)]
    assert "note_tip_resign" not in blk, "%s зовёт счётчик — прибор встал в денежный путь" % fn
# ...а в аудите spec_fire_live он под СВОИМ try/except и не оборачивает пересборку
sfl = _pl.Path("chain/spec_fire_live.py").read_text()
_c = sfl.index("note_tip_resign")
_t = sfl.rfind("try:", 0, _c)
_e = sfl.index("except Exception", _c)
_seg = sfl[_t:_e]
assert "invalidate_ladder" not in _seg and "_refill(" not in _seg and "presign_ladder" not in _seg, \
    "try счётчика оборачивает пересборку — его сбой оторвёт ноги от аудита"
print("UNIT_OK: счётчик пересборок по tip висит сбоку — его сбой денег не трогает")

ex.invalidate_ladder(ADDR)
print("=== #127 ЗАКРЫТ: смена tip триггерит пересборку, долив держит tip лестницы, "
      "счётчик сбоку ===")
