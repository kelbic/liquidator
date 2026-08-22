"""ДЕТОНАТОР-ЧУВСТВИТЕЛЬНОСТЬ (пост-мортем $128k 22.08): per-target flip-dist правит порог
огня окна и арм-порог predict. Приёмка по спеке, красным-на-базе:
(а) реплей детонатора (hf у флипа, движение 0.13%) — старый порог 0.5% молчит, новый
    открывает огонь ДО флипа; (б) штиль на далёких целях (hf=1.05) окон не рождает;
(в) пол шума держит hf=1.000001; (г) кап окон близости в минуту; (д) откат одним env;
(е) predict: det-арм + гистерезис от эффективного порога + TTL.
"""
import sys
import pathlib as _pl
from types import SimpleNamespace

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import threading  # noqa: E402
import chain.spec_fire_live as sfl  # noqa: E402
import chain.predict as pr  # noqa: E402

_sv_label = sfl._label_of
sfl._label_of = lambda s: s                              # фикстура: токен = метка


def mk(hf_map):
    o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    o.stats = {}
    o._log_lines = []
    o._log = o._log_lines.append
    o._now = lambda: 1000.0
    m = SimpleNamespace(collateral_token="XRP", loan_token="USDC", bonus=0.1268)
    o.shared = SimpleNamespace(lock=threading.Lock(), groups={"0xmid": list(hf_map)},
                               debt_by={("0xmid", b): 128492.0 for b in hf_map},
                               by_id={"0xmid": m},
                               hf_by={("0xmid", b): h for b, h in hf_map.items()})
    return o


UNI = 0.005                                              # универсальный порог XRP (dev 0.5%)

# ---- (а) реплей $128k: детонатор у флипа -> огонь при 0.13%, старый код молчит ----
o = mk({"0xdet": 1.0013})                                # до транзита цель чуть НАД водой
d = o._min_flip_dist("XRP", "down")
check("flip_dist детонатора ~0.13% (1-1/hf)", d is not None and 0.0012 < d < 0.0014, d)
thr, dd = o._det_thr("XRP", "down", UNI)
check("порог огня связан детонатором: %.3f%% вместо 0.5%%" % (thr * 100),
      abs(thr - d) < 1e-9, (thr, d))
RET = 0.0013                                             # движение эпизода
check("РЕПЛЕЙ: старый порог молчит (0.13% < 0.5%), новый стреляет ДО флипа",
      RET < UNI and RET >= thr)
o2 = mk({"0xdet": 0.9987})                               # уже за флипом (кейс из лога)
thr2, _ = o2._det_thr("XRP", "down", UNI)
check("цель за флипом: dist=0 -> порог падает на ПОЛ ШУМА (0.05%), не в ноль",
      abs(thr2 - sfl.SPEC_FIRE_DET_NOISE_FLOOR) < 1e-12, thr2)

# ---- (б) негативный контроль: далёкие цели окон близости не рождают ----
o3 = mk({"0xfar": 1.05})
thr3, d3 = o3._det_thr("XRP", "down", UNI)
check("штиль: hf=1.05 (dist 4.8%) -> порог УНИВЕРСАЛЬНЫЙ", thr3 == UNI and d3 > UNI, (thr3, d3))
o4 = mk({})
o4.shared.hf_by = {}
check("HF ни у кого не вычислим -> fail-open на универсальный",
      o4._det_thr("XRP", "down", UNI) == (UNI, None))

# ---- (в) пол шума: hf=1.000001 не открывает окно каждым тиком ----
o5 = mk({"0xmicro": 1.000001})
thr5, _ = o5._det_thr("XRP", "down", UNI)
check("пол шума: порог = 0.05%, не 0.0001%",
      abs(thr5 - sfl.SPEC_FIRE_DET_NOISE_FLOOR) < 1e-12, thr5)

# ---- (г) кап окон близости: ПЕР-ФИД (не глобальный — каскад разных фидов не гасится) ----
o6 = mk({"0xdet": 1.0013})
o6.stats["_det_win_ts::XRP"] = [999.0] * sfl.SPEC_FIRE_DET_WIN_CAP_MIN
thr6, _ = o6._det_thr("XRP", "down", UNI)
check("кап: %d det-окон/мин у ФИДА -> универсальный + счётчик" % sfl.SPEC_FIRE_DET_WIN_CAP_MIN,
      thr6 == UNI and o6.stats.get("det_win_capped") == 1, (thr6, o6.stats))
o6._det_thr("XRP", "down", UNI)
check("счётчик капа считает ПЕРЕХОДЫ, не 5с-рефреши (второй вызов не инкрементит)",
      o6.stats.get("det_win_capped") == 1, o6.stats)
o6.shared.by_id["0xmid"].collateral_token = "BTC"        # тот же стенд, другой фид
thr6b, _ = o6._det_thr("BTC", "down", UNI)
check("кап XRP НЕ душит соседний фид каскада (BTC связывается своим детонатором)",
      thr6b < UNI, thr6b)
o6.shared.by_id["0xmid"].collateral_token = "XRP"

