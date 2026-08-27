// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// СЕТОЧНАЯ ПРИЁМКА СВИПА (ред.2, 27.08). Ровно тот стенд, которого потребовала адверсарная
// линза, снёсшая ред.1: сетка по лимиту газа пробы, ХОЛОДНЫЕ слоты и БОЕВАЯ длина роута,
// два инварианта на каждой точке:
//   (И1) проба НИКОГДА не возвращает ok=false — родительский фрейм не падает;
//   (И2) баланс владельца после пробы НИКОГДА не меньше, чем до неё — взятые деньги не
//        откатываются попыткой, ушедшей в out-of-gas.
// Ред.1 имела SWEEP_MIN_TRY_GAS=120 000 при копии плана 154 229 газа: гейт пропускал, копия
// не влезала, OOG случался в РОДИТЕЛЕ. Линза нашла 6 лимитов из сетки [130k..900k], где вся
// tx уходила в откат, и 37 из 351 в [1.5M..5M], где вместе с ней гибла УЖЕ ВЗЯТАЯ жертва.
import {Test, console2, Vm} from "forge-std/Test.sol";
import {LiquidatorV6} from "../src/LiquidatorV6.sol";
import {MarketParams} from "../src/Liquidator.sol";
import {MockERC20B, MockMorpho, MockSwapperB, GasBurner} from "./Batch.t.sol";

contract V6SweepGridTest is Test {
    MockERC20B loan; MockERC20B coll; MockMorpho morpho; MockSwapperB swapper;
    LiquidatorV6 liq;
    MarketParams mp;
    address leg = address(0x1EEE);
    uint64 dl;

    bytes32 constant EV_EXEC = keccak256("PlanExecuted(address,uint256)");

    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new MockMorpho(); swapper = new MockSwapperB(coll, loan, 2e18);
        liq = new LiquidatorV6(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 100_000e18);
        loan.mint(address(swapper), 10_000_000e18);
        morpho.set(10e18, 15e18);
        liq.setAuthorized(leg, true);
        dl = uint64(block.timestamp + 900);
    }

    function _borrower(uint256 i) internal pure returns (address) { return address(uint160(0xB0 + i)); }

    /// БОЕВАЯ длина swapCallData: реальный Slipstream exactInput с двухноговым path — ~2 КБ.
    /// Ред.1 проверялась селектором в 4 байта, и вся её арифметика жила в режиме, которого в
    /// бою не бывает.
    function _fatCallData() internal pure returns (bytes memory out) {
        out = new bytes(2048);
        out[0] = 0xc0; out[1] = 0x4b; out[2] = 0x8d; out[3] = 0x59;
    }

    function _arm(uint256 n, uint256 healthyIdx) internal {
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) {
            ts[i] = LiquidatorV6.Target({
                borrower: _borrower(i), repaidShares: 1, seizedAssets: 0,
                swapTarget: address(swapper),
                swapCallData: i == healthyIdx ? _fatCallData()
                                              : abi.encodeWithSelector(MockSwapperB.swapAll.selector),
                minProfit: 0, unwrapTarget: address(0), triggerPrice: 0
            });
        }
        liq.setGroup(0, mp, dl, 0, ts);
    }

    /// Слоты группы ОСТУЖАЮТСЯ: боевая проба — всегда свежая tx, её SLOAD'ы холодные.
    /// Без этого копия плана стоит 100/слово вместо 2100, и вся сетка живёт не в том режиме.
    function _coolBoard() internal {
        vm.cool(address(liq));
    }

    function _probe(uint256 gas_) internal returns (bool ok) {
        _coolBoard();
        vm.prank(leg);
        (ok, ) = address(liq).call{gas: gas_}(hex"01");
    }

    /// И1+И2 на СЕТКЕ лимитов: ни одна точка не роняет tx и не съедает уже взятое.
    function test_grid_never_reverts_and_never_loses_banked() public {
        uint256 s = uint256(keccak256(abi.encodePacked(leg))) % 8;
        uint256 bad = 0;
        for (uint256 g = 150_000; g <= 5_000_000; g += 50_000) {
            _arm(8, s);                                   // голова кольца — с жирной калдатой
            morpho.setHealthy(_borrower(s), true);        // и она уже забрана соседом
            uint256 before = loan.balanceOf(address(this));
            bool ok = _probe(g);
            uint256 aft = loan.balanceOf(address(this));
            if (!ok) { bad++; console2.log("REVERT at gas limit", g); }
            assertGe(aft, before, unicode"баланс владельца УМЕНЬШИЛСЯ — взятое откатили");
        }
        assertEq(bad, 0, unicode"есть лимиты, на которых проба роняет всю транзакцию");
    }

    /// ПОЗИТИВНЫЙ КОНТРОЛЬ сетки: на боевом лимите свип реально БЕРЁТ деньги, а не просто
    /// «не падает». Без этого зелёная сетка неотличима от механизма, который ничего не делает.
    function test_combat_limit_takes_money_after_taken_head() public {
        uint256 s = uint256(keccak256(abi.encodePacked(leg))) % 8;
        _arm(8, s);
        morpho.setHealthy(_borrower(s), true);
        uint256 before = loan.balanceOf(address(this));
        vm.recordLogs();
        assertTrue(_probe(1_000_000), unicode"боевой лимит 1M: проба ревертнула");
        uint256 exec;
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(liq) && logs[i].topics[0] == EV_EXEC) exec++;
        }
        assertGe(exec, 1, unicode"на боевом лимите свип обязан взять ХОТЯ БЫ одну жертву");
        assertGt(loan.balanceOf(address(this)), before, unicode"деньги не пришли владельцу");
        console2.log("combat 1M: victims taken", exec);
    }

    /// ГЛУБИНА: сколько целей проба успевает попробовать на боевом лимите (все головы заняты).
    function test_combat_limit_depth() public {
        uint256 s = uint256(keccak256(abi.encodePacked(leg))) % 8;
        _arm(8, 99);                                      // жирная калдата никому — считаем витки
        for (uint256 i = 0; i < 8; i++) morpho.setHealthy(_borrower(i), true);
        vm.recordLogs();
        assertTrue(_probe(1_000_000), unicode"проба ревертнула на полностью занятой группе");
        uint256 fails;
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 evFail = keccak256("PlanFailed(bytes4)");
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(liq) && logs[i].topics[0] == evFail) fails++;
        }
        console2.log("combat 1M: targets tried", fails);
        assertGe(fails, 2, unicode"свип обязан пробовать больше одной цели — иначе он бесполезен");
    }
}
