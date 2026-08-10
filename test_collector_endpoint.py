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

Признак отказа транспорта отличим от «ещё не намайнено»: `batch_receipts` на отказе возвращает
ПУСТОЙ словарь, а непришедшая квитанция приезжает как {hash: None} — словарь не пуст.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

SRC = pathlib.Path("chain/spec_fire_live.py").read_text()

# --- 1. Ручка есть и по умолчанию ИНЕРТНА (прежнее поведение) ----------------------------
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

# --- 2. СТРУКТУРНО: страховка боевым есть, и она НЕ обходится ----------------------------
i = SRC.index("def _receipts(hs):")
blk = SRC[i:SRC.index("col = _fe.ReceiptCollector(", i)]
assert "if not SPEC_FIRE_COLLECTOR_RPC_URL:" in blk, "нет ветки «ручка пуста -> боевой»"
assert blk.count("_combat_url()") >= 2, \
    "боевой узел не используется как страховка — брони не снимутся при молчании выделенного"
assert "if out:" in blk, "пустой ответ не отличается от нормального — страховка не сработает"
assert "collector_fallback" in blk, "срабатывание страховки не считается (диагностики не будет)"
print("OK 2/4: пустой ответ выделенного узла -> берём боевой, срабатывание считается")

# --- 3. ЛОГИКА страховки на моках: пустой словарь != {hash: None} ------------------------
calls = []


def make_receipts(dedicated_url, dedicated_out, combat_out):
    """Повторяет боевую ветку один-в-один, чтобы экзаменовать РЕШЕНИЕ, а не текст."""
    stats = {}

    def _batch(url, hs):
        calls.append(url)
        return dict(dedicated_out) if url == dedicated_url else dict(combat_out)

    def _receipts(hs):
        if not dedicated_url:
            return _batch("combat", hs)
        out = _batch(dedicated_url, hs)
        if out:
            return out
        stats["collector_fallback"] = stats.get("collector_fallback", 0) + 1
        return _batch("combat", hs)
    return _receipts, stats


H = "0xaaa"

# 3a. выделенный ответил -> боевой НЕ трогаем (нагрузка ушла с боевого ключа)
calls.clear()
f, st = make_receipts("dedicated", {H: {"status": "0x1"}}, {H: {"status": "0x1"}})
assert f([H]) == {H: {"status": "0x1"}}
assert calls == ["dedicated"], "боевой ключ дёрнут зря — увод не состоялся: %s" % calls
assert not st, "страховка засчитана на здоровом ответе"

# 3b. ЕЩЁ НЕ НАМАЙНЕНО ({hash: None}) — это НЕ отказ, страховку НЕ зовём
calls.clear()
f, st = make_receipts("dedicated", {H: None}, {H: {"status": "0x1"}})
assert f([H]) == {H: None}, "«не намайнено» подменено ответом боевого"
assert calls == ["dedicated"], "страховка сработала на нормальном ответе — лишний расход боевого"
assert not st

# 3c. ОТКАЗ ТРАНСПОРТА (пустой словарь) -> страховка боевым
calls.clear()
f, st = make_receipts("dedicated", {}, {H: {"status": "0x1"}})
assert f([H]) == {H: {"status": "0x1"}}, "выделенный молчит, а квитанции не добраны боевым"
assert calls == ["dedicated", "combat"], calls
assert st.get("collector_fallback") == 1, "срабатывание страховки не посчитано"
print("OK 3/4: ответ -> только выделенный; «не намайнено» -> без страховки; отказ -> боевой")

# --- 4. КОНТРОЛЬ: ручка пуста -> всё как раньше, выделенный не дёргается -----------------
calls.clear()
f, st = make_receipts("", {}, {H: {"status": "0x1"}})
assert f([H]) == {H: {"status": "0x1"}}
assert calls == ["combat"], "при пустой ручке тронут посторонний узел: %s" % calls
print("OK 4/4: КОНТРОЛЬ — пустая ручка = прежнее поведение, весь трафик на боевом")
print("=== #99 ЗАКРЫТ (увод не трогает готовность: брони снимаются в любом случае) ===")
