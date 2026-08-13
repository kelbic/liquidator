"""БЛОКИРУЮЩЕЕ условие пакета coincurve: подпись на C обязана давать ПОБАЙТОВО ТЕ ЖЕ байты.

Смена ECC-бэкенда — это смена кода, который считает подпись боевой транзакции. Расхождение
хотя бы в одном бите = другой hash = другая tx: план армится не тот, проба не та, выстрел не
тот. Поэтому эквивалентность проверяется не «должно совпадать по стандарту», а ЗАПУСКОМ обоих
бэкендов на НАШИХ формах и сравнением сырых байтов.

ЧТО СРАВНИВАЕМ (формы взяты из настоящего кода, калдату строят настоящие энкодеры бота):
  * проба залпа       chain/execute.py:_sign_run  (V2_PROBE_CALLDATA = 0x01, EIP-1559);
  * арм доски         encode_set_plan / encode_clear_plan;
  * боевой выстрел    encode_liquidate / encode_batch_liquidate;
  * границы           пустая data, калдата 7000 байт, tip=0, чужие chainId, nonce 2^32-1;
  * фузз              N случайных ключей x случайные параметры (ловит редкие ветки подписи:
                      r/s с ведущим нулём, обе чётности v — их печатаем, чтобы «фузз прошёл»
                      не значило «фузз их не встретил»).

ПОЧЕМУ ДВА ПРОЦЕССА. eth_keys выбирает бэкенд один раз (get_backend_class читает
ECC_BACKEND_CLASS на импорте) — внутри одного процесса честно переиграть выбор нельзя.

ПОЗИТИВНЫЙ КОНТРОЛЬ. В каждом процессе считаем вызовы ecdsa_sign НА КЛАССЕ пиненного бэкенда:
если бы подпись шла мимо него, сравнение сравнивало бы одно и то же с самим собой и всегда
было бы зелёным. Тест краснеет, если счётчик не совпал с числом подписей.
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = "eth_keys.backends.native.main.NativeECCBackend"
COINCURVE = "eth_keys.backends.coincurve.CoinCurveECCBackend"
FUZZ_N = int(os.environ.get("SIGN_EQ_FUZZ_N", "400"))

try:
    import coincurve  # noqa: F401
    _HAVE = True
except Exception as _e:
    _HAVE = False
    _WHY = type(_e).__name__

WORKER = r'''
import hashlib, json, os, random, sys, time
sys.path.insert(0, %(here)r)
from eth_keys import keys
from eth_account import Account
import chain.execute as ex

cls = type(keys.backend)
BACKEND = cls.__module__ + "." + cls.__name__
hits = [0]
_orig = cls.ecdsa_sign
def _counting(self, h, pk):
    hits[0] += 1
    return _orig(self, h, pk)
cls.ecdsa_sign = _counting

LIQ = "0x1111111111111111111111111111111111111111"
MP = {"loanToken": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "collateralToken": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
      "oracle": "0x663BECd10daE6C4A3Dcd89F1d76c1174199639B9",
      "irm": "0x46415998764C29aB2a25CbeA6254146D50D22687",
      "lltv": 860000000000000000}
BORROWER = "0x00000000000000000000000000000000000dEaD1"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
def sw(n): return "0x" + "ab" * n

KEY = bytes.fromhex("11" * 32)          # тестовый, одноразовый: .env не читаем
A = Account.from_key(KEY)
forms = []
for i, (n, g, cid, mf, tp) in enumerate([
        (0, 1000000, 8453, 100000000, 1000000), (1, 1000000, 8453, 100000000, 1000000),
        (12345, 710000, 8453, 5000000, 1000), (2**32 - 1, 3000000, 8453, 2**63, 2**62),
        (7, 21000, 1, 10**9, 10**8), (7, 21000, 84532, 10**9, 10**8),
        (0, 1000000, 8453, 100000000, 0)]):
    forms.append(("probe%%d" %% i, {"from": A.address, "to": LIQ, "data": ex.V2_PROBE_CALLDATA,
                  "nonce": n, "gas": g, "chainId": cid, "maxFeePerGas": mf,
                  "maxPriorityFeePerGas": tp}))
for i, ln in enumerate([0, 4, 68, 1024, 7000]):
    cd = ex.encode_set_plan(MP, BORROWER, 12345678901234567890, ROUTER, sw(ln),
                            min_profit=10**16, deadline=1893456000, trigger_price=2 * 10**36)
    forms.append(("setplan%%d_len%%d" %% (i, ln), {"from": A.address, "to": LIQ, "data": cd,
                  "nonce": 100 + i, "gas": 3000000, "chainId": 8453,
                  "maxFeePerGas": 60000000, "maxPriorityFeePerGas": 1000000}))
forms.append(("clearplan", {"from": A.address, "to": LIQ, "data": ex.encode_clear_plan(),
              "nonce": 200, "gas": 100000, "chainId": 8453, "maxFeePerGas": 60000000,
              "maxPriorityFeePerGas": 1000000}))
for i, ln in enumerate([0, 260, 4096]):
    cd = ex.encode_liquidate(MP, BORROWER, 999999999999, ROUTER, sw(ln), min_profit=0)
    forms.append(("liq%%d_len%%d" %% (i, ln), {"from": A.address, "to": LIQ, "data": cd,
                  "nonce": 300 + i, "gas": 1500000, "chainId": 8453,
                  "maxFeePerGas": 120000000, "maxPriorityFeePerGas": 2000000}))
preps = [{"mp": MP, "borrower": BORROWER, "repaid_shares": 5, "swap_target": ROUTER,
          "swap_calldata": sw(96), "min_profit": 0, "unwrap_target": ex.ZERO_ADDR},
         {"mp": MP, "borrower": A.address, "repaid_shares": 6, "swap_target": ROUTER,
          "swap_calldata": sw(0), "min_profit": 10**15, "unwrap_target": ex.ZERO_ADDR}]
forms.append(("batchliq", {"from": A.address, "to": LIQ, "data": ex.encode_batch_liquidate(preps),
              "nonce": 400, "gas": 2500000, "chainId": 8453, "maxFeePerGas": 120000000,
              "maxPriorityFeePerGas": 2000000}))
forms.append(("bare", {"from": A.address, "to": LIQ, "value": 1, "nonce": 500, "gas": 21000,
              "chainId": 8453, "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8}))

def sign(acct, t):
    s = acct.sign_transaction(t)
    raw = getattr(s, "raw_transaction", None)
    if raw is None:
        raw = s.rawTransaction
    return {"raw": bytes(raw).hex(), "hash": bytes(s.hash).hex(),
            "r": int(s.r), "s": int(s.s), "v": int(s.v)}

t0 = time.perf_counter()
named = {n: sign(A, t) for n, t in forms}
t_named = time.perf_counter() - t0

rnd = random.Random(20250813)
rows, lz_r, lz_s, vs = [], 0, 0, set()
t0 = time.perf_counter()
for _ in range(%(fuzz)d):
    k = rnd.getrandbits(256).to_bytes(32, "big")
    if not (1 <= int.from_bytes(k, "big") < 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141):
        continue
    a = Account.from_key(k)
    dl = rnd.choice([0, 1, 4, 36, 100, 260, 1000, 3300])
    d = ex.V2_PROBE_CALLDATA if dl == 1 else "0x" + "ab" * dl
    r = sign(a, {"from": a.address, "to": LIQ, "data": d,
                 "nonce": rnd.choice([0, 1, 7, 4096, 2**32 - 1]),
                 "gas": rnd.choice([21000, 710000, 1000000, 3000000]),
                 "chainId": rnd.choice([8453, 1, 84532, 10]),
                 "maxFeePerGas": rnd.randrange(1, 10**11),
                 "maxPriorityFeePerGas": rnd.randrange(0, 10**9)})
    if r["r"] < 2**248: lz_r += 1
    if r["s"] < 2**248: lz_s += 1
    vs.add(r["v"])
    rows.append((r["raw"], r["hash"], r["v"]))
t_fuzz = time.perf_counter() - t0

json.dump({"backend": BACKEND, "named": named,
           "fuzz_digest": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
           "fuzz_n": len(rows), "lead_zero_r": lz_r, "lead_zero_s": lz_s,
           "v_values": sorted(vs), "signs": len(named) + len(rows),
           "ecdsa_calls": hits[0], "t_named": t_named, "t_fuzz": t_fuzz},
          open(sys.argv[1], "w"))
print("DONE %%s подписей=%%d вызовов_ecdsa=%%d" %% (BACKEND, len(named) + len(rows), hits[0]))
'''


def _run(backend, path):
    code = WORKER % {"here": HERE, "fuzz": FUZZ_N}
    env = dict(os.environ, ECC_BACKEND_CLASS=backend)
    p = subprocess.run([sys.executable, "-c", code, path], capture_output=True, text=True,
                       env=env, timeout=1800)
    assert p.returncode == 0, "%s упал:\n%s" % (backend, p.stderr[-2500:])
    return json.load(open(path))


if not _HAVE:
    # Нельзя объявить эквивалентность там, где второго бэкенда нет. Скип ГРОМКИЙ и НЕ зелёный
    # по смыслу: приёмка пакета требует прогона в venv С coincurve.
    print("SKIP: coincurve не установлен в этом окружении (%s) — ЭКВИВАЛЕНТНОСТЬ НЕ ПРОВЕРЕНА. "
          "Это ровно то состояние Шарлотты, ради которого пакет и делается. Прогнать в venv "
          "после `pip install -r requirements.txt`." % _WHY)
    sys.exit(0)

import tempfile                                                       # noqa: E402
_d = tempfile.mkdtemp(prefix="signeq-")
a = _run(NATIVE, os.path.join(_d, "n.json"))
b = _run(COINCURVE, os.path.join(_d, "c.json"))

# ---- 0. ПОЗИТИВНЫЙ КОНТРОЛЬ: подписывал именно пиненный бэкенд --------------------------
assert a["backend"] == NATIVE and b["backend"] == COINCURVE, (a["backend"], b["backend"])
for r in (a, b):
    assert r["ecdsa_calls"] == r["signs"], \
        "%s: подписей %d, а ecdsa_sign бэкенда позван %d раз — сравнение сравнивало бы не то" \
        % (r["backend"], r["signs"], r["ecdsa_calls"])
print("UNIT_OK 0: оба процесса подписали СВОИМ бэкендом (%d и %d вызовов ecdsa_sign)"
      % (a["ecdsa_calls"], b["ecdsa_calls"]))

# ---- 1. ИМЕНОВАННЫЕ БОЕВЫЕ ФОРМЫ: побайтово ---------------------------------------------
bad = [n for n in a["named"] if a["named"][n] != b["named"][n]]
assert not bad, "РАСХОЖДЕНИЕ НА ПРОВОДЕ (блокер пакета): %s" % bad
assert len(a["named"]) >= 17, "набор форм усох — проверять стало нечего: %d" % len(a["named"])
_mx = max(len(v["raw"]) // 2 for v in a["named"].values())
print("UNIT_OK 1: %d боевых форм совпали побайтово (raw/hash/v/r/s), максимум %d Б"
      % (len(a["named"]), _mx))

# ---- 2. ФУЗЗ + отчёт о встреченных редких ветках ----------------------------------------
assert a["fuzz_n"] == b["fuzz_n"] and a["fuzz_n"] >= min(FUZZ_N, 100), (a["fuzz_n"], b["fuzz_n"])
assert a["fuzz_digest"] == b["fuzz_digest"], \
    "фузз разошёлся: %s против %s" % (a["fuzz_digest"][:16], b["fuzz_digest"][:16])
assert a["v_values"] == b["v_values"] == [0, 1], \
    "фузз не встретил обе чётности v — выборка мала: %s" % (a["v_values"],)
assert a["lead_zero_r"] > 0 and a["lead_zero_s"] > 0, \
    "фузз не встретил r/s с ведущим нулём (%d/%d) — редкая ветка НЕ покрыта, подними " \
    "SIGN_EQ_FUZZ_N" % (a["lead_zero_r"], a["lead_zero_s"])
print("UNIT_OK 2: фузз %d ключей — digest совпал (%s); встречено r<2^248: %d, s<2^248: %d, v=%s"
      % (a["fuzz_n"], a["fuzz_digest"][:16], a["lead_zero_r"], a["lead_zero_s"], a["v_values"]))

# ---- 3. НЕГАТИВНЫЙ КОНТРОЛЬ САМОГО СРАВНЕНИЯ --------------------------------------------
# Если сравнение слепое, оно не заметит и подмену одного бита. Проверяем прибор, а не подпись.
_probe = dict(a["named"]["probe0"])
_probe["raw"] = _probe["raw"][:-1] + ("0" if _probe["raw"][-1] != "0" else "1")
assert _probe != b["named"]["probe0"], "сравнение слепое: испорченный raw признан равным"
_d2 = hashlib.sha256(b"x").hexdigest()
assert _d2 != a["fuzz_digest"], "digest вырожден"
print("UNIT_OK 3: сравнение зрячее (мутация одного полубайта ловится)")

print("ПРОШЛИ: эквивалентность подписи native <-> coincurve на боевых формах "
      "(%d подписей, 0 расхождений; native %.2fс против coincurve %.2fс = x%.1f)"
      % (a["signs"], a["t_named"] + a["t_fuzz"], b["t_named"] + b["t_fuzz"],
         (a["t_named"] + a["t_fuzz"]) / max(1e-9, b["t_named"] + b["t_fuzz"])))
