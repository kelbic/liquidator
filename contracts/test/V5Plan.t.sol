// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// v5 (stateful board, «го» kelbic 07.08): owner армит план заранее, залповые EOA пробят
// <=1 байтом калдаты через fallback. Здоровая проба = ТИХИЙ no-op ДО копирования плана
// (triggerPrice против оракула рынка), авторитет ликвидируемости — сам Morpho (try/catch).
// Форк на реальный Morpho, как V4Auth.t.sol. Главное здесь — обе полярности FAIL-OPEN
// (trigger=0 => пробовать; оракул ревертит => пробовать) и газ-границы, доказывающие,
// что здоровая проба НЕ трогает KB-скейл swapCallData.

import {Test, console2} from "forge-std/Test.sol";
import {Liquidator, MarketParams, LiquidationItem} from "../src/Liquidator.sol";

struct MarketP {
    uint128 totalSupplyAssets; uint128 totalSupplyShares;
    uint128 totalBorrowAssets; uint128 totalBorrowShares;
    uint128 lastUpdate; uint128 fee;
}

interface IMorphoFullP {
    function owner() external view returns (address);
    function enableIrm(address irm) external;
    function enableLltv(uint256 lltv) external;
    function createMarket(MarketParams memory mp) external;
    function supply(MarketParams memory mp, uint256 assets, uint256 shares, address onBehalf, bytes memory data) external returns (uint256, uint256);
    function supplyCollateral(MarketParams memory mp, uint256 assets, address onBehalf, bytes memory data) external;
    function borrow(MarketParams memory mp, uint256 assets, uint256 shares, address onBehalf, address receiver) external returns (uint256, uint256);
    function position(bytes32 id, address user) external view returns (uint256 supplyShares, uint128 borrowShares, uint128 collateral);
}

