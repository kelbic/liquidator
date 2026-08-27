"""#136: пейсер по эндпоинту + spillover рассылки сырых проб.

Факт боя (16.08 18:26 и 18.08 02:55): секвенсер-эндпоинт (proxyd, mainnet-sequencer.base.org)
режет рассылку «over rate limit» ПО ПИКУ — лимитер фикс-окна ~2с (= блок Base), пачка в голове
блока выжигает квоту за ~0.7с, дальше отказы до сброса окна. HEAD брал ТОЛЬКО первый url из
broadcast_raw_urls и игнорировал остальные — spill'а не было.

Правка: token-bucket ПО url в единой точке `_post_raw_sync` (paced-полоса), а `send_presigned_probe`
обходит ВСЕ каналы по порядку (try-take -> отправка; spill на следующий канал И при pacer_dry, И
при transport-reject — повтор той же подписанной tx идемпотентен). БОЕВАЯ ликвидационная tx НИКОГДА
не пейсится (она идёт мимо `_post_raw_sync`, через `_post_raw_tx`). Причины отказа считаются
РАЗДЕЛЬНО (pacer_dry / transport_reject — дисциплина #132). Откат VOLLEY_PACER=off = HEAD бит-в-бит.

Стенды гоняют НАСТОЯЩИЕ функции; подменён только транспортный шов (_post_raw_sync/_seq_session).
Красный-на-базе: до правки нет `volley_pacer_stats` (ImportError), нет параметра `paced`, и второй
url не вызывается НИКОГДА (spill отсутствует).
"""
import sys
import threading
import time
import types

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.execute as ex   # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:220])))
    if not ok:
        FAILS.append(name)


class LogRec:
    def __init__(self):
        self.msgs = []

    def info(self, *a):
        self.msgs.append(a)

    def exception(self, *a):
        self.msgs.append(a)

    def warning(self, *a):
        self.msgs.append(a)


CFG = types.SimpleNamespace(eth_price_usd=1730.0)
RAW = "0x" + "ab" * 12
U1 = "https://seq.example/base"
U2 = "https://spill.example/base"


def reset_pacer():
    for d in ("_PACER_BUCKETS", "_PACER_DRY", "_PACER_SPILL_SENT"):
        if hasattr(ex, d):
            getattr(ex, d).clear()
    # 20.08: transport_reject стал ПЕР-КАНАЛЬНЫМ словарём (был суммарный int) — вместе со
    # вторым egress-IP суммарное число перестало отвечать на вопрос «умер адрес или режет
    # секвенсер». Сбрасываем как словарь; прежняя величина = сумма значений (см. rej_total).
    if hasattr(ex, "_PACER_TRANSPORT_REJECT"):
        ex._PACER_TRANSPORT_REJECT = {}


def rej_total(rej):
    """Прежняя скалярная семантика transport_reject, восстановленная из пер-канального словаря."""
    return sum(rej.values()) if isinstance(rej, dict) else rej


def arm(addr, nonce=500):
    """Зарядить ногу так, чтобы send_presigned_probe ДОШЁЛ до транспорта (все гейты открыты)."""
    ex._VOLLEY_GAS_USD = 0.0
    ex._VOLLEY_GAS_RESERVED = 0.0
    ex._INFLIGHT = 0
    ex._VOLLEY_LOCKS[addr] = threading.Lock()
    h = "0xprobe%d" % nonce
    ex._VOLLEY_LADDER[addr] = {"q": [(nonce, RAW, h)], "gas": 21000,
                               "max_fee": 10 ** 9, "ts": time.monotonic()}
    ex._VOLLEY_BAL[addr] = [10 ** 20, time.monotonic()]     # патрон большой и свежий
    ex._VOLLEY_NONCE[addr] = nonce
    return nonce, RAW, h


# Скрипт транспорта на УРОВНЕ _post_raw_sync (подмена шва): url -> (ok, err) либо список по порядку.
SCRIPT = {}
CALLS = []


