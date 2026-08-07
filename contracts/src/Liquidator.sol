// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

/// @notice Morpho Blue market parameters (mirrors IMorpho.MarketParams).
struct MarketParams {
    address loanToken;
    address collateralToken;
    address oracle;
    address irm;
    uint256 lltv;
}

/// @notice One target of a batchLiquidate call (same arguments as liquidate(), packed).
struct LiquidationItem {
    MarketParams mp;
    address borrower;
    uint256 repaidShares;
    address swapTarget;
    bytes swapCallData;
    uint256 minProfit;
    address unwrapTarget;
}

interface IMorpho {
    function liquidate(
        MarketParams memory marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes memory data
    ) external returns (uint256 seized, uint256 repaid);
}

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @dev Minimal ERC4626 surface for the unwrap hop (wsuperOETHb -> superOETHb).
interface IERC4626 {
    function asset() external view returns (address);
    function redeem(uint256 shares, address receiver, address owner) external returns (uint256);
}

/// @dev Morpho oracle wrapper (collateral price in loan units, 1e36 scale).
interface IOracle {
    function price() external view returns (uint256);
}

/// @title Liquidator — zero-capital Morpho Blue liquidations on Base.
/// @notice Flow (verified against Morpho.sol): liquidate() seizes collateral to THIS contract,
/// then Morpho calls onMorphoLiquidate where we swap collateral->loanToken (generic aggregator
/// calldata), then Morpho pulls `repaidAssets` of loanToken. No standing capital: the seized
/// collateral funds the repayment via the swap; the surplus (LIF bonus minus swap cost) is profit.
///
/// Safety (from review): swap-success + can-repay checks, minProfit gate (= slippage protection),
/// nonReentrant, onlyOwner entry / onlyMorpho callback, return-data-checked ERC20 ops, market
/// params passed as arguments (not hardcoded), force-approve (USDT-safe), allowance reset.
contract Liquidator {
    address public immutable MORPHO;
    address public owner;
    uint256 private _locked = 1; // 1 = unlocked, 2 = locked (nonzero-init saves gas)
    /// @notice v4 (cascade tier): volley EOAs allowed to FIRE liquidations. Profit still sweeps
    /// to `owner` (liquidate() transfers to owner unconditionally), so an authorized key can
    /// spend its own gas but never redirect proceeds; a malicious swapTarget from a compromised
    /// key is stopped by CannotRepay / the checked profit math (balAfter - balBefore reverts on
    /// deficit). Keys are add/remove-able only by owner.
    mapping(address => bool) public isAuthorized;

    /// @notice v5 (stateful board): owner arms a full liquidation plan BEFORE the window; volley
    /// EOAs then probe with <=1 byte of calldata (fallback). The probe never reveals the target
    /// (v4 probes carry full liquidate args readable by competitors in our healthy-reverts) and a
    /// healthy probe is a cheap SILENT no-op: `triggerPrice` is checked against the market oracle
    /// BEFORE the plan (with its KB-scale swapCallData) is ever copied out of storage — copying on
    /// every probe would cost ~2100 gas/word and dwarf the v4 calldata cost it replaces.
    /// Morpho stays the authority on liquidatability: the trigger is only a gas filter, the actual
    /// attempt runs through this.liquidate -> Morpho, and a healthy revert is swallowed.
    struct Plan {
        MarketParams mp;
        address borrower;
        uint256 repaidShares;
        address swapTarget;
        bytes swapCallData;
        uint256 minProfit;
        address unwrapTarget;     // packs with `deadline` into one slot
        uint64 deadline;          // unix ts; 0 = no plan. TTL: stale routes must not execute.
        uint256 triggerPrice;     // oracle-wrapper units (1e36); 0 = no filter (fail-open)
    }
    Plan private _plan;

    /// @dev Swap context handed to the callback via Morpho's `data`.
    struct SwapData {
        address swapTarget;       // aggregator router (0x/1inch/Odos/KyberSwap)
        bytes swapCallData;       // pre-built off-chain by the bot
        address loanToken;
        address collateralToken;  // token the SWAP spends: market collateral, or its ERC4626 underlying when unwrapping
        address unwrapTarget;     // ERC4626 wrapper to redeem before the swap (0 = no unwrap)
    }

    error NotOwner();
    error NotMorpho();
    error Reentrant();
    error SwapFailed();
    error CannotRepay();
    error ProfitTooLow(uint256 got, uint256 min);
    error ERC20OpFailed();
    error BadDeadline();

    event Liquidated(address indexed borrower, address indexed loanToken, uint256 profit);
    event OwnerChanged(address indexed from, address indexed to);
    event AuthorizedChanged(address indexed who, bool ok);
    /// @notice A batch item that reverted (reason = first 4 bytes of the revert data).
    event BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason);
    /// @notice v5: deliberately content-free — probe txs and arm/disarm must not leak the target
    /// (the setPlan calldata is public regardless, but events are the cheap thing to read).
    event PlanSet(uint64 deadline);
    event PlanCleared();
    /// @notice v5: plan consumed by a successful probe (borrower is public knowledge by then).
    event PlanExecuted(address indexed borrower, uint256 profit);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @dev batchLiquidate calls liquidate() via `this.` (external self-call) so each item gets its
    /// own revert frame (try/catch isolation). The self-call must pass auth: owner, the contract,
    /// or (v4) an authorized volley EOA.
    modifier onlyOwnerOrSelf() {
        if (msg.sender != owner && msg.sender != address(this) && !isAuthorized[msg.sender]) {
            revert NotOwner();
        }
        _;
    }

    /// @dev v4: entry gate for batchLiquidate — owner or an authorized volley EOA.
    modifier onlyAuth() {
        if (msg.sender != owner && !isAuthorized[msg.sender]) revert NotOwner();
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

    function setOwner(address newOwner) external onlyOwner {
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    /// @notice v4: add/remove a volley EOA. Owner-only; profit destination is unaffected.
    function setAuthorized(address who, bool ok) external onlyOwner {
        isAuthorized[who] = ok;
        emit AuthorizedChanged(who, ok);
    }

    // --- v5 stateful board -------------------------------------------------------------------

    /// @notice Arm (or re-arm, overwriting) the plan. Owner-only: the swap route in the plan is
    /// therefore always our own — same trust line as liquidate()'s onlyOwnerOrSelf.
    /// @param deadline plan TTL as unix ts; must be in the future.
    /// @param triggerPrice fire filter in ORACLE-WRAPPER units (1e36-scaled collateral-in-loan,
    /// NOT the Chainlink feed the predictor watches): probe attempts only when price() <= trigger.
    /// 0 disables the filter (every authorized probe attempts) — fail-open by construction.
    function setPlan(
        MarketParams calldata mp,
        address borrower,
        uint256 repaidShares,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit,
        address unwrapTarget,
        uint64 deadline,
        uint256 triggerPrice
    ) external onlyOwner {
        if (deadline <= block.timestamp) revert BadDeadline();
        _plan = Plan({
            mp: mp, borrower: borrower, repaidShares: repaidShares, swapTarget: swapTarget,
            swapCallData: swapCallData, minProfit: minProfit, unwrapTarget: unwrapTarget,
            deadline: deadline, triggerPrice: triggerPrice
        });
        emit PlanSet(deadline);
    }

    /// @notice Disarm. Also implicit on successful execution (one-shot).
    function clearPlan() external onlyOwner {
        delete _plan;
        emit PlanCleared();
    }

    /// @notice Cheap bot-side arming check (full plan content stays out of view calls too).
    function planInfo() external view returns (address borrower, uint64 deadline, uint256 triggerPrice) {
        return (_plan.borrower, _plan.deadline, _plan.triggerPrice);
    }

    /// @notice v5 probe entry: <=1 byte of calldata, silent no-op unless everything lines up.
    /// Order is cost-ordered and every ambiguity fails OPEN toward attempting:
    ///   no/expired plan -> return; unauthorized -> return (griefers can't consume the plan);
    ///   trigger set and oracle says healthy -> return WITHOUT touching the KB-scale plan bytes;
    ///   trigger zero OR oracle reverts -> attempt anyway (Morpho hits the same oracle and is
    ///   the real authority); Morpho-healthy -> revert swallowed, plan RETAINED for the next probe.
    /// No nonReentrant here for the same proven reason as batchLiquidate: the external self-call
    /// takes the lock itself; a reentrant probe during the swap lands in the try -> Reentrant ->
    /// caught -> no-op. >1 byte means a mistyped ABI call — revert loudly, never silently no-op.
    fallback() external {
        if (msg.data.length > 1) revert();
        uint64 dl = _plan.deadline;
        if (dl == 0 || block.timestamp > dl) return;
        if (msg.sender != owner && !isAuthorized[msg.sender]) return;
        uint256 trig = _plan.triggerPrice;
        if (trig != 0) {
            try IOracle(_plan.mp.oracle).price() returns (uint256 px) {
                if (px > trig) return;            // healthy by price: plan bytes never loaded
            } catch {}                            // oracle broken -> filter off, Morpho decides
        }
        Plan memory p = _plan;
        try this.liquidate(
            p.mp, p.borrower, p.repaidShares, p.swapTarget, p.swapCallData, p.minProfit,
            p.unwrapTarget
        ) returns (uint256 profit) {
            delete _plan;                         // one-shot: next volley leg is a 2-SLOAD no-op
            emit PlanExecuted(p.borrower, profit);
        } catch {}                                // healthy / CannotRepay / raced: plan retained
    }

    /// @notice Liquidate `borrower` on market `mp`, repaying `repaidShares` of debt, swapping the
    /// seized collateral to loanToken via `swapTarget`/`swapCallData`. Reverts unless realized
    /// profit (swept to owner) >= `minProfit`. onlyOwner so swap calldata is always our own.
    /// @param unwrapTarget ERC4626 wrapper (== mp.collateralToken) to redeem before the swap when
    /// the wrapper itself has no DEX liquidity but its underlying does (wsuperOETHb -> superOETHb);
    /// the swap calldata must then spend the UNDERLYING. address(0) = no unwrap (unchanged path).
    function liquidate(
        MarketParams calldata mp,
        address borrower,
        uint256 repaidShares,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit,
        address unwrapTarget
    ) external onlyOwnerOrSelf nonReentrant returns (uint256 profit) {
        // asset() read on-chain: the swap-spend token can never disagree with what redeem() emits
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
        // seizedAssets = 0 -> repay `repaidShares`; Morpho seizes the incentivized collateral.
        IMorpho(MORPHO).liquidate(mp, borrower, 0, repaidShares, data);
        uint256 balAfter = IERC20(mp.loanToken).balanceOf(address(this));

        profit = balAfter - balBefore;
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        _safeTransfer(mp.loanToken, owner, balAfter); // sweep everything (incl. any prior dust)
        emit Liquidated(borrower, mp.loanToken, profit);
    }

    /// @notice Liquidate up to N targets in ONE tx (cascade capture: every prize lands on the same
    /// tx-index instead of consecutive nonces). Deliberately NO nonReentrant here: each item runs
    /// through the external self-call `this.liquidate(...)` which takes the lock itself — a guard on
    /// the batch would make every item revert Reentrant (proven by prototype test C2). Reentrancy
    /// stays closed: every entry point is onlyOwnerOrSelf / onlyOwner / onlyMorpho (prototype C2b).
    /// A failed item emits BatchItemFailed and the loop continues (per-item try/catch); gas must be
    /// budgeted by the caller with headroom — a starved tail fails SILENTLY inside the same
    /// successful tx (prototype C3c), so outcomes must be read from receipt events only.
    function batchLiquidate(LiquidationItem[] calldata items) external onlyAuth returns (uint256 nOk) {
        for (uint256 i = 0; i < items.length; i++) {
            try this.liquidate(
                items[i].mp, items[i].borrower, items[i].repaidShares,
                items[i].swapTarget, items[i].swapCallData, items[i].minProfit,
                items[i].unwrapTarget
            ) returns (uint256) {
                nOk++;
            } catch (bytes memory reason) {
                emit BatchItemFailed(i, items[i].borrower, bytes4(reason));
            }
        }
    }

    /// @notice Morpho callback: collateral already received; swap it to loanToken and make sure
    /// the contract can cover `repaidAssets` (Morpho pulls it right after this returns).
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external {
        if (msg.sender != MORPHO) revert NotMorpho();
        SwapData memory s = abi.decode(data, (SwapData));

        if (s.unwrapTarget != address(0)) {
            // seized wrapper -> underlying; redeem to self, swap below spends the underlying
            IERC4626(s.unwrapTarget).redeem(
                IERC20(s.unwrapTarget).balanceOf(address(this)), address(this), address(this)
            );
        }
        uint256 collBal = IERC20(s.collateralToken).balanceOf(address(this));
        _forceApprove(s.collateralToken, s.swapTarget, collBal);
        (bool ok, ) = s.swapTarget.call(s.swapCallData);
        if (!ok) revert SwapFailed();
        _forceApprove(s.collateralToken, s.swapTarget, 0); // drop dangling allowance

        if (IERC20(s.loanToken).balanceOf(address(this)) < repaidAssets) revert CannotRepay();
        _forceApprove(s.loanToken, MORPHO, repaidAssets); // Morpho pulls exactly this next
    }

    /// @notice Recover stuck tokens (dust collateral from a partial swap, airdrops) to owner.
    function sweep(address token) external onlyOwner {
        _safeTransfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    // --- return-data-checked ERC20 helpers (handle non-standard tokens) ---

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
