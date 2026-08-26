"""TIP-ПОЛОСА ПРОБ ГЛАВНОГО КОШЕЛЬКА (вскрытие гонки 50480780, 26.08).

Класс дефекта: константа, снятая с одного замера (пол 1.0 mgwei, 11.08), против ЖИВОЙ
величины. Решающий блок каскада держал весь хвост на 1.3 mgwei: наш фикс-пол сортировался
позади всей полосы (суб-блок сортирует по УБЫВАНИЮ tip) и не проходил отсечку включения —
проба ждала 27 блоков при годном maxFee. Пол-tip = запрет участия в каскадный блок по
построению.

Фикс: пол подтягивается к МОДЕ живой полосы из зеркала транзитов (probe_tip_band),
клэмп [статический пол, SPEC_FIRE_TIP_TRACK_CAP_WEI], незнание -> пол.

Приёмка:
(а) реплей гонки: полоса 1.3M в зеркале -> tip проб 1.3M (был 1.0M);
(б) выброс (12.9M в кольце) моду НЕ сдвигает — мода, не медиана;
(в) разброд (мода <1/3) = «не знаю» -> статический пол (незнание не открывает трату);
(г) пустое/протухшее зеркало -> пол бит-в-бит;
(д) полоса выше капа -> клэмп 3.0M; полоса ниже пола -> пол (named limit: против транзита
    ниже пола мы и раньше стояли впереди — не регресс);
(е) откат SPEC_FIRE_TIP_TRACK=0 -> пол бит-в-бит без кода;
(ж) КРАСНЫЙ-НА-БАЗЕ: прежняя формула на живой полосе 1.3M вернула бы 1.0M;
(з) гард дрейфа: пре-подписанные лестницы ног (probe_tip_for/Ш24) НЕ задеты.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.execute as ex  # noqa: E402
import chain.spec_fire_live as sfl  # noqa: E402

FLOOR = ex._PROBE_TIP_WEI                                  # статический пол (1.0 mgwei)
# ЧАСЫ РЕАЛЬНЫЕ: _probe_tip_wei зовёт probe_tip_band() без now (боевой путь живёт на
# time.time()), синтетический NOW сделал бы все сэмплы протухшими ровно для потребителя.
import time as _t  # noqa: E402
NOW = _t.time()


def seed(samples, now=NOW):
    """Чистое зеркало + сэмплы (tip_wei, age_sec)."""
    with ex._TRANSIT_TIPS_LOCK:
        ex._TRANSIT_TIPS.clear()
    for w, age in samples:
        ex.note_transit_tip(w, agg="0xagg", now=now - age)


# ---- (г) пустое зеркало -> пол бит-в-бит ----
seed([])
check("(г) пустое зеркало: band=None, tip = статический пол",
      ex.probe_tip_band(now=NOW) is None
      and sfl._probe_tip_wei(0.05) == min(FLOOR, int(sfl.SPEC_FIRE_TIP_CAP_GWEI * 1e9)),
      (ex.probe_tip_band(now=NOW), sfl._probe_tip_wei(0.05)))

# ---- (а) РЕПЛЕЙ ГОНКИ: полоса 1.3M -> tip 1.3M ----
seed([(1_300_000, 10), (1_300_000, 30), (1_300_000, 60), (1_300_000, 90)])
check("(а) реплей 50480780: полоса 1.3M в зеркале -> tip проб 1.3M (был бы 1.0M)",
      ex.probe_tip_band(now=NOW) == 1_300_000
      and sfl._probe_tip_wei(0.05) == 1_300_000, sfl._probe_tip_wei(0.05))

# ---- (б) выброс моду не сдвигает ----
seed([(1_300_000, 10), (1_300_000, 30), (1_300_000, 60), (1_300_000, 90), (12_900_000, 5)])
check("(б) выброс 12.9M в кольце: мода остаётся 1.3M (медиана бы поехала к дорогому)",
      ex.probe_tip_band(now=NOW) == 1_300_000, ex.probe_tip_band(now=NOW))

# ---- (в) разброд = не знаю ----
seed([(1_000_000, 10), (1_300_000, 20), (2_000_000, 30), (4_000_000, 40), (10_000_000, 50)])
check("(в) разброд (мода 1/5 < 1/3): band=None -> пол (незнание не открывает трату)",
      ex.probe_tip_band(now=NOW) is None and sfl._probe_tip_wei(0.05) == FLOOR,
      ex.probe_tip_band(now=NOW))

# ---- протухшие сэмплы не считаются ----
seed([(1_300_000, ex._TIP_BAND_TTL_SEC + 60), (1_300_000, ex._TIP_BAND_TTL_SEC + 90),
      (1_300_000, ex._TIP_BAND_TTL_SEC + 120)])
check("протухшие (> TTL %ds) сэмплы полосу не образуют" % int(ex._TIP_BAND_TTL_SEC),
      ex.probe_tip_band(now=NOW) is None, ex.probe_tip_band(now=NOW))

# ---- мало сэмплов -> не знаю ----
seed([(1_300_000, 10), (1_300_000, 20)])
check("n=2 < MIN_N=%d: полосе нужен минимум улик" % ex._TIP_BAND_MIN_N,
      ex.probe_tip_band(now=NOW) is None)

# ---- (д) клэмпы ----
seed([(5_000_000, 10), (5_000_000, 30), (5_000_000, 60)])
check("(д1) полоса 5M выше капа -> клэмп %.1fM (плата выше полосы выносит ВПЕРЁД транзита)"
      % (ex._TIP_BAND_CAP_WEI / 1e6),
      sfl._probe_tip_wei(0.05) == ex._TIP_BAND_CAP_WEI, sfl._probe_tip_wei(0.05))
seed([(500_000, 10), (500_000, 30), (500_000, 60)])
check("(д2) полоса 0.5M ниже пола -> пол (named limit: не регресс против статики)",
      sfl._probe_tip_wei(0.05) == FLOOR, sfl._probe_tip_wei(0.05))

# ---- (е) откат одним env ----
seed([(1_300_000, 10), (1_300_000, 30), (1_300_000, 60), (1_300_000, 90)])
_sv = ex._TIP_BAND_TRACK
ex._TIP_BAND_TRACK = False
check("(е) откат SPEC_FIRE_TIP_TRACK=0: полоса игнорируется, пол бит-в-бит",
      ex.probe_tip_band(now=NOW) is None and sfl._probe_tip_wei(0.05) == FLOOR)
ex._TIP_BAND_TRACK = _sv

# ---- (ж) КРАСНЫЙ-НА-БАЗЕ: прежняя формула слепа к полосе ----
def _legacy_tip():
    """Формула ДО фикса, дословно."""
    return int(min(ex._PROBE_TIP_WEI, sfl.SPEC_FIRE_TIP_CAP_GWEI * 1e9))


check("(ж) КРАСНЫЙ-НА-БАЗЕ: прежняя формула при живой полосе 1.3M вернула бы 1.0M "
      "(дефект гонки воспроизводим)",
      _legacy_tip() == 1_000_000 and sfl._probe_tip_wei(0.05) == 1_300_000,
      (_legacy_tip(), sfl._probe_tip_wei(0.05)))

# ---- (з) гарды дрейфа ----
_E = open("chain/execute.py").read()
_S = open("chain/spec_fire_live.py").read()
check("(з1) пре-подписанные лестницы ног НЕ задеты: probe_tip_for не зовёт probe_tip_band",
      "probe_tip_band" not in _E[_E.index("def probe_tip_for"):
                                 _E.index("def mirror_slots_changed")])
check("(з2) причина записана в коде (27 блоков, запрет по построению)",
      "27 блоков" in _E and "по ПОСТРОЕНИЮ" in _E.replace("ПО ПОСТРОЕНИЮ", "по ПОСТРОЕНИЮ"))
check("(з3) потребитель клэмпит [пол, кап] и падает на пол при band=None",
      "max(band, floor)" in _S and "_TIP_BAND_CAP_WEI" in _S
      and "return floor" in _S[_S.index("def _probe_tip_wei"):
                               _S.index("def _receipt_has_win")])
check("(з4) мода, не медиана — решение записано",
      "медиана между двумя классами" in _E or "медиану бы сдвинул" in _E)
check("(з5) откат одним env записан в коде", 'SPEC_FIRE_TIP_TRACK", "1"' in _E)

# чистим зеркало за собой (модуль общий с другими стендами)
with ex._TRANSIT_TIPS_LOCK:
    ex._TRANSIT_TIPS.clear()

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: tip проб следует живой полосе транзитов; незнание и откат падают "
                     "на пол; лестницы ног не задеты"))
sys.exit(1 if FAILS else 0)
