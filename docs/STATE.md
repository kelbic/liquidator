# STATE.md — живой срез состояния (liquidator)

> **ПЕРЕПИСАН НАЧИСТО 2026-07-08.** Убрана слоёная история разворотов сессии 07-08 (8 ошибок
> идентификации, все закрыты первоисточником). Сохранены все load-bearing факты, коммиты, запреты, уроки.
> Точный HEAD — всегда из `git log`, не из этого файла.

---

## 🛑🛑 GAP ИЗМЕРЕН ОКОНЧАТЕЛЬНО — НЕ ВОЗВРАЩАТЬСЯ К ИЗМЕРЕНИЮ GAP 🛑🛑
**Полное распределение (gap_profile 170 ликвидаций/30д) — РЕШАЮЩЕЕ:**
- **reaction (gap>2, ПУБЛИЧНЫЙ мемпул — ДОСТИЖИМО НАМ): 69–75%, ~$1.74M.**
- atomic (gap≤2, приватный bundle, prio 0.024 gwei — НЕдостижимо): 25–31%.

**АРТЕФАКТ-ЛОВУШКА (НЕ ПОВТОРЯТЬ): малая выборка к КРУПНЫМ призам** (5–30 событий, `replay_winners`,
`gap_verdict` на топ-призах) → медиана gap=1 → ЛОЖНОЕ «всё атомарно, сегмент пуст». Это ОШИБКА ВЫБОРКИ,
опровергнутая полным распределением. `replay_winners` на 6 событиях (07-08) = ровно эта ловушка,
в неё наступили и откатили.

**НАШ СЕГМЕНТ (reaction gap>2) СУЩЕСТВУЕТ И ДОМИНИРУЕТ. Архитектура под него валидна.**
Если снова видишь «все gap=1» на малой выборке — СТОП, это артефакт, читай эту строку. НЕ мерить gap.

Gap мерить ТОЛЬКО по tx-index внутри блока (`txgap`). Замеры «в каком блоке цена менялась» — мусор
(transmit всегда в блоке ликвидации).

---

## ⭐ ТЕКУЩАЯ ПРАВДА — самодостаточно, читать первой

**Бот ВООРУЖЁН** (`MODE=execute`, `LOOP_MODE=block`, `HOT_PATH=1`, `PRICE_FROM_FEED=1`), ARM 2026-06-23.
Прод-`.env`: `TIP_GWEI=6.0`, `HOT_MIN_REPAID_USD=450`, `MIN_PROFIT_USD=5`, `MAX_INFLIGHT=5`.
Kill-switch $60 loss / $100 gas / день — честный (`715d3f5`), доказан в бою. Контракт
`0x54730EE13f10C073772f25ee475EE13f2252Fb82` (`liquidate()` = onlyOwner). Кошелёк `0xE2Aca8e…`,
рантвей ~$44 (≈29 ревертов при 6 gwei = потолок риска). Соседи VPS (twidgest/essayist) не задеты.

**P0-ПАКЕТ СОБРАН (07-08, `a254462`, НЕ ЗАДЕПЛОЕН на VPS):** аудит с каноническим Morpho.liquidate
показал: сквозной горячий путь был ~2.5–3с ПОСЛЕДОВАТЕЛЬНЫХ RTT (8–9 обращений × ~343мс) — больше
блока Base, reaction-класс был недостижим НЕЗАВИСИМО от рынка. Тезис «победа ждёт рынка» ПЕРЕСМОТРЕН:
ждала кода. P0 срезает до ~0.9с: (а) feed-gate fix — px_hint теперь ЦЕНА цикла, gate не читает preconf
(раньше слой 1 давал ранний триггер, но НЕ раннюю цену: gate перезаписывал хинт, а при preconf-лаге
убивал feed-спавн при уже дедупнутом poll — триггер терялся на блок); (б) prepare_hot без rp_state-дубля
(state от read_positions), DIAG-read в тред, guard-read удалён (дублировал sim), gas per-market кэш;
(в) локальный nonce (0 RTT) + SEND-3 raw в секвенсер параллельно Alchemy (SEQUENCER_RPC, "off"=выкл);
(г) warm-start кандидатов с диска (снапшот 60с, age≤1ч) — слепые окна рестартов закрыты; (д) времена
рестартов в missed.py + daily_summary + liquidator-daily.timer; (е) inflight-гейт kill-switch ОЖИЛ
(был хардкод GuardState(inflight=0) в 4 местах). 23 юнит-смоука зелёные (venv-песочница).
**ДЕПЛОЙ (владелец):** git pull на VPS → restart → в journal ждать `warm-start:` строку; таймер:
`cp liquidator-daily.{service,timer} /etc/systemd/system/ && systemctl daemon-reload && systemctl
enable --now liquidator-daily.timer`. `.env` менять НЕ нужно (SEQUENCER_RPC дефолтом включён).
Watch: `DIAG seq-send` (ошибки секвенсера), `dispatch: sent .. nonce=` (nonce-кэш), hot stats.

