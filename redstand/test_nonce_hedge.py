"""#67: чтение nonce — ТЁПЛАЯ СЕССИЯ. Хедж откачен боевым инцидентом.

ЗАМЕР 11.08 (разбор живой гонки 14:51 и трассы окна): боевая латентность ready->провод
2.129с, из них ~2.093с (98%) съел ОДИН вызов `_pending_nonce`. Alchemy отдал 429, а прежняя
редакция строила НОВЫЙ Web3-провайдер прямо в этой точке — свежее TCP+TLS рукопожатие в самом
горячем месте. Комментарий рядом честно признавал «тёплую сессию не держим».

Цена вопроса названа гонкой: мы отстали на ДВА блока от победителя, приз $15 594 погашения
при net >= $207.

Хедж, а не «сначала основная, потом запасная»: последовательная схема платит ПОЛНЫЙ таймаут
основной ноды перед началом второй попытки — так и набежали две секунды.
"""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                                    # noqa: E402


class Eth:
    def __init__(self, val, delay=0.0, boom=False):
        self.val, self.delay, self.boom, self.calls = val, delay, boom, 0

    def get_transaction_count(self, a, blk=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.boom:
            raise RuntimeError("429 Too Many Requests")
        return self.val


class W3:
    def __init__(self, eth):
        self.eth = eth


def with_fb(fb_eth):
    """Подменить ЗАПАСНУЮ ногу, не трогая сеть."""
    ex._NONCE_FB_W3 = W3(fb_eth)


_saved_fb = ex._NONCE_FB_W3
try:
    # --- 1. Быстрая основная: запасную НЕ дёргаем вовсе -------------------------------
    fb = Eth(999)
    with_fb(fb)
    main = Eth(4242, delay=0.02)
    t0 = time.monotonic()
    n = ex._pending_nonce(W3(main), "0xabc")
    dt = time.monotonic() - t0
    assert n == 4242, n
    assert fb.calls == 0, "запасная нода дёрнута зря — лишний запрос в горячей точке"
    assert dt < 0.2, "быстрый путь занял %.3fс" % dt
    print("OK 1/5: основная ответила за %.0fмс, запасная не тронута" % (dt * 1000))

    # --- 2. ГЛАВНОЕ (после отката хеджа): НИ ОДНОГО ТРЕДА на вызов -------------------
    # ИНЦИДЕНТ 11.08 15:41: первая редакция пускала обе ноги параллельно, по треду на каждую.
    # Функция на горячем пути, при зависшей ноде тред живёт до её таймаута — треды пробили
    # cgroup-кап юнита: «can't start new thread» шесть раз и падение hot_loop. Скорость,
    # купленная тредами на горячем пути, стоит дороже, чем экономит: отказ пришёлся бы на
    # каскад, то есть ровно на деньги.
    fb = Eth(777, delay=0.01)
    with_fb(fb)
    n_before = threading.active_count()
    for _ in range(50):
        ex._pending_nonce(W3(Eth(0, boom=True)), "0xabc")
    assert threading.active_count() <= n_before, \
        "чтение nonce плодит треды (%d -> %d) — так мы уронили hot_loop 11.08" % (
            n_before, threading.active_count())
    print("OK 2/5: 50 вызовов при мёртвой основной -> НИ ОДНОГО нового треда")

    # --- 3. Основная ПАДАЕТ (429) -> запасная подхватывает ----------------------------
    fb = Eth(555, delay=0.02)
    with_fb(fb)
    main = Eth(0, boom=True)
    t0 = time.monotonic()
    n = ex._pending_nonce(W3(main), "0xabc")
    assert n == 555, n
    assert (time.monotonic() - t0) < 1.0
    print("OK 3/5: 429 на основной -> ответ запасной, без пересборки провайдера")

    # --- 4. ТЁПЛАЯ СЕССИЯ: клиент запасной строится ОДИН раз --------------------------
    ex._NONCE_FB_W3 = None
    made = []
    _real = ex._nonce_fb_w3

    def _counting():
        if ex._NONCE_FB_W3 is None:
            made.append(1)
            ex._NONCE_FB_W3 = W3(Eth(321, delay=0.01))
        return ex._NONCE_FB_W3
    ex._nonce_fb_w3 = _counting
    try:
        for _ in range(5):
            ex._pending_nonce(W3(Eth(0, boom=True)), "0xabc")
        assert len(made) == 1, "клиент запасной строится каждый раз (%d) — это TLS в горячей точке" % len(made)
    finally:
        ex._nonce_fb_w3 = _real
        ex._NONCE_FB_W3 = None
    print("OK 4/5: клиент запасной ноды строится ОДИН раз на процесс (тёплая сессия)")

    # --- 5. КОНТРОЛЬ: обе молчат -> исключение НАВЕРХ, а не выдуманный nonce ----------
    # Выдумывать nonce на денежном пути нельзя: дыра вешает кошелёк.
    ex._NONCE_FB_W3 = W3(Eth(0, boom=True))
    try:
        ex._pending_nonce(W3(Eth(0, boom=True)), "0xabc")
        raise AssertionError("обе ноги мертвы, а функция вернула значение — это выдуманный nonce")
    except AssertionError:
        raise
    except Exception:
        pass
    print("OK 5/5: КОНТРОЛЬ — обе ноги мертвы -> исключение наверх, nonce не выдумывается")
finally:
    ex._NONCE_FB_W3 = _saved_fb

# --- СТРУКТУРНО: провайдер больше не строится внутри чтения -------------------------
src = pathlib.Path("chain/execute.py").read_text()
i = src.index("def _pending_nonce(")
blk = src[i:src.index("\ndef ", i + 10)]
assert "Web3(Web3.HTTPProvider" not in blk, \
    "провайдер снова строится ВНУТРИ чтения nonce — это TLS-рукопожатие в горячей точке"
print("СТРУКТУРНО: сборки провайдера внутри чтения nonce больше нет")
print("=== #67 ЗАКРЫТ: 2.09с -> десятки мс на самом горячем вызове ===")
