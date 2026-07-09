# HANDOFF — живой операционный брифинг (обновлять при каждом значимом действии)

> Цель: любая модель/сессия продолжает работу БЕЗ контекста предыдущей. STATE.md = стратегия и
> история; этот файл = «что прямо сейчас, что дальше, чего не делать». Короткий, перезаписывается.

## Дата среза: 2026-07-09 ~02:50 UTC

## ГДЕ ЧТО
- **Прод (боевой бот): Charlotte NC `root@185.173.146.134`**, `/root/liquidator`, systemd
  `liquidator-bot` (mode=execute). TZ=UTC. Переехал с Вены 07-08 19:52.
- **Бэкап: Вена `root@138.124.90.144`** (= хост песочницы ассистента), `liquidator-bot`
  **DISABLED** — НЕ включать (два инстанса с одним кошельком = nonce-крах). Тут же лежит `origin`
  с ssh-кредами владельца: пуш в GitHub идёт ОТСЮДА (`git push origin main`).
- **Деплой на Charlotte:** git bundle + scp с песочницы (прямой `git pull /home/...` работает
  только на Вене — тот же хост). `git pull` НЕ рестартит сервис — нужен явный `systemctl restart`.
- **GitHub:** `git@github.com:kelbic/liquidator.git`. HEAD на 07-09: `357b73e`+ (docs поверх).
- Ассистент имеет root-ssh на ОБЕ машины (дан владельцем 07-08). Секреты (WALLET_KEY/RPC_URL/
  TG_BOT_TOKEN) в `.env` — их вписывал ВЛАДЕЛЕЦ, ассистент не трогает.

## ГИГИЕНА (закрытое)
- **TG-алертинг был МЁРТВ до 07-09 12:50** — `TG_ADMIN_ID` пуст в .env (и на Вене тоже, изначально).
  Токен @essayist_bot валиден, но без chat_id все `alerter.send()` молча возвращали False (HOT/
  kill-switch/сводка не доходили). Чинено: `TG_ADMIN_ID=265715923` прописан на ОБЕИХ машинах,
  тест-отправка ok, бот рестартнут. Живые алерты теперь работают.

## ЧТО В БОЮ СЕЙЧАС (флаги живого процесса — проверять ТОЛЬКО через
## `/proc/$(systemctl show -p MainPID --value liquidator-bot)/environ`, НЕ python -c)
- `HOT_PATH=1 PRICE_FROM_FEED=1 MODE=execute LOOP_MODE=block`, TIP_GWEI=6.0, floor $450.
- Латентность: P0 (путь ~2.5-3с→~0.9с) + переезд US-East (−~90мс/вызов) → read=~130мс в бою.
- Кап repaidShares (глубокие позиции), Slipstream-маршруты cbBTC/EURC + cbLTC, дедуп двойной
  отправки — ВСЁ включено безусловно.
- **Флаги ещё OFF:** `POSITIONS_FROM_CACHE`, `FAST_LANE` (нет в .env).
- Kill-switch: $60 loss / $100 gas в день. Последний рубеж — НЕ трогать.

## ЗАПЛАНИРОВАНО (переживает смену модели, НЕ переживает конец сессии)
- **Cron `b5b6f3df` на 2026-07-09 20:23 UTC:** гейт калибровки слоя 2 → при чистом (≥30 сравнений
  cache-divergence за сутки, mism=0, без tba_eq=False; ночью уже 150/0) включает ОБА флага
  `POSITIONS_FROM_CACHE=1`+`FAST_LANE=1` в .env Charlotte, restart, push владельцу. Предохранитель:
  серия 3+ liquidate revert после FAST_LANE → сам ставит FAST_LANE=0, restart, push.
  **Если сессия умерла до 20:23** — выполнить вручную: две grep-команды из STATE (P1-6 гейт), при
  чистом — дописать флаги в .env + restart + проверить `src=cache` в DIAG hotset.
- **Monitor `binrbao9t`:** ssh-tail журнала Charlotte, фильтр = только важное (HOT ready/ACTIONABLE,
  реверты, mism>0, tba_eq=False, cbLTC mismatch, крэш-луп, kill-switch).
- **Daily TG-сводка:** systemd timer на Charlotte, 08:00 UTC (независимо от сессии).

## СЛЕДУЮЩИЕ ШАГИ (по порядку)
1. Вечерний гейт (cron сам, см. выше).
2. Первое reaction-событие рынка → смотреть `submit-xray` дельту (хватает ли скорости).
3. **P2-10 standing-спам на ноже — СВЕЖЕЙ СЕССИЕЙ** (money-фича). Дизайн в STATE.md (раздел P2):
   кандидаты 1.0≤HF<1.002 на direct-маршрутах, STANDING_TIP_GWEI 0.02-0.1, fire-and-forget +
   фоновый receipt-коллектор, свой под-кап газа STANDING_MAX_GAS_USD. Единственный публичный путь
   в atomic-класс подтипа Б («мгновенный флип на ноже»).

## ЧЕГО НЕ ДЕЛАТЬ
- НЕ включать Вену (nonce). НЕ мерить gap (запрет STATE — артефакт малой выборки). НЕ трогать
  kill-switch. НЕ печатать/логировать значения секретов. НЕ включать FAST_LANE без работающего
  дедупа (сделан, 357b73e). Деплой в hot_loop — только после AST/symtable-проверок (урок: NameError
  молча убил слой 1). НЕ переоткрывать закрытое (UniV3 для cbLTC/EURC мёртв; seizedAssets-режим;
  atomic целиком — подтип А теперь достижим, но это measured-переоткрытие, не отмена запрета).

## ПРОВЕРКА ЗДОРОВЬЯ (одной командой)
`ssh root@185.173.146.134 'systemctl is-active liquidator-bot; journalctl -u liquidator-bot --since "-4 min" --no-pager -o cat | grep "hot stats" | tail -1'`
Норма: active; poll≈60, none=0, poll_fb=0.
