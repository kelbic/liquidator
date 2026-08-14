"""Дрель Р1б/#120: tip хвоста — отдельный режим, гейт и выстрел считают ОДНО число.

Замер суб-блоков 49965023: реактивный конвейер структурно не попадает в суб-блок транзита,
опасность Ш28 (голова суб-блока со старой ценой) к нему неприменима, а класс 0.001-0.02
проигрывает сортировку хвоста реактору (~4 gwei) всегда. Tip хвоста — от экономики цели.
"""
import os
import sys

os.environ.setdefault("REACTIVE_TAIL_TIP_GWEI", "4.0")
os.environ.setdefault("REACTIVE_TAIL_TIP_FRAC", "0.25")

import chain.hotpath as hp                                    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Cfg:
    tip_gwei = 0.001
    gas_limit_est = 1_200_000
    eth_price_usd = 4000.0


cfg = Cfg()
# измеренный класс рынка = 1.6e6 wei (0.0016 gwei) — гонка 11.08
hp.note_dominant_tip(1_600_000)
_dyn = hp.dynamic_tip_gwei(cfg, 100.0)
check("предусловие: класс читается", abs(_dyn - 0.0016) < 1e-9, str(_dyn))

# 1. ОТКАТ: ручка=0 -> бит-в-бит dynamic
_saved = hp._TAIL_TIP_GWEI
hp._TAIL_TIP_GWEI = 0.0
try:
    check("откат: tail == dynamic бит-в-бит", hp.tail_tip_gwei(cfg, 1000.0, 100.0) == _dyn)
finally:
    hp._TAIL_TIP_GWEI = _saved

# 2. Жирный хвост: afford >> 4 -> кап классом реактора 4.0
t = hp.tail_tip_gwei(cfg, 1000.0, 100.0)
check("жирный хвост капится 4.0 gwei", abs(t - 4.0) < 1e-9, str(t))

# 3. Средний хвост: afford между классом и капом -> платим afford
#    profit $10: afford = 0.25*10*1e9/(1.2e6*4000) = 0.5208... gwei
t = hp.tail_tip_gwei(cfg, 10.0, 100.0)
check("средний хвост платит afford (~0.52)", 0.5 < t < 0.55, str(t))

# 4. Пыль: afford ниже класса рынка -> пол = класс (ниже рынка не сядем)
t = hp.tail_tip_gwei(cfg, 0.01, 100.0)
check("пыль не проваливается ниже класса", abs(t - 0.0016) < 1e-9, str(t))

# 5. Незнание не повышает ставку: кривой cfg -> afford=0 -> класс
class BadCfg:
    tip_gwei = 0.001
    gas_limit_est = 0
    eth_price_usd = 0
check("незнание (gas/eth=0) -> класс, не кап",
      abs(hp.tail_tip_gwei(BadCfg(), 1000.0, 100.0) - 0.0016) < 1e-9)

# 6. КОГЕРЕНТНОСТЬ: _net_gate_tail отдаёт cost, посчитанный ТЕМ ЖЕ tip, что вернул
p, c, n, tip = hp._net_gate_tail(10**18, 100.0, 10**18, cfg)   # profit_usd = 100.0
c2 = cfg.gas_limit_est * tip * cfg.eth_price_usd / 1e9
check("гейт и tip — одно число (cost==gas*tip*eth)", abs(c - c2) < 1e-12, "%s vs %s" % (c, c2))
check("net = profit - cost", abs(n - (p - c)) < 1e-12)

# 7. ПРОВОДКА (шов не выше дефекта): обе боевые точки реактива идут через _net_gate_tail,
#    и выстрел подписывается тем же _tail_tip
src = open(os.path.join(os.path.dirname(hp.__file__), "hotpath.py")).read()
check("проводка: prepare_hot через _net_gate_tail",
      "net_usd, _tail_tip = _net_gate_tail(profit_wei" in src)
check("проводка: prepare_fast через _net_gate_tail",
      "net_usd, _tail_tip = _net_gate_tail(min_profit" in src)
check("проводка: tip выстрела = _tail_tip (2 точки)", src.count("_tip = int(_tail_tip * 1e9)") == 2)
check("проводка: старый dynamic-tip в prepare больше не подписывает",
      "int(dynamic_tip_gwei(cfg, debt_usd) * 1e9)" not in src)

# 8. Боевой дефолт: ручка включена из коробки
check("дефолт REACTIVE_TAIL_TIP_GWEI > 0", hp._TAIL_TIP_GWEI > 0)

if FAILS:
    print("=== ДРЕЛЬ #120 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ #120 ЗЕЛЁНАЯ: хвост платит ранг от экономики цели, гейт видит ту же цену ===")
