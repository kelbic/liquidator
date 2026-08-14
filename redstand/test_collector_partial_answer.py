"""#101 хвосты: частичный ответ узла (блокер №3) и ПОЛНОТА отката (блокер №2).

БЛОКЕР №3 — «УЗЕЛ НЕ ОТВЕТИЛ ПРО ЭТОТ id» НЕОТЛИЧИМО ОТ «НЕ НАМАЙНЕНО».
`batch_receipts` держит контракт «ключ есть у каждого запрошенного» префиллом {h: None}. Если
узел ответил не про все id (короче массив, поэлементная ошибка), недостача выглядела как
ненамайненные пробы: партия молча теряла квитанции, а через COLLECT_TIMEOUT_SEC (180с) живая
нога получала сброс nonce и слом лестницы — приговор по МОЛЧАНИЮ узла.

БЛОКЕР №2 — РУЧКА ОТКАТА СНИМАЛА ПОЛОВИНУ РЕШЕНИЯ.
SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY=1 возвращала «пусто -> переспросить боевым», но НЕ
возвращала «молчат оба узла -> вернуть {}». В прежней редакции исключения на этом пути не
существовало вовсе, поэтому с «откатом» вызывающий всё равно шёл по НОВОЙ ветке.

ШОВ: подменяется только сетевой стык `chain.execute._seq_session` (НИЖЕ дефекта). Настоящими
исполняются `chain.execute.batch_receipts`, `chain.spec_fire_live.collect_receipts`, боевой
метод `SpecFireLive._receipts_fn` и `chain.fire_engine.ReceiptCollector.tick`.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.execute as ex                                    # noqa: E402
import chain.fire_engine as fe                                # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

DED = "https://dedicated.example/KEYPATH"
COMBAT = "https://combat.example/KEYPATH"
H = ["0x" + "%064x" % i for i in range(5)]
POSTS = []


class _Resp:
    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


class _Sess:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def post(self, url, json=None, timeout=None):
        POSTS.append((url, [it["params"][0] for it in json]))
        b = self.behaviour[url]
        if isinstance(b, Exception):
            raise b
        return _Resp(b(json))


def answers(ids, mined=(), errors=()):
    """Узел отвечает ТОЛЬКО про перечисленные id (остальные молча пропускает)."""
    want, bad = {H[i] for i in ids}, {H[i] for i in errors}
    mine = {H[i] for i in mined}

    def _f(payload):
        out = []
        for it in payload:
            h = it["params"][0]
            if h in bad:
                out.append({"jsonrpc": "2.0", "id": it["id"],
                            "error": {"code": -32000, "message": "boom"}})
            elif h in want:
                out.append({"jsonrpc": "2.0", "id": it["id"],
                            "result": ({"status": "0x1"} if h in mine else None)})
        return out
    return _f


REASKS: list = []


def run(behaviour, hs=None, **kw):
    POSTS.clear()
    REASKS.clear()
    ex._SEQ_SESSIONS.clear()
    sess = _Sess(behaviour)
    _orig = ex._seq_session
    ex._seq_session = lambda url: sess
    try:
        fired = []
        out = sfl.collect_receipts(hs or H, DED, COMBAT, ex.batch_receipts,
                                   on_fallback=fired.append,
                                   on_reask=lambda n, why: REASKS.append((n, why)), **kw)
        return out, list(POSTS), fired
    finally:
        ex._seq_session = _orig


def combat_posts(posts):
    return [p for p in posts if p[0] == COMBAT]


# --- 1. КОНТРАКТ: ответ несёт множество ОТВЕЧЕННЫХ id ------------------------------------
POSTS.clear()
_o = ex._seq_session
ex._seq_session = lambda url: _Sess({DED: answers(ids=(0, 1), mined=(0,))})
try:
    got = ex.batch_receipts(DED, H)
finally:
    ex._seq_session = _o
assert set(got) == set(H), "контракт «ключ у каждого запрошенного» сломан: %s" % sorted(got)
assert got.answered == frozenset(H[:2]), "множество отвеченных id неверно: %s" % (got.answered,)
assert ex.unanswered_hashes(got, H) == H[2:], "недостача не вычисляется: %s" % ex.unanswered_hashes(got, H)
assert ex.unanswered_hashes({h: None for h in H}, H) == [], \
    "обычный словарь (двойник/сторонний шов) обязан считаться ПОЛНЫМ ответом"
# поэлементная ошибка — тоже «не ответил», а не «не намайнено»
ex._seq_session = lambda url: _Sess({DED: answers(ids=(0, 1), mined=(0,), errors=(1,))})
try:
    got2 = ex.batch_receipts(DED, H)
finally:
    ex._seq_session = _o
assert got2.answered == frozenset([H[0]]), "поэлементная ошибка засчитана как ответ: %s" % (got2.answered,)
# И ТА ЖЕ ПРОВЕРКА НА НАРЕЗАННОЙ ПАРТИИ: в бою chunk=3 (предел dRPC), то есть боевая ветка —
# именно эта. Слияние кусков обязано складывать и ответы, и множество отвеченных id.
ex._seq_session = lambda url: _Sess({DED: answers(ids=(0, 2, 4), mined=(2,))})
try:
    got3 = ex.batch_receipts(DED, H, chunk=2)          # куски [0,1] [2,3] [4]
finally:
    ex._seq_session = _o
assert set(got3) == set(H) and got3.answered == frozenset([H[0], H[2], H[4]]), \
    "нарезка потеряла отчёт об ответах: %s" % (got3.answered,)
assert got3[H[2]] == {"status": "0x1"} and got3[H[0]] is None
assert ex.unanswered_hashes(got3, H) == [H[1], H[3]]
# а кусок, на который узел не ответил ВООБЩЕ, — это отказ узла, а не «не намайнено»
ex._seq_session = lambda url: _Sess({DED: answers(ids=(0, 1), mined=())})
try:
    ex.batch_receipts(DED, H, chunk=2)
    raise AssertionError("полностью немой кусок принят за пустую партию")
except ex.ReceiptsUnavailable:
    pass
finally:
    ex._seq_session = _o
print("OK 1/6: batch_receipts несёт .answered (и при нарезке, боевой путь chunk=3) — «узел "
      "промолчал про id» отделено от «не намайнено»")

# --- 2. ЭФФЕКТ: недостача ПЕРЕСПРАШИВАЕТСЯ, а полученное не теряется ---------------------
out, posts, fired = run({DED: answers(ids=(0, 1, 2), mined=(0,)),
                         COMBAT: answers(ids=(0, 1, 2, 3, 4), mined=(3,))})
cp = combat_posts(posts)
assert len(cp) == 1, "переспроса не было: %s" % posts
assert cp[0][1] == H[3:], "переспрошена не недостача, а %s (лишний расход боевого ключа)" % cp[0][1]
assert out[H[0]] == {"status": "0x1"}, "квитанция выделенного узла потеряна при переспросе"
assert out[H[3]] == {"status": "0x1"}, "переспрос не добрал квитанцию: %r" % out
assert out[H[4]] is None and out[H[1]] is None and out[H[2]] is None, \
    "ненамайненные подменены: %r" % out
assert fired == [], "переспрос засчитан как УВОД ПАРТИИ — по collector_fallback выносится #99"
assert len(REASKS) == 1 and REASKS[0][0] == 2 and "частичный ответ" in REASKS[0][1], REASKS
print("OK 2/6: частичный ответ (3 id из 5) -> переспрошены РОВНО 2 недостающих, квитанции "
      "добраны, и это НЕ увод партии (свой счётчик)")

# --- 3. ПОЛНЫЙ ответ переспроса НЕ вызывает (боевой ключ не трогаем) --------------------
out, posts, fired = run({DED: answers(ids=(0, 1, 2, 3, 4), mined=(1,)),
                         COMBAT: answers(ids=(0, 1, 2, 3, 4), mined=(0, 1, 2, 3, 4))})
assert combat_posts(posts) == [], "боевой дёрнут при ПОЛНОМ ответе выделенного: %s" % posts
assert out[H[1]] == {"status": "0x1"} and out[H[0]] is None
print("OK 3/6: полный ответ (в т.ч. «ничего не намайнено») -> 0 обращений к боевому")


# --- 4. НАСТОЯЩИЙ КОЛЛЕКТОР: потерянная квитанция = сброшенная лестница ------------------
# Дрель ВО ВРЕМЕНИ: частичный ответ на каждом полле, дедлайн 100с. С переспросом квитанция
# доезжает; без него нога доживает до таймаута и теряет склад.
def _collector(fn, now_box):
    resolved, tmo = [], []
    col = fe.ReceiptCollector(receipts_fn=fn, on_result=lambda m, h, r: resolved.append(h),
                              on_timeout=lambda m, h: tmo.append(h),
                              now=lambda: now_box[0], sleep=lambda *_: None,
                              min_age_sec=0.0, timeout_sec=100.0)
    return col, resolved, tmo


_saved_url = sfl.SPEC_FIRE_COLLECTOR_RPC_URL
_saved_chunk = sfl.SPEC_FIRE_COLLECTOR_BATCH_CHUNK


class _Rpc:
    rpc_url = COMBAT


class _Stub:
    _log = staticmethod(lambda m: LOGS.append(m))
    _now = staticmethod(lambda: NOW[0])
    rpc = _Rpc()
    stats: dict = {}
    _receipts_fn = sfl.SpecFireLive._receipts_fn
    _note_collector_fallback = sfl.SpecFireLive._note_collector_fallback
    _note_collector_reask = sfl.SpecFireLive._note_collector_reask


LOGS: list = []
NOW = [1000.0]

try:
    sfl.SPEC_FIRE_COLLECTOR_RPC_URL = DED
    sfl.SPEC_FIRE_COLLECTOR_BATCH_CHUNK = 0
    st = _Stub()
    fn = st._receipts_fn(lambda: COMBAT)
    beh = {DED: answers(ids=(0, 1, 2), mined=(0,)),          # про 3..4 узел молчит ВСЕГДА
           COMBAT: answers(ids=(0, 1, 2, 3, 4), mined=(0, 3))}
    _o = ex._seq_session
    ex._seq_session = lambda url: _Sess(beh)
    try:
        now = [5000.0]
        col, resolved, tmo = _collector(fn, now)
        for h in H:
            col.track(h, ("leg", None))
        col.tick()
        assert sorted(resolved) == sorted([H[0], H[3]]), \
            "квитанция из молчащей части партии потеряна: %s" % resolved
        assert tmo == [], "таймаут до дедлайна"
        # и во ВРЕМЕНИ: то, чего нет ни у кого, честно доживает до дедлайна и снимает бронь
        now[0] = 5101.0
        col.tick()
        assert sorted(tmo) == sorted([H[1], H[2], H[4]]), \
            "брони не сняты по дедлайну — гейт огня самозапирается: %s" % tmo
    finally:
        ex._seq_session = _o
    print("OK 4/6: настоящий ReceiptCollector — квитанция из НЕОТВЕЧЕННОЙ половины доехала за "
          "один тик; ненамайненные сняты дедлайном (огонь не заперт)")

    # --- 5. ОТКАТ ПЕРЕСПРОСА снимает МЕХАНИЗМ, и регрессия видна ------------------------
    _sv = sfl.SPEC_FIRE_COLLECTOR_REASK_PARTIAL
    try:
        sfl.SPEC_FIRE_COLLECTOR_REASK_PARTIAL = False
        POSTS.clear()
        ex._seq_session = lambda url: _Sess(beh)
        now = [7000.0]
        col, resolved, tmo = _collector(fn, now)
        for h in H:
            col.track(h, ("leg", None))
        col.tick()
        assert resolved == [H[0]], "с выключенным переспросом квитанция всё равно доехала: %s" % resolved
        now[0] = 7101.0
        col.tick()
        assert H[3] in tmo, ("КОНТРОЛЬ ПРОВАЛЕН: без переспроса намайненная проба H3 не потерялась "
                             "— значит раздел 4 ничего не доказывает")
        assert POSTS and all(p[0] == DED for p in POSTS), "без переспроса боевой всё равно дёрнут"
    finally:
        sfl.SPEC_FIRE_COLLECTOR_REASK_PARTIAL = _sv
        ex._seq_session = _o
    print("OK 5/6: КОНТРОЛЬ — с SPEC_FIRE_COLLECTOR_REASK_PARTIAL=0 та же партия ТЕРЯЕТ "
          "намайненную квитанцию и сбрасывает склад ноги по таймауту")
finally:
    sfl.SPEC_FIRE_COLLECTOR_RPC_URL = _saved_url
    sfl.SPEC_FIRE_COLLECTOR_BATCH_CHUNK = _saved_chunk

# --- 6. ОТКАТ FALLBACK_ON_EMPTY возвращает ПРЕЖНЮЮ РЕДАКЦИЮ ЦЕЛИКОМ ---------------------
# 6a. по умолчанию (механизм включён) молчание ОБОИХ узлов = исключение наружу
_raised = None
try:
    run({DED: OSError("timed out"), COMBAT: OSError("timed out")})
except ex.ReceiptsUnavailable as e:
    _raised = e
assert _raised is not None, "молчание обоих узлов не пробросилось — коллектор не отличит его от «пусто»"
# 6b. с ручкой — прежнее поведение: пустая партия ВМЕСТО исключения
_sv = sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY
try:
    sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY = True
    out, posts, fired = run({DED: OSError("timed out"), COMBAT: OSError("timed out")})
    assert out == {}, "откат не вернул прежнее «вернуть {}»: %r" % out
    assert any("молчат оба узла" in f for f in fired), fired
    # и второй половиной ручки — прежнее решение по ПУСТОТЕ партии
    out, posts, fired = run({DED: answers(ids=(0, 1, 2, 3, 4)),
                             COMBAT: answers(ids=(0, 1, 2, 3, 4), mined=(0,))})
    assert len(combat_posts(posts)) == 1, "откат не вернул решение по пустоте: %s" % posts
    assert combat_posts(posts)[0][1] == H, "прежняя редакция переспрашивала партию ЦЕЛИКОМ"
    # и переспроса частичного ответа в прежней редакции не было вовсе
    out, posts, fired = run({DED: answers(ids=(0,), mined=(0,)),
                             COMBAT: answers(ids=(0, 1, 2, 3, 4), mined=(1,))})
    assert combat_posts(posts) == [], "откат оставил переспрос — это уже НЕ прежняя редакция: %s" % posts
finally:
    sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY = _sv
# 6c. ручка читается из окружения, а не только правится в памяти
_src = pathlib.Path(__file__).resolve().parent / "chain/spec_fire_live.py"
_src = _src.read_text()
assert '"SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY"' in _src and \
       '"SPEC_FIRE_COLLECTOR_REASK_PARTIAL"' in _src, "ручки отката не из окружения"
print("OK 6/6: SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY=1 возвращает ПРЕЖНЮЮ РЕДАКЦИЮ ЦЕЛИКОМ: "
      "и {} вместо исключения, и решение по пустоте, и отсутствие переспроса")


# --- 7. ПОТОЛОК ОБЪЁМА ПЕРЕСПРОСА В БОЕВОЙ КЛЮЧ (блокер ревью 12.08) ---------------------
# Лечение #101 не имело границы: систематически частичный узел давал переспрос подмножества
# КАЖДЫЙ полл все 180с — ровно нагрузка, от которой уводил #99 (замер ревьюера: половинный
# ответ = 21 подзапрос за тик = ~315 CU/с устойчиво).
H64 = ["0x" + "%064x" % (1000 + i) for i in range(64)]


def answer_only(hs_answered, mined=()):
    """Узел отвечает только про перечисленные ХЭШИ (остальные молча пропускает)."""
    want, mine = set(hs_answered), set(mined)

    def _f(payload):
        out = []
        for it in payload:
            h = it["params"][0]
            if h in want:
                out.append({"jsonrpc": "2.0", "id": it["id"],
                            "result": ({"status": "0x1"} if h in mine else None)})
        return out
    return _f


# 7a. УЗЕЛ СИСТЕМАТИЧЕСКИ ЧАСТИЧЕН (1 id из 64): боевой ключ не платит ВООБЩЕ, причина — одна
# громкая строка. Приёмка ревьюера дословно.
out, posts, fired = run({DED: answer_only(H64[:1], mined=H64[:1]),
                         COMBAT: answer_only(H64, mined=H64)}, hs=H64)
assert combat_posts(posts) == [], "боевой ключ оплатил деградацию узла: %s" % combat_posts(posts)
assert len(REASKS) == 1 and REASKS[0][0] == 0 and "систематически частичен" in REASKS[0][1], REASKS
assert fired == [], "деградация узла засчитана в collector_fallback (#99 смешается с B4)"
assert set(out) == set(H64), "контракт «ключ у каждого запрошенного» сломан"
assert out[H64[0]] == {"status": "0x1"} and out[H64[5]] is None
print("OK 7a: узел ответил про 1 id из 64 -> 0 обращений к боевому и РОВНО одна строка причины")

# 7b. Недостача в пределах доли, но выше потолка за тик: переспрашиваем ПЕРВЫЕ (они же
# ближайшие к дедлайну — ReceiptCollector отдаёт хэши в порядке постановки, дедлайн у всех один).
out, posts, fired = run({DED: answer_only(H64[:40]),
                         COMBAT: answer_only(H64, mined=H64[40:44])}, hs=H64)
cp = combat_posts(posts)
assert len(cp) == 1, cp
assert cp[0][1] == H64[40:40 + sfl.SPEC_FIRE_COLLECTOR_REASK_MAX], \
    "переспрошены не ближайшие к дедлайну (или мимо потолка): %d id" % len(cp[0][1])
assert len(REASKS) == 1 and REASKS[0][0] == sfl.SPEC_FIRE_COLLECTOR_REASK_MAX, REASKS
assert "потолок" in REASKS[0][1], REASKS
assert out[H64[40]] == {"status": "0x1"}, "квитанция ближайшей к дедлайну не добрана"
assert out[H64[60]] is None, "неспрошенный id получил значение из ниоткуда"
print("OK 7b: недостача 24 из 64 -> переспрошено %d ближайших к дедлайну, остальные ждут "
      "следующего полла" % sfl.SPEC_FIRE_COLLECTOR_REASK_MAX)

# 7c. ПОРЯДОК, на который опирается 7b, — свойство НАСТОЯЩЕГО коллектора, а не соглашение.
_order = []
_col = fe.ReceiptCollector(receipts_fn=lambda hs: (_order.append(list(hs)) or {h: None for h in hs}),
                           on_result=lambda *a: None, on_timeout=lambda *a: None,
                           now=lambda: 0.0, sleep=lambda *_: None, min_age_sec=0.0)
for h in H64[:10]:
    _col.track(h, ("leg", None))
_col.tick()
assert _order and _order[0] == H64[:10], "коллектор отдаёт хэши не в порядке постановки: %s" % _order
print("OK 7c: ReceiptCollector отдаёт партию в порядке ПОСТАНОВКИ — «первые» и есть ближайшие "
      "к дедлайну (таймаут у всех один)")

# 7d. СЧЁТЧИКИ РАЗДЕЛЕНЫ — через БОЕВЫЕ методы, а не пересказ ветки.
LOGS.clear()
_Stub.stats = {}
try:
    sfl.SPEC_FIRE_COLLECTOR_RPC_URL = DED
    sfl.SPEC_FIRE_COLLECTOR_BATCH_CHUNK = 0
    st2 = _Stub()
    fn2 = st2._receipts_fn(lambda: COMBAT)
    _o2 = ex._seq_session
    ex._seq_session = lambda url: _Sess({DED: answers(ids=(0, 1, 2), mined=(0,)),
                                         COMBAT: answers(ids=(0, 1, 2, 3, 4), mined=(3,))})
    try:
        got7 = fn2(H)
    finally:
        ex._seq_session = _o2
    assert got7[H[3]] == {"status": "0x1"}
    assert st2.stats.get("collector_fallback", 0) == 0, \
        "переспрос вырос в collector_fallback — вердикт #99 станет нечитаемым: %s" % st2.stats
    assert st2.stats.get("collector_reask_partial") == 2, st2.stats
    assert not any("уведена на БОЕВОЙ" in m for m in LOGS), \
        "переспрос печатается как увод партии: %s" % LOGS
    assert any("частичный ответ" in m for m in LOGS), LOGS
finally:
    sfl.SPEC_FIRE_COLLECTOR_RPC_URL = _saved_url
    sfl.SPEC_FIRE_COLLECTOR_BATCH_CHUNK = _saved_chunk
print("OK 7d: переспрос считается отдельно (collector_reask_partial=2), collector_fallback НЕ "
      "растёт — приёмка #99 и приёмка потолка не смешиваются")

# 7e. ОТКАТ потолка — ручкой, и он снимает МЕХАНИЗМ (доля/потолок больше не спрашиваются).
_svf, _svm = sfl.SPEC_FIRE_COLLECTOR_REASK_MAX_FRAC, sfl.SPEC_FIRE_COLLECTOR_REASK_MAX
try:
    sfl.SPEC_FIRE_COLLECTOR_REASK_MAX_FRAC = 1.0
    sfl.SPEC_FIRE_COLLECTOR_REASK_MAX = 10**6
    out, posts, fired = run({DED: answer_only(H64[:1], mined=H64[:1]),
                             COMBAT: answer_only(H64, mined=H64)}, hs=H64)
    cp = combat_posts(posts)
    assert len(cp) == 1 and len(cp[0][1]) == 63, \
        ("КОНТРОЛЬ ПРОВАЛЕН: без потолка переспрос всё равно не ушёл в боевой — 7a ничего "
         "не доказывает: %s" % cp)
finally:
    sfl.SPEC_FIRE_COLLECTOR_REASK_MAX_FRAC, sfl.SPEC_FIRE_COLLECTOR_REASK_MAX = _svf, _svm
print("OK 7e: КОНТРОЛЬ — без потолка тот же стенд шлёт 63 id в боевой ключ каждый полл")

print("=== #101 хвосты закрыты: частичный ответ переспрашивается, откат снимает механизм целиком ===")
