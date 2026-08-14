"""Вердикт победы у пробы ДОСКИ — из логов протокола, а не из статуса tx (#32, 07.08).

ДЕФЕКТ, КОТОРЫЙ ЭТИ ТЕСТЫ ЛОВЯТ (найден до боя, но код с ним уже стоял на Шарлотте
с SPEC_FIRE_V2_BOARD=1). У пробы V1 статус решает всё: цель здорова -> Morpho ревертит ->
tx ревертит. У пробы доски (1 байт в fallback) status=1 у ТРЁХ РАЗНЫХ исходов:
    healthy по цене  -> `return`                -> ~18.6k газа;
    попытка неудачна -> `catch {}` (Liquidator.sol:232) -> сотни тысяч газа, план цел;
    ПОБЕДА           -> liquidate прошёл        -> + событие Liquidated.
Прежний критерий «status==1 = WIN» объявил бы победой ПЕРВУЮ ЖЕ холостую пробу и устроил
блэкаут: mark_recent_send глушит реактивный дубль, кулдаун гасит ВСЕ окна, окно закрывается —
ровно в том каскаде, ради которого строился флот.

Почему прежние тесты его не видели: ни один не доводил пробу доски до сборщика квитанции.
Поэтому здесь ПОЗИТИВНЫЙ КОНТРОЛЬ обязателен — тест, который умеет только не-победу,
доказывал бы ровно ничего (боевой урок «тест, который может вернуть только ноль»).
Без сети.
"""
import sys
import threading
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive

LIQ = "0x85BadE2cdD375Ab1b75f5C522B7989a12f939292"
FOREIGN = "0x1111111111111111111111111111111111111111"
WIN_TOPIC = sfl_mod.LIQUIDATED_TOPIC0
OTHER_TOPIC = "0x" + "ee" * 32

cfg = types.SimpleNamespace(liquidator_address=LIQ, gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=1730.0)
shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})


def make(receipt):
    sf = SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                      dev_by={"BTC": 0.001}, build_fn=lambda m, b, d: None,
                      send_fn=lambda c, g: "0xh", receipt_fn=lambda h: receipt,
                      log_fn=lambda s: None)
    sf._now = lambda: 100000.0
    return sf


def rcpt(status, won, gas_usd=0.0002):
    """Квитанция в том же виде, в каком её отдаёт _receipt: 5-й элемент — вердикт по логам."""
    return (status, gas_usd, 42, 7, won)


import chain.execute as ex_mod
ex_mod.volley_gas_add = lambda x: None
ex_mod.volley_invalidate_nonce = lambda a: None
ex_mod.mark_recent_send = lambda m, b: None


def fire(sf, board, receipt):
    """Прогнать сборщик и вернуть, объявлена ли победа."""
    sf.stats["wins"] = 0
    sf._cooldown_until = 0.0
    sf._collect_volley("0xh", "0xmid", "0xborrower", 50_000.0, "", "0xEA01", board)
    return sf.stats["wins"] > 0, sf._cooldown_until > 0.0


# ---- 1. ДОСКА: холостая проба (status=1, события нет) — НЕ победа ----
sf = make(rcpt(1, False))
won, cooled = fire(sf, True, rcpt(1, False))
assert not won, "холостая проба доски объявлена победой — это блэкаут огня"
assert not cooled, "кулдаун по холостой пробе гасит ВСЕ окна"
assert sf.stats.get("board_noop", 0) == 1
print("UNIT_OK: доска — status=1 без события Liquidated это НЕ победа (кулдаун не взведён)")