contract MockERC20P {
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

contract MockOracleP {
    uint256 public p;
    bool public broken;                          // тест fail-open: оракул ревертит
    function setPrice(uint256 v) external { p = v; }
    function setBroken(bool b) external { broken = b; }
    function price() external view returns (uint256) {
        require(!broken, "oracle down");
        return p;
    }
}

contract MockIrmP {
    function borrowRate(MarketParams memory, MarketP memory) external pure returns (uint256) { return 0; }
    function borrowRateView(MarketParams memory, MarketP memory) external pure returns (uint256) { return 0; }
}

contract MockSwapperP {
    MockERC20P public coll; MockERC20P public loan; uint256 public rate;
    constructor(MockERC20P c, MockERC20P l, uint256 r) { coll = c; loan = l; rate = r; }
    function swapAll() external {
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

/// @dev Злой роут в плане (компрометация возможна только через owner'а, но инвариант тот же,
/// что у v4): тянет залог, не отдаёт loan — обязан лечь CannotRepay и НЕ потребить план.
contract EvilSwapperP {
    MockERC20P public coll;
    constructor(MockERC20P c) { coll = c; }
    function swapAll() external {
        coll.transferFrom(msg.sender, address(this), coll.balanceOf(msg.sender));
    }
}

/// @dev Реентрант-проба посреди свопа: злой роутер дёргает fallback ликвидатора ИЗНУТРИ
/// исполнения плана, потом честно свопит. Двойного огня быть не должно (Reentrant -> catch).
contract ReentrantSwapperP {
    MockERC20P public coll; MockERC20P public loan; address public liq;
    uint256 public reentered;
    constructor(MockERC20P c, MockERC20P l, address q) { coll = c; loan = l; liq = q; }
    function swapAll() external {
        (bool ok, ) = liq.call(hex"01");         // реентрант-проба (успех = тихий no-op)
        require(ok, "reentrant probe reverted");
        reentered++;
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * 18e17 / 1e18);
    }
}

contract V5PlanForkTest is Test {
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    uint256 constant LLTV = 0.86e18;
    uint64 constant TTL = 600;                   // секунд жизни плана в тестах
    IMorphoFullP morpho = IMorphoFullP(MORPHO);
    MockERC20P loan; MockERC20P coll; MockOracleP oracle; MockIrmP irm; MockSwapperP swapper;
    Liquidator liq;
    MarketParams mp; bytes32 id;
    address borrower = address(0xB0B5);
    address volley = address(0xEA01);
    address stranger = address(0xBAD1);
    uint256 trigger;                             // цена ликвидности в юнитах ОРАКУЛА-ОБЁРТКИ

    event PlanExecuted(address indexed borrower, uint256 profit);

    function setUp() public {
        vm.createSelectFork(vm.envString("RPC_URL"));
        loan = new MockERC20P(); coll = new MockERC20P(); oracle = new MockOracleP(); irm = new MockIrmP();

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

        liq = new Liquidator(MORPHO);            // owner = this
        liq.setAuthorized(volley, true);
        swapper = new MockSwapperP(coll, loan, 1.8e18);
        loan.mint(address(swapper), 1_000_000e18);

        // порог ликвидности в юнитах обёртки: price <= borrowed*1e36/(collateral*lltv).
        // borrowed=170e18, collateral=100e18, lltv=0.86 => ~1.9767e36
        trigger = 170e18 * 1e36 / (100e18 * LLTV / 1e18);
    }

    // --- хелперы -----------------------------------------------------------------------------

    function _swapData() internal pure returns (bytes memory) {
        return abi.encodeWithSelector(MockSwapperP.swapAll.selector);
    }

    /// @dev Роут, раздутый до боевого размера (Kyber = KB-скейл): газ-граница здоровой пробы
    /// обязана доказывать, что эти байты НЕ копируются из стораджа.
    function _fatSwapData() internal pure returns (bytes memory) {
        return abi.encodePacked(abi.encodeWithSelector(MockSwapperP.swapAll.selector), new bytes(2048));
    }

    function _arm(bytes memory route, address target, uint256 trig) internal {
        liq.setPlan(mp, borrower, _shares(), target, route, 0, address(0),
                    uint64(block.timestamp) + TTL, trig);
    }

    function _shares() internal view returns (uint256 bs) {
        (, uint128 s,) = morpho.position(id, borrower);
        bs = uint256(s);
    }

    function _probe(address from) internal returns (bool ok, uint256 gasUsed) {
        // остудить сторадж: боевая проба — свежая tx, слоты ХОЛОДНЫЕ (2100/SLOAD), и весь
        // смысл фильтра — не платить эту цену за KB-скейл байты плана на здоровом пути
        vm.cool(address(liq));
        vm.cool(address(oracle));
        vm.prank(from);
        uint256 g0 = gasleft();
        (ok, ) = address(liq).call(hex"01");
        gasUsed = g0 - gasleft();
    }

    function _deadline() internal view returns (uint64 dl) {
        (, dl,) = liq.planInfo();
    }

    // --- доступ ------------------------------------------------------------------------------

    function testSetPlanOnlyOwner() public {
        vm.prank(stranger);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liq.setPlan(mp, borrower, 1, address(swapper), _swapData(), 0, address(0),
                    uint64(block.timestamp) + TTL, 0);
        vm.prank(stranger);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liq.clearPlan();
    }

    function testSetPlanBadDeadline() public {
        vm.expectRevert(Liquidator.BadDeadline.selector);
        liq.setPlan(mp, borrower, 1, address(swapper), _swapData(), 0, address(0),
                    uint64(block.timestamp), 0);
    }

    // --- тихие no-op пути --------------------------------------------------------------------

    function testProbeNoPlanSilent() public {
        (bool ok, ) = _probe(volley);
        assertTrue(ok, "probe with no plan must not revert");
        assertGt(_shares(), 0, "nothing may execute");
    }

    function testProbeStrangerSilent() public {
        _arm(_swapData(), address(swapper), trigger);
        oracle.setPrice(1.8e36);                 // ликвидируемо — но чужому нельзя
        (bool ok, ) = _probe(stranger);
        assertTrue(ok, "stranger probe is a silent no-op");
        assertGt(_shares(), 0, "stranger must not consume the plan");
        assertGt(_deadline(), 0, "plan retained");
    }

    function testProbeExpiredSilent() public {
        _arm(_swapData(), address(swapper), trigger);
        oracle.setPrice(1.8e36);
        vm.warp(block.timestamp + TTL + 1);      // TTL вышел — протухший роут не исполняется
        (bool ok, ) = _probe(volley);
        assertTrue(ok, "expired probe is a silent no-op");
        assertGt(_shares(), 0, "expired plan must not execute");
    }

    function testBadCalldataReverts() public {
        _arm(_swapData(), address(swapper), 0);
        (bool ok2, ) = address(liq).call(hex"beef");
        assertFalse(ok2, "2-byte calldata = mistyped call, must revert");
        (bool ok4, ) = address(liq).call(hex"deadbeef");
        assertFalse(ok4, "unknown selector must revert, not silently probe");
    }

    // --- газ-фильтр: план не копируется на здоровой пробе --------------------------------------

    function testHealthyProbeCheapAndTriggerZeroAttempts() public {
        _arm(_fatSwapData(), address(swapper), trigger);   // цена 2e36 > trigger => здоров
        (bool ok, uint256 gFiltered) = _probe(volley);
        assertTrue(ok, "healthy probe must not revert");
        assertGt(_shares(), 0, "healthy: nothing executes");
        assertGt(_deadline(), 0, "healthy: plan retained");

        // ПОЗИТИВНЫЙ КОНТРОЛЬ (канон): trigger=0 => фильтра нет => план копируется и Morpho
        // пробуется (здоровый реверт глотается). Разница газа обязана быть >= стоимости копии
        // 2KB из стораджа (~64 слова * 2100), иначе фильтр не делает того, что заявлено.
        _arm(_fatSwapData(), address(swapper), 0);
        (bool ok0, uint256 gAttempt) = _probe(volley);
        assertTrue(ok0, "trigger=0 healthy attempt swallowed");
        assertGt(_shares(), 0, "morpho refused: healthy");
        assertGt(_deadline(), 0, "failed attempt retains plan");
        assertGt(gAttempt, gFiltered + 100_000, "filter must skip the plan-bytes copy");
        assertLt(gFiltered, 40_000, "healthy filtered probe must stay cheap");
        console2.log("gas filtered-noop (cold, w/o 21k intrinsic):", gFiltered);
        console2.log("gas trigger=0 attempt (cold, 2KB route):    ", gAttempt);
    }

    function testOracleRevertFailOpen() public {
        _arm(_fatSwapData(), address(swapper), trigger);
        oracle.setBroken(true);                  // фильтр обязан СНЯТЬСЯ, не закрыть огонь
        (bool ok, uint256 g) = _probe(volley);
        assertTrue(ok, "broken oracle: probe still succeeds");
        assertGt(_shares(), 0, "morpho also reverts on broken oracle");
        assertGt(_deadline(), 0, "plan retained");
        assertGt(g, 100_000, "attempt must have loaded the plan (fail-open), not returned early");
    }

    function testEpsilonZoneMorphoAuthority() public {
        // price <= trigger, но Morpho считает позицию здоровой: триггер чуть выше правды.
        _arm(_swapData(), address(swapper), 2.1e36);       // цена 2e36 <= 2.1e36 => фильтр пройден
        (bool ok, ) = _probe(volley);
        assertTrue(ok, "epsilon probe swallowed");
        assertGt(_shares(), 0, "Morpho is the authority: healthy stays healthy");
        assertGt(_deadline(), 0, "plan retained for the next probe");
    }

    // --- огонь -------------------------------------------------------------------------------

    function testProbeFiresProfitToOwnerPlanConsumed() public {
        _arm(_swapData(), address(swapper), trigger);
        oracle.setPrice(1.8e36);
        uint256 ownerBefore = loan.balanceOf(address(this));
        uint256 volleyBefore = loan.balanceOf(volley);
        vm.expectEmit(true, false, false, false, address(liq));
        emit PlanExecuted(borrower, 0);
        (bool ok, ) = _probe(volley);
        assertTrue(ok, "fire probe reverted");
        assertEq(_shares(), 0, "debt not cleared");
        assertGt(loan.balanceOf(address(this)), ownerBefore, "profit must sweep to OWNER");
        assertEq(loan.balanceOf(volley), volleyBefore, "volley EOA must get nothing");
        assertEq(_deadline(), 0, "plan must be consumed (one-shot)");

        // вторая нога залпа в тот же блок: дешёвый no-op вместо реверта «уже ликвидирован»
        (bool ok2, uint256 g2) = _probe(volley);
        assertTrue(ok2, "post-consume probe must not revert");
        assertLt(g2, 10_000, "post-consume probe must be a 1-SLOAD-class no-op");
    }

    function testOwnerCanProbe() public {
        _arm(_swapData(), address(swapper), trigger);
        oracle.setPrice(1.8e36);
        (bool ok, ) = _probe(address(this));
        assertTrue(ok, "owner probe reverted");
        assertEq(_shares(), 0, "owner probe must fire too");
    }

    function testSetPlanOverwrites() public {
        // первый план — на пустого «заёмщика» (нет позиции => Morpho ревертит, план остаётся)
        liq.setPlan(mp, stranger, 1, address(swapper), _swapData(), 0, address(0),
                    uint64(block.timestamp) + TTL, 0);
        oracle.setPrice(1.8e36);
        (bool okA, ) = _probe(volley);
        assertTrue(okA); assertGt(_shares(), 0, "wrong-borrower plan must not clear real debt");
        _arm(_swapData(), address(swapper), trigger);      // перезапись правильным планом
        (bool okB, ) = _probe(volley);
        assertTrue(okB);
        assertEq(_shares(), 0, "overwritten plan must fire");
    }

    // --- инварианты безопасности -------------------------------------------------------------

    function testEvilRouteCannotStealPlanRetained() public {
        EvilSwapperP evil = new EvilSwapperP(coll);
        liq.setPlan(mp, borrower, _shares(), address(evil),
                    abi.encodeWithSelector(EvilSwapperP.swapAll.selector), 0, address(0),
                    uint64(block.timestamp) + TTL, trigger);
        oracle.setPrice(1.8e36);
        (bool ok, ) = _probe(volley);
        assertTrue(ok, "evil-route attempt swallowed (CannotRepay inside try)");
        assertGt(_shares(), 0, "debt untouched");
        assertEq(coll.balanceOf(address(evil)), 0, "collateral must not leave on revert");
        assertGt(_deadline(), 0, "failed plan retained");
    }

    function testReentrantProbeNoDoubleFire() public {
        ReentrantSwapperP reent = new ReentrantSwapperP(coll, loan, address(liq));
        loan.mint(address(reent), 1_000_000e18);
        liq.setPlan(mp, borrower, _shares(), address(reent),
                    abi.encodeWithSelector(ReentrantSwapperP.swapAll.selector), 0, address(0),
                    uint64(block.timestamp) + TTL, trigger);
        oracle.setPrice(1.8e36);
        (bool ok, ) = _probe(volley);
        assertTrue(ok, "outer probe reverted");
        assertEq(reent.reentered(), 1, "swap ran exactly once");
        assertEq(_shares(), 0, "single fire cleared the debt");
        assertEq(_deadline(), 0, "plan consumed once");
    }

    // --- V1 не тронут ------------------------------------------------------------------------

    function testV1PathsUnchanged() public {
        oracle.setPrice(1.8e36);
        uint256 bs = _shares();
        LiquidationItem[] memory items = new LiquidationItem[](1);
        items[0] = LiquidationItem(mp, borrower, bs, address(swapper), _swapData(), 0, address(0));
        vm.prank(volley);
        uint256 nOk = liq.batchLiquidate(items);
        assertEq(nOk, 1, "v4 batch path must keep working untouched");
        assertEq(_shares(), 0);
    }
}
