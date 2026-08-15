// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

// ПРИБОР Г1б, НЕ БОЕВОЙ КОД. Три прототипа мультислота, отличающиеся ТОЛЬКО раскладкой планов
// и телом fallback(). Денежный путь (liquidate / onMorphoLiquidate / ERC20-хелперы / auth)
// скопирован из v5 БИТ-В-БИТ и вынесен в V6Base — иначе замер сравнивал бы не слоты, а
// две разные ликвидации. contracts/src/Liquidator.sol НЕ ТРОНУТ.
//
// D4 — D1 ⊕ D3: байт выбирает ГРУППУ (рынок), внутри группы один oracle.price() гейтит M целей.
//      Нужен потому, что D3 как прототип держит ОДИН рынок, а флот покрывает 23 фида / 40 рынков.
// D1 — байт пробы = ИНДЕКС слота (1..8), fallback читает РОВНО один слот.
// D2 — байт игнорируется, fallback перебирает ВСЕ активные слоты (по одному oracle.price() на план).
// D3 — слоты сгруппированы ПО РЫНКУ: общий заголовок (mp + дедлайн + ГРУБЫЙ порог = max по целям),
//      один oracle.price() на весь пакет; персональный порог цели проверяется ПОСЛЕ того, как
//      грубый гейт открылся (то есть на пути ОГНЯ, не на пути промаха).

import {MarketParams, IMorpho, IERC20, IERC4626, IOracle} from "./Liquidator.sol";

/// @dev Денежный путь v5 без изменений. Раскладка стораджа: owner=0, _locked=1, isAuthorized=2,
/// планы наследника начинаются со слота 3 — ровно там, где у v5 лежит `Plan private _plan`.
abstract contract V6Base {
    address public immutable MORPHO;
    address public owner;
    uint256 private _locked = 1;
    mapping(address => bool) public isAuthorized;

    struct Plan {
        MarketParams mp;
        address borrower;
        uint256 repaidShares;
        address swapTarget;
        bytes swapCallData;
        uint256 minProfit;
        address unwrapTarget;
        uint64 deadline;
        uint256 triggerPrice;
    }

    struct SwapData {
        address swapTarget;
        bytes swapCallData;
        address loanToken;
        address collateralToken;
        address unwrapTarget;
    }

    error NotOwner();
    error NotMorpho();
    error Reentrant();
    error SwapFailed();
    error CannotRepay();
    error ProfitTooLow(uint256 got, uint256 min);
    error ERC20OpFailed();
    error BadDeadline();
    error TooManySlots();

    event Liquidated(address indexed borrower, address indexed loanToken, uint256 profit);
    event AuthorizedChanged(address indexed who, bool ok);
    event PlanSet(uint64 deadline);
    event PlanExecuted(address indexed borrower, uint256 profit);
    event PlanFailed(bytes4 reason);
    event SwapInput(uint256 collateral);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyOwnerOrSelf() {
        if (msg.sender != owner && msg.sender != address(this) && !isAuthorized[msg.sender]) {
            revert NotOwner();
        }
        _;
    }

    modifier nonReentrant() {
        if (_locked == 2) revert Reentrant();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor(address morpho) {
        MORPHO = morpho;
        owner = msg.sender;
    }

    function setAuthorized(address who, bool ok) external onlyOwner {
        isAuthorized[who] = ok;
        emit AuthorizedChanged(who, ok);
    }

    function liquidate(
        MarketParams calldata mp,
        address borrower,
        uint256 repaidShares,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit,
        address unwrapTarget
    ) external onlyOwnerOrSelf nonReentrant returns (uint256 profit) {
        address swapToken = unwrapTarget == address(0)
            ? mp.collateralToken
            : IERC4626(unwrapTarget).asset();
        bytes memory data = abi.encode(
            SwapData({
                swapTarget: swapTarget,
                swapCallData: swapCallData,
                loanToken: mp.loanToken,
                collateralToken: swapToken,
                unwrapTarget: unwrapTarget
            })
        );

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

        if (s.unwrapTarget != address(0)) {
            IERC4626(s.unwrapTarget).redeem(
                IERC20(s.unwrapTarget).balanceOf(address(this)), address(this), address(this)
            );
        }
        uint256 collBal = IERC20(s.collateralToken).balanceOf(address(this));
        emit SwapInput(collBal);
        _forceApprove(s.collateralToken, s.swapTarget, collBal);
        (bool ok, ) = s.swapTarget.call(s.swapCallData);
        if (!ok) revert SwapFailed();
        _forceApprove(s.collateralToken, s.swapTarget, 0);

        if (IERC20(s.loanToken).balanceOf(address(this)) < repaidAssets) revert CannotRepay();
        _forceApprove(s.loanToken, MORPHO, repaidAssets);
    }

    function _forceApprove(address token, address spender, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, 0));
        if (amount != 0) {
            _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
    }

    function _call(address token, bytes memory payload) private {
        (bool ok, bytes memory ret) = token.call(payload);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert ERC20OpFailed();
    }
}

