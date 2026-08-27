// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// АДВЕРСАРНАЯ ЛИНЗА на свип (не входит в репозиторий) — /tmp only.
import {Test, console2} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {LiquidatorV6} from "../src/LiquidatorV6.sol";
import {MarketParams} from "../src/Liquidator.sol";
import {MockERC20B, MockMorpho, MockSwapperB, GasBurner} from "./Batch.t.sol";

/// Morpho, у которого ОТКАЗ «здоров» СТОИТ ГАЗА (реальный Morpho жжёт accrueInterest + oracle
/// перед revert; мок в суите ревертит на первом SLOAD и потому TRY-гейт 120k не проверяет).
contract CostlyMorpho {
    mapping(address => bool) public healthy;
    uint256 public collateralOut;
    uint256 public repaidIn;
    uint256 public burn;            // сколько газа сжечь ДО revert
    uint256 public sink;
    function set(uint256 cOut, uint256 rIn) external { collateralOut = cOut; repaidIn = rIn; }
    function setHealthy(address b, bool h) external { healthy[b] = h; }
    function setBurn(uint256 b) external { burn = b; }
    function liquidate(MarketParams memory mp, address borrower, uint256, uint256, bytes memory data)
        external returns (uint256, uint256)
    {
        if (healthy[borrower]) {
            uint256 g0 = gasleft();
            uint256 s = sink;
            while (g0 - gasleft() < burn) { s = uint256(keccak256(abi.encodePacked(s))); }
            sink = s;               // не оптимизируется прочь
            revert("position is healthy");
        }
        MockERC20B(mp.collateralToken).transfer(msg.sender, collateralOut);
        if (data.length > 0) ILiqCb(msg.sender).onMorphoLiquidate(repaidIn, data);
        MockERC20B(mp.loanToken).transferFrom(msg.sender, address(this), repaidIn);
        return (collateralOut, repaidIn);
    }
}
interface ILiqCb { function onMorphoLiquidate(uint256, bytes calldata) external; }

