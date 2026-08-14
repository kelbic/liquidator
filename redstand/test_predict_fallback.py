"""Предиктор не слепнет на backoff: сперва запасной узел (#82).

Замер на боевом ключе (сутки 08-09.08): отказов основного всего ~1% (226 на 21600 опросов), но
POLL_BACKOFF_AFTER=3 + база 5с + кап 30с превращают их в 1725с ≈ 29 МИНУТ полной слепоты в
сутки (127 событий backoff, среднее 13.6с = до 7 пропущенных опросов подряд). 429-шторм на
bg-ключе повторился сразу после рестарта 10.08 — семь отказов подряд.

Отступать на 429 правильно. Отступать В НИКУДА — нет: замер публичного узла тем же клиентом,
что и бой, дал 26% отказов, то есть 74% успеха. Это несравнимо лучше 100% немоты, и слепнет при
этом ровно тот слой, на котором мы вообще можем выигрывать — гонку берут предвидением.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.predict as P                                    # noqa: E402


class Clk:
    t = 1000.0


def mk(poll_fn, fallback=None):
    e = P.PredictEngine()
    d = P.PredictDriver(e, mid_fn=lambda s: None, poll_fn=poll_fn,
                        fallback_poll_fn=fallback, log=lambda m: d_logs.append(m))
    d._now = lambda: Clk.t
    return d


d_logs = []


def boom():
    raise RuntimeError("429 over rate limit")


READS = {"XRPUSDT": (1786400000, 0.5)}

# --- 1. Основной отказал, запасной ответил -> слепоты НЕТ ---------------------------------
d_logs.clear()
d = mk(boom, fallback=lambda: dict(READS))
d._poll(Clk.t)
assert d._poll_backoff_until == 0.0, "ушли в backoff при живом запасном — 29 минут слепоты назад"
assert d._poll_fail_streak == 0, "серия отказов не сброшена — следующий отказ уйдёт в backoff раньше"
assert d.stats.get("poll_fallback_ok") == 1, "успех запасного не посчитан"
assert d.stats.get("poll_fail", 0) == 0, "отказ основного записан как отказ ПОЛЛА, хотя данные есть"
assert any("ЗАПАСНОЙ" in m for m in d_logs), "подмена узла молчит в журнале"
print("OK 1/4: основной отказал — запасной ответил, backoff не взведён, слепоты нет")

# --- 2. КОНТРОЛЬ «до фикса»: запасного нет -> backoff и слепота --------------------------
d2 = mk(boom, fallback=None)
for _ in range(P.POLL_BACKOFF_AFTER):
    d2._poll(Clk.t)
assert d2._poll_backoff_until > Clk.t, "без запасного backoff обязан взводиться (прежнее поведение)"
assert d2.stats["poll_fail"] == P.POLL_BACKOFF_AFTER
print("OK 2/4: КОНТРОЛЬ — без запасного узла всё как раньше: backoff на %.0fс" % (
    d2._poll_backoff_until - Clk.t))

# --- 3. Оба отказали -> честный backoff, и это ВИДНО ------------------------------------
d_logs.clear()
d3 = mk(boom, fallback=boom)
for _ in range(P.POLL_BACKOFF_AFTER):
    d3._poll(Clk.t)
assert d3._poll_backoff_until > Clk.t, "оба узла легли, а backoff не взведён — будем долбить впустую"
assert d3.stats.get("poll_fallback_fail") == P.POLL_BACKOFF_AFTER
assert any("запасной узел тоже отказал" in m for m in d_logs), "падение запасного молчит"
print("OK 3/4: оба легли — backoff честный, отказ запасного назван в журнале")

# --- 4. Обработка ОДНА на оба пути (иначе дубль разойдётся при первой правке) ------------
src = pathlib.Path("chain/predict.py").read_text()
assert src.count("def _apply_reads(") == 1, "обработка показаний размножена"
_p = src.index("def _poll(self, wall")
_blk = src[_p:src.index("def _publish", _p)]
# считаем именно ВЫЗОВЫ (self._apply_reads), а не определение — первая редакция гарда
# ловила и def, и падала на здоровом коде
assert _blk.count("self._apply_reads(") == 2, \
    "запасной путь не кормит ту же обработку — дубль разойдётся при первой правке"
# и структурно: фолбэк ПРОБУЕТСЯ ДО того, как взводится backoff
assert _blk.index("fallback_poll_fn") < _blk.index("_poll_backoff_until = self._now()"), \
    "запасной узел пробуется ПОСЛЕ ухода в backoff — то есть уже вслепую"
print("OK 4/4: обработка одна на оба пути, запасной пробуется ДО backoff")
print("=== #82 ЗАКРЫТ ===")
