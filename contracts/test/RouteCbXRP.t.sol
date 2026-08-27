// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// 27.08 ПОЗИТИВНЫЙ КОНТРОЛЬ маршрута cbXRP -> USDC на ВТОРОЙ CL-фабрике (роутер 0x698Cb2b6,
// путь cbXRP ts100 WETH ts50 USDC) — РЕАЛЬНЫЙ своп на форке живой ноды, той же v1-формой
// exactInput и тем же селектором, которыми стреляет direct_swap. Рядом — тот же размер через
// СТАРЫЙ путь (Slipstream 0xBE6D8f0d, ts2000 -> ts100): негативный контроль, доказывающий,
// что дефект был в маршруте, а не в стенде. Запуск: RPC_URL=https://mainnet.base.org forge test
// --match-path test/RouteCbXRP.t.sol -vv
import {Test, console2} from "forge-std/Test.sol";

interface IERC20Min {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

interface ISlipstreamRouter {
    struct ExactInputParams { bytes path; address recipient; uint256 deadline; uint256 amountIn; uint256 amountOutMinimum; }
    function exactInput(ExactInputParams calldata) external payable returns (uint256 amountOut);
}

interface IQuoter {
    function quoteExactInput(bytes memory path, uint256 amountIn)
        external returns (uint256 amountOut, uint160[] memory, uint32[] memory, uint256);
}

contract RouteCbXRPTest is Test {
    address constant CBXRP = 0xcb585250f852C6c6bf90434AB21A00f02833a4af;
    address constant WETH  = 0x4200000000000000000000000000000000000006;
    address constant USDC  = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant R_NEW = 0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F;   // SwapRouter CLFactory 0xf8f2eb49
    address constant Q_NEW = 0x514c8B5f54112481E28028F1166Bd78501089259;   // QuoterV2 той же фабрики
    address constant R_OLD = 0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5;   // Aerodrome Slipstream (в бою до 27.08)
    uint256 constant SIZE = 16_000e6;                                        // сейз кита $19.4k из блока 50483683

    function setUp() public {
        vm.createSelectFork(vm.envString("RPC_URL"));
        deal(CBXRP, address(this), 2 * SIZE);
    }

    function _path(uint24 ts1, uint24 ts2) internal pure returns (bytes memory) {
        return abi.encodePacked(CBXRP, ts1, WETH, ts2, USDC);
    }

    function _swap(address router, bytes memory path, uint256 amt) internal returns (bool ok, uint256 out) {
        IERC20Min(CBXRP).approve(router, amt);
        try ISlipstreamRouter(router).exactInput(
            ISlipstreamRouter.ExactInputParams(path, address(this), 2 ** 63, amt, 0)
        ) returns (uint256 o) { return (true, o); } catch { return (false, 0); }
    }

    /// Реальный своп 16k cbXRP новым путём: сходится с квотером той же фабрики, импакт < 1%.
    function test_new_route_swaps_16k_close_to_quote() public {
        (uint256 q, , , ) = IQuoter(Q_NEW).quoteExactInput(_path(100, 50), SIZE);
        (uint256 qs, , , ) = IQuoter(Q_NEW).quoteExactInput(_path(100, 50), 100e6);
        uint256 before = IERC20Min(USDC).balanceOf(address(this));
        (bool ok, uint256 out) = _swap(R_NEW, _path(100, 50), SIZE);
        assertTrue(ok, unicode"своп новым роутером ревертнул");
        assertEq(IERC20Min(USDC).balanceOf(address(this)) - before, out, unicode"выход не пришёл на счёт");
        assertApproxEqRel(out, q, 1e15, unicode"исполнение разошлось с квотером >0.1%");
        // импакт к 100 XRP: out/SIZE против qs/100
        uint256 impBps = 10_000 - (out * 100e6 * 10_000) / (qs * SIZE);
        console2.log("new route 16k cbXRP -> USDC:", out / 1e6, "impact bps:", impBps);
        assertLt(impBps, 100, unicode"импакт 16k на новом пути обязан быть <1%");
    }

    /// НЕГАТИВНЫЙ КОНТРОЛЬ: тот же размер старым боевым путём теряет большую часть выхода.
    function test_old_route_same_size_collapses() public {
        (uint256 qn, , , ) = IQuoter(Q_NEW).quoteExactInput(_path(100, 50), SIZE);
        (bool ok, uint256 out) = _swap(R_OLD, _path(2000, 100), SIZE);
        console2.log("old route 16k cbXRP -> USDC:", ok ? out / 1e6 : 0, "vs new quote:", qn / 1e6);
        assertTrue(!ok || out * 2 < qn, unicode"старый путь вдруг держит размер — вывод о миграции ложен");
    }
}
