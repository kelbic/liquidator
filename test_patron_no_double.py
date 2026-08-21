"""П.4 пакета kelbic 21.08: патрон ноги НЕ списывается дважды (оценка при отправке + факт
по квитанции). Дрель стендовая: N отправок с квитанциями -> кэш баланса обязан сойтись с
«цепью» (start − Σфакт) в пределах eps; расхождение > eps = баг.

eps = цена одной пробы ($0.002 ≈ 6e11 wei при факте ~40k газа): допускаем хвост ОДНОЙ
нерасчитанной оценки (проба в полёте), ноль — когда все квитанции пришли."""
import sys
import time
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

import chain.execute as ex  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


ADDR = "0x" + "dd" * 20
START = 4_490_000_000_000_000                   # 0.00449 ETH — реальный патрон ноги
EST = int(ex._VOLLEY_EXPECTED_GAS) * 100_000_000    # оценка no-op при maxFee 0.1 gwei
FACT = 40_000 * 100_000_000                     # факт по квитанции (~40k газа)


def _drill(n_send, n_receipt):
    """Стендовая дрель: n отправок (оптимистичный декремент как в send_volley_probe),
    затем n_receipt фактов через volley_bal_charge. Возврат: кэш ноги."""
    ex._VOLLEY_BAL[ADDR] = [START, time.monotonic()]
    try:
        ex._VOLLEY_EST_Q.pop(ADDR, None)        # есть только на фиксе
    except AttributeError:
        pass
    for _ in range(n_send):
        bal = ex._VOLLEY_BAL[ADDR]
        ex.volley_bal_note_sent(ADDR, EST) if hasattr(ex, "volley_bal_note_sent") else None
        if not hasattr(ex, "volley_bal_note_sent"):
            bal[0] -= EST                       # база: голый оптимистичный декремент
    for _ in range(n_receipt):
        ex.volley_bal_charge(ADDR, FACT)
    return ex._VOLLEY_BAL[ADDR][0]


# ---- ЯДРО: 20 отправок, 20 квитанций -> кэш == цепь (start − 20×факт) ----
got = _drill(20, 20)
chain_truth = START - 20 * FACT
eps = 6 * 10**11
check("дрель 20/20: кэш сходится с цепью (одинарное списание)",
      abs(got - chain_truth) <= eps, f"кэш={got} цепь={chain_truth} дельта={got-chain_truth}")

# ---- Проба в полёте: 20 отправок, 19 квитанций -> дельта ровно одна оценка ----
got = _drill(20, 19)
chain_truth = START - 19 * FACT
check("в полёте одна проба: дельта <= одна оценка",
      0 <= chain_truth - got <= EST + eps, f"дельта={chain_truth-got} EST={EST}")

# ---- Квитанция без отправки (путь V1/чужой учёт): голый факт, не в минус ----
ex._VOLLEY_BAL[ADDR] = [FACT // 2, time.monotonic()]
getattr(ex, "_VOLLEY_EST_Q", {}).pop(ADDR, None)      # чистый кейс: оценок в очереди нет
ex.volley_bal_charge(ADDR, FACT)
check("факт больше кэша не уводит в минус", ex._VOLLEY_BAL[ADDR][0] == 0)

# ---- Гард дрейфа: send-путь пишет через note_sent (единый шов), не голым bal[0]-= ----
_SRC = open(ex.__file__.replace(".pyc", ".py")).read()
check("send_volley_probe списывает через volley_bal_note_sent",
      "volley_bal_note_sent(addr, _spend)" in _SRC)
check("charge ЗАМЕЩАЕТ оценку фактом (возврат из очереди)",
      "_VOLLEY_EST_Q" in _SRC)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: патрон — одинарное списание, факт замещает оценку, рефреш сверху"))
sys.exit(1 if FAILS else 0)
