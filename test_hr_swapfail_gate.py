"""27.08 HEALTHY-RACE не стреляет по цели, у которой СВОП только что не собрался.

Инцидент 16:15:54Z (0xb3920b96/0xAA80d36B): в 16:15:13 симуляция ревертнула SwapFailed и путь
сказал "HOT skip: preconf sim revert", а через 41с тот же реверт пришёл как "healthy" -> HR
классифицировал это как лаг оракула и отправил РЕАЛЬНУЮ tx. Она упала нашим же CannotRepay()
(0x2206a00f). Замер выхода: полный сейз 4 119 453 stkWELL выкупается за $242 при долге $11 554
(импакт -95.5%) — приз $1 465 считался по цене ОРАКУЛА, а не по достижимому выходу.

Проверяем: (1) память об отказе свопа закрывает HR; (2) она ПРОТУХАЕТ (гард не вечный);
(3) чистая цель по-прежнему проходит — ПОЗИТИВНЫЙ КОНТРОЛЬ, без него «всё закрыто» неотличимо
от «гейт сломал всё»; (4) селектор CannotRepay ловится ЯВНО (он в бакете "other", не "swapfailed").
"""
import sys
sys.path.insert(0, ".")
import chain.healthy_race as hr
from chain.hotpath import _diag_revert_bucket

MID, BOR = "0xb3920b96dec75b6a1144b71f963f30236fb200f3e33e93c2e9c0d222c1fa53c2", "0xAA80d36B"
NOW = 1_700_000_000.0
KW = dict(hf_est=0.9889, prize_net_usd=1465.0, data_age_s=0.0, borrow_shares=10**15,
          episode=True, corpse=False)

hr.HR_ON = True
hr._SWAP_FAIL.clear()

# (3) ПОЗИТИВНЫЙ КОНТРОЛЬ ПЕРВЫМ: без отказа свопа цель обязана проходить
st, why = hr.classify(MID, BOR, now=NOW, **KW)
assert st == "HEALTHY_RACE", ("позитивный контроль: чистая цель не проходит (%s / %s) — "
                              "дальше ноль ничего не доказывает" % (st, why))
print("UNIT_OK 1: позитивный контроль — чистая цель проходит HEALTHY_RACE")

# (1) свежий отказ свопа закрывает
hr._RECENT.clear()
hr.note_swap_fail(MID, BOR, "SwapFailed", now=NOW)
st, why = hr.classify(MID, BOR, now=NOW + 5, **KW)
assert st == "SKIP" and "выход сломан" in why, (st, why)
assert hr.COUNTERS.get("hr_swapfail_skips", 0) >= 1, hr.COUNTERS
print("UNIT_OK 2: свежий SwapFailed закрывает HR — %s" % why)

# (2) протухает
hr._RECENT.clear()
st, why = hr.classify(MID, BOR, now=NOW + hr.HR_SWAPFAIL_TTL_SEC + 1, **KW)
assert st == "HEALTHY_RACE", ("гард обязан протухать, иначе одна осечка глушит цель навсегда: %s"
                              % (why,))
print("UNIT_OK 3: гард протухает через %.0fс — цель снова доступна" % hr.HR_SWAPFAIL_TTL_SEC)

# (4) CannotRepay в бакете "other" -> ловим по селектору, а не по бакету
assert _diag_revert_bucket("('0x2206a00f', '0x2206a00f')") == "other", \
    "если CannotRepay попал в свой бакет — упростить проводку в hotpath"
assert _diag_revert_bucket("('0x81ceff30',)") == "swapfailed"
src = open("chain/hotpath.py").read()
i = src.index("_diag_bucket = _diag_revert_bucket(")
blk = src[i:i + 1200]
assert "0x2206a00f" in blk and "note_swap_fail" in blk, \
    "проводка потеряна: CannotRepay обязан запоминаться явным селектором рядом с бакетом"
print("UNIT_OK 4: CannotRepay ловится селектором (его бакет — other), проводка на месте")

