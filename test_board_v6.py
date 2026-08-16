"""Юнит-приёмка обвязки v6-D4 (chain/board_v6.py) — БЕЗ форка (§2/§3/§4/§6/§9 как чистая логика).

Что здесь доказывается ЧИСЛАМИ, а не самоотчётом:
 * селекторы всех owner-функций совпадают с methodIdentifiers АРТЕФАКТА (кодирование не уйдёт в
   чужую функцию — класс #84);
 * setGroup-калдата round-trip'ится обратно в те же аргументы (порядок полей цели верен);
 * mp_hash совпадает с НЕЗАВИСИМОЙ формулой стенда (keccak 5 склеенных слов);
 * verify_arm требует ОБА доказательства и ловит хвост #84 (совпал первый borrower — мало);
 * until берётся как min по спану, мёртвая группа = громкий отказ (§4);
 * choose_eviction: гистерезис #88 на уровне группы + живой спан не вытесняется (§1/§2);
 * assert_disjoint_pools: пересечение пулов = PoolOverlap (§9, консервативно);
 * make_target: дефолт repaidShares, обрезание uint128 — громко (§6).

Запуск: `python3 test_board_v6.py` (или `pytest test_board_v6.py`).
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from eth_abi import decode as abi_decode          # noqa: E402
from eth_abi import encode as abi_encode           # noqa: E402
from eth_utils import keccak                        # noqa: E402

import chain.board_v6 as b                          # noqa: E402

_ART = json.load(open("contracts/out/LiquidatorV6.sol/LiquidatorV6.json"))
MPD = {"loanToken": "0x" + "a1" * 20, "collateralToken": "0x" + "a2" * 20,
       "oracle": "0x" + "a3" * 20, "irm": "0x" + "a4" * 20, "lltv": 625000000000000000}
BORR = "0x" + "be" * 20
ROUTER = "0x" + "51" * 20


def _t(borrower=BORR, repaid=7, seized=0, route=b"\x11" * 32, trig=0):
    return b.make_target(borrower, repaid_shares=repaid, seized_assets=seized, swap_target=ROUTER,
                         swap_calldata=route, min_profit=0, unwrap_target=b.ZERO_ADDR,
                         trigger_price=trig)


# ---------------------------------------------------------------------------------------------
def test_selectors_match_artifact():
    mids = _ART["methodIdentifiers"]
    # обратная карта: сигнатура из артефакта -> селектор
    for name, sig in b._SIG.items():
        assert sig in mids, "сигнатуры %s нет в артефакте: %s" % (name, sig)
        assert mids[sig] == b.selector(name).hex(), (name, mids[sig], b.selector(name).hex())


def test_setgroup_encoding_roundtrip():
    ts = [_t(borrower="0x" + "b%d" % k * 20 if False else ("0x%040x" % (0xB0 + k)), repaid=k + 1,
             route=bytes([k]) * (10 * (k + 1))) for k in range(3)]
    cd = b.encode_set_group(4, MPD, 123456, 999, ts)
    assert cd[:10] == "0x7a193c68", cd[:10]
    g, mp, dl, gate, back = abi_decode(
        ["uint256", b._MP_T, "uint64", "uint256", "%s[]" % b._TARGET_T],
        bytes.fromhex(cd[10:]))
    assert g == 4 and dl == 123456 and gate == 999
    assert mp == b.mp_tuple(MPD)
    assert len(back) == 3
    for k in range(3):
        # порядок полей: borrower, repaidShares, seizedAssets, swapTarget, cd, minProfit, unwrap, trig
        assert back[k][0].lower() == ("0x%040x" % (0xB0 + k)), back[k][0]
        assert back[k][1] == k + 1 and back[k][2] == 0
        assert back[k][3].lower() == ROUTER
        assert back[k][4] == bytes([k]) * (10 * (k + 1))


def test_target_field_order_matches_stand():
    # test_fork_v6d4.target() возвращает (borrower, sh, seized, ROUTER, r, minprofit, unwrap, trig)
    t = _t(repaid=5, seized=0, route=b"\xab\xcd")
    assert t[0].lower() == BORR and t[1] == 5 and t[2] == 0
    assert t[3].lower() == ROUTER and t[4] == b"\xab\xcd" and t[5] == 0
    assert t[6] == b.ZERO_ADDR and t[7] == 0


def test_make_target_default_repaid_shares_and_uint128_loud():
    # §6: дефолт — repaidShares; seized=0 по умолчанию
    t = b.make_target(BORR, repaid_shares=10, swap_target=ROUTER, swap_calldata=b"")
    assert b.is_repaid_shares_target(t) and t[2] == 0
    # ровно одно ненулевое: оба нуля — ошибка
    for bad in (dict(repaid_shares=0, seized_assets=0), dict(repaid_shares=3, seized_assets=4)):
        try:
            b.make_target(BORR, swap_target=ROUTER, swap_calldata=b"", **bad)
            assert False, "должно было упасть: %s" % bad
        except ValueError:
            pass
    # обрезание uint128 — ГРОМКО (класс #121)
    try:
        b.make_target(BORR, repaid_shares=1 << 128, swap_target=ROUTER, swap_calldata=b"")
        assert False, "2^128 обязано падать, а не обрезаться"
    except ValueError:
        pass
    # 2^128-1 законен (граница)
    t2 = b.make_target(BORR, repaid_shares=(1 << 128) - 1, swap_target=ROUTER, swap_calldata=b"")
    assert t2[1] == (1 << 128) - 1
    # seizedAssets путь (класс #79) — тоже валиден
    t3 = b.make_target(BORR, seized_assets=99, swap_target=ROUTER, swap_calldata=b"")
    assert t3[2] == 99 and t3[1] == 0 and not b.is_repaid_shares_target(t3)


def test_mp_hash_matches_independent_stand_formula():
    # НЕЗАВИСИМАЯ формула (как в test_fork_v6d4.py:MPH): keccak 5 склеенных 32-байтовых слов.
    manual = keccak(hexstr="".join([
        "0" * 24 + MPD["loanToken"][2:], "0" * 24 + MPD["collateralToken"][2:],
        "0" * 24 + MPD["oracle"][2:], "0" * 24 + MPD["irm"][2:], "%064x" % MPD["lltv"]]).lower())
    assert b.mp_hash(MPD) == manual, (b.mp_hash(MPD).hex(), manual.hex())
    # и через 5-кортеж — то же
    assert b.mp_hash((MPD["loanToken"], MPD["collateralToken"], MPD["oracle"], MPD["irm"],
                      MPD["lltv"])) == manual


# --- фейковая цепь для приёмки §3/§4 (call_fn(to,data)->bytes) --------------------------------
class FakeChain:
    """Минимальный слот-стор для groupInfo/targetInfo/hotInfo. Возвращает ABI-байты как контракт."""
    def __init__(self):
        self.groups = {}     # g -> (oracle, deadline, gate, n)
        self.targets = {}    # (g,i) -> (borrower, repaid, seized, trig)
        self.hot = (0, 0, 0)

    def set_group(self, g, oracle, deadline, n, gate=0):
        self.groups[g] = (oracle, deadline, gate, n)

    def set_target(self, g, i, borrower):
        self.targets[(g, i)] = (borrower, 1, 0, 0)

    def call(self, to, data):
        sel = bytes.fromhex(data[2:10])
        body = bytes.fromhex(data[10:])
        if sel == b.selector("groupInfo"):
            (g,) = abi_decode(["uint256"], body)
            o, dl, gate, n = self.groups.get(g, (b.ZERO_ADDR, 0, 0, 0))
            return abi_encode(["address", "uint64", "uint256", "uint8"], [o, dl, gate, n])
        if sel == b.selector("targetInfo"):
            g, i = abi_decode(["uint256", "uint256"], body)
            bo, rs, sa, tp = self.targets.get((g, i), (b.ZERO_ADDR, 0, 0, 0))
            return abi_encode(["address", "uint128", "uint128", "uint256"], [bo, rs, sa, tp])
        if sel == b.selector("hotInfo"):
            g0, c, u = self.hot
            return abi_encode(["uint8", "uint8", "uint64"], [g0, c, u])
        raise AssertionError("неизвестный селектор в фейке: %s" % sel.hex())


def test_until_from_chain_min_and_loud_on_dead():
    fc = FakeChain()
    fc.set_group(2, "0x" + "a3" * 20, 1000, 8)
    fc.set_group(3, "0x" + "a3" * 20, 950, 8)     # минимум спана
    fc.set_group(4, "0x" + "a3" * 20, 1200, 8)
    assert b.until_from_chain(fc.call, "0xC", 2, 3) == 950
    # мёртвая группа спана => громкий отказ (§4)
    fc.set_group(5, b.ZERO_ADDR, 0, 0)
    try:
        b.until_from_chain(fc.call, "0xC", 3, 3)   # включает мёртвую 5
        assert False, "мёртвая группа спана должна ронять until_from_chain"
    except ValueError:
        pass


def test_verify_arm_requires_both_proofs_and_84_tail():
    fc = FakeChain()
    g, dl = 2, 4242
    borrowers = ["0x%040x" % (0xB0 + k) for k in range(3)]
    fc.set_group(g, "0x" + "a3" * 20, dl, 3)
    for k, bo in enumerate(borrowers):
        fc.set_target(g, k, bo)

    ok, _ = b.verify_arm(fc.call, "0xC", g, {"status": 1}, dl, borrowers)
    assert ok, "полное совпадение обязано пройти"

    # красный контроль (§8.1): квитанция status!=1 => НЕ armed (арм с малым лимитом газа)
    ok, why = b.verify_arm(fc.call, "0xC", g, {"status": 0}, dl, borrowers)
    assert not ok and "status" in why

    # чужой слот / протухший переарм: дедлайн не наш
    ok, why = b.verify_arm(fc.call, "0xC", g, {"status": 1}, dl + 1, borrowers)
    assert not ok and "deadline" in why

    # n не сходится
    ok, why = b.verify_arm(fc.call, "0xC", g, {"status": 1}, dl, borrowers[:2])
    assert not ok and "n=" in why

    # ХВОСТ #84: первый borrower совпал, второй — нет => сверки одного мало
    tail = list(borrowers)
    tail[1] = "0x" + "cc" * 20
    ok, why = b.verify_arm(fc.call, "0xC", g, {"status": 1}, dl, tail)
    assert not ok and "borrower" in why

    # квитанция как СЫРОЙ JSON-RPC (hex-статус) — тоже читается
    ok, _ = b.verify_arm(fc.call, "0xC", g, {"status": "0x1"}, dl, borrowers)
    assert ok


def test_verify_hot():
    fc = FakeChain()
    fc.hot = (2, 4, 950)
    ok, _ = b.verify_hot(fc.call, "0xC", 2, 4, 950)
    assert ok
    ok, why = b.verify_hot(fc.call, "0xC", 2, 4, 999)
    assert not ok and "hotInfo" in why


def test_arm_gas_limit_floor_and_monotone():
    ref = b.ARM_GAS_REF
    assert b.arm_gas_limit(8, 2048) >= int(ref * b.ARM_GAS_SAFETY) >= ref
    assert b.arm_gas_limit(8, 2048) > b.arm_gas_limit(8, 1024)     # растёт с роутом
    assert b.arm_gas_limit(8, 2048) > b.arm_gas_limit(4, 2048)     # растёт с числом целей


def test_choose_eviction_hysteresis_and_live_span():
    margin = 0.2
    # меньше 8 занято => вытеснять некого
    assert b.choose_eviction([{"g": 0, "ev": 1.0}], 100.0, ev_margin=margin) is None
    full = [{"g": i, "ev": float(i + 1)} for i in range(8)]     # слабейший g=0, ev=1
    # превосходит слабейшего на порог => вытеснить g0
    assert b.choose_eviction(full, 1.0 * (1 + margin) + 0.01, ev_margin=margin) == 0
    # в пределах порога => держим (демпфер флапа #88)
    assert b.choose_eviction(full, 1.0 * (1 + margin) - 0.01, ev_margin=margin) is None
    # ЖИВОЙ спан не вытесняется: слабейший под спаном, следующий свободный g=1 ev=2
    full2 = [dict(s) for s in full]
    full2[0]["live_span"] = True
    assert b.choose_eviction(full2, 2.5, ev_margin=margin) == 1        # не 0 (он под спаном)
    assert b.choose_eviction(full2, 2.0 * (1 + margin) - 0.01, ev_margin=margin) is None
    # все 8 под живым спаном => None (новый ждёт; §1: реверт вместо увода)
    allspan = [{"g": i, "ev": float(i), "live_span": True} for i in range(8)]
    assert b.choose_eviction(allspan, 999.0, ev_margin=margin) is None


def test_assert_disjoint_pools():
    b.assert_disjoint_pools([{"pA"}, {"pB"}, {"pC"}])          # непересекающиеся — ок
    for bad in ([{"pA"}, {"pA"}], [{"pA", "pB"}, {"pB"}]):
        try:
            b.assert_disjoint_pools(bad)
            assert False, "пересечение обязано ронять: %s" % bad
        except b.PoolOverlap:
            pass
    # неизвестный пул = пересечение со всем (незнание закрывает гард)
    try:
        b.assert_disjoint_pools([{"pA"}, set()])
        assert False, "неизвестный пул обязан ронять"
    except b.PoolOverlap:
        pass


def test_layout_ordering_and_span_split():
    # §2: цели упорядочены по EV убыванием; каскад >8 => подряд группы, все ОДИН mp
    items = [(float(ev), _t(borrower="0x%040x" % ev, repaid=ev)) for ev in range(1, 11)]
    plan = b.plan_market_groups(MPD, items)
    assert plan["count"] == 2 and len(plan["groups"][0]) == 8 and len(plan["groups"][1]) == 2
    assert plan["mp"] == b.mp_tuple(MPD)
    # первая группа = 8 самых жирных (ev 10..3), голова = ev 10
    first = plan["groups"][0]
    assert first[0][1] == 10 and first[-1][1] == 3      # repaid == ev, порядок убыванием
    # §9: пулы за пределом одной группы могут повторяться, ВНУТРИ группы — нет
    items3 = [(3.0, _t(repaid=3)), (2.0, _t(repaid=2)), (1.0, _t(repaid=1))]
    b.plan_market_groups(MPD, items3, max_per_group=2, pools=[{"p1"}, {"p2"}, {"p1"}])  # p1 в разных группах — ок
    try:
        b.plan_market_groups(MPD, items3, max_per_group=3, pools=[{"p1"}, {"p2"}, {"p1"}])  # p1 в ОДНОЙ группе
        assert False, "пересечение пулов ВНУТРИ группы обязано ронять"
    except b.PoolOverlap:
        pass


def test_clear_and_batch_encoders():
    assert b.encode_clear_hot() == "0x" + "afce807b"
    assert b.encode_clear_group(3)[:10] == "0x" + "a3553332"
    assert b.encode_clear_target(2, 5)[:10] == "0x" + "4c5e1866"
    # round-trip clearTarget
    g, i = abi_decode(["uint256", "uint256"], bytes.fromhex(b.encode_clear_target(2, 5)[10:]))
    assert (g, i) == (2, 5)
    cd = b.encode_set_authorized_batch(["0x" + "01" * 20, "0x" + "02" * 20], True)
    assert cd[:10] == "0x" + "35474cd2"
    addrs, ok = abi_decode(["address[]", "bool"], bytes.fromhex(cd[10:]))
    assert ok is True and len(addrs) == 2


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    fails = 0
    for fn in ALL:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as e:
            fails += 1
            print("FAIL %s: %r" % (fn.__name__, e))
    print("=" * 60)
    if fails:
        print("ПРОВАЛ: %d из %d" % (fails, len(ALL)))
        sys.exit(1)
    print("OK: %d/%d юнит-проверок обвязки v6-D4 прошли" % (len(ALL), len(ALL)))
