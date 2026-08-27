// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// 27.08 РЕПЕТИЦИЯ ПОБЕДЫ: берём НАСТОЯЩУЮ жертву настоящего каскада (блок 50483683, рынок
// cbXRP/USDC 0xd4a903dc) БОЕВЫМ контрактом 0x8F92CAc0 и НОВЫМ маршрутом (роутер второй CL-фабрики
// 0x698Cb2b6, путь cbXRP ts100 WETH ts50 USDC). Форк на блоке 50483682 — ДО каскада, поэтому цена
// оракула ещё старая и позиции здоровы: транзит того блока подменяем vm.mockCall на цену, которая
// реально встала в 50483683 (1358989380e18 против 1365924650e18). Это тот же приём, которым
// проверяют «взяли бы мы приз, если бы стояли в блоке».
//
// НЕГАТИВНЫЙ КОНТРОЛЬ в том же файле: та же жертва тем же контрактом, но СТАРЫМ маршрутом
// (Slipstream 0xBE6D8f0d, ts2000 -> ts100). Он обязан провалиться (CannotRepay/минус), иначе
// вывод «нас держал маршрут» ложен и дело в чём-то другом.
// Запуск: RPC_URL=<нода> forge test --match-path test/WinRehearsalCbXRP.t.sol -vv
import {Test, console2} from "forge-std/Test.sol";

struct MarketParams { address loanToken; address collateralToken; address oracle; address irm; uint256 lltv; }

interface ILiqV6 {
    function owner() external view returns (address);
    function liquidateEx(MarketParams calldata mp, address borrower, uint256 seizedAssets,
        uint256 repaidShares, address swapTarget, bytes calldata swapCallData,
        uint256 minProfit, address unwrapTarget) external returns (uint256 profit);
}
interface IERC20Min { function balanceOf(address) external view returns (uint256); }

interface ISlipstreamRouter {
    struct ExactInputParams { bytes path; address recipient; uint256 deadline; uint256 amountIn; uint256 amountOutMinimum; }
    function exactInput(ExactInputParams calldata) external payable returns (uint256);
}

contract WinRehearsalCbXRPTest is Test {
    address constant BOARD = 0x8F92CAc0b6586F834DE33cF31819db0EbF6e52b9;
    address constant CBXRP = 0xcb585250f852C6c6bf90434AB21A00f02833a4af;
    address constant WETH  = 0x4200000000000000000000000000000000000006;
    address constant USDC  = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant ORACLE = 0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE;
    address constant IRM    = 0x46415998764C29aB2a25CbeA6254146D50D22687;
    address constant R_NEW = 0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F;
    address constant R_OLD = 0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5;
    bytes4 constant AERO_EXACT_INPUT = 0xc04b8d59;

    uint256 constant PRICE_AFTER_TRANSIT = 1358989380000000000000000000000000000;
    address constant VICTIM_BIG = 0xba93e7D9171Fc940Ad0232740bF57e91Bf0AB446;   // конкурент погасил $19 408
    uint256 constant SEIZED_BIG = 16091716635;                                   // ровно его сейз, 16 091.7 cbXRP

    MarketParams mp;
    address owner_;

    function setUp() public {
        vm.createSelectFork(vm.envString("RPC_URL"), 50483682);
        mp = MarketParams(USDC, CBXRP, ORACLE, IRM, 625000000000000000);
        owner_ = ILiqV6(BOARD).owner();
        // транзит блока 50483683, из-за которого и случился каскад
        vm.mockCall(ORACLE, abi.encodeWithSignature("price()"), abi.encode(PRICE_AFTER_TRANSIT));
    }

    function _path(uint24 ts1, uint24 ts2) internal pure returns (bytes memory) {
        return abi.encodePacked(CBXRP, ts1, WETH, ts2, USDC);
    }

    /// Ровно так строит калдату боевой `direct_swap` (chain/execute.py): селектор + abi.encode
    /// КОРТЕЖА. Собирать её `encodeWithSelector` от уже закодированных байтов нельзя — получится
    /// лишняя обёртка bytes, и роутер уходит в невалидный опкод (проверено 27.08 на этом же стенде).
    function _swapData(uint24 ts1, uint24 ts2, uint256 amt) internal pure returns (bytes memory) {
        return abi.encodeCall(ISlipstreamRouter.exactInput,
            (ISlipstreamRouter.ExactInputParams(_path(ts1, ts2), BOARD, 2 ** 63, amt, 0)));
    }

    function _try(address router, bytes memory data, uint256 seized)
        internal returns (bool ok, uint256 profit, bytes memory err)
    {
        vm.prank(owner_);
        try ILiqV6(BOARD).liquidateEx(mp, VICTIM_BIG, seized, 0, router, data, 0, address(0))
            returns (uint256 p) { return (true, p, ""); }
        catch (bytes memory e) { return (false, 0, e); }
    }

    /// ПОЗИТИВНЫЙ КОНТРОЛЬ: боевой контракт + новый маршрут берут настоящую жертву каскада.
    function test_new_route_takes_the_real_cascade_victim() public {
        uint256 ownerBefore = IERC20Min(USDC).balanceOf(owner_);
        (bool ok, uint256 profit, bytes memory err) = _try(R_NEW, _swapData(100, 50, SEIZED_BIG), SEIZED_BIG);
        if (!ok) console2.logBytes(err);
        assertTrue(ok, unicode"новый маршрут не смог взять жертву, которую взял конкурент");
        console2.log("NEW route: profit USDC =", profit / 1e6);
        assertGt(profit, 0, unicode"профит не положителен");
        assertEq(IERC20Min(USDC).balanceOf(owner_) - ownerBefore, profit, unicode"профит не выметен владельцу");
    }

    /// НЕГАТИВНЫЙ КОНТРОЛЬ: та же жертва, тот же контракт, СТАРЫЙ боевой маршрут — обязан провалиться.
    function test_old_route_cannot_take_the_same_victim() public {
        (bool ok, uint256 profit, ) = _try(R_OLD, _swapData(2000, 100, SEIZED_BIG), SEIZED_BIG);
        console2.log("OLD route: ok =", ok); console2.log("OLD route: profit USDC =", profit / 1e6);
        assertTrue(!ok || profit == 0, unicode"старый маршрут вдруг берёт жертву — вывод «держал маршрут» ложен");
    }

    /// Контроль стенда: без подмены цены позиция ЗДОРОВА и ликвидация обязана отказать —
    /// иначе успех выше объяснялся бы не маршрутом, а тем, что стенд ликвидирует что угодно.
    function test_control_healthy_before_transit_is_refused() public {
        vm.clearMockedCalls();
        (bool ok, , ) = _try(R_NEW, _swapData(100, 50, SEIZED_BIG), SEIZED_BIG);
        assertTrue(!ok, unicode"здоровая позиция ликвидировалась — стенд берёт что угодно");
    }
}
