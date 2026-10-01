# Теневой прогон B+6 — журнал

Протокол: [`SHADOW-B6-PROTOCOL.md`](SHADOW-B6-PROTOCOL.md) (записан 18.09 ДО первого тика).
Окно: 18.09 → 29.09.2026. Капитал 0, отправленных транзакций 0.
В Telegram не слать (владелец: «пока ждём»).

---

## Гейт запуска: оффлайн-реплей 15.09 (сделан 18.09 ДО живого прогона)

Прогон: `shadow_b6/replay.py detect` + `control 60` на Charlotte, архивные `eth_call`
(drpc/tenderly), вселенная заёмщиков **не зависит от исхода** (снимок позиций боевого бота
на 31.08 + логи `Borrow`/`SupplyCollateral` 31.08→15.09 00:00Z, события самого 15.09
во вселенную не входят).

| проверка | результат | норма протокола |
|---|---|---|
| **M1 полнота, по числу** | **70 из 81** (уникальные рынок+заёмщик+блок) = **86.4 %** | ≥ 90 % |
| **M1 полнота, по деньгам** | **$370 123 из $377 349 = 98.09 %** | — |
| пропуск **математики** | **0** | 0 |
| пропуск **вселенной** | 11 (13.6 %) — все промахи только этого класса | назвать отдельно |
| события > $1 000 | названо **30 из 31** | — |
| модель цены (позитивный контроль) | 17 сверок с фактическим `oracle.price()` на B_rel, **max ошибка = 0** | совпадение |
| ложные выстрелы в призовых блоках | 7 названных не ликвидированы, **все с бонусом < $3**; при пороге $20 — **0** | — |
| **контрольный набор** (98 оценок «раунд×рынок» 15.09, где ликвидаций в B+6 не было) | названо 8 позиций, **все с бонусом $0.00** (пыль WETH/USDC); при пороге $20 — **0 ложных выстрелов** | не только ноль |

**Вердикт гейта: прибор годен, но с названным пределом.** Норма «recall ≥ 90 % по числу»
не выполнена (86.4 %), при этом **все 11 промахов — пропуск вселенной, ни одного промаха
математики**, а по деньгам полнота 98.1 %. Промахи — артефакт ограниченной вселенной реплея
(снимок бота 31.08 был построен API-фильтром по HF и полным никогда не был). Для живого
прогона вселенная строится иначе: глубокий скан логов `Borrow`/`SupplyCollateral` за 120
суток (46 280 000 → 51 320 527) **плюс** инкрементальный добор каждые 2 минуты — то есть
класс промаха, давший все 11 потерь, в живом режиме закрыт конструкцией, а не надеждой.
Крупнейший потерянный приз реплея — $3 695 (cbXRP, блок 51354418).

Контроль нуля пройден в обе стороны: на призовых блоках прибор даёт **не ноль**
(12/12 в 51354208, 15/16 в 51354418, 28/36 в 51357120), на раундах без приза — **ноль
выше порога $20**.

---

## Предполётные факты, задающие интерпретацию прогона

* Вне-аукционный приз B+6 за 21.59 суток лежал в **14 блоках**; денежный (≥$1 000) — в
  **6 блоках, все 15.09**. Ожидание за 11 суток: **0–2 денежных призовых блока.**
* Победитель денежного B+6 стоит на `txi` 1…19 (27 из 31 события — в первых 10 % блока) и
  платит **tip медиана 21.97 gwei, p90 155, максимум 355.7** при газе 0.5–1.55 M, то есть
  **$60–1 430 за выстрел** (максимум: 0.318 ETH за приз $79.6k погашения).
* Поэтому «доля побед» — произведение трёх множителей, и каждый мерится отдельно:
  детект (M1), попадание в первый флешблок (M2, n = все блоки), готовность перебить ставку
  (M3, политика 35 % валового бонуса, кап 400 gwei).

---

## Суточные строки

| сутки | события вне аукциона | $ погашено | наших названо | M4 успели | M5 опередили | раундов | выстрелов | медиана задержки | попадание в fb#0 | RPC-запросов |
|---|---|---|---|---|---|---|---|---|---|---|

> ⚠️ 2026-09-18T07:40:03Z сторож тени: цикл замер при свежем heartbeat: fb 4068->657, blocks 371->60 (адресат: dedup)

*(строки добавляются кроном/суточным отчётом `shadow_b6/daily.py`; первая полная строка — за 19.09)*

---

## 18.09 — установка и пуск

