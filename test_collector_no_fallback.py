"""#101 Страховка боевым узлом — ТОЛЬКО на реальном отказе выделенного.

ЧТО БЫЛО. `_receipts` уводил партию на боевой Alchemy по признаку «выделенный вернул пустой
словарь», а комментарий рядом обосновывал безопасность так: «непришедшая квитанция приезжает
как {hash: None}, то есть словарь НЕ пуст». В `batch_receipts` вставка шла под `if r is not
None` — ключа со значением None не появлялось НИКОГДА. Значит «ни одна из 64 проб ещё не в
блоке» и «узел лёг» давали ОДИН И ТОТ ЖЕ {}, и каждый такой полл переспрашивал те же хэши у
боевого ключа: батч 64 x 15 CU = 960 CU за переспрос.

ЗАМЕР НА БОЮ (12.08, журнал Шарлотты, 134 окна подряд): collector_fallback == polls в КАЖДОМ
окне (4953 == 4953), при этом коллектор резолвил ~15.7 квитанций на полл. То есть выделенный
узел (bg-2, dRPC) не отдавал НИЧЕГО пригодного вообще ни разу, а весь резолв делал боевой
Alchemy — увод #99 не работал ни секунды и молчал об этом.

ШОВ. Подменяется ТОЛЬКО сетевой стык — `chain.execute._seq_session` (ниже дефекта). Настоящими
исполняются `chain.execute.batch_receipts` (сборка батча, разбор ответа, учёт CU, классификация
отказа) и настоящая `chain.spec_fire_live.collect_receipts` (решение о страховке), а в разделе 5
— настоящий боевой метод `SpecFireLive._receipts_fn` и настоящий `ReceiptCollector.tick`.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.execute as ex                                    # noqa: E402
import chain.rpc as rpc                                       # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

DED = "https://dedicated.example/KEYPATH"
COMBAT = "https://combat.example/KEYPATH"
H = ["0x" + "%064x" % i for i in range(4)]

POSTS = []                                                    # [(url, n_subrequests)]


class _Resp:
    def __init__(self, body):
        self._b = body

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


class _Sess:
    """Фальшивая сессия requests. Отвечает ПО URL — ровно то, что задал сценарий."""

    def __init__(self, behaviour):
        self.behaviour = behaviour

    def post(self, url, json=None, timeout=None):
        POSTS.append((url, len(json)))
        b = self.behaviour[url]
        if isinstance(b, Exception):
            raise b
        return _Resp(b(json) if callable(b) else b)


def mined(ids):
    """Ответ узла: у перечисленных id квитанция есть, у остальных result=null (не намайнено)."""
    def _f(payload):
        return [{"jsonrpc": "2.0", "id": it["id"],
                 "result": ({"status": "0x1", "gasUsed": "0x1"} if it["id"] in ids else None)}
                for it in payload]
    return _f


def run(behaviour, hs=None):
    POSTS.clear()
    rpc.rpc_usage_take()                                      # обнуляем окно учёта CU
    ex._SEQ_SESSIONS.clear()
    sess = _Sess(behaviour)
    _orig = ex._seq_session
    ex._seq_session = lambda url: sess
    try:
        fired = []
        out = sfl.collect_receipts(hs or H, DED, COMBAT, ex.batch_receipts,
                                   on_fallback=fired.append)
        return out, list(POSTS), fired
    finally:
        ex._seq_session = _orig


def combat_calls(posts):
    return [p for p in posts if p[0] == COMBAT]


# --- 1. КОНТРАКТ batch_receipts: «запрошен, но не намайнен» = ключ со значением None -----
POSTS.clear()
_s = _Sess({DED: mined(set())})
_o = ex._seq_session
ex._seq_session = lambda url: _s
try:
    got = ex.batch_receipts(DED, H)
finally:
    ex._seq_session = _o
assert set(got) == set(H), "контракт нарушен: в словаре не все запрошенные хэши: %s" % sorted(got)
assert all(got[h] is None for h in H), "ненамайненный хэш приехал не как None: %r" % got
assert got != {}, "«ничего не намайнено» снова неотличимо от отказа — это и был дефект"

# отказ транспорта: НЕ словарь, а исключение — различие настоящее, а не соглашение о размере
_s = _Sess({DED: OSError("connection reset by peer")})
ex._seq_session = lambda url: _s
try:
    ex.batch_receipts(DED, H)
    raise AssertionError("отказ транспорта вернул словарь вместо ReceiptsUnavailable")
except ex.ReceiptsUnavailable as e:
    assert "dedicated.example" in str(e), "в отказе нет хоста — не понять, какой контур лёг"
    assert "KEYPATH" not in str(e), "СЕКРЕТ: путь URL уехал в текст исключения"
finally:
    ex._seq_session = _o
print("OK 1/6: контракт — {hash: None} у ненамайненных, отказ = ReceiptsUnavailable(хост)")

# --- 2. НИЧЕГО НЕ НАМАЙНЕНО -> НОЛЬ обращений к боевому ----------------------------------
out, posts, fired = run({DED: mined(set()), COMBAT: mined({0, 1, 2, 3})})
assert combat_calls(posts) == [], "боевой узел дёрнут на «ещё не намайнено»: %s" % posts
assert out == {h: None for h in H}, "ответ подменён боевым: %r" % out
assert fired == [], "страховка засчитана без отказа"
print("OK 2/6: «ничего не намайнено» -> 0 обращений к боевому (было 1 на КАЖДЫЙ полл)")

# --- 3. ОТКАЗ ТРАНСПОРТА -> РОВНО одно обращение к боевому -------------------------------
out, posts, fired = run({DED: OSError("timed out"), COMBAT: mined({0, 1, 2, 3})})
assert len(combat_calls(posts)) == 1, "на отказе боевой дёрнут %d раз" % len(combat_calls(posts))
assert all(out[h] is not None for h in H), "страховка не добрала квитанции: %r" % out
assert len(fired) == 1 and "отказ узла" in fired[0], fired
# батч не принят узлом (одиночная ошибка на массив) — тоже ОТКАЗ, а не «нет квитанций»
out, posts, fired = run({DED: {"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32600, "message": "batch not supported"}},
                         COMBAT: mined({0, 1, 2, 3})})
assert len(combat_calls(posts)) == 1, "непринятый батч не признан отказом: %s" % posts
assert "batch not supported" in fired[0], fired
print("OK 3/6: отказ транспорта и непринятый батч -> РОВНО одно обращение к боевому")

# --- 4. ЧАСТИЧНО НАМАЙНЕНО -> ноль обращений к боевому, None у ненамайненных -------------
out, posts, fired = run({DED: mined({0, 2}), COMBAT: mined({0, 1, 2, 3})})
assert combat_calls(posts) == [], "боевой дёрнут при частично намайненной партии: %s" % posts
assert out[H[0]] is not None and out[H[2]] is not None, "намайненные потеряны: %r" % out
assert out[H[1]] is None and out[H[3]] is None, "ненамайненные приехали не как None: %r" % out
assert set(out) == set(H), "не все запрошенные хэши в ответе"
print("OK 4/6: частичная партия -> 0 обращений к боевому, None у ненамайненных")

# --- 5. УЧЁТ CU: батч виден телеметрии, и по ПОДЗАПРОСАМ ---------------------------------
rpc.rpc_usage_take()
POSTS.clear()
_s = _Sess({DED: mined(set())})
ex._seq_session = lambda url: _s
try:
    ex.batch_receipts(DED, H)
finally:
    ex._seq_session = _o
snap = rpc.rpc_usage_take()
_hosts = [h for h in snap if h.startswith("dedicated.example")]
assert _hosts, "батч ушёл МИМО учёта CU — дыра открыта заново: %r" % snap
_n = snap[_hosts[0]].get("eth_getTransactionReceipt")
assert _n == len(H), "учтено %r подзапросов вместо %d (у Alchemy тариф поштучный)" % (_n, len(H))
assert rpc.rpc_usage_cu(snap) == 15 * len(H), \
    "цена батча посчитана как за один вызов: %d" % rpc.rpc_usage_cu(snap)
print("OK 5/6: батч под учётом CU — %d подзапросов = %d CU (HTTP-пост один)"
      % (_n, rpc.rpc_usage_cu(snap)))

# --- 6. ПОЗИТИВНЫЙ КОНТРОЛЬ + ОТКАТ ------------------------------------------------------
# 6a. Контроль стенда: прежняя редакция (старый batch_receipts + старое решение) НА ТОМ ЖЕ
# стенде обязана дать обращение к боевому. Иначе тест выше не доказывает ничего — он мог бы
# показывать «0 обращений» просто потому, что стенд не умеет их считать.


def _legacy_batch(url, hs, timeout=5):
    """Тело batch_receipts ДО правки, дословно: только пришедшие квитанции, отказ = {}."""
    out = {}
    try:
        s = ex._seq_session(url)
        if s is None:
            return out
        payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionReceipt",
                    "params": [h]} for i, h in enumerate(hs)]
        resp = s.post(url, json=payload, timeout=timeout).json()
        if isinstance(resp, dict):
            return out
        for item in resp:
            try:
                i = int(item.get("id"))
            except Exception:
                continue
            if 0 <= i < len(hs) and not item.get("error"):
                r = item.get("result")
                if r is not None:
                    out[hs[i]] = r
    except Exception:
        return out
    return out


def _legacy_receipts(hs, batch_fn):
    """Тело _receipts ДО правки, дословно."""
    out = batch_fn(DED, hs)
    if out:
        return out
    return batch_fn(COMBAT, hs)


POSTS.clear()
_s = _Sess({DED: mined(set()), COMBAT: mined({0, 1, 2, 3})})
ex._seq_session = lambda url: _s
try:
    _lo = _legacy_receipts(H, _legacy_batch)
finally:
    ex._seq_session = _o
assert len(combat_calls(POSTS)) == 1, \
    "КОНТРОЛЬ ПРОВАЛЕН: стенд не видит боевых обращений даже у прежней редакции — " \
    "значит «ноль обращений» в п.2 не доказывает ничего"
assert all(_lo.get(h) for h in H), \
    "прежняя редакция не добрала партию боевым — сценарий воспроизведён неверно: %r" % _lo
print("OK 6a/6: КОНТРОЛЬ — прежняя редакция на этом же стенде даёт 1 обращение к боевому "
      "на «ничего не намайнено» (регрессия ловится)")

# 6b. РУЧКА ОТКАТА снимает МЕХАНИЗМ: решение снова принимается по «ничего не резолвилось»,
# то есть боевой ключ снова дёргается на каждый пустой полл — поведение прежней редакции.
_saved = sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY
try:
    sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY = True
    out, posts, fired = run({DED: mined(set()), COMBAT: mined({0, 1, 2, 3})})
    assert len(combat_calls(posts)) == 1, "откат не вернул прежнее поведение: %s" % posts
    assert fired and "откат" in fired[0], fired
    # и при откате частичная партия тоже идёт как раньше — по боевому НЕ ходим
    out, posts, _ = run({DED: mined({0}), COMBAT: mined({0, 1, 2, 3})})
    assert combat_calls(posts) == [], "откат сломал нормальный путь"
finally:
    sfl.SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY = _saved
# и он действительно ЧИТАЕТСЯ из окружения, а не только правится в памяти
_src = pathlib.Path("chain/spec_fire_live.py").read_text()
assert 'os.environ.get(\n    "SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY"' in _src \
    or '"SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY"' in _src, "ручка отката не из окружения"
print("OK 6b/6: откат SPEC_FIRE_COLLECTOR_FALLBACK_ON_EMPTY=1 возвращает прежний МЕХАНИЗМ "
      "решения (а не подкручивает порог)")

# --- 7. СКВОЗЬ БОЕВОЙ МЕТОД: _receipts_fn + настоящий ReceiptCollector.tick --------------
# Здесь не пересказ ветки, а тот самый шов, который получает боевой коллектор.
import chain.fire_engine as fe                                # noqa: E402


class _Rpc:
    rpc_url = COMBAT


class _Stub:
    """Носитель боевых методов: сами методы — НАСТОЯЩИЕ (берутся из класса)."""
    _log = staticmethod(lambda m: LOGS.append(m))
    _now = staticmethod(lambda: NOW[0])
    rpc = _Rpc()
    stats: dict = {}
    _receipts_fn = sfl.SpecFireLive._receipts_fn
    _note_collector_fallback = sfl.SpecFireLive._note_collector_fallback


LOGS: list = []
NOW = [1000.0]

_saved_url = sfl.SPEC_FIRE_COLLECTOR_RPC_URL
try:
    sfl.SPEC_FIRE_COLLECTOR_RPC_URL = DED
    st = _Stub()
    fn = st._receipts_fn(lambda: COMBAT)

    # 7a. ничего не намайнено -> боевой не тронут, счётчик страховки НЕ растёт
    POSTS.clear()
    _s = _Sess({DED: mined(set()), COMBAT: mined({0, 1, 2, 3})})
    ex._seq_session = lambda url: _s
    got = fn(H)
    assert combat_calls(POSTS) == [], "боевой метод дёрнул боевой узел на «не намайнено»"
    assert st.stats.get("collector_fallback", 0) == 0, st.stats

    # 7b. узел лёг -> ровно один боевой запрос, счётчик +1, и это ВИДНО в журнале
    POSTS.clear()
    _s = _Sess({DED: OSError("timed out"), COMBAT: mined({0, 1, 2, 3})})
    ex._seq_session = lambda url: _s
    got = fn(H)
    assert len(combat_calls(POSTS)) == 1, POSTS
    assert st.stats["collector_fallback"] == 1, st.stats
    assert any("уведена на БОЕВОЙ" in m for m in LOGS), "срабатывание страховки молчит: %s" % LOGS

    # 7b-bis. ВО ВРЕМЕНИ, а не в моменте: у прореживания строки есть TTL, значит обязана быть
    # дрель «неделя штиля -> новый отказ». Защёлка, пережившая штиль, сделала бы отказ узла
    # немым ровно тогда, когда он снова начался.
    LOGS.clear()
    _s = _Sess({DED: OSError("timed out"), COMBAT: mined({0, 1, 2, 3})})
    ex._seq_session = lambda url: _s
    NOW[0] = 1001.0
    fn(H)
    assert not LOGS, "прореживание не работает: строка на каждый полл лежащего узла"
    NOW[0] = 1000.0 + 7 * 86400                               # неделя штиля
    fn(H)
    assert any("уведена на БОЕВОЙ" in m for m in LOGS), \
        "после штиля отказ снова НЕМОЙ — защёлка пережила своё окно"
    assert st.stats["collector_fallback"] == 3, st.stats
    LOGS.clear()

    # 7c. НАСТОЯЩИЙ ReceiptCollector с этим швом: ненамайненные не резолвятся и не таймаутят
    POSTS.clear()
    _s = _Sess({DED: mined({0}), COMBAT: mined({0, 1, 2, 3})})
    ex._seq_session = lambda url: _s
    done, tmo = [], []
    col = fe.ReceiptCollector(receipts_fn=fn, on_result=lambda m, h, r: done.append(h),
                              on_timeout=lambda m, h: tmo.append(h),
                              min_age_sec=0.0, timeout_sec=60.0)
    for h in H:
        col.track(h, ("leg", None))
    col.tick()
    assert done == [H[0]], "коллектор довёл не то: %s" % done
    assert tmo == [], "ненамайненная проба объявлена дропнутой"
    assert combat_calls(POSTS) == [], "боевой узел дёрнут из настоящего коллектора: %s" % POSTS
    assert col.pending() == 3, col.pending()
finally:
    ex._seq_session = _o
    sfl.SPEC_FIRE_COLLECTOR_RPC_URL = _saved_url
print("OK 7/6: боевой шов SpecFireLive._receipts_fn + настоящий ReceiptCollector.tick — "
      "0 обращений к боевому, страховка только на отказе и она громкая")

print("=== #101 ЗАКРЫТ: страховка боевым — только на РЕАЛЬНОМ отказе, батч под учётом CU ===")