/// @dev Общая для D1/D2 раскладка: массив из 8 ПОЛНЫХ планов + счётчик активных.
/// D1 и D2 отличаются ТОЛЬКО fallback'ом, поэтому цена АРМА у них одна и та же по построению
/// (стенд это отдельно перепроверяет квитанциями, а не доверием к этому комментарию).
abstract contract V6Slots is V6Base {
    Plan[8] internal _plans;
    uint256 internal _nSlots;

    function setPlans(Plan[] calldata ps) external onlyOwner {
        uint256 n = ps.length;
        if (n > 8) revert TooManySlots();
        for (uint256 i = 0; i < n; i++) {
            if (ps[i].deadline <= block.timestamp) revert BadDeadline();
            _plans[i] = ps[i];
        }
        _nSlots = n;
        emit PlanSet(n == 0 ? 0 : ps[0].deadline);
    }

    function clearSlots() external onlyOwner {
        for (uint256 i = 0; i < 8; i++) delete _plans[i];
        _nSlots = 0;
    }

    function slotInfo(uint256 i)
        external
        view
        returns (address borrower, uint64 deadline, uint256 triggerPrice)
    {
        return (_plans[i].borrower, _plans[i].deadline, _plans[i].triggerPrice);
    }

    function nSlots() external view returns (uint256) {
        return _nSlots;
    }
}

/// @notice D1: байт пробы = индекс слота (1..8). Промах трогает РОВНО один слот -> O(1) по M.
/// Следствие (считается в стенде C, не здесь): одна проба покрывает одну цель, значит плотность
/// на цель делится на M либо число проб растёт в M раз.
contract LiquidatorV6D1 is V6Slots {
    constructor(address morpho) V6Base(morpho) {}

    fallback() external {
        if (msg.data.length > 1) revert();
        if (msg.data.length == 0) return;
        uint256 i = uint256(uint8(msg.data[0]));
        if (i == 0 || i > 8) return;
        unchecked { i -= 1; }
        Plan storage sp = _plans[i];
        uint64 dl = sp.deadline;
        if (dl == 0 || block.timestamp > dl) return;
        if (msg.sender != owner && !isAuthorized[msg.sender]) return;
        uint256 trig = sp.triggerPrice;
        if (trig != 0) {
            try IOracle(sp.mp.oracle).price() returns (uint256 px) {
                if (px > trig) return;             // здоров: KB-план из стораджа НЕ копируется
            } catch {}
        }
        Plan memory p = sp;
        try this.liquidate(
            p.mp, p.borrower, p.repaidShares, p.swapTarget, p.swapCallData, p.minProfit,
            p.unwrapTarget
        ) returns (uint256 profit) {
            delete _plans[i];                      // односрельность: гаснет ТОЛЬКО свой слот
            emit PlanExecuted(p.borrower, profit);
        } catch (bytes memory reason) {
            emit PlanFailed(bytes4(reason));
        }
    }
}