**P0+P1 ЗАДЕПЛОЕНЫ НА VPS 07-08 18:41 (git pull 75ef31a→8dee451, рестарт чистый).** Post-deploy
по журналу: 0 ошибок; hot stats poll=60/none=0/fb=0; shadow-map теперь 4 рынка (+cbLTC
0x5bf848b4); position-cache markets=5 (+cbBTC/EURC, +cbLTC через ROUTE_COLLATERALS), dt=2.0-2.5с
(сустейн в норме, busy-skip единичный); **cache-divergence уже пишет mism=0 tba_eq=True** —
калибровка слоя 2 пошла. Ассистент имеет read-only журнал (systemd-journal) с этой сессии;
/root и systemctl — только владелец. Владельцу осталось: (1) установить daily-таймер;
(2) `ls -la /root/liquidator/candidates_snapshot.json` (файл должен обновляться ~60с) — строка
`warm-start:` подтвердится на СЛЕДУЮЩЕМ рестарте (на деплойном снапшота ещё не было).

**P1-ПАКЕТ СОБРАН (07-08, `6669137`; два флага OFF до калибровки):**
- **Слой 2 потребление — ГОТОВО за флагом `POSITIONS_FROM_CACHE` (OFF).** Гейт включения:
  `DIAG cache-divergence` (значения кэша vs живое чтение на том же транзите) должен показывать
  mism=0 на потоке ≥ суток → тогда флаг ON. Рынки вне кэша падают в живой read без изменений.
- **Кап repaidShares (глубокие позиции) — ВКЛЮЧЁН безусловно.** Раньше полный долг на глубокой
  позиции давал Panic 0x11 → skip (главные призы каскадов). Кап ДО сайзинга свопа (запрет «НЕ
  капаем» касался пост-хок капа); весь залог, остаток спишет Morpho (bad debt). seized теперь
  ТОЧНО по канону (toAssetsDown), не оценкой сверху. Fuzz 2000 состояний: 0 нарушений
  seized≤collateral. cast-форк-сверка НЕВОЗМОЖНА: в журнале (30д) НОЛЬ sim-revert panic-кейсов
  (согласуется с «Panic был лог-призраком») — реального кейса не существует. Страховка капа =
  прод-sim (остаётся гейтом каждого сенда): неверный кап → sim-revert → skip, деньги не рискуют.
- **Маршруты: cbBTC/EURC и cbLTC/USDC открыты через Aerodrome Slipstream** (см. «Закрыто» — UniV3
  для них МЁРТВ, это проверено). direct_swap: token_out — честный ключ (был хардкод USDC-выхода:
  EURC-рынок упал бы CannotRepay). Scope слоя 2 → ROUTE_COLLATERALS (5 коллатералей). Первый
  живой sim на Slipstream-маршруте — вотч-пойнт (bucket=swapfailed → смотреть путь/ликвидность).
- **Fast-lane — ГОТОВ за флагом `FAST_LANE` (OFF).** Без sim/estimate: fee из fb-хедера, gas из
  кэша, только direct-маршруты, minProfit=50% бонуса; исход гарантируют контракты, промах = газ
  реверта под kill-switch. **РЕШЕНИЕ ВЛАДЕЛЬЦА 07-08: включать ВМЕСТЕ с POSITIONS_FROM_CACHE по
  чистому гейту калибровки (mism=0 за сутки), НЕ дожидаясь живых выстрелов обычного пути.**
  Watch после включения: `HOT ready FAST` и серии liquidate revert (одиночные реверты = норма
  fast-lane, цена ~центы; серия = стоп и разбор).

**ПЕРЕЕХАЛИ: БОТ В БОЮ НА CHARLOTTE С 07-08 19:52 UTC.** Cutover чистый (Вена stop+DISABLE =
выключенный бэкап, НЕ включать — nonce; db с дневными тоталами и snapshot перенесены; warm-start
676 кандидатов). **Эффект немедленный: position-cache cycle 2.0–2.6с (Вена) → 0.6–0.8с** — сустейн-
вопрос слоя 2 снят, цикл < трети блока. Ошибок 0. Прод теперь `/root/liquidator` на
`185.173.146.134` (TZ=UTC, daily timer 08:00 UTC); деплой на него: владелец `git push` origin →
Charlotte `git pull origin main`, либо ассистент git-bundle+scp с сандбокса (сандбокс живёт на
Вене-хосте). Журнал Charlotte ассистент читает по ssh. Ниже — история подготовки:

**(история) ПЕРЕЕЗД В US-EAST ГОТОВ К CUTOVER (07-08):** новый VPS Charlotte NC `185.173.146.134`
(NetCrafters, root у ассистента). Замер: sequencer warm=30ms, preconf=38ms (Вена=121/133) —
**−90мс/вызов подтверждён на боевой машине.** Площадка собрана: репо (origin, HEAD 5958360+),
venv (py3.14/web3 7.16 == Вена), covered_markets/feed_to_market перенесены, systemd-юниты
установлены DISABLED, `.env` собран (несекретные строки с Вены; **WALLET_KEY/RPC_URL/TG_BOT_TOKEN
= плейсхолдеры `__FILL_ME__`**), конфиг+импорт валидированы (mode=execute hot=True tip=6.0 флаги
OFF), P0+P1 смоук зелёные (кроме устаревшего cbLTC=1e28-ассерта — фикс на месте).
**ОСТАЛОСЬ 2 ШАГА:** (1) ВЛАДЕЛЕЦ вписывает 3 секрета в `/root/liquidator/.env` на Charlotte;
(2) по «го» ассистент гонит `scratchpad/cutover.sh` — строгий порядок: Вена stop+disable →
финальные db+snapshot Вена→Charlotte → Charlotte enable+start → журнал-проверка. Вена =
выключенный бэкап (НЕ enable — два инстанса с одним кошельком = nonce-крах). Cutover в тихое
окно. Charlotte TZ=UTC (daily timer 08:00 UTC).

