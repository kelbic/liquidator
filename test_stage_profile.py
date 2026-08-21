"""П.7 (kelbic 21.08): пер-стадийный профиль задержки до первой пробы на классе.

Главная цифра проекта — медиана delta ≈ +140 позиций НА ВЕРНОМ КЛАССЕ. Это не сеть (RTT
30мс) и не класс (tip-match есть): 200-500мс внутри конвейера. Резать вслепую запрещено —
прибор обязан НАЗВАТЬ стадию-вкладчика.

ЧЕСТНЫЙ ПРЕДЕЛ СТЕНДА (назван, не спрятан): «реплей утреннего эпизода» невозможен буквально
— меток тогда не существовало, сохранять было нечего. Поэтому реплей здесь СИНТЕТИЧЕСКИЙ:
метки расставлены по ЗАМЕРЕННЫМ величинам эпизода (кадр→детект ~40мс, детект→handoff ~15мс,
подпись за 90с до кадра, handoff→pacer 260мс — кандидат, pacer→ack 30мс = замеренный RTT).
Стенд проверяет, что прибор ПРАВИЛЬНО НАЗЫВАЕТ вкладчика на известной раскладке, а не что
раскладка именно такова.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.stage_probe as sp  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


def _reset():
    with sp._LOCK:
        sp._MARKS.clear(); sp._ORDER.clear(); sp._FEED.clear(); sp._BLOCK.clear()


# ---- 1. Синтетический реплей эпизода: вкладчик = handoff→pacer ----
_reset()
H, BN, FEED = "0xprobe1", 50262420, "BTC"
T = 1_000_000.0
sp.stamp(H, "sign", T - 90.0)          # лестница подписана за 90с до кадра
sp.mark_frame(BN, T)                   # кадр принят
sp.mark_feed(FEED, "detect", T + 0.040)
sp.mark_feed(FEED, "handoff", T + 0.055)
sp.stamp(H, "pacer", T + 0.315)        # +260мс — кандидат-вкладчик
sp.stamp(H, "ack", T + 0.345)          # +30мс RTT (замер)
prof = sp.profile(H, feed=FEED, bn=BN)
check("профиль собран из трёх источников (хэш+фид+блок)",
      set(prof) == {"sign", "frame", "detect", "handoff", "pacer", "ack"}, sorted(prof))
line, (worst, ms) = sp.format_profile(prof)
check("вкладчик назван верно: handoff→pacer", worst == "handoff→pacer", (worst, ms))
check("вклад посчитан верно (~260мс)", 255 <= ms <= 265, ms)
check("строка несёт все переходы и сумму", "frame→detect" in line and "всего" in line, line)
check("подпись — НЕ звено цепи, а возраст лестницы отдельным полем",
      "возраст_лестницы 90с" in line and "sign→" not in line, line)

# ---- 2. Другая раскладка -> другой вкладчик (прибор не приколочен) ----
_reset()
sp.mark_frame(BN, T)
sp.mark_feed(FEED, "detect", T + 0.400)      # медленный детект
sp.mark_feed(FEED, "handoff", T + 0.410)
sp.stamp(H, "pacer", T + 0.420)
sp.stamp(H, "ack", T + 0.450)
_, (worst2, ms2) = sp.format_profile(sp.profile(H, feed=FEED, bn=BN))
check("вкладчик переехал на frame→detect", worst2 == "frame→detect" and 395 <= ms2 <= 405,
      (worst2, ms2))

# ---- 3. Отказоустойчивость и границы ----
_reset()
check("пустой профиль -> «—»", sp.format_profile({})[0] == "—")
check("одна метка -> без вкладчика", sp.format_profile({"frame": T})[1] == (None, None))
sp.stamp(None, "sign"); sp.mark_feed(None, "detect"); sp.mark_frame("мусор")
check("мусорные метки не бросают", True)
for i in range(sp._CAP + 50):
    sp.stamp("0x%d" % i, "sign")
with sp._LOCK:
    n = len(sp._MARKS)
check("кольцо капировано (~CAP)", n <= sp._CAP + 1, n)

# ---- 4. Ручка отката ----
_reset()
sp.STAGE_ON = False
sp.stamp("0xoff", "sign"); sp.mark_frame(1); sp.mark_feed("F", "detect")
check("STAGE_PROBE=0: метки не пишутся", sp.profile("0xoff", feed="F", bn=1) == {})
sp.STAGE_ON = True

# ---- 5. Гарды дрейфа: метки стоят в шести боевых точках ----
_SRC_E = open("chain/execute.py").read()
_SRC_H = open("chain/hotpath.py").read()
_SRC_S = open("chain/spec_fire_live.py").read()
check("sign — в цикле подписи лестницы", '_stage_stamp(_hh, "sign")' in _SRC_E)
check("pacer — сразу после выдачи токена", '_stage_stamp(_probe_hash_hint(), "pacer")' in _SRC_E)
check("ack — после приёма секвенсером", '_stage_stamp(_probe_hash_hint(), "ack")' in _SRC_E)
check("хэш пробы кладётся при pop из лестницы", "_probe_hash_set(h)" in _SRC_E)
check("frame — в recv-петле у offer_block", "_stage_frame(bn)" in _SRC_H)
check("detect — на feed-spawn", '_stage_feed(_sym, "detect")' in _SRC_H)
check("handoff — в note_ws_transmit", '_mf(feed, "handoff")' in _SRC_S)
check("читатель печатает строку СТАДИИ рядом с ПЕРЕСЕЧЕНИЕМ",
      "SPECFIRE СТАДИИ blk=" in _SRC_S and "топ-вкладчик" in _SRC_S)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: профиль стадий называет вкладчика; прибор сбоку, кольцо капировано"))
sys.exit(1 if FAILS else 0)