# ---- (д) откат одним env ----
_sv = sfl.SPEC_FIRE_DET_SENSE
sfl.SPEC_FIRE_DET_SENSE = False
check("SPEC_FIRE_DET_SENSE=0: порог бит-в-бит универсальный",
      o._det_thr("XRP", "down", UNI) == (UNI, None))
sfl.SPEC_FIRE_DET_SENSE = _sv

# ---- (е) predict: det-арм, гистерезис от эффективного порога, TTL ----
pr._DET_DIST.clear()
pr.note_det_dist("XRP", 0.0013, now=1000.0)
eff = pr._det_arm_pct("XRP", 0.0045, now=1001.0)
check("predict: арм-порог связан детонатором (0.9x0.13%=0.117%)",
      abs(eff - 0.0013 * 0.9) < 1e-9, eff)
check("predict: TTL гасит протухший сигнал (универсальный арм)",
      pr._det_arm_pct("XRP", 0.0045, now=1000.0 + pr._DET_TTL + 1) == 0.0045)
pr.note_det_dist("XRP", 0.0000001, now=1000.0)
check("predict: пол шума и здесь", pr._det_arm_pct("XRP", 0.0045, now=1001.0) == pr._DET_FLOOR)
# живой арм-цикл: тик 0.13% армит det-фид и гистерезис не гасит его тем же тиком
pr._DET_DIST.clear()
pr.note_det_dist("XRP", 0.0013)
P = pr.PredictEngine()
P.bootstrap("XRPUSDT", 1.0)
ev = P.on_mid("XRPUSDT", 1.0 - 0.0013)
check("живой predict: тик 0.13% АРМИТ (универсальный 0.45% молчал бы)",
      any(e.get("event") == "arm" for e in ev), ev)
f = P.feeds["XRPUSDT"]
check("гистерезис det-арма ниже эффективного порога (не гаснет тем же тиком)",
      getattr(f, "eff_disarm", None) is not None and f.eff_disarm < 0.0013 * 0.9,
      getattr(f, "eff_disarm", None))
ev2 = P.on_mid("XRPUSDT", 1.0 - 0.0002)                  # ретрейс ниже гистерезиса
check("ретрейс ниже det-гистерезиса -> disarm", any(e.get("event") == "disarm" for e in ev2),
      ev2)
# линза (3): eff_disarm СБРАСЫВАЕТСЯ на следующем УНИВЕРСАЛЬНОМ арме (не липнет det-значение)
pr._DET_DIST.clear()
P.bootstrap("XRPUSDT", 1.0)
ev3 = P.on_mid("XRPUSDT", 1.0 - 0.0046)                  # универсальный арм 0.45%
check("универсальный арм после det: eff_disarm вернулся к disarm_pct (окно-зомби исключено)",
      any(e.get("event") == "arm" for e in ev3)
      and abs(f.eff_disarm - f.disarm_pct) < 1e-12, (getattr(f, "eff_disarm", None),
                                                     f.disarm_pct))

# ---- (ж) ИСПОЛНЯЕМЫЙ конец-в-конец: shared(hf) -> пушер -> predict -> арм ----
pr._DET_DIST.clear()
o7 = mk({"0xdet": 1.0013})
o7.dev_by = {"XRP": 0.005}
n_push = o7._det_push_once()
check("пушер: один проход кладёт XRP в predict (независимо от движка)",
      n_push == 1 and "XRP" in pr._DET_DIST
      and abs(pr._DET_DIST["XRP"][0] - (1.0 - 1.0 / 1.0013)) < 1e-9,
      (n_push, pr._DET_DIST.get("XRP")))
check("и арм-порог из ЭТОГО пуша связан: 0.9x0.13%",
      abs(pr._det_arm_pct("XRP", 0.0045) - (1.0 - 1.0 / 1.0013) * 0.9) < 1e-9)
pr._DET_DIST.clear()

# ---- гарды дрейфа ----
_S = open("chain/spec_fire_live.py").read()
_P = open("chain/predict.py").read()
check("порог огня окна ходит через _det_thr + 5с-рефреш",
      "self._det_thr(feed, direction, _thr_uni)" in _S and "_det_next = self._now() + 5.0" in _S)
check("строка ДЕТОНАТОР при связанном пороге + счётчик det_windows",
      "SPECFIRE ДЕТОНАТОР" in _S and "det_windows" in _S)
check("пушер стартует в __init__ (с первой секунды, не из ленивого движка)",
      "target=self._det_push_loop" in _S and "_pr.note_det_dist" in _S)
check("петля пушера на НАСТОЯЩЕМ time.sleep (фейк self._sleep дал бы busy-loop)",
      "time.sleep(30.0)" in _S)
check("predict: арм через _det_arm_pct, гистерезис от eff_disarm",
      "_det_arm_pct(f.label, f.arm_pct)" in _P and 'getattr(f, "eff_disarm"' in _P)
check("откат ОДНИМ env в обоих слоях", _S.count('SPEC_FIRE_DET_SENSE", "1"') == 1
      and _P.count('SPEC_FIRE_DET_SENSE", "1"') == 1)

sfl._label_of = _sv_label
print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: детонатор виден обоим слоям — окно и арм считают войну от близости цели"))
sys.exit(1 if FAILS else 0)
