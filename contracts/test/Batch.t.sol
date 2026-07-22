// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// Боевые тесты batchLiquidate на РЕАЛЬНОМ src/Liquidator.sol — порт верифицированного прототипа
// BatchProto.t.sol (10/10 PASS 07-17, дизайн в docs/BATCH_LIQUIDATE_PLAN.md). Отличия от прототипа:
// C1 (self-call ревертит на onlyOwner) устарел by design — заменён явными auth-тестами;
// C2 (batchLiquidateGuarded) был пробой дизайна, в боевом контракте guarded-варианта нет.
import {Test, console} from "forge-std/Test.sol";
import {Liquidator, MarketParams, LiquidationItem, IMorpho, IERC20} from "../src/Liquidator.sol";

// ---- mocks (как в прототипе) -----------------------------------------------

contract MockERC20B {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address sp, uint256 amt) external returns (bool) { allowance[msg.sender][sp] = amt; return true; }
    function transfer(address to, uint256 amt) external returns (bool) { balanceOf[msg.sender] -= amt; balanceOf[to] += amt; return true; }
    function transferFrom(address f, address t, uint256 amt) external returns (bool) {
        allowance[f][msg.sender] -= amt;
        balanceOf[f] -= amt; balanceOf[t] += amt; return true;
    }
}

interface ILiqCallback { function onMorphoLiquidate(uint256, bytes calldata) external; }

/// Mimics Morpho Blue liquidate flow: push collateral to caller, callback, pull loanToken.
contract MockMorpho {
    mapping(address => bool) public healthy;
    uint256 public collateralOut;
    uint256 public repaidIn;
    function set(uint256 cOut, uint256 rIn) external { collateralOut = cOut; repaidIn = rIn; }
    function setHealthy(address b, bool h) external { healthy[b] = h; }

    function liquidate(MarketParams memory mp, address borrower, uint256, uint256, bytes memory data)
        external returns (uint256, uint256)
    {
        require(!healthy[borrower], "position is healthy");
        MockERC20B(mp.collateralToken).transfer(msg.sender, collateralOut);
        if (data.length > 0) ILiqCallback(msg.sender).onMorphoLiquidate(repaidIn, data);
        MockERC20B(mp.loanToken).transferFrom(msg.sender, address(this), repaidIn);
        return (collateralOut, repaidIn);
    }
}

