// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// ПРИЁМКА СВИПА ГРУППЫ (27.08). Вскрытие каскада 15:04 (блок 50483683): наша проба стояла на
// idx 1461 МЕЖДУ чужими победами 1460 и 1463..1470 — восемь жертв одного рынка, один вызывающий.
// Проба сгорела «position is healthy»: цель кольца, на которую она пришла, за миг до неё забрал
// сосед, а fallback после ПЕРВОЙ попытки делал `return` («одна проба = одна попытка») и бросал
// СЕМЬ живых. Здесь это правится и здесь же проверяется.
//
// КАНОН ПРИЁМКИ: сначала ПОЗИТИВНЫЙ контроль (стенд вообще умеет стрелять — иначе ноль означал бы
// слепоту стенда, а не работу свипа), затем НЕГАТИВНЫЙ (все цели здоровы => успехов ноль, успех
// не рисуется обвязкой), и только потом главный. Главный ОБЯЗАН быть красным на базе: со снятым
// свипом (`return` после первой попытки) он даёт 0 успехов.
//
// Стенд — моки Batch.t.sol (тот же MockMorpho: `require(!healthy[borrower], "position is healthy")`
// = ровно та ошибка, что сожгла боевую пробу).
import {Test, console} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {LiquidatorV6} from "../src/LiquidatorV6.sol";
import {MarketParams} from "../src/Liquidator.sol";
import {MockERC20B, MockMorpho, MockSwapperB, GasBurner} from "./Batch.t.sol";

