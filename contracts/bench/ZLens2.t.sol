// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;
import {Test, console2} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {LiquidatorV6} from "../src/LiquidatorV6.sol";
import {MarketParams} from "../src/Liquidator.sol";
import {MockERC20B} from "./Batch.t.sol";

interface ILiqCb { function onMorphoLiquidate(uint256, bytes calldata) external; }

contract CostlyMorpho2 {
    mapping(address => bool) public healthy;
    uint256 public collateralOut; uint256 public repaidIn; uint256 public burn; uint256 public sink;
    function set(uint256 c, uint256 r) external { collateralOut = c; repaidIn = r; }
    function setHealthy(address b, bool h) external { healthy[b] = h; }
    function setBurn(uint256 b) external { burn = b; }
    function _spin(uint256 amt) internal { uint256 g0 = gasleft(); uint256 s = sink;
        while (g0 - gasleft() < amt) { s = uint256(keccak256(abi.encodePacked(s))); } sink = s; }
    function liquidate(MarketParams memory mp, address borrower, uint256, uint256, bytes memory data)
        external returns (uint256, uint256) {
        if (healthy[borrower]) { _spin(burn); revert("position is healthy"); }
        _spin(burn);                       // реальный Morpho платит accrue+oracle и на успехе
        MockERC20B(mp.collateralToken).transfer(msg.sender, collateralOut);
        if (data.length > 0) ILiqCb(msg.sender).onMorphoLiquidate(repaidIn, data);
        MockERC20B(mp.loanToken).transferFrom(msg.sender, address(this), repaidIn);
        return (collateralOut, repaidIn);
    }
}

