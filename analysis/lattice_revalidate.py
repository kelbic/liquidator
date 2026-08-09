"""РЕВАЛИДАЦИЯ РЕШЁТКИ OCR НА ИСТОРИИ (#59, хвост #49; разведка READ-ONLY, ключей нет).

ЗАЧЕМ. 09.08 метки фазового гейта переведены с времени ПОЛЛА на `updatedAt` ОРАКУЛА
(chain/spec_fire_live.py::note_push, счётчики lattice_oracle_ts / lattice_skew_bad). До фикса
полл ходил раз в PREDICT_POLL_SEC=4с при толерансе фазы SPEC_FIRE_LATTICE_TOL=2с — метки были
квантованы ГРУБЕЕ самого гейта, а живой валидатор hits/misses мерил теми же смещёнными часами
и выглядел здоровым (самореферентная валидация, зелёная по построению). Поэтому решётку надо
перемерить по ЧУЖИМ часам.

НЕЗАВИСИМЫЕ ЧАСЫ. Обе шкалы берутся ИЗ ЦЕПИ и к нашему буферу отношения не имеют:
  * `updatedAt` из данных лога AnswerUpdated — часы самого оракула (то, что теперь штампует
    note_push);
  * `block.timestamp` блока, где лог сел — часы секвенсера.
Скрипт считает обе и печатает их расхождение: если решётка видна только на одной из них, это
свойство часов, а не рынка.

ЧТО ОТВЕЧАЕТ:
  (1) доля пушей, попадающих на узлы решётки P=30с в пределах ±2с — против боевого порога
      SPEC_FIRE_LATTICE_MIN_FRAC=0.80. Считается ДВАЖДЫ: статически по всем гэпам в горизонте
      (большой n) и в боевой форме — НАСТОЯЩЕЙ функцией `_lattice_frac` на скользящем буфере
      64 меток, как в бою;
  (2) сколько прошлых срабатываний гейта были АРТЕФАКТОМ СКОСА: реплей одной и той же истории
      двумя режимами меток — «до фикса» (метка = момент детекта поллом) и «после фикса»
      (метка = updatedAt). Артефакт = пуш, в чью секунду гейт «до фикса» был ЗАКРЫТ, а по
      оракульным часам ОТКРЫТ, то есть огонь глушился в истинную секунду транзита.

БОЕВОЙ КОД ИСПОЛНЯЕТСЯ НАСТОЯЩИЙ. Реплей зовёт `SpecFireLive.note_push`, `_lattice_frac`,
`_lattice_ready`, `_gate_open` из chain/spec_fire_live.py — вместе с дедупом 5с, капом буфера
64 и всеми fail-open гардами. Единственный шов — ЧАСЫ (`_now` инстанса и `sfl.time` на время
реплея): истории 9 суток назад иначе не проиграть.

МОДЕЛЬ ПОЛЛА (режим «до фикса»). chain/predict.py: драйвер тикает `interval` и опрашивает
агрегаторы, когда с прошлого полла прошло >= `poll_interval` (main.py: 4.0с), поэтому каденс
не сетка, а процесс обновления с шагом 4.0с + квант петли. Пуш становится виден узлу через
prop после блока; замечает его ПЕРВЫЙ полл после этого. Если между двумя поллами село два
апдейта, полл видит только ПОСЛЕДНИЙ (`updated_at != _last_updated`) — ранний не порождает
события вовсе. Эта потеря общая для обоих режимов (менялось только ЗНАЧЕНИЕ метки), поэтому
реплей теряет их в обоих. Фаза полла неизвестна -> Монте-Карло по seed'ам, печатается разброс.

ПОЗИТИВНЫЕ КОНТРОЛИ (канон: «нет сигнала ⇒ нет явления» — только после контроля НА ТОМ ЖЕ
объекте). До любых выводов:
  PC1 свежайший AnswerUpdated == latestRoundData() того же агрегатора (RPC/сигнатура/декод);
  PC2 карта block->ts валидирована на случайных блоках, и у >99% логов `updatedAt` ненулевой
      (иначе «оракульные часы» — это наша выдумка);
  PC3 реплей УМЕЕТ отличать: синтетическая идеальная решётка 30с даёт frac=1.0 и гейт открыт
      в секунду пуша, а равномерно-случайная серия даёт frac ниже порога и гейт не армится;
  PC4 модель полла УМЕЕТ портить: та же идеальная решётка, пропущенная через квант 4с, теряет
      подтверждение (frac падает) и даёт ненулевой счётчик артефактов.
Провал любого -> Fail, выводы не печатаются.

ЗАПУСК (только чтения публичного узла; кэш логов вне репозитория):
    python -m analysis.lattice_revalidate --days 9
    python -m analysis.lattice_revalidate --days 14 --seeds 8 --out out/lattice_revalidate.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.push_hazard import (                      # noqa: E402  (транспорт разведки)
    PROXIES, BlockClock, Fail, Rpc, SEL_LATEST_ROUND,
    fetch_pushes, log, quantiles, resolve_aggregators, _s256,
)
import chain.spec_fire_live as sfl                      # noqa: E402  (БОЕВОЙ гейт)

# --- боевые константы гейта берём ИЗ БОЕВОГО МОДУЛЯ, а не переписываем ------------------
P_SEC = sfl.SPEC_FIRE_LATTICE_P                 # 30с
TOL_SEC = sfl.SPEC_FIRE_LATTICE_TOL             # ±2с
MAX_AGE = sfl.SPEC_FIRE_LATTICE_MAX_AGE         # 1800с
MIN_N = sfl.SPEC_FIRE_LATTICE_MIN_N             # 12 гэпов
MIN_FRAC = sfl.SPEC_FIRE_LATTICE_MIN_FRAC       # 0.80
# --- модель полла (chain/predict.py + main.py) -----------------------------------------
POLL_SEC = float(os.environ.get("PREDICT_POLL_SEC", "4.0"))
STEP_QUANTUM = 0.5                              # квант петли драйвера (interval)
PROP_SEC = 0.3                                  # блок сел -> виден узлу (RTT + распространение)


# ---------------------------------------------------------------- шов часов
class _FrozenTime:
    """Шов ТОЛЬКО на стенных часах: боевой note_push берёт wall-время из sfl.time.time(),
    чтобы посчитать возраст апдейта. Реплей истории иначе невозможен. Всё остальное из
    модуля time проксируется как есть."""

    def __init__(self, real):
        self._real = real
        self.now = 0.0

    def time(self):
        return self.now

    def __getattr__(self, k):
        return getattr(self._real, k)


class Replay:
    """Боевой SpecFireLive с управляемыми часами. Ничего не подменяем, кроме времени:
    _push_ts/stats — настоящие поля, note_push/_lattice_frac/_gate_open — настоящий код.

    force_ready=True — КОНТРФАКТ: подтверждение решётки считается выданным, чтобы отделить
    чистый эффект СКОСА ЧАСОВ от эффекта «гейт вообще не армился». Тело _gate_open (вся
    фазовая арифметика) при этом исполняется настоящее."""

    def __init__(self, force_ready=False):
        sf = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
        self.t = 0.0
        sf._now = lambda: self.t
        sf._log = lambda m: None
        sf._push_ts = {}
        sf.stats = {}
        if force_ready:
            sf._lattice_ready = lambda feed: True
        self.sf = sf

    def at(self, t):
        self.t = float(t)
        return self.sf


# ---------------------------------------------------------------- статика решётки
def on_lattice(gap, period=P_SEC, tol=TOL_SEC):
    ph = gap % period
    return min(ph, period - ph) <= tol


def null_chance(period=P_SEC, tol=TOL_SEC, tick=2.0):
    """База случайности. Считается по ТИКУ ЧАСОВ, а не по наблюдённым остаткам: опора
    остатков — это ровно то, что мы измеряем, и брать её за нулевую гипотезу значит доказать
    решётку самой решёткой. На Base блок ровно 2с и updatedAt == block.timestamp, поэтому все
    гэпы ЧЁТНЫЕ: равномерность даёт не (2*tol+1)/P, а долю чётных остатков в допуске
    (P=30, tol=2 -> попадают {0,2,28} из 15 -> 0.200)."""
    grid = [i * tick for i in range(int(round(period / tick)))]
    if not grid:
        return min(1.0, (2 * tol + 1) / period)
    ok = sum(1 for r in grid if min(r, period - r) <= tol)
    return ok / float(len(grid))


def static_frac(marks, period=P_SEC, tol=TOL_SEC, max_age=MAX_AGE, tick=2.0):
    """Доля гэпов на решётке по ВСЕЙ серии (тот же критерий, что у боевого _lattice_frac,
    но без капа буфера 64 — нужен большой n для ответа на вопрос (1))."""
    gaps = [b - a for a, b in zip(marks, marks[1:]) if 0 < b - a <= max_age]
    if not gaps:
        return {"n": 0, "frac": None, "chance": None, "lift": None}
    hit = sum(1 for g in gaps if on_lattice(g, period, tol))
    chance = null_chance(period, tol, tick)
    frac = hit / float(len(gaps))
    return {"n": len(gaps), "frac": frac, "chance": chance,
            "lift": (frac / chance) if chance else None}


GAP_BUCKETS = ((0, 60), (60, 120), (120, 240), (240, 480), (480, 900),
               (900, 1300), (1300, 1800))


def frac_by_gap(all_gaps, period=P_SEC, tol=TOL_SEC):
    """Решётка по РАССТОЯНИЮ до предыдущего пуша. Главный вопрос гейта: не «держится ли
    решётка в среднем», а «держится ли она там, где пуши идут ЧАСТО» — то есть в каскаде, где
    и живут деньги. Если короткие гэпы держат решётку лучше длинных, гейт острее всего ровно
    в горячей фазе; если наоборот — гейт слепнет там, где приз."""
    out = []
    for lo, hi in GAP_BUCKETS:
        part = [g for g in all_gaps if lo < g <= hi]
        if not part:
            continue
        hit = sum(1 for g in part if on_lattice(g, period, tol))
        out.append({"lo": lo, "hi": hi, "n": len(part), "frac": hit / float(len(part))})
    return out


def residues(all_gaps, period=P_SEC):
    """Гистограмма остатков gap mod P: показывает, СИММЕТРИЧЕН ли допуск. Пуш, который
    систематически опаздывает к узлу, требует несимметричного окна, а не ±TOL."""
    h = {}
    for g in all_gaps:
        r = int(round(g % period))
        h[r] = h.get(r, 0) + 1
    return dict(sorted(h.items()))


def frac_by_day(marks, period=P_SEC, tol=TOL_SEC, max_age=MAX_AGE):
    """Устойчивость во времени: средняя доля за 14 суток ничего не скажет, если в день
    стресса решётка рассыпается — а именно такой день и есть день денег."""
    acc = {}
    for a, b in zip(marks, marks[1:]):
        g = b - a
        if not (0 < g <= max_age):
            continue
        day = time.strftime("%m-%d", time.gmtime(b))
        n, hit = acc.get(day, (0, 0))
        acc[day] = (n + 1, hit + int(on_lattice(g, period, tol)))
    return {d: {"n": n, "frac": h / float(n)} for d, (n, h) in sorted(acc.items())}


def frac_by_prize(rows, period=P_SEC, tol=TOL_SEC, max_age=MAX_AGE, tick=2.0, q=4):
    """ОБЪЕКТ В ЗНАМЕНАТЕЛЕ (урок 06.08): решётка «на пуш» ничего не говорит о решётке «на
    приз». Гэпы ранжируются по МОДУЛЮ ценового шага пуша, который их закрыл, и доля меряется
    по квартилям: если в верхнем квартиле решётка рассыпается, гейт слепнет ровно там, где
    деньги."""
    pairs = []
    for a, b in zip(rows, rows[1:]):
        g = b["ts_o"] - a["ts_o"]
        if not (0 < g <= max_age) or not a["answer"]:
            continue
        pairs.append((abs(b["answer"] - a["answer"]) / float(abs(a["answer"])), g))
    if len(pairs) < q * 4:
        return None
    pairs.sort(key=lambda x: x[0])
    n, out = len(pairs), []
    for i in range(q):
        part = pairs[i * n // q:(i + 1) * n // q]
        hit = sum(1 for _, g in part if on_lattice(g, period, tol))
        out.append({"n": len(part), "frac": hit / float(len(part)),
                    "move_lo": part[0][0], "move_hi": part[-1][0]})
    return out


# ---------------------------------------------------------------- модель полла
def poll_detects(push_ts, seed=0, poll_sec=POLL_SEC, quantum=STEP_QUANTUM, prop=PROP_SEC):
    """Времена ДЕТЕКТА: первый полл после того, как апдейт стал виден узлу. Возвращает
    список (t_push, t_detect, seen), seen=False — апдейт перекрыт следующим внутри одного
    полла и события не породил (боевой путь: `updated_at != _last_updated` увидит только
    последнее значение)."""
    if not push_ts:
        return []
    rng = random.Random(seed)
    t = push_ts[0] - rng.random() * poll_sec        # неизвестная фаза полла
    ticks = []
    end = push_ts[-1] + poll_sec + quantum + 5.0
    while t < end:
        ticks.append(t)
        t += poll_sec + rng.random() * quantum
    out = []
    k = 0
    for p in push_ts:
        vis = p + prop
        while k < len(ticks) and ticks[k] < vis:
            k += 1
        if k >= len(ticks):
            out.append((p, None, False))
            continue
        out.append((p, ticks[k], True))
    # два пуша на один тик -> виден только последний
    for i in range(len(out) - 1):
        if out[i][1] is not None and out[i][1] == out[i + 1][1]:
            out[i] = (out[i][0], out[i][1], False)
    return out


# ---------------------------------------------------------------- реплей двух режимов
def replay_feed(feed, det, ftime, probe_sec=0.0, probe_horizon=60.0):
    """Проигрывает историю одного фида ДВАЖДЫ (метка = updatedAt / метка = момент детекта)
    и сравнивает решения БОЕВОГО гейта в физически ОДНИ И ТЕ ЖЕ секунды.

    det: [(t_push, t_detect|None, seen)] — общий для обоих режимов (полл один и тот же).
    probe_sec>0 -> дополнительно меряется ХОЛОСТОЕ ВРЕМЯ: сетка с шагом probe_sec на
    probe_horizon секунд после каждой доставленной метки.
    """
    ro, rp = Replay(), Replay()                   # как в бою: подтверждение решётки настоящее
    roc, rpc_ = Replay(True), Replay(True)        # контрфакт: решётка считается подтверждённой
    events = []                                   # (t, kind, payload); kind: 0=deliver 1=decide
    for (tp, td, seen) in det:
        events.append((tp, 1, (tp,)))
        if seen and td is not None:
            events.append((td, 0, (tp, td)))
    events.sort(key=lambda e: (e[0], e[1]))       # доставка раньше решения при равенстве

    res = {"n_push": len(det), "n_seen": sum(1 for d in det if d[2]),
           "decide": 0, "ready_o": 0, "ready_p": 0,
           "open_o": 0, "open_p": 0,
           "muted_by_skew": 0,        # (2) как в бою: P закрыл, O открыт
           "opened_by_skew": 0,       # обратная ошибка: P открыл там, где фазы нет
           "cf_open_o": 0, "cf_open_p": 0,
           "cf_muted": 0,             # (2) контрфакт: чистый эффект скоса при армленном гейте
           "cf_opened": 0,
           "probe_n": 0, "probe_muted": 0, "probe_open_o": 0, "probe_open_p": 0}
    for idx, (t, kind, pl) in enumerate(events):
        if kind == 0:
            tp, td = pl
            ftime.now = td
            for r in (ro, roc):
                r.at(td).note_push(feed, updated_at=tp)
            for r in (rp, rpc_):
                r.at(td).note_push(feed)
            if probe_sec > 0:
                # ХОЛОСТОЕ ВРЕМЯ: гейт спрашивают на каждом тике горячей петли, не только в
                # секунду пуша. Сетка считается СРАЗУ после доставки (буфер = как в тот
                # момент) и обрывается следующим событием — иначе меряли бы гейт с будущими
                # метками, а он тогда fail-open по отрицательному возрасту.
                nxt = events[idx + 1][0] if idx + 1 < len(events) else float("inf")
                lim = min(td + probe_horizon, nxt)
                s = td
                while s < lim:
                    oo = roc.at(s)._gate_open(feed)
                    op = rpc_.at(s)._gate_open(feed)
                    res["probe_n"] += 1
                    res["probe_open_o"] += int(oo)
                    res["probe_open_p"] += int(op)
                    if oo and not op:
                        res["probe_muted"] += 1
                    s += probe_sec
        else:
            tp = pl[0]
            so, sp = ro.at(tp), rp.at(tp)
            ready_o, ready_p = so._lattice_ready(feed), sp._lattice_ready(feed)
            open_o, open_p = so._gate_open(feed), sp._gate_open(feed)
            open_oc, open_pc = roc.at(tp)._gate_open(feed), rpc_.at(tp)._gate_open(feed)
            res["decide"] += 1
            res["ready_o"] += int(ready_o)
            res["ready_p"] += int(ready_p)
            res["open_o"] += int(open_o)
            res["open_p"] += int(open_p)
            res["cf_open_o"] += int(open_oc)
            res["cf_open_p"] += int(open_pc)
            if open_o and not open_p:
                res["muted_by_skew"] += 1
            elif open_p and not open_o:
                res["opened_by_skew"] += 1
            if open_oc and not open_pc:
                res["cf_muted"] += 1
            elif open_pc and not open_oc:
                res["cf_opened"] += 1
    res["frac_o"] = ro.sf._lattice_frac(feed)
    res["frac_p"] = rp.sf._lattice_frac(feed)
    res["stats_o"] = dict(ro.sf.stats)
    res["stats_p"] = dict(rp.sf.stats)
    return res


# ---------------------------------------------------------------- позитивные контроли
def pc3_replay_discriminates(ftime):
    """PC3: реплей обязан УМЕТЬ дать оба ответа на заведомо известных сериях."""
    t0 = 1_700_000_000.0
    perfect = [t0 + 30.0 * i for i in range(40)]
    det = [(p, p + 0.4, True) for p in perfect]
    r = replay_feed("PC", det, ftime)
    if r["frac_o"] < 0.999:
        raise Fail("PC3: идеальная решётка 30с дала frac=%.3f (боевой _lattice_frac сломан?)"
                   % r["frac_o"])
    if r["open_o"] != r["decide"]:
        raise Fail("PC3: на идеальной решётке гейт закрылся %d раз из %d"
                   % (r["decide"] - r["open_o"], r["decide"]))
    # счётчик артефактов обязан уметь и НОЛЬ: лаг детекта постоянный -> фаза не едет
    if r["cf_muted"] or r["cf_opened"]:
        raise Fail("PC3: при ПОСТОЯННОМ лаге детекта счётчик скоса не ноль (%d/%d) — "
                   "счётчик срабатывает сам по себе" % (r["cf_muted"], r["cf_opened"]))
    rng = random.Random(11)
    rnd, t = [], t0
    for _ in range(40):
        t += rng.uniform(20.0, 200.0)
        rnd.append(t)
    r2 = replay_feed("PC", [(p, p + 0.4, True) for p in rnd], ftime)
    if r2["frac_o"] >= MIN_FRAC:
        raise Fail("PC3: СЛУЧАЙНАЯ серия подтвердила решётку (frac=%.3f >= %.2f) — "
                   "измеритель не умеет говорить «нет»" % (r2["frac_o"], MIN_FRAC))
    if r2["ready_o"]:
        raise Fail("PC3: на случайной серии гейт армился %d раз" % r2["ready_o"])
    return {"perfect_frac": r["frac_o"], "random_frac": r2["frac_o"]}


def pc4_poll_model_bites(ftime):
    """PC4: квант полла обязан ПОРТИТЬ идеальную решётку — иначе весь вопрос (2) пустой."""
    t0 = 1_700_000_000.0
    perfect = [t0 + 30.0 * i for i in range(60)]
    worst = None
    for seed in range(6):
        det = poll_detects(perfect, seed=seed)
        r = replay_feed("PC", det, ftime)
        if worst is None or r["frac_p"] < worst["frac_p"]:
            worst = r
    if worst["frac_p"] >= worst["frac_o"]:
        raise Fail("PC4: квант полла НЕ портит решётку (frac_p=%.3f >= frac_o=%.3f) — "
                   "модель полла не работает" % (worst["frac_p"], worst["frac_o"]))
    if not worst["cf_muted"]:
        raise Fail("PC4: на идеальной решётке квант полла не дал НИ ОДНОГО артефакта скоса — "
                   "счётчик не умеет срабатывать")
    return {"frac_o": worst["frac_o"], "frac_p": worst["frac_p"],
            "cf_muted": worst["cf_muted"], "decide": worst["decide"]}


def pc1(rpc, label, agg, rows):
    """PC1: последний AnswerUpdated == latestRoundData() того же агрегатора."""
    if not rows:
        raise Fail("PC1 %s: getLogs не вернул НИ ОДНОГО AnswerUpdated" % label)
    raw = rpc.call("eth_call", [{"to": agg, "data": SEL_LATEST_ROUND}, "latest"])
    body = raw[2:]
    ans = _s256("0x" + body[64:128])
    upd = int(body[192:256], 16)
    last = rows[-1]
    if ans != last["answer"] and upd == last["updated_at"]:
        raise Fail("PC1 %s: цена лога %d != latestRoundData %d при одном updatedAt"
                   % (label, last["answer"], ans))
    if upd < last["updated_at"]:
        raise Fail("PC1 %s: latestRoundData старше последнего лога (%d < %d)"
                   % (label, upd, last["updated_at"]))
    return {"log_answer": last["answer"], "chain_answer": ans,
            "log_updated_at": last["updated_at"], "chain_updated_at": upd}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=9.0)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--feeds", default=",".join(PROXIES))
    ap.add_argument("--probe-sec", type=float, default=1.0)
    ap.add_argument("--blocks", default="", help="from:to — повторить прогон РОВНО на том же "
                                                "диапазоне (кэш логов попадает, RPC не доим)")
    ap.add_argument("--out", default="out/lattice_revalidate.json")
    a = ap.parse_args()
    labels = [x.strip().upper() for x in a.feeds.split(",") if x.strip()]

    ftime = _FrozenTime(sfl.time)
    sfl.time = ftime                                    # шов часов ТОЛЬКО на время реплея
    try:
        log("[0] Позитивные контроли реплея (боевой код на известных сериях)")
        c3 = pc3_replay_discriminates(ftime)
        log("  PC3 ok: идеальная решётка frac=%.3f, случайная frac=%.3f (порог %.2f)"
            % (c3["perfect_frac"], c3["random_frac"], MIN_FRAC))
        c4 = pc4_poll_model_bites(ftime)
        log("  PC4 ok: квант полла %.1fс роняет ту же решётку %.3f -> %.3f "
            "(артефактов скоса %d из %d)"
            % (POLL_SEC, c4["frac_o"], c4["frac_p"], c4["cf_muted"], c4["decide"]))

        log("\n[1] Цепь: агрегаторы и карта времени (только чтения)")
        rpc = Rpc()
        head = int(rpc.call("eth_blockNumber", []), 16)
        meta = resolve_aggregators(rpc, labels)
        clock = BlockClock(rpc, head)
        if a.blocks:
            b_from, b_to = (int(x) for x in a.blocks.split(":"))
            a.days = (b_to - b_from) * 2.0 / 86400.0
        else:
            span = int(a.days * 86400 / 2.0)            # Base тикает 2с
            b_to = head - 30                            # хвост реорганизаций не берём
            b_from = b_to - span
        rng = random.Random(5)
        clock.validate([rng.randint(b_from, b_to) for _ in range(6)])

        log("\n[2] Сбор AnswerUpdated за %.1f сут (блоки %d..%d)" % (a.days, b_from, b_to))
        pushes = {}
        for lb in labels:
            rows = fetch_pushes(rpc, meta[lb]["agg"], b_from, b_to)
            for r in rows:
                r["ts"] = clock.ts(r["block"])
            pushes[lb] = rows
            log("  %-5s n=%d" % (lb, len(rows)))

        log("\n[3] PC1/PC2 — часы и декод")
        pc1_out, zero_upd, lag_all = {}, 0, []
        for lb in labels:
            pc1_out[lb] = pc1(rpc, lb, meta[lb]["agg"], pushes[lb])
            for r in pushes[lb]:
                if not r["updated_at"]:
                    zero_upd += 1
                else:
                    lag_all.append(r["ts"] - r["updated_at"])
        n_all = sum(len(v) for v in pushes.values())
        if n_all and zero_upd / float(n_all) > 0.01:
            raise Fail("PC2: у %d/%d логов updatedAt == 0 — «оракульных часов» нет"
                       % (zero_upd, n_all))
        lagq = quantiles(lag_all)
        log("  PC1 ok на всех %d фидах; PC2 ok: updatedAt непустой у %d/%d логов"
            % (len(labels), n_all - zero_upd, n_all))
        log("  block.timestamp - updatedAt: p10/50/90 = %s/%s/%s с (n=%d)"
            % (lagq.get("p10"), lagq.get("p50"), lagq.get("p90"), len(lag_all)))

        log("\n[4] ВОПРОС (1): доля пушей на узлах решётки P=%.0fс ±%.0fс (порог %.2f)"
            % (P_SEC, TOL_SEC, MIN_FRAC))
        q1 = {}
        for lb in labels:
            rows = [dict(r, ts_o=(r["updated_at"] or r["ts"])) for r in pushes[lb]]
            m_o = [float(r["ts_o"]) for r in rows]
            m_b = [float(r["ts"]) for r in rows]
            so, sb = static_frac(m_o), static_frac(m_b)
            gaps_all = [b - x for x, b in zip(m_o, m_o[1:])]
            byq = frac_by_prize(rows)
            q1[lb] = {"n_push": len(rows), "oracle": so, "block": sb, "by_prize": byq,
                      "gap_q": quantiles(gaps_all) if gaps_all else {},
                      "in_horizon_share": (so["n"] / float(len(gaps_all))) if gaps_all else None}
            if so["n"] < MIN_N:
                log("  %-5s гэпов в горизонте %dс всего %d (<%d) — решётка НЕ заявляется, "
                    "гейт для фида fail-open" % (lb, MAX_AGE, so["n"], MIN_N))
                continue
            log("  %-5s n_gap=%-5d в горизонте %-5d  ORACLE frac=%.3f (lift ×%.1f при базе "
                "%.2f)  BLOCK frac=%.3f  %s"
                % (lb, len(gaps_all), so["n"], so["frac"], so["lift"], so["chance"],
                   sb["frac"], "ПОДТВЕРЖДАЕТ" if so["frac"] >= MIN_FRAC else "НЕ подтверждает"))
            if byq:
                log("        по КВАРТИЛЯМ ценового шага (низ->верх): %s"
                    % "  ".join("%.0f%%(n=%d)" % (x["frac"] * 100, x["n"]) for x in byq))

        log("\n[5] ВОПРОС (2): реплей двух режимов меток (%d seed'ов полла)" % a.seeds)
        q2 = {}
        for lb in labels:
            rows = [r for r in pushes[lb] if r["updated_at"]]
            ts = [float(r["updated_at"]) for r in rows]
            if len(ts) < MIN_N + 1:
                continue
            runs = []
            for seed in range(a.seeds):
                det = poll_detects(ts, seed=seed)
                runs.append(replay_feed(lb, det, ftime, probe_sec=a.probe_sec))
            agg = {}
            for k in ("decide", "ready_o", "ready_p", "open_o", "open_p", "muted_by_skew",
                      "opened_by_skew", "cf_open_o", "cf_open_p", "cf_muted", "cf_opened",
                      "probe_n", "probe_muted", "probe_open_o", "probe_open_p", "n_seen"):
                vals = sorted(r[k] for r in runs)
                agg[k] = {"med": vals[len(vals) // 2], "min": vals[0], "max": vals[-1]}
            agg["frac_o"] = sorted(r["frac_o"] for r in runs)[len(runs) // 2]
            agg["frac_p"] = sorted(r["frac_p"] for r in runs)[len(runs) // 2]
            agg["frac_p_max"] = max(r["frac_p"] for r in runs)
            agg["n_push"] = runs[0]["n_push"]
            q2[lb] = agg
            d = agg["decide"]["med"] or 1
            log("  %-5s пушей %-5d  frac буфером: оракул %.3f / полл %.3f (макс %.3f) | "
                "гейт армлен (оракул/полл) %d/%d из %d"
                % (lb, agg["n_push"], agg["frac_o"], agg["frac_p"], agg["frac_p_max"],
                   agg["ready_o"]["med"], agg["ready_p"]["med"], agg["decide"]["med"]))
            log("        КАК В БОЮ: заглушено скосом %d (обратно %d) | "
                "КОНТРФАКТ (решётка подтверждена): пуш в фазе оракул %d/%d = %.0f%%, "
                "полл %d/%d = %.0f%%, АРТЕФАКТ СКОСА %d (%.0f%% решений)"
                % (agg["muted_by_skew"]["med"], agg["opened_by_skew"]["med"],
                   agg["cf_open_o"]["med"], d, 100.0 * agg["cf_open_o"]["med"] / d,
                   agg["cf_open_p"]["med"], d, 100.0 * agg["cf_open_p"]["med"] / d,
                   agg["cf_muted"]["med"], 100.0 * agg["cf_muted"]["med"] / d))
            if agg["probe_n"]["med"]:
                pn = agg["probe_n"]["med"]
                log("        холостое время окна (%.0fс после метки, шаг %.1fс): открыто "
                    "оракулом %.0f%%, поллом %.0f%%; заглушено скосом %.0f%% тиков"
                    % (60.0, a.probe_sec, 100.0 * agg["probe_open_o"]["med"] / pn,
                       100.0 * agg["probe_open_p"]["med"] / pn,
                       100.0 * agg["probe_muted"]["med"] / pn))

        log("\n[6] СТРУКТУРА: где решётка держится и где сыпется (оракульные часы)")
        all_gaps, marks_all = [], []
        for lb in labels:
            m = [float(r["updated_at"] or r["ts"]) for r in pushes[lb]]
            marks_all.append(m)
            all_gaps += [b - x for x, b in zip(m, m[1:]) if 0 < b - x <= MAX_AGE]
        by_gap = frac_by_gap(all_gaps)
        res_hist = residues(all_gaps)
        n_res = float(sum(res_hist.values())) or 1.0
        log("  по РАССТОЯНИЮ до прошлого пуша:")
        for r in by_gap:
            log("    %4d-%-5dс n=%-5d на решётке %.3f" % (r["lo"], r["hi"], r["n"], r["frac"]))
        top = sorted(res_hist.items(), key=lambda kv: -kv[1])[:5]
        log("  остатки gap mod %.0f (топ-5): %s" % (
            P_SEC, ", ".join("%d=%.1f%%" % (k, 100.0 * v / n_res) for k, v in top)))
        days = {}
        for m in marks_all:
            for d, v in frac_by_day(m).items():
                n, h = days.get(d, (0, 0))
                days[d] = (n + v["n"], h + v["n"] * v["frac"])
        worst = sorted(((h / n, d, n) for d, (n, h) in days.items() if n >= 50))[:3]
        log("  по суткам (n>=50): худшие — %s"
            % ", ".join("%s %.3f (n=%d)" % (d, f, n) for f, d, n in worst))
        struct = {"by_gap": by_gap, "residues": res_hist,
                  "by_day": {d: {"n": n, "frac": h / n} for d, (n, h) in sorted(days.items())}}

        out = {"generated": int(time.time()), "days": a.days, "blocks": [b_from, b_to],
               "structure": struct,
               "params": {"P": P_SEC, "TOL": TOL_SEC, "MAX_AGE": MAX_AGE, "MIN_N": MIN_N,
                          "MIN_FRAC": MIN_FRAC, "POLL_SEC": POLL_SEC},
               "pc": {"pc1": pc1_out, "pc3": c3, "pc4": c4,
                      "block_lag_q": lagq, "zero_updated_at": zero_upd},
               "q1_lattice": q1, "q2_replay": q2}
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1, default=str)
        log("\n[=] записан %s (RPC-вызовов: %d)" % (a.out, rpc.calls))
    finally:
        sfl.time = ftime._real                          # часы боевого модуля возвращаем


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        log("\nПРОВАЛ: %s" % e)
        sys.exit(2)
