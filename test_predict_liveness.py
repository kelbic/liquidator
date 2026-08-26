import sys, time
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.hotpath as hp, chain.predict as pr
F = []
def check(n, ok, x=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", n, "" if ok else " — %s" % str(x)[:160]))
    if not ok: F.append(n)
# метка ставится на событии движка
pr._LAST_EVENT_TS[0] = 0.0
check("до событий last_event_ts=None", pr.last_event_ts() is None)
pr._note_event_ts()
check("после события метка свежая", (time.time() - pr.last_event_ts()) < 2)
# логика сторожа (та же арифметика, что в петле)
def verdict(pushes, silence_s, born_ago):
    return pushes >= hp.PREDICT_SILENCE_PUSHES and silence_s >= hp.PREDICT_SILENCE_SEC
check("боевой случай 23-26.08 (528 пушей, 2.5 суток тишины) -> ТРЕВОГА",
      verdict(528, 216000, 216000))
check("штиль с живым предиктом (20 пушей, тишина 60с) -> молчим", not verdict(20, 60, 3600))
check("тишина есть, но пушей мало (поле мертво) -> молчим", not verdict(3, 3600, 3600))
_H = open("chain/hotpath.py").read()
check("счётчик пушей инкрементится на feed-spawn", '_PRED_WATCH["pushes"] += 1' in _H)
check("вердикт печатается ERROR с подсказкой py-spy", "PREDICT МЁРТВ" in _H and "py-spy" in _H)
check("порог из наблюдённого, вынесен в env",
      'PREDICT_SILENCE_PUSHES", "20"' in _H and 'PREDICT_SILENCE_SEC", "1800"' in _H)
print("\n%s" % ("FAIL: %d" % len(F) if F else "OK: сторож живости predict — молчание больше не «всё хорошо»"))
sys.exit(1 if F else 0)
