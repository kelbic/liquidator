// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// v4 (каскадный тир, «го» kelbic 07.08): authorized-set пускает залповые EOA в
// liquidate()/batchLiquidate(), но профит ВСЕГДА уходит owner'у. Тесты — форк на
// реальный Morpho (как Liquidator.t.sol): чужой ревертит, authorized стреляет,
// профит owner'у (не стрелявшему EOA), деавторизация закрывает обратно.

import {Test} from "forge-std/Test.sol";
import {Liquidator, MarketParams, LiquidationItem} from "../src/Liquidator.sol";

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
    MockERC20 public coll; MockERC20 public loan; uint256 public rate;
    constructor(MockERC20 c, MockERC20 l, uint256 r) { coll = c; loan = l; rate = r; }
    function swapAll() external {
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

/// @dev Злой swapTarget скомпрометированного залпового ключа: тянет одобренный залог и НЕ
/// отдаёт loan — кража обязана лечь ревертом (CannotRepay / checked-math профита).
contract EvilSwapper {
    MockERC20 public coll;
    constructor(MockERC20 c) { coll = c; }
    function swapAll() external {
        coll.transferFrom(msg.sender, address(this), coll.balanceOf(msg.sender));
    }
}

contract V4AuthForkTest is Test {
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    uint256 constant LLTV = 0.86e18;
    IMorphoFull morpho = IMorphoFull(MORPHO);
    MockERC20 loan; MockERC20 coll; MockOracle oracle; MockIrm irm; MockSwapper swapper;
    Liquidator liq;
    MarketParams mp; bytes32 id;
    address borrower = address(0xB0B4);
    address volley = address(0xEA01);          // залповый EOA
    address stranger = address(0xBAD1);

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
        oracle.setPrice(2e36);
        morpho.createMarket(mp);

        loan.mint(address(this), 1_000_000e18);
        loan.approve(MORPHO, type(uint256).max);
        morpho.supply(mp, 500_000e18, 0, address(this), "");

        coll.mint(borrower, 100e18);
        vm.startPrank(borrower);
        coll.approve(MORPHO, type(uint256).max);
        morpho.supplyCollateral(mp, 100e18, borrower, "");
        morpho.borrow(mp, 170e18, 0, borrower, borrower);
        vm.stopPrank();

        liq = new Liquidator(MORPHO);          // owner = this
        swapper = new MockSwapper(coll, loan, 1.8e18);
        loan.mint(address(swapper), 1_000_000e18);
    }

    function _drop() internal returns (uint128 borrowShares) {
        oracle.setPrice(1.8e36);
        (, borrowShares,) = morpho.position(id, borrower);
        assertGt(borrowShares, 0, "no debt");
    }

    function testStrangerReverts() public {
        uint128 bs = _drop();
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        vm.prank(stranger);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liq.liquidate(mp, borrower, uint256(bs), address(swapper), swapData, 0, address(0));

        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = LiquidationItem(mp, borrower, uint256(bs), address(swapper), swapData, 0, address(0));
        vm.prank(stranger);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liq.batchLiquidate(items);
    }

    function testSetAuthorizedOnlyOwner() public {
        vm.prank(stranger);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liq.setAuthorized(stranger, true);
    }

    function testVolleyFiresProfitToOwner() public {
        liq.setAuthorized(volley, true);
        uint128 bs = _drop();
        uint256 ownerBefore = loan.balanceOf(address(this));
        uint256 volleyBefore = loan.balanceOf(volley);
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        vm.prank(volley);
        uint256 profit = liq.liquidate(mp, borrower, uint256(bs), address(swapper), swapData, 0, address(0));
        assertGt(profit, 0, "no profit");
        assertEq(loan.balanceOf(address(this)) - ownerBefore, profit, "profit must sweep to OWNER");
        assertEq(loan.balanceOf(volley), volleyBefore, "volley EOA must get nothing");
        (, uint128 after_,) = morpho.position(id, borrower);
        assertEq(after_, 0, "debt not cleared");
    }

    function testVolleyBatchFires() public {
        liq.setAuthorized(volley, true);
        uint128 bs = _drop();
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = LiquidationItem(mp, borrower, uint256(bs), address(swapper), swapData, 0, address(0));
        uint256 ownerBefore = loan.balanceOf(address(this));
        vm.prank(volley);
        uint256 nOk = liq.batchLiquidate(items);
        assertEq(nOk, 1, "batch item failed");
        assertGt(loan.balanceOf(address(this)), ownerBefore, "profit must sweep to OWNER");
    }

    function testDeauthorizeClosesGate() public {
        liq.setAuthorized(volley, true);
        liq.setAuthorized(volley, false);
        uint128 bs = _drop();
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        vm.prank(volley);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liq.liquidate(mp, borrower, uint256(bs), address(swapper), swapData, 0, address(0));
    }

    function testCompromisedVolleyCannotSteal() public {
        liq.setAuthorized(volley, true);
        uint128 bs = _drop();
        EvilSwapper evil = new EvilSwapper(coll);
        bytes memory swapData = abi.encodeWithSelector(EvilSwapper.swapAll.selector);
        vm.prank(volley);
        vm.expectRevert();                     // CannotRepay: своп не вернул loan
        liq.liquidate(mp, borrower, uint256(bs), address(evil), swapData, 0, address(0));
        assertEq(coll.balanceOf(address(evil)), 0, "collateral must not leave on revert");
    }
}