**P0-3 ПОДТВЕРЖДЁН В БОЮ (07-08 18:52, cbADA, прямое A/B в одном журнале):** feed-spawn с px_hint →
`noflip trig=feed price=0ms read=421ms` против соседнего `trig=poll price=381ms` — gate-RTT цены
РЕАЛЬНО снят; сквозной декод→HF(11 позиций)=422мс, из них 421мс = read_positions (снимет включение
слоя 2 → путь до prepare ~1мс). shadow match=True (px_feed==px_preconf), cache-divergence same=11
mism=0 на money-рынке.

**P2 (07-08, `3667e0a`): 11 и 12 сделаны, 10 — план готов, НЕ собран.**
- **P2-12 submit-side телеметрия — В КОДЕ:** каждый замайненный выстрел логирует tx_index
  (`submit-xray: WIN idx=` / при реверте — idx победителя по topic3=borrower узким getLogs и
  дельта позиций). Своих выстрелов касается, gap-запрет не задет.
- **P2-11 гео-замер — ЗАКРЫТ ЗАМЕРОМ (07-08, GH Actions us-east, 3 серии):** из Boydton/Virginia
  sequencer warm=18-26ms, preconf=24-28ms против Вены (Aeza) 121/133ms ⇒ **−95-110ms НА КАЖДЫЙ
  вызов, подтверждено.** Origin US-East. Комбинаторика с флагами: сегодня из Вены путь ~0.9-1.1с;
  из US-East тот же код ≈ −300мс; +POSITIONS_FROM_CACHE ≈ 0.15-0.25с; +FAST_LANE ≈ 50-100мс от
  декода до send-ack = СУБ-ФЛЭШБЛОК (200мс) класс. Крупнейший оставшийся рычаг после флагов.
  ⚠️ РЕПЛИКА С ТЕМ ЖЕ КОШЕЛЬКОМ НЕВОЗМОЖНА (nonce-конфликт двух инстансов) — либо ПОЛНЫЙ переезд
  бота на US-East VPS (Aeza остаётся выключенным бэкапом), либо второй кошелёк+контракт. Решение
  и выбор хостера — владелец. Замер повторяем: Actions → «RTT probe (us-east)» → Run workflow.
- **P2-10 live-fire спам на ноже — ДИЗАЙН (собирать СВЕЖЕЙ сессией, money-фича):** кандидаты
  1.0≤HF<1.002 на direct-маршрутах из hf_cache блок-лупа; каждый блок fast-lane-подобная tx
  (аналитический minProfit, БЕЗ sim) с ОТДЕЛЬНЫМ STANDING_TIP_GWEI≈0.02-0.1 (НЕ 6.0!);
  fire-and-forget + фоновый receipt-коллектор (иначе kill-switch слеп к газу ревертов — критично);
  реверт ~$0.02 ⇒ час висения ~1800 блоков ~$36 газа — дневной кап $100 держит ~3ч спама, нужен
  свой под-кап (STANDING_MAX_GAS_USD). Экономика: break-even win-rate 0.024-0.061% (STATE);
  единственный публичный путь в atomic-класс (VVV-тип «мгновенный флип на ноже»).

**ГДЕ МЫ (07-08):** Архитектура латентности **почти собрана, план ПОЛОН, нового рычага нет.**
Три части «архитектуры победителя» (ноль RPC в горячем пути):
1. **Декодер цены (слой 1) — ЕСТЬ, включён, РАБОТАЕТ.** Снимает RTT цены (~350мс).
2. **UniV3-direct своп (вариант A) — ЕСТЬ** (`2c2d6fe`). KyberSwap выкинут из гонки.
3. **Слой 2 (позиции в памяти) — ОСТАЛОСЬ ПОТРЕБЛЕНИЕ.** Ядро+драйвер в проде (измерит. фаза).
   Снимает RTT позиций (~350мс). Упирается в сустейн-вопрос (фоновая нагрузка на preconf).

**ОСТАНОВИЛИСЬ ПЕРЕД СБОРКОЙ СЛОЯ 2** (сознательно, конец тяжёлой сессии). Слой 2 трогает горячий
путь с деньгами — собирать свежей сессией: калибровка-первой, флаг + log-only фаза (кэш сравнить с
RPC на потоке ДО потребления), AST-тест на scope (scope-баги hot_loop юниты песочницы НЕ ловят —
см. NameError ниже).

**Первая ПОБЕДА ждёт РЫНКА, не кода** (STATE_old, урок 10 ходов): готовность ≈ есть, деньги ждут
reaction-события, где мы окажемся быстрее конкретного конкурента. Не точить латентность вслепую.

---

## ★ СЛОЙ 1 — СТАТУС (подтверждён рабочим на данных)

**Путь:** `debc80f` (собран за флагом) → **был МЁРТВ** (`spawn_kwargs` не в scope hot_loop → NameError
на каждом feed-спавне, глотался ws-error-хендлером как реконнект, `_process_transmit` не запускался)
→ `8278263` (NameError устранён, inline тех же kwargs, AST scope-тест) → `c871ae8` (trig=subidx в
noflip/hotset — триггер feed/poll теперь наблюдаем).