# соседняя цель не задета
hr._SWAP_FAIL.clear(); hr._RECENT.clear()
hr.note_swap_fail(MID, "0xDEAD", "SwapFailed", now=NOW)
st, _ = hr.classify(MID, BOR, now=NOW + 5, **KW)
assert st == "HEALTHY_RACE", "отказ по ЧУЖОЙ цели не должен глушить эту"
print("UNIT_OK 5: память пер-цель — соседний заёмщик не задет")
print("=== HR swapfail-гейт: 5/5 ===")

# --- 6. РЕАЛЬНЫЙ РЕВЕРТ ТОЖЕ УЧИТ ГЕЙТ (27.08, второй инцидент) --------------------------
# 16:15 и 18:38 — два выстрела по одной цели, оба упали нашим CannotRepay(0x2206a00f), по $0.10.
# Первый фикс писал память ТОЛЬКО из симуляции, поэтому второй выстрел прошёл: сим в тот раз
# не ревертил свопом, а исполненная tx — да. Реальный реверт сильнее симуляции и обязан учить.
src_ex = open("chain/execute.py").read()
i = src_ex.index('log.warning("liquidate revert %s/%s reason=%s tx=%s"')
blk = src_ex[i:i + 1400]
assert "0x2206a00f" in blk and "0x81ceff30" in blk and "note_swap_fail" in blk, \
    "реальный реверт не учит гейт: тот же выстрел повторится"
assert "except Exception" in blk, "запись обязана быть СБОКУ (бросок не трогает денежный путь)"
print("UNIT_OK 6: реальный реверт (CannotRepay/SwapFailed) пишет память гейта")

# поведенчески: после отметки из tx-ветки классификатор закрыт
hr._SWAP_FAIL.clear(); hr._RECENT.clear()
hr.note_swap_fail(MID, BOR, "CannotRepay(tx)", now=NOW)
st, why = hr.classify(MID, BOR, now=NOW + 3, **KW)
assert st == "SKIP" and "CannotRepay(tx)" in why, (st, why)
print("UNIT_OK 7: отметка из tx-ветки закрывает HEALTHY_RACE — %s" % why)
print("=== HR swapfail-гейт: 7/7 ===")

# --- 8. ОТСРОЧКА РАСТЁТ НА ПОВТОРАХ ------------------------------------------------------
# Плоские 300с = структурно непродаваемая цель простреливается каждые 5 минут (~$1.2/час газа).
hr._SWAP_FAIL.clear(); hr._RECENT.clear()
T = hr.HR_SWAPFAIL_TTL_SEC
hr.note_swap_fail(MID, BOR, "CannotRepay(tx)", now=NOW)
assert hr.swap_fail_fresh(MID, BOR, now=NOW + T - 1) is not None
assert hr.swap_fail_fresh(MID, BOR, now=NOW + T + 1) is None, "первое окно = ровно TTL"
hr.note_swap_fail(MID, BOR, "CannotRepay(tx)", now=NOW + T + 1)          # повтор
assert hr.swap_fail_fresh(MID, BOR, now=NOW + T + 1 + 2 * T - 1) is not None, \
    "второй отказ обязан УДВОИТЬ окно молчания"
hr.note_swap_fail(MID, BOR, "CannotRepay(tx)", now=NOW + T + 1 + 2 * T + 1)
assert hr.swap_fail_fresh(MID, BOR, now=NOW + T + 1 + 2 * T + 1 + 4 * T - 1) is not None, \
    "третий отказ — вчетверо"
# ПОЗИТИВНЫЙ КОНТРОЛЬ: цель, которая ПОЧИНИЛАСЬ, не наказана навсегда
hr._SWAP_FAIL.clear()
hr.note_swap_fail(MID, BOR, "SwapFailed", now=NOW)
assert hr.swap_fail_fresh(MID, BOR, now=NOW + hr.HR_SWAPFAIL_BACKOFF_MAX + 1) is None, \
    "отсрочка обязана иметь потолок — иначе одна осечка глушит цель навсегда"
print("UNIT_OK 8: окно молчания удваивается на повторах (%.0f -> %.0f -> %.0f с), потолок %.0f с"
      % (T, 2 * T, 4 * T, hr.HR_SWAPFAIL_BACKOFF_MAX))
print("=== HR swapfail-гейт: 8/8 ===")