def scripted_sync(url, raw_hex, timeout=5, paced=True, rate=None, burst=None, source_ip=None,
                  urgent=False, **_kw):
    # source_ip — канал рассылки (20.08). Стенд сети не касается, поэтому просто фиксируем.
    # urgent (27.08) — залповая полоса в пейсере; двойник обязан принимать НОВЫЕ параметры боевой
    # функции, иначе TypeError красит блок B целиком и прячет то, что он на самом деле проверяет.
    # **_kw — намеренно: следующий добавленный флаг не должен снова валить весь блок.
    CALLS.append(url if source_ip is None else "%s|%s" % (url, source_ip))
    seq = SCRIPT.get(url)
    if isinstance(seq, list):
        return seq.pop(0) if seq else (False, "over rate limit")
    return seq if seq is not None else (True, None)


ADDR = "0x" + "a1" * 20

# =============================================================================================
# БЛОК A — НАСТОЯЩЕЕ ведро внутри _post_raw_sync (шов = _seq_session): burst, затем pacer_dry;
#          paced=False обходит ведро; VOLLEY_PACER=off ведро не трогает.
# =============================================================================================
try:
    POSTS = []

    class _FakeResp:
        def json(self):
            return {"result": "0x1"}

    class _FakeSession:
        def post(self, url, json=None, timeout=None):
            POSTS.append(url)
            return _FakeResp()

    _real_seq_session = ex._seq_session
    ex._seq_session = lambda url: _FakeSession()
    reset_pacer()
    # rate=0 -> пополнения нет; burst=2 -> ровно 2 токена, третий сухой
    # 27.08: у ведра ПОЯВИЛСЯ резерв «бёрст-в-щель» (_PACER_RESERVE): обычной отправке нужно
    # tok >= 1 + reserve, залповой (urgent=True) — tok >= 1. Прежние строки считали, что burst=2
    # даёт ДВЕ обычные отправки; с резервом это неверно ПО ЗАМЫСЛУ, и тест краснел на живой фиче.
    # Проверяем обе семантики: ниже — чистое ведро (резерв снят), а следом отдельный блок на резерв.
    _saved_reserve = ex._PACER_RESERVE
    ex._PACER_RESERVE = 0.0
    r1 = ex._post_raw_sync("uA", RAW, paced=True, rate=0.0, burst=2)
    r2 = ex._post_raw_sync("uA", RAW, paced=True, rate=0.0, burst=2)
    r3 = ex._post_raw_sync("uA", RAW, paced=True, rate=0.0, burst=2)
    check("ведро: первые burst отправок проходят", r1 == (True, None) and r2 == (True, None), (r1, r2))
    check("ведро: сверх burst -> pacer_dry БЕЗ сетевого похода",
          r3 == (False, "pacer_dry") and len(POSTS) == 2, (r3, POSTS))
    # paced=False -> боевая полоса: ведро СУХОЕ, но отправка всё равно уходит
    r4 = ex._post_raw_sync("uA", RAW, paced=False, rate=0.0, burst=2)
    check("ведро: paced=False обходит СУХОЕ ведро (приоритетная полоса)",
          r4 == (True, None) and len(POSTS) == 3, (r4, POSTS))
    # VOLLEY_PACER=off -> ведро не трогается вовсе
    ex._VOLLEY_PACER_ON = False
    r5 = ex._post_raw_sync("uA", RAW, paced=True, rate=0.0, burst=2)
    check("ведро: VOLLEY_PACER=off -> ведро не консультируется",
          r5 == (True, None) and len(POSTS) == 4, (r5, POSTS))
    # --- РЕЗЕРВ: обычная отправка держит щель для залповой -------------------------------
    # Пейсер ОБЯЗАН быть включён: блок стоит после кейса VOLLEY_PACER=off, и без этой строки
    # весь блок мерил бы выключенный механизм (все отправки проходят => «резерв работает»).
    ex._VOLLEY_PACER_ON = True
    reset_pacer(); POSTS.clear()
    ex._PACER_RESERVE = 1.0
    q1 = ex._post_raw_sync("uB", RAW, paced=True, rate=0.0, burst=2)   # tok 2 -> 1
    q2 = ex._post_raw_sync("uB", RAW, paced=True, rate=0.0, burst=2)   # нужно >= 2 -> сухо
    check("резерв: обычная отправка НЕ съедает щель (burst=2, reserve=1 -> одна обычная)",
          q1 == (True, None) and q2 == (False, "pacer_dry"), (q1, q2))
    q3 = ex._post_raw_sync("uB", RAW, paced=True, rate=0.0, burst=2, urgent=True)
    check("резерв: залповая (urgent) щель ЗАБИРАЕТ — ради неё резерв и держали",
          q3 == (True, None), (q3, POSTS))
    q4 = ex._post_raw_sync("uB", RAW, paced=True, rate=0.0, burst=2, urgent=True)
    check("резерв: после щели ведро пусто и для залповой (гард не бесконечный)",
          q4 == (False, "pacer_dry"), (q4,))
    ex._PACER_RESERVE = _saved_reserve
    ex._VOLLEY_PACER_ON = True
    ex._seq_session = _real_seq_session