contract MockSwapperB {
    MockERC20B public coll; MockERC20B public loan; uint256 public rate;
    constructor(MockERC20B c, MockERC20B l, uint256 r) { coll = c; loan = l; rate = r; }
    function swapAll() external {
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

contract GasBurner {
    function burn() external pure { while (true) {} } // OOGs the subcall
}

/// Злой «роутер»: из swap-вызова onMorphoLiquidate пытается ре-энтернуть liquidate/batch.
contract Reenterer {
    Liquidator public lq; MarketParams mp;
    bool public reenteredLiquidate; bool public reenteredBatch;
    constructor(Liquidator l, MarketParams memory m) { lq = l; mp = m; }
    function attack() external {
        try lq.liquidate(mp, address(0xdead), 1, address(this), "", 0) returns (uint256) {
            reenteredLiquidate = true;
        } catch {}
        LiquidationItem[] memory items = new LiquidationItem[](0);
        try lq.batchLiquidate(items) returns (uint256) {
            reenteredBatch = true;
        } catch {}
    }
}

// ---- tests -----------------------------------------------------------------

contract BatchTest is Test {
    MockERC20B loan; MockERC20B coll; MockMorpho morpho; MockSwapperB swapper;
    Liquidator lq;
    MarketParams mp;
    address borrower1 = address(0xB1);
    address borrower2 = address(0xB2);
    address rando = address(0xBAD);

    event Liquidated(address indexed borrower, address indexed loanToken, uint256 profit);
    event BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason);

    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new MockMorpho(); swapper = new MockSwapperB(coll, loan, 2e18);
        lq = new Liquidator(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 1_000e18);
        loan.mint(address(swapper), 1_000_000e18);
        morpho.set(10e18, 15e18); // seize 10 coll -> swap to 20 loan, repay 15 -> profit 5
    }

    function _item(address b, uint256 minP) internal view returns (LiquidationItem memory) {
        return LiquidationItem(mp, b, 1, address(swapper), abi.encodeWithSelector(MockSwapperB.swapAll.selector), minP);
    }

    // --- auth (замена C1 прототипа: теперь self ДОЛЖЕН проходить, чужой — нет) ---

    function testAuth_RandomCannotLiquidate() public {
        vm.prank(rando);
        vm.expectRevert(Liquidator.NotOwner.selector);
        lq.liquidate(mp, borrower1, 1, address(swapper), "", 0);
    }

    function testAuth_RandomCannotBatch() public {
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = _item(borrower1, 0);
        vm.prank(rando);
        vm.expectRevert(Liquidator.NotOwner.selector);
        lq.batchLiquidate(items);
    }

    // --- одиночный путь: регресс бит-в-бит (селектор/поведение не менялись) ---

    function testSinglePathStillWorks() public {
        uint256 profit = lq.liquidate(
            mp, borrower1, 1, address(swapper),
            abi.encodeWithSelector(MockSwapperB.swapAll.selector), 0
        );
        assertEq(profit, 5e18);
        assertEq(loan.balanceOf(address(this)), 5e18); // swept to owner
    }

    // C1b: батч через self-call работает, профит уходит владельцу.
    function testC1b_BatchSelfCallWorks() public {
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = _item(borrower1, 0);
        uint256 nOk = lq.batchLiquidate(items);
        assertEq(nOk, 1);
        assertEq(loan.balanceOf(address(this)), 5e18);
    }

    // C2b: реентранси роутером отбита (снаружи item умирает CannotRepay, вход не состоялся).
    function testC2b_RouterCannotReenter() public {
        Reenterer r = new Reenterer(lq, mp);
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = LiquidationItem(mp, borrower1, 1, address(r), abi.encodeWithSelector(Reenterer.attack.selector), 0);
        uint256 nOk = lq.batchLiquidate(items);
        assertEq(r.reenteredLiquidate(), false);
        assertEq(r.reenteredBatch(), false);
        assertEq(nOk, 0);
    }

    // C3: Error(string) Morpho («position is healthy») ловится, батч продолжается.
    function testC3_StringRevertCaughtBatchContinues() public {
        morpho.setHealthy(borrower1, true);
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 0);
        items[1] = _item(borrower2, 0);
        vm.expectEmit(true, true, false, true);
        emit BatchItemFailed(0, borrower1, bytes4(0x08c379a0)); // Error(string)
        uint256 nOk = lq.batchLiquidate(items);
        assertEq(nOk, 1);
        assertEq(loan.balanceOf(address(this)), 5e18);
    }

    // C3b: OOG подвызова ловится (63/64 запас), батч продолжается.
    function testC3b_OOGSubcallCaughtBatchContinues() public {
        GasBurner gb = new GasBurner();
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = LiquidationItem(mp, borrower1, 1, address(gb), abi.encodeWithSelector(GasBurner.burn.selector), 0);
        items[1] = _item(borrower2, 0);
        uint256 nOk = lq.batchLiquidate{gas: 30_000_000}(items);
        assertEq(nOk, 1, "item2 should still succeed after item1 OOG");
        assertEq(loan.balanceOf(address(this)), 5e18);
    }

    // C3c: тесный газ МОЛЧА морит хвост items (tx успешна, nOk<N) — политика газа ×1.25 обязательна.
    function testC3c_TightGasStarvesLaterItems() public {
        GasBurner gb = new GasBurner();
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = LiquidationItem(mp, borrower1, 1, address(gb), abi.encodeWithSelector(GasBurner.burn.selector), 0);
        items[1] = _item(borrower2, 0);
        uint256 nOk = lq.batchLiquidate{gas: 3_000_000}(items);
        console.log("nOk at 3M gas:", nOk);
        assertEq(nOk, 0, "item2 starved by item1 burner");
    }

    // C4: два item с одним loanToken — per-item sweep-all, двойного счёта нет.
    function testC4_TwoItemsSameLoanTokenAccounting() public {
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 5e18);
        items[1] = _item(borrower2, 5e18);
        vm.expectEmit(true, true, false, true);
        emit Liquidated(borrower1, address(loan), 5e18);
        vm.expectEmit(true, true, false, true);
        emit Liquidated(borrower2, address(loan), 5e18);
        uint256 nOk = lq.batchLiquidate(items);
        assertEq(nOk, 2);
        assertEq(loan.balanceOf(address(lq)), 0);
        assertEq(loan.balanceOf(address(this)), 10e18);
    }

    // C6: allowance не течёт между items.
    function testC6_NoAllowanceLeak() public {
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 0);
        items[1] = _item(borrower2, 0);
        lq.batchLiquidate(items);
        assertEq(coll.allowance(address(lq), address(swapper)), 0);
        assertEq(loan.allowance(address(lq), address(morpho)), 0);
    }

    // Кастомный селектор (ProfitTooLow) всплывает в BatchItemFailed.reason.
    function testCustomErrorSelectorSurfaces() public {
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = _item(borrower1, 100e18);
        vm.expectEmit(true, true, false, true);
        emit BatchItemFailed(0, borrower1, Liquidator.ProfitTooLow.selector);
        uint256 nOk = lq.batchLiquidate(items);
        assertEq(nOk, 0);
    }
}
