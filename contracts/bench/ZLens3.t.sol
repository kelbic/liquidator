// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;
import {Test, console2} from "forge-std/Test.sol";
import {LiquidatorV6} from "../src/LiquidatorV6.sol";
import {MarketParams} from "../src/Liquidator.sol";
import {MockERC20B} from "./Batch.t.sol";
import {CostlyMorpho2, CostlySwapper} from "./ZLens2.t.sol";

contract ZLens3 is Test {
    MockERC20B loan; MockERC20B coll; CostlyMorpho2 morpho; CostlySwapper swapper;
    LiquidatorV6 liq; MarketParams mp; address leg = address(0x1EEE); uint64 dl;
    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new CostlyMorpho2();
        swapper = new CostlySwapper(coll, loan, 2e18, 180_000);
        liq = new LiquidatorV6(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 100_000e18); loan.mint(address(swapper), 100_000_000e18);
        morpho.set(10e18, 15e18); morpho.setBurn(80_000);
        liq.setAuthorized(leg, true); dl = uint64(block.timestamp + 900);
    }
    function _b(uint256 i) internal pure returns (address) { return address(uint160(0xB0 + i)); }
    function _fat() internal pure returns (bytes memory) {
        return abi.encodePacked(abi.encodeWithSelector(CostlySwapper.swapAll.selector), new bytes(2048)); }
    function _arm(uint256 n) internal {
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) ts[i] = LiquidatorV6.Target({
            borrower: _b(i), repaidShares: 1, seizedAssets: 0, swapTarget: address(swapper),
            swapCallData: _fat(), minProfit: 0, unwrapTarget: address(0), triggerPrice: 0});
        liq.setGroup(0, mp, dl, 0, ts); }
    function _probe(uint256 g) internal returns (bool ok) {
        vm.cool(address(liq)); vm.prank(leg); (ok, ) = address(liq).call{gas: g}(hex"01"); }

    /// РОВНО боевой лимит 1 000 000 (замер 1: у 141 из 142 наших tx) на трёх сценах.
    function test_combat_limit_on_three_scenes() public {
        uint256 n = 8;
        // сцена A: ВСЕ цели забраны конкурентом (самый частый проигрыш)
        _arm(n); for (uint256 i = 0; i < n; i++) morpho.setHealthy(_b(i), true);
        uint256 g0 = gasleft(); bool okA = _probe(1_000_000); uint256 usedA = g0 - gasleft();
        console2.log("A: all taken   -> ok:", okA); console2.log("   gasUsed:", usedA);

        // сцена B: живы 4 из 8
        for (uint256 i = 0; i < n; i += 2) morpho.setHealthy(_b(i), false);
        g0 = gasleft(); bool okB = _probe(1_000_000); uint256 usedB = g0 - gasleft();
        console2.log("B: half alive  -> ok:", okB); console2.log("   gasUsed:", usedB);
        console2.log("   owner balance:", loan.balanceOf(address(this)));
    }

    /// А ЧТО ПРИ 3 000 000 (прежний лимит доски до #89)?
    function test_at_3M() public {
        uint256 n = 8; _arm(n);
        for (uint256 i = 0; i < n; i++) morpho.setHealthy(_b(i), true);
        uint256 g0 = gasleft(); bool ok = _probe(3_000_000); uint256 used = g0 - gasleft();
        console2.log("all taken, limit 3M -> ok:", ok); console2.log("   gasUsed:", used);
    }
}
