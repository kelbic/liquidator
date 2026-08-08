"""Путь огня со склада: НАСТОЯЩИЙ send_presigned_probe до транспортного шва.

Урок seam-stubbed-above-the-defect: подмена этажом выше уже прятала мёртвый боевой путь
(NameError в send_board_tx за 38 зелёными тестами). Здесь исполняется реальная функция,
шов — только _post_raw_sync (HTTP в секвенсер). Гарды денег проверяются в обе стороны:
это они держат бюджет, когда движок получает право огня от fat-окна.
"""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import chain.execute as ex                               # noqa: E402

ADDR = "0x" + "a" * 40
SEQ = ["http://sequencer.test"]


def seed(q=None):
    ex._VOLLEY_LOCKS[ADDR] = threading.Lock()
    ex._VOLLEY_LADDER[ADDR] = {"q": list(q or [(5, "0xdeadbeef", "0xhash5")]),
                               "gas": 18600, "max_fee": 10**7}
    ex._VOLLEY_BAL[ADDR] = [10**18, time.monotonic()]
    ex._VOLLEY_NONCE.pop(ADDR, None)


_saved = (ex._post_raw_sync, ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY)
try:
    ex._VOLLEY_DAY = int(time.time() // 86400)          # суточный ролл не мешает сценарию
    ex._VOLLEY_GAS_USD = 0.0
    ex._INFLIGHT = 0
    ex._post_raw_sync = lambda url, raw, timeout=5: (True, None)

    # 1. счастливый путь: запись потрачена, nonce ноги продвинут
    seed()
    h = ex.send_presigned_probe(None, None, ADDR, broadcast_raw_urls=SEQ)
    assert h == "0xhash5", h
    assert ex._VOLLEY_LADDER[ADDR]["q"] == [], "запись не потрачена"
    assert ex._VOLLEY_NONCE.get(ADDR) == 6, ex._VOLLEY_NONCE.get(ADDR)

    # 2. КОНТРОЛЬ: $-бакет тира выбран -> молчим, склад не тронут
    seed()
    ex._VOLLEY_GAS_USD = ex.CASCADE_TIER_GAS_USD
    assert ex.send_presigned_probe(None, None, ADDR, broadcast_raw_urls=SEQ) is None
    assert len(ex._VOLLEY_LADDER[ADDR]["q"]) == 1, "гейт закрыт, а запись потрачена"
    ex._VOLLEY_GAS_USD = 0.0

    # 3. боевая tx в полёте НЕ глушит пробу движка (решение kelbic 08.08: немота съедала
    #    75% горячего окна каскада; у 1-байт проб свои nonce и цена $0.0004)
    seed()
    ex._INFLIGHT = 1
    assert ex.send_presigned_probe(None, None, ADDR, broadcast_raw_urls=SEQ) == "0xhash5", \
        "проба движка обязана лететь при боевой tx в полёте (дефолт)"
    ex._INFLIGHT = 0

    # 3б. КОНТРОЛЬ ОТКАТА: SPEC_FIRE_ENGINE_IGNORE_INFLIGHT=0 -> прежнее «боевая главнее»
    seed()
    ex._INFLIGHT = 1
    _sv = ex._ENGINE_PROBE_IGNORE_INFLIGHT
    ex._ENGINE_PROBE_IGNORE_INFLIGHT = False
    try:
        assert ex.send_presigned_probe(None, None, ADDR, broadcast_raw_urls=SEQ) is None
        assert len(ex._VOLLEY_LADDER[ADDR]["q"]) == 1, "гейт отката закрыт, а запись потрачена"
    finally:
        ex._ENGINE_PROBE_IGNORE_INFLIGHT = _sv
    ex._INFLIGHT = 0

    # 3в. КОНТРОЛЬ границы: V1-нога залпа (send_volley_probe) ОСТАЁТСЯ под гейтом —
    #    освобождён только путь движка
    _src = pathlib.Path("chain/execute.py").read_text()
    _volley_fn = _src[_src.index("def send_volley_probe"):_src.index("def send_presigned_probe")]
    assert "_INFLIGHT > 0" in _volley_fn and "_ENGINE_PROBE_IGNORE_INFLIGHT" not in _volley_fn, \
        "гейт V1-ноги изменён — освобождать договаривались только движок"

    # 4. рассылка не удалась -> запись ВЕРНУЛАСЬ В НАЧАЛО (дыра в nonce повесила бы ногу)
    seed([(5, "0xaa", "0xh5"), (6, "0xbb", "0xh6")])
    ex._post_raw_sync = lambda url, raw, timeout=5: (False, "boom")
    assert ex.send_presigned_probe(None, None, ADDR, broadcast_raw_urls=SEQ) is None
    assert ex._VOLLEY_LADDER[ADDR]["q"][0] == (5, "0xaa", "0xh5"), \
        ex._VOLLEY_LADDER[ADDR]["q"]
    ex._post_raw_sync = lambda url, raw, timeout=5: (True, None)

    # 5. патрон ниже худшего случая (gas x max_fee + пол) -> нога молчит, склад цел
    seed()
    ex._VOLLEY_BAL[ADDR] = [ex._VOLLEY_BAL_FLOOR_WEI, time.monotonic()]
    assert ex.send_presigned_probe(None, None, ADDR, broadcast_raw_urls=SEQ) is None
    assert len(ex._VOLLEY_LADDER[ADDR]["q"]) == 1

    # 6. пул секвенсер-сессии держит пик каскада: 8 тредов движка + 3 боевых + канарейка > 10
    #    дефолтных коннектов urllib3 — переполнёж клал бы боевой броадкаст на холодный TCP+TLS
    if ex._requests is not None:
        _u = "http://pool-size.test"
        _pm = ex._seq_session(_u).get_adapter(_u)._pool_maxsize
        ex._SEQ_SESSIONS.pop(_u, None)
        assert _pm >= 16, "pool_maxsize=%s — пик каскада переполняет пул" % _pm
finally:
    (ex._post_raw_sync, ex._VOLLEY_GAS_USD, ex._INFLIGHT, ex._VOLLEY_DAY) = _saved
    for d in (ex._VOLLEY_LOCKS, ex._VOLLEY_LADDER, ex._VOLLEY_BAL, ex._VOLLEY_NONCE):
        d.pop(ADDR, None)

print("OK: огонь со склада исполняется до транспорта, денежные гарды держат в обе стороны")