/// @notice D2: байт игнорируется, fallback перебирает ВСЕ активные слоты. Огонь — по ПЕРВОМУ
/// сработавшему слоту (и только по нему: остальные M-1 остаются живыми, как требует односрельность).
contract LiquidatorV6D2 is V6Slots {
    constructor(address morpho) V6Base(morpho) {}

    fallback() external {
        if (msg.data.length > 1) revert();
        uint256 n = _nSlots;
        if (n == 0) return;
        if (msg.sender != owner && !isAuthorized[msg.sender]) return;
        for (uint256 i = 0; i < n; i++) {
            Plan storage sp = _plans[i];
            uint64 dl = sp.deadline;
            if (dl == 0 || block.timestamp > dl) continue;
            uint256 trig = sp.triggerPrice;
            if (trig != 0) {
                try IOracle(sp.mp.oracle).price() returns (uint256 px) {
                    if (px > trig) continue;       // ЦЕНА ПРОМАХА: этот виток на КАЖДЫЙ слот
                } catch {}
            }
            Plan memory p = sp;
            try this.liquidate(
                p.mp, p.borrower, p.repaidShares, p.swapTarget, p.swapCallData, p.minProfit,
                p.unwrapTarget
            ) returns (uint256 profit) {
                delete _plans[i];
                emit PlanExecuted(p.borrower, profit);
            } catch (bytes memory reason) {
                emit PlanFailed(bytes4(reason));
            }
            return;
        }
    }
}

/// @notice D4 = D1 ⊕ D3. Байт пробы выбирает ГРУППУ (рынок) — как в D1; внутри группы общий
/// заголовок гейтит все M целей ОДНИМ oracle.price() — как в D3. Промах обязан быть O(1) и по
/// числу групп G, и по числу целей M: читаются только дедлайн и порог АДРЕСОВАННОЙ группы.
/// Существует потому, что D3 как прототип держит РОВНО ОДИН рынок, а флот покрывает 23 фида.
contract LiquidatorV6D4 is V6Base {
    struct Target {
        address borrower;
        uint256 repaidShares;
        address swapTarget;
        bytes swapCallData;
        uint256 minProfit;
        address unwrapTarget;
        uint256 triggerPrice;
    }

    struct Group {
        MarketParams mp;
        uint256 gateTrigger;
        uint64 deadline;
        uint8 n;
    }

    Group[8] internal _groups;
    mapping(uint256 => Target[8]) internal _targets;

    constructor(address morpho) V6Base(morpho) {}

    function setGroup(
        uint256 g,
        MarketParams calldata mp,
        uint64 deadline,
        uint256 gateTrigger,
        Target[] calldata ts
    ) external onlyOwner {
        if (deadline <= block.timestamp) revert BadDeadline();
        uint256 n = ts.length;
        if (g >= 8 || n > 8) revert TooManySlots();
        Group storage gr = _groups[g];
        gr.mp = mp;
        gr.gateTrigger = gateTrigger;
        gr.deadline = deadline;
        for (uint256 i = 0; i < n; i++) _targets[g][i] = ts[i];
        gr.n = uint8(n);
        emit PlanSet(deadline);
    }

    function groupInfo(uint256 g)
        external
        view
        returns (address oracle, uint64 deadline, uint256 gate, uint8 n)
    {
        Group storage gr = _groups[g];
        return (gr.mp.oracle, gr.deadline, gr.gateTrigger, gr.n);
    }

    function targetInfo(uint256 g, uint256 i)
        external
        view
        returns (address borrower, uint256 triggerPrice)
    {
        return (_targets[g][i].borrower, _targets[g][i].triggerPrice);
    }

    fallback() external {
        if (msg.data.length > 1) revert();
        if (msg.data.length == 0) return;
        uint256 g = uint256(uint8(msg.data[0]));
        if (g == 0 || g > 8) return;
        unchecked { g -= 1; }
        Group storage gr = _groups[g];
        uint64 dl = gr.deadline;
        if (dl == 0 || block.timestamp > dl) return;
        if (msg.sender != owner && !isAuthorized[msg.sender]) return;
        uint256 gate = gr.gateTrigger;
        uint256 px = type(uint256).max;
        if (gate != 0) {
            try IOracle(gr.mp.oracle).price() returns (uint256 p) {
                if (p > gate) return;          // ЦЕНА ПРОМАХА: 2 SLOAD + 1 oracle, и всё
                px = p;
            } catch {}
        }
        uint256 n = gr.n;
        if (n == 0) return;
        MarketParams memory mp = gr.mp;
        Target[8] storage ts = _targets[g];
        for (uint256 i = 0; i < n; i++) {
            Target storage st = ts[i];
            if (st.borrower == address(0)) continue;
            uint256 t = st.triggerPrice;
            if (t != 0 && px > t) continue;
            Target memory tg = st;
            try this.liquidate(
                mp, tg.borrower, tg.repaidShares, tg.swapTarget, tg.swapCallData, tg.minProfit,
                tg.unwrapTarget
            ) returns (uint256 profit) {
                delete ts[i];
                emit PlanExecuted(tg.borrower, profit);
            } catch (bytes memory reason) {
                emit PlanFailed(bytes4(reason));
            }
            return;
        }
    }
}

