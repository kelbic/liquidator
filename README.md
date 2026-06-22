# liquidator

Бот ликвидаций на **Base** поверх **Morpho Blue**. Стратегия — длинный хвост:
изолированные permissionless-рынки, где конкуренция тоньше, бонус выше (LIF до ~13% на
нашем наборе), а колбэк `liquidate` в Morpho даёт нулевой капитал (контракт получает залог
ДО стяжки погашения — отдельный flash loan не нужен) и почти нулевой honeypot (в контракте
нет стоящих средств, профит свипается владельцу, горячий кошелёк держит только газ).

Третий продукт Kelbic рядом с twidgest/essayist: отдельный репо, отдельный
systemd-сервис, своя SQLite, отдельный клон. Ресурсно изолирован через cgroup —
всплеск здесь не может задеть twidgest (см. `liquidator-bot.service`).

## Статус

Обе фазы в проде. Бот **вооружён** (`MODE=execute`), на блочной wss-петле, с параллельной отправкой.

- **Фаза 1 — monitor.** ✅ Живёт на 40 хвостовых рынках. Перечисляет заёмщиков через
  Morpho API, подтверждает HF on-chain одним батчем Multicall3 (~3-4 eth_call/цикл
  независимо от числа рынков), симулирует профит, пишет в SQLite. Денег под риском ноль.
- **Фаза 2 — execute.** ✅ Собрана, вооружена флагом `MODE=execute`. Контракт `Liquidator`
  задеплоен на Base, форк-тестирован на реальном Morpho. Путь: свежие on-chain чтения → размер
  свопа → KyberSwap-агрегатор → **`simulate_tx`-гейт (eth_call)** → **честный net-флор** →
  подпись+отправка EIP-1559 с floor `minProfit` (реверт, если исполнение даст <95%
  симулированного). Капиталу ничего не грозит: гейтится симуляцией-перед-отправкой и
  kill switch'ем (дневные лимиты потерь/газа, inflight); худший случай — реверт за центы газа.

Режим переключается переменной `MODE` в `.env` (`monitor` | `execute`).

## Скорость и конкуренция

Конкуренция на Base — **латентностная игра, не аукцион ставок** (нет публичного mempool, Flashblocks
~200мс упорядочивают по priority fee только в первом слоте, дальше доминирует латентность). Поэтому:

- **Блочная wss-петля** (`LOOP_MODE=block`): реакция ~2.4с (вместо ~40с поллинга). Подписка на
  `newHeads`, **горячий набор** (на блок ассессятся только позиции у порога HF, ~0.4с; полный
  набор раз в 30с), дренаж буфера (самовыравнивание при лаге RPC). `poll` — фолбэк.
- **Честный net-флор + конкурентный tip:** гейт по NET = profit − (tip×газ в USD), не по гросс-
  профиту свопа. Tip бидится выше наблюдаемого потолка конкурента — но как тай-брейк при равной
  скорости, не как война (на тонком хвосте перебивать в минус). Цена ETH живая (Morpho API).
- **Параллельная отправка:** в каскаде волатильности бот собирает ВСЕ ликвидируемые позиции блока,
  готовит каждую (свежий simulate+флор) и отправляет до `MAX_INFLIGHT` разом — НЕБЛОКИРУЮЩЕ, с
  последовательными nonce. Каскад не сериализуется на одной блокирующей отправке ~2с.
- **Авторотация рынков** каждые 6ч (рескан + hot-swap) с фильтрами: SVR-оракулы (`EXCLUDE_ORACLES`)
  и невыходимый залог (Pendle PT — KyberSwap не маршрутизирует его AMM).

## Как работает ликвидация (zero-capital)

Контракт `contracts/src/Liquidator.sol` (self-contained, без OpenZeppelin-сабмодулей). Вызов
`liquidate` дёргает `morpho.liquidate` с колбэком: Morpho отдаёт залог контракту →
`onMorphoLiquidate` свопает залог→loan-токен через generic `swapTarget`/`swapData` (calldata
бот строит оффчейн у агрегатора) → Morpho стягивает погашение. Профит (бонус LIF минус
стоимость свопа) свипается владельцу. Контракт валидирует ИСХОД (`minProfit`), а не маршрут —
route-agnostic. Гарды: `nonReentrant`, `onlyOwner`(вход)/`onlyMorpho`(колбэк), ERC20 с
проверкой return-data + force-approve.

## Быстрый старт

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # RPC_URL, WALLET_KEY, LIQUIDATOR_ADDRESS, MODE,
                            # LOOP_MODE=block, MAX_INFLIGHT; chmod 600 .env
python3 analysis/build_covered_markets.py covered_markets.json   # отобрать хвостовые рынки

# контракт (нужен Foundry):
cd contracts && forge build && forge test     # форк-тест против Base mainnet (RPC_URL)
# деплой (--constructor-args ДОЛЖЕН быть последним):
# forge create src/Liquidator.sol:Liquidator --rpc-url $RPC_URL \
#   --private-key $WALLET_KEY --broadcast --constructor-args <MORPHO_ADDR>

# сервис:
sudo cp liquidator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now liquidator-bot
systemd-cgtop               # убедиться, что не давит twidgest

# read-only анализ конкуренции (кого/что мы выигрываем/упускаем на наших рынках):
python3 -m analysis.competition_report 30      # за 30 дней, с реальными котировками KyberSwap
```

## Граница ответственности

Ключ кошелька живёт только на VPS в `.env` (не коммитится, в код не попадает, ассистенту не
передаётся — `cast` выводит лишь адрес). Деплой и владение ключом — на владельце; ассистент
готовит код и проверки. Горячий ключ держит минимум средств; профит можно свипать на
холодный адрес через `setOwner` (после первого профита). Подробности — `docs/START_HERE.md`.

## Структура

- `main.py` — петля: `fetch_candidates` (API) + `process_candidates` (on-chain, собирает
  ликвидируемых) + `_execute_actionable` (monitor→paper; execute→prepare+параллельный dispatch) +
  `block_driven_loop` (wss, горячий набор, дренаж, фон-поток: кандидаты/цена ETH/ротация рынков)
- `config.py`, `store.py` (SQLite), `alerts.py` — каркас
- `chain/` — RPC (`rpc.py`), чтение Morpho (`morpho.py`), Multicall3-батч (`multicall.py`),
  симуляция/HF-математика (`simulate.py`), исполнение (`execute.py`: kyber, encode_liquidate,
  simulate_tx, send_tx, `prepare_liquidation`, `try_liquidate`, `dispatch_liquidations`)
- `contracts/` — Foundry: `src/Liquidator.sol` + форк-тест `test/Liquidator.t.sol`
- `strategy/` — покрытие рынков (`scanner.py`), PnL/LIF-математика (`pnl.py`), kill switch (`guard.py`)
- `analysis/` — отбор рынков (`build_covered_markets.py`: borrow-полоса, bonus>1%, SVR/PT-фильтры),
  отчёт о конкуренции (`competition_report.py`: реальные котировки), модель, SQL-скан
- `docs/` — START_HERE / STATE / WORKFLOW / ARCHITECTURE
