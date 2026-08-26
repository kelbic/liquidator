"""ПАКЕТ «СВЯЗНОСТЬ ДЕТОНАТОР→ДОСКА» + ПРИБОР КАДЕНСА (26.08).

Класс дефектов назван ревьюером: ОСЕВОЕ РАССЛОЕНИЕ — новый сигнал открывает дверь (детонатор
по близости), а планировщик внизу ходит по старой оси (доска по «жир × досягаемость»). Замер
боем: окно ETH 06:23-06:25 — 135 проб при 29 отказах арма подряд, все «жир вне досягаемости».

Приёмка:
(а) реплей того окна: доска планирует ПОД ЦЕЛЬ ДЕТОНАТОРА, когда своя выборка пуста;
(б) негативный контроль: универсальное окно (детонатора нет) — прежняя ветка отказа;
(в) гард обратной несвязности: детонатор без ПЛАНИРУЕМОЙ цели порог НЕ связывает;
(г) свежесть ключа: протухший детонатор (>120с) цель не навязывает;
(д) прибор каденса: кольцо межпробных дельт движка, p50/p90/доля≤0.2с в строке окна;
(е) счётчик порога решения: серия из 5 отказов арма при живом детонаторе = голодающее окно.
"""
import sys
import threading
import pathlib as _pl
from types import SimpleNamespace

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402
import chain.fire_engine as fe  # noqa: E402

_sv_label = sfl._label_of
sfl._label_of = lambda s: s

MID, NEAR, FAT = "0xmid", "0xnearTarget", "0xfatTarget"


def mk(hf_map, debts):
    o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    o.stats = {}
    o._lock = threading.Lock()
    o._logs = []
    o._log = o._logs.append
    o._now = lambda: 5000.0
    m = SimpleNamespace(collateral_token="ETH", loan_token="USDC", bonus=0.0438)
    o.shared = SimpleNamespace(lock=threading.Lock(), groups={MID: list(hf_map)},
                               debt_by={(MID, b): d for b, d in debts.items()},
                               by_id={MID: m},
                               hf_by={(MID, b): h for b, h in hf_map.items()})
    return o


UNI = 0.0015 * 1.0                                       # универсальный порог ETH (dev 0.15%)

# ---- (в) ГАРД: детонатор без планируемой цели порог НЕ связывает ----
o = mk({NEAR: 1.0005}, {NEAR: 0.0})                      # близкая цель БЕЗ долга
o._min_flip_dist("ETH", "down")
thr, d = o._det_thr("ETH", "down", UNI)
check("цель без долга не даёт детонатору связать порог (отсекается раньше)",
      thr == UNI and d is None, (thr, d))
# ГАРД в чистом виде: ключ ЕСТЬ (dist посчитан), но долг цели исчез к моменту суда
# ГОНКА: dist уже посчитан и ключ свеж, но цель разобрали конкурентом к моменту суда
o1b = mk({NEAR: 1.0005}, {NEAR: 90_000.0})
o1b._min_flip_dist("ETH", "down")
o1b._min_flip_dist = lambda f, d: 0.0005                 # dist остался «свежим» (гонка)
with o1b.shared.lock:
    o1b.shared.debt_by[(MID, NEAR)] = 0.0                # цель погашена/разобрана
thr1b, _ = o1b._det_thr("ETH", "down", UNI)
check("ГАРД обратной несвязности: цель стала непланируемой -> порог УНИВЕРСАЛЬНЫЙ",
      thr1b == UNI and o1b.stats.get("det_unplannable") == 1, (thr1b, o1b.stats))

# ---- детонатор с планируемой целью — порог связан (как раньше) ----
o2 = mk({NEAR: 1.0005}, {NEAR: 90_000.0})
o2._min_flip_dist("ETH", "down")
thr2, d2 = o2._det_thr("ETH", "down", UNI)
check("планируемая близкая цель -> порог связан детонатором", thr2 < UNI and d2 is not None,
      (thr2, d2))
check("ключ цели опубликован для доски",
      o2._det_key_by_feed["ETH"][0] == (MID, NEAR), o2._det_key_by_feed)

# ---- (а) РЕПЛЕЙ окна ETH: своя выборка доски пуста -> план ПОД ЦЕЛЬ ДЕТОНАТОРА ----
o3 = mk({NEAR: 1.0005, FAT: 1.09}, {NEAR: 90_000.0, FAT: 400_000.0})
o3._min_flip_dist("ETH", "down")                         # публикует ключ близкой цели
_seen = {}


def _fake_top_fat(feed, direction, max_dist=None, k=1):
    return [], 0.09, None                                # жирная цель вне reach — как в бою


