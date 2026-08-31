"""Сторож квоты CU: маркер вывода из эксплуатации гасит СУБЪЕКТ, а не тревогу (31.08).

Приёмка обеих сторон, как в threads-watch: с маркером — молчит и НЕ звонит, без маркера —
звонит по-прежнему (сторож перенаправлен, а не убит). Плюс негативный контроль к детектору
слепоты: час из одних бут-окон — это ПРОЧИТАННЫЙ журнал, а не слепота.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis import cu_quota_watch as W  # noqa: E402

MARKER = "RETIRED-liquidator-bot.md"
# Реальная строка бота с бутовым топом и молодым скольз-спаном (обе координаты бута).
BOOT_LINE = ("Aug 31 05:24:04 h python[42]: 2026-08-31 05:24:04,574 INFO CU "
             "base-mainnet.g.alchemy.com#3133: 1036 CU/окно = 9 CU/s (проекция 22.3M/мес, "
             "скольз.5мин 42.9M/мес) | eth_call@execute.py:volley_bal_warm×26")


def _run(tmp, text, marker=False, state=None):
    hist = os.path.join(tmp, "cu-quota.history.jsonl")
    if state is not None:
        with open(os.path.join(tmp, "cu-quota.state"), "w") as f:
            json.dump(state, f)
    if marker:
        with open(os.path.join(tmp, MARKER), "w") as f:
            f.write("выведен из эксплуатации\n")
    calls, lines = [], []
    rc = W.run(fetch=lambda: text, notify_fn=lambda *a, **k: calls.append((a, k)),
               out=lines.append, history_path=hist)
    with open(os.path.join(tmp, "cu-quota.state")) as f:
        st = json.load(f)
    return rc, calls, lines, st


def main():
    ok = 0
    # 1. С МАРКЕРОМ: не звоним, rc=0, серия слепых прогонов обнулена, строка ОДНА на переход.
    with tempfile.TemporaryDirectory() as tmp:
        rc, calls, lines, st = _run(tmp, "", marker=True, state={"fails": 3})
        assert rc == 0, rc
        assert calls == [], calls
        assert st["fails"] == 0, st
        assert st.get("retired") is True, st
        assert sum("retired=1" in ln for ln in lines) == 1, lines
        ok += 1
        # второй прогон подряд — уже молча (лог не вымывается), но по-прежнему rc=0
        rc2, calls2, lines2, _ = _run(tmp, "", marker=True)
        assert rc2 == 0 and calls2 == [] and lines2 == [], (rc2, calls2, lines2)
        ok += 1
        # маркер снят — наблюдение возвращается и звонок вместе с ним
        os.remove(os.path.join(tmp, MARKER))
        rc3, calls3, lines3, st3 = _run(tmp, "")
        assert rc3 == 1, rc3
        assert st3["fails"] == 1 and "retired" not in st3, st3
        assert any("retired=0" in ln for ln in lines3), lines3
        ok += 1

    # 2. БЕЗ МАРКЕРА молчащий бот по-прежнему копит серию и звонит на третьем прогоне.
    with tempfile.TemporaryDirectory() as tmp:
        rc, calls, _, st = _run(tmp, "", state={"fails": 2})
        assert rc == 1 and st["fails"] == 3, (rc, st)
        assert len(calls) == 1 and calls[0][1]["key"] == "cu-quota:blind", calls
        ok += 1

    # 3. НЕГАТИВНЫЙ КОНТРОЛЬ детектора слепоты: журнал из одних БУТОВЫХ окон прочитан ⇒
    #    слепотой не является (до правки len(projs)==0 давало ложную тревогу).
    with tempfile.TemporaryDirectory() as tmp:
        text = "\n".join([BOOT_LINE] * 5)
        per = W.parse_cu_windows(text)
        assert per["base-mainnet.g.alchemy.com#3133"]["boot"] == 5, per
        assert per["base-mainnet.g.alchemy.com#3133"]["projs"] == [], per
        rc, calls, _, st = _run(tmp, text, state={"fails": 2})
        assert calls == [], calls
        assert st["fails"] == 0, st
        ok += 1

    print("test_cu_quota_retired: %d/5 OK" % ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