# ---- 2. ПОЗИТИВНЫЙ КОНТРОЛЬ: настоящая победа доски распознана ----
sf = make(rcpt(1, True))
won, cooled = fire(sf, True, rcpt(1, True))
assert won, "настоящая победа доски НЕ распознана — тест умел бы только не-победу"
# #46 (решение kelbic 08.08): победа ПРОБЫ больше не гасит арм — инвентарь она не тратит,
# а глобальная пауза отдавала стене ноги 2+ каскада. Пауза осталась за боевой победой.
assert not cooled, "победа пробы снова гасит флот (#46)"
import chain.spec_fire_live as _sfl_rb                      # КОНТРОЛЬ ОТКАТА
_rb = _sfl_rb.SPEC_FIRE_COOLDOWN_TRIGGER
_rbs = _sfl_rb.SPEC_FIRE_COOLDOWN_SCOPE
_sfl_rb.SPEC_FIRE_COOLDOWN_TRIGGER = "any"
_sfl_rb.SPEC_FIRE_COOLDOWN_SCOPE = "global"      # прежняя пауза = любая победа + все фиды
try:
    sf_rb = make(rcpt(1, True))
    _w, _c = fire(sf_rb, True, rcpt(1, True))
    assert _w and _c, "откат SPEC_FIRE_COOLDOWN_TRIGGER=any не возвращает прежнюю паузу"
finally:
    _sfl_rb.SPEC_FIRE_COOLDOWN_TRIGGER = _rb
    _sfl_rb.SPEC_FIRE_COOLDOWN_SCOPE = _rbs
print("UNIT_OK: позитивный контроль — событие Liquidated даёт WIN и кулдаун")

# ---- 3. Неизвестный вердикт (логов в квитанции нет) — доска молчит, fail-closed ----
sf = make(rcpt(1, None))
won, _ = fire(sf, True, rcpt(1, None))
assert not won, "нет сведений о логах => победу объявлять нельзя (цена ложной = блэкаут)"
print("UNIT_OK: доска fail-closed — «не знаю» это не победа")

# ---- 4. V1 (не доска): семантика статуса СОХРАНЕНА бит-в-бит ----
sf = make(rcpt(0, False))
won, _ = fire(sf, False, rcpt(0, False))
assert not won, "V1: revert (цель здорова) — не победа"
sf = make(rcpt(1, None))
won, cooled = fire(sf, False, rcpt(1, None))
# #46: победа V1-ноги — тоже победа ПРОБЫ, паузу по дефолту не ставит (гасить весь
# флот на 15 минут ради взятого приза = отдавать стене ноги 2+ каскада). Вердикт
# «победа» при этом обязан сохраниться бит-в-бит — его и проверяем.
assert won, "V1: прямая liquidate прошла => победа даже без сведений о логах"
assert not cooled, "V1-победа снова гасит все окна (#46)"
print("UNIT_OK: путь V1 не тронут — там статус по-прежнему решает")

# ---- 5. Разбор логов: чужое событие и чужой эмитент не считаются нашей победой ----
def _r(logs):
    return {"gasUsed": 100, "effectiveGasPrice": 1, "status": 1, "logs": logs}


assert sfl_mod._receipt_has_win(_r([{"address": LIQ, "topics": [WIN_TOPIC]}]), LIQ) is True
assert sfl_mod._receipt_has_win(_r([{"address": FOREIGN, "topics": [WIN_TOPIC]}]), LIQ) is False, \
    "Liquidated КОНКУРЕНТА в том же блоке — не наша победа"
assert sfl_mod._receipt_has_win(_r([{"address": LIQ, "topics": [OTHER_TOPIC]}]), LIQ) is False, \
    "другое событие нашего контракта (PlanSet и т.п.) — не победа"
assert sfl_mod._receipt_has_win(_r([]), LIQ) is False, "пустые логи = достоверно не победа"
assert sfl_mod._receipt_has_win({"gasUsed": 1, "effectiveGasPrice": 1, "status": 1}, LIQ) is None, \
    "поля logs нет => «не знаю», а не «нет»"
# регистр адреса и topic без 0x не должны менять вердикт (RPC отдаёт по-разному)
assert sfl_mod._receipt_has_win(_r([{"address": LIQ.lower(), "topics": [WIN_TOPIC[2:]]}]), LIQ) is True
print("UNIT_OK: событие сверяется с НАШИМ эмитентом и нашим topic0, регистр/0x не важны")

print("ПРОШЛИ: вердикт победы доски — из логов протокола")
