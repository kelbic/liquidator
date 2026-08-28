"""Линза на a702380 (скретч, НЕ коммитить): пробы spec-fire с ног против лестниц залпа.

Стенд коммита подменяет send_tx фейком и стабит гейты резерва — ровно там, где живут два дефекта.
Здесь: НАСТОЯЩИЕ send_tx / presign_ladder / send_presigned_probe / cached_balance_wei /
probe_allowed_by_reserve против фейкового узла и фейкового секвенсера с правилами op-geth
(nonce too low / replacement underpriced / already known).

  A. РЕЗЕРВ: гейт патрона считается по балансу ноги (0.008 ETH) против BULLET_RESERVE 0.12 ETH,
     а кэш баланса — ОДИН глобальный слот: отравляет и канарейку главного.
  B. КОЛЛИЗИЯ NONCE: первая же проба с ноги берёт pending-nonce = голова её лестницы.
  C. ФАНТОМ: устаревший _LOCAL_NONCE[нога] + SEND-3 => send_tx глотает «nonce too low» в
     uncertain-hash, проба «отправлена», счётчик не лечится; NO-RECEIPT снимает nonce ГЛАВНОГО.
"""
import logging
import sys
import threading
import time
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator")
import chain.execute as ex  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.typed_transactions import TypedTransaction  # noqa: E402
from eth_utils import keccak  # noqa: E402
from hexbytes import HexBytes  # noqa: E402

assert ex.__file__.startswith("/home/claude-agent/work/liquidator/chain/"), ex.__file__

K_MAIN = "0x" + "11" * 32
K_L1 = "0x" + "22" * 32
K_L2 = "0x" + "33" * 32
MAIN = Account.from_key(K_MAIN).address
L1 = Account.from_key(K_L1).address
L2 = Account.from_key(K_L2).address
BASEFEE = int(0.005e9)
TIP = 1_000_000            # 0.001 gwei — пол (_PROBE_TIP_WEI)
GAS_PROBE = 100_000
GAS_LADDER = 3_000_000
LIQ = Account.from_key("0x" + "aa" * 32).address


class Seq:
    """Мемпул/секвенсер с правилами op-geth: nonce too low / replacement underpriced / already known."""

    def __init__(self):
        self.chain_nonce = {}          # addr -> следующий nonce на цепи (после mine)
        self.pool = {}                 # (addr, nonce) -> (fee, tip, raw_hex, data_len)
        self.events = []
        self.rtt_nonce_reads = 0

    @staticmethod
    def _decode(raw):
        raw_hex = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
        d = TypedTransaction.from_bytes(HexBytes(raw_hex)).as_dict()

        def _i(v):
            return int(v, 16) if isinstance(v, str) else int(v)
        sender = Account.recover_transaction(raw_hex)
        return raw_hex, sender, _i(d["nonce"]), _i(d["maxFeePerGas"]), _i(d["maxPriorityFeePerGas"]), len(d.get("data") or b"")

    def submit(self, raw):
        raw_hex, sender, nonce, fee, tip, dlen = self._decode(raw)
        if nonce < self.chain_nonce.get(sender, 0):
            self.events.append(("REJECT nonce too low", sender[:8], nonce, dlen))
            raise ValueError("nonce too low")
        key = (sender, nonce)
        old = self.pool.get(key)
        if old is not None:
            if old[2] == raw_hex:
                raise ValueError("already known")
            if fee < old[0] * 11 // 10 or tip < old[1] * 11 // 10:
                self.events.append(("REJECT replacement underpriced", sender[:8], nonce, dlen))
                raise ValueError("replacement transaction underpriced")
            self.events.append(("REPLACED (старая tx выкинута)", sender[:8], nonce, "old_data_len=%d new=%d" % (old[3], dlen)))
        else:
            self.events.append(("ACCEPT", sender[:8], nonce, dlen))
        self.pool[key] = (fee, tip, raw_hex, dlen)
        return HexBytes(keccak(HexBytes(raw_hex)))

    def pending(self, addr):
        n = self.chain_nonce.get(addr, 0)
        while (addr, n) in self.pool:
            n += 1
        return n

    def mine(self):
        for (a, n) in sorted(self.pool):
            self.chain_nonce[a] = max(self.chain_nonce.get(a, 0), n + 1)
        self.pool.clear()