except Exception as e:
    check("ведро: первые burst отправок проходят", False, "%s: %s" % (type(e).__name__, e))


# =============================================================================================
# БЛОК B — send_presigned_probe: spill-порядок, счётчики, requeue, already-known (шов=_post_raw_sync)
# =============================================================================================
_real_post_raw_sync = ex._post_raw_sync
ex._post_raw_sync = scripted_sync

# B1: pacer_dry на первом -> spill на второй -> доставлено; счётчики разведены
try:
    reset_pacer()
    CALLS.clear()
    SCRIPT.clear()
    SCRIPT[U1] = (False, "pacer_dry")
    SCRIPT[U2] = (True, None)
    n, raw, h = arm(ADDR, nonce=501)
    out = ex.send_presigned_probe(None, CFG, ADDR, log=LogRec(), broadcast_raw_urls=[U1, U2])
    dry, spill, rej = ex.volley_pacer_stats()
    check("B1 spill(pacer_dry): доставлено вторым каналом", out == h and CALLS == [U1, U2], (out, CALLS))
    check("B1 spill(pacer_dry): pacer_dry[U1]=1, spill_sent[U2]=1, reject=0",
          dry.get(U1) == 1 and spill.get(U2) == 1 and rej_total(rej) == 0, (dry, spill, rej))
    check("B1 spill(pacer_dry): nonce ноги продвинут", ex._VOLLEY_NONCE[ADDR] == n + 1,
          ex._VOLLEY_NONCE[ADDR])
except Exception as e:
    check("B1 spill(pacer_dry): доставлено вторым каналом", False, "%s: %s" % (type(e).__name__, e))

# B2: transport-reject «over rate limit» на первом -> spill на второй; reject считается ОТДЕЛЬНО
try:
    reset_pacer()
    CALLS.clear()
    SCRIPT.clear()
    SCRIPT[U1] = (False, "over rate limit")
    SCRIPT[U2] = (True, None)
    n, raw, h = arm(ADDR, nonce=502)
    out = ex.send_presigned_probe(None, CFG, ADDR, log=LogRec(), broadcast_raw_urls=[U1, U2])
    dry, spill, rej = ex.volley_pacer_stats()
    check("B2 spill(over-rate-limit): доставлено вторым каналом", out == h and CALLS == [U1, U2], (out, CALLS))
    check("B2 spill(over-rate-limit): transport_reject=1, pacer_dry пуст, spill_sent[U2]=1",
          rej_total(rej) == 1 and not dry and spill.get(U2) == 1, (dry, spill, rej))
except Exception as e:
    check("B2 spill(over-rate-limit): доставлено вторым каналом", False, "%s: %s" % (type(e).__name__, e))

