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

- **Фаза 1 — monitor.** ✅ Живёт на 40 хвостовых рынках. Перечисляет заёмщиков через
  Morpho API, подтверждает HF on-chain одним батчем Multicall3 (~3-4 eth_call/цикл
  независимо от числа рынков), симулирует профит, пишет в SQLite. Денег под риском ноль.
- **Фаза 2 — execute.** ✅ Собрана и в проде, вооружается флагом `MODE=execute`. Контракт
  `Liquidator` задеплоен на Base, форк-тестирован на реальном Morpho. Путь на одну позицию:
  свежие on-chain чтения → размер свопа → KyberSwap-агрегатор → **`simulate_tx`-гейт
  (eth_call)** → подпись+отправка EIP-1559 с floor `minProfit` (реверт, если исполнение даст
  <95% симулированного). Капиталу ничего не грозит: гейтится симуляцией-перед-отправкой и
  kill switch'ем (дневные лимиты потерь/газа, inflight); худший случай — реверт за центы газа.

Режим переключается переменной `MODE` в `.env` (`monitor` | `execute`).

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
cp .env.example .env        # RPC_URL, WALLET_KEY, LIQUIDATOR_ADDRESS, MODE; chmod 600 .env
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
```

## Граница ответственности

Ключ кошелька живёт только на VPS в `.env` (не коммитится, в код не попадает, ассистенту не
передаётся — `cast` выводит лишь адрес). Деплой и владение ключом — на владельце; ассистент
готовит код и проверки. Горячий ключ держит минимум средств; профит можно свипать на
холодный адрес через `setOwner`. Подробности — `docs/START_HERE.md`.

## Структура

- `main.py`, `config.py`, `store.py`, `alerts.py` — каркас + петля скана
- `chain/` — RPC (`rpc.py`), чтение Morpho (`morpho.py`), Multicall3-батч (`multicall.py`),
  симуляция/HF-математика (`simulate.py`), исполнение (`execute.py`: kyber, encode_liquidate,
  simulate_tx, send_tx, try_liquidate)
- `contracts/` — Foundry: `src/Liquidator.sol` + форк-тест `test/Liquidator.t.sol`
- `strategy/` — покрытие рынков, PnL/LIF-математика, kill switch
- `analysis/` — отбор рынков (`build_covered_markets.py`), модель, мост Dune, SQL-скан
- `docs/` — START_HERE / STATE / WORKFLOW / ARCHITECTURE
