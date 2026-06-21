// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test, console} from "forge-std/Test.sol";
import {Liquidator, MarketParams} from "../src/Liquidator.sol";

struct Market {
    uint128 totalSupplyAssets; uint128 totalSupplyShares;
    uint128 totalBorrowAssets; uint128 totalBorrowShares;
    uint128 lastUpdate; uint128 fee;
}

interface IMorphoFull {
    function owner() external view returns (address);
    function enableIrm(address irm) external;
    function enableLltv(uint256 lltv) external;
    function createMarket(MarketParams memory mp) external;
    function supply(MarketParams memory mp, uint256 assets, uint256 shares, address onBehalf, bytes memory data) external returns (uint256, uint256);
    function supplyCollateral(MarketParams memory mp, uint256 assets, address onBehalf, bytes memory data) external;
    function borrow(MarketParams memory mp, uint256 assets, uint256 shares, address onBehalf, address receiver) external returns (uint256, uint256);
    function position(bytes32 id, address user) external view returns (uint256 supplyShares, uint128 borrowShares, uint128 collateral);
}

contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address sp, uint256 amt) external returns (bool) { allowance[msg.sender][sp] = amt; return true; }
    function transfer(address to, uint256 amt) external returns (bool) { balanceOf[msg.sender] -= amt; balanceOf[to] += amt; return true; }
    function transferFrom(address f, address t, uint256 amt) external returns (bool) {
        uint256 a = allowance[f][msg.sender];
        if (a != type(uint256).max) allowance[f][msg.sender] = a - amt;
        balanceOf[f] -= amt; balanceOf[t] += amt; return true;
    }
}

contract MockOracle {
    uint256 public price;
    function setPrice(uint256 p) external { price = p; }
}

contract MockIrm {
    function borrowRate(MarketParams memory, Market memory) external pure returns (uint256) { return 0; }
    function borrowRateView(MarketParams memory, Market memory) external pure returns (uint256) { return 0; }
}

contract MockSwapper {
    MockERC20 public coll; MockERC20 public loan; uint256 public rate; // loan per coll, 1e18
    constructor(MockERC20 c, MockERC20 l, uint256 r) { coll = c; loan = l; rate = r; }
    function swapAll() external {
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

contract LiquidatorForkTest is Test {
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    uint256 constant LLTV = 0.86e18;
    IMorphoFull morpho = IMorphoFull(MORPHO);
    MockERC20 loan; MockERC20 coll; MockOracle oracle; MockIrm irm; MockSwapper swapper;
    Liquidator liq;
    MarketParams mp; bytes32 id;
    address borrower = address(0xB0B);

    function setUp() public {
        vm.createSelectFork(vm.envString("RPC_URL"));
        loan = new MockERC20(); coll = new MockERC20(); oracle = new MockOracle(); irm = new MockIrm();

        address mOwner = morpho.owner();
        vm.startPrank(mOwner);
        try morpho.enableIrm(address(irm)) {} catch {}
        try morpho.enableLltv(LLTV) {} catch {}
        vm.stopPrank();

        mp = MarketParams({loanToken: address(loan), collateralToken: address(coll), oracle: address(oracle), irm: address(irm), lltv: LLTV});
        id = keccak256(abi.encode(mp));

        oracle.setPrice(2e36);                 // 1 coll = 2 loan
        morpho.createMarket(mp);

        loan.mint(address(this), 1_000_000e18);
        loan.approve(MORPHO, type(uint256).max);
        morpho.supply(mp, 500_000e18, 0, address(this), "");

        coll.mint(borrower, 100e18);
        vm.startPrank(borrower);
        coll.approve(MORPHO, type(uint256).max);
        morpho.supplyCollateral(mp, 100e18, borrower, "");
        morpho.borrow(mp, 170e18, 0, borrower, borrower);   // max 172 at price 2, lltv 0.86
        vm.stopPrank();

        liq = new Liquidator(MORPHO);          // owner = this
        swapper = new MockSwapper(coll, loan, 1.8e18);  // swap at post-drop price
        loan.mint(address(swapper), 1_000_000e18);
    }

    function testLiquidate() public {
        oracle.setPrice(1.8e36);               // 100 coll = 180 loan, max 154.8 < 170 -> liquidatable
        (, uint128 borrowShares,) = morpho.position(id, borrower);
        assertGt(borrowShares, 0, "no debt");

        uint256 ownerBefore = loan.balanceOf(address(this));
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        uint256 profit = liq.liquidate(mp, borrower, uint256(borrowShares), address(swapper), swapData, 0);

        (, uint128 sharesAfter,) = morpho.position(id, borrower);
        console.log("profit (loan wei):", profit);
        console.log("borrowShares after:", sharesAfter);
        assertGt(profit, 0, "no profit");
        assertEq(sharesAfter, 0, "debt not cleared");
        assertEq(loan.balanceOf(address(this)) - ownerBefore, profit, "profit not swept");
    }

    function testRevertsIfMinProfitTooHigh() public {
        oracle.setPrice(1.8e36);
        (, uint128 borrowShares,) = morpho.position(id, borrower);
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        vm.expectRevert();                     // ProfitTooLow
        liq.liquidate(mp, borrower, uint256(borrowShares), address(swapper), swapData, 1_000_000e18);
    }
}