/// Своп с ЗАДАННОЙ ценой: боевой роут агрегатора стоит сотни тысяч, мок Batch.t.sol — единицы.
contract CostlySwapper {
    MockERC20B public coll; MockERC20B public loan; uint256 public rate; uint256 public burn; uint256 public sink;
    constructor(MockERC20B c, MockERC20B l, uint256 r, uint256 b) { coll = c; loan = l; rate = r; burn = b; }
    function swapAll() external {
        uint256 g0 = gasleft(); uint256 s = sink;
        while (g0 - gasleft() < burn) { s = uint256(keccak256(abi.encodePacked(s))); } sink = s;
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

contract ZLens2 is Test {
    MockERC20B loan; MockERC20B coll; CostlyMorpho2 morpho; CostlySwapper swapper;
    LiquidatorV6 liq; MarketParams mp; address leg = address(0x1EEE); uint64 dl;
    bytes32 constant EV_EXEC = keccak256("PlanExecuted(address,uint256)");
    bytes32 constant EV_FAIL = keccak256("PlanFailed(bytes4)");

    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new CostlyMorpho2();
        swapper = new CostlySwapper(coll, loan, 2e18, 180_000);   // полный огонь ~450k как на форке
        liq = new LiquidatorV6(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 100_000e18); loan.mint(address(swapper), 100_000_000e18);
        morpho.set(10e18, 15e18); morpho.setBurn(80_000);
        liq.setAuthorized(leg, true); dl = uint64(block.timestamp + 900);
    }
    function _b(uint256 i) internal pure returns (address) { return address(uint160(0xB0 + i)); }
    function _fat() internal pure returns (bytes memory) {
        return abi.encodePacked(abi.encodeWithSelector(CostlySwapper.swapAll.selector), new bytes(2048));
    }
    function _arm(uint256 n) internal {
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) ts[i] = LiquidatorV6.Target({
            borrower: _b(i), repaidShares: 1, seizedAssets: 0, swapTarget: address(swapper),
            swapCallData: _fat(), minProfit: 0, unwrapTarget: address(0), triggerPrice: 0});
        liq.setGroup(0, mp, dl, 0, ts);
    }
    function _start(uint256 n) internal view returns (uint256) {
        return uint256(keccak256(abi.encodePacked(leg))) % n; }
    function _probe(uint256 g) internal returns (bool ok) {
        vm.cool(address(liq)); vm.prank(leg);
        (ok, ) = address(liq).call{gas: g}(hex"01"); }
    function _count(Vm.Log[] memory logs) internal view returns (uint256 e, uint256 f) {
        for (uint256 i = 0; i < logs.length; i++) { if (logs[i].emitter != address(liq)) continue;
            if (logs[i].topics[0] == EV_EXEC) e++; else if (logs[i].topics[0] == EV_FAIL) f++; } }

    /// БОЕВАЯ СЦЕНА 15:04: голова кольца забрана соседом, 7 живых рядом, боевой роут 2 КБ,
    /// реальные цены отказа/огня. Проходим сеткой по лимиту ноги и смотрим ЧТО РЕАЛЬНО БЫВАЕТ.
    function test_combat_scene_grid() public {
        uint256 n = 8; _arm(n); uint256 s = _start(n);
        morpho.setHealthy(_b(s), true);
        uint256 bad; uint256 firstBad; uint256 firstWin; uint256 wins;
        for (uint256 g = 300_000; g <= 3_000_000; g += 20_000) {
            uint256 snap = vm.snapshotState();
            vm.recordLogs();
            bool ok = _probe(g);
            (uint256 e, ) = _count(vm.getRecordedLogs());
            if (!ok) { bad++; if (firstBad == 0) firstBad = g; }
            if (e > 0) { wins++; if (firstWin == 0) firstWin = g; }
            vm.revertToState(snap);
        }
        console2.log("grid [300k..3M step 20k], N =", uint256(136));
        console2.log("limits where the WHOLE probe reverted (leg pays FULL limit, no logs):", bad);
        console2.log("first such limit:", firstBad);
        console2.log("limits that banked >=1 liquidation:", wins);
        console2.log("first winning limit:", firstWin);
    }

    /// Точка боевого лимита 1 000 000 (замер 1: 141 из 142 tx) — что она даёт.
    function test_at_combat_limit_1M() public {
        uint256 n = 8; _arm(n); uint256 s = _start(n);
        morpho.setHealthy(_b(s), true);
        vm.recordLogs();
        bool ok = _probe(1_000_000);
        (uint256 e, uint256 f) = _count(vm.getRecordedLogs());
        console2.log("limit 1000000: ok =", ok);
        console2.log("  liquidations banked:", e);
        console2.log("  loud failures:", f);
        console2.log("  owner loanToken:", loan.balanceOf(address(this)));
    }

    /// ПОТЕРЯ УЖЕ ВЗЯТЫХ ДЕНЕГ: ищем лимит, на котором успех состоялся и был СНЕСЁН откатом.
    /// Признак — немонотонность: лимит L2 > L1, на L1 деньги взяты, на L2 tx ревертит целиком.
    function test_banked_success_can_be_reverted() public {
        uint256 n = 8; _arm(n); uint256 s = _start(n);
        morpho.setHealthy(_b(s), true);
        uint256 lowWin; uint256 higherRevert;
        for (uint256 g = 300_000; g <= 3_000_000; g += 10_000) {
            uint256 snap = vm.snapshotState();
            vm.recordLogs();
            bool ok = _probe(g);
            (uint256 e, ) = _count(vm.getRecordedLogs());
            uint256 bal = loan.balanceOf(address(this));
            vm.revertToState(snap);
            if (ok && e > 0 && bal > 0 && lowWin == 0) lowWin = g;
            if (!ok && lowWin != 0 && higherRevert == 0) higherRevert = g;
        }
        console2.log("smallest limit that BANKS money:", lowWin);
        console2.log("bigger limit that reverts the WHOLE tx (money would be lost):", higherRevert);
        assertEq(higherRevert, 0, "MORE gas => whole tx reverts: banked liquidation destroyed");
    }

    /// КАЛИБРОВКА СТЕНДА: сколько стоит ОДИН полный огонь через доску (форк-эталон 449 983).
    function test_calibration_single_fire_cost() public {
        _arm(1);
        vm.recordLogs();
        vm.cool(address(liq));
        vm.prank(leg);
        uint256 g0 = gasleft();
        (bool ok, ) = address(liq).call{gas: 5_000_000}(hex"01");
        uint256 used = g0 - gasleft();
        (uint256 e, ) = _count(vm.getRecordedLogs());
        console2.log("single full board-fire, cold 2KB route, gasUsed:", used);
        console2.log("  ok / banked:", ok, e);
        // и цена ОДНОГО провала «position is healthy» на том же роуте
        liq.clearGroup(0); _arm(1); morpho.setHealthy(_b(0), true);
        vm.cool(address(liq));
        vm.prank(leg);
        g0 = gasleft();
        address(liq).call{gas: 5_000_000}(hex"01");
        console2.log("single FAILED attempt (healthy), cold 2KB route, gasUsed:", g0 - gasleft());
    }

    /// БОЕВАЯ СЦЕНА КАСКАДА: мы берём ОДНУ жертву, остальные уже забраны конкурентами.
    /// Свип идёт дальше по забранным целям и может уронить РОДИТЕЛЬСКИЙ фрейм — вместе с
    /// уже взятыми деньгами.
    function test_banked_success_lost_in_cascade() public {
        uint256 n = 8; _arm(n); uint256 s = _start(n);
        for (uint256 i = 0; i < n; i++) morpho.setHealthy(_b(i), true);
        morpho.setHealthy(_b((s + 1) % n), false);      // ЖИВА ровно одна — следующая за головой
        uint256 lost; uint256 firstLost; uint256 won;
        for (uint256 g = 1_500_000; g <= 5_000_000; g += 10_000) {
            uint256 snap = vm.snapshotState();
            vm.recordLogs();
            bool ok = _probe(g);
            (uint256 e, ) = _count(vm.getRecordedLogs());
            uint256 bal = loan.balanceOf(address(this));
            vm.revertToState(snap);
            if (ok && e > 0 && bal > 0) won++;
            if (!ok) { lost++; if (firstLost == 0) firstLost = g; }
        }
        console2.log("cascade scene, grid [1.5M..5M step 10k]:");
        console2.log("  limits that banked the one live victim:", won);
        console2.log("  limits where the WHOLE tx reverted (victim + gas lost):", lost);
        console2.log("  first such limit:", firstLost);
        assertEq(lost, 0, "sweep over already-taken neighbours destroys the banked liquidation");
    }
}