* **Charlotte чиста от боевого:** `ps` на 18.09 не показал ни одного процесса бота, крон
  ликвидатора закомментирован 31.08, боевые юниты не загружены. Чекаут `/root/liquidator`
  (HEAD e706fde, 30.08) не тронут; тень живёт в отдельном `/root/shadow-b6`.
* **Тень запущена** 07:35Z, один процесс под `nice -n 19`, единственность — через `flock`
  (не pidfile: сторож и рука оператора могут стартовать одновременно; проверено в бою —
  крон и ручной старт столкнулись в 07:35, второй экземпляр не поднялся).
* **Поток флешблоков** (IPv4-привязка, урок STATE 9934): 4.6 сообщения/с, ~11 флешблоков на
  блок, обрывов нет.
* **Задержка решения** (от прихода транзита в суб-блоке до готовой ПОДПИСАННОЙ tx):
  **46–57 мс** на первых раундах. Это не связывающий предел: до B+6 у нас 12 секунд.
* **Вселенная заёмщиков**: 65 618 позиций на 20 рынках (глубокий скан логов
  `Borrow`/`SupplyCollateral` 46 280 000→51 320 527 = 120 суток, плюс снимок бота 31.08,
  плюс инкремент каждые 2 мин). Позитивный контроль: **10 из 11** промахов вселенной реплея
  теперь внутри; последний (`0x6975407ab1…`, приз $122) старше 120 суток — идёт вторая
  историческая нарезка 41 100 000→46 279 999.
* **С долгом и залогом**: 47 638 позиций — их здоровье пересчитывается локально на каждой
  новой цене (RPC на это не тратится).
* **M2 (попадание в первый флешблок)**, первые 56 границ блоков: хиндсайт-политика 56/56,
  **предсказательная политика (по часам цепи, упреждение 150 мс) — 56/56**. Геометрия окна
  относительно ожидаемого прихода fb#0: `[-499, -29]` мс, то есть годится любое упреждение
  от ~40 до ~490 мс. Окно широкое, RTT 30 мс в него укладывается с запасом.
  **Осторожно:** n = 56 и рынок спокоен; в каскаде зазор между флешблоками сжимается.
* **Сторож — позитивный контроль боем:** процесс убит `kill -9`, крон-сторож на следующем
  прогоне увидел `процесс_жив=0`, перезапустил (новый pid), записал строку. Вторая проверка
  (живость ЦИКЛА по росту счётчиков) отработала отдельно.
* **Две ложные тревоги при установке** (07:21) — обе доехали до инбокса агента, то есть труба
  проверена боем. Причины и гарды: (1) `rc` ssh-команды как признак — `tail` по ещё не
  созданному `watch.log` даёт 1; (2) рост счётчиков судился на двух прогонах в 3 с, а
  heartbeat пишется раз в 10 с. Оба гарда в коде (`MIN_ELAPSED = 120 c`, признак — маркер
  в выводе). Записи переведены в архив разобранного.
* **RTT:** метрики считаются по замеру **30 мс до СЕКВЕНСЕРА** (16.08). TCP-край
  `mainnet.base.org` с Charlotte — 6.6 мс; это НЕ путь до секвенсера и в метрики не входит
  (ровно это число однажды уже было ошибкой — память `fleet-session-scope-and-premise-check`).

### Дефекты, пойманные контролем ДО первого замера (18.09)

1. **Чтение раунда обгоняло запечатывание блока.** Тень видит транзит во флешблоке, а
   `eth_call(latest)` отвечает по последнему **запечатанному** блоку — то есть по предыдущему
   раунду. Контроль (`check_round_race.py`: журнальный `a_new` против лога `AnswerUpdated`
   того же блока) дал **0 совпадений из 10** — цена B+6 считалась бы по старому answer, M1
   занижался бы молча, и в журнале не было бы ни одного признака. Исправлено ожиданием
   следующего блока в потоке флешблоков + требованием `roundId > предыдущего`.
   *(Класс: [[own-meter-blind-path]] — свой счётчик показывал «раунд принят».)*
2. **Отпирание Atlas между B и B+6** делало раунд неотличимым от проигранной гонки
   («назвали N, событий 0»). Теперь читается и пишется отдельно (`atlas_unlocked.jsonl`),
   такие выстрелы исключаются из стоимости M7.
3. **Суточная строка считалась только по блокам, которые тень сама выбрала** — тест,
   способный вернуть только зелёное. Добавлен независимый скан всех `Liquidate` за сутки
   с классификацией и строкой пропущенных.
