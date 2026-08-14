# BATCH_LIQUIDATE — зафиксированный дизайн (2026-07-17, верифицирован кодом и тестами)

> **СТАТУС: НЕ ПОСТРОЕНО. Гейт владельца в силе: строить ПОСЛЕ первого живого выигранного
> submit-xray** (решение 07-10, подтверждено 07-17). Этот док фиксирует дизайн так, чтобы любая
> следующая сессия/модель собрала его без контекста. Дизайн верифицирован: (а) адверсариальная
> проверка утверждений против кода (агенты 07-17, вердикты ниже), (б) **рабочий прототип контракта
> `contracts/test/BatchProto.t.sol` — 10/10 мок-тестов + 2/2 форк-теста на живом Base PASS**.

## Зачем (данные)
- Победители батчат: каскады в данных почти все span=0s (одна tx, несколько Liquidate) — урок 22а.
- Каскады редки, но жирны: 24 hot-каскада/30д (все в 4 стресс-дня 06-23..25, 07-01), «за первым
  призом» $15.9M repaid/30д на covered (пробник 07-17; repaid≠profit, >$1.3M призы убыточны через
  одиночный UniV3 — компаньон-фикс: Slipstream-сплит, бэклог).
- После латентных фиксов 07-17 (канарейка: feed-медиана +14, in-block 100%) приз №1 берётся —
  батч кладёт призы №2..N на ТОТ ЖЕ tx-index (+14 для всех вместо +15..+18 у последовательных nonce).
- Мультикошельки — НЕ строить (разбор 07-11 в HANDOFF: маржинально, дорого, фандинг-шардинг).

## 1. Контракт (2 правки к src/Liquidator.sol — код ПРОВЕРЕН, брать из BatchProto.t.sol)
Верифицированный образец: `contracts/test/BatchProto.t.sol` (контракт `LiquidatorBatch`) — промоутнуть
в `contracts/src/` при сборке. Изменения против боевого Liquidator.sol ровно два + новые члены:
1. `liquidate(...)`: модификатор `onlyOwner` → `onlyOwnerOrSelf`
   (`msg.sender != owner && msg.sender != address(this) → NotOwner`). Селектор/ABI НЕ меняются —
   одиночный путь бота работает с новым контрактом без правок кода.
2. Новая `batchLiquidate(LiquidationItem[] calldata items) external onlyOwner returns (uint256 nOk)`
   — **БЕЗ nonReentrant** (testC2 ДОКАЗАЛ: с guard'ом self-call в nonReentrant-liquidate ревертит
   Reentrant на каждом item, nOk=0). Дыра реентранси НЕ открывается: все входы закрыты
   (onlyOwnerOrSelf / onlyOwner / onlyMorpho; роутер не может войти — testC2b).
   Тело: цикл `try this.liquidate(items[i]...) returns(uint256){nOk++} catch (bytes memory r)
   {emit BatchItemFailed(i, items[i].borrower, bytes4(r));}`.
3. `struct LiquidationItem { MarketParams mp; address borrower; uint256 repaidShares;
   address swapTarget; bytes swapCallData; uint256 minProfit; }` +
   `event BatchItemFailed(uint256 indexed i, address indexed borrower, bytes4 reason)`.

Эмпирика прототипа (все PASS): C1 self-call при onlyOwner ревертит NotOwner (правка №1 обязательна);
C1b батч проходит с onlyOwnerOrSelf; C2 guard на батче убивает все items; C2b реентранси роутером
отбита (снаружи видна как SwapFailed — ассертить флагами в мок-роутере, НЕ expectRevert(NotOwner));
C3 Error(string) Morpho «position is healthy» ловится, батч продолжается; C3b OOG подвызова ловится
(63/64); **C3c тесный gas limit МОЛЧА морит хвост items (nOk<N, tx успешна)** — политика газа ниже
обязательна; C4 два item с одним loanToken: per-item sweep-all, двойного счёта нет; C6 allowance
между items не течёт; кастомные селекторы (ProfitTooLow и т.п.) всплывают в BatchItemFailed.reason.

## 2. Бот-сторона (вердикты верификации против кода, файлы/строки на main@2f12df6)
**prep обязан расширяться** (мина: `prep["calldata"]` — это закодированный ОДИНОЧНЫЙ вызов, для
батча непригоден): prepare_hot (hotpath.py:~716) и prepare_fast (~776) дополнительно кладут сырые
компоненты `mp, borrower, repaid_shares, swap_target, swap_calldata, min_profit` И `debt_usd,
debt_assets` (оба уже аргументы этих функций) — без них в receipt-цикле НЕЧЕМ конвертировать
per-item профит в USD (P3 REFUTED: meta_by в dispatch не передаётся, debt_assets_by — локал).

**config.py**: `batch_liquidate: bool = False` + `batch_max_items: int = 5` (dataclass после
fast_lane_max_consec_reverts, парсеры в from_env по образцу FAST_LANE/MAX_INFLIGHT). Адрес:
редеплой прозрачен → тот же `LIQUIDATOR_ADDRESS` (старый адрес в коммент на откат).

**dispatch_liquidations (execute.py)**: n=1 или флаг OFF → старый путь БЕЗ изменений. Иначе:
- сортировка items по net_usd desc **ПОСЛЕ TTL-дедупа, под _SEND_LOCK** (иначе fee-источник может
  быть выкинут дедупом); срез `[:batch_max_items]` внутри tx, **хвост — следующими tx в пределах
  max_inflight** (не выбрасывать);
