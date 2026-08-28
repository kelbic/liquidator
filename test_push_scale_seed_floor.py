"""Семя push_scale — НИЖНЯЯ граница reach(), а не только замена до MIN_N (28.08).
Зовёт БОЕВОЙ reach(), не копию. Негативный контроль — через ручку отката."""
import importlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _fresh_module(env):
    for k in ("PUSH_SCALE_SEED_FLOOR", "PUSH_SCALE_MIN_N"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import chain.push_scale as ps
    importlib.reload(ps)
    ps._RINGS.clear()
    return ps

def _feed_calm(ps, feed, n, pct, now):
    for i in range(n):
        ps.note_push(feed, pct, now=now - i)

def test_calm_week_does_not_lower_reach_below_seed():
    ps = _fresh_module({"PUSH_SCALE_MIN_N": "20", "PUSH_SCALE_SEED_FLOOR": "1"})
    now = 1_800_000_000.0
    _feed_calm(ps, "XRP", 60, 0.616, now)           # тихая неделя: все пуши 0.616%
    r = ps.reach("XRP", now=now) * 100.0
    assert abs(r - ps._SEED["XRP"]) < 1e-9, "штиль опустил reach ниже семени: %.3f" % r

def test_live_cascade_still_raises_reach_above_seed():
    ps = _fresh_module({"PUSH_SCALE_MIN_N": "20", "PUSH_SCALE_SEED_FLOOR": "1"})
    now = 1_800_000_000.0
    _feed_calm(ps, "XRP", 60, 5.0, now)             # каскадная неделя: пуши по 5%
    r = ps.reach("XRP", now=now) * 100.0
    assert r > ps._SEED["XRP"], "живой замер вверх заблокирован: %.3f" % r

def test_negative_control_rollback_restores_old_behaviour():
    ps = _fresh_module({"PUSH_SCALE_MIN_N": "20", "PUSH_SCALE_SEED_FLOOR": "0"})
    now = 1_800_000_000.0
    _feed_calm(ps, "XRP", 60, 0.616, now)
    r = ps.reach("XRP", now=now) * 100.0
    assert abs(r - 0.616) < 1e-9, "откат не вернул прежнее поведение: %.3f" % r

def test_gate_accepts_target_within_seed_budget():
    """Боевой _det_reach_ok с бюджетом seed x 2: цель в 5.0% для XRP (семя 2.772%) проходит."""
    ps = _fresh_module({"PUSH_SCALE_MIN_N": "20", "PUSH_SCALE_SEED_FLOOR": "1"})
    now = 1_800_000_000.0
    _feed_calm(ps, "XRP", 60, 0.616, now)
    import chain.spec_fire_live as sfl
    importlib.reload(sfl)
    class _Cfg: liquidator_address = "0x" + "0"*40
    logs = []
    obj = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    obj._log = logs.append; obj.stats = {}
    ok = sfl.SpecFireLive._det_reach_ok(obj, "XRP", 0.050, mid="0xm", borrower="0xb")
    assert ok, "цель в 5.0%% отклонена при бюджете %.3f%%" % (ps.reach("XRP", now=now)*200)

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
