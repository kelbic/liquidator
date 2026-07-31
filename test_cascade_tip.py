"""Юниты каскадного tip-режима (инжект now, без сети/чейна)."""
import sys, types
sys.path.insert(0, ".")
import chain.hotpath as hp

cfg = types.SimpleNamespace(tip_gwei=0.05, tip_step_gwei=0.5, tip_mid_gwei=2.0,
                            tip_big_gwei=30.0, tip_mid_repaid_usd=2000.0,
                            tip_big_repaid_usd=20000.0)


def reset():
    hp._CASCADE_UNTIL = 0.0
    hp._XMIT_BLOCK = None
    hp._XMIT_AGGS = set()


# 1) СПЛЮЩЕННАЯ лестница (31.07, решение владельца): без давки base для ЛЮБОГО приза —
#    FCFS места по tip не продаёт (30 gwei проиграли 0.0034), размер приза tip не поднимает
reset()
assert hp.dynamic_tip_gwei(cfg, 100, now=1000.0) == 0.05, "рутина"
assert hp.dynamic_tip_gwei(cfg, 5000, now=1000.0) == 0.05, "бывший mid -> base"
assert hp.dynamic_tip_gwei(cfg, 50000, now=1000.0) == 0.05, "бывший big -> base"
assert hp.dynamic_tip_gwei(cfg, 3_400_000, now=1000.0) == 0.05, "мега-приз -> base"
print("UNIT_OK: лестница сплющена — без давки base для любого приза")

# 2) один агрегатор в блоке НЕ армит; два разных — армят
reset()
assert hp.note_transmit_for_cascade("0xA", 100, now=1000.0) is False, "1 агг не давка"
assert hp.cascade_active(now=1000.0) is False
assert hp.note_transmit_for_cascade("0xB", 100, now=1000.5) is True, "2-й агг = давка"
assert hp.cascade_active(now=1000.5) is True
print("UNIT_OK: >=2 разных агрегаторов в блоке армят давку")

# 3) давка = ЕДИНСТВЕННАЯ ступень tip_step (~$0.4), независимо от приза
assert hp.dynamic_tip_gwei(cfg, 100, now=1001.0) == 0.5, "давка -> step"
assert hp.dynamic_tip_gwei(cfg, 5000, now=1001.0) == 0.5, "давка -> step (не mid)"
assert hp.dynamic_tip_gwei(cfg, 50000, now=1001.0) == 0.5, "давка -> step (не big)"
print("UNIT_OK: давка = одна ступень step, страховка ~$0.4 вместо $26")

# 4) TTL: после окна давка гаснет
assert hp.cascade_active(now=1000.5 + hp.CASCADE_TTL_SEC - 1) is True
assert hp.cascade_active(now=1000.5 + hp.CASCADE_TTL_SEC + 1) is False
assert hp.dynamic_tip_gwei(cfg, 100, now=1000.5 + hp.CASCADE_TTL_SEC + 1) == 0.05, "после TTL рутина"
print("UNIT_OK: давка гаснет по TTL")

# 5) смена блока сбрасывает счётчик агрегаторов (1 агг в новом блоке не армит)
reset()
hp.note_transmit_for_cascade("0xA", 200, now=2000.0)
assert hp.note_transmit_for_cascade("0xA", 201, now=2001.0) is False, "новый блок, 1 агг — не давка"
assert hp.cascade_active(now=2001.0) is False
print("UNIT_OK: смена блока сбрасывает набор агрегаторов")

# 6) тот же агрегатор дважды в блоке НЕ считается за двоих
reset()
hp.note_transmit_for_cascade("0xA", 300, now=3000.0)
assert hp.note_transmit_for_cascade("0xA", 300, now=3000.1) is False, "дубль агг не давка"
print("UNIT_OK: дубль одного агрегатора не армит")

# 7) проигрыш победителю с ВЫШЕ tip армит; равный/ниже — нет
reset()
assert hp.note_race_loss_for_cascade(10.0, 0.05, now=4000.0) is True, "10>0.05 армит"
assert hp.cascade_active(now=4000.0) is True
reset()
assert hp.note_race_loss_for_cascade(0.003, 0.05, now=4000.0) is False, "0.003<0.05 не армит (FCFS)"
assert hp.cascade_active(now=4000.0) is False
reset()
assert hp.note_race_loss_for_cascade(None, 0.05, now=4000.0) is False, "None не армит"
assert hp.note_race_loss_for_cascade("bad", 0.05, now=4000.0) is False, "мусор не армит"
print("UNIT_OK: проигрыш high-tip победителю армит, low-tip (FCFS) — нет")

# 8) флот-урок реального разбора 15:10: победитель 0.003 gwei против нашего 0.05 — НЕ давка
reset()
assert hp.note_race_loss_for_cascade(0.003, hp.dynamic_tip_gwei(cfg, 9900, now=5000.0), now=5000.0) is False
print("UNIT_OK: реальный разбор 15:10 (0.003 vs 0.05) не ложно-армит")

# 9) откат TIP_LADDER_LEGACY=1: прежняя (base,mid,big)+1-в-давку
reset()
hp._TIP_LADDER_LEGACY = True
assert hp.dynamic_tip_gwei(cfg, 100, now=6000.0) == 0.05, "legacy рутина"
assert hp.dynamic_tip_gwei(cfg, 5000, now=6000.0) == 2.0, "legacy mid"
assert hp.dynamic_tip_gwei(cfg, 50000, now=6000.0) == 30.0, "legacy big"
hp.note_transmit_for_cascade("0xA", 700, now=6001.0)
hp.note_transmit_for_cascade("0xB", 700, now=6001.1)
assert hp.dynamic_tip_gwei(cfg, 5000, now=6001.2) == 30.0, "legacy mid->big в давку"
hp._TIP_LADDER_LEGACY = False
reset()
assert hp.dynamic_tip_gwei(cfg, 5000, now=7000.0) == 0.05, "возврат к сплющенной"
print("UNIT_OK: откат TIP_LADDER_LEGACY возвращает прежнюю лестницу")

# 10) шаг давки не опускается НИЖЕ base, если base подняли выше step
cfg_hi = types.SimpleNamespace(tip_gwei=1.0, tip_step_gwei=0.5)
reset()
hp.note_transmit_for_cascade("0xA", 800, now=8000.0)
hp.note_transmit_for_cascade("0xB", 800, now=8000.1)
assert hp.dynamic_tip_gwei(cfg_hi, 100, now=8000.2) == 1.0, "step не режет base"
print("UNIT_OK: max(base, step) — давка никогда не опускает tip")

print("=== ВСЕ CASCADE-TIP ЮНИТЫ ПРОШЛИ (сплющенная лестница) ===")
