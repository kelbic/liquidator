"""Дрель #54: выручка победы пробы видна kill-switch'у.

Победа пробы (движок/доска) раньше не писала ничего в actions — выигрышный день выглядел
чистым расходом. Теперь: _book_probe_revenue (сбоку, через _accounted) -> модульный шов
_REVENUE_BOOK (арм ЯВНЫЙ в main). Канон: незнание НЕ приписывает выручку; store фейковый.
"""
import os
import re
import sys
import types

import chain.spec_fire_live as sfl                            # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


LIQ_ADDR = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"


def mk_receipt(loan, profit, addr=LIQ_ADDR):
    """Квитанция с событием Liquidated(borrower indexed, loan indexed, profit) от addr —
    раскладка как в parse_batch_receipt: borrower/loan в topics[1..2], profit в data."""
    from eth_utils import keccak
    sig = "0x" + keccak(text="Liquidated(address,address,uint256)").hex()
    t_borrower = "0x" + "00" * 12 + "47" * 20
    t_loan = "0x" + "00" * 12 + loan[2:].lower()
    data = "0x" + int(profit).to_bytes(32, "big").hex()
    return {"logs": [{"address": addr, "topics": [sig, t_borrower, t_loan], "data": data}]}


class FakeSelf:
    """Носитель _book_probe_revenue без конструктора SpecFireLive."""
    def __init__(self):
        self.cfg = types.SimpleNamespace(liquidator_address=LIQ_ADDR, eth_price_usd=4000.0)
        self.stats = {}


booked = []

# --- 1. Без арма — тишина (канал закрыт по умолчанию) ---
sfl.set_revenue_book(None)
fs = FakeSelf()
sfl.SpecFireLive._book_probe_revenue(fs, "0xAAA", mk_receipt(USDC, 142_000_000))
check("без арма: не книжится", len(booked) == 0 and "revenue_booked_usd" not in fs.stats)

# --- 2. Арм + USDC-профит: книжится в долларах ---
sfl.set_revenue_book(lambda b, usd, h: booked.append((b, usd, h)))
try:
    fs = FakeSelf()
    sfl.SpecFireLive._book_probe_revenue(fs, "0xBBB", mk_receipt(USDC, 142_000_000))
    check("USDC: одна запись", len(booked) == 1)
    if booked:
        b, usd, h = booked[0]
        check("USDC: $142.00", abs(usd - 142.0) < 1e-9, str(usd))
        check("USDC: хэш проведён", h == "0xBBB")
    check("USDC: счётчик приёмки", abs(fs.stats.get("revenue_booked_usd", 0) - 142.0) < 1e-9)

    # --- 3. WETH: конвертация через cfg.eth_price_usd ---
    booked.clear()
    fs = FakeSelf()
    sfl.SpecFireLive._book_probe_revenue(fs, "0xCCC", mk_receipt(WETH, 10**16))   # 0.01 ETH
    check("WETH: $40 при eth=4000", booked and abs(booked[0][1] - 40.0) < 1e-9,
          str(booked))

    # --- 4. Неизвестный займ: НЕ книжится (незнание не приписывает) ---
    booked.clear()
    fs = FakeSelf()
    sfl.SpecFireLive._book_probe_revenue(fs, "0xDDD", mk_receipt("0x" + "99" * 20, 10**18))
    check("неизвестный займ: тишина", len(booked) == 0)

    # --- 5. Чужое Liquidated (не наш контракт): фантомная прибыль запрещена ---
    booked.clear()
    fs = FakeSelf()
    sfl.SpecFireLive._book_probe_revenue(fs, "0xEEE", mk_receipt(USDC, 10**9, addr="0x" + "88" * 20))
    check("чужой эмитент: тишина", len(booked) == 0)
finally:
    sfl.set_revenue_book(None)

# --- 6. ПРОВОДКА: вызов стоит в _engine_result_verdict ЗА _probe_win, через _accounted ---
src = open(os.path.join(os.path.dirname(sfl.__file__), "spec_fire_live.py")).read()
# Ищем ВЫЗОВ по устойчивому признаку (имя + источник), а не по точной сигнатуре: 27.08 в вызов
# добавили receipt=r, и точный литерал перестал находиться — тест краснел, ХОТЯ проводка цела.
# Проверяемое требование не в форме аргументов, а в том, что книжка стоит ЗА деньгами и СБОКУ
# (через _accounted, чтобы бросок учёта не трогал огонь — правило р-7).
i_win = next((m.start() for m in re.finditer(r'self\._probe_win\([^)]*src="движок"', src)), -1)
check("проводка: книжка после _probe_win, сбоку",
      i_win > 0 and '_accounted("выручка победы"' in src[i_win:i_win + 300],
      "вызов _probe_win(src=движок) не найден" if i_win < 0 else "найден, но книжки за ним нет")
src_main = open(os.path.join(os.path.dirname(os.path.dirname(sfl.__file__)), "main.py")).read()
check("проводка: арм явный в main + фильтр kill-switch",
      "set_revenue_book" in src_main and "submitted:1 probe-win" in src_main)
check("проводка: газ не двоится (gas_usd=0.0 в main)", "gas_usd=0.0" in src_main)

if FAILS:
    print("=== ДРЕЛЬ #54 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ #54 ЗЕЛЁНАЯ: выручка побед проб видна kill-switch'у, фантом и незнание не книжатся ===")
