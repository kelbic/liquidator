# WORKFLOW.md — рабочий контракт (liquidator)

Тот же контур, что держит twidgest/essayist в проде без откатов, плюс слой денежной
безопасности (этот бот, в отличие от контентных, может потерять деньги). Не упрощать.

---

## Формат одного шага

Один логический шаг = одно сообщение:
1. `.bak`-бэкап затрагиваемых файлов.
2. Patcher-heredoc с `assert t.count(old) == 1` на каждый якорь (падает громко).
3. Новые файлы целиком — **в том же сообщении**.
4. Блок проверок: компиляция + рантайм-импорт + AST-проверка, с явным «ожидаю: …».
5. Коммит + push + рестарт сервиса + grep на ошибки.

Владелец выполняет всё разом и присылает вывод. По выводу — решение.

---

## Клон и синхронизация

Клон: `/root/_clone_liq` (стабильнее, чем `/tmp` — он вычищается).
```bash
cd /root/_clone_liq && git checkout -q . && git clean -qfd 2>/dev/null; git pull -q && git log --oneline -5
```
Если клон исчез — `cd /root/liquidator`, затем `git clone https://github.com/kelbic/liquidator /root/_clone_liq`.

---

## Env-заглушки для рантайм-импорта

`config.py` импорт-безопасен (читает env лениво через `Config.from_env`), `chain/*`
импортируют web3 ЛЕНИВО — поэтому базовый импорт чист без заглушек:
```bash
python3 -c "import main; print('RUNTIME OK')"
```
Под реальный прогон (не импорт) минимальный env:
```bash
RPC_URL=x WALLET_ADDRESS=0x0 MODE=monitor TG_BOT_TOKEN= TG_ADMIN_ID=0 \
python3 -c "import config; config.Config.from_env(); print('CONFIG OK')"
```

---

## AST-проверка — обязательна при правке имён/атрибутов

`py_compile` и `import` НЕ ловят: имя без импорта внутри функции; обращение к
несуществующему атрибуту; локальный `from..import X`, затеняющий модульное имя X
(код выше импорта падает с `UnboundLocalError`). Шаблоны — те же, что в твоих
twidgest/essayist `WORKFLOW.md` (сбор ImportFrom/Attribute из AST и сверка с фактом).

---

## Миграции SQLite

Перед `ALTER TABLE` — бэкап базы с датой:
```bash
cp liquidator.db "pre-<что>-$(date +%Y%m%d-%H%M).db"
sqlite3 liquidator.db "ALTER TABLE ...;"
```
Колонку — И в БД, И в `SCHEMA` (`store.py`).

---

## Слой денежной безопасности (новое vs контентные боты)

- **Тестнет первым.** Логика отправки — на Base Sepolia (`CHAIN_ID=84532`) до mainnet.
- **Симуляция-перед-отправкой = on-chain dry-run.** Каждая реальная ликвидация
  гейтится успешной `eth_call`/форк-симуляцией с net ≥ `MIN_PROFIT_USD`. Реверт или
  минус → НЕ отправляем. Это твой assert-перед-прод, но для денег.
- **Kill switch.** `MAX_DAILY_LOSS_USD` / `MAX_DAILY_GAS_USD` / `MAX_INFLIGHT`; пробой
  → бот встаёт + Telegram-алерт (антифлуд). В monitor-режиме гварды тоже считаются —
  логируем, когда «встали бы».
- **Ключ.** `WALLET_KEY` только в `.env` (chmod 600), не коммитим, ассистенту не
  передаём. Средства — только газ.
- **Ресурс-чек.** После деплоя: `systemd-cgtop` + `systemctl show liquidator-bot
  -p CPUWeight,MemoryMax,CPUQuota` — капы применились, twidgest не задет.

---

## Деплой и контроль

```bash
git add <files>
git commit -m "<императив: что и зачем>"
git push
systemctl restart liquidator-bot && sleep 8 && systemctl is-active liquidator-bot
journalctl -u liquidator-bot --since "-2 min" --no-pager | grep -cE "ERROR|Traceback"
systemd-cgtop -b -n1 | grep -E "liquidator-bot|twidgest-bot"   # ресурсы под контролем
rm -f <bak-файлы>
```
Здоровый финал: `active` + `0` ошибок + ликвидатор не давит twidgest. `activating`
(не `active`) — crash-loop, читать `journalctl`, форвард-фикс или откат из `.bak`.

---

## Если что-то упало в проде

1. `journalctl -u liquidator-bot --since "<окно>" --no-pager | sed -n '/Traceback/,/INFO/p'`.
2. Воспроизвести на клоне, починить, dry-run, деплой форвард-фиксом.
3. Откат из `.bak` — крайняя мера.

Помни про часовой сдвиг VPS относительно времени клиента при выборе окна `--since`
(время в Telegram — клиентское, в `journalctl` — серверное).