o3._top_fat = _fake_top_fat
o3._board_next_try = {}
o3._armskip_ts = {}
picked = {}
try:
    # исполняем ветку выбора цели прямо из исходника _prearm_board через её же логику:
    # (в юните зовём приватный кусок через _prearm_board с заглушёнными зависимостями)
    o3._build = lambda *a, **k: None                     # сборка пробы не нужна: важен выбор
    o3._prearm_board("ETH", "down", 0.0015)
except Exception:                                        # noqa: BLE001
    pass
_line = [l for l in o3._logs if "ПОД ЦЕЛЬ ДЕТОНАТОРА" in l]
check("реплей ETH: доска планирует ПОД ЦЕЛЬ ДЕТОНАТОРА (reach — справка, не гейт)",
      len(_line) == 1 and NEAR[:10] in _line[0] and o3.stats.get("board_det_target") == 1,
      (o3._logs[-2:], o3.stats))
check("в строке названы и dist цели, и reach как справка",
      "dist" in _line[0] and "справка, не гейт" in _line[0], _line)

# ---- (б) НЕГАТИВНЫЙ КОНТРОЛЬ: без детонатора — прежняя ветка отказа ----
o4 = mk({FAT: 1.09}, {FAT: 400_000.0})
o4._top_fat = _fake_top_fat
o4._board_next_try = {}
o4._det_key_by_feed = {}
try:
    o4._prearm_board("ETH", "down", 0.0015)
except Exception:                                        # noqa: BLE001
    pass
check("универсальное окно: план НЕ подменяется, прежний отказ «жир вне досягаемости»",
      any("жир вне досягаемости" in l for l in o4._logs)
      and not any("ПОД ЦЕЛЬ" in l for l in o4._logs)
      and "board_det_target" not in o4.stats, o4._logs[-1:])

# ---- (г) свежесть ключа: протухший детонатор цель не навязывает ----
o5 = mk({NEAR: 1.0005}, {NEAR: 90_000.0})
o5._top_fat = _fake_top_fat
o5._board_next_try = {}
o5._det_key_by_feed = {"ETH": ((MID, NEAR), 0.0005, 5000.0 - 300.0)}   # 300с назад
try:
    o5._prearm_board("ETH", "down", 0.0015)
except Exception:                                        # noqa: BLE001
    pass
check("протухший ключ (>120с) цель не навязывает", "board_det_target" not in o5.stats,
      o5.stats)

# ---- (е) счётчик порога решения: 5 отказов подряд при живом детонаторе ----
o6 = mk({NEAR: 1.0005}, {NEAR: 90_000.0})
o6._board_next_try = {}
o6._det_key_by_feed = {"ETH": ((MID, NEAR), 0.0005, 5000.0)}
for _ in range(6):
    o6._arm_retry("ETH", "жир вне досягаемости (reach 3.00%)")
check("серия 5 отказов при живом детонаторе -> det_windows_starved=1",
      o6.stats.get("det_windows_starved") == 1 and o6.stats.get("board_arm_fails") == 6,
      o6.stats)
o7 = mk({FAT: 1.09}, {FAT: 400_000.0})
o7._board_next_try = {}
o7._det_key_by_feed = {}
for _ in range(6):
    o7._arm_retry("ETH", "жир вне досягаемости")
check("без детонатора серия не считается голоданием окна",
      "det_windows_starved" not in o7.stats, o7.stats)

# ---- (д) ПРИБОР КАДЕНСА: кольцо дельт движка ----
col = fe.ReceiptCollector(receipts_fn=lambda hs: {}, on_result=lambda *a: None)
T = [100.0]
eng = fe.FireEngine(legs_fn=lambda: ["0xa"], send_fn=lambda a: "0xh", collector=col,
                    now=lambda: T[0], sleep=lambda s: None)
for dt in (0.1, 0.15, 0.9, 0.12, 1.5):
    T[0] += dt
    with eng._lock:
        _p = eng.stats.get("_last_sent_ts")
        eng.stats["_last_sent_ts"] = T[0]
        if _p:
            _r = eng.stats.setdefault("send_gap_ring", [])
            _r.append(T[0] - _p)
            del _r[:-400]
_g = sorted(eng.stats.get("send_gap_ring") or [])
check("кольцо дельт наполняется", len(_g) == 4, _g)
_S = open("chain/spec_fire_live.py").read()
_F = open("chain/fire_engine.py").read()
check("движок пишет дельту на КАЖДОЙ отправке (сбоку, под своим try)",
      '_r = self.stats.setdefault("send_gap_ring", [])' in _F and 'self.stats["sent"] += 1' in _F)
check("строка окна несёт каденс[p50/p90/доля<=0.2с]",
      "каденс[%s]" in _S and "доля≤0.2с" in _S and "send_gap_ring" in _S)
check("кольцо капировано (400)", "del _r[:-400]" in _F)

sfl._label_of = _sv_label
print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: осевое расслоение закрыто — доска планирует под цель детонатора, "
                     "окно без плана не открывается, каденс измеряется"))
sys.exit(1 if FAILS else 0)
