"""МАЙОР М2 (ревью р-6): латч «узел не умеет квитанции» запирал СВОЙ ЖЕ источник опровержения.

ЧТО БЫЛО. После K=3 ответов `-32601` способность гасилась (`_receipts_ok=False`), а оба чтения
квитанций стоят ЗА ЭТИМ ЖЕ ГАРДОМ: денежная ветка `scan_block` (`facts['ours'] and
self._receipts_ok`) и проба способности `_probe_receipts_cap` (`if not self._receipts_ok:
return`). Значит НИ успех, ни проба снять латч не могли — единственным выходом был таймер
6 часов. Три ответа ПУЛА (за одним адресом стоит несколько бэкендов) выключали главное приёмное
число пакета (`index_hits`/`polls_saved`) на шесть часов ГАРАНТИРОВАННО.

ЧТО ТЕПЕРЬ. Переспрос идёт ЛЕСЕНКОЙ 300с → 900с → 3600с → потолок 6ч, а успешный ответ СПИСКОМ
сбрасывает ступень. Асимметрия цены названа прямо: лишняя проба раз в 300с стоит ОДИН запрос,
лишние 6ч латча стоят ВСЕЙ приёмки (в).

НАСТОЯЩИЕ здесь: `_catch_up`, `_confirmed_head`, `_probe_receipts_cap`, `_rcpt_unlatch`,
`_note_unsupported`, `_note_receipts_ok`, `_rcpt_retry_sec`. Подменены два шва: сокет и часы.
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402

BN0 = 49890266
LOGGER = logging.getLogger("liquidator")


def drill(blip_until=10 ** 9, seconds=6 * 3600, step=30.0, ladder=None):
    """Узел отвечает -32601 до `blip_until` модельных секунд, дальше — СПИСКОМ.

    blip_until=+inf — узел, который метода правда не умеет (латч обязан жить и не жечь запросы).
    Возвращает (idx, история [(t, rcpt_probe, receipts_ok, ступень)], число вызовов метода).
    """
    T = [1000.0]
    calls = {"rcpt": 0}

    def _http(url, payload, timeout):
        m = payload["method"]
        el = T[0] - 1000.0
        if m == "eth_blockNumber":
            return {"result": hex(BN0 + int(el // 2))}
        if m == "eth_getLogs":
            return {"result": []}
        if m == "eth_getBlockReceipts":
            calls["rcpt"] += 1
            if el < blip_until:
                return {"error": {"code": -32601, "message": "rpc method is unsupported"}}
            return {"result": []}
        return {"result": None}

    idx = ci.ChainIndex("https://node.test/KEY", watch_to=["0x" + "ab" * 20], http=_http,
                        now=lambda: T[0], sleep=lambda s: None, log_fn=lambda m: None)
    hist = []
    _sv_lad = ci.CHAIN_INDEX_RCPT_UNSUP_LADDER
    _lvl = LOGGER.level
    LOGGER.setLevel(logging.CRITICAL)          # драйвер, а не журнал: строки проверены в др. тесте
    if ladder is not None:
        ci.CHAIN_INDEX_RCPT_UNSUP_LADDER = ladder
    try:
        for _ in range(int(seconds / step)):
            T[0] += step
            idx._catch_up(BN0 + int((T[0] - 1000.0) // 2) + 1)
            hist.append((T[0] - 1000.0, idx._rcpt_probe, idx._receipts_ok,
                         idx._rcpt_unsup_step))
    finally:
        ci.CHAIN_INDEX_RCPT_UNSUP_LADDER = _sv_lad
        LOGGER.setLevel(_lvl)
    return idx, hist, calls["rcpt"]


def t_of(hist, pred):
    for t, probe, ok, step in hist:
        if pred(probe, ok, step):
            return t
    return None


# ------------------------------------------------- 1. МОРГНУВШИЙ ПУЛ: способность возвращается
# Узел отвечает -32601 первые 20 минут (моргнул пул), дальше исправен. Прежняя редакция держала
# латч 6 часов, потому что снять его было нечем; лесенка обязана вернуть способность быстро.
idx, hist, calls = drill(blip_until=1200.0, seconds=6 * 3600)
t_latch = t_of(hist, lambda p, ok, s: ok is False)
t_back = t_of(hist, lambda p, ok, s: p == "поддержан")
assert t_latch is not None, "узел, отвечающий -32601, обязан гаситься — иначе дрель ловит не то"
assert t_back is not None, ("способность обязана ВЕРНУТЬСЯ: латч запирал собственный источник "
                            "опровержения, и это и есть майор М2. история: %s" % hist[-1:])
_dt = t_back - t_latch
assert _dt < ci.CHAIN_INDEX_RCPT_UNSUP_RETRY_SEC, \
    ("возврат обязан идти ПО ЛЕСЕНКЕ (300с), а не по потолку 6ч: %.0fс" % _dt)
assert idx._rcpt_unsup_step == 0, \
    ("успех обязан СБРАСЫВАТЬ ступень — иначе штраф за одну случайность живёт всю жизнь "
     "процесса: ступень=%d" % idx._rcpt_unsup_step)
assert idx._receipts_ok is True and idx._rcpt_probe == "поддержан", (idx._rcpt_probe,)
print("UNIT_OK: моргнувший пул (-32601 20 минут) — латч на %.0fс, способность вернулась через "
      "%.0fс после латча (потолок был 21600с), ступень сброшена успехом" % (t_latch, _dt))

# ------------------------------- 2. УЗЕЛ, КОТОРЫЙ ПРАВДА НЕ УМЕЕТ: лесенка растёт, запросы малы
# Обратная сторона: переспрашивать вечно каждые 300с — это 288 бесполезных запросов в сутки к
# узлу, который метода не умеет. Ступень обязана расти до потолка.
idx2, hist2, calls2 = drill(seconds=24 * 3600, step=60.0)
assert idx2._receipts_ok is False and idx2._rcpt_probe == "не поддержан", idx2._rcpt_probe
assert idx2._rcpt_unsup_step >= 3, ("лесенка обязана дойти до потолка: ступень=%d"
                                    % idx2._rcpt_unsup_step)
assert idx2._rcpt_retry_sec() == ci.CHAIN_INDEX_RCPT_UNSUP_RETRY_SEC, idx2._rcpt_retry_sec()
assert calls2 <= 40, ("узел, который метода не умеет, не имеет права стоить нам сотен запросов "
                      "в сутки: вызовов=%d" % calls2)
print("UNIT_OK: узел БЕЗ метода — латч жив, ступень дошла до потолка (%.0fс), за модельные "
      "сутки потрачено %d вызовов eth_getBlockReceipts" % (idx2._rcpt_retry_sec(), calls2))

# ------------------------------------------------- 3. МУТАЦИЯ: вернуть ОДИН срок 6ч (как в р-5)
_idx3, hist3, _c3 = drill(blip_until=1200.0, seconds=6 * 3600 - 60,
                          ladder=(ci.CHAIN_INDEX_RCPT_UNSUP_RETRY_SEC,))
assert t_of(hist3, lambda p, ok, s: p == "поддержан") is None, \
    ("мутация «единственный срок 6ч» ОБЯЗАНА краснить стенд: способность не имеет права "
     "вернуться внутри прогона — иначе тест не держит лесенку")
print("UNIT_OK: мутация (единственный срок 6ч, редакция р-5) краснит стенд — способность за "
      "6 часов прогона не возвращается ни разу")

print("ПРОШЛИ: латч квитанций проверяем ВО ВРЕМЕНИ — лесенкой переспроса, а не одним сроком, "
      "который сам себе запрещал доказательство противного")
