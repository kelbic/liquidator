"""ДРЕЛЬ (требование kelbic 09.08): старт окна ПОСЛЕ НЕДЕЛИ ШТИЛЯ.

Регрессия #45 родилась из слепого пятна приёмки: тесты экзаменовали гейт В МОМЕНТЕ, а не
гейт × ВРЕМЯ. Реальная последовательность в бою другая: неделя штиля (склад полон, никто
ничего не шлёт) -> приходит fat-окно -> все 110 ног обязаны выстрелить. При пер-нoговом
прогреве, стоявшем только при СОЗДАНИИ лестницы, здесь замолчали бы все ноги разом, а каждая
немая ещё и будила бы дозор — 110 тредов в боевой ключ (наш класс 429-шторма).

Критерий приёмки владельца: ВСЕ ноги дают первую пробу без дозорного шторма.
Исполняются НАСТОЯЩИЕ FireEngine._refill_loop, volley_bal_warm_fleet и send_presigned_probe;
швы — только транспорт (мультиколл и рассылка).
"""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                               # noqa: E402
import chain.fire_engine as fe                           # noqa: E402

LEGS = ["0x" + ("%040x" % (0xa0000 + i)) for i in range(110)]
GAS = 3_000_000
FUNDED = int(0.008 * 10**18)
WEEK = 7 * 24 * 3600


class _Rpc:
    """Шов транспорта: мультиколл балансов флота + счётчик ОДИНОЧНЫХ чтений (шторм = провал)."""

    def __init__(self):
        self.multicalls = 0
        self.single_reads = 0

    def _web3(self):
        outer = self

        class _W3:
            class eth:
                @staticmethod
                def get_balance(a):
                    outer.single_reads += 1
                    return FUNDED
        return _W3



def _fake_aggregate3(rpc, calls, chunk=500, block_identifier=None):
    """Заглушка ТРАНСПОРТА, повторяющая РАСПАКОВКУ настоящего aggregate3.

    Урок 09.08 (повтор канона seam-stubbed-above-the-defect): прежняя заглушка принимала ЛЮБЫЕ
    кортежи, и мой дефект — volley_bal_warm_fleet кормил тремя элементами при ожидаемых двух —
    жил за зелёным тестом, а в бою прогрев падал ValueError на КАЖДОМ вызове и весь флот немел
    через MAX_AGE. web3 на этой машине не установлен, исполнить настоящую функцию нельзя,
    поэтому: (1) распаковываем ровно как она, (2) ниже стоит структурная привязка к её коду.
    """
    rpc.multicalls += 1
    out = []
    for (t, cd) in calls:                      # ТА ЖЕ форма, что в chain/multicall.py
        assert isinstance(cd, (bytes, bytearray)) and len(cd) == 36, \
            "calldata getEthBalance должна быть 4+32 байта, а не %r" % (len(cd) if cd else None)
        out.append((True, FUNDED.to_bytes(32, "big")))
    return out


def seed_fleet(age_sec):
    """Флот после `age_sec` штиля: склады ПОЛНЫЕ (никто не стрелял), патрон соответственно старый."""
    now = time.monotonic()
    for a in LEGS:
        ex._VOLLEY_LOCKS[a] = threading.Lock()
        ex._VOLLEY_LADDER[a] = {"q": [(i, "0xdead", "0xh%s%d" % (a[-4:], i)) for i in range(40)],
                                "gas": GAS, "max_fee": 147 * 10**6}   # 0.147 gwei при мультипл. 20
        ex._VOLLEY_BAL[a] = [FUNDED, now - age_sec]
        ex._VOLLEY_NONCE.pop(a, None)


_saved = (ex._post_raw_sync, ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY,
          ex._VOLLEY_FLOOR_SKIPS, ex._VOLLEY_UNKNOWN_SKIPS)
