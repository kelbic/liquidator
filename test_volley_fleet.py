"""Юниты флота залповых EOA (07.08, «добавляем столько ног сколько нужно» kelbic).

Замер конкурента 07.08 (блок 49642833): 2592 tx от 2592 РАЗНЫХ EOA в ОДНОМ блоке — 2590
холостых зондов ровно по 27260 газа плюс один исполнитель на 709k с логами. Восемь env-имён
такую стену не опишут, отсюда keyfile.

ГЛАВНОЕ, ЧТО ТУТ ПРОВЕРЯЕТСЯ:
  1. keyfile с плохими правами НЕ грузится (приватки залповых ног — на боевой машине);
  2. ключи не дублируются: два имени одного адреса = один nonce-поток на две ноги = затор;
  3. ноги режутся на ПАЧКИ — тред-на-ногу при десятках ног душит два ядра (есть инциденты);
  4. расстановка фаз при этом не портится: сдвиг считается от НАЧАЛА ЗАЛПА по индексу ноги,
     а не накапливается поверх времени отправки.
Без сети.
"""
import os
import sys
import threading
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.execute as ex
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive

sfl_mod.SPEC_FIRE_PEAK_OWED = False
TMP = os.environ.get("CLAUDE_JOB_DIR", "/tmp") + "/tmp"
os.makedirs(TMP, exist_ok=True)
KF = os.path.join(TMP, "fleet_keys.txt")

# детерминированные ключи (тестовые, никакой связи с боевыми)
KEYS = ["0x" + ("%064x" % (0x1000 + i)) for i in range(12)]


def reset_registry():
    ex._VOLLEY_ACCTS = None
    for k in list(os.environ):
        if k.startswith("SPEC_FIRE_EOA_KEY_"):
            del os.environ[k]
    os.environ.pop("SPEC_FIRE_EOA_KEYFILE", None)
    os.environ.pop("SPEC_FIRE_EOA_MAX", None)


def write_keyfile(keys, mode=0o600):
    with open(KF, "w") as f:
        f.write("\n".join(keys) + "\n")
    os.chmod(KF, mode)


# ---- 1. keyfile с открытыми правами НЕ грузится ----
reset_registry()
write_keyfile(KEYS[:5], mode=0o644)
os.environ["SPEC_FIRE_EOA_KEYFILE"] = KF
assert ex.volley_accounts() == [], "keyfile 0644 обязан быть отвергнут — это приватки"
print("UNIT_OK: keyfile с правами 0644 не грузится (ключи не берём из читаемого миром файла)")

# ---- 2. keyfile 0600 грузится ----
reset_registry()
write_keyfile(KEYS[:5])
os.environ["SPEC_FIRE_EOA_KEYFILE"] = KF
accs = ex.volley_accounts()
assert len(accs) == 5, len(accs)
print("UNIT_OK: keyfile 0600 даёт %d ног" % len(accs))

# ---- 3. env-ключи и флот складываются, дубли схлопываются ----
reset_registry()
os.environ["SPEC_FIRE_EOA_KEY_2"] = KEYS[0]        # тот же ключ, что первая строка файла
os.environ["SPEC_FIRE_EOA_KEY_3"] = KEYS[9]
write_keyfile(KEYS[:5])
os.environ["SPEC_FIRE_EOA_KEYFILE"] = KF
accs = ex.volley_accounts()
addrs = [a for a, _ in accs]
assert len(addrs) == len(set(addrs)), "дубль адреса = два nonce-потока на один EOA = затор"
assert len(accs) == 6, accs                        # KEYS[0], KEYS[9] + KEYS[1..4]
print("UNIT_OK: env + keyfile складываются, дубль адреса схлопнут (%d ног)" % len(accs))

# ---- 4. мусор и комментарии в keyfile не роняют реестр ----
reset_registry()
with open(KF, "w") as f:
    f.write("# комментарий\n\n" + KEYS[0] + "\n   \n" + KEYS[1] + "\n")
os.chmod(KF, 0o600)
os.environ["SPEC_FIRE_EOA_KEYFILE"] = KF
assert len(ex.volley_accounts()) == 2
print("UNIT_OK: пустые строки и комментарии в keyfile пропускаются")

# ---- 5. отсутствующий keyfile не ломает env-ключи ----
reset_registry()
os.environ["SPEC_FIRE_EOA_KEY_2"] = KEYS[0]
os.environ["SPEC_FIRE_EOA_KEYFILE"] = os.path.join(TMP, "нет-такого-файла")
assert len(ex.volley_accounts()) == 1
print("UNIT_OK: пропавший keyfile не гасит именованные ключи")

# ---- 5b. СТУПЕНЬ включения: SPEC_FIRE_EOA_MAX режет флот, keyfile не трогаем ----
# Авторизация ноги на цепи — разовая (owner-only, бот стоит), поэтому на цепи открываем ВЕСЬ
# флот, а сколько ног стреляет — правка .env. Срез обязан быть с НАЧАЛА и одинаковый между
# рестартами: иначе на каждом подъёме стреляют другие ноги и nonce-потоки скачут по флоту.
def _load(keys, cap=None):
    reset_registry()
    write_keyfile(keys)
    os.environ["SPEC_FIRE_EOA_KEYFILE"] = KF
    if cap is not None:
        os.environ["SPEC_FIRE_EOA_MAX"] = cap
    return [a for a, _ in ex.volley_accounts()]


