"""Начисление процента ДО проверки здоровья (28.08). Зовёт БОЕВОЙ _flips/health_from, не копию.
Красный-на-базе: цель ровно на границе по хранимым тоталам была «здорова», Morpho её ликвидировал."""
import importlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _hp(env):
    os.environ.pop("HF_ACCRUAL", None); os.environ.update(env)
    import chain.hotpath as hp
    importlib.reload(hp); hp._MARKET_ACCRUAL.clear()
    return hp

# ФАКТ С ЦЕПИ, не модель: цель блока 50542867 (cbXRP/USDC) в блоке B-1 — её взял конкурент, у нас
# по хранимым тоталам HF=1.00000 («здорова»). Ставка — боевым encode_borrow_rate_view_call.
MID = "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109"
LLTV = 625_000_000_000_000_000
PRICE = 1_458_860_000_000_000_000_000_000_000_000_000_000
TBA, TBS = 42_529_506_960_922, 40_917_923_180_554_887_066
COLL, BS = 3_653_572, 3_205_047_463_984
RATE_PS = 1_910_824_085            # 6.03%/год
ELAPSED = 46                       # block_ts - lastUpdate
NOW = 1_787_875_081

def _run(hp):
    hp.note_market_accrual(MID, NOW - ELAPSED, rate_ps=RATE_PS, now=NOW)
    return hp._flips(PRICE, LLTV, TBA, TBS, [("0xb", BS, COLL, 1.0)], mid=MID, now=NOW)

def test_boundary_target_flips_with_accrual():
    hp = _hp({"HF_ACCRUAL": "1"})
    out = _run(hp)
    assert len(out) == 1, "цель на границе с 44с неначисленного процента ОБЯЗАНА флипнуть (как у Morpho)"
    assert out[0][1].hf < 1.0

def test_negative_control_rollback_keeps_old_behaviour():
    hp = _hp({"HF_ACCRUAL": "0"})
    out = _run(hp)
    assert out == [], "откат HF_ACCRUAL=0 обязан вернуть прежнее: цель на границе здорова"

def test_unknown_rate_does_not_open():
    hp = _hp({"HF_ACCRUAL": "1"})
    hp.note_market_accrual(MID, NOW - ELAPSED, rate_ps=None, now=NOW)     # ставки нет
    out = hp._flips(PRICE, LLTV, TBA, TBS, [("0xb", BS, COLL, 1.0)], mid=MID, now=NOW)
    assert out == [], "незнание ставки НЕ открывает: поведение прежнее"

def test_healthy_margin_target_stays_healthy():
    hp = _hp({"HF_ACCRUAL": "1"})
    hp.note_market_accrual(MID, NOW - ELAPSED, rate_ps=RATE_PS, now=NOW)
    out = hp._flips(PRICE, LLTV, TBA, TBS, [("0xb", BS * 95 // 100, COLL, 1.0)], mid=MID, now=NOW)
    assert out == [], "цель с запасом 5% от 44с начисления не флипает (ложных срабатываний нет)"

def test_accrued_tba_matches_morpho_taylor():
    hp = _hp({"HF_ACCRUAL": "1"})
    hp.note_market_accrual(MID, NOW - ELAPSED, rate_ps=RATE_PS, now=NOW)
    t = hp.accrued_tba(MID, TBA, now=NOW)
    x = RATE_PS * ELAPSED; W = 10 ** 18
    exp = TBA + TBA * (x + x * x // (2 * W) + (x * x // (2 * W)) * x // (3 * W)) // W
    assert t == exp and t > TBA

if __name__ == "__main__":
    import pytest; sys.exit(pytest.main([__file__, "-q"]))