_mc_saved = None
try:
    import chain.multicall as mc
    _mc_saved = mc.aggregate3
    mc.aggregate3 = _fake_aggregate3
    ex._VOLLEY_DAY = int(time.time() // 86400)
    ex._VOLLEY_GAS_USD = 0.0
    ex._INFLIGHT = 0
    ex._post_raw_sync = lambda url, raw, timeout=5: (True, None)

    # --- 1. КОНТРОЛЬ ДЕФЕКТА: без прогрева неделя штиля = флот НЕМ ЦЕЛИКОМ ---------------
    # Без этого пункта успех п.2 неотличим от «гейт вообще не смотрит на возраст».
    seed_fleet(WEEK)
    rpc0 = _Rpc()
    ex._VOLLEY_UNKNOWN_SKIPS = 0
    _fired0 = sum(1 for a in LEGS[:20]
                  if ex.send_presigned_probe(rpc0, None, a, broadcast_raw_urls=["http://s"]))
    assert _fired0 == 0, "патрон недельной давности принят за истину: выстрелов %d" % _fired0
    assert ex.volley_probe_skips()[1] == 20, ex.volley_probe_skips()
    time.sleep(0.3)                                        # дать дозорам осесть
    with ex._INFLIGHT_LOCK:
        ex._VOLLEY_BAL_REFRESHING.clear()

    # --- 2. ДРЕЛЬ: фоновый прогрев движка обязан ОЖИВИТЬ весь флот до огня ---------------
    seed_fleet(WEEK)
    rpc = _Rpc()
    _sv_warm = fe.ENGINE_WARM_SEC
    fe.ENGINE_WARM_SEC = 0.05                              # ускоряем каденс, логика та же
    eng = fe.FireEngine(legs_fn=lambda: list(LEGS), send_fn=lambda a: None,
                        collector=None, threads=1, rate=10.0, ttl=0.05,
                        refill_fn=lambda a: None, depth_fn=lambda a: 40,
                        fleet_warm_fn=lambda addrs: ex.volley_bal_warm_fleet(rpc, addrs))
    eng.start()
    _dl = time.time() + 5.0
    while time.time() < _dl:
        if all(not ex.volley_bal_stale(a) for a in LEGS):
            break
        time.sleep(0.05)
    eng.stop()
    fe.ENGINE_WARM_SEC = _sv_warm

    _stale_left = [a for a in LEGS if ex.volley_bal_stale(a)]
    assert not _stale_left, "после прогрева %d ног всё ещё протухшие" % len(_stale_left)

    # ВСЕ 110 ног дают первую пробу
    ex._VOLLEY_UNKNOWN_SKIPS = ex._VOLLEY_FLOOR_SKIPS = 0
    fired = sum(1 for a in LEGS
                if ex.send_presigned_probe(rpc, None, a, broadcast_raw_urls=["http://s"]))
    assert fired == len(LEGS), "на старте окна выстрелили %d из %d ног" % (fired, len(LEGS))
    assert ex.volley_probe_skips() == (0, 0), ex.volley_probe_skips()

    # --- 3. БЕЗ ШТОРМА: флот освежается мультиколлом, а не 110 одиночными чтениями -------
    assert rpc.single_reads == 0, \
        "дозор ушёл в одиночные чтения: %d запросов в боевой ключ" % rpc.single_reads
    assert rpc.multicalls >= 1, "флотский прогрев не случился вовсе"
    assert rpc.multicalls <= 120, "прогрев штормит: %d мультиколлов" % rpc.multicalls

    # --- 4. КВОТА: каденс прогрева обязан быть редким (боевая величина, не тестовая) -----
    assert fe.ENGINE_WARM_SEC >= 30.0, \
        "боевой каденс прогрева %.1fс — это десятки миллионов CU в месяц" % fe.ENGINE_WARM_SEC
    # 110 ног одним вызовом раз в минуту = 1440 запросов/сутки против 110*2*2880 без батча
    assert ex.volley_bal_warm_fleet(rpc, LEGS) == len(LEGS), "мультиколл не покрыл весь флот"
finally:
    if _mc_saved is not None:
        import chain.multicall as mc
        mc.aggregate3 = _mc_saved
    (ex._post_raw_sync, ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY,
     ex._VOLLEY_FLOOR_SKIPS, ex._VOLLEY_UNKNOWN_SKIPS) = _saved
    for a in LEGS:
        for d in (ex._VOLLEY_LOCKS, ex._VOLLEY_LADDER, ex._VOLLEY_BAL, ex._VOLLEY_NONCE):
            d.pop(a, None)
    with ex._INFLIGHT_LOCK:
        ex._VOLLEY_BAL_REFRESHING.clear()
        ex._VOLLEY_MAXFEE_MUTE.clear()

_mc_src = pathlib.Path("chain/multicall.py").read_text()
_agg = _mc_src[_mc_src.index("def aggregate3"):_mc_src.index("def batch_positions")]
assert "for (t, cd) in calls[" in _agg, \
    "aggregate3 сменил форму вызова — заглушка теста больше не повторяет настоящую распаковку"
assert "(Web3.to_checksum_address(t), True, cd)" in _agg, \
    "aggregate3 больше не ставит allowFailure сам — проверить volley_bal_warm_fleet"

print("OK: после недели штиля все 110 ног стреляют, прогрев — один мультиколл, шторма нет")
