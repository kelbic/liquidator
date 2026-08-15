"""Дрель Ш24b/#104: тиры проб зеркалят TIP ТРАНЗИТА, незнание — статический портфель бит-в-бит.

Доказательство боем 49978436: транзит 0.0010, победитель 0.0010 (зеркало до wei), из наших
62 проб рабочих 21 — остальные (класс блока 0.0016-0.0030) сортируются ПЕРЕД транзитом.
"""
import os
import sys

os.environ.setdefault("SPEC_FIRE_PROBE_TIP_MIRROR", "1")

import chain.execute as ex                                    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def reset():
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()


ADDRS = ["0x" + ("%040x" % i) for i in range(1, 9)]           # адреса всех 4 классов hash%4


def tiers_for(addrs):
    return sorted({ex.probe_tip_for(a) for a in addrs})


# --- 1. Границы записи: мусор и вне-диапазона отбрасываются ---
reset()
check("запись: мусор отброшен", ex.note_transit_tip("x") is False)
check("запись: 100 wei вне границ", ex.note_transit_tip(100) is False)
check("запись: -1 (дефолт индекса) отброшен", ex.note_transit_tip(-1) is False)
check("запись: 1e6 принят", ex.note_transit_tip(1_000_000) is True)

# --- 2. Мало данных -> None -> статический портфель ---
reset()
for _ in range(3):
    ex.note_transit_tip(1_000_000)
check("мало данных: зеркала нет", ex.transit_tip_mirror() is None)
check("мало данных: тиры статические", tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))

# --- 3. Счастливый путь: мода 1e6 (8 из 12), вторая 1.2e6 ---
reset()
for _ in range(8):
    ex.note_transit_tip(1_000_000)
for _ in range(4):
    ex.note_transit_tip(1_200_000)
mm = ex.transit_tip_mirror()
check("зеркало живо", mm == (1_000_000, 1_200_000), str(mm))
t = tiers_for(ADDRS)
check("тиры = {мода, вторая}", t == [1_000_000, 1_200_000], str(t))
n_mode = sum(1 for a in ADDRS if ex.probe_tip_for(a) == 1_000_000)
check("3 класса из 4 на моде", n_mode == 6, "мода у %d из 8 адресов" % n_mode)   # 8 адресов = 2 полных цикла hash%4

# --- 4. Разброд (доля моды < 50%) -> None ---
reset()
for w in (1_000_000, 1_200_000, 1_500_000, 1_775_000, 1_000_000, 1_200_000, 1_500_000, 1_775_000):
    ex.note_transit_tip(w)
check("разброд: зеркала нет", ex.transit_tip_mirror() is None)

# --- 5. TTL: протухшие записи не считаются ---
reset()
import time
old = time.time() - ex._MIRROR_TTL_SEC - 10
for _ in range(8):
    ex.note_transit_tip(1_000_000, now=old)
check("TTL: протухло -> None", ex.transit_tip_mirror() is None)

# --- 6. Кольцо K: старые значения выталкиваются ---
reset()
for _ in range(ex._MIRROR_K):
    ex.note_transit_tip(3_000_000)
for _ in range(ex._MIRROR_K):
    ex.note_transit_tip(1_000_000)                            # вытолкнули все 3e6
mm = ex.transit_tip_mirror()
check("кольцо: свежая мода победила", mm is not None and mm[0] == 1_000_000, str(mm))

# --- 7. ОТКАТ: PROBE_TIP_MIRROR=0 -> статика бит-в-бит даже при живом зеркале ---
_saved = ex.PROBE_TIP_MIRROR
ex.PROBE_TIP_MIRROR = False
try:
    check("откат: статические тиры", tiers_for(ADDRS) == sorted(set(ex._PROBE_TIP_TIERS)))
finally:
    ex.PROBE_TIP_MIRROR = _saved

# --- 8. ПРОВОДКА: индекс зовёт подписчика, main армирует, лестница ходит через probe_tip_for ---
import chain.chain_index as ci
src_ci = open(ci.__file__).read()
check("проводка: set_on_transit в индексе", "set_on_transit" in src_ci and "_cbt(bn, _tr)" in src_ci)
src_main = open(os.path.join(os.path.dirname(os.path.dirname(ex.__file__)), "main.py")).read()
check("проводка: арм явный в main", "note_transit_tip" in src_main and "set_on_transit" in src_main)
src_ex = open(ex.__file__.replace(".pyc", ".py")).read()
check("проводка: presign_ladder через probe_tip_for", "probe_tip_for" in src_ex)
check("боевой дефолт: зеркало включено", ex.PROBE_TIP_MIRROR is True)

if FAILS:
    print("=== ДРЕЛЬ #104 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ #104 ЗЕЛЁНАЯ: тиры зеркалят транзит, незнание/разброд/откат — статический портфель ===")
