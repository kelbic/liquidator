// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// REFERENCE PROTOTYPE для docs/BATCH_LIQUIDATE_PLAN.md — промоутнуть в src/ при сборке (10/10 PASS 07-17).
import {Test, console} from "forge-std/Test.sol";
import {Liquidator, MarketParams, IMorpho, IERC20} from "../src/Liquidator.sol";

// ---- mocks -----------------------------------------------------------------

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
    // borrower => healthy? (healthy -> revert like Morpho's require string)
    mapping(address => bool) public healthy;
    uint256 public collateralOut;  // seized per call
    uint256 public repaidIn;       // pulled per call
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

// ---- the proposed design, verbatim ----------------------------------------

struct LiquidationItem {
    MarketParams mp;
    address borrower;
    uint256 repaidShares;
    address swapTarget;
    bytes swapCallData;
    uint256 minProfit;
}

contract LiquidatorBatch {
    address public immutable MORPHO;
    address public owner;
    uint256 private _locked = 1;

    struct SwapData { address swapTarget; bytes swapCallData; address loanToken; address collateralToken; }

    error NotOwner(); error NotMorpho(); error Reentrant(); error SwapFailed();
    error CannotRepay(); error ProfitTooLow(uint256 got, uint256 min); error ERC20OpFailed();

    event Liquidated(address indexed borrower, address indexed loanToken, uint256 profit);
    event BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason);

    modifier onlyOwner() { if (msg.sender != owner) revert NotOwner(); _; }
    modifier onlyOwnerOrSelf() { if (msg.sender != owner && msg.sender != address(this)) revert NotOwner(); _; }
    modifier nonReentrant() { if (_locked == 2) revert Reentrant(); _locked = 2; _; _locked = 1; }

    constructor(address morpho) { MORPHO = morpho; owner = msg.sender; }

    // C2 probe: same batch but WITH nonReentrant
    function batchLiquidateGuarded(LiquidationItem[] calldata items) external onlyOwner nonReentrant returns (uint256 nOk) {
        nOk = _run(items);
    }

    function batchLiquidate(LiquidationItem[] calldata items) external onlyOwner returns (uint256 nOk) {
        nOk = _run(items);
    }

    function _run(LiquidationItem[] calldata items) internal returns (uint256 nOk) {
        for (uint256 i = 0; i < items.length; i++) {
            try this.liquidate(
                items[i].mp, items[i].borrower, items[i].repaidShares,
                items[i].swapTarget, items[i].swapCallData, items[i].minProfit
            ) returns (uint256) { nOk++; }
            catch (bytes memory reason) { emit BatchItemFailed(i, items[i].borrower, bytes4(reason)); }
        }
    }

    function liquidate(
        MarketParams calldata mp, address borrower, uint256 repaidShares,
        address swapTarget, bytes calldata swapCallData, uint256 minProfit
    ) external onlyOwnerOrSelf nonReentrant returns (uint256 profit) {
        bytes memory data = abi.encode(SwapData(swapTarget, swapCallData, mp.loanToken, mp.collateralToken));
        uint256 balBefore = IERC20(mp.loanToken).balanceOf(address(this));
        IMorpho(MORPHO).liquidate(mp, borrower, 0, repaidShares, data);
        uint256 balAfter = IERC20(mp.loanToken).balanceOf(address(this));
        profit = balAfter - balBefore;
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        _safeTransfer(mp.loanToken, owner, balAfter);
        emit Liquidated(borrower, mp.loanToken, profit);
    }

    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external {
        if (msg.sender != MORPHO) revert NotMorpho();
        SwapData memory s = abi.decode(data, (SwapData));
        uint256 collBal = IERC20(s.collateralToken).balanceOf(address(this));
        _forceApprove(s.collateralToken, s.swapTarget, collBal);
        (bool ok, ) = s.swapTarget.call(s.swapCallData);
        if (!ok) revert SwapFailed();
        _forceApprove(s.collateralToken, s.swapTarget, 0);
        if (IERC20(s.loanToken).balanceOf(address(this)) < repaidAssets) revert CannotRepay();
        _forceApprove(s.loanToken, MORPHO, repaidAssets);
    }

    function _forceApprove(address token, address spender, uint256 amount) internal {
        _c(token, abi.encodeWithSelector(IERC20.approve.selector, spender, 0));
        if (amount != 0) _c(token, abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
    }
    function _safeTransfer(address token, address to, uint256 amount) internal {
        _c(token, abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
    }
    function _c(address token, bytes memory payload) private {
        (bool ok, bytes memory ret) = token.call(payload);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert ERC20OpFailed();
    }
}

// ---- tests -----------------------------------------------------------------

contract BatchProtoTest is Test {
    MockERC20B loan; MockERC20B coll; MockMorpho morpho; MockSwapperB swapper;
    LiquidatorBatch lb;
    Liquidator current; // current prod contract, for C1
    MarketParams mp;
    address borrower1 = address(0xB1);
    address borrower2 = address(0xB2);

    event Liquidated(address indexed borrower, address indexed loanToken, uint256 profit);
    event BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason);

    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new MockMorpho(); swapper = new MockSwapperB(coll, loan, 2e18);
        lb = new LiquidatorBatch(address(morpho));
        current = new Liquidator(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 1_000e18);
        loan.mint(address(swapper), 1_000_000e18);
        morpho.set(10e18, 15e18); // seize 10 coll -> swap to 20 loan, repay 15 -> profit 5
    }

    function _item(address b, uint256 minP) internal view returns (LiquidationItem memory) {
        return LiquidationItem(mp, b, 1, address(swapper), abi.encodeWithSelector(MockSwapperB.swapAll.selector), minP);
    }

    // C1: on the CURRENT contract (onlyOwner), a self-call reverts NotOwner.
    // We simulate by pranking address(current) as caller.
    function testC1_SelfCallRevertsNotOwnerOnCurrent() public {
        vm.prank(address(current));
        vm.expectRevert(Liquidator.NotOwner.selector);
        current.liquidate(mp, borrower1, 1, address(swapper), "", 0);
    }

    // C1b: with onlyOwnerOrSelf, self-call passes the auth check (whole flow succeeds).
    function testC1b_BatchSelfCallWorks() public {
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = _item(borrower1, 0);
        uint256 nOk = lb.batchLiquidate(items);
        assertEq(nOk, 1);
        assertEq(loan.balanceOf(address(this)), 5e18); // profit swept to owner
    }

    // C2: batch WITH nonReentrant -> every item fails with Reentrant selector.
    function testC2_GuardedBatchFailsEveryItem() public {
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 0);
        items[1] = _item(borrower2, 0);
        vm.expectEmit(true, true, false, true);
        emit BatchItemFailed(0, borrower1, LiquidatorBatch.Reentrant.selector);
        vm.expectEmit(true, true, false, true);
        emit BatchItemFailed(1, borrower2, LiquidatorBatch.Reentrant.selector);
        uint256 nOk = lb.batchLiquidateGuarded(items);
        assertEq(nOk, 0);
    }

    // C2b: reentrancy probe — a malicious swapTarget trying to re-enter liquidate/batch.
    function testC2b_RouterCannotReenter() public {
        Reenterer r = new Reenterer(lb, mp);
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = LiquidationItem(mp, borrower1, 1, address(r), abi.encodeWithSelector(Reenterer.attack.selector), 0);
        uint256 nOk = lb.batchLiquidate(items);
        // swap "succeeds" only if attack() swallows its own reverts; assert re-entry never landed
        assertEq(r.reenteredLiquidate(), false);
        assertEq(r.reenteredBatch(), false);
        assertEq(nOk, 0); // item fails at CannotRepay since no swap happened
    }

    // C3: Error(string) from Morpho ("position is healthy") is caught; batch continues.
    function testC3_StringRevertCaughtBatchContinues() public {
        morpho.setHealthy(borrower1, true);
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 0); // healthy -> Error(string)
        items[1] = _item(borrower2, 0); // fine
        vm.expectEmit(true, true, false, true);
        emit BatchItemFailed(0, borrower1, bytes4(0x08c379a0)); // Error(string) selector
        uint256 nOk = lb.batchLiquidate(items);
        assertEq(nOk, 1);
        assertEq(loan.balanceOf(address(this)), 5e18);
    }

    // C3b: OOG in the subcall (infinite-loop swap target) is caught; batch continues.
    function testC3b_OOGSubcallCaughtBatchContinues() public {
        GasBurner gb = new GasBurner();
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = LiquidationItem(mp, borrower1, 1, address(gb), abi.encodeWithSelector(GasBurner.burn.selector), 0);
        items[1] = _item(borrower2, 0);
        uint256 nOk = lb.batchLiquidate{gas: 30_000_000}(items);
        assertEq(nOk, 1, "item2 should still succeed after item1 OOG");
        assertEq(loan.balanceOf(address(this)), 5e18);
    }

    // C3c: same but tight gas — demonstrates the 63/64 landmine: batch returns,
    // but item2 is starved and fails too (no revert of the whole tx though).
    function testC3c_TightGasStarvesLaterItems() public {
        GasBurner gb = new GasBurner();
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = LiquidationItem(mp, borrower1, 1, address(gb), abi.encodeWithSelector(GasBurner.burn.selector), 0);
        items[1] = _item(borrower2, 0);
        uint256 nOk = lb.batchLiquidate{gas: 3_000_000}(items);
        console.log("nOk at 3M gas:", nOk);
        assertEq(nOk, 0, "item2 starved by item1 burner");
    }

    // C4: two items, same loanToken — per-item profit correct, no double count.
    function testC4_TwoItemsSameLoanTokenAccounting() public {
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 5e18); // exact profit 5 => passes only if accounting clean
        items[1] = _item(borrower2, 5e18);
        vm.expectEmit(true, true, false, true);
        emit Liquidated(borrower1, address(loan), 5e18);
        vm.expectEmit(true, true, false, true);
        emit Liquidated(borrower2, address(loan), 5e18);
        uint256 nOk = lb.batchLiquidate(items);
        assertEq(nOk, 2);
        assertEq(loan.balanceOf(address(lb)), 0);
        assertEq(loan.balanceOf(address(this)), 10e18);
    }

    // C6: allowances are zero after each item.
    function testC6_NoAllowanceLeak() public {
        LiquidationItem[] memory items = new LiquidationItem[](2);
        items[0] = _item(borrower1, 0);
        items[1] = _item(borrower2, 0);
        lb.batchLiquidate(items);
        assertEq(coll.allowance(address(lb), address(swapper)), 0);
        assertEq(loan.allowance(address(lb), address(morpho)), 0);
    }

    // ProfitTooLow custom error selector surfaces in BatchItemFailed
    function testCustomErrorSelectorSurfaces() public {
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = _item(borrower1, 100e18); // profit 5 < 100
        vm.expectEmit(true, true, false, true);
        emit BatchItemFailed(0, borrower1, LiquidatorBatch.ProfitTooLow.selector);
        uint256 nOk = lb.batchLiquidate(items);
        assertEq(nOk, 0);
    }
}

/// Malicious "router": from inside onMorphoLiquidate's swap call, tries to re-enter.
contract Reenterer {
    LiquidatorBatch public lb; MarketParams mp;
    bool public reenteredLiquidate; bool public reenteredBatch;
    constructor(LiquidatorBatch l, MarketParams memory m) { lb = l; mp = m; }
    function attack() external {
        try lb.liquidate(mp, address(0xdead), 1, address(this), "", 0) returns (uint256) {
            reenteredLiquidate = true;
        } catch {}
        LiquidationItem[] memory items = new LiquidationItem[](0);
        try lb.batchLiquidate(items) returns (uint256) {
            reenteredBatch = true;
        } catch {}
    }
}
