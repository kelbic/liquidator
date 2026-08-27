// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// 27.08 ПОЗИТИВНЫЙ КОНТРОЛЬ маршрута SOL -> USDC (рынок 0x7dc02ff6, третий по деньгам за 7 суток:
// $28 490 погашено, медиана $1 239 — и роута у нас не было ВОВСЕ). Берём ПРЯМОЙ пул UniV3
// SOL/USDC fee=3000 через роутер, который бот уже использует (SwapRouter02, форма exactInput без
// deadline). Реальный своп на форке живой ноды тем же селектором, что строит direct_swap.
// Негативный контроль — размер за капом: он обязан ломать экономику (импакт много больше бонуса
// 12.68%), иначе кап 100 SOL выбран не по обрыву, а с потолка.
// Запуск: RPC_URL=<нода> forge test --match-path test/RouteSOL.t.sol -vv
import {Test, console2} from "forge-std/Test.sol";

interface IERC20Min {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

interface IUniRouter02 {
    struct ExactInputParams { bytes path; address recipient; uint256 amountIn; uint256 amountOutMinimum; }
    function exactInput(ExactInputParams calldata) external payable returns (uint256);
}

contract RouteSOLTest is Test {
    address constant SOL_T = 0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82;
    address constant USDC  = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant R_UNI = 0x2626664c2603336E57B271c5C0b26F421741e481;   // UniV3 SwapRouter02
    uint256 constant CAP = 100e9;      // кап маршрута: 100 SOL (SOL = 9 знаков)

    function setUp() public { vm.createSelectFork(vm.envString("RPC_URL")); }

    function _path() internal pure returns (bytes memory) {
        return abi.encodePacked(SOL_T, uint24(3000), USDC);
    }

    function _swap(uint256 amt) internal returns (bool ok, uint256 out) {
        deal(SOL_T, address(this), amt);
        IERC20Min(SOL_T).approve(R_UNI, amt);
        try IUniRouter02(R_UNI).exactInput(
            IUniRouter02.ExactInputParams(_path(), address(this), amt, 0)
        ) returns (uint256 o) { return (true, o); } catch { return (false, 0); }
    }

    /// Медианный размер победителя (~11.5 SOL): своп проходит, импакт заметно ниже бонуса 12.68%.
    function test_median_size_swaps_within_bonus() public {
        (bool okRef, uint256 ref) = _swap(1e9);
        assertTrue(okRef, unicode"референс 1 SOL не прошёл — стенд слеп");
        (bool ok, uint256 out) = _swap(12e9);
        assertTrue(ok, unicode"медианный размер не свопается");
        uint256 impBps = 10_000 - (out * 1e9 * 10_000) / (ref * 12e9);
        console2.log("12 SOL -> USDC:", out / 1e6, "impact bps:", impBps);
        assertLt(impBps, 300, unicode"импакт медианного размера обязан быть < 3%");
    }

    /// На КАПЕ маршрут ещё жив (иначе кап завышен).
    function test_at_cap_still_alive() public {
        (bool okRef, uint256 ref) = _swap(1e9);
        (bool ok, uint256 out) = _swap(CAP);
        assertTrue(okRef && ok, unicode"на капе своп обязан проходить");
        uint256 impBps = 10_000 - (out * 1e9 * 10_000) / (ref * CAP);
        console2.log("100 SOL (cap) -> USDC:", out / 1e6, "impact bps:", impBps);
        assertLt(impBps, 1268, unicode"на капе импакт обязан быть НИЖЕ бонуса 12.68% — иначе кап завышен");
    }

    /// НЕГАТИВНЫЙ КОНТРОЛЬ: вчетверо выше капа экономика ломается — значит кап выбран по обрыву.
    function test_above_cap_breaks_economics() public {
        (bool okRef, uint256 ref) = _swap(1e9);
        (bool ok, uint256 out) = _swap(4 * CAP);
        uint256 impBps = ok ? 10_000 - (out * 1e9 * 10_000) / (ref * 4 * CAP) : 10_000;
        console2.log("400 SOL (4x cap) -> USDC:", ok ? out / 1e6 : 0, "impact bps:", impBps);
        assertGt(impBps, 1268, unicode"за капом импакт обязан ПРЕВЫШАТЬ бонус — иначе кап взят с потолка");
    }
}
