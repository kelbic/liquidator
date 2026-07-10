# P2-10 «STANDING-СПАМ НА НОЖЕ» — контроль-план для агента-исполнителя (слабая LLM)

> Живой бот ликвидаций Base/Morpho, `MODE=execute`, фондированный ключ. Фича шлёт РЕАЛЬНЫЕ
> транзакции. Исполнитель — слабая модель, поэтому план механический: точные якоря, встроенные
> assert'ы, STOP-гейт владельцу после каждого шага, нулевая импровизация.
>
> План проверен адверсариально (ревьюер искал шаги, где слабая модель теряет деньги / кирпичит
> боевой путь / оставляет kill-switch слепым / вооружает спам раньше предохранителей). Все 10
> найденных дыр закрыты и помечены в тексте как **[FIX]**.

---

## 0. ЗАПОЛНИТЬ ВЛАДЕЛЬЦУ ПЕРЕД СТАРТОМ

```
CLONE=/root/_clone_liq          # клон для dry-run (агент работает ТОЛЬКО здесь)
PROD=/root/liquidator           # прод (правит ТОЛЬКО владелец, агент НЕ трогает)
```

- **[FIX путей]** Агент работает исключительно в `$CLONE`, все команды — абсолютными путями от
  `$CLONE`. Первое действие КАЖДОГО шага: `test -f $CLONE/<file> && echo TREE_OK || echo STOP-NO-TREE`.
  Любое обращение к `$PROD` из-под агента ЗАПРЕЩЕНО (правило изоляции VPS). `cd` без абсолютного
  пути запрещён — cwd между bash-вызовами сбрасывается.
- Стартовые ARM-значения (для шага 9, вписывает владелец в прод-`.env` СВОЕЙ рукой):
  `STANDING_TIP_GWEI=0.02  STANDING_MAX_GAS_USD=2  STANDING_MAX_INFLIGHT=1  STANDING_MIN_DEBT_USD=2000  STANDING_MAX_REPAID_USD=20000  STANDING_WHITELIST=<mid одного глубокого рынка, напр. cbBTC/USDC>`.

**Почему такая дисциплина (суть фичи).** Некоторые позиции долго висят на HF≈1.000x. Когда они
флипают <1, победитель берёт их АТОМАРНО в том же блоке, что и апдейт цены (класс gap≤1) — наш
block-polling структурно опаздывает на 1 блок и проигрывает. Единственный публичный способ взять
этот класс — держать «наготове» ликвидационную tx КАЖДЫЙ блок для кандидатов у самого ножа, с
НИЗКИМ standing-tip, fire-and-forget. Почти все попытки ревертят (позиция ещё здорова → Morpho
ревертит → потеря = только газ, гарантия контракта `minProfit`). Один блок, где флипнуло, мы берём
атомарно. Экономика: реверт ~$0.02; час висения ~1800 блоков ~$36; break-even win-rate 0.024–0.061%.
**Риск — не капитал (контракт защищает), а ОБЪЁМ газа спама + взаимодействие с боевым nonce.**

---

## 1. РОЛИ И ЖЕЛЕЗНЫЕ ГРАНИЦЫ

- **Агент-исполнитель (слабая модель)** только: синхронизирует `$CLONE`, готовит `.bak` + patcher
  + проверки ОДНИМ сообщением, гоняет проверки НА КЛОНЕ, печатает РЕАЛЬНЫЙ stdout, затем **STOP** и
  отдаёт блок владельцу. Агент НЕ деплоит в прод, НЕ вооружает, НЕ трогает `.env`, НЕ трогает
  kill-switch-пороги.
- **Владелец (человек)** — гейт после каждого шага: применяет патч в прод, присылает вывод, решает
  go/no-go; на шаге 9 лично вооружает через `.env`.
- **[FIX] Проверка засчитана ТОЛЬКО при приложенном настоящем stdout процесса (с exit-code).**
  Ручной ввод «ожидаемой» строды запрещён — если проверка не запускалась, шаг не пройден.

---

## 2. ИНВАРИАНТ ПОРЯДКА (безопасность строго ВПЕРЁД отправки)