/// @notice D3: пакет целей ОДНОГО рынка. Заголовок несёт mp/дедлайн один раз на пакет и ГРУБЫЙ
/// порог `gateTrigger` = max(персональных порогов). Промах: 1 SLOAD дедлайна + 1 SLOAD порога +
/// РОВНО ОДИН oracle.price() независимо от M — цели вообще не читаются. Когда грубый гейт
/// открылся, перебираются цели и у каждой сверяется её СОБСТВЕННЫЙ порог (точность не потеряна).
/// Байт пробы задаёт индекс, С КОТОРОГО начинается перебор — ноги расходятся по разным целям.
contract LiquidatorV6D3 is V6Base {
    struct Target {
        address borrower;
        uint256 repaidShares;
        address swapTarget;
        bytes swapCallData;
        uint256 minProfit;
        address unwrapTarget;
        uint256 triggerPrice;      // персональный порог цели (0 = без фильтра)
    }

    MarketParams internal _mp;
    uint256 internal _gateTrigger;
    uint64 internal _deadline;
    uint8 internal _n;
    Target[8] internal _targets;

    constructor(address morpho) V6Base(morpho) {}

    function setMarketPlan(
        MarketParams calldata mp,
        uint64 deadline,
        uint256 gateTrigger,
        Target[] calldata ts
    ) external onlyOwner {
        if (deadline <= block.timestamp) revert BadDeadline();
        uint256 n = ts.length;
        if (n > 8) revert TooManySlots();
        _mp = mp;
        _gateTrigger = gateTrigger;
        _deadline = deadline;
        for (uint256 i = 0; i < n; i++) _targets[i] = ts[i];
        _n = uint8(n);
        emit PlanSet(deadline);
    }

    function clearMarketPlan() external onlyOwner {
        _deadline = 0;
        _n = 0;
        for (uint256 i = 0; i < 8; i++) delete _targets[i];
    }

    function hdrInfo() external view returns (address oracle, uint64 deadline, uint256 gate, uint8 n) {
        return (_mp.oracle, _deadline, _gateTrigger, _n);
    }

    function targetInfo(uint256 i)
        external
        view
        returns (address borrower, uint256 repaidShares, uint256 triggerPrice)
    {
        return (_targets[i].borrower, _targets[i].repaidShares, _targets[i].triggerPrice);
    }

    fallback() external {
        if (msg.data.length > 1) revert();
        uint64 dl = _deadline;
        if (dl == 0 || block.timestamp > dl) return;
        if (msg.sender != owner && !isAuthorized[msg.sender]) return;
        uint256 gate = _gateTrigger;
        uint256 px = type(uint256).max;
        if (gate != 0) {
            try IOracle(_mp.oracle).price() returns (uint256 p) {
                if (p > gate) return;              // ЦЕНА ПРОМАХА: один oracle-вызов на ВЕСЬ пакет
                px = p;
            } catch {}
        }
        uint256 n = _n;
        if (n == 0) return;
        uint256 start = 0;
        if (msg.data.length == 1) {
            uint256 b = uint256(uint8(msg.data[0]));
            if (b >= 1 && b <= n) start = b - 1;
        }
        MarketParams memory mp = _mp;
        for (uint256 k = 0; k < n; k++) {
            uint256 i = start + k;
            if (i >= n) i -= n;
            Target storage st = _targets[i];
            address b = st.borrower;
            if (b == address(0)) continue;
            uint256 t = st.triggerPrice;
            if (t != 0 && px > t) continue;        // персональный порог — уже ПОСЛЕ гейта
            Target memory tg = st;
            try this.liquidate(
                mp, tg.borrower, tg.repaidShares, tg.swapTarget, tg.swapCallData, tg.minProfit,
                tg.unwrapTarget
            ) returns (uint256 profit) {
                delete _targets[i];                // гаснет ТОЛЬКО своя цель, заголовок цел
                emit PlanExecuted(tg.borrower, profit);
            } catch (bytes memory reason) {
                emit PlanFailed(bytes4(reason));
            }
            return;
        }
    }
}