- gas: `sum(prep.get("gas", cfg.gas_limit_est)) + 30_000*n`, итог ×1.25 (63/64-мина C3c; блок-луп
  preps БЕЗ gas — .get-фолбэк обязателен, dispatch общий для блок-лупа, main.py:106/147);
- fee: от первого prep, У КОТОРОГО ЕСТЬ max_fee (P1: у prepare_hot поля условны, у блок-лупа нет) —
  иначе не задавать (send_tx доберёт сам);
- nonce: одна tx = base+0, `_LOCAL_NONCE=base+1`; `_RECENT_SENDS` пометить ВСЕ (mid,borrower) (P2 ok);
- inflight: `_inflight_add(1)` на tx (НЕ на item), симметрично в receipt/timeout; wait_receipts=False
  ветка: −1 на tx (иначе гейт guard ложно стопит);
- результат: **N строк** с общим hash (потребители ждут строку на (mid,borrower): hotpath.py:~1087
  store.log_action+TG, main.py:~149 meta[...] KeyError на синтетике).

**receipt-путь (критично, фантомная прибыль запрещена)**: `status==1` НЕ значит «все сели».
Per-item исход — ТОЛЬКО из событий receipt.logs: `Liquidated(borrower indexed, loanToken, profit)`
= успех (profit_usd_i = profit × debt_usd/debt_assets — арифметика _net_gate), `BatchItemFailed(i,
borrower, reason)` = провал. Несколько Morpho.Liquidate в одном receipt — матчить по repaidAssets
(урок 22а). log_action: строка на item, status с префиксом `submitted:` и mode=execute (иначе
kill-switch СЛЕПНЕТ — store.py:80-87 фильтрует по ним); газ батча книжить одной строкой (или
пропорционально), сумма по дню остаётся точной (P4 ok). Timeout-ветка: суммарный est-cost ОДИН раз.

**Предохранители**:
- `_fl_record` — ОДИН раз на receipt, `is_fast=all(p.get("fast"))` (per-item вызовы: один реверт
  батча с 3+ fast-items ложно триггерит II.6-breaker);
- **новая мина: батч «все-фейл» (nOk=0) — tx УСПЕШНА** → невидим и breaker'у (считает реверты), и
  алертам. Нужен счётчик серии пустых батчей (по BatchItemFailed без Liquidated) + TG-пейдж;
- `_submit_xray`: tx-реверт (только top-level/OOG) → цикл по всем N borrowers; частичный успех →
  xray только для borrowers без Liquidated-события;
- апфронт-баланс: секвенсер требует balance ≥ gas_total×maxFee ОДНИМ чеком (~0.015-0.03 ETH при
  n=5) — кошелёк держать ≥0.05 ETH, иначе теряются ВСЕ items разом.

## 3. Тесты перед деплоем
1. Промоутнуть LiquidatorBatch → src/ (заменив Liquidator.sol или новым файлом), портировать
   10 тестов из BatchProto.t.sol + регресс существующего Liquidator.t.sol (форк-гибрид: реальный
   Morpho, мок-токены; НЕ пиннит блок — для регресс-гейта запиннить).
2. Форк-реплей каскада: anvil fork блока cbBTC-обвала 06-25 ($6.6M/134с ≈ 67 блоков) — 2+ реальных
   item БЕЗ мока оракула. Шаги, которых нет в доках: (а) восстановить номер блока (Morpho API
   Liquidation timestamps → блоки); (б) archive-state нужен Alchemy-ключ с Charlotte (публичный RPC
   так глубоко не даёт); (в) items ≤$500k (крупнее — убыточны через одиночный UniV3), swapCallData
   строить под ликвидность ТОГО блока.
3. Foundry есть и в песочнице (~/.foundry/bin, публичный Base RPC работает — STATE «нет Base» устарел
   для тестов) — итерации здесь, archive/деплой на Charlotte.

## 4. Деплой-ранбук (владелец — ключ; ассистент — всё остальное)
1. `forge create src/Liquidator.sol:Liquidator --rpc-url $RPC_URL --private-key $WALLET_KEY
   --broadcast --constructor-args 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb` (README:67-69,
   constructor-args ПОСЛЕДНИМ; owner = деплоящий ключ).
2. `.env` ОБЕИХ машин: LIQUIDATOR_ADDRESS=<новый> (старый `0x54730EE13f10C073772f25ee475EE13f2252Fb82`
   в коммент) + `systemctl restart liquidator-bot` (git pull НЕ рестартит).
3. Соак 1-2 дня на одиночном пути (batch-флаг OFF — поведение бит-в-бит старое, селектор liquidate
   не менялся).
4. BATCH_LIQUIDATE=1 в тихое окно, полная hot-path дисциплина (AST/symtable, смоук, коммит сразу).
5. Откат: вернуть старый адрес + restart; пыль с нового контракта — `sweep()`; оба контракта
   независимы, standing-капитала нет.

## 5. Порядок включения (гейты)
0. **Первый живой выигранный submit-xray** (гейт владельца) → 1. redeploy+соак → 2. батч-флаг →
   3. Slipstream-сплит cbBTC (разблокирует >$1.3M призы каскадов) — отдельной сессией.