Ничто не может отправить tx, пока все предохранители не стоят и не проверены. Отсюда порядок:
1. **config-поля** (мёртвый код, `standing_spam=False`) — фундамент, ноль отправок.
2. **фикс nonce-gap** — высокочастотный fire-and-forget кратно повышает шанс застрявшего `_LOCAL_NONCE`, а это КИРПИЧ боевого пути. Чинить ДО всего.
3. **store: миграция + учёт газа ревертов** — данные для коллектора и под-капа.
4. **store: WAL + busy_timeout** — коллектор = параллельный писатель, БД обязана быть WAL до него.
5. **kill-switch под-кап** (первая проверка `blocked_reason`) + все 3 точки GuardState.
6. **receipt-коллектор + heartbeat** — единственный источник газа ревертов; без него kill-switch слеп.
7. **standing-селектор** — LOG-ONLY, ноль отправок.
8. **standing-отправитель за флагом OFF** — свой tip, свой inflight, dedup-namespace, health-gate.
9. **вооружение** — owner-only, поэтапно.

---

## 3. ГЛОБАЛЬНЫЕ ЗАПРЕТЫ (нарушение = потеря денег / кирпич / слепой kill-switch)

1. **НЕ менять** `cfg.tip_gwei` (config.py) и строку `tip = int(cfg.tip_gwei * 1e9)` (execute.py) —
   это боевой tip; его подмена уронит все боевые включения. Standing-tip ставится ТОЛЬКО через
   `prep['tip']`/`prep['max_fee']` ПОСЛЕ `prepare_fast`.
2. **[FIX] НЕ писать в `.env` и НЕ ставить `standing_spam=on/true/True` НИГДЕ.** Готовые строки `.env`
   агент кладёт только текстом в STATE.md с пометкой «применяет владелец». Дефолт в config.py —
   `False`, менять на `True` запрещено.
3. **[FIX] Флаг проверять литералом `if cfg.standing_spam is True`** (не `getattr(...,True)`, не
   truthy) — это единственный настоящий backstop нулевого расхода.
4. **НЕ трогать** общие пороги kill-switch (`max_daily_*`, `max_inflight`) — под-кап ДОБАВЛЯЕТСЯ
   отдельным полем.
5. **НЕ вызывать `prepare_fast` и не слать prep без переопределения `prep['tip']`/`prep['max_fee']`
   на `standing_tip_gwei`** — иначе каждый блочный выстрел уйдёт с боевым tip (газ-разорение).
6. **НЕ писать standing в ОБЩИЙ `_RECENT_SENDS`** — только namespace `'standing'`, иначе дешёвый
   low-tip standing подавит боевой high-tip той же жертвы на TTL (прямая потеря профита).
7. **НЕ снижать** `minProfit` floor (hotpath, `repaid*(LIF-1)/2`) и net-гейт.
8. **НЕ ставить** standing-логику в чистую `block_driven_loop` — `prepare_fast` требует
   `_fresh_basefee()` и `_FULL_PARAMS_CACHE`, которые наполняет ТОЛЬКО `hot_loop` → иначе `ok:False`
   (фича молча мертва). Standing принадлежит `hot_loop`.
9. **НЕ читать `hf_cache`** из `hot_loop` — это локальная переменная `block_driven_loop`, в hot_loop
   NameError/stale. Брать СВЕЖИЙ assess по `shared.groups` с block-меткой.
10. **[FIX] НЕ ломать префикс `submitted:`** в `update_action_receipt` (формат
    `status='submitted:'||receipt_status`) — иначе строка выпадет из `WHERE status LIKE 'submitted:%'`
    и газ снова исчезнет.
11. **НЕ ставить** standing-ветку в `blocked_reason` ПОСЛЕ общего gas-капа — под-кап должен быть ПЕРВОЙ проверкой.
12. **НЕ забыть** расширить ВСЕ ТРИ точки `GuardState` (main block, main poll, hotpath hot) — частичное = дырявый под-кап.
13. **НЕ вооружать** без живого receipt-коллектора и свежего heartbeat.
14. **[FIX] Правки только аддитивные** (новые поля с дефолтами, новые опц. параметры, новые методы/
    модули). Никаких переименований/перестановок существующих сигнатур.
15. **НЕ пушить с красными тестами** (`test_live_detector`/`test_backtest_detection`/`test_route_a_builder`;
    при правках контракта — `forge test`). Красный тест → чинить, не пушить.
