"""Линза nonce по #127 (скретч, НЕ коммитить): гонка «проба в полёте невидима фазе-3 пересборки».

Утверждение: send_presigned_probe снимает запись с очереди ПОД локом (execute.py:1729), но
_VOLLEY_NONCE поднимает только ПОСЛЕ транспорта (execute.py:1769). Пересборка, начавшаяся в
этом окне, снимает _n_snap ДО инкремента (1546) и фаза-3 (1657) его не ловит => новая лестница
устанавливается С ЗАНЯТОГО nonce. #127 не меняет механику, но учащает вход (tip-триггер
срабатывает ровно в горячие окна, когда пробы в полёте).

Три секции:
  A. ПОЗИТИВНЫЙ КОНТРОЛЬ СТЕНДА: выстрел завершается ВО ВРЕМЯ фазы-2 => фаза-3 ОБЯЗАНА
     отбросить пересборку (иначе стенд не мерит то, что заявлено).
  B. ГОНКА: выстрел завершается ПОСЛЕ фазы-3 => голова новой лестницы = занятый nonce
     (сверка ПО БАЙТАМ подписи, не по бухгалтерии).
  C. ЗАЖИВЛЕНИЕ: следующий выстрел ловит «nonce too low», запись потрачена, голова = n+1,
     счётчик сходится (клин <= 1 блок).
"""
import sys
import threading
import time
import types

FIX_WT = "/home/claude-agent/work/liquidator/.claude/worktrees/wf_f8a5fcce-1cd-1"
sys.path.insert(0, FIX_WT)
import chain.execute as ex
assert ex.__file__.startswith(FIX_WT), "дрель импортировала не тот worktree: %s" % ex.__file__
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

KEY = "0x" + "42" * 32
ACCT = Account.from_key(KEY)
ADDR = ACCT.address
GAS = 3_000_000
BASEFEE = int(0.005e9)
TIP_A = 1_000_000
TIP_B = 1_100_000


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
        o = self._outer
        if o.fail_mode is not None:
            raise RuntimeError(o.fail_mode)
        if o.block_evt is not None:
            o.in_transport.set()          # выстрел вошёл в транспорт (запись уже снята)
            assert o.block_evt.wait(20), "стенд: транспорт не отпущен"
        o.sent.append(raw)
        return b"\x00"


class _FakeW3:
    def __init__(self, outer):
        self.eth = _FakeEth(outer)


class _FakeRpc:
    chain_id = 8453

    def __init__(self):
        self.sent = []
        self.pending_nonce = 0
        self.balance_wei = 10**18
        self.block_evt = None
        self.in_transport = threading.Event()
        self.fail_mode = None

    def _web3(self):
        return _FakeW3(self)


cfg = types.SimpleNamespace(liquidator_address="0x85BadE2cdD375Ab1b75f5C522B7989a12f939292",
                            tip_gwei=0.05, gas_limit_est=500_000, eth_price_usd=1730.0)


def _nonce_of(raw_hex):
    d = TypedTransaction.from_bytes(HexBytes(raw_hex)).as_dict()
    v = d["nonce"]
    return int(v, 16) if isinstance(v, str) else int(v)


def fresh():
    ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
    ex.invalidate_ladder(ADDR)
    ex._VOLLEY_NONCE.pop(ADDR, None)
    ex._VOLLEY_BAL[ADDR] = [10**18, time.monotonic()]
    ex._VOLLEY_GAS_USD = 0.0
    ex._VOLLEY_GAS_RESERVED = 0.0
    ex._INFLIGHT = 0
    return _FakeRpc()


def _fire(rpc, out):
    out["h"] = ex.send_presigned_probe(rpc, cfg, ADDR, log=None)


# ================= A. ПОЗИТИВНЫЙ КОНТРОЛЬ: инкремент ДО фазы-3 => отброс =================
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, tip=TIP_A, start_nonce=0)
assert ex.ladder_depth(ADDR) == 5 and ex._VOLLEY_NONCE[ADDR] == 0

rpc.block_evt = threading.Event()
out = {}
t = threading.Thread(target=_fire, args=(rpc, out), daemon=True)
t.start()
assert rpc.in_transport.wait(10), "стенд: выстрел не дошёл до транспорта"
assert ex.ladder_depth(ADDR) == 4          # запись nonce=0 снята, счётчик ещё 0
assert ex._VOLLEY_NONCE[ADDR] == 0

# смена tip => #127-триггер пересборки (max_fee в полосе — старая полоса слепа)
assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP_B) is True

