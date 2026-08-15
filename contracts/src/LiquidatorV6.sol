// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {MarketParams, LiquidationItem, IMorpho, IERC20, IERC4626, IOracle} from "./Liquidator.sol";

/// @title LiquidatorV6 — БОЕВОЙ мультислот D4 (G=8 групп x M=8 целей = 64 цели).
/// @notice Реализация docs/SPEC_V6D4.md. Что меняется против v5: снимается потолок «один
/// armed-план = одна цель» (в каскаде блока бывает 25 ликвидаций одного рынка). Что НЕ меняется:
/// денежный путь (liquidate / onMorphoLiquidate / ERC20-хелперы / auth) перенесён из v5
/// БИТ-В-БИТ, кроме параметризации `seizedAssets` (К1, §2.3). Гонку v6 не выигрывает: прибытие
/// и плотность в предочереди — отдельное звено.
///
/// contracts/src/Liquidator.sol (v5, В БОЮ) не тронут; contracts/src/LiquidatorV6Probe.sol —
/// ПРИБОР репетиции, здесь он не используется (в нём дефект §2.2 и нет решений §2.1/§2.3).
///
/// РАСКЛАДКА СТОРАДЖА (эталон приёмки — СЫРОЙ сторадж, не view: view умеет лгать, сторадж нет):
///   0            owner
///   1            _locked
///   2            isAuthorized (mapping)
///   3 .. 58      _groups[8], по 7 слотов на группу:
///                  +0 loanToken, +1 collateralToken, +2 oracle, +3 irm, +4 lltv,
///                  +5 gateTrigger, +6 (deadline | n<<64)
///   59           _targets (mapping): база группы g = keccak256(abi.encode(g, 59)),
///                  цель i лежит по base + 7*i:
///                  +0 borrower, +1 (repaidShares | seizedAssets<<128), +2 swapTarget,
///                  +3 swapCallData(header), +4 minProfit, +5 unwrapTarget, +6 triggerPrice
///   60           _hot (g0 | count<<8 | until<<16)
contract LiquidatorV6 {
    address public immutable MORPHO;
    address public owner;
    uint256 private _locked = 1; // 1 = unlocked, 2 = locked (ненулевая инициализация дешевле)
    /// @notice Ноги залпа, которым разрешено СТРЕЛЯТЬ. Профит всё равно уходит owner'у
    /// (`_liquidate` переводит безусловно), поэтому скомпрометированный ключ ноги тратит свой
    /// газ, но не перенаправляет выручку.
    mapping(address => bool) public isAuthorized;

    /// @notice Цель. К1 (§2.3): `seizedAssets` и `repaidShares` УПАКОВАНЫ в один слот по uint128.
    /// Ровно одно из двух обязано быть ненулевым — авторитет проверки Morpho, мы пробрасываем.
    /// Заявка по `seizedAssets` делает вход свопа ТОЧНЫМ по построению: сколько коллатерала
    /// придёт, известно до подписи calldata, поэтому запечённая сумма свопа всегда верна, а
    /// плавает только `repaid` (вниз при падении цены — по направлению каскада безопасно).
    struct Target {
        address borrower;
        uint128 repaidShares;     // } один слот
        uint128 seizedAssets;     // }
        address swapTarget;
        bytes swapCallData;
        uint256 minProfit;
        address unwrapTarget;
        uint256 triggerPrice;     // персональный порог цели (единицы обёртки оракула, 1e36); 0 = без фильтра
    }

    /// @notice Заголовок группы. Группы индексуются НАМИ: ничто не обязывает их быть разными
    /// рынками — каскад на 26 целей одного рынка законно занимает 4 группы с одинаковым `mp`.
    struct Group {
        MarketParams mp;
        uint256 gateTrigger;      // грубый гейт = max персональных порогов; 0 = гейта нет
        uint64 deadline;          // } один слот
        uint8 n;                  // }
    }

    /// @notice Горячий указатель (§2.1). ОДИН слот: запись ~5k газа против ~1.57M за арм слота.
    /// Пред-армим группы заранее (дорого, в тишине), в горячий момент перенаправляем весь огонь
    /// предподписанных ног одной дешёвой записью — БЕЗ пересборки лестниц (77 с CPU, невозможно
    /// в момент открытия окна).
    struct Hot {
        uint8 g0;                 // первая группа спана
        uint8 count;              // ширина спана: 1 = вся плотность на один рынок, 4 = каскад на 32 цели
        uint64 until;             // TTL указателя (unix ts)
    }

    Group[8] internal _groups;
    mapping(uint256 => Target[8]) internal _targets;
    Hot internal _hot;

    /// @dev Контекст свопа, уходящий в колбэк Morpho через `data` (v5 бит-в-бит).
    struct SwapData {
        address swapTarget;
        bytes swapCallData;
        address loanToken;
        address collateralToken;  // токен, который ТРАТИТ своп: коллатерал рынка либо его ERC4626-андерлаинг
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
    event OwnerChanged(address indexed from, address indexed to);
    event AuthorizedChanged(address indexed who, bool ok);
    event BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason);
    /// @notice Намеренно бессодержательно: арм не должен раскрывать цель.
    event PlanSet(uint64 deadline);
    event PlanExecuted(address indexed borrower, uint256 profit);
    /// @notice #70: неудача цели обязана быть слышимой — 4 байта причины, без borrower.
    event PlanFailed(bytes4 reason);
    /// @notice #73: сколько коллатерала контракт РЕАЛЬНО получил перед свопом.
    event SwapInput(uint256 collateral);
    /// @notice Новое в v6: горячий указатель переставлен.
    event HotSet(uint8 g0, uint8 count, uint64 until);

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

    function setAuthorized(address who, bool ok) external onlyOwner {
        isAuthorized[who] = ok;
        emit AuthorizedChanged(who, ok);
    }

    /// @notice 110 ног авторизуются одной-двумя tx, а не 110 (§3).
    function setAuthorizedBatch(address[] calldata who, bool ok) external onlyOwner {
        for (uint256 i = 0; i < who.length; i++) {
            isAuthorized[who[i]] = ok;
            emit AuthorizedChanged(who[i], ok);
        }
    }

    // --- арм доски -----------------------------------------------------------------------

    /// @notice Заголовок группы + цели. Дорого (~1.57M газа на цель с боевым роутом 2 КБ),
    /// поэтому делается ЗАРАНЕЕ, в тишине; в горячий момент двигается только `_hot`.
    /// @dev Цели с индексами >= ts.length НЕ затираются (это стоило бы газа за слоты, которые
    /// цикл огня всё равно не читает: он ограничен `n`). Полная зачистка — `clearGroup`.
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

    /// @notice Докладка каскада: перезапись диапазона целей БЕЗ заголовка (mp/deadline/gate целы).
    /// @dev `n` расширяется до конца диапазона — иначе дописанные цели были бы недостижимы
    /// (цикл огня ограничен `n`). Сужать `n` эта функция не может: для этого есть clearGroup.
    function setTargets(uint256 g, uint8 from, Target[] calldata ts) external onlyOwner {
        uint256 end = uint256(from) + ts.length;
        if (g >= 8 || end > 8) revert TooManySlots();
        for (uint256 i = 0; i < ts.length; i++) _targets[g][from + i] = ts[i];
        Group storage gr = _groups[g];
        if (end > gr.n) gr.n = uint8(end);
        emit PlanSet(gr.deadline);
    }

    /// @notice Горячий указатель: одна дешёвая запись перенаправляет огонь ВСЕГО флота.
    /// @dev Валидность спана НЕ проверяется на записи намеренно: гард стоит на ЧТЕНИИ (fallback),
    /// где он тотален и стоит десятки газа. Бессмысленный указатель = отсутствие огня, а не
    /// поломанный арм; `clearHot()` = `setHot(0,0,0)`.
    function setHot(uint8 g0, uint8 count, uint64 until) public onlyOwner {
        _hot = Hot({g0: g0, count: count, until: until});
        emit HotSet(g0, count, until);
    }

    function clearHot() external onlyOwner {
        setHot(0, 0, 0);
    }

    /// @notice Полное разоружение группы: заголовок + все 8 целей (возврат газа за очистку).
    function clearGroup(uint256 g) external onlyOwner {
        if (g >= 8) revert TooManySlots();
        delete _groups[g];
        for (uint256 i = 0; i < 8; i++) delete _targets[g][i];
    }

    /// @notice Погасить ОДНУ цель (например, ту, что стабильно отказывает и держит очередь).
    function clearTarget(uint256 g, uint256 i) external onlyOwner {
        if (g >= 8 || i >= 8) revert TooManySlots();
        delete _targets[g][i];
    }

    // --- чтения (приёмка арма ведётся по цепи, не по хэшу tx) -----------------------------

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
        returns (address borrower, uint128 repaidShares, uint128 seizedAssets, uint256 triggerPrice)
    {
        Target storage t = _targets[g][i];
        return (t.borrower, t.repaidShares, t.seizedAssets, t.triggerPrice);
    }

    function hotInfo() external view returns (uint8 g0, uint8 count, uint64 until) {
        return (_hot.g0, _hot.count, _hot.until);
    }

    // --- горячий путь --------------------------------------------------------------------

    /// @notice Проба: <= 1 байта калдаты, тихий no-op во всех случаях кроме сложившегося огня.
    /// ПОРЯДОК = ЦЕНА ПРОМАХА и менять его нельзя (§4): господствующий случай — промах, он
    /// обязан отработать на двух SLOAD'ах и одном oracle.price(), НЕ копируя КБ-байты плана.
    ///
    /// Байт пробы:
    ///   0xFF — сентинел: группа берётся из `_hot` (пробы ПРЕДПОДПИСАНЫ, байт запечён в подпись;
    ///          перецеливание через сторадж — единственный способ не пересобирать лестницы);
    ///   1..8 — явная группа (дрели, движковый прямой огонь, хедж);
    ///   иначе (0x00 и 9..0xFE) — return: случайный нулевой байт стрелять не должен.
    ///
    /// Незнание закрывает гард: пустой/протухший/вылезающий за 8 групп указатель = ОГНЯ НЕТ.
    /// Но неизвестная ЦЕНА (§2.2) — не незнание доски, а отсутствие фильтра: она выключает
    /// персональный порог, а не запирает огонь (авторитет ликвидируемости — Morpho, триггер
    /// это только газовый фильтр). Ровно этим v6 отличается от прибора-прототипа.
    fallback() external {
        if (msg.data.length > 1) revert();
        if (msg.data.length == 0) return;
        uint256 b = uint256(uint8(msg.data[0]));
        uint256 g;
        if (b == 0xFF) {
            uint256 cnt = _hot.count;
            if (cnt == 0) return;                       // указателя нет
            if (_hot.until <= block.timestamp) return;  // протух
            uint256 g0 = _hot.g0;
            if (g0 + cnt > 8) return;                   // спан вылезает за доску
            // count > 1: ноги детерминированно расходятся по спану БЕЗ пересборки лестниц.
            g = cnt == 1 ? g0 : g0 + (uint160(msg.sender) % cnt);
        } else if (b >= 1 && b <= 8) {
            unchecked { g = b - 1; }
        } else {
            return;
        }
        Group storage gr = _groups[g];
        uint64 dl = gr.deadline;
        if (dl == 0 || block.timestamp > dl) return;
        if (msg.sender != owner && !isAuthorized[msg.sender]) return;   // гриферы не жгут план
        uint256 gate = gr.gateTrigger;
        uint256 px;
        bool pxKnown;
        if (gate != 0) {
            try IOracle(gr.mp.oracle).price() returns (uint256 p) {
                if (p > gate) return;      // здоров по цене: КБ-байты плана НЕ копируются
                px = p;
                pxKnown = true;
            } catch {}                     // §2.2: оракул сломан -> фильтр ВЫКЛЮЧЕН, не запрет
        }
        uint256 n = gr.n;
        if (n == 0) return;
        MarketParams memory mp = gr.mp;
        Target[8] storage ts = _targets[g];
        // §2.4: старт цикла = СМЕЩЕНИЕ НОГИ, обход кольцевой. Без состояния и без счётчиков
        // отказов (учёт на денежном пути запрещён). Иначе цель, которая стабильно отказывает,
        // стоя в голове, запирает ВСЮ группу: «одна проба = одна попытка», а отказ цель не
        // гасит — и потолок ёмкости, ради снятия которого строится v6, возвращается через путь
        // отказа, в 8 раз хуже, чем в v5. На промахе цикл не исполняется вовсе => цена промаха
        // не меняется; покрытие не теряется (нога обходит всё кольцо).
        uint256 start = uint160(msg.sender) % n;
        for (uint256 k = 0; k < n; k++) {
            uint256 i = start + k;
            if (i >= n) i -= n;
            Target storage st = ts[i];
            if (st.borrower == address(0)) continue;    // погашенная цель
            uint256 t = st.triggerPrice;
            // Персональный порог применяется ТОЛЬКО при известной цене. Неизвестная цена
            // (оракул ревертнул ИЛИ гейта нет вовсе) = фильтра нет = пробуем.
            if (pxKnown && t != 0 && px > t) continue;
            Target memory tg = st;                      // копия КБ-байтов ТОЛЬКО здесь
            try this.liquidateEx(
                mp, tg.borrower, tg.seizedAssets, tg.repaidShares, tg.swapTarget, tg.swapCallData,
                tg.minProfit, tg.unwrapTarget
            ) returns (uint256 profit) {
                delete ts[i];              // односрельность: гаснет РОВНО отстрелянная цель
                emit PlanExecuted(tg.borrower, profit);
            } catch (bytes memory reason) {
                emit PlanFailed(bytes4(reason));        // #70: молчаливый отказ хуже отказа
            }
            return;                        // одна проба = одна попытка
        }
    }

    // --- денежный путь (v5 бит-в-бит, кроме К1) -------------------------------------------

    /// @notice v5-ABI (7 аргументов) сохранён как обёртка: обвязка бота и `batchLiquidate`
    /// не ломаются. `seizedAssets = 0` => Morpho гасит `repaidShares` (поведение v5).
    function liquidate(
        MarketParams calldata mp,
        address borrower,
        uint256 repaidShares,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit,
        address unwrapTarget
    ) external onlyOwnerOrSelf nonReentrant returns (uint256 profit) {
        return _liquidate(mp, borrower, 0, repaidShares, swapTarget, swapCallData, minProfit,
                          unwrapTarget);
    }

    /// @notice К1 (§2.3): заявка ЛИБО по `seizedAssets`, ЛИБО по `repaidShares` — ровно одно
    /// ненулевое (проверяет Morpho). По `seizedAssets` вход свопа известен ДО подписи calldata,
    /// поэтому запечённая сумма свопа точна по построению (закрывает класс #79).
    function liquidateEx(
        MarketParams calldata mp,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit,
        address unwrapTarget
    ) external onlyOwnerOrSelf nonReentrant returns (uint256 profit) {
        return _liquidate(mp, borrower, seizedAssets, repaidShares, swapTarget, swapCallData,
                          minProfit, unwrapTarget);
    }

    /// @dev Тело v5.liquidate() без изменений, кроме проброса `seizedAssets` в Morpho. Общее
    /// для обоих входов намеренно: две копии денежного пути разъезжаются.
    function _liquidate(
        MarketParams memory mp,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        address swapTarget,
        bytes memory swapCallData,
        uint256 minProfit,
        address unwrapTarget
    ) internal returns (uint256 profit) {
        // asset() читается с цепи: токен, который тратит своп, не может разойтись с redeem()
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
        IMorpho(MORPHO).liquidate(mp, borrower, seizedAssets, repaidShares, data);
        uint256 balAfter = IERC20(mp.loanToken).balanceOf(address(this));

        profit = balAfter - balBefore;
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        _safeTransfer(mp.loanToken, owner, balAfter);   // выметаем всё, включая прежнюю пыль
        emit Liquidated(borrower, mp.loanToken, profit);
    }

    /// @notice Каскадный захват в ОДНОЙ tx (v5 бит-в-бит; ABI элемента не менялся).
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

    /// @notice Колбэк Morpho: коллатерал уже получен, меняем его на loanToken и убеждаемся,
    /// что покрываем `repaidAssets` (Morpho заберёт их сразу после возврата).
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external {
        if (msg.sender != MORPHO) revert NotMorpho();
        SwapData memory s = abi.decode(data, (SwapData));

        if (s.unwrapTarget != address(0)) {
            IERC4626(s.unwrapTarget).redeem(
                IERC20(s.unwrapTarget).balanceOf(address(this)), address(this), address(this)
            );
        }
        uint256 collBal = IERC20(s.collateralToken).balanceOf(address(this));
        emit SwapInput(collBal);          // #73: ФАКТИЧЕСКИЙ вход свопа против нашей оценки
        _forceApprove(s.collateralToken, s.swapTarget, collBal);
        (bool ok, ) = s.swapTarget.call(s.swapCallData);
        if (!ok) revert SwapFailed();
        _forceApprove(s.collateralToken, s.swapTarget, 0);

        if (IERC20(s.loanToken).balanceOf(address(this)) < repaidAssets) revert CannotRepay();
        _forceApprove(s.loanToken, MORPHO, repaidAssets);
    }

    function sweep(address token) external onlyOwner {
        _safeTransfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    // --- ERC20-хелперы с проверкой return-data (v5 бит-в-бит) -----------------------------

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