full = _load(KEYS[:10])
assert len(full) == 10, full
step = _load(KEYS[:10], "4")
assert step == full[:4], "срез обязан идти с начала списка и совпадать между подъёмами"
assert _load(KEYS[:10], "999") == full, "ступень выше числа ног не режет ничего"
assert _load(KEYS[:10], "0") == full, "0 = без ограничения"
# опечатка в переменной НЕ должна глушить залп: молчащий флот выглядит как исправный
assert _load(KEYS[:10], "двадцать") == full, "мусор в ступени = без ограничения, не пустой флот"
reset_registry()
print("UNIT_OK: ступень SPEC_FIRE_EOA_MAX режет флот с начала; перебор и мусор не глушат ноги")

# ---- 6. пол патрона настраиваемый ----
import importlib
os.environ["SPEC_FIRE_VOLLEY_FLOOR_ETH"] = "0.0003"
importlib.reload(ex)
assert ex._VOLLEY_BAL_FLOOR_WEI == int(0.0003e18), ex._VOLLEY_BAL_FLOOR_WEI
del os.environ["SPEC_FIRE_VOLLEY_FLOOR_ETH"]
importlib.reload(ex)
assert ex._VOLLEY_BAL_FLOOR_WEI == int(0.003e18), "дефолт обязан остаться прежним"
print("UNIT_OK: пол патрона настраиваемый, дефолт не изменился")

# ---- 7. ПАЧКИ: тредов не больше лимита, ноги все отработали, фаза не поехала ----
SENT = []
SLEEPS = []
NAMES = set()
CLOCK = {"t": 0.0}


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l, bonus=0.05)


FAT = "0x" + "f" * 40
shared = types.SimpleNamespace(lock=threading.Lock(), groups={"0xbtc": [FAT]},
                               debt_by={("0xbtc", FAT): 1_500_000.0},
                               by_id={"0xbtc": mk("cbBTC", "USDC")})
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)
N_LEGS = 24


def _sleep(s):
    SLEEPS.append(s)
    CLOCK["t"] += max(0.0, s) + 0.004        # +сеть: как будто отправка стоила 4мс


def _send(rpc, cfg_, addr, key, cd, gas, **kw):
    NAMES.add(threading.current_thread().name)
    SENT.append((addr, CLOCK["t"]))
    CLOCK["t"] += 0.004
    return "0xv%d" % len(SENT)


sf = SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                  dev_by={"BTC": 0.0010},
                  build_fn=lambda m, b, d: {"calldata": "0xde", "gas": 625_000},
                  send_fn=lambda c, g: "0xh", receipt_fn=lambda h: (0, 0.011),
                  log_fn=lambda m: None, now=lambda: CLOCK["t"], sleep=_sleep,
                  volley_accounts_fn=lambda: [("0x%040x" % i, "k%d" % i) for i in range(N_LEGS)],
                  volley_send_fn=_send)
sf._collect_volley = lambda *a, **k: None
sf._cascade_active = lambda: True
sf._volley({"calldata": "0xde", "gas": 625_000}, "0xbtc", FAT, 1_500_000.0, "BTC")
def drain(deadline=15.0):
    """Ждать ВСЕ залповые треды. Просто enumerate() сразу после _volley не годится: тред
    залпа только собирается породить пачки, и мы бы померили пустоту (тот же класс, что
    «тест, который может вернуть только ноль»)."""
    import time as _t
    end = _t.monotonic() + deadline
    while _t.monotonic() < end:
        live = [t for t in threading.enumerate()
                if t is not threading.current_thread() and t.name.startswith("volley")]
        if not live:
            return True
        for t in live:
            t.join(timeout=0.2)
    return False


assert drain(), "залповые треды не завершились"
assert len(SENT) == N_LEGS, "все %d ног обязаны отработать, ушло %d" % (N_LEGS, len(SENT))
assert len(NAMES) <= sfl_mod.SPEC_FIRE_VOLLEY_MAX_THREADS, \
    "тредов %d > лимита %d — на двух ядрах это душит боевой контур" % (
        len(NAMES), sfl_mod.SPEC_FIRE_VOLLEY_MAX_THREADS)
print("UNIT_OK: %d ног отработали в %d тредах (лимит %d)" % (
    N_LEGS, len(NAMES), sfl_mod.SPEC_FIRE_VOLLEY_MAX_THREADS))

# фаза: ни одна нога не спит БОЛЬШЕ своего номинала — значит сдвиги не накапливаются поверх
# времени отправки (ровно тот дефект, что чинили 07.08: «расписание задавала сеть»)
P = sfl_mod.SPEC_FIRE_VOLLEY_PHASE_SEC
assert all(s <= P * N_LEGS + 1e-9 for s in SLEEPS), max(SLEEPS)
assert any(s < P * N_LEGS for s in SLEEPS), "компенсация обязана срабатывать хоть где-то"
print("UNIT_OK: сдвиг компенсируется временем отправки — фаза наша, не сетевая")

# ---- 8. пачки не теряют ног при лимите=1 (вырожденный случай) ----
SENT.clear(); NAMES.clear(); SLEEPS.clear(); CLOCK["t"] = 0.0
_orig = sfl_mod.SPEC_FIRE_VOLLEY_MAX_THREADS
sfl_mod.SPEC_FIRE_VOLLEY_MAX_THREADS = 1
try:
    sf._volley({"calldata": "0xde", "gas": 625_000}, "0xbtc", FAT, 1_500_000.0, "BTC")
    assert drain(), "залповые треды не завершились"
    assert len(SENT) == N_LEGS, len(SENT)
    assert len(NAMES) == 1, NAMES
finally:
    sfl_mod.SPEC_FIRE_VOLLEY_MAX_THREADS = _orig
print("UNIT_OK: лимит тредов=1 — все ноги живы, просто последовательно")

reset_registry()
os.path.exists(KF) and os.remove(KF)
print("ПРОШЛИ: флот залповых EOA")