4. **Предсказательная метрика M2 сравнивала часы эпохи с часами аптайма** (wall против
   monotonic) и давала ровно 0 попаданий. Исправлено мостом wall→mono; записывается
   геометрия окна, чтобы любое упреждение оценивалось офлайн.

> ⚠️ 2026-09-18T08:00:04Z сторож тени: цикл замер при свежем heartbeat: fb 1427->879, blocks 130->81 (адресат: dedup)

### Стоимость прогона — замер 18.09 (после буты, окно 60 с)

| статья | число |
|---|---|
| установившийся темп | **0.37 запроса/с = 31 667/сутки** |
| полный проход по вселенной (92 249 позиций = 461 мультиколл) раз в час | +11 064/сутки |
| **итого оценка** | **≈ 43 000 запросов/сутки** |
| из них на **Alchemy** (`ALCHEMY_C_URL`) | **0** — платный ключ стоит только третьим фолбэком |
| эндпоинты | `base-rpc.publicnode.com` и `base.drpc.org`, отказов **0** после ограничения горячего слоя |
| CPU Charlotte | load average 0.18 при 4 ядрах, один процесс под `nice -n 19` |

Бюджет протокола (≤ 1/3 суточной квоты одного ключа = ≤ 333k CU/сутки на Alchemy)
соблюдён с запасом: расход на платном ключе нулевой. Замер сделан **после** стартового
всплеска: во время буты темп 5.38 запроса/с (первое полное чтение 92 тыс. позиций), и
выдавать его за установившийся было бы ровно ошибкой [[quota-is-an-integral-not-a-rate]].

Оптимизация расхода по ходу установки: «горячий» слой (частое перечитывание позиций)
ограничен HF ≤ 1.10 и 2 000 позициями на рынок — до этого при отсутствии контекста рынка
горячим становился весь рынок, и темп был 0.64/с при 37 % отказов у dRPC.
**Предел, который это создаёт:** залог и доля долга позиции, не попавшей в горячий слой,
могут быть старше на час (решение всё равно принимается по ней — просто по кэшу); в реплее
этот класс дал 0 промахов, но на живом каскаде он будет назван отдельно.

### Вселенная на старте прогона

92 249 позиций на 21 рынке = снимок бота (31.08) ∪ логи `Borrow`/`SupplyCollateral` за
**240 суток** (41 100 000 → 51 320 527, две нарезки) ∪ инкремент каждые 2 минуты.
**Позитивный контроль: все 11 из 11** заёмщиков, потерянных вселенной реплея, теперь внутри.

### Сторож: три класса ложной тревоги закрыты при установке

| класс | как проявился | гард |
|---|---|---|
| `rc` команды вместо содержимого | `tail` по ещё не созданному `watch.log` даёт 1 → «Charlotte недоступна» | признак = маркер в выводе |
| два прогона внутри интервала heartbeat | 3 с между прогонами, heartbeat пишется раз в 10 с → «цикл замер» | `MIN_ELAPSED = 120 с` |
| законный рестарт обнуляет счётчики | `fb 4068 -> 328` → «цикл замер», сторож перезапустил живой процесс | сравнение pid и `up_sec` |

Положительная проверка обоих слоёв выполнена боем: `kill -9` → крон-сторож увидел
`процесс_жив=0`, поднял, записал; тревоги доехали до инбокса агента (hil=False).
| 2026-09-18 | 0 | $0 | 0 | 0 | 0 | 975 | 0 | 1432мс | 45879/45931 | 33281 |

<details><summary>детали 2026-09-18</summary>

```

детали 2026-09-18: раундов 975 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=45931, попадание в fb#0 99.9%, зазор между последним fb блока N-1 и fb#0 блока N: p10 439 p50 467 p90 479 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=20893(err 0), base.drpc.org=10988(err 3166), mainnet.base.org=1400(err 0)
   НЕЗАВИСИМЫЙ СКАН блоков 51450127..51493327: событий на отслеживаемых рынках 106, вне tx Atlas 102, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение [None]) — 102
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 500: Internal Server Error
   сторож: up 22.4 ч, fb_msg 444113, обрывов WS 3, позиций с долгом 58166, ошибок решения 0
```
</details>
| 2026-09-19 | 0 | $0 | 0 | 0 | 0 | 787 | 0 | 1422мс | 95004/95061 | 62277 |

<details><summary>детали 2026-09-19</summary>