# B3: ВСЕ каналы отказали -> запись вернулась В НАЧАЛО очереди тем же кортежем; лог #104; nonce НЕ рос
try:
    reset_pacer()
    CALLS.clear()
    SCRIPT.clear()
    SCRIPT[U1] = (False, "pacer_dry")
    SCRIPT[U2] = (False, "over rate limit")
    n, raw, h = arm(ADDR, nonce=503)
    lg = LogRec()
    out = ex.send_presigned_probe(None, CFG, ADDR, log=lg, broadcast_raw_urls=[U1, U2])
    dry, spill, rej = ex.volley_pacer_stats()
    q0 = ex._VOLLEY_LADDER[ADDR]["q"][0] if ex._VOLLEY_LADDER[ADDR]["q"] else None
    _fmt = " ".join(str(m[0]) for m in lg.msgs)
    check("B3 exhaustion: возврат None, оба канала опрошены по разу", out is None and CALLS == [U1, U2], (out, CALLS))
    check("B3 exhaustion: запись возвращена В НАЧАЛО тем же кортежем (nonce,raw,h)",
          q0 == (n, raw, h), q0)
    check("B3 exhaustion: nonce НЕ продвинут (инвариант подряд)", ex._VOLLEY_NONCE[ADDR] == n, ex._VOLLEY_NONCE[ADDR])
    check("B3 exhaustion: причины разведены (pacer_dry[U1]=1, reject=1)",
          dry.get(U1) == 1 and rej_total(rej) == 1, (dry, rej))
    check("B3 exhaustion: лог #104 с контекстом сохранён", "запись возвращена" in _fmt and "nonce" in _fmt, _fmt[:120])
except Exception as e:
    check("B3 exhaustion: возврат None, оба канала опрошены по разу", False, "%s: %s" % (type(e).__name__, e))

# B4: reject на первом -> already-known на втором = ПОТРАЧЕНО (байты у ордерера): нет requeue, nonce++
try:
    reset_pacer()
    CALLS.clear()
    SCRIPT.clear()
    SCRIPT[U1] = (False, "over rate limit")
    SCRIPT[U2] = (False, "already known")
    n, raw, h = arm(ADDR, nonce=504)
    out = ex.send_presigned_probe(None, CFG, ADDR, log=LogRec(), broadcast_raw_urls=[U1, U2])
    dry, spill, rej = ex.volley_pacer_stats()
    q = ex._VOLLEY_LADDER[ADDR]["q"]
    check("B4 already-known: терминальный успех (возврат h, запись НЕ возвращена)",
          out == h and not q, (out, q))
    check("B4 already-known: nonce продвинут, already-known НЕ считается reject'ом",
          ex._VOLLEY_NONCE[ADDR] == n + 1 and rej_total(rej) == 1 and not spill, (ex._VOLLEY_NONCE[ADDR], rej, spill))
except Exception as e:
    check("B4 already-known: терминальный успех (возврат h, запись НЕ возвращена)", False, "%s: %s" % (type(e).__name__, e))

ex._post_raw_sync = _real_post_raw_sync


# =============================================================================================
# БЛОК C — ОТКАТ VOLLEY_PACER=off: первый url, одна попытка, без вёдер/spill/счётчиков (HEAD бит-в-бит)
# =============================================================================================
try:
    ex._post_raw_sync = scripted_sync
    reset_pacer()
    CALLS.clear()
    SCRIPT.clear()
    SCRIPT[U1] = (True, None)
    SCRIPT[U2] = (True, None)
    ex._VOLLEY_PACER_ON = False
    n, raw, h = arm(ADDR, nonce=505)
    out = ex.send_presigned_probe(None, CFG, ADDR, log=LogRec(), broadcast_raw_urls=[U1, U2])
    dry, spill, rej = ex.volley_pacer_stats()
    check("C off: РОВНО одна отправка на первый url (spill не активен)", out == h and CALLS == [U1], (out, CALLS))
    check("C off: счётчики пейсера не двигаются", not dry and not spill and rej_total(rej) == 0, (dry, spill, rej))
    ex._VOLLEY_PACER_ON = True
    ex._post_raw_sync = _real_post_raw_sync