class _Eth:
    account = Account
    chain_id = 8453
    max_priority_fee = 0

    def __init__(self, o):
        self.o = o

    def get_transaction_count(self, addr, blk="pending"):
        self.o.seq.rtt_nonce_reads += 1
        return self.o.seq.pending(addr)

    def get_balance(self, addr):
        return self.o.balances.get(addr, 0)

    def get_block(self, _b):
        return {"baseFeePerGas": BASEFEE}

    def send_raw_transaction(self, raw):
        return self.o.seq.submit(raw)


class Rpc:
    chain_id = 8453

    def __init__(self, seq, balances):
        self.seq = seq
        self.balances = balances

    def _web3(self):
        return types.SimpleNamespace(eth=_Eth(self))


cfg = types.SimpleNamespace(wallet_key=K_MAIN, liquidator_address=LIQ, tip_gwei=0.001,
                            gas_limit_est=500_000, eth_price_usd=4000.0)

_LOGS = []


class _H(logging.Handler):
    def emit(self, r):
        _LOGS.append(r.getMessage())


lg = logging.getLogger("lens")
lg.setLevel(logging.INFO)
lg.addHandler(_H())
ex.log.addHandler(_H())
ex.log.setLevel(logging.INFO)


def fresh(balances):
    seq = Seq()
    rpc = Rpc(seq, balances)
    ex._VOLLEY_ACCTS = [(L1, K_L1), (L2, K_L2)]
    for a, _ in ex._VOLLEY_ACCTS:
        ex._VOLLEY_LOCKS.setdefault(a, threading.Lock())
        ex._VOLLEY_LADDER.pop(a, None)
        ex._VOLLEY_NONCE.pop(a, None)
        ex._VOLLEY_BAL[a] = [balances.get(a, 0), time.monotonic()]
    ex._LOCAL_NONCE.clear()
    ex._BAL_CACHE.update({"wei": None, "ts": 0.0, "spent": 0})
    if hasattr(ex, "_PROBE_LEG_RR"): ex._PROBE_LEG_RR[0] = 0     # на базе атрибута нет — стенд обязан дойти до проверок
    ex._INFLIGHT = 0
    ex._SPECFIRE_GAS_USD = 0.0
    ex._SPECFIRE_PROBES = 0
    ex._SPECFIRE_MIN_INTERVAL = 0.0
    ex._VOLLEY_GAS_USD = 0.0
    ex._VOLLEY_GAS_RESERVED = 0.0
    ex.SPEC_FIRE_TIER_A_LEGS = 0            # однотирная формула патрона ноги
    ex._post_raw_tx = lambda url, raw_hex, timeout=5: _seq_post(seq, raw_hex)   # SEND-3 нога
    ex._post_raw_sync = lambda url, raw_hex, **kw: (True, None) if _seq_post(seq, raw_hex) else (False, "rejected")
    del _LOGS[:]
    return rpc, seq


def _seq_post(seq, raw_hex):
    try:
        seq.submit(raw_hex)
        return True
    except Exception:
        return False


def probe(rpc, seq_urls=("http://seq",)):
    ex._SPECFIRE_LAST_TS = 0.0
    return ex.send_spec_fire_probe(rpc, cfg, "0x" + "de" * 200, GAS_PROBE, log=lg,
                                   max_fee=BASEFEE * 2 + TIP, tip=TIP,
                                   broadcast_raw_urls=list(seq_urls) if seq_urls else None)


def _wait_threads():
    for t in threading.enumerate():
        if t is not threading.current_thread() and not t.daemon:
            t.join(2)
    time.sleep(0.05)


print("=" * 78)
# =============================================================================================
# СТЕНД ПОСЛЕ ЛИНЗЫ (28.08): ветка ноги идёт через send_volley_probe. Инварианты линзы:
#  A. ветка ноги НЕ трогает глобальный слот баланса главного и не судится его резервом;
#  B. один nonce-поток на ногу: лестница и проба делят _VOLLEY_NONCE, REJECT нет, nonce непрерывен;
#  C. NO-RECEIPT пробы с ноги снимает nonce НОГИ, а _LOCAL_NONCE главного не трогает.
# =============================================================================================
FAILS = []
def check(name, ok, info=""):
    print(("OK   " if ok else "FAIL ") + name + ((" — " + str(info)) if info else ""))
    if not ok: FAILS.append(name)