16. **НЕ ALTER на живой БД без датированного бэкапа**; миграцию проверять на КОПИИ.
17. Не трогать twidgest-bot, не логировать/коммитить секреты и `WALLET_KEY`, ничего не отправлять наружу.

---

## 4. ПОШАГОВЫЙ ПЛАН (9 шагов; каждый: подготовка на `$CLONE` → проверки → STOP → владелец)

Общий скелет каждого шага (стиль docs/WORKFLOW.md): `.bak` с датой → patcher-heredoc с
`assert t.count(old)==1` на каждый якорь → новые файлы целиком в том же сообщении → блок проверок
(py_compile + import + AST-затенение + symtable-скоуп + stdlib-юнит + smoke) с ОЖИДАЕМЫМ выводом →
commit+push при зелёных боевых тестах → **STOP, отдать владельцу**.

### Шаг 1 — Config: поля `standing_*` (мёртвый код, OFF)
- **Файл:** `$CLONE/config.py`. **[FIX] +поле `standing_whitelist: str = ""`** (пусто = ни один рынок
  не разрешён, containment по умолчанию).
- **Патч:** после якоря `fast_lane_min_repaid_usd: float = 1000.0 ...` добавить поля:
  `standing_spam=False, standing_tip_gwei=0.05, standing_max_gas_usd=5.0, standing_hf_low=1.0,
  standing_hf_high=1.002, standing_min_debt_usd=2000.0, standing_max_repaid_usd=20000.0,
  standing_max_inflight=1, standing_whitelist=""`. Зеркально в `from_env` (якорь
  `fast_lane_min_repaid_usd=f(...)`), парсинг тем же `s/i/f`-паттерном; `standing_whitelist=s("STANDING_WHITELIST","")`.
- **Проверки:** `py_compile`; `import` c `assert c.standing_spam is False`; symtable; `grep -c 'standing_'` ≥ 18.
- **Гейт:** GO только при `CONFIG OK standing_spam= False`. **Откат:** `.bak`.

### Шаг 2 — Фикс nonce-gap (предусловие №1)
- **Файл:** `$CLONE/chain/execute.py`. **Шаг 0:** `sed -n '570,585p' $CLONE/chain/execute.py | cat -A` —
  снять ЛИТЕРАЛЬНЫЙ текст receipt-timeout-ветки (где книжится убыток по таймауту).
- **[FIX] Якорь — литеральная строка из cat -A, `assert count==1`.** В `new` сразу после книжки
  убытка добавить `_LOCAL_NONCE.pop(bot, None)  # P2-10: дроп/timeout -> сбросить локальный nonce`.
  Отступ из cat -A (пробелы). Больше НИЧЕГО не трогать (`_reconcile_nonce`, advance, pop-на-throw — как есть).
- **[FIX] Проверки:** py_compile; AST-затенение; **symtable-скоуп: новый `pop` находится ИМЕННО в
  timeout-ветке** (проверить по имени обрабатывающей функции/ветки, не просто «два pop в файле»);
  `grep -c '_LOCAL_NONCE.pop'` == 2; **юнит multi-inflight:** base=10, in-flight 11/12/13, дроп 11 →
  heal НЕ приводит к повторной выдаче 12/13 и `_LOCAL_NONCE.get(bot) is None` после timeout.
- **Гейт:** GO при `NONCE-HEAL OK` + боевые тесты зелёные + на dry-run в monitor нет регрессии в
  dispatch-логах. STOP при любом Traceback в dispatch. **Откат:** `.bak` + рестарт.

### Шаг 3 — Store: миграция + `update_action_receipt` + `standing_realized_today`
- **Файл:** `$CLONE/store.py`. Бэкап ЖИВОЙ БД делает ВЛАДЕЛЕЦ на проде перед деплоем; агент мигрирует
  только КОПИЮ.