**ПОДТВЕРЖДЕНО ДАННЫМИ (суточный watch):** `23 trig=feed` в noflip → feed-triggered `_process_transmit`
ЗАВЕРШАЕТ HF-проверку в проде 23 раза ⟹ feed-путь доходит до конца (не мок, не «поток стартует»).
Опровергает «feed молча не завершается». **Дедуп-дыра `feed_handled` НЕ реализуется** (feed проверяет
помеченное, покрытие не теряется). **health_from КАЛИБРОВАНА** (=1.0261 на известной позиции
`0x26673097` @ px=17163200, расхождение 0.0001) — HF-выводы на верных адресах надёжны.

**Механика:** shadow-декод из flashblock (offset 676) при `cfg.price_from_feed` спавнит
`_process_transmit(subidx="feed", px_hint=...)`; `feed_handled` (oracle,bn) гасит polling-дубль (дедуп
ПО ФАКТУ обработки, не по цене — устойчив к preconf-лагу ~0.5с); px_hint через 2b `_gate_px` применяется
ТОЛЬКО к своему оракулу. Флаг OFF = поведение идентично. Флаг проверять ТОЛЬКО через
`/proc/$(systemctl show -p MainPID --value liquidator-bot)/environ` (ручной `python -c` не читает
EnvironmentFile → даёт False, НЕ показатель).

**ГРАНИЦА:** подтверждено до NOFLIP (оценка «ликвидируема ли»), НЕ до отправки — за окно не было флипа,
дошедшего до prepare_hot (`prep=0`, ликвидируемых на money-рынках нет). Путь `feed→prepare_hot→sim→
dispatch` за флипом не наблюдался. `prep>0 ≠ выигранная гонка` (sim+send остаются).

---

## `prep=0` — НЕ ОДИН БАРЬЕР (главный концептуальный вывод)

Разбор money-событий детально: `prep=0` вызывается РАЗНЫМИ механизмами. Постановка «найти THE barrier
+ одно лечение» НЕВЕРНА — объясняет, почему прошлые «нашёл причину» не держались. Классы промаха:

- **Ценовая латентность reaction** (06-30 cbDOGE — money-событие): цена сползает тик-за-тиком,
  победитель берёт по свежей цене раньше нашего чтения, НО с реакционным запасом (gap>2). **06-30
  замкнут:** обе хот-цели HF@N-1≤1.10 (были бы в наборе, взяты раньше). **Слой 1 релевантен ЭТОМУ классу.**
- **07-08 cbADA — АТОМАРНЫЙ, gap=1 (identity закреплена с чейна, каждое звено первоисточником):**
  Оракул жертвы `0x35D87a74` (Morpho idToMarketParams cbADA-mid), его底层 aggregator `0x9491aEdF`
  (ДОКАЗАНО: latestRoundData底层 @384=17285343, @385=17163200 — точно совпало с price() обёртки, значит
  обёртка читает этот底层 динамически). roundId底层 3359→3360 между 384/385 (updatedAt=ts блока 385,
  сверено get_block) → цена упала В блоке 385. AnswerUpdated底层 на позиции 83 (ОДНО чтение узкого
  getLogs: answer=topic1=17163200 упавшая, roundId=topic2=3360, tx-idx=83 — НЕ сшивка), winner-liquidate
  на 84 → **gap=84−83=1 от ПРАВИЛЬНОГО источника, каждое звено первоисточником.** Жертва здорова @384
  (HF=1.0006), ликвидируема @385 (HF=0.9935, калибр. health_from), взята следующей tx за底层-update →
  **bundle-класс (prio 0.024 gwei, ЗАКРЫТ), слой 1 бы НЕ успел** (детекцию решили — update@83 увидели
  бы; взятие@84 требует bundle-механики).
  ⚠️ **GAP-ЗАПРЕТ СОБЛЮДЁН: ОДНО событие из атомарных ~25-31%, НЕ обобщать** — 69-75% reaction достижимо,
  флип 07-08 из недоступной трети, наш сегмент не затронут. Жертва реальна (topic3), была в наборе (n=8).
  ПУТЬ (8 идентификац. ошибок, все закрыты первоисточником): не тот mid (5 ходов) → фантом на
  сломанном запросе → gap против чужого события@83 → identity-first доказал 0x9491aEdF=底层 жертвы →
  gap из одного getLogs. Маятник «атомарный постулат»→«всё под вопросом»→«атомарный, каждое звено чейном».
- **Мгновенный флип на ноже** (VVV $185k, cbBTC/EURC $6k, 07-05): позиция висит HF≈1.000x долго,
  берётся АТОМАРНО на копеечном тике — нет обвального транзита, **слой 1 НЕ берёт по построению**.
  Берётся только bundle-каналом (ЗАКРЫТ) или упреждением (невозможно). VVV УЖЕ в covered set —
  промах структурный, не покрытия.
- **Submit-side** (cbBTC 01-14): дошли до отправки, проиграли включение (~2.5 блока). n=1.

Следствие: слой 1 нужен (ценовая латентность reaction), но лечит ОДИН класс; доля класса не измерена.
Из разобранных money-событий: 06-30 ценовая reaction (слой 1 берёт), 07-08 атомарная (недоступна) —
РАЗНЫЕ классы. Слой 1 бьёт в reaction, не в атомарный.

---

## 🧭 АРХИТЕКТУРА КАК ЕСТЬ

