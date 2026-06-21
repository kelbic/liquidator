# liquidator

Бот ликвидаций на **Base** поверх **Morpho Blue**. Стратегия — длинный хвост:
изолированные permissionless-рынки, где конкуренция тоньше, бонус выше (LIF до 15%),
а встроенный flash loan Morpho даёт нулевой капитал и почти нулевой honeypot
(в контракте нет стоящих средств, кошелёк держит только газ).

Третий продукт Kelbic рядом с twidgest/essayist: отдельный репо, отдельный
systemd-сервис, своя SQLite, отдельный клон. Ресурсно изолирован через cgroup —
всплеск здесь не может задеть twidgest (см. `liquidator-bot.service`).

## Фазы

- **Фаза 1 — monitor / paper-trade (сейчас).** Следит за рынками, ловит HF<1,
  симулирует профит, **логирует, что сделал бы** — без отправки транзакций.
  Не латентно-критична, ложится на общий VPS под ресурсным капом, денег под риском ноль.
- **Фаза 2 — execute (позже).** Реальная отправка. Гейтится симуляцией-перед-отправкой,
  kill switch'ем и тестнетом-первым.

Сейчас Фаза 1 ещё не реализована: стабы `chain/*` бросают `NotImplementedError`,
`main.py` крутится вхолостую (можно поднять сервис и проверить капы до всякой логики).

## Быстрый старт

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить; chmod 600 .env
# установка сервиса:
sudo cp liquidator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now liquidator-bot
systemd-cgtop               # убедиться, что не давит twidgest
```

## Граница ответственности

Ключ кошелька живёт только на VPS в `.env` (не коммитится, в код не попадает,
ассистенту не передаётся). Деплой и владение ключом — на владельце; ассистент
готовит код и проверки. Подробности — `docs/START_HERE.md`.

## Структура

- `main.py`, `config.py`, `store.py`, `alerts.py` — каркас
- `chain/` — RPC, чтение Morpho, симуляция (профит ДО отправки)
- `strategy/` — покрытие рынков, PnL-математика, kill switch
- `analysis/` — модель (`liq_model.py`), мост Dune (`liq_measure.py`), SQL-скан
- `docs/` — START_HERE / STATE / WORKFLOW / ARCHITECTURE