contract ZLens is Test {
    MockERC20B loan; MockERC20B coll; CostlyMorpho morpho; MockSwapperB swapper;
    LiquidatorV6 liq;
    MarketParams mp;
    address leg = address(0x1EEE);
    uint64 dl;

    bytes32 constant EV_EXEC = keccak256("PlanExecuted(address,uint256)");
    bytes32 constant EV_FAIL = keccak256("PlanFailed(bytes4)");

    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new CostlyMorpho(); swapper = new MockSwapperB(coll, loan, 2e18);
        liq = new LiquidatorV6(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 100_000e18);
        loan.mint(address(swapper), 100_000_000e18);
        morpho.set(10e18, 15e18);
        liq.setAuthorized(leg, true);
        dl = uint64(block.timestamp + 900);
    }

    function _borrower(uint256 i) internal pure returns (address) { return address(uint160(0xB0 + i)); }
    function _start(uint256 n) internal view returns (uint256) {
        return uint256(keccak256(abi.encodePacked(leg))) % n;
    }
    /// боевой роут: 4 байта селектора + 2048 байт хвоста (как V5Plan._fatSwapData)
    function _fat() internal pure returns (bytes memory) {
        return abi.encodePacked(abi.encodeWithSelector(MockSwapperB.swapAll.selector), new bytes(2048));
    }
    function _thin() internal pure returns (bytes memory) {
        return abi.encodeWithSelector(MockSwapperB.swapAll.selector);
    }
    function _armRoutes(bytes memory route, uint256 n) internal {
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) {
            ts[i] = LiquidatorV6.Target({
                borrower: _borrower(i), repaidShares: 1, seizedAssets: 0,
                swapTarget: address(swapper), swapCallData: route,
                minProfit: 0, unwrapTarget: address(0), triggerPrice: 0
            });
        }
        liq.setGroup(0, mp, dl, 0, ts);
    }
    /// БОЕВАЯ проба = СВЕЖАЯ tx: слоты плана ХОЛОДНЫЕ (2100/SLOAD). Без vm.cool стенд меряет
    /// тёплый сторадж (100/SLOAD) — режим, которого в бою не бывает. Ср. V5Plan.t.sol:_probe.
    function _probe(uint256 gas_) internal returns (bool ok) {
        vm.cool(address(liq));
        vm.prank(leg);
        (ok, ) = address(liq).call{gas: gas_}(hex"01");
    }
    function _count(Vm.Log[] memory logs) internal view returns (uint256 exec, uint256 fail) {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != address(liq)) continue;
            if (logs[i].topics[0] == EV_EXEC) exec++;
            else if (logs[i].topics[0] == EV_FAIL) fail++;
        }
    }

    // ==================================================================================
    // L1. ЦЕНА КОПИИ БОЕВОГО РОУТА ИЗ ХОЛОДНОГО СТОРАДЖА vs SWEEP_MIN_TRY_GAS=120k
    // ==================================================================================
    function test_L1_copy_cost_vs_try_gate() public {
        // одна цель, ЗДОРОВА и её отказ бесплатен -> весь расход = преамбула + копия + вызов
        morpho.setBurn(0);
        _armRoutes(_thin(), 1);
        morpho.setHealthy(_borrower(0), true);
        vm.cool(address(liq));
        uint256 g0 = gasleft(); _probe(5_000_000); uint256 gThin = g0 - gasleft();

        liq.clearGroup(0);
        _armRoutes(_fat(), 1);
        morpho.setHealthy(_borrower(0), true);
        vm.cool(address(liq));
        g0 = gasleft(); _probe(5_000_000); uint256 gFat = g0 - gasleft();

        console2.log("probe with 4-byte route  :", gThin);
        console2.log("probe with 2052-byte route:", gFat);
        console2.log("delta = cold copy of combat route:", gFat - gThin);
        console2.log("SWEEP_MIN_TRY_GAS        :", uint256(120_000));
    }

    // ==================================================================================
    // L2. TRY-ГЕЙТ НЕ ДЕРЖИТ КОПИЮ: гейт пройден, копия не влезла -> ВСЯ tx в откат
    // ==================================================================================
    function test_L2_parent_oog_inside_copy() public {
        morpho.setBurn(0);
        _armRoutes(_fat(), 8);
        for (uint256 i = 0; i < 8; i++) morpho.setHealthy(_borrower(i), true);
        // прогоняем лимит ноги по сетке: ищем значения, на которых ВСЯ проба ревертит
        uint256 bad;
        uint256 firstBad;
        for (uint256 g = 300_000; g <= 3_000_000; g += 10_000) {
            uint256 snap = vm.snapshotState();
            bool ok = _probe(g);
            if (!ok) { bad++; if (firstBad == 0) firstBad = g; }
            vm.revertToState(snap);
        }
        console2.log("gas limits in [300k..3M] step 10k where the WHOLE probe reverts:", bad);
        console2.log("first such limit:", firstBad);
        assertEq(bad, 0, "TRY-gate does not bound the parent frame: probe reverts wholesale");
    }

    // ==================================================================================
    // L3. То же с ПЛАТНЫМ отказом Morpho (реальный «position is healthy» стоит газа)
    // ==================================================================================
    function test_L3_parent_oog_costly_healthy() public {
        morpho.setBurn(80_000);            // реальный Morpho: accrue + oracle перед revert
        _armRoutes(_fat(), 8);
        for (uint256 i = 0; i < 8; i++) morpho.setHealthy(_borrower(i), true);
        uint256 bad; uint256 firstBad;
        for (uint256 g = 300_000; g <= 3_000_000; g += 10_000) {
            uint256 snap = vm.snapshotState();
            bool ok = _probe(g);
            if (!ok) { bad++; if (firstBad == 0) firstBad = g; }
            vm.revertToState(snap);
        }
        console2.log("costly-healthy: limits where the WHOLE probe reverts:", bad);
        console2.log("first such limit:", firstBad);
        assertEq(bad, 0, "TRY-gate does not bound the parent frame (costly revert)");
    }

    // ==================================================================================
    // L4. ЖОР СОСЕДА НА НИЗКОМ ОСТАТКЕ: 1/64 от 120k = 1875 газа < ~6k пути после catch
    // ==================================================================================
    function test_L4_burner_at_low_gasleft_kills_whole_tx() public {
        GasBurner burner = new GasBurner();
        morpho.setBurn(0);
        uint256 n = 8;
        uint256 s = _start(n);
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) {
            bool isBurner = (i == (s + 1) % n);
            ts[i] = LiquidatorV6.Target({
                borrower: _borrower(i), repaidShares: 1, seizedAssets: 0,
                swapTarget: isBurner ? address(burner) : address(swapper),
                swapCallData: isBurner ? abi.encodeWithSelector(GasBurner.burn.selector) : _thin(),
                minProfit: 0, unwrapTarget: address(0), triggerPrice: 0
            });
        }
        liq.setGroup(0, mp, dl, 0, ts);
        morpho.setHealthy(_borrower(s), true);    // голова забрана соседом -> идём на жора

        uint256 bad; uint256 firstBad; uint256 lastBad;
        for (uint256 g = 130_000; g <= 900_000; g += 5_000) {
            uint256 snap = vm.snapshotState();
            bool ok = _probe(g);
            if (!ok) { bad++; if (firstBad == 0) firstBad = g; lastBad = g; }
            vm.revertToState(snap);
        }
        console2.log("burner: limits in [130k..900k] step 5k where WHOLE probe reverts:", bad);
        console2.log("first:", firstBad, " last:", lastBad);
        assertEq(bad, 0, "child OOG at low gasleft takes the parent frame with it");
    }
}
