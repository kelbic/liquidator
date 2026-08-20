"""ОГРЫЗОК ПОСЛЕ НАШЕГО CAP-ВЫСТРЕЛА — ТРУП (20.08).

ЧТО ПРОИСХОДИТ В БОЮ. Кап-ветка `_cap_repaid_shares` забирает почти весь залог и оставляет
пыль: замер форк-стенда (test_fork_route_cbbtc_weth П5) — залог 3 000 000 -> 2 999 (10 bps).
Долг при этом остаётся, позиция ПЕРЕЖИВАЕТ выстрел, и дальше вечно жжёт подготовку: греется,
готовится, ревертит по CannotRepay. Прежние два признака трупа её не берут ПО ПОСТРОЕНИЮ:
bad_debt=0 (Morpho ничего не списал), repaid < borrow_shares (мы и просили меньше долга).

ПОЧЕМУ ПОРОГ 20 bps, а не «на глаз»: наш кап целится в collateral минус max(ASK_SHAVE_BPS=10,
живой дрейф) плюс 1-3 единицы округления => остаток НАШЕГО выстрела заведомо <= ~10 bps.
Двойной запас. Остаток ЧУЖОЙ частичной ликвидации на порядки больше (88.7% ликвидаций
частичные, типичный остаток — десятки процентов), поэтому порог их не задевает даже случайно.

ГЛАВНЫЙ РИСК, ради которого стенд и написан: ЛОЖНЫЙ ТРУП = отдать живую цель без боя. Поэтому
правило применяется ТОЛЬКО к целям, где ОБА факта наши: кап-план построен НАМИ и выстрел
ПОДТВЕРЖДЁН (квитанция либо лог Morpho с нашим caller).

ЗАПУСК: python3 test_cap_corpse.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.hotpath as hp                                   # noqa: E402

FAILS = []
MID = "0x" + "5d" * 32
B1 = "0x" + "a1" * 20                       # наша цель: кап-выстрел
B2 = "0x" + "b2" * 20                       # чужая частичная ликвидация
B3 = "0x" + "c3" * 20                       # обычный наш выстрел (не кап)
COLL_BEFORE = 3_000_000                     # предвыстрельный залог (числа форк-стенда)
COLL_STUB = 2_999                           # остаток после нашего кап-выстрела: 10.00 bps


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


def reset():
    with hp._CAP_LOCK:
        hp._CAP_PLANS.clear()
        hp._CAP_SHOTS.clear()


# --- 1. КРАСНЫЙ-НА-БАЗЕ: прежние два признака огрызок НЕ берут -------------------------------
# Без этого «труп похоронен» неотличимо от «его и так хоронили».
reset()
_row_partial = (10 ** 20, 0)                # индекс видел ликвидацию, bad_debt=0, repaid < bs
check("КРАСНЫЙ-НА-БАЗЕ: без нового правила огрызок НЕ труп (bad_debt=0, repaid<bs)",
      hp.corpse_verdict(_row_partial, 10 ** 24) is False)
check("КРАСНЫЙ-НА-БАЗЕ: он же не труп и при пустом row (индекс о нём ничего не знает)",
      hp.corpse_verdict(None, 10 ** 24) is False)

# --- 2. ЗЕЛЁНЫЙ: наш кап-выстрел + пыль залога = труп ---------------------------------------
reset()
hp.note_cap_plan(MID, B1, COLL_BEFORE)
check("намерение без подтверждения ТРУПОМ НЕ ДЕЛАЕТ (выстрела ещё не было)",
      hp.corpse_verdict(None, 10 ** 24, key=(MID, B1), collateral=COLL_STUB) is False)
check("подтверждение выстрела принято", hp.note_cap_shot_confirmed(MID, B1) is True)
_dead, _left = hp.cap_corpse((MID, B1), COLL_STUB)
check("ЗЕЛЁНЫЙ: огрызок нашего кап-выстрела ПОХОРОНЕН (остаток %.2f bps <= %.0f)"
      % (_left, hp.CAP_CORPSE_MAX_BPS), _dead is True)
check("вердикт видит его и через общую дверь corpse_verdict",
      hp.corpse_verdict(None, 10 ** 24, key=(MID, B1), collateral=COLL_STUB) is True)
check("вердикт видит его и при непустом row (правило row не требует)",
      hp.corpse_verdict(_row_partial, 10 ** 24, key=(MID, B1), collateral=COLL_STUB) is True)

# --- 3. ЧУЖАЯ ЧАСТИЧНАЯ ЛИКВИДАЦИЯ С МАЛЫМ ОСТАТКОМ — НЕ ТРУП -------------------------------
# Главный риск: ложный труп отдаёт живую цель без боя. Мы про эту цель кап-плана не строили.
reset()
check("чужая частичная ликвидация: та же пыль, но НЕ наш выстрел -> НЕ труп",
      hp.corpse_verdict(_row_partial, 10 ** 24, key=(MID, B2), collateral=COLL_STUB) is False)
check("cap_corpse про неё ничего не знает и не выдумывает",
      hp.cap_corpse((MID, B2), COLL_STUB) == (False, None))
# ПОЗИТИВНЫЙ КОНТРОЛЬ НА ТОМ ЖЕ ОБЪЕКТЕ: стоит нам самим отстрелять её кап-планом — похоронится
hp.note_cap_plan(MID, B2, COLL_BEFORE)
hp.note_cap_shot_confirmed(MID, B2)
check("...а после НАШЕГО кап-выстрела по ней — хоронится (контроль не слеп)",
      hp.corpse_verdict(None, 10 ** 24, key=(MID, B2), collateral=COLL_STUB) is True)

# --- 4. ОБЫЧНЫЙ НАШ ВЫСТРЕЛ (не кап) — как раньше -------------------------------------------
reset()
check("обычный выстрел кап-плана не регистрирует", hp.note_cap_shot_confirmed(MID, B3) is False)
check("обычная цель судится ПРЕЖНИМИ признаками (жива)",
      hp.corpse_verdict(_row_partial, 10 ** 24, key=(MID, B3), collateral=COLL_STUB) is False)
check("обычная цель с bad_debt — труп, как и была",
      hp.corpse_verdict((0, 5), 10 ** 24, key=(MID, B3), collateral=COLL_STUB) is True)
check("обычная цель с repaid>=bs — труп, как и была",
      hp.corpse_verdict((10 ** 24, 0), 10 ** 24, key=(MID, B3), collateral=COLL_STUB) is True)

# --- 5. ГРАНИЦА ПОРОГА: остаток чуть выше 20 bps — НЕ труп ----------------------------------
reset()
hp.note_cap_plan(MID, B1, COLL_BEFORE)
hp.note_cap_shot_confirmed(MID, B1)
_at = int(COLL_BEFORE * hp.CAP_CORPSE_MAX_BPS / 10_000)
check("ровно на пороге (%d ед. = %.0f bps) — труп" % (_at, hp.CAP_CORPSE_MAX_BPS),
      hp.cap_corpse((MID, B1), _at)[0] is True)
check("на единицу выше порога — НЕ труп (цель, которую жуёт конкурент, не хороним)",
      hp.cap_corpse((MID, B1), _at + 1)[0] is False)
check("залог вырос (заёмщик долил) — НЕ труп",
      hp.cap_corpse((MID, B1), COLL_BEFORE)[0] is False)

# --- 6. TTL И ГИГИЕНА РЕЕСТРА ---------------------------------------------------------------
reset()
hp.note_cap_plan(MID, B1, COLL_BEFORE)
hp.note_cap_shot_confirmed(MID, B1, now=1000.0)
check("в пределах TTL — труп", hp.cap_corpse((MID, B1), COLL_STUB, now=1000.0 + 10)[0] is True)
check("за TTL — правило молчит (реестр не вечен)",
      hp.cap_corpse((MID, B1), COLL_STUB, now=1000.0 + hp.CAP_CORPSE_TTL_SEC + 1)[0] is False)
reset()
hp.note_cap_plan(MID, B1, 0)
check("нулевой предвыстрельный залог не регистрируется (делить не на что)",
      hp.note_cap_shot_confirmed(MID, B1) is False)

# --- 7. РЕГИСТР НЕЧУВСТВИТЕЛЕН К РЕГИСТРУ АДРЕСА (канон формы ключа corpse_pairs) -----------
reset()
hp.note_cap_plan(MID.upper(), B1.upper(), COLL_BEFORE)
hp.note_cap_shot_confirmed(MID, B1)
check("ключ нормализован: CHECKSUM-адрес и нижний регистр — одна цель",
      hp.corpse_verdict(None, 10 ** 24, key=(MID, B1), collateral=COLL_STUB) is True)

# --- 8. ПРОВОДКА: обе двери подтверждения и точки регистрации --------------------------------
_ex = open(pathlib.Path(__file__).parent / "chain" / "execute.py").read()
_sf = open(pathlib.Path(__file__).parent / "chain" / "spec_fire_live.py").read()
_hp = open(pathlib.Path(__file__).parent / "chain" / "hotpath.py").read()
check("подтверждение по КВИТАНЦИИ боевой tx (execute)", "note_cap_shot_confirmed" in _ex)
check("подтверждение по ЛОГУ ПРОТОКОЛА с нашим caller (v6 §7)", "note_cap_shot_confirmed" in _sf)
check("регистрация кап-плана в горячем пути", "note_cap_plan(market_id" in _hp)
check("регистрация кап-плана в пути доски", "note_cap_plan(mid, borrower, coll_amt)" in _sf)
check("горячий фильтр спрашивает вердикт с ключом и живым залогом",
      "corpse_verdict(_row, _bc[0] if _bc else None, key=_ckey, collateral=_ccol)" in _hp)

# --- 9. ГЛАВНЫЙ РИСК ЛИНЗЫ: ПРОТУХШЕЕ НАМЕРЕНИЕ + ПОЗДНИЙ ВЫСТРЕЛ != ТРУП -------------------
# Механизм ложного трупа: кап-план пишется при КАЖДОЙ сборке, гонки мы в основном ПРОИГРЫВАЕМ,
# значит намерения копятся. Без срока годности любой поздний подтверждённый выстрел по той же
# цели превратил бы многодневный снимок залога в «факт» — и живая цель, чей залог с тех пор
# вырос на порядок, похоронилась бы по ЧУЖОМУ масштабу.
reset()
hp.note_cap_plan(MID, B1, COLL_BEFORE, now=1000.0)
_late = 1000.0 + hp.CAP_PLAN_FRESH_SEC + 1
check("протухшее намерение НЕ конвертируется поздним выстрелом",
      hp.note_cap_shot_confirmed(MID, B1, now=_late) is False)
check("...и цель остаётся ЖИВОЙ (ложного трупа нет)",
      hp.corpse_verdict(None, 10 ** 24, key=(MID, B1), collateral=COLL_STUB, ) is False)
# ПОЗИТИВНЫЙ КОНТРОЛЬ НА ТОМ ЖЕ ОБЪЕКТЕ: свежее намерение тот же выстрел конвертирует
reset()
hp.note_cap_plan(MID, B1, COLL_BEFORE, now=1000.0)
check("свежее намерение (в пределах %.0fс) конвертируется" % hp.CAP_PLAN_FRESH_SEC,
      hp.note_cap_shot_confirmed(MID, B1, now=1000.0 + hp.CAP_PLAN_FRESH_SEC - 1) is True)

# --- 10. ЧИСТКА РЕЕСТРА НАМЕРЕНИЙ ------------------------------------------------------------
# Непочищенный _CAP_PLANS и был механизмом: он копится тем быстрее, чем чаще мы проигрываем.
reset()
for i in range(50):
    hp.note_cap_plan(MID, "0x%040x" % i, COLL_BEFORE, now=1000.0)
with hp._CAP_LOCK:
    _before_n = len(hp._CAP_PLANS)
hp.note_cap_shot_confirmed(MID, "0x%040x" % 999, now=1000.0 + hp.CAP_PLAN_FRESH_SEC + 5)
with hp._CAP_LOCK:
    _after_n = len(hp._CAP_PLANS)
check("реестр намерений чистится тем же проходом (%d -> %d)" % (_before_n, _after_n),
      _before_n == 50 and _after_n == 0)

# --- 11. БАТЧЕВАЯ ТОЧКА ПОДТВЕРЖДЕНИЯ --------------------------------------------------------
# Каскадный путь стреляет кап-планами чаще всех; без этой точки правило мертво именно там.
_ex2 = open(pathlib.Path(__file__).parent / "chain" / "execute.py").read()
check("подтверждение в БАТЧЕВОЙ ветке (разбор Liquidated юнита)",
      _ex2.count("note_cap_shot_confirmed") >= 3)
_batch_blk = _ex2[_ex2.index("status==1: разбор по Liquidated"):]
check("...и оно стоит именно в ветке ВЗЯТО (_key in wins)",
      "note_cap_shot_confirmed" in _batch_blk[:_batch_blk.index("_batch_record(n_win")])

if FAILS:
    print("=== ОГРЫЗОК: %d ПРОВАЛОВ ===" % len(FAILS))
    sys.exit(1)
print("=== ОГРЫЗОК ХОРОНИТСЯ: только НАШ кап-выстрел, только по пыли залога, чужие частичные "
      "ликвидации не тронуты ===")