- **Патчи:** (#1 SCHEMA) после `status TEXT ...` добавить столбцы `kind TEXT, block_number INTEGER,
  collected INTEGER DEFAULT 0`. (#2 миграция) после `c.executescript(SCHEMA)` — идемпотентный
  `ALTER TABLE actions ADD COLUMN` в try/except (дубликат-колонка на уже-мигрированной БД — pass).
  (#3 методы) после `log_action`: `update_action_receipt(tx_hash, gas_usd, net_usd, receipt_status)`
  → `UPDATE ... SET gas_usd=?, net_usd=?, status='submitted:'||?, collected=1 WHERE tx_hash=?`
  (**[FIX] префикс `submitted:` СОХРАНЯЕТСЯ**); `standing_realized_today()` → SUM(net),SUM(gas)
  `WHERE ts>=since AND mode='execute' AND kind='standing' AND status LIKE 'submitted:%'`.
- **[FIX] `since` вычислять ТЕМ ЖЕ выражением/хелпером, что и боевой `realized_today()`** (не
  переписывать окно заново). Не трогать `realized_today()`.
- **Проверки:** py_compile; **миграция на КОПИИ** старой БД (`MIGRATION OK` + столбцы есть);
  идемпотентность (повторный `Store()` не падает); `RECEIPT-UPDATE OK` (после update газ>0 И статус
  начинается с `submitted:`); `ISOLATION OK` (боевые kind=NULL не в standing-SUM); **[FIX] `SINCE-PARITY OK`**
  (standing-строка «вчера» не считается; граница окна == границе `realized_today`).
- **Гейт:** GO при всех OK. STOP если после update газ выпал из SUM. **Откат:** `.bak` (ALTER аддитивен, БД не откатывать).

### Шаг 4 — Store: WAL + busy_timeout
- **Файл:** `$CLONE/store.py`. **Патч:** после `conn = sqlite3.connect(self.path)` добавить
  `PRAGMA journal_mode=WAL` и `PRAGMA busy_timeout=5000`.
- **Проверки:** `WAL OK wal`; нагрузка-smoke 2 потока × 500 записей без `database is locked` (`CONTENTION OK`).
- **Гейт:** GO при обоих. **Откат:** `.bak`.

### Шаг 5 — Kill-switch: под-кап + все 3 точки GuardState
- **Файлы:** `$CLONE/strategy/guard.py`, `$CLONE/main.py`, `$CLONE/chain/hotpath.py`. **Шаг 0:**
  `grep -n 'GuardState(' main.py chain/hotpath.py` и `grep -n 'KillSwitch(' main.py` — зафиксировать РЕАЛЬНЫЕ точки.
- **Патчи:** GuardState +`standing_gas_spent_today: float = 0.0`; KillSwitch `__init__` +параметр
  `standing_max_gas_usd: float = 0.0` и `self.standing_max_gas_usd=...`; в `blocked_reason` вставить
  standing-ветку **ПЕРВОЙ** (`if self.standing_max_gas_usd and st.standing_gas_spent_today >
  self.standing_max_gas_usd: return ...`); в `main.py` конструктор KillSwitch +`cfg.standing_max_gas_usd`;
  в КАЖДОЙ из 3 точек `GuardState(...)` +`standing_gas_spent_today=store.standing_realized_today()["gas"]`.
- **[FIX] Проверки-assert'ы:** `ORDER OK` (под-кап срабатывает РАНЬШЕ общего gas-капа); `BACKCOMPAT OK`
  (дефолты не ломают старые вызовы); **`grep -c 'standing_gas_spent_today=store.standing_realized_today'`
  == 3**; **`grep -n 'KillSwitch(' main.py` содержит `cfg.standing_max_gas_usd` (`SUBCAP-WIRED OK`)** —
  иначе под-кап получит дефолт 0.0 и МОЛЧА выключится; **runtime-assert на старте:** если
  `cfg.standing_max_gas_usd>0`, а `guard.standing_max_gas_usd==0.0` → громкое исключение.
- **Гейт:** GO при `ORDER OK` + ровно 3 точки + `SUBCAP-WIRED OK` + `BACKCOMPAT OK`. STOP если ветка
  не первая или точек <3. **Откат:** 3 `.bak` + рестарт.

### Шаг 6 — Receipt-коллектор + heartbeat health-gate
- **Файлы:** новый `$CLONE/chain/standing_collector.py` + патчи `main.py`, `chain/hotpath.py`.
- **Модуль:** `run_standing_collector(rpc, cfg, store, heartbeat, stop_evt)`: цикл → выбрать
  `kind='standing' AND collected=0` с непустым `tx_hash` → `getTransactionReceipt` → **[FIX]
  `realized_econ(profit_usd=0.0, gasUsed, effectiveGasPrice, status, cfg.eth_price_usd)` —
  ЕДИНОЕ имя символа из `chain.execute`, импортировать оттуда, формулу НЕ дублировать** →
  `update_action_receipt(...)` → `heartbeat['ts']=time.monotonic()` каждую итерацию → `sleep(~2с)`.
  web3 импортировать лениво.
- **Патчи:** в `store.log_action(...)` (hotpath, район отправки) прокинуть `kind=r.get("kind"),
  block_number=r.get("block"), tx_hash=r.get("hash")`; в `main.py` рядом с KillSwitch стартовать
  daemon-тред коллектора (общий `heartbeat={}`), тред стартует всегда, боевой газ не портит (фильтр kind).
- **Проверки:** py_compile; AST; symtable (коллектор не тянет `hf_cache`); `COLLECTOR OK` (реверт
  status=0 → газ учтён, net<0, collected=1, heartbeat свеж); `COLLECTOR ISOLATION OK` (боевые kind=NULL
  не тронуты); **[FIX] `ECON-PARITY OK`** (коллектор и боевой путь дают идентичный `gas_usd` на
  одинаковых gasUsed/effectiveGasPrice); smoke старта на клоне (monitor) — тред стартует, 0 Traceback.
- **Гейт:** GO при `COLLECTOR OK`+`ISOLATION OK`+`ECON-PARITY OK`+heartbeat растёт. **Откат:** удалить модуль + 2 `.bak` + рестарт.

### Шаг 7 — Standing-селектор — LOG-ONLY (без dispatch)
- **Файл:** `$CLONE/chain/hotpath.py`. Точка — блок `if bn != last_poll_block:` (уникальный, per-block).
- **Патч:** чистая функция `_standing_select(cfg, groups_snapshot, hf_by_block, by_id)` → список
  `(mid,borrower,repaid_usd)` с фильтром: `standing_hf_low<=hf<standing_hf_high`, `coll.lower() in
  ROUTE_COLLATERALS`, `direct_swap(...) is not None`, `standing_min_debt_usd<=repaid<=standing_max_repaid_usd`,
  **[FIX] `mid in cfg.standing_whitelist`** (пустой whitelist → 0 кандидатов). Для каждого —
  `log.info('standing-candidate ...')`. **БЕЗ `prepare_fast`, БЕЗ dispatch.** HF из СВЕЖЕГО assess
  по `shared.groups` (не `hf_cache`).
- **[FIX] Проверки:** py_compile; **statический assert `grep -nE 'dispatch|prepare_fast|send_raw|eth_sendRaw'`
  по диапазону патча == 0** (`LOG-ONLY OK`); symtable (`_standing_select` не тянет `hf_cache` как
  свободное имя); юнит `SELECT OK` (1 из 4 mock: нож+direct+whitelist+в [min,max] проходит, остальные
  отсеяны); `BOUNDS OK` (hf=1.002 отсеян, hf=1.0 выбран); **`WHITELIST-EMPTY OK`** (пустой whitelist → 0
  кандидатов); smoke — в логах есть `standing-candidate`, recv-loop стабилен.
- **Гейт:** владелец сутки на клоне (monitor, hot_path=on, standing_spam=False) собирает логи. GO если
  кандидаты вменяемы (deep-рынки, repaid в [min,max], HF в ноже), нет мусора. **Откат:** `.bak` + рестарт.

### Шаг 8 — Standing-отправитель за флагом OFF
- **Файлы:** `$CLONE/chain/hotpath.py`, `$CLONE/chain/execute.py`.
- **Патч (hotpath):** внутри standing-блока `if cfg.standing_spam is True and _heartbeat_fresh(heartbeat):`
  (**[FIX] литерал `is True`**) для каждого кандидата: `prep=prepare_fast(...)`; проверить `prep.get('ok')`;
  **переопределить** `_st=int(cfg.standing_tip_gwei*1e9); prep['tip']=_st; prep['max_fee']=_fresh_basefee()*2+_st;
  assert prep['tip']==int(cfg.standing_tip_gwei*1e9)`; собрать `ready=[(mid,borrower,prep)]` с полями
  `kind='standing', block=bn`; dispatch с `wait_receipts=False, max_inflight=cfg.standing_max_inflight,
  dedup_ns='standing'`. `_heartbeat_fresh`: `monotonic()-heartbeat.get('ts',0) < 10`. Дедуп
  standing-vs-боевой на `(mid,borrower,block)`: пропустить, если уже ушёл боевым в этом блоке.
- **Патч (execute):** `dispatch_liquidations` +опц. параметр `dedup_ns=''` (дефолт=боевой); ключ дедупа
  `(dedup_ns, mid, borrower)`. НЕ трогать `tip = int(cfg.tip_gwei * 1e9)` и config.py боевой tip.
- **[FIX] Проверки:** `BOEVOY TIP OK` (`grep` боевой tip нетронут, боевой prep ушёл бы с 3.0);
  `STANDING TIP OK` (override → 0.05); `DEDUP-NS OK` (боевой не подавлен standing-записью);
  `HEALTH-GATE OK` (мёртвый heartbeat → не шлёт); **`OFF OK` (standing_spam=False → dispatch не зван)
  И `OFF-MISSING-ATTR OK` (cfg без атрибута standing_spam → тоже не шлёт)**; AST+symtable; боевые тесты
  зелёные; smoke (monitor, standing_spam=False) — dispatch standing не зван.
- **Гейт:** GO при всех OK + боевые тесты зелёные. STOP если боевой tip изменился или OFF-ветка зовёт
  dispatch. **Откат:** 2 `.bak` + рестарт.

### Шаг 9 — ВООРУЖЕНИЕ (owner-only) — **[FIX] агент делает 0 изменений в исполняемом коде**
- **Файлы:** только `$CLONE/docs/STATE.md` (журнал). **[FIX] `.env` УБРАН из редактируемых агентом
  файлов** — агент кладёт готовые `.env`-строки ТОЛЬКО текстом в STATE.md с пометкой «применяет
  владелец вручную». Агент НЕ трогает config.py:`standing_spam` (остаётся False; `.env` перебивает).
- **Владелец** лично применяет `.env` постадийно (протокол §5), рестартит, наблюдает.

---

## 5. ПРОТОКОЛ ВООРУЖЕНИЯ (owner-only, монотонный, с замером)

- **Стадия 0 — предусловия (все зелёные ДО любого сенда):** nonce-healer (шаг 2); миграция+receipt-update
  сохраняет `submitted:` (шаг 3); WAL (шаг 4); под-кап — ПЕРВАЯ проверка, 3 точки GuardState,
  `SUBCAP-WIRED OK` (шаг 5); коллектор жив, heartbeat свеж, реверт→газ (шаг 6); боевой tip нетронут,
  dedup-namespace, health-gate, OFF+OFF-MISSING-ATTR, whitelist пуст→0 (шаги 7–8). Любой красный → вооружение запрещено.
- **Стадия 1 — OFF/log-only, сутки:** `standing_spam=off`, `hot_path=on`. Кандидаты только глубокие
  direct-рынки из whitelist, repaid в [min,max], HF в [1.0,1.002), без мусора.
- **Стадия 2 — микро-ARM:** владелец в прод-`.env`: `STANDING_SPAM=on, STANDING_TIP_GWEI=0.02,
  STANDING_MAX_GAS_USD=2, STANDING_MAX_INFLIGHT=1, STANDING_WHITELIST=<один рынок>`. Наблюдение 1–2ч.
  GO (всё одновременно): `standing_realized_today()['gas']` РАСТЁТ (kill-switch видит газ); НЕТ
  застрявшего nonce (боевые tx не «queued»); боевые ликвидации не подавлены дедупом и не проиграли
  гонку; при $2 под-кап глушит ТОЛЬКО standing, боевой продолжает. Любой сбой → `STANDING_SPAM=off` немедленно.
- **Стадия 3 — замер экономики, сутки:** реальный standing-газ/сутки против ожидаемого (~$0.02/реверт),
  доля осмысленных попаданий, влияние на латентность боевого dispatch.
- **Стадия 4 — расширение, каждое ОТДЕЛЬНЫМ STOP-гейтом:** по одному параметру за раз
  (под-кап → замер; +рынок в whitelist → замер; inflight → замер latency; tip → замер hit-rate).
  Никогда два сразу. Разоружение всегда: `STANDING_SPAM=off` + рестарт → весь код снова мёртв.

---

## 6. ГОТОВЫЙ ПРОМПТ ДЛЯ АГЕНТА-ИСПОЛНИТЕЛЯ (вставить как задачу)

```
Ты внедряешь фичу P2-10 (standing-спам на ноже) в ЖИВОЙ бот ликвидаций Base/Morpho по
приложенному контроль-плану P2-10_CONTROL_PLAN.md. Это деньги под риском. Действуй строго
механически, без импровизации.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
1. Работаешь ТОЛЬКО в клоне $CLONE (см. §0 плана), все команды — абсолютными путями от $CLONE.
   Прод $PROD не трогаешь никогда. Первое действие каждого шага:
   `test -f $CLONE/<файл> && echo TREE_OK || echo STOP-NO-TREE`. Если STOP-NO-TREE — остановись,
   сообщи владельцу, не выдумывай якорь.
2. Делаешь РОВНО ОДИН шаг из §4 за заход, по порядку 1→9. Не сливаешь шаги.
3. Каждый шаг: (а) Шаг-0 — `sed -n .. | cat -A` / `grep -n` сверить РЕАЛЬНЫЙ текст файла, якорь брать
   из ЭТОГО вывода, не из памяти; (б) `.bak` с датой; (в) patcher-heredoc с `assert t.count(old)==1`
   на каждый якорь (якорь встречается дважды — расширь контекстом до count==1, не подавляй); (г) новые
   файлы целиком в том же сообщении; (д) блок проверок из шага; (е) STOP, отдать вывод владельцу.
4. Проверку считаешь пройденной ТОЛЬКО если приложен НАСТОЯЩИЙ stdout запуска (с exit-code).
   НИКОГДА не печатай «ожидаемую» строку вручную. Не запускалось — шаг не пройден.
5. НЕ пиши в .env. НЕ ставь standing_spam=on/true/True нигде. Дефолт config.py остаётся False.
   Флаг в коде — литерал `if cfg.standing_spam is True`, не getattr/truthy.
6. НЕ меняй боевой tip (cfg.tip_gwei / `tip = int(cfg.tip_gwei * 1e9)`). Standing-tip — только через
   prep['tip']/prep['max_fee'] после prepare_fast.
7. Правки только аддитивные (новые поля с дефолтами, новые опц. параметры, новые методы/модули).
   Никаких переименований/перестановок существующих сигнатур.
8. Не пушь с красными тестами (test_live_detector / test_backtest_detection / test_route_a_builder).
9. Прочитай §3 «Глобальные запреты» целиком перед КАЖДЫМ шагом и соблюдай их все.
10. После проверок — ОСТАНОВИСЬ и отдай блок владельцу (он деплоит в прод и присылает вывод).
    Ты НЕ деплоишь в прод, НЕ вооружаешь, НЕ трогаешь kill-switch-пороги. Шаг 9 — только текст в
    STATE.md, ноль изменений в исполняемом коде.

Начни с шага 1. Выведи Шаг-0 (сверку якоря), .bak, patcher и проверки одним сообщением, затем СТОП.
```

---

## 7. КАРТА ПРЕДОХРАНИТЕЛЕЙ (что чем закрыто)

| Угроза | Предохранитель | Шаг |
|---|---|---|
| Runaway газ спама | под-кап `STANDING_MAX_GAS_USD` (первая проверка) + heartbeat health-gate | 5, 6, 8 |
| Kill-switch слеп к газу ревертов | receipt-коллектор → `update_action_receipt` (префикс `submitted:`) → `standing_realized_today` | 3, 6 |
| Кирпич боевого пути (nonce-gap) | сброс `_LOCAL_NONCE` на timeout + multi-inflight юнит | 2 |
| Standing с боевым tip 6.0 | обязательный override `prep['tip']` + `BOEVOY TIP OK` grep-assert | 8 |
| Standing подавляет боевой (дедуп) | dedup-namespace `'standing'` | 8 |
| Спам по тонким/не-direct рынкам | `ROUTE_COLLATERALS`+`direct_swap` фильтр + пустой whitelist по умолчанию | 1, 7 |
| Капитал (профит из воздуха) | контракт `minProfit` floor: здоровая позиция → revert → потеря = только газ | (контракт) |
| Вооружение раньше предохранителей | флаг `is True` + OFF-по-умолчанию + owner-only ARM | 8, 9 |
| Тихий слом предохранителя слабой моделью | встроенные runtime-assert'ы + литеральные якоря + запрет ручного stdout | все |