**Детекция:** flashblocks (~2с раньше newHeads, 46/46) → money идёт через ПОЛЛ (bare-match слеп:
forward-обёртка прячет агрегатор; shadow-декодер видит money через FORWARD_SEL). Дедуп: poll_seen
(oracle→preconf-цена), feed_handled (oracle,bn — слой 1), last_price[agg] (bare-match).

**Два preconf-RTT в горячем пути** (~350мс каждый): цена (→слой 1, снят) и позиции (→слой 2, осталось).
Живой `read=` медиана ~343мс (не масштабируется с n: n=1→375, n=64→424 — это RTT, не размер).
Confirmed Alchemy отстаёт ~1.8с на pending (потому noflip рвался). Preconf лидирует, LEADS+CORRECT.

**Воронка:** flashblock-транзит → gate (цена оракула) → read_positions (preconf-pending) → _flips →
prepare_hot (UniV3-direct exactInput, `amountOutMin=repaid_assets`) → sim (preconf) → dispatch.

**Оракулы:** 7 из 40 covered-рынков на non-Chainlink OCR — структурно вне bare-match. Money-рынки
cbXRP/cbDOGE/cbADA — Chainlink, декодируемы (offset 676). Morpho-oracle = обёртка (напр. cbADA
`0x35D87a74`), читает底层 Chainlink aggregator (`0x9491aEdF`) динамически при price() — БЕЗ
собственного AnswerUpdated (AnswerUpdated эмитит底层 aggregator, см. урок 30).

---

## 🎯 ИДЕЯ 2 — убрать preconf-RTT из горячего пути (план ПОЛОН)

Три слоя = архитектура победителя (ноль RPC в момент флипа):
1. **Цена из фида (слой 1)** — ЕСТЬ, работает. 0.7мс декод vs 350мс RTT.
2. **Позиции в памяти (слой 2)** — ядро+драйвер в проде, ОСТАЛОСЬ ПОТРЕБЛЕНИЕ (переключить горячий
   путь на кэш вместо RPC, 350мс→0). Сустейн-вопрос: фоновая нагрузка на preconf → приоритизация
   refresh (близкие-к-флипу первыми). Собирать: калибровка кэш==RPC на известной позиции ДО
   интеграции; флаг + log-only divergence (кэш vs RPC на потоке); AST-scope-тест; потом потребление.
3. **UniV3-direct своп** — ЕСТЬ (`2c2d6fe`), Kyber в гонке заменён.

