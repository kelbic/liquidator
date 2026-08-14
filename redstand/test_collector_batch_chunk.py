"""#101-b: НАРЕЗКА ПАРТИИ ПОД ВЫДЕЛЕННЫЙ УЗЕЛ.

Замер 12.08 прямо на боевой машине: `lb.drpc.live` принимает JSON-RPC-массив длиной 1-3 и
отдаёт HTTP 500 начиная с ЧЕТЫРЁХ (граница проверена повтором: n=3 принят дважды, n=4 отказ).
Наш COLLECT_BATCH=64 ⇒ увод #99 отказывал на 100% поллов с самого деплоя и молча уходил на
платный Alchemy: collector_fallback РАВЕН polls в каждом из 134 окон журнала.

Тут исполняются НАСТОЯЩИЕ chain.execute.batch_receipts и chain.spec_fire_live.collect_receipts.
Подменён только сетевой стык `_seq_session` — этаж НИЖЕ дефекта.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                                    # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

DED = "https://lb.drpc.live/ogrpc?network=base&dkey=XXX"
COMBAT = "https://base-mainnet.g.alchemy.com/v2/YYY"
MINED = "0x" + "ab" * 32


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _Node:
    """Узел с ПРЕДЕЛОМ длины батча, как у dRPC: больше max_batch -> HTTP 500 (исключение)."""

    def __init__(self, max_batch, mined=()):
        self.max_batch = max_batch
        self.mined = set(mined)
        self.posts = []                # длины принятых партий
        self.rejected = 0

    def post(self, url, json=None, timeout=None):
        n = len(json or [])
        if n > self.max_batch:
            self.rejected += 1
            raise OSError("HTTP Error 500: Internal Server Error")
        self.posts.append(n)
        out = []
        for item in json:
            h = item["params"][0]
            out.append({"id": item["id"], "jsonrpc": "2.0",
                        "result": ({"status": "0x1"} if h in self.mined else None)})
        return _Resp(out)


def _install(nodes):
    """nodes: {url: _Node}. Возвращает откат."""
    real = ex._seq_session
    ex._seq_session = lambda url: nodes.get(url)
    return lambda: setattr(ex, "_seq_session", real)


HASHES = ["0x%064x" % i for i in range(64)]

# --- 1. НАСТОЯЩАЯ batch_receipts режет партию и собирает результат целиком ---------------
ded = _Node(max_batch=3, mined={HASHES[7]})
undo = _install({DED: ded})
try:
    out = ex.batch_receipts(DED, HASHES, chunk=3)
finally:
    undo()
assert len(out) == 64, "нарезка потеряла хэши: %d из 64" % len(out)
assert set(out) == set(HASHES), "нарезка подменила ключи"
assert out[HASHES[7]] == {"status": "0x1"}, "намайненная квитанция не доехала"
assert out[HASHES[0]] is None, "ненамайненный обязан приезжать ключом со значением None"
assert ded.posts == [3] * 21 + [1], "куски не по 3: %s" % ded.posts[:5]
assert ded.rejected == 0, "узел отказал при включённой нарезке"
print("OK 1/5: партия 64 нарезана на 22 поста (3x21+1), все 64 ключа на месте, отказов 0")

# --- 2. ПОЗИТИВНЫЙ КОНТРОЛЬ: без нарезки тот же узел отказывает -------------------------
ded2 = _Node(max_batch=3)
undo = _install({DED: ded2})
try:
    _raised = None
    try:
        ex.batch_receipts(DED, HASHES, chunk=0)          # прежнее поведение
    except ex.ReceiptsUnavailable as e:
        _raised = e
finally:
    undo()
assert _raised is not None, "БЕЗ нарезки узел обязан отказать — иначе тест ничего не доказывает"
assert "lb.drpc.live" in str(_raised), "в отказе должен быть ХОСТ: %s" % _raised
assert "XXX" not in str(_raised) and "dkey" not in str(_raised), "КЛЮЧ УТЁК В ТЕКСТ ОТКАЗА: %s" % _raised
assert ded2.posts == [] and ded2.rejected == 1
print("OK 2/5: контроль — без нарезки тот же узел отказывает, в тексте только хост, ключа нет")

# --- 3. БОЕВОЙ ШОВ: страховка боевым НЕ трогается, когда нарезка спасла партию ----------
ded3, comb3 = _Node(max_batch=3, mined={HASHES[1]}), _Node(max_batch=10_000)
undo = _install({DED: ded3, COMBAT: comb3})
_fb = []
try:
    got = sfl.collect_receipts(HASHES, DED, COMBAT, ex.batch_receipts,
                               on_fallback=_fb.append, dedicated_chunk=3)
finally:
    undo()
assert len(got) == 64 and got[HASHES[1]] == {"status": "0x1"}
assert comb3.posts == [], "боевой ключ ТРОНУТ, хотя выделенный справился: %s" % comb3.posts
assert _fb == [], "страховка сработала зря: %s" % _fb
print("OK 3/5: боевой шов — выделенный узел справился нарезкой, Alchemy не тронут ни разу")

# --- 4. Настоящий отказ выделенного узла по-прежнему уводит на боевой, ГРОМКО -----------
ded4, comb4 = _Node(max_batch=0), _Node(max_batch=10_000, mined={HASHES[2]})
undo = _install({DED: ded4, COMBAT: comb4})
_fb = []
try:
    got = sfl.collect_receipts(HASHES[:3], DED, COMBAT, ex.batch_receipts,
                               on_fallback=_fb.append, dedicated_chunk=3)
finally:
    undo()
assert got[HASHES[2]] == {"status": "0x1"}, "страховка не отдала квитанцию"
assert len(comb4.posts) == 1, "боевой обязан быть спрошен РОВНО раз: %s" % comb4.posts
assert len(_fb) == 1 and "отказ узла" in _fb[0], "срабатывание страховки обязано быть громким: %s" % _fb
print("OK 4/5: настоящий отказ узла -> ровно одно обращение к боевому, с названной причиной")

# --- 5. ОТКАТ МЕХАНИЗМА: chunk=0 возвращает прежний путь бит-в-бит ----------------------
ded5, comb5 = _Node(max_batch=3), _Node(max_batch=10_000)
undo = _install({DED: ded5, COMBAT: comb5})
_fb = []
try:
    sfl.collect_receipts(HASHES, DED, COMBAT, ex.batch_receipts,
                         on_fallback=_fb.append, dedicated_chunk=0)
finally:
    undo()
assert len(comb5.posts) == 1, "с выключенной нарезкой партия обязана уйти на боевой (прежний дефект)"
assert ded5.rejected == 1 and ded5.posts == []
assert sfl.SPEC_FIRE_COLLECTOR_BATCH_CHUNK == 3, "дефолт нарезки должен быть 3 (замер dRPC)"
print("OK 5/5: откат — SPEC_FIRE_COLLECTOR_BATCH_CHUNK=0 возвращает ПРЕЖНИЙ путь целиком")

print("=== #101-b ЗАКРЫТ: партия режется под предел узла, боевой ключ только на реальном отказе ===")