except Exception as e:
    check("C off: РОВНО одна отправка на первый url (spill не активен)", False, "%s: %s" % (type(e).__name__, e))
    ex._VOLLEY_PACER_ON = True
    ex._post_raw_sync = _real_post_raw_sync


# =============================================================================================
# БЛОК D — ПРИОРИТЕТНАЯ ПОЛОСА: боевая send_tx с broadcast_raw_urls шлёт МИМО ведра (даже сухого)
# =============================================================================================
try:
    class _Hex:
        def __init__(self, s):
            self._s = s

        def hex(self):
            return self._s

    class _FakeAcct:
        address = "0xB07"

        def sign_transaction(self, t):
            return types.SimpleNamespace(raw_transaction=b"rawbytes",
                                         hash=_Hex("0xbattlehash"))

    class _FakeEth:
        max_priority_fee = 1_000_000

        def get_block(self, _tag):
            return {"baseFeePerGas": 5_000_000}

        def estimate_gas(self, _t):
            return 100_000

        def send_raw_transaction(self, raw):
            return _Hex("0xbattlehash")

        def get_transaction_count(self, _addr, _tag="pending"):
            return 7

    _W3 = types.SimpleNamespace(eth=_FakeEth())
    _RPC = types.SimpleNamespace(_web3=lambda: _W3, chain_id=8453)

    class _SyncThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    POST_TX = []
    SYNC_CALLS = []
    _real_thr = ex.threading
    _real_acct = ex.account_for
    _real_ptx = ex._post_raw_tx
    _real_prs = ex._post_raw_sync
    _real_recon = ex._reconcile_nonce
    ex.threading = types.SimpleNamespace(Thread=_SyncThread, Lock=_real_thr.Lock,
                                         current_thread=_real_thr.current_thread)
    ex.account_for = lambda w3, key: _FakeAcct()
    ex._post_raw_tx = lambda url, raw_hex, timeout=5: POST_TX.append(url)
    ex._post_raw_sync = lambda *a, **k: (SYNC_CALLS.append(a[0] if a else None), (True, None))[1]
    ex._reconcile_nonce = lambda w3, bot: None
    # ВЁДРА СУХИЕ — боевая полоса обязана их игнорировать
    reset_pacer()
    ex._PACER_BUCKETS[U1] = [0.0, time.monotonic()]
    ex._PACER_BUCKETS[U2] = [0.0, time.monotonic()]
    res = ex.send_tx(_RPC, "k", {"to": "0xL1Q", "data": "0xdead", "gas": 500000, "nonce": 7,
                                 "maxFeePerGas": 2_000_000, "maxPriorityFeePerGas": 50_000},
                     wait=False, broadcast_raw_urls=[U1, U2])
    check("D боевая: raw роздан во ВСЕ broadcast-каналы (мимо ведра)", POST_TX == [U1, U2], POST_TX)
    check("D боевая: пейсер-точка _post_raw_sync НЕ вызывалась (бой не пейсится)",
          SYNC_CALLS == [] and res.get("hash") == "0xbattlehash", (SYNC_CALLS, res))
    ex.threading = _real_thr
    ex.account_for = _real_acct
    ex._post_raw_tx = _real_ptx
    ex._post_raw_sync = _real_prs
    ex._reconcile_nonce = _real_recon
except Exception as e:
    check("D боевая: raw роздан во ВСЕ broadcast-каналы (мимо ведра)", False, "%s: %s" % (type(e).__name__, e))


# =============================================================================================
# БЛОК E — ПРОВОДКА счётчиков в закрывающую строку окна (spec_fire_live.py), как #132
# =============================================================================================
src = open("chain/spec_fire_live.py").read()
check("E проводка: volley_pacer_stats импортируется в окне закрытия", "volley_pacer_stats" in src)
check("E проводка: поля пейсера в форматной строке окна",
      "pacer_dry=" in src and "spill_sent=" in src and "transport_reject=" in src)


print("=" * 62)
if FAILS:
    print("ПРОВАЛ: %d — %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("OK: пейсер+spill по каналам, счётчики разведены, бой не пейсится, откат бит-в-бит")