**Transmit-декодер (точная форма реального transmit'а через корреляцию updatedAt с flashblock-tx)** —
точнее по латентности, но ХРУПКО, требует пробинга. Price-change-детектор (слой 1) робастнее.
ГЕЙТ: строить transmit-матчинг только если живой win-rate покажет нехватку скорости. Не вслепую.

**IN-MEMORY ФЛИП = СВЯЗКА, не одна фича:** удаляет оба RTT (цена+позиции), считает HF в памяти на
oracle-update. Все три части нужны вместе для «ноль RPC».

---

## ⛔ ЗАКРЫТО ОКОНЧАТЕЛЬНО — не переоткрывать, не перемерять

- **seizedAssets-режим — мёртв 3×** (контракт принимает ТОЛЬКО repaidShares; collateral-underflow от
  устаревшего снимка; «Panic 0x11» был log-призраком от `log` NameError, фикс `e1eaa37`, после — 0 Panic).
- **Вариант A как локальная Kyber-calldata — НЕВОЗМОЖЕН** (`routeID`/`extra.ri` серверный ASCII).
  Решён иначе: UniV3-direct (`2c2d6fe`). Keep-alive к Kyber не существует (латентность серверная).
- **On-chain quote одним Quoter'ом — отклонён** (ликвидность фрагментирована: cbDOGE $50k = 24 хопа/9 DEX).
- **Atomic/bundle сегмент (gap≤2) закрыт** (приватный канал к секвенсеру, prio 0.024 gwei). Atomic ≠
  оператор оракула (transmit и liquidate от разных EOA; барьер = bundle-канал). Флип 07-08 — этого класса
  (gap=1, доказано).
- **Упреждающие ликвидации ДО флипа невозможны** (Morpho ревертит при HF≥1). Что спасается = Идея 2
  (слать в блок флипа первым). Юнит-экономика упреждения: break-even win-rate 0.024–0.061%, реальный
  win-rate попадания в блок неизвестен — только живой огонь.
- **«Верх заперт» — ОПРОВЕРГНУТО** (contestation: разовых% растёт с размером до 81%; $94k cbXRP, мы 0%
  из-за латентности, не канала).
- **cbBTC/USDC ($69M/30д) — дыра покрытия реальна, но НЕ джекпот** (MID $10–100k: 58 ботов плотнее cbXRP).
- **Покрытие расширять не на что** (7 слепых рынков 0 ликв/30д, composite закрыт поллингом, FUSD-оракул None).
- **«Мгновенный флип на ноже» структурно НЕ наш** (VVV/cbBTC 07-05: HF висит на ноже, атомарное взятие).
- **UniV3-маршруты для cbLTC и EURC — МЕРТВЫ, не переоткрывать** (проверено живым RPC 07-08:
  cbLTC/WETH 3000 и WETH/EURC 500 существуют с liquidity=0; cbLTC/USDC только 10000 с liq=0;
  EURC/USDC 100 liq=0; Aerodrome basic (vAMM) тоже пуст — $6.1k cbBTC давал выход $61). Глубина —
  ТОЛЬКО Aerodrome Slipstream: cbBTC/EURC ts=100, cbLTC/WETH ts=200, WETH/USDC ts=100, USDC/EURC
  ts=1 (~$10M). Роутер `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5`, exactInput v1-ФОРМА С deadline
  (селектор `c04b8d59`, НЕ b858183f), factory=CL `0x5e7BB104…` — сверено bytecode.
- **Silo-трек закрыт** (TVL/объёмы малы; `/root/silo/`, SILO_STATE.md).
- **Win-rate ретроспективно невычислим** — только живой огонь.

---

## 📋 ОТКРЫТЫЕ ВОПРОСЫ / ГЕЙТЫ / БЭКЛОГ

**Ждут события/каскада — код не ускорит:**
- Первая отправка feed-пути (флип HF<1 на money-рынке, REACTION-класс → prep>0 → sim → dispatch).
  Watch: `HOT ready subidx=feed`. На нём — попали ли в блок (submit-side) или проиграли. (Атомарные
  флипы вроде 07-08 слой 1 не возьмёт — ждём reaction.)
- Живой win-rate → гейт для transmit-декодера и для оценки, хватает ли слоя 1+2.

**Готово к сборке, не срочно:**
- **Слой 2 потребление** (последняя часть плана — см. Идея 2). Свежей сессией, не в конце дня.

**Гигиена / watch-items:**
- **refresh-дедлайн ЗАДЕПЛОЕН** `008970d` (cycle_deadline=30с, partial-return, healthy-path не тронут,
  3 юнита). Закрыл 30-мин слепоту hot-set (было 5 зависаний >60с/3д, макс 1767с). Watch: зависаний
  >30с быть не должно.
- Logrotate для hotset.log (open-append-close, rename-safe).

**Money-safety:** kill-switch проверять перед execute-деплоем; guard свежести долга (repaidShares≤
свежий долг) спроектирован.

---

## 🧪 УРОКИ — операционные правила (нарушение = повтор ошибки)

**Идентификация (главный риск, стоил сессии 07-08 ВОСЬМИ ошибок подряд):**
- **28. Borrower — ВСЕГДА из Liquidate topic3 ПЕРЕД разбором жертвы.** Первый скрипт может извлечь
  неверный адрес; missed.py-символ рынка может быть неверен. 8 ходов разбирал жертву на неверном mid,
  потом «фантом» на сломанном запросе. Проверять topic3 + marketId из события ПЕРВЫМ.
- **29. Проверять ВАЛИДНОСТЬ запроса, не принимать пустоту за факт.** Сломанный GraphQL (`$f never
  used` → 0 результатов) дал ложный «жертва не ликвидирована» → разворот. Пустой вывод = сначала
  проверить запрос/фильтр, потом выводы.
- **30. Identity底层 aggregator с чейна ДО вывода gap + правильный декод события.** gap=1 сначала
  посчитал против ЧУЖОГО события (единственный AnswerUpdated в блоке ≠ оракул жертвы). Закрыто:
  idToMarketParams→oracle обёртка → latestRoundData底层 совпадает с price() обёртки → это底层 жертвы →
  gap от ЕГО обновления. Финальное звено gap=84−83 закрыто ОДНИМ узким getLogs (fromBlock==toBlock,
  обходит range-limit 400): answer=topic1=17163200, roundId=topic2=3360, tx-idx=83 — не сшивка.
  ⚠️ **AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt): answer=topic1,
  НЕ data[0]!** current+roundId ИНДЕКСИРОВАНЫ; декод data[0] как answer даёт updatedAt (timestamp) —
  тихая ошибка, случайно совпала с ts блока (иллюзия сверки). `price()` = чтение состояния (СИЛЬНЕЕ
  события); отсутствие AnswerUpdated ОБЁРТКИ ≠ цена не менялась (обёртка читает底层 динамически).
  Маятник overreach: «атомарный постулат» → «всё под вопросом» → «атомарный, каждое звено первоисточником».
- **27. Грепать по ВСЕМ mid рынка** (один заёмщик держит позиции на ≥3 mid; 0x26673097 в 3 рынках).
  Отсутствие в отфильтрованном ≠ отсутствие в дампе.
- **16. Грубая HF без shares→assets (tba/tbs) — неверна**; доверять health_from (калибрована).
- **17. HF блока-0 читать нельзя** (погашенная позиция → HF=миллиарды).

**Дисциплина вывода (overreach обоих знаков + underreach за сессию):**
- **Вывод НЕ впереди проверки, которую сам назвал нужной.** Знак цены («фид опередил») — арифметически
  проверять ДО вывода. Overreach вверх («слой 1 обоснован») и вниз («не берёт 3/4») оба на неполных
  данных. Калибровку HF откладывал 3 хода — некалиброванное число нельзя звать фактом.
- **ЧИТАТЬ собственный вывод перед выводом «предел».** Watch дал `23 trig=feed` (разрешающее число) —
  объявил «останавливаюсь», не прочитав. Underreach: сдаться до чтения результата.
- **Первоисточник заканчивает серию ошибок.** 8 идентификац. ошибок были на КОСВЕННЫХ привязках (mid
  из логов, адрес из первого скрипта, «единственное событие в блоке», сшивка roundId+позиция). Переход
  на первоисточник (idToMarketParams, latestRoundData, topic3, узкий getLogs, один-источник-на-звено)
  серию ЗАКОНЧИЛ. Не «форензик не даётся» — форензик на прокси не даётся; на первоисточнике даётся.
