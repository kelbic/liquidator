"""Огонь движка ВИДЕН, коллектор не штормит, потеря флота ГРОМКАЯ (#56/#66/#48).

Ревью 09.08: (1) пробы движка не растили ни одного счётчика — согласованный критерий приёмки
«board_probes>0 при включённом движке» был недостижим ПО ПОСТРОЕНИЮ; (2) коллектор тянул до
200 квитанций в секунду по боевому ключу (~3000 CU/с против потолка 500) и этот расход был
невидим счётчику — то есть первое же настоящее окно плотности само себе устраивало 429 ровно
тогда, когда квитанции побед и нужны; (3) пропавший keyfile давал ТИХИЙ скип: флот исчезает
целиком, движок выглядит живым, в журнале пусто.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.fire_engine as fe                            # noqa: E402

# --- 1. #66: квитанции моложе блока НЕ опрашиваются (они не могут существовать) ----------
asked = []


def _receipts(hs):
    asked.append(list(hs))
    return {}


now = [1000.0]
col = fe.ReceiptCollector(receipts_fn=_receipts, on_result=lambda *a: None,
                          now=lambda: now[0])
col.track("0xh1", ("leg", None))
assert col.tick() == 0 and asked == [], \
    "свежая проба опрошена сразу — это CU впустую, квитанции у неё быть не может"
now[0] += fe.COLLECT_MIN_AGE_SEC + 0.1
col.tick()
assert asked and asked[-1] == ["0xh1"], "созревшая проба не опрошена: %s" % asked

# --- 2. #66: расход коллектора СЧИТАЕТСЯ (иначе он невидим сторожу квоты) ---------------
assert col.stats["cu"] == fe.CU_PER_RECEIPT, col.stats
assert fe.COLLECT_BATCH <= 100, "батч %d = %.0f CU за полл, это шторм" % (
    fe.COLLECT_BATCH, fe.COLLECT_BATCH * fe.CU_PER_RECEIPT)
assert fe.COLLECT_BATCH * fe.CU_PER_RECEIPT / max(0.1, fe.COLLECT_POLL_SEC) <= 1500, \
    "потолок расхода коллектора выше 1500 CU/с"

# --- 3. КОНТРОЛЬ: таймаут по-прежнему выносит приговор (min-age не съел его) -------------
fired = []
col2 = fe.ReceiptCollector(receipts_fn=lambda hs: {}, on_result=lambda *a: None,
                           on_timeout=lambda meta, h: fired.append(h), now=lambda: now[0],
                           timeout_sec=10.0)
assert col2._min_age == fe.COLLECT_MIN_AGE_SEC, "боевой дефолт зрелости не применяется"
col2.track("0xh2", ("leg", None))
now[0] += 11.0
col2.tick()
assert fired == ["0xh2"], "таймаут перестал срабатывать — дроп не будет распознан"

# --- 4. #56: отправка движка растит board_probes (то, что ищет приёмка) -----------------
src = pathlib.Path("chain/spec_fire_live.py").read_text()
_snd = src[src.index("            def _send(addr):"):]
_snd = _snd[:_snd.index("def _refill")]
assert 'self.stats["board_probes"]' in _snd and 'self.stats["engine_probes"]' in _snd, \
    "огонь движка снова не считается — критерий приёмки board_probes>0 недостижим"
assert "if h is not None:" in _snd, "считаем даже сухие вызовы — счётчик врал бы"
# close-строка обязана печатать счётчики движка и коллектора
assert "engine=%s collector=%s" in src, "статистика движка/коллектора не попадает в журнал"

# --- 5. #48: пропавший или пустой keyfile — ГРОМКО --------------------------------------
_ex = pathlib.Path("chain/execute.py").read_text()
_kf = _ex[_ex.index('kf = os.environ.get("SPEC_FIRE_EOA_KEYFILE"'):]
_kf = _kf[:_kf.index("return accs") if "return accs" in _kf else 2000]
assert "НЕ НАЙДЕН" in _kf and "log.error" in _kf, \
    "пропавший keyfile снова тихий — флот исчезает целиком без строки в журнале"
assert "ПУСТ или не дал ни одного ключа" in _kf, "пустой keyfile тоже обязан кричать"

print("OK: огонь движка виден, коллектор не штормит, потеря флота громкая")