contract V6SweepTest is Test {
    MockERC20B loan; MockERC20B coll; MockMorpho morpho; MockSwapperB swapper;
    LiquidatorV6 liq;
    MarketParams mp;
    address leg = address(0x1EEE);           // авторизованная нога залпа (она же — вход fallback)
    uint64 dl;

    bytes32 constant EV_EXEC = keccak256("PlanExecuted(address,uint256)");
    bytes32 constant EV_FAIL = keccak256("PlanFailed(bytes4)");

    function setUp() public {
        loan = new MockERC20B(); coll = new MockERC20B();
        morpho = new MockMorpho(); swapper = new MockSwapperB(coll, loan, 2e18);
        liq = new LiquidatorV6(address(morpho));
        mp = MarketParams(address(loan), address(coll), address(1), address(2), 0.86e18);
        coll.mint(address(morpho), 10_000e18);
        loan.mint(address(swapper), 1_000_000e18);
        morpho.set(10e18, 15e18);            // seize 10 coll -> swap 20 loan -> repay 15 => профит 5
        liq.setAuthorized(leg, true);
        dl = uint64(block.timestamp + 900);
    }

    // --- утилиты --------------------------------------------------------------------------

    function _borrower(uint256 i) internal pure returns (address) {
        return address(uint160(0xB0 + i));
    }

    /// Группа 0 из n целей; байт пробы 0x01 (b=1 => g=0). Гейт 0 — оракул в стенде не нужен.
    function _arm(uint256 n) internal {
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) {
            ts[i] = LiquidatorV6.Target({
                borrower: _borrower(i), repaidShares: 1, seizedAssets: 0,
                swapTarget: address(swapper),
                swapCallData: abi.encodeWithSelector(MockSwapperB.swapAll.selector),
                minProfit: 0, unwrapTarget: address(0), triggerPrice: 0
            });
        }
        liq.setGroup(0, mp, dl, 0, ts);
    }

    /// Старт кольца ВЫЧИСЛЯЕТСЯ ровно как в контракте — иначе «первая цель» в тесте была бы
    /// не той, на которую придёт проба, и стенд проверял бы не то.
    function _start(uint256 n) internal view returns (uint256) {
        return uint256(keccak256(abi.encodePacked(leg))) % n;
    }

    function _probe(uint256 gas_) internal returns (bool ok) {
        vm.prank(leg);
        (ok, ) = address(liq).call{gas: gas_}(hex"01");
    }

    function _count(Vm.Log[] memory logs) internal view returns (uint256 exec, uint256 fail) {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != address(liq)) continue;
            if (logs[i].topics[0] == EV_EXEC) exec++;
            else if (logs[i].topics[0] == EV_FAIL) fail++;
        }
    }

    // ======================================================================================
    // 1. ПОЗИТИВНЫЙ КОНТРОЛЬ — стенд умеет стрелять
    // ======================================================================================

    function test_positive_control_probe_fires() public {
        _arm(1);
        vm.recordLogs();
        assertTrue(_probe(3_000_000), unicode"проба ревертнула");
        (uint256 exec, uint256 fail) = _count(vm.getRecordedLogs());
        assertEq(exec, 1, unicode"позитивный контроль: ликвидация не прошла => стенд слеп, дальше судить нечего");
        assertEq(fail, 0, unicode"позитивный контроль: неожиданный отказ");
        assertGt(loan.balanceOf(address(this)), 0, unicode"профит не выметен владельцу");
    }

    // ======================================================================================
    // 2. НЕГАТИВНЫЙ КОНТРОЛЬ — успех не рисуется обвязкой
    // ======================================================================================

    function test_negative_control_all_healthy_zero_exec() public {
        _arm(8);
        for (uint256 i = 0; i < 8; i++) morpho.setHealthy(_borrower(i), true);
        vm.recordLogs();
        assertTrue(_probe(3_000_000), unicode"проба ревертнула на здоровой группе");
        (uint256 exec, uint256 fail) = _count(vm.getRecordedLogs());
        assertEq(exec, 0, unicode"успех из ниоткуда: обвязка рисует победу");
        assertEq(fail, 8, unicode"свип обязан ОБОЙТИ всё кольцо: 8 отказов");
    }

    // ======================================================================================
    // 3. ГЛАВНЫЙ (красный на базе): цель кольца уже забрана — семь живых рядом
    // ======================================================================================

    function test_sweep_takes_next_when_ring_head_already_taken() public {
        _arm(8);
        uint256 s = _start(8);
        morpho.setHealthy(_borrower(s), true);        // ровно ту, на которую придёт проба
        vm.recordLogs();
        assertTrue(_probe(3_000_000), unicode"проба ревертнула");
        (uint256 exec, uint256 fail) = _count(vm.getRecordedLogs());
        // НА БАЗЕ (`return` после первой попытки): exec=0, fail=1 — ровно ожог 15:04.
        assertGe(exec, 1, unicode"ДЕФЕКТ 15:04: голова кольца забрана и проба ушла в ноль при живых соседях");
        assertEq(fail, 1, unicode"отказ головы обязан быть ровно один и громкий");
        (address b, , , ) = liq.targetInfo(0, s);
        assertEq(b, _borrower(s), unicode"чужая победа НЕ должна гасить нашу цель (гаснет только отстрелянная)");
    }

    /// Односрельность не сломана свипом: взятая цель гаснет, здоровая — остаётся.
    function test_sweep_clears_only_taken_slots() public {
        _arm(8);
        uint256 s = _start(8);
        morpho.setHealthy(_borrower(s), true);
        assertTrue(_probe(3_000_000), unicode"проба ревертнула");
        uint256 alive;
        for (uint256 i = 0; i < 8; i++) {
            (address b, , , ) = liq.targetInfo(0, i);
            if (b != address(0)) alive++;
        }
        assertEq(alive, 1, unicode"живой обязана остаться РОВНО забранная соседом голова");
    }

    // ======================================================================================
    // 4. БОЕВОЙ ГАЗ НОГИ (~771k): после успеха свип ОСТАНАВЛИВАЕТСЯ
    // ======================================================================================

    /// Ноги подписаны на ~771k. SWEEP_MIN_NEXT_GAS=900k => второй ПОЛНОЙ ликвидации в пробе быть
    /// не может: иначе out-of-gas второй откатил бы и ПЕРВУЮ, уже взятую. Один успех на пробу.
    function test_combat_gas_limit_yields_exactly_one_success() public {
        _arm(8);
        uint256 s = _start(8);
        morpho.setHealthy(_borrower(s), true);
        vm.recordLogs();
        assertTrue(_probe(771_000), unicode"боевой газ ноги не дотянул до конца пробы");
        (uint256 exec, uint256 fail) = _count(vm.getRecordedLogs());
        assertEq(exec, 1, unicode"на боевом лимите обязан быть РОВНО один успех");
        assertEq(fail, 1, unicode"и ровно один отказ забранной головы");
    }

    // ======================================================================================
    // 5. OUT-OF-GAS СОСЕДА НЕ ОТКАТЫВАЕТ УЖЕ ВЗЯТУЮ ЖЕРТВУ
    // ======================================================================================

    /// Свип продолжает обход ПОСЛЕ взятия денег — значит попытка, сожравшая весь свой газ,
    /// не имеет права утащить в откат уже забранную жертву (try/catch держит откат внутри
    /// подвызова, родителю остаётся 1/64 остатка; гейт успеха 900k делает эту 1/64 >=14k, а
    /// пути после catch — emit, два SLOAD'а, виток — хватает ~6k, копия плана за гардом).
    function test_oog_neighbour_does_not_revert_earlier_success() public {
        GasBurner burner = new GasBurner();
        uint256 n = 8;
        uint256 s = _start(n);
        LiquidatorV6.Target[] memory ts = new LiquidatorV6.Target[](n);
        for (uint256 i = 0; i < n; i++) {
            bool isBurner = (i == (s + 1) % n);        // РОВНО следующая за стартом цель кольца
            ts[i] = LiquidatorV6.Target({
                borrower: _borrower(i), repaidShares: 1, seizedAssets: 0,
                swapTarget: isBurner ? address(burner) : address(swapper),
                swapCallData: isBurner
                    ? abi.encodeWithSelector(GasBurner.burn.selector)
                    : abi.encodeWithSelector(MockSwapperB.swapAll.selector),
                minProfit: 0, unwrapTarget: address(0), triggerPrice: 0
            });
        }
        liq.setGroup(0, mp, dl, 0, ts);

        vm.recordLogs();
        // Лимит выше SWEEP_MIN_NEXT_GAS, чтобы свип ПОШЁЛ дальше после успеха и напоролся на жор.
        assertTrue(_probe(8_000_000), unicode"OOG соседа утащил в откат всю tx вместе с уже взятой жертвой");
        (uint256 exec, uint256 fail) = _count(vm.getRecordedLogs());
        assertGe(exec, 1, unicode"успех до жора не состоялся — стенд проверяет не то");
        assertGe(fail, 1, unicode"жор обязан быть ГРОМКИМ отказом, а не тишиной");
        assertGt(loan.balanceOf(address(this)), 0, unicode"деньги взятой жертвы не уцелели после OOG соседа");
    }
}
