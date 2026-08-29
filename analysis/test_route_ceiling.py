"""Проба потолка пула: выход, не растущий вдвое при удвоении входа, = упёрлись в резерв.

Повод — тревога 29.08 (route-canary:4cbc66e0): wstETH/WETH держал impact 0.00% на капе,
пока кап съедал 96% резерва WETH; обычный своп на 56.7 wstETH обвалил выход втрое.
Контроли ОБА: глубокий пул обязан МОЛЧАТЬ, упёршийся — звонить.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analysis.route_canary as rc
from analysis.route_canary_watch import evaluate, CEILING_MIN

CAP = 150 * 10**18
ITEMS = [(("0xcoll", "0xloan"), {"router": "0xr", "style": "v1", "path": "0xpath",
                                 "max_in": CAP})]


def _quotes(table):
    """Транспорт-заглушка: amount -> выход. Подменён ТОЛЬКО транспорт, не логика scan."""
    def call(_to, data, **kw):
        # rc.quote кодирует amount в последних 32 байтах calldata пути-квоты; заглушка
        # получает уже готовый hex — распознаём размер по таблице подстановкой.
        raise AssertionError("не должен вызываться: quote подменён целиком")
    return call


def _fake_quote(table):
    def quote(_quoter, _path, amount, **kw):
        v = table.get(amount)
        return (v, None, rc.ALIVE) if v is not None else (None, "нет ступени", rc.DEAD)
    return quote


def _scan_with(table):
    real = rc.quote
    rc.quote = _fake_quote(table)
    try:
        return rc.scan(ITEMS, module=object(), block="0x1")
    finally:
        rc.quote = real


REF = CAP // 100
# 1) НЕГАТИВНЫЙ КОНТРОЛЬ: глубокий пул — удвоение входа удваивает выход. Молчание.
deep = _scan_with({REF: 1863369683552569000, CAP: 186_340 * 10**15, CAP * 2: 372_680 * 10**15})[0]
assert deep["status"] == rc.ALIVE, deep
assert abs(deep["headroom"] - 2.0) < 1e-9, deep["headroom"]
assert evaluate([deep], {}) == [], "глубокий пул обязан молчать"

# 2) ПОЗИТИВНЫЙ КОНТРОЛЬ: числа РЕАЛЬНОГО замера 29.08 за час до обвала (impact 0.00%!).
tight = _scan_with({REF: 1863369683552569000, CAP: 186_180 * 10**15, CAP * 2: 193_070 * 10**15})[0]
assert tight["impact"] < 0.1, tight["impact"]          # impact-порог тут слеп — это и есть суть
assert tight["headroom"] < CEILING_MIN, tight["headroom"]
probs = evaluate([tight], {})
assert len(probs) == 1 and probs[0]["kind"] == "ceiling", probs
assert "ПОТОЛОК ПУЛА" in probs[0]["text"]

# 3) НЕЗНАНИЕ НЕ ЗВОНИТ И НЕ УБИВАЕТ МАРШРУТ: реверт квотера на 2x — свойство пробы.
blind = _scan_with({REF: 1863369683552569000, CAP: 186_340 * 10**15})[0]
assert blind["status"] == rc.ALIVE, "реверт на двойном размере не смеет убивать маршрут"
assert blind["headroom"] is None and evaluate([blind], {}) == []

# 4) УЖЕ ГЛУБОКИЙ (impact выше порога) не удваивается в тревоге вторым пунктом,
#    НО ЧИСЛО ПОТОЛКА ОБЯЗАНО ЕХАТЬ В ЕГО ЖЕ ТЕКСТЕ (тревога 09df3967, 29.08 23:07):
#    elif-цепочка отправляла упёршийся роут в "deep" и роняла headroom молча.
probs2 = evaluate([tight], {("0xcoll", "0xloan"): (0.01, "тест")})
assert len(probs2) == 1 and probs2[0]["kind"] == "deep", probs2
assert "ПОТОЛОК ПУЛА" in probs2[0]["text"], probs2[0]["text"]
assert "x1.0370" in probs2[0]["text"], probs2[0]["text"]

# 5) НЕГАТИВНЫЙ КОНТРОЛЬ К ФИКСУ: глубокий ПО ЦЕНЕ (запас есть) не смеет получить приписку
#    про потолок — иначе разбор погонят не туда. Тот же путь, отличается только headroom.
pricey = _scan_with({REF: 1863369683552569000, CAP: 100_000 * 10**15,
                     CAP * 2: 200_000 * 10**15})[0]
assert abs(pricey["headroom"] - 2.0) < 1e-9, pricey["headroom"]
probs3 = evaluate([pricey], {("0xcoll", "0xloan"): (0.01, "тест")})
assert len(probs3) == 1 and probs3[0]["kind"] == "deep", probs3
assert "ПОТОЛОК ПУЛА" not in probs3[0]["text"], probs3[0]["text"]

print("UNIT_OK: проба потолка — глубокий молчит (x2.0), упёршийся звонит (x1.037), "
      "незнание молчит и не убивает маршрут, дубля с impact-тревогой нет, "
      "а ЧИСЛО потолка едет в тексте глубокой тревоги (и НЕ едет при живом запасе)")