- **8. Знак расхождения — из замера, не интуиции.**
- **11. Срез/выборка определяют вывод** (топ-призы → «закрыто»; полное распределение → «открыто»).
  Прямо связано с GAP-запретом выше.
- **26. Утверждение, пересмотренное дважды — grep кода/чейна ДО третьего вывода.** N-й пересказ без
  верификации = стоп-и-читай.

**Читать бэклог (нарушено 3× за сессию):**
- Перед предложением рычага — grep STATE/STATE_old. Дважды предложено решённое (слой 2 как «новое»;
  build-срез Kyber, уже устранён вариантом A). Раз нарушен записанный запрет (gap).

**Деплой:**
- **13/25. Деплой-блок и data-gate — РАЗНЫМИ сообщениями** (гейт исполняется вторым, проскакивает —
  SEND-2 ушёл на 7ч вместо 24ч). Отложенный деплой = «прогони X, пришли вывод» → деплой ПОСЛЕ X.
- **Scope-баги hot_loop юниты песочницы НЕ ловят** (NameError убил слой 1 молча). AST use-before-def
  ОБЯЗАТЕЛЕН перед деплоем в hot_loop. `python3 -c "import chain.hotpath"` ловит forward-ref в
  default-параметрах, что py_compile пропускает.
- Шаг-0 `cat -A` прода; якоря `assert count==1`; COMPILE/RUNTIME/AST/юниты; commit+push СРАЗУ после
  патчера; `git log --oneline -1` == origin ДО обновления STATE; `.env` перебивает dataclass (менять оба).
- **19. Датчики ставить в тихий период, ДО события** (штиль = окно на подготовку).
- **4. Always-on захват ground truth, не event-gated** (каскад невоспроизводим). Закрыто hotset-дампом.

**Прочее:**
- **22а. Receipt с несколькими Liquidate — матчить по repaidAssets** (победители батчат 2/tx).
- **15. Kyber через прод-функции** (403=нет x-client-id; 400=объём>ликвидности).
- **getLogs широкий диапазон → 400 Bad Request** (Alchemy лимит). Узкий `fromBlock==toBlock` обходит.
  latestRoundData/roundId точечно даёт тайминг обновления без getLogs (roundId сменился между блоками
  = обновление в этом окне).
- **STATE-гигиена: полный новый файл, не diff-патчи; убирать слоёную историю, сохранять load-bearing.**

---

## 🔩 ПРОВЕРЕННЫЕ ФАКТЫ (адреса / селекторы / форматы / среда)

**Market IDs (подтверждены чейном):**
- cbADA/USDC = `0xd7520ad198b497b6eb75bc690268f4597630dbc12e305e9d4105843bab36e41d`
  (collat `0xcbADA732173e39521CDBE8bf59a6Dc85A9fc7b8c`, oracle-обёртка `0x35D87a743D1F2f7CaFb42D855dC1c5Df857Ce45f`,
  底层 aggregator `0x9491aEdFe0Da70Eb110582B66B2Edace28D82a5d`, lltv 0.625)
- **cbLTC/USDC = `0x9125d0fa03c31371...`** (collat `0xcb17C9Db87B595717C857a08468793f5bAb6445F`,
  oracle `0x47f961E6653423A77b1EF8Fb680eE53bf7d8a00d`, lltv 0.625, loan USDC) — ОТДЕЛЬНЫЙ рынок,
  один заёмщик может быть на cbADA И cbLTC (источник mid-путаницы 07-08).
- cbDOGE/USDC = `0x73527ddd`; cbBTC/EURC = `0x67ebd84b` (lltv 0.86); VVV/FXUSD = `0x87c7d652`
  (оракул `0xa781…88FA` `price()` Morpho-style, НЕ Chainlink); cbXRP ≈ `0xd4a903dc` (не подтверждён).
- USDC loan = `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.

**Контракты:** Morpho `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb`; Multicall3 `0xcA11…CA11`;
Liquidator `0x54730EE13f10C073772f25ee475EE13f2252Fb82`; кошелёк `0xE2Aca8e…`;
UniV3 SwapRouter02 (Base) в execute.py; KyberSwap router `0x6131B5fae19EA4f9D964eAc0408E4408b66337b5`.

**RPC:** preconf `https://mainnet-preconf.base.org` (исполняет полный liquidate при block='pending',
лидирует ~1.8с). Alchemy `eth_call(pending)` И estimate_gas STALE (см. SEND-1). Alchemy free
`eth_getLogs` ≤10 блоков + широкий диапазон → 400 (узкий fromBlock==toBlock обходит). Flashblocks
`wss://mainnet.flashblocks.base.org/ws` (brotli-JSON, index=0=новый блок с baseFeePerGas, tx в
diff/transactions сырой RLP). Транзиты: sel `0x6fadcf72`, агрегатор в calldata, 1188-байт OCR (offset 676).

