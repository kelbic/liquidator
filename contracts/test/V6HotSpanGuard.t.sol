// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test} from "forge-std/Test.sol";
import {LiquidatorV6} from "../src/LiquidatorV6.sol";
import {MarketParams} from "../src/Liquidator.sol";

/// @title Приёмка §1 SPEC_V6D4_WIRING.md — гард HotSpanLive в setGroup/clearGroup.
/// @notice ЕДИНЫЙ файл на ОБЕ редакции контракта (канон «красный-на-базе → зелёный-с-правкой»):
/// селектор ошибки берётся ЛИТЕРАЛОМ `bytes4(keccak256("HotSpanLive()"))`, поэтому тест
/// компилируется и против базы (741c34b, без ошибки) и против правки.
///  * НА БАЗЕ (без гарда): `test_setGroup_into_live_span` / `test_clearGroup_into_live_span`
///    ПАДАЮТ, и лог печатает ДЕФЕКТ — мутация ПРИНЯТА, отпечаток спана разошёлся постфактум
///    (увод флота в чужой рынок / молчаливое гашение группы). Контроли (не гардятся) — зелёные.
///  * С ПРАВКОЙ: те же два — зелёные (revert HotSpanLive), контроли по-прежнему зелёные.
/// Гард ПОЗИЦИОННЫЙ: он ревертит ЛЮБОЙ setGroup/clearGroup в группу живого спана (даже тем же
/// рынком) — штатная перезарядка обязана идти после clearHot(); докладка каскада (setTargets,
/// заголовок цел) НЕ гардится и остаётся позитивным контролем.
contract V6HotSpanGuardTest is Test {
    LiquidatorV6 liq;
    bytes4 constant HOTSPANLIVE = bytes4(keccak256("HotSpanLive()"));

    MarketParams mpA;   // рынок спана
    MarketParams mpB;   // ЧУЖОЙ рынок (иной отпечаток) — по нему виден увод флота
    bytes32 mphA;
    uint64 dl;          // дедлайн армленных групп
    uint64 until;       // TTL указателя; < dl, чтобы протухание указателя тестировать при живых группах

    function setUp() public {
        liq = new LiquidatorV6(address(0x1234));   // адрес Morpho для пути арма не важен
        mpA = MarketParams({loanToken: address(0xA1), collateralToken: address(0xA2),
            oracle: address(0xA3), irm: address(0xA4), lltv: 6e17});
        mpB = MarketParams({loanToken: address(0xB1), collateralToken: address(0xB2),
            oracle: address(0xB3), irm: address(0xB4), lltv: 7e17});
        mphA = keccak256(abi.encode(mpA));
        dl = uint64(block.timestamp + 900);
        until = dl - 100;                          // указатель короче плана (§4/§2.5б)
    }

    // --- утилиты ------------------------------------------------------------------------------
    function _oneTarget() internal pure returns (LiquidatorV6.Target[] memory ts) {
        ts = new LiquidatorV6.Target[](1);
        ts[0] = LiquidatorV6.Target({borrower: address(0xBEEF), repaidShares: 1, seizedAssets: 0,
            swapTarget: address(0x5111), swapCallData: "", minProfit: 0, unwrapTarget: address(0),
            triggerPrice: 0});
    }

    /// Спан из трёх групп [1,4) на рынке mpA + указатель на весь спан.
    function _armSpan() internal {
        liq.setGroup(1, mpA, dl, 0, _oneTarget());
        liq.setGroup(2, mpA, dl, 0, _oneTarget());
        liq.setGroup(3, mpA, dl, 0, _oneTarget());
        liq.setHot(1, 3, until, mphA);
        (uint8 g0, uint8 cnt, uint64 u) = liq.hotInfo();
        assertEq(g0, 1, "hot g0"); assertEq(cnt, 3, "hot count"); assertEq(u, until, "hot until");
    }

    function _trySetGroup(uint256 g, MarketParams memory mp, uint64 d)
        internal returns (bool ok, bytes4 sel)
    {
        try liq.setGroup(g, mp, d, 0, _oneTarget()) { return (true, bytes4(0)); }
        catch (bytes memory r) { return (false, r.length >= 4 ? bytes4(r) : bytes4(0)); }
    }

    function _tryClearGroup(uint256 g) internal returns (bool ok, bytes4 sel) {
        try liq.clearGroup(g) { return (true, bytes4(0)); }
        catch (bytes memory r) { return (false, r.length >= 4 ? bytes4(r) : bytes4(0)); }
    }

    // ==========================================================================================
    // КРАСНЫЙ-НА-БАЗЕ → ЗЕЛЁНЫЙ (таблица §1, строки 1-2)
    // ==========================================================================================

    /// §1 строка 1: setGroup в группу живого спана с ДРУГИМ mp.
    function test_setGroup_into_live_span() public {
        _armSpan();
        (bool ok, bytes4 sel) = _trySetGroup(2, mpB, dl);
        if (ok) {
            // ДЕФЕКТ (красный на базе): принято — отпечаток группы 2 разошёлся со спаном.
            (address orc,,,) = liq.groupInfo(2);
            (uint8 g0, uint8 cnt,) = liq.hotInfo();
            emit log_named_address("DEFECT-RED: setGroup(2,mpB) ACCEPTED; group2 oracle now", orc);
            emit log_named_uint("  hot still spans g0", g0);
            emit log_named_uint("  hot still spans count", cnt);
            assertEq(orc, mpB.oracle,
                "invariant broken: spanned group carries FOREIGN oracle while _hot spans it");
        }
        assertTrue(!ok,
            "DEFECT-RED: setGroup(g in live span) ACCEPTED -> fleet split to foreign market silently");
        assertEq(sel, HOTSPANLIVE, "setGroup into live span must revert HotSpanLive");
    }

    /// §1 строка 2: clearGroup в группу живого спана.
    function test_clearGroup_into_live_span() public {
        _armSpan();
        (bool ok, bytes4 sel) = _tryClearGroup(2);
        if (ok) {
            (, uint64 d,, uint8 n) = liq.groupInfo(2);
            emit log_named_uint("DEFECT-RED: clearGroup(2) ACCEPTED; group2 deadline now", d);
            emit log_named_uint("  group2 n now", n);
            assertEq(d, 0, "invariant broken: spanned group wiped while _hot spans it (legs go mute)");
        }
        assertTrue(!ok,
            "DEFECT-RED: clearGroup(g in live span) ACCEPTED -> span legs die silently (healthy-miss look)");
        assertEq(sel, HOTSPANLIVE, "clearGroup into live span must revert HotSpanLive");
    }

    // ==========================================================================================
    // КОНТРОЛИ — зелёные на ОБЕИХ редакциях (гард не должен трогать штатное)
    // ==========================================================================================

    /// §1 строка 3 (граница снизу): setGroup ВНЕ спана.
    function test_control_setGroup_below_span() public {
        _armSpan();
        (bool ok,) = _trySetGroup(0, mpB, dl);   // группа 0 ниже спана [1,4)
        assertTrue(ok, "setGroup below span must be accepted");
    }

    /// §1 строка 3 (граница сверху): setGroup ВНЕ спана.
    function test_control_setGroup_above_span() public {
        _armSpan();
        (bool ok,) = _trySetGroup(4, mpB, dl);   // группа 4 выше спана [1,4)
        assertTrue(ok, "setGroup above span must be accepted");
    }

    /// §1 строка 4: setGroup в тот же индекс ПОСЛЕ clearHot().
    function test_control_setGroup_after_clearHot() public {
        _armSpan();
        liq.clearHot();
        (bool ok,) = _trySetGroup(2, mpB, dl);
        assertTrue(ok, "setGroup after clearHot must be accepted");
        (address orc,,,) = liq.groupInfo(2);
        assertEq(orc, mpB.oracle, "re-arm after clearHot must land");
    }

    /// §1 строка 5: setGroup в группу спана с ИСТЁКШИМ `until` (мёртвый указатель не запирает).
    function test_control_setGroup_expired_until() public {
        _armSpan();
        vm.warp(uint256(until) + 1);              // указатель протух, но группы (dl) ещё живы
        uint64 freshDl = uint64(block.timestamp + 900);
        (bool ok,) = _trySetGroup(2, mpB, freshDl);
        assertTrue(ok, "setGroup into span with EXPIRED until must be accepted (dead pointer)");
    }

    /// §1 строка 6 (ПОЗИТИВНЫЙ КОНТРОЛЬ): setTargets (докладка) в группу живого спана — ПРИНЯТА.
    function test_control_setTargets_into_live_span() public {
        _armSpan();
        (,,, uint8 n0) = liq.groupInfo(2);
        try liq.setTargets(2, n0, _oneTarget()) {
            (,,, uint8 n1) = liq.groupInfo(2);
            assertEq(n1, n0 + 1, "setTargets must extend n (cascade grows inside live window)");
        } catch {
            assertTrue(false,
                "POSITIVE CONTROL FAILED: setTargets (dokladka) into live span must NOT be blocked");
        }
    }

    /// Гард ПОЗИЦИОННЫЙ: даже setGroup ТЕМ ЖЕ рынком в живой спан ревертит (перезарядка требует
    /// clearHot). На базе — принят (красный); с правкой — HotSpanLive (зелёный).
    function test_setGroup_same_market_into_live_span_also_guarded() public {
        _armSpan();
        (bool ok, bytes4 sel) = _trySetGroup(2, mpA, dl);   // ТОТ ЖЕ рынок
        assertTrue(!ok, "DEFECT-RED: full re-arm (even same market) inside live span accepted");
        assertEq(sel, HOTSPANLIVE, "same-market setGroup into live span must revert HotSpanLive");
    }
}