```

детали 2026-09-19: раундов 787 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=95061, попадание в fb#0 99.9%, зазор между последним fb блока N-1 и fb#0 блока N: p10 437 p50 468 p90 479 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=37086(err 0), base.drpc.org=22326(err 6737), mainnet.base.org=2865(err 0)
   НЕЗАВИСИМЫЙ СКАН блоков 51493327..51536527: событий на отслеживаемых рынках 0, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 500: Internal Server Error
   сторож: up 46.4 ч, fb_msg 919262, обрывов WS 5, позиций с долгом 58147, ошибок решения 0
```
</details>
| 2026-09-20 | 0 | $0 | 0 | 0 | 0 | 914 | 0 | 1416мс | 144228/144287 | 91623 |

<details><summary>детали 2026-09-20</summary>

```

детали 2026-09-20: раундов 914 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=144287, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 436 p50 469 p90 479 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=53924(err 0), base.drpc.org=33338(err 9963), mainnet.base.org=4361(err 0)
   НЕЗАВИСИМЫЙ СКАН блоков 51536527..51579727: событий на отслеживаемых рынках 0, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 500: Internal Server Error
   сторож: up 70.4 ч, fb_msg 1394361, обрывов WS 6, позиций с долгом 58131, ошибок решения 0
```
</details>
| 2026-09-21 | 0 | $0 | 0 | 0 | 0 | 1586 | 0 | 1558мс | 194025/194117 | 122192 |

<details><summary>детали 2026-09-21</summary>

```

детали 2026-09-21: раундов 1586 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=194117, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 435 p50 468 p90 480 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=71912(err 0), base.drpc.org=44368(err 13464), mainnet.base.org=5912(err 0)
   НЕЗАВИСИМЫЙ СКАН блоков 51579727..51622927: событий на отслеживаемых рынках 3, вне tx Atlas 2, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение [None]) — 2
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 500: Internal Server Error
   сторож: up 94.4 ч, fb_msg 1868245, обрывов WS 11, позиций с долгом 58150, ошибок решения 0
```
</details>
| 2026-09-22 | 0 | $0 | 0 | 0 | 0 | 1284 | 0 | 1466мс | 243133/243244 | 149272 |

<details><summary>детали 2026-09-22</summary>

```

детали 2026-09-22: раундов 1284 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=243244, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 435 p50 468 p90 480 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=88986(err 0), base.drpc.org=52794(err 15239), mainnet.base.org=7492(err 530)
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
   скан 51622927 ошибка HTTP Error 413: Payload Too Large
```
</details>
| 2026-09-23 | 0 | $0 | 0 | 0 | 0 | 1301 | 3 | 1834мс | 292726/292867 | 188214 |

<details><summary>детали 2026-09-23</summary>

```

детали 2026-09-23: раундов 1301 (с названными позициями 2), выстрелов-в-тени 3; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=292867, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 425 p50 468 p90 481 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=108374(err 0), base.drpc.org=70246(err 21625), mainnet.base.org=9594(err 882)
   НЕЗАВИСИМЫЙ СКАН блоков 51666127..51709327: событий на отслеживаемых рынках 7, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 500: Internal Server Error
   отпираний Atlas до B+6 (выстрел отменяется): 2
   сторож: up 142.4 ч, fb_msg 2815007, обрывов WS 17, позиций с долгом 58310, ошибок решения 0
```
</details>
| 2026-09-24 | 0 | $0 | 0 | 0 | 0 | 1299 | 4 | 1764мс | 342140/342301 | 306128 |

<details><summary>детали 2026-09-24</summary>

```

детали 2026-09-24: раундов 1299 (с названными позициями 2), выстрелов-в-тени 4; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=342301, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 424 p50 467 p90 481 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=179415(err 0), base.drpc.org=116264(err 24774), mainnet.base.org=10449(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51709327..51752527: событий на отслеживаемых рынках 10, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 500: Internal Server Error
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 166.4 ч, fb_msg 3288669, обрывов WS 23, позиций с долгом 58717, ошибок решения 0
```
</details>
| 2026-09-25 | 0 | $0 | 0 | 0 | 0 | 1282 | 0 | 1587мс | 391659/391823 | 344769 |

<details><summary>детали 2026-09-25</summary>

```

детали 2026-09-25: раундов 1282 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=391823, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 425 p50 467 p90 481 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=198810(err 0), base.drpc.org=133981(err 30411), mainnet.base.org=11978(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51752527..51795727: событий на отслеживаемых рынках 0, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ РАУНДА (выборка 25): совпало 25, РАСХОЖДЕНИЙ 0, транзитов без лога 0
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 190.4 ч, fb_msg 3763007, обрывов WS 28, позиций с долгом 58651, ошибок решения 0
```
</details>
| 2026-09-26 | 0 | $0 | 0 | 0 | 0 | 634 | 0 | 1556мс | 440882/441048 | 377093 |