_orig_account_for = ex.account_for
_released = {"done": False}


def _acct_release_mid_sign(w3, key):
    acct = _orig_account_for(w3, key)

    class _W:
        def sign_transaction(self, tx):
            if not _released["done"]:
                _released["done"] = True
                rpc.block_evt.set()        # транспорт отпущен В СЕРЕДИНЕ фазы-2
                t.join(10)                 # инкремент счётчика (1769) садится ДО фазы-3
            return acct.sign_transaction(tx)
    return _W()


ex.account_for = _acct_release_mid_sign
try:
    ex.invalidate_ladder(ADDR)             # как делает аудит (spec_fire_live.py:1773)
    d = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, tip=TIP_B)
finally:
    ex.account_for = _orig_account_for
assert out["h"] is not None and ex._VOLLEY_NONCE[ADDR] == 1
assert d == 0 and ex.ladder_depth(ADDR) == 0, \
    "ПОЗИТИВНЫЙ КОНТРОЛЬ СЛОМАН: фаза-3 не отбросила пересборку при уехавшем счётчике"
print("UNIT_OK[A]: инкремент до фазы-3 => пересборка отброшена (1657 работает; стенд мерит то)")

# ================= B. ГОНКА: инкремент ПОСЛЕ фазы-3 => занятый nonce в голове =================
rpc = fresh()
ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, tip=TIP_A, start_nonce=0)
rpc.block_evt = threading.Event()
out = {}
t = threading.Thread(target=_fire, args=(rpc, out), daemon=True)
t.start()
assert rpc.in_transport.wait(10)
assert ex.ladder_depth(ADDR) == 4 and ex._VOLLEY_NONCE[ADDR] == 0

assert ex.ladder_resign_needed(ADDR, BASEFEE, TIP_B) is True   # #127-триггер
ex.invalidate_ladder(ADDR)
d = ex.presign_ladder(rpc, cfg, ADDR, KEY, GAS, depth=5, tip=TIP_B)
assert d == 5, "пересборка не села — гонка не воспроизведена"
head_raw = ex._VOLLEY_LADDER[ADDR]["q"][0][1]

rpc.block_evt.set()                        # транспорт завершается ПОСЛЕ установки
t.join(10)
assert out["h"] is not None, "стенд: выстрел не завершился"
assert ex._VOLLEY_NONCE[ADDR] == 1         # 1769 перетёр 1667 (0 -> 1)

_raw0 = rpc.sent[0]
sent_nonce = _nonce_of(_raw0 if isinstance(_raw0, str) else "0x" + bytes(_raw0).hex())
head_nonce = _nonce_of(head_raw)
assert sent_nonce == 0 and head_nonce == 0, \
    "гонки нет: sent=%s head=%s" % (sent_nonce, head_nonce)
print("UNIT_OK[B]: ГОНКА ЕСТЬ — nonce 0 уже в сети, голова новой лестницы тоже 0 "
      "(фаза-3 слепа к пробе в полёте: pop@1729 до инкремента@1769)")

# ================= C. ЗАЖИВЛЕНИЕ: «nonce too low» тратит запись, клин <= 1 блок =================
rpc.fail_mode = "nonce too low"            # старую пробу намайнили следующим блоком
out2 = {}
_fire(rpc, out2)
# «nonce too low» = «байты уже у секвенсера»: запись ПОТРАЧЕНА, хэш возвращён и уйдёт в
# коллектор (execute.py:1768-1769) — это ФАНТОМНЫЙ хэш (эти байты не сядут никогда), его
# добьёт NO-RECEIPT, но volley_leg_landed (цепь догнала) пощадит склад (spec_fire_live:2455).
assert out2["h"] is not None
assert ex.ladder_depth(ADDR) == 4
assert _nonce_of(ex._VOLLEY_LADDER[ADDR]["q"][0][1]) == 1
assert ex._VOLLEY_NONCE[ADDR] == 1
rpc.fail_mode = None
out3 = {}
_fire(rpc, out3)
assert out3["h"] is not None and ex._VOLLEY_NONCE[ADDR] == 2
print("UNIT_OK[C]: заживление — занятый nonce сгорает классификацией отказа, голова n+1, "
      "счётчик сошёлся (клин <= 1 блок; вторая ветка «replacement underpriced» возвращает "
      "запись и ждёт того же блока)")
print("=== ЛИНЗА: окно pre-existing (все строки — код до #127), #127 учащает вход ===")