**ABI:** position `0x93c52062`→(uint256,uint128,uint128)[bShares,collateral]; market `0x5c60e39a`→
(uint128×6)[tba=2,tbs=3]; idToMarketParams `0x2c3c9157`→(addr×4,uint256)[loan=0,collat=1,oracle=2,
lltv=4]; oracle price `0xa035b1fe`; Chainlink latestRoundData `0xfeaf968c`→(roundId,answer,startedAt,
updatedAt,answeredInRound). ORACLE_PRICE_SCALE=1e36; цена cbADA/cbLTC ×1e28 в логах;底层 answer ×1e8.
Liquidate SIG=keccak("Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"),
topic3=borrower (неоспоримый источник жертвы), data[0:32]=repaidAssets, data[32:64]=repaidShares.
health_from (chain/simulate.py): borrowed=to_assets_up(bs,tba,tbs); mb=max_borrow(coll,px,lltv);
liq=(mb<borrowed); HF=mb/borrowed. КАЛИБРОВАНА (=1.0261 на 0x26673097).
**AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt): answer=topic1,
roundId=topic2, updatedAt=data[0:32] (НЕ data[0]=answer! current+roundId ИНДЕКСИРОВАНЫ — декод data
как answer даёт updatedAt/timestamp, тихая ошибка). Эмитит底层 aggregator, НЕ Morpho-обёртка.**

**Morpho API** `https://api.morpho.org/graphql`: enum `Liquidation` инлайн без кавычек; tx-поле
`txHash`; Market БЕЗ `uniqueKey`/`borrowShares`; MarketPosition БЕЗ `borrowShares`; repaidAssets без
usd; фильтр по ликвидатору клиентом; marketTransactions НЕ фильтрует по борроверу (брать широко +
topic3). positions_at_risk фильтрует API-HF≤1.10 (API-HF систематически ВЫШЕ on-chain). ⚠️ переменные
GraphQL должны ИСПОЛЬЗОВАТЬСЯ (неиспользуемая `$f` → весь запрос отвергнут → 0 результатов).

**KyberSwap:** keyless + браузерный UA; ~415–850мс серверные; build 55–79% латентности; PT не
маршрутизирует. (В гонке НЕ используется — вариант A UniV3.)

**Среда:** песочница ассистента ≠ VPS (нет Base/Morpho/Kyber) — пробники гоняет владелец:
`cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) /root/liquidator/venv/bin/python /tmp/X.py`.
Флаги через systemd EnvironmentFile=/root/liquidator/.env; config читает os.environ; флаг процесса
ТОЛЬКО через `/proc/$(systemctl show -p MainPID --value liquidator-bot)/environ`.
Foundry v1.7.1 на VPS (`cast run <tx> --rpc-url <alchemy> --quick` — форк исторических блоков работает).
Клон dry-run: `/root/_clone_liq` (никогда MODE=execute с реальным ключом).
Market dataclass: `collateral_token` = СИМВОЛ-строка (не адрес!).

**Форк/ретро-инструменты (read-only, БЕЗ ожидания событий):**
- `analysis/replay_winners.py` (HOURS/MAX_WINNERS/MIN_REPAID_USD env) — выигранные ликвидации: был ли
  oracle-update до winner (детекция) + gap update→winner (латентность). ⚠️ малая выборка → gap=1
  артефакт (см. GAP-запрет). Детекция: 5/6 update-before-winner (100% Chainlink) = детекция решена.
- `cast run <tx>` на VPS — форк блока, трейс.
- Прямое чтение блока (`get_block full_transactions`, receipt transactionIndex, latestRoundData
  roundId, узкий getLogs) — gap/тайминг без широкого getLogs. identity底层 = latestRoundData совпадает
  с price() обёртки; событие-цена = answer из topic1 (индексирован).

---

## ✅ ВЕХИ (сжатый; полная цепочка — `git log`)

`87e27be` SEND-1 (gas из preconf, мина send-пути снята) → `2c67002` hotset ground-truth+src+ring →
`3d01117` re-entrancy guard refresh → `8022294` shadow-декодер слоя 1 (log-only) → `7ffa476` baseFee-
замер → `9b07737` SEND-2 (fee из fb-хедера, 0 Alchemy в send) → `f8ed3ea` 2b (переиспользование
gate-цены) → `2c2d6fe` UniV3-direct вариант A (Kyber выкинут) → `008970d` refresh-дедлайн (гигиена) →
`debc80f` слой 1 за флагом → `8278263` NameError-фикс (слой 1 был мёртв) → `c871ae8` trig=subidx →
`a254462` P0-пакет (feed-gate fix: слой 1 теперь реально снимает ценовой RTT; prepare_hot −3 RTT;
nonce-кэш; SEND-3 секвенсер; warm-start; inflight-гейт ожил; daily-сводка).
Плюс: `PRICE_FROM_FEED=1` включён (подтверждён /proc/PID/environ). health_from калибрована.

---

## 🖥 ОПЕРАЦИОНКА

- **Мониторинг:** `python -m analysis.missed` (⚠️ символ рынка может врать — сверять marketId).
  `replay_winners` (ретро детекции/латентности, БЕЗ ожидания). Watch слоя 1: `journalctl | grep trig=`.
- **Ежедневно:** блок [1] ликвидаций читать (06-30 две хот-цели пролежали непрочитанными 1.4 дня =
  ~$444 упущено). refresh-зависания >30с (не должно после `008970d`).
- **Откаты:** `.bak-*` на VPS / `git revert <hash>`.
- **Если нода ребутнётся:** `systemctl is-enabled liquidator-bot` (AEZA-инцидент 4ч изоляции пережит).
- Workflow-доки: `CLAUDE.md`, `WORKFLOW.md`, `STATE.md` (этот файл — заливается вручную).