<details><summary>детали 2026-09-26</summary>

```

детали 2026-09-26: раундов 634 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=441048, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 426 p50 468 p90 482 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=216551(err 0), base.drpc.org=147128(err 33947), mainnet.base.org=13414(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51795727..51838927: событий на отслеживаемых рынках 0, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ РАУНДА (выборка 25): совпало 25, РАСХОЖДЕНИЙ 0, транзитов без лога 0
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 214.4 ч, fb_msg 4237770, обрывов WS 28, позиций с долгом 56881, ошибок решения 0
```
</details>
| 2026-09-27 | 0 | $0 | 0 | 0 | 0 | 636 | 0 | 1564мс | 490713/490880 | 408643 |

<details><summary>детали 2026-09-27</summary>

```

детали 2026-09-27: раундов 636 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=490880, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 427 p50 469 p90 482 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=233201(err 1), base.drpc.org=160533(err 37555), mainnet.base.org=14909(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51838927..51882127: событий на отслеживаемых рынках 1, вне tx Atlas 1, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение [None]) — 1
   КОНТРОЛЬ ГОНКИ РАУНДА (выборка 25): совпало 25, РАСХОЖДЕНИЙ 0, транзитов без лога 0
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 238.4 ч, fb_msg 4712644, обрывов WS 28, позиций с долгом 58551, ошибок решения 0
```
</details>
| 2026-09-28 | 0 | $0 | 0 | 0 | 0 | 1331 | 0 | 1555мс | 539884/540088 | 520583 |

<details><summary>детали 2026-09-28</summary>

```

детали 2026-09-28: раундов 1331 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=540088, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 428 p50 469 p90 482 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=306536(err 2), base.drpc.org=197892(err 48439), mainnet.base.org=16155(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51882127..51925327: событий на отслеживаемых рынках 4, вне tx Atlas 4, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение [None]) — 4
   КОНТРОЛЬ ГОНКИ РАУНДА (выборка 25): совпало 25, РАСХОЖДЕНИЙ 0, транзитов без лога 0
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 262.4 ч, fb_msg 5187226, обрывов WS 31, позиций с долгом 58980, ошибок решения 0
```
</details>
| 2026-09-29 | 0 | $0 | 0 | 0 | 0 | 1037 | 0 | 1532мс | 589397/589622 | 639561 |

<details><summary>детали 2026-09-29</summary>

```

детали 2026-09-29: раундов 1037 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=589622, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 429 p50 469 p90 482 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=380665(err 2), base.drpc.org=241700(err 63484), mainnet.base.org=17196(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51925327..51968527: событий на отслеживаемых рынках 11, вне tx Atlas 7, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение [None]) — 7
   КОНТРОЛЬ ГОНКИ РАУНДА (выборка 25): совпало 25, РАСХОЖДЕНИЙ 0, транзитов без лога 0
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 286.4 ч, fb_msg 5659978, обрывов WS 33, позиций с долгом 59196, ошибок решения 0
```
</details>
| 2026-09-30 | 0 | $0 | 0 | 0 | 0 | 996 | 0 | 1580мс | 638808/639063 | 762272 |

<details><summary>детали 2026-09-30</summary>

```

детали 2026-09-30: раундов 996 (с названными позициями 0), выстрелов-в-тени 0; событий на целевых блоках 0, вне аукциона 0
   границы блоков: n=639063, попадание в fb#0 100.0%, зазор между последним fb блока N-1 и fb#0 блока N: p10 427 p50 468 p90 482 мс
   RPC по эндпоинтам: base-rpc.publicnode.com=456586(err 37), base.drpc.org=287347(err 79040), mainnet.base.org=18339(err 883)
   НЕЗАВИСИМЫЙ СКАН блоков 51968527..52011727: событий на отслеживаемых рынках 0, вне tx Atlas 0, из них тень видела 0, назвала 0
     из пропущенных: КЛАССА B+6 (раунд ровно за 6 блоков) — 0; вне класса (смещение []) — 0
   КОНТРОЛЬ ГОНКИ: ошибка HTTP Error 429: Too Many Requests
   отпираний Atlas до B+6 (выстрел отменяется): 4
   сторож: up 310.4 ч, fb_msg 6130479, обрывов WS 39, позиций с долгом 59699, ошибок решения 0
```
</details>
