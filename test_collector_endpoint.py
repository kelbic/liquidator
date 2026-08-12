"""Коллектор квитанций — на отдельный узел, но СО СТРАХОВКОЙ боевым (#99).

Зачем уводить: замер 10.08 — за одно окно с огнём коллектор сжёг 78 450 CU, а проекции монитора
скакали до 84M/мес при лимите 30M. Коллектор не в горячем пути (пробы уходят прямо в
секвенсер), поэтому он — естественный кандидат.

КУДА уводить — поправка по факту конфига: боевой `RPC_URL` это ALCHEMY (израсходовано 11M из
30), а оба bg-ключа (`BG_RPC_URL`, `BG_RPC_URL_2`) — dRPC, и они БЕСПЛАТНЫ (решение kelbic
09.08). Второго ключа Alchemy в конфиге НЕТ вовсе. Значит адресат — bg-страйп: увод снимает
нагрузку с платного лимита целиком и НЕ требует нового секрета. Первая редакция этого файла
исходила из «пропишем второй Alchemy» — премисса была ложной.

Почему НЕ безусловно: с #93 гейт огня решает по «ФАКТ + БРОНЬ», а бронь снимается КВИТАНЦИЕЙ.
Если выделенный узел молчит, брони копятся и гейт закрывается НА НАС ЖЕ — то есть увод на
худший узел ударил бы по готовности, хотя коллектор и не в горячем пути. Отсюда страховка.

ПРАВКА 12.08 (#101). Прежняя редакция этого файла утверждала: «batch_receipts на отказе
возвращает ПУСТОЙ словарь, а непришедшая квитанция приезжает как {hash: None} — словарь не
пуст». Утверждение было ЛОЖНЫМ: вставка шла под `if r is not None`, ключа со значением None не
появлялось никогда, и «не намайнено» было неотличимо от «узел лёг». Раздел 3 этого файла
проверял СВОЮ КОПИЮ боевой ветки («повторяет боевую ветку один-в-один») — копия была верна,
боевая ветка нет: классическая заглушка этажом выше дефекта. Решение теперь живёт в
`collect_receipts`, и экзаменуется ОНО САМО в test_collector_no_fallback.py; здесь остаётся
адресат узла и ПРОВОДКА (что боевой коллектор получает именно этот шов).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

SRC = pathlib.Path("chain/spec_fire_live.py").read_text()

# --- 1. Ручка есть и по умолчанию ИНЕРТНА (прежнее поведение) ----------------------------
import chain.execute as ex                                    # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

assert hasattr(sfl, "SPEC_FIRE_COLLECTOR_RPC_URL"), "ручки узла коллектора нет"
# Приоритет: своя ручка -> BG_RPC_URL_2 -> BG_RPC_URL -> боевой. В окружении теста ни одна не
# задана, поэтому остаёмся на боевом. Адресат выбран НЕ «второй ключ Alchemy» (его в конфиге
# нет вовсе): боевой RPC_URL — Alchemy с лимитом 30M, а оба bg-ключа dRPC и бесплатны, значит
# увод на bg снимает нагрузку с платного лимита целиком и нового секрета не требует.
assert sfl.SPEC_FIRE_COLLECTOR_RPC_URL == "", \
    "без единой ручки коллектор обязан остаться на боевом"
assert sfl._collector_node_kind() == "боевой"
_src_ch = SRC[SRC.index("SPEC_FIRE_COLLECTOR_RPC_URL = ("):]
_src_ch = _src_ch[:_src_ch.index("\n\n")]
assert "BG_RPC_URL_2" in _src_ch and "BG_RPC_URL" in _src_ch, \
    "цепочка приоритета не берёт bg-страйп — увод потребует ручной правки .env"
print("OK 1/4: цепочка узлов своя->bg-2->bg->боевой; без ручек остаёмся на боевом")

# --- 2. ПРОВОДКА: боевой коллектор получает шов _receipts_fn, а тот зовёт collect_receipts -
# Проверяем не наличие текста «страховка», а что решение принимает ОДНА функция, и что она же
# стоит в конструкторе ReceiptCollector. Иначе повторится #101: тест экзаменует копию ветки.
i = SRC.index("col = _fe.ReceiptCollector(")
blk = SRC[i:SRC.index(")", SRC.index("log=self._log", i))]
assert "receipts_fn=self._receipts_fn(" in blk, \
    "коллектор получает не боевой шов — проводка разорвана: %s" % blk[:200]
j = SRC.index("def _receipts_fn(self, combat_url):")
fblk = SRC[j:SRC.index("\n    # ---- движок огня", j)]
assert "collect_receipts(" in fblk and "batch_receipts" in fblk, \
    "шов не делегирует решение collect_receipts"
assert "if out" not in fblk, "в шве завелась своя логика решения — решение обязано быть одно"
dblk = SRC[SRC.index("def collect_receipts("):SRC.index("SPEC_FIRE_FACTS_TTL_SEC = ")]
assert "ReceiptsUnavailable" in dblk, \
    "страховка снова решает по ПУСТОТЕ словаря, а не по отказу узла — это и был #101"
assert dblk.count("batch_fn(combat_url") >= 2, \
    "боевой узел не используется как страховка — брони не снимутся при молчании выделенного"
assert "on_fallback" in dblk, "срабатывание страховки не считается (диагностики не будет)"
print("OK 2/4: ReceiptCollector <- _receipts_fn <- collect_receipts; решение одно и по ОТКАЗУ")

# --- 3. ЛОГИКА страховки на НАСТОЯЩЕЙ функции (не на пересказе) --------------------------
DED, COMBAT = "https://ded.example/K", "https://combat.example/K"
H = "0x" + "11" * 32
calls = []


def batch(url, hs, timeout=5):
    """Шов НИЖЕ дефекта: сам batch_receipts экзаменуется в test_collector_no_fallback.py,
    здесь проверяется решение о страховке при каждом из трёх исходов контракта."""
    calls.append(url)
    if url == DED:
        return DEDICATED() if callable(DEDICATED) else dict(DEDICATED)
    return {H: {"status": "0x1"}}


# 3a. выделенный ответил -> боевой НЕ трогаем (нагрузка ушла с боевого ключа)
calls.clear()
DEDICATED = {H: {"status": "0x1"}}
fired = []
assert sfl.collect_receipts([H], DED, COMBAT, batch, on_fallback=fired.append) == DEDICATED
assert calls == [DED], "боевой ключ дёрнут зря — увод не состоялся: %s" % calls
assert not fired, "страховка засчитана на здоровом ответе"

# 3b. ЕЩЁ НЕ НАМАЙНЕНО ({hash: None}) — это НЕ отказ, страховку НЕ зовём
calls.clear()
DEDICATED = {H: None}
assert sfl.collect_receipts([H], DED, COMBAT, batch, on_fallback=fired.append) == {H: None}, \
    "«не намайнено» подменено ответом боевого"
assert calls == [DED], "страховка сработала на нормальном ответе — лишний расход боевого: %s" % calls
assert not fired

# 3c. ОТКАЗ УЗЛА (ReceiptsUnavailable) -> страховка боевым


def DEDICATED():
    raise ex.ReceiptsUnavailable("транспорт ConnectionError", "ded.example")


calls.clear()
assert sfl.collect_receipts([H], DED, COMBAT, batch,
                            on_fallback=fired.append) == {H: {"status": "0x1"}}, \
    "выделенный молчит, а квитанции не добраны боевым"
assert calls == [DED, COMBAT], calls
assert len(fired) == 1 and "ded.example" in fired[0], "срабатывание страховки не названо: %s" % fired
print("OK 3/4: ответ -> только выделенный; «не намайнено» -> без страховки; ОТКАЗ -> боевой")

# --- 4. КОНТРОЛЬ: ручка пуста -> всё как раньше, выделенный не дёргается -----------------
calls.clear()
DEDICATED = {}
assert sfl.collect_receipts([H], "", COMBAT, batch) == {H: {"status": "0x1"}}
assert calls == [COMBAT], "при пустой ручке тронут посторонний узел: %s" % calls
print("OK 4/4: КОНТРОЛЬ — пустая ручка = прежнее поведение, весь трафик на боевом")
print("=== #99 ЗАКРЫТ (увод не трогает готовность: брони снимаются в любом случае) ===")