def probe_from(rpc, addr):
    """Проба spec-fire с КОНКРЕТНОЙ ноги: ставим round-robin на её индекс."""
    idx = [a for a, _k in ex._VOLLEY_ACCTS].index(addr)
    ex._PROBE_LEG_RR[0] = idx
    return probe(rpc)

def presign_and_fire(rpc, addr):
    """Лестница ноги: предподписать с pending-головы и выстрелить одной пробой лестницы."""
    key = dict(ex._VOLLEY_ACCTS)[addr]
    ex.presign_ladder(rpc, cfg, addr, key, GAS_LADDER, depth=5, tip=TIP, base_fee=BASEFEE, log=lg)
    return ex.send_presigned_probe(rpc, cfg, addr, log=lg, broadcast_raw_urls=None)

bal = {MAIN: 10**18, L1: int(0.008e18), L2: int(0.008e18)}
rpc, seq = fresh(bal)
ex.PROBE_FROM_LEGS = True
print("A. ветка ноги и резерв главного")
ex._BAL_CACHE.update({"wei": None, "ts": 0.0, "spent": 0})
h1 = probe(rpc); h2 = probe(rpc)
check("проба с ноги отправлена (hash есть)", bool(h1) and bool(h2), (h1, h2))
check("глобальный слот баланса главного НЕ тронут пробой ноги", ex._BAL_CACHE["wei"] is None, ex._BAL_CACHE["wei"])
check("резерв главного (0.12 ETH) НЕ глушит ногу с 0.008 ETH", bool(h1))
accepts = [e for e in seq.events if e[0] == "ACCEPT"]
check("обе пробы ПРИНЯТЫ секвенсером", len(accepts) >= 2, seq.events[-4:])

print("B. один nonce-поток на ногу (лестница + проба)")
seq.events.clear()
ladder_first = presign_and_fire(rpc, L1)          # лестница L1 подписывает и стреляет со своей головы
print("   лестница ->", ladder_first, "| события:", seq.events[-3:])
hp = probe_from(rpc, L1)                          # проба с той же ноги СРАЗУ после
print("   проба   ->", hp, "| события:", seq.events[-3:])
rej = [e for e in seq.events if e[0] == "REJECT"]
check("REJECT (replacement underpriced / nonce too low) НЕТ", not rej, rej[:3])
nonces = sorted(e[2] for e in seq.events if e[0] == "ACCEPT" and L1.lower().startswith(e[1].lower()))   # секвенсер-фейк режет адрес до префикса
check("nonce ноги непрерывен и без дублей", nonces == list(range(nonces[0], nonces[0] + len(nonces))) if nonces else False, ("nonces", nonces))
check("_LOCAL_NONCE ноги НЕ заводится (второго счётчика нет)", L1 not in ex._LOCAL_NONCE and L1.lower() not in ex._LOCAL_NONCE, list(ex._LOCAL_NONCE)[:3])

print("C. NO-RECEIPT пробы с ноги")
ex._LOCAL_NONCE[MAIN] = 27991
h = probe_from(rpc, L1)
snd = ex.probe_sender(h)
check("отправитель пробы записан как нога", snd and snd.lower() == L1.lower(), snd)
before_main = ex._LOCAL_NONCE.get(MAIN); vb = dict(ex._VOLLEY_NONCE)
ex.volley_invalidate_nonce(L1)
check("nonce главного НЕ снят при NO-RECEIPT ноги", ex._LOCAL_NONCE.get(MAIN) == before_main, ex._LOCAL_NONCE.get(MAIN))
check("nonce ноги снят/инвалидирован", ex._VOLLEY_NONCE.get(L1) != vb.get(L1) or L1 not in ex._VOLLEY_NONCE, (vb.get(L1), ex._VOLLEY_NONCE.get(L1)))

print("\n" + ("FAIL: %d" % len(FAILS) if FAILS else "OK: пробы с ног — один nonce-поток, резерв главного не судит, NO-RECEIPT снимает nonce ноги"))
sys.exit(1 if FAILS else 0)
