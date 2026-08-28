"""Ближайшая досягаемая цель гарантирована в группе (28.08, гонка 50571836). Зовёт БОЕВОЙ _top_fat."""
import importlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
def _sfl(env):
    os.environ.pop("SPEC_FIRE_EDGE_SLOT", None); os.environ.update(env)
    import chain.spec_fire_live as sfl; importlib.reload(sfl)
    import test_top_fat_tiebreak as tt; importlib.reload(tt)
    return sfl, tt
MID = "0xm"   # тот же рынок, что в фикстуре _mk из test_top_fat_tiebreak
FAT = {("%s" % MID, "0xF%d" % i): 1.0 / (1.0 - 0.0036 - 0.0002 * i) for i in range(7)}   # запас 0.36%..0.48%: ВСЕ внутри семени BTC 0.53% => вес 1 у всех, решает долг, и NEAR ($51) без edge-слота ВЫЛЕТАЕТ
NEAR = {(MID, "0xNEAR"): 1.0 / (1.0 - 0.0034)}                                          # запас 0.34%
DEBT = {k: 18_000.0 for k in FAT}
# NEAR — мелочь, но ВЫШЕ рыночного порога _market_min_repaid_usd (иначе тест проверяет порог, а не слот):
# порог берём из боевой функции при bonus фикстуры 0.05, +1 доллар.
import chain.spec_fire_live as _sfl0
DEBT[(MID, "0xNEAR")] = float(_sfl0._market_min_repaid_usd(0.05)) + 1.0
def test_near_target_guaranteed_in_group():
    sfl, tt = _sfl({"SPEC_FIRE_EDGE_SLOT": "1"})
    o = tt._mk({**FAT, **NEAR}, DEBT, [b for (_, b) in list(FAT) + list(NEAR)])
    top, hold, edge = o._top_fat("BTC", "down", max_dist=0.03, k=7)
    assert edge is not None and edge[1] == "0xNEAR"
    assert any(t[1] == "0xNEAR" for t in top), "ближайшая цель обязана быть в группе: %s" % [t[1] for t in top]
    assert len(top) == 7
def test_negative_control_rollback():
    sfl, tt = _sfl({"SPEC_FIRE_EDGE_SLOT": "0"})
    o = tt._mk({**FAT, **NEAR}, DEBT, [b for (_, b) in list(FAT) + list(NEAR)])
    top, hold, edge = o._top_fat("BTC", "down", max_dist=0.03, k=7)
    assert not any(t[1] == "0xNEAR" for t in top), "откат обязан вернуть прежнее: мелочь вне группы"
def test_near_already_in_top_not_duplicated():
    sfl, tt = _sfl({"SPEC_FIRE_EDGE_SLOT": "1"})
    o = tt._mk({**FAT, **NEAR}, DEBT, [b for (_, b) in list(FAT) + list(NEAR)])
    top, hold, edge = o._top_fat("BTC", "down", max_dist=0.03, k=8)
    assert sum(1 for t in top if t[1] == "0xNEAR") == 1
if __name__ == "__main__":
    import pytest; sys.exit(pytest.main([__file__, "-q"]))
