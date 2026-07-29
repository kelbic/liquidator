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
        uint256 profit = liq.liquidate(mp, borrower, uint256(borrowShares), address(swapper), swapData, 0, address(0));

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
        liq.liquidate(mp, borrower, uint256(borrowShares), address(swapper), swapData, 1_000_000e18, address(0));
    }
}

// --- unwrap-хоп (29.07): обёртка без DEX-ликвидности -> redeem -> своп ПОДЛЕЖАЩИМ ---

contract MockVault4626 {
    MockERC20 public immutable UNDER;
    mapping(address => uint256) public balanceOf;
    uint256 public rate;                                   // assets per share, 1e18
    constructor(MockERC20 u, uint256 r) { UNDER = u; rate = r; }
    function asset() external view returns (address) { return address(UNDER); }
    function mintShares(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address, uint256) external pure returns (bool) { return true; }
    function transfer(address to, uint256 amt) external returns (bool) {
        balanceOf[msg.sender] -= amt; balanceOf[to] += amt; return true;
    }
    function transferFrom(address f, address t, uint256 amt) external returns (bool) {
        balanceOf[f] -= amt; balanceOf[t] += amt; return true;
    }
    function redeem(uint256 shares, address receiver, address owner_) external returns (uint256 assets) {
        require(owner_ == msg.sender, "owner");
        balanceOf[owner_] -= shares;
        assets = shares * rate / 1e18;
        UNDER.mint(receiver, assets);
    }
}

/// @dev Своп, тратящий ПОДЛЕЖАЩИЙ токен (redeem обязан случиться до него, иначе нечего тратить).
contract UnderlyingSwapper {
    MockERC20 public immutable UNDER; MockERC20 public immutable LOAN; uint256 public rate;
    constructor(MockERC20 u, MockERC20 l, uint256 r) { UNDER = u; LOAN = l; rate = r; }
    function swapAll() external {
        uint256 amountIn = UNDER.balanceOf(msg.sender);
        require(amountIn > 0, "nothing unwrapped");
        UNDER.transferFrom(msg.sender, address(this), amountIn);
        LOAN.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

contract UnwrapForkTest is Test {
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    uint256 constant LLTV = 0.86e18;
    IMorphoFull morpho = IMorphoFull(MORPHO);
    MockERC20 loan; MockERC20 under; MockVault4626 vault; MockOracle oracle; MockIrm irm;
    UnderlyingSwapper uswapper;
    Liquidator liq;
    MarketParams mp; bytes32 id;
    address borrower = address(0xB0B2);

    function setUp() public {
        vm.createSelectFork(vm.envString("RPC_URL"));
        loan = new MockERC20(); under = new MockERC20(); oracle = new MockOracle(); irm = new MockIrm();
        vault = new MockVault4626(under, 1.25e18);         // 1 share = 1.25 подлежащего (как wsuperOETHb)

        address mOwner = morpho.owner();
        vm.startPrank(mOwner);
        try morpho.enableIrm(address(irm)) {} catch {}
        try morpho.enableLltv(LLTV) {} catch {}
        vm.stopPrank();

        mp = MarketParams({loanToken: address(loan), collateralToken: address(vault),
                           oracle: address(oracle), irm: address(irm), lltv: LLTV});
        id = keccak256(abi.encode(mp));
        oracle.setPrice(2e36);
        morpho.createMarket(mp);

        loan.mint(address(this), 1_000_000e18);
        loan.approve(MORPHO, type(uint256).max);
        morpho.supply(mp, 500_000e18, 0, address(this), "");

        vault.mintShares(borrower, 100e18);
        vm.startPrank(borrower);
        vault.approve(MORPHO, type(uint256).max);
        morpho.supplyCollateral(mp, 100e18, borrower, "");
        morpho.borrow(mp, 170e18, 0, borrower, borrower);
        vm.stopPrank();

        liq = new Liquidator(MORPHO);
        // подлежащего на 1 шер = 1.25; курс свопа 1.5 loan за подлежащий => 1 шер ~ 1.875 loan
        uswapper = new UnderlyingSwapper(under, loan, 1.5e18);
        loan.mint(address(uswapper), 1_000_000e18);
    }

    /// Полный флоу: seize обёртки -> redeem -> своп подлежащего -> repay -> профит владельцу.
    function testLiquidateWithUnwrap() public {
        oracle.setPrice(1.8e36);
        (, uint128 borrowShares,) = morpho.position(id, borrower);
        uint256 ownerBefore = loan.balanceOf(address(this));

        uint256 profit = liq.liquidate(
            mp, borrower, uint256(borrowShares), address(uswapper),
            abi.encodeWithSelector(UnderlyingSwapper.swapAll.selector), 0, address(vault));

        (, uint128 sharesAfter,) = morpho.position(id, borrower);
        assertGt(profit, 0, "no profit");
        assertEq(sharesAfter, 0, "debt not cleared");
        assertEq(loan.balanceOf(address(this)) - ownerBefore, profit, "profit not swept");
        assertEq(vault.balanceOf(address(liq)), 0, "wrapper stuck");
        assertEq(under.balanceOf(address(liq)), 0, "underlying stuck");
    }

    /// Без unwrap тот же рынок падает в свопе (нечего тратить) — redeem действительно load-bearing.
    function testWithoutUnwrapSwapStarves() public {
        oracle.setPrice(1.8e36);
        (, uint128 borrowShares,) = morpho.position(id, borrower);
        vm.expectRevert();                                 // SwapFailed: у свопа нет подлежащего
        liq.liquidate(mp, borrower, uint256(borrowShares), address(uswapper),
                      abi.encodeWithSelector(UnderlyingSwapper.swapAll.selector), 0, address(0));
    }
}

/// @dev НАСТОЯЩИЙ wsuperOETHb на форке Base: redeem из нашего контракта отдаёт superOETHb.
contract RealWsuperOETHbTest is Test {
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address constant WSUPER = 0x7FcD174E80f264448ebeE8c88a7C4476AAF58Ea6;
    address constant SUPER  = 0xDBFeFD2e8460a6Ee4955A68582F85708BAEA60A3;

    function setUp() public {
        vm.createSelectFork(vm.envString("RPC_URL"));
    }

    /// asset() совпадает с superOETHb; redeem живой обёртки из контракта-держателя работает,
    /// подлежащее приходит держателю (лестница Kyber 29.07: superOETHb->WETH бездонный).
    function testRealRedeemFromContract() public {
        Liquidator liq = new Liquidator(MORPHO);
        assertEq(Liquidator(liq).MORPHO(), MORPHO);
        assertEq(IERC4626View(WSUPER).asset(), SUPER, "asset() != superOETHb");

        uint256 amt = 5e18;                                // ~5 шеров (скромно против supply 2080)
        vm.prank(MORPHO);                                  // Morpho держит залоги реальных позиций
        IERC20View(WSUPER).transfer(address(liq), amt);
        assertEq(IERC20View(WSUPER).balanceOf(address(liq)), amt);

        uint256 underBefore = IERC20View(SUPER).balanceOf(address(liq));
        vm.prank(address(liq));
        uint256 got = IERC4626View(WSUPER).redeem(amt, address(liq), address(liq));
        assertGt(got, amt, "rate must be > 1 (rebase accrual)");
        assertEq(IERC20View(SUPER).balanceOf(address(liq)) - underBefore, got, "underlying not received");
        assertEq(IERC20View(WSUPER).balanceOf(address(liq)), 0, "shares not burned");
    }
}

interface IERC4626View {
    function asset() external view returns (address);
    function redeem(uint256, address, address) external returns (uint256);
}

interface IERC20View {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
}
