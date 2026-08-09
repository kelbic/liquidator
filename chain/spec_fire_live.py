"""SPEC-FIRE LIVE (Stage B, 24.07): боевые пробы-ликвидации по ЖИРНЫМ целям в последние
секунды predict-окна — попытка взять предочередь (11% призов, победители-спекулянты idx+2).

Гейт снят данными (см. STATE 24.07): бэктест каскада 24-25.06 (87% ликвидаций при армленном
окне, lead med 67с) + live spec-canary DELTA=4/3 (предочередь достижима). Дизайн:
- ОКНО: arm-событие с целями >= SPEC_FIRE_MIN_REPAID_USD ($500k, директива kelbic).
- ОГОНЬ: только когда |ret| >= dev × SPEC_FIRE_THRESHOLD (1.0) — транзит неминуем в секундах
  (оракул триггерится на dev); до порога НЕ стреляем (там lead минуты — пробы в пустоту).
- ПРОБА: настоящий liquidate-calldata top-1 жирной цели: полный repaid, своп из PREARM-кэша
  (мега: Kyber-агрегат, обрыв пула) либо direct-роут (0 RTT); minProfit-floor аналитический
  (SPEC_FIRE_MIN_PROFIT_BPS от repaid) — контракт ревертит плохой филл. До флипа Morpho
  ревертит 'healthy' (~$0.01-0.02); проба в блоке транзита ПОСЛЕ него = WIN полным призом.
- БЕЗОПАСНОСТЬ: send через send_spec_fire_probe (канареечная nonce-машинерия под общим
  _SEND_LOCK; _INFLIGHT>0 -> пробы молчат — боевая tx главнее); пробы НЕ трогают
  _RECENT_SENDS (не глушат реактивный выстрел); WIN маркирует дедуп отдельно; свой
  газ-бакет SPEC_FIRE_MAX_GAS_USD; капы проб на окно; receipts собираются фоном
  (kill-switch-слепоты к газу нет — газ букается в бакет по receipt).
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time

from chain.spec_fire import _label_of

log = logging.getLogger("liq.spec_fire_live")

# Дефекты кода, которые бой не переживёт НИКОГДА: их нельзя показывать как осечку связи.
_CODE_BUGS = (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError)


def _diag(e) -> str:
    """Класс исключения + его текст (отредактированный от ключей).

    Голый type(e).__name__ прятал арм доски пять суток: строка «доска arm fail: NameError»
    читается как операционная осечка, из-за которой залп разумно откатился на V1, — а на деле
    это была необъявленная переменная, то есть путь не мог сработать ни разу и ни у кого.
    """
    from chain.rpc import redact
    tag = " ДЕФЕКТ КОДА" if isinstance(e, _CODE_BUGS) else ""
    return "%s%s: %s" % (type(e).__name__, tag, redact(e)[:200])

SPEC_FIRE_MIN_REPAID_USD = float(os.environ.get("SPEC_FIRE_MIN_REPAID_USD", "500000"))
SPEC_FIRE_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_INTERVAL_SEC", "0.7"))
# ПЛОТНЫЙ РЕЖИМ (31.07: первая боевая гонка cbBTC 14:07 проиграна ДЕНЬГАМИ — победитель сидел
# в предочереди блока транзита idx 2928 с tip 0.0034, наша реакция 106мс легла блоком позже с
# tip 30): в FCFS решает не скорость и не tip, а присутствие пробы в очереди МЕЖДУ транзитом и
# спамом конкурента. Это гонка плотностей: P(win) ~ наш темп / (наш + конкурента). Хвост 2.0с
# давал ~1 пробу на блок со случайной фазой — флип 14:07 упал между пробами. Плотный режим:
# вся горячая фаза (|ret| >= порога) на DENSE_INTERVAL 0.5с (4 пробы/блок; пол send-слоя
# SPEC_FIRE_MIN_INTERVAL_SEC=0.25 совместим), джиттер ±0.15 ломает подпись расписания.
# Выравнивание фазы по границе 2с-блока НЕ строим: flashblocks каждые 200мс делают границу
# бессмысленной — окно очереди скользящее, место покупает только плотность.
# Откат без кода: SPEC_FIRE_DENSE=0 -> прежний burst/slow-каденс и кап 24.
SPEC_FIRE_DENSE = os.environ.get("SPEC_FIRE_DENSE", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_DENSE_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_DENSE_INTERVAL_SEC", "0.5"))
SPEC_FIRE_DENSE_JITTER_SEC = float(os.environ.get("SPEC_FIRE_DENSE_JITTER_SEC", "0.15"))
# Кап окна в плотном режиме: 150 × 0.5с = 75с покрытия >= p75 «первая проба -> транзит» жирных
# окон (64с, датасет 27.07). Цена худшего окна 150×$0.015 ~= $2.25 — суточный бакет $10 держит
# 4+ полных окна, WIN-cooldown (900с) рвёт серию раньше; окно и так стопится на confirmed/disarm.
SPEC_FIRE_MAX_PROBES_WINDOW = int(os.environ.get("SPEC_FIRE_MAX_PROBES_WINDOW")
                                  or (150 if SPEC_FIRE_DENSE else 24))
# EV-ПЛОТНОСТЬ (06.08, «го» kelbic; вскрытие $65.6k cbBTC 27.07): победитель держал 10-17
# проб/блок против наших 4 и взял приз $2,879 пробой idx+8 после транзита; наша реакция
# (детект суб-блочно, send 510мс) легла ревертом −$6.55 — реакция не бьёт предочередь,
# место покупает только плотность. Плотность = функция размера добычи: жирная цель
# (долг >= FAT_DEBT_USD) в досягаемости тика переключает каденс DENSE 0.5с -> 0.12с
# (квант петли SPEC_FIRE_POLL_SEC=0.15 даёт ~13 проб/блок — паритет с лучшим замеренным
# конкурентом) и кап окна 150 -> 600 (~90с горячей фазы). Худшее жирное окно 600×$0.015
# ~= $9 против приза >=$1.5k при долге $50k. Сопряжённо: пол send-слоя 0.25 -> 0.10 и
# бакет 20 -> 40 (execute.py). Откат: SPEC_FIRE_EV_DENSITY=0 (см. docs/ROLLBACKS.md).
SPEC_FIRE_EV_DENSITY = os.environ.get("SPEC_FIRE_EV_DENSITY", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_FAT_DEBT_USD = float(os.environ.get("SPEC_FIRE_FAT_DEBT_USD", "50000"))
SPEC_FIRE_FAT_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_FAT_INTERVAL_SEC", "0.12"))
SPEC_FIRE_FAT_JITTER_SEC = float(os.environ.get("SPEC_FIRE_FAT_JITTER_SEC", "0.04"))
SPEC_FIRE_FAT_CAP = int(os.environ.get("SPEC_FIRE_FAT_CAP", "600"))
# ГЕЙТ ЖИРА ПО ПРИЗУ («го» kelbic 07.08; CASCADE_TIER_DESIGN, экономика 180д): окупаемость
# плотности определяет ПРИЗ = долг × бонус, а FAT_DEBT меряет долг. $50k долга на cbBTC
# (LIF 4.39%) = приз $2.2k — кромка окупаемости, а на 12.68%-рынках (cbXRP/AERO/stkWELL)
# окупаемость начинается уже с долга ~$16k — такие окна мы не уплотняли. PRIZE_USD > 0 =
# гейт по призу (bonus рынка из covered-снапшота); рынок без бонуса -> фолбэк на долг-гейт
# (fail-open к прежнему). 0 = прежний долг-гейт бит-в-бит. Откат: убрать из .env.
SPEC_FIRE_FAT_PRIZE_USD = float(os.environ.get("SPEC_FIRE_FAT_PRIZE_USD", "0"))


def _tgts_fat(tgts, bonus_of=None) -> bool:
    """Жир в досягаемости тика -> EV-каденс/кап окна."""
    if not SPEC_FIRE_EV_DENSITY:
        return False
    if SPEC_FIRE_FAT_PRIZE_USD > 0 and bonus_of is not None:
        def _fat(t):
            try:
                b = float(bonus_of(t[0]) or 0.0)
            except Exception:
                b = 0.0
            return (t[2] * b >= SPEC_FIRE_FAT_PRIZE_USD) if b > 0 else (t[2] >= SPEC_FIRE_FAT_DEBT_USD)
        return any(_fat(t) for t in tgts)
    return any(t[2] >= SPEC_FIRE_FAT_DEBT_USD for t in tgts)


def _window_cap(fat_mode: bool) -> int:
    return SPEC_FIRE_FAT_CAP if (SPEC_FIRE_EV_DENSITY and fat_mode) else SPEC_FIRE_MAX_PROBES_WINDOW


def _dense_interval(fat_mode: bool) -> float:
    if SPEC_FIRE_EV_DENSITY and fat_mode:
        _j = SPEC_FIRE_FAT_JITTER_SEC
        return max(0.10, SPEC_FIRE_FAT_INTERVAL_SEC + (random.uniform(-_j, _j) if _j > 0 else 0.0))
    _j = SPEC_FIRE_DENSE_JITTER_SEC
    return max(0.25, SPEC_FIRE_DENSE_INTERVAL_SEC + (random.uniform(-_j, _j) if _j > 0 else 0.0))
# КЭШ ПРОБЫ: _build на каждую пробу = 2×aggregate3 + роут — на плотном каденсе это душило бы
# ядро и RPC (эффект наблюдателя). Позиция/шары за секунды не меняются, плохой филл ревертится
# min_profit-полом контракта -> собранный calldata переиспользуем REBUILD_SEC.
# 5.0 -> 2.5 (31.07, «го» kelbic после апгрейда Шарлотты до 2 ядер): 5с = 2.5 блока жизни
# жертвы, проба могла стрелять устаревшими шарами/роутом; 2.5с = свежесть в один блок, защита
# hot path больше не требует голодания (2-е ядро). Гарды на первом окне: load>1.5 или
# деградация реакции -> откат SPEC_FIRE_REBUILD_SEC=5 (см. docs/ROLLBACKS.md).
SPEC_FIRE_REBUILD_SEC = float(os.environ.get("SPEC_FIRE_REBUILD_SEC", "2.5"))
# TIP-ГАРД: FCFS — tip НЕ покупает место (победитель 14:07 взял приз с tip 0.0034 против наших
# 30). Проба обязана остаться копеечной, даже если боевой TIP_GWEI поднимут или сработает
# каскадная ступень: жёсткий кап поверх cfg.tip_gwei.
SPEC_FIRE_TIP_CAP_GWEI = float(os.environ.get("SPEC_FIRE_TIP_CAP_GWEI", "0.1"))
# TOP-K ротация (дефолт 1 = прежний top-1): в каскад флипаются несколько жертв — K>1 чередует
# пробы по K самым жирным досягаемым целям (плотность НА ЦЕЛЬ делится на K — включать только
# когда плотность на top-1 доказала себя).
SPEC_FIRE_TOPK = max(1, int(os.environ.get("SPEC_FIRE_TOPK", "1")))
# EDGE-СЛОТ («го» kelbic 06.08, проигранная гонка №2 cbXRP 05.08): все 119 проб плотного
# режима легли в top-1-ПО-ДОЛГУ ($33k, dist 0.63%), а флипнулась БЛИЖАЙШАЯ-по-dist мелочь
# ($1.5k, 0 проб) — победитель взял её в блоке флипа (idx=4729), хотя наши пробы сидели
# ГЛУБЖЕ в предочереди (idx 1-3). Ранг по долгу максимизирует размер приза, но P(флип)
# максимальна у кромки: ближайшая-по-dist досягаемая цель получает ГАРАНТИРОВАННЫЙ слот в
# ротации (head = top-K по долгу + edge; плотность на цель делится, 4 пробы/блок хватает
# на двоих). Откат SPEC_FIRE_EDGE_SLOT=0 = прежний чистый ранг по долгу.
SPEC_FIRE_EDGE_SLOT = os.environ.get("SPEC_FIRE_EDGE_SLOT", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_RECEIPT_POLL_SEC = float(os.environ.get("SPEC_FIRE_RECEIPT_POLL_SEC", "1.0"))
# КАСКАДНЫЙ ТИР («го» kelbic 07.08; v4 authorized-set + 4 залповых EOA задеплоены): залп =
# та же проба РАЗНЫМИ nonce-потоками вслед за пробой главного кошелька, сдвиг фазы ~50мс
# на ногу — модель P(win)=W/τ: τ делится на (1+N ног), 5 потоков = 2-9% на контестед-событие
# (см. CASCADE_TIER_DESIGN). Активация СТРОГО fat_mode И каскад-детект (армы>=CASCADE_WINDOWS/час
# И свежий факт-флаг — тот же гибрид, что бустер бакета); вне каскада тир молчит — один EOA,
# штиль не жжём ($0 в худший месяц). Ноги НЕ входят в кап окна (кап меряет главный nonce-поток) —
# их ограничивает $-бакет CASCADE_TIER_GAS_USD (execute). WIN любой ноги закрывает окно, глушит
# реактивный дубль и рвёт паттерн кулдауном — как главная. Откат: SPEC_FIRE_VOLLEY=0 (execute).
SPEC_FIRE_VOLLEY_PHASE_SEC = float(os.environ.get("SPEC_FIRE_VOLLEY_PHASE_MS", "50")) / 1000.0
# ПАРАЛЛЕЛЬНЫЕ НОГИ (07.08, замер боевого send-пути: 1717 отправок за 7д, journalctl DIAG
# send-stages). Разложение: sign p50=4мс/p99=36, relay_spawn p50=0 (SEND-3 отдаёт raw в
# секвенсер daemon-тредами ДО синхронной ноги), alch p50=55мс/p90=69/max=222. Итог: до
# СЕКВЕНСЕРА (= гонка) нога доезжает за ~5мс, но вызов send_tx ДЕРЖИТ тред 60мс (p50), и
# последовательный цикл прибавлял их к сдвигу: шаг ног был 50+60=110мс вместо 50 — расписание
# задавала сеть, а не мы, и 4-я нога уходила на ~440мс. Тред на ногу: сдвиг снова наш
# (i+1)*PHASE, RTT перекрываются. Откат: SPEC_FIRE_VOLLEY_PARALLEL=0 -> прежний цикл бит-в-бит.
SPEC_FIRE_VOLLEY_PARALLEL = os.environ.get(
    "SPEC_FIRE_VOLLEY_PARALLEL", "1").lower() not in ("0", "false", "no")
# ОДНОВРЕМЕННЫЙ ЗАЛП ПО ДЕТЕКТУ: в ветке pending-handoff транзит УЖЕ виден — момент известен,
# и лесенка сдвига там перевёрнута против цели (окно победы W=0.5-2.2мс, а нога 4 уходила
# через сотни мс). Разнос фазы ценен ровно там, где момент НЕизвестен (обычная ветка: N ног =
# N участков покрытия); по детекту бьём разом. Откат: SPEC_FIRE_VOLLEY_DETECT_SYNC=0.
SPEC_FIRE_VOLLEY_DETECT_SYNC = os.environ.get(
    "SPEC_FIRE_VOLLEY_DETECT_SYNC", "1").lower() not in ("0", "false", "no")
# ФАЗОВЫЙ ГЕЙТ ПО РЕШЁТКЕ OCR («го» kelbic 07.08; замер analysis/push_hazard.py — 2520 пушей
# за 7д + проверка объекта в знаменателе). Пуш садится не когда угодно, а на границе
# 30-секундного раунда от ПРЕДЫДУЩЕГО пуша: точно на узле BTC 79%/ETH 76%/ADA 57%, в ±2с — 96%
# у BTC/ETH. Деградация по РАССТОЯНИЮ, не по фиду: гэп <1800с держит 92-100%, дальше фаза
# уплывает (~2с/час, скью часов L2), у альтов медианный гэп 4600-8000с — они почти всегда за
# горизонтом и гейт для них сам выключится. Значит 24 секунды из каждых 30 в горячей фазе
# доказуемо холостые: замеренный эффект ×1.15 на ТОМ ЖЕ газе (против ×1.19 за ТРОЙНОЙ газ у
# «просто больше проб»). Решётка проверена на КРУПНЫХ движениях отдельно (внутрифидовые
# квартили: пул рангов 87/91/90/90 при контроле 90/90/89/89; BTC/ETH верхний квартиль 96/97%) —
# гейт не слепнет там, где приз. Замер считан «на пуш», а не «на приз»: живые счётчики
# lattice_hits/misses закрывают этот предел нашим собственным потоком.
# ВСЕ ГАРДЫ FAIL-OPEN (сомнение => стреляем как раньше): мало пушей, решётка не подтверждена
# онлайн, фаза протухла. Откат: SPEC_FIRE_LATTICE_GATE=0.
SPEC_FIRE_LATTICE_GATE = os.environ.get(
    "SPEC_FIRE_LATTICE_GATE", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_LATTICE_P = float(os.environ.get("SPEC_FIRE_LATTICE_P_SEC", "30"))
SPEC_FIRE_LATTICE_TOL = float(os.environ.get("SPEC_FIRE_LATTICE_TOL_SEC", "2"))
SPEC_FIRE_LATTICE_MAX_AGE = float(os.environ.get("SPEC_FIRE_LATTICE_MAX_AGE_SEC", "1800"))
SPEC_FIRE_LATTICE_MIN_N = int(os.environ.get("SPEC_FIRE_LATTICE_MIN_N", "12"))
SPEC_FIRE_LATTICE_MIN_FRAC = float(os.environ.get("SPEC_FIRE_LATTICE_MIN_FRAC", "0.80"))
# --- v5 stateful board (07.08): armed-план в контракте, ноги залпа пробят 1 байтом ---
# Мотив уточнён разведкой 07.08: конкурент держит стену 2592 tx от 2592 EOA в ОДНОМ блоке.
# 1-байт калдаты делает такую плотность дешёвой и попутно не палит цель (наши сегодняшние
# healthy-реверты несут полные аргументы liquidate и читаются кем угодно).
# ВЫКЛЮЧЕНО по умолчанию: включать только после деплоя v5 (у v4 нет fallback -> пробы
# ревертят). OFF = V1 бит-в-бит, ни одной новой tx.
SPEC_FIRE_V2_BOARD = os.environ.get("SPEC_FIRE_V2_BOARD", "0").lower() not in ("0", "false", "no")
SPEC_FIRE_V2_PLAN_TTL_SEC = int(os.environ.get("SPEC_FIRE_V2_PLAN_TTL_SEC", "900"))
SPEC_FIRE_V2_REARM_SEC = float(os.environ.get("SPEC_FIRE_V2_REARM_SEC", "240"))
# #57 (ревью 08.08): арм доски — ЕДИНСТВЕННАЯ точка входа флота в бой (право огня движку даёт
# только живой севший план; V1-деградация сидит за тир-гейтом, ложным в решающий блок). Осечка
# ОДНОГО setPlan при каденсе переарма 240с оставляла окно короче ретрая вовсе без доски: движок
# выглядит здоровым, проб ноль. Поэтому неудача переспрашивается БЫСТРО, а не через каденс.
SPEC_FIRE_V2_ARM_RETRY_SEC = float(os.environ.get("SPEC_FIRE_V2_ARM_RETRY_SEC", "5"))
# Право огня движку по подтверждённому транзиту вне зависимости от порога огня (#47+#41).
SPEC_FIRE_HANDOFF_KEEPALIVE = os.environ.get(
    "SPEC_FIRE_HANDOFF_KEEPALIVE", "1").lower() not in ("0", "false", "no")
# Как часто переиздавать отметку probe_board, пока план жив (потребители гейтят 10с).
SPEC_FIRE_BOARD_RESTAMP_SEC = float(os.environ.get("SPEC_FIRE_BOARD_RESTAMP_SEC", "4"))
# Право огня движку в ЗАКРЫТУЮ секунду фазового гейта (#64): гейт экономит газ кадансовых
# проб, а не выключает плотность. Откат: SPEC_FIRE_LATTICE_KEEPALIVE=0.
SPEC_FIRE_LATTICE_KEEPALIVE = os.environ.get(
    "SPEC_FIRE_LATTICE_KEEPALIVE", "1").lower() not in ("0", "false", "no")
# #49: предел доверия к `updatedAt` оракула. Больше — часы разъехались (или апдейт из
# прошлого), метка встаёт на время детекта, и это видно счётчиком lattice_skew_bad.
SPEC_FIRE_LATTICE_MAX_SKEW_SEC = float(os.environ.get("SPEC_FIRE_LATTICE_MAX_SKEW_SEC", "120"))
SPEC_FIRE_V2_PROBE_GAS = int(os.environ.get("SPEC_FIRE_V2_PROBE_GAS", "260000"))
# ЛИМИТ ГАЗА ПРОБЫ ДОСКИ — считается от ВЫИГРЫША, а не от no-op'а (замер 07.08, #32).
# Однобайтовая проба почти всегда тихий no-op (18.6k), но в тот единственный раз, когда
# триггер сработал, ОНА ЖЕ исполняет весь план. Три измеренных числа задают лимит:
#   709 038 — реальная ликвидация со свопом (исполнитель конкурента, блок 49642833);
#   +214 941 — надбавка нашего пути: план на 2KB копируется из ХОЛОДНОГО стораджа
#              (форк-замер testFireGasFallbackVsDirectHeavyRoute: 449 983 против 235 042);
#   2 470 096 — фактический расход на ките $1.5M (форк-экзамен 25.07, из-за него главный
#              путь огня и держит gas_limit_floor=4M).
# Прежние 260k (и 625k у V1-пробы) не покрывают ни одного из этих случаев, а OOG внутри
# fallback ГЛОТАЕТСЯ try/catch: tx успешна, план цел, приз ушёл — без следа в журнале.
# На Base платим за ИСПОЛЬЗОВАННЫЙ газ, поэтому запас в лимите почти бесплатен; его
# единственная цена — резерв maxFee x limit на балансе ноги, и он разведён с расходом
# отдельно (см. _VOLLEY_EXPECTED_GAS в execute: гейт по худшему случаю, декремент по факту).
SPEC_FIRE_V2_FIRE_GAS = int(os.environ.get("SPEC_FIRE_V2_FIRE_GAS", "3000000"))
SPEC_FIRE_V2_TRIGGER_MARGIN_BPS = int(os.environ.get("SPEC_FIRE_V2_TRIGGER_MARGIN_BPS", "30"))
# Пауза между отправкой setPlan и доверием к доске: пока запись не села в блок, однобайтовая
# нога попадает в пустой сторадж и тихо возвращается. 6с = 3 блока Base с запасом.
SPEC_FIRE_V2_BOARD_SETTLE_SEC = float(os.environ.get("SPEC_FIRE_V2_BOARD_SETTLE_SEC", "6"))
# ВЕРДИКТ ПОБЕДЫ — ИЗ ЛОГОВ ПРОТОКОЛА, А НЕ ИЗ СТАТУСА TX (дефект найден 07.08, #32).
# У пробы V1 (полная калдата liquidate) статус говорит всё: цель здорова -> Morpho ревертит ->
# ревертит и tx (status=0). У пробы ДОСКИ (1 байт в fallback) статус не значит НИЧЕГО:
#   healthy по цене  -> `return`               -> status=1, газ ~18.6k;
#   попытка неудачна -> `catch {}` (строка 232)-> status=1, газ сотни тысяч, план цел;
#   ПОБЕДА           -> liquidate прошёл       -> status=1 + событие Liquidated.
# То есть на доске status==1 у ВСЕХ трёх исходов, и прежний критерий «status==1 = WIN» объявил бы
# победой первую же холостую пробу: mark_recent_send глушит реактивный дубль, кулдаун гасит ВСЕ
# окна на WIN_COOLDOWN_SEC, окно закрывается. Блэкаут ровно в тот каскад, ради которого строился
# флот. Признак победы один и общий для обоих путей: событие Liquidated от НАШЕГО контракта
# (fallback зовёт ту же liquidate, поэтому оно есть и там). Газ негоден как признак: неудачная
# попытка жжёт его столько же, сколько выигрыш.
# Асимметрия цены ошибки задаёт направление отказа: ложная победа = блэкаут огня, пропущенная =
# приз УЖЕ у владельца (контракт сметает прибыль сам), план одноразовый (delete _plan), а лишние
# пробы по нему — дешёвые no-op'ы. Значит на доске fail-closed: нет события — нет победы.
LIQUIDATED_TOPIC0 = "0xde0aa27286f5cb3a4ed853dc4823ead62d63e92cdf13de09d6aece56970721a4"
# Флот (07.08): ног может быть десятки. Тред-на-ногу при 70 ногах на ДВУХЪЯДЕРНОЙ машине —
# это ровно тот эффект наблюдателя, на который у нас есть задокументированные инциденты.
# Поэтому ноги режутся на пачки: не больше N тредов на залп, каждый ведёт свою долю ног.
SPEC_FIRE_VOLLEY_MAX_THREADS = int(os.environ.get("SPEC_FIRE_VOLLEY_MAX_THREADS", "8"))
# ФОЛБЭК ЦЕЛИ (01.08, ревью гонки cbDOGE): топ-цель «нет свопа» монополизировала тик —
# $4.1k-жертва в досягаемости И в капе пула не получила НИ ОДНОЙ пробы за три окна
# (18:45-18:50), конкурент выиграл присутствием (tip 0.0055 < наших 0.05). Небилдящаяся
# цель уходит на скамейку NOBUILD_RETRY_SEC (prearm/partial могут ожить в окне), тик
# отдаётся следующей досягаемой (глубина списка FALLBACK_DEPTH; плотность по-прежнему
# ротируется в top-K, хвост — только фолбэк). Откат SPEC_FIRE_BUILD_FALLBACK=0.
SPEC_FIRE_BUILD_FALLBACK = os.environ.get("SPEC_FIRE_BUILD_FALLBACK", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_FALLBACK_DEPTH = int(os.environ.get("SPEC_FIRE_FALLBACK_DEPTH", "4"))
SPEC_FIRE_NOBUILD_RETRY_SEC = float(os.environ.get("SPEC_FIRE_NOBUILD_RETRY_SEC", "10"))
# КАСКАД-ДЕТЕКТ, ГИБРИД (калибровка 31.07: армы — надежда, факты — подтверждение; 31.07 было
# 44 арма/час при 5 ликвидациях за ДЕНЬ — армы одни не сигнал ни при каком пороге, а факты
# разделяют штиль/каскад идеально: p90 штиля 3/час vs 11/час у мини-каскада 28.07):
# бустер бакета дёргается только при АРМЫ >= CASCADE_WINDOWS/час И свежем факт-флаге.
# Флаг пишет вотчер Вены (>=3 наших ликвидаций/час, blue-api + get_logs-фолбэк); нет/устарел
# (> FACTS_TTL) -> fail-closed: бустер молчит (цена пропуска = штатный бакет $20, слепой
# подъём хуже — боевой урок: API теряет ~8% событий и таймаутит именно в каскад).
# Откат гибрида: SPEC_FIRE_HYBRID=0 = бустер по одним армам (как до калибровки).
# ДРОБЛЕНИЕ ПРОБЫ (01.08, проигранная гонка cbDOGE: сейз кита $80k > ёмкость прямого пула
# ~$6.8k => direct_swap=None, prearm пару «роутнутой мелочью» не грел => «нет свопа» все 50с
# лида; победитель взял $5.1k ЧАСТИЧКОЙ, подогнанной под глубину выхода — сейз/репей ровно
# LIF). Если полный сейз не лезет в кап роута — режем repaid_shares до HEADROOM% капа: роут
# снова 0 RTT, minProfit контракта скейлится с repaid, остаток жертвы лечится и ждёт
# следующий срез. Kyber тут НЕ спасение (замер 25.07: на ките $80k cbDOGE отдаёт 21.5%
# номинала). Откат: SPEC_FIRE_PARTIAL=0 = прежнее «только полный сайз».
SPEC_FIRE_PARTIAL = os.environ.get("SPEC_FIRE_PARTIAL", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_CASCADE_WINDOWS = int(os.environ.get("SPEC_FIRE_CASCADE_WINDOWS", "4"))
SPEC_FIRE_HYBRID = os.environ.get("SPEC_FIRE_HYBRID", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_FACTS_FLAG = os.environ.get("SPEC_FIRE_FACTS_FLAG", "state/cascade_facts.flag")
SPEC_FIRE_FACTS_TTL_SEC = float(os.environ.get("SPEC_FIRE_FACTS_TTL_SEC", "900"))
# Право огня движку в fat-окне выдаёт САМО окно, не тир выше (вскрытие 07.08 17:00, блок
# 49666338). Гонка живёт в одном блоке, а тир (армы>=N/час И свежий факт-флаг) в решающий
# блок ложен ПО ПОСТРОЕНИЮ: армы копятся медленнее часа, факт появляется только ПОСЛЕ чьей-то
# состоявшейся ликвидации. Месяц журнала: тир открылся 10 раз при 198 fail-closed, ноги и
# доска — 0 боевых проб, а позиционную лотерею стены выиграли на плотности (наши пробы легли
# на idx 2439+ при победителях 2176-2187 сразу за транзитом). Бюджет держат гейты
# send_presigned_probe ($-бакет тира, патрон ноги, боевая tx в полёте), токен-бакет движка и
# TTL keepalive (перестали продлевать — потух); fat-окон — 2 за месяц ретенции.
# Откат: SPEC_FIRE_ENGINE_FAT_KEEPALIVE=0 — право огня снова только у тир-гейта.
SPEC_FIRE_ENGINE_FAT_KEEPALIVE = os.environ.get(
    "SPEC_FIRE_ENGINE_FAT_KEEPALIVE", "1").lower() not in ("0", "false", "no")
# ПАКЕТ B — SCHED (план ревьювера 04.08): дыра 18:50:15→39 = 7 проб ушли в XRP-окно ($16k),
# пока рвался DOGE-кит ($71k); fallback 3870d89 починил лок ЦЕЛИ внутри окна, но не расписание
# МЕЖДУ окнами. Пробный слот (общий $-бакет + send-интервал) достаётся окну с самой богатой
# доступной (не на nobuild-скамейке) жертвой; бедное окно уступает тик БЕЗ расхода капа.
# Ничья -> round-robin по временным слотам. Заявка протухает за PRIO_TTL (окно остыло/умерло —
# никто не голодает по мёртвой заявке). pending-handoff арбитра НЕ спрашивает: подтверждённый
# флип главнее богатства. Откат: SPEC_FIRE_SCHED=0 (прежний FCFS окон).
SPEC_FIRE_SCHED = os.environ.get("SPEC_FIRE_SCHED", "1").lower() not in ("0", "false", "no")
SPEC_FIRE_PRIO_TTL_SEC = float(os.environ.get("SPEC_FIRE_PRIO_TTL_SEC", "3.0"))
# ПАКЕТ A — швы pending-detect (chain/pending_detect.py): handoff-флаг живёт FRESH_SEC (протухший
# = детект устарел на блоки, не стреляем вслепую); при ЗДОРОВОМ pending-цикле on-chain пробы
# растягиваются до KEEPALIVE (A3: детект несёт бесплатный pending-ярус, газ проб падает ~7×;
# 3.5с < 4с — B3-гейт «пробел топ-1 < 4с» держится и в кипэлайве). Нездоровый pending (health-
# gate в PendingDetect) -> pending_healthy_fn=False -> полная каденсия сама собой (A6).
PENDING_FIRE_FRESH_SEC = float(os.environ.get("PENDING_FIRE_FRESH_SEC", "2.0"))
PENDING_KEEPALIVE_SEC = float(os.environ.get("PENDING_KEEPALIVE_SEC", "3.5"))
# WS-handoff (разбор 06.08, «го на пендинг детект»): источник детекта для A2 — НЕ полл
# сторонних "pending" (у base.org/nodies/dRPC это алиас latest — ярус-1 слеп до запечатывания,
# 0 handoff за всю жизнь слоя; замер pd_probe 06.08), а СОБСТВЕННАЯ recv-петля flashblocks:
# она уже видит transmit в формируемом блоке (~200мс суб-кадры) и зовёт _process_transmit.
# note_ws_transmit() ставит pending_fire армленному окну с досягаемой доской -> окно стреляет
# ГОТОВОЙ пробой за <=SPEC_FIRE_POLL_SEC, минуя конвейер реактива (гонка №3: победитель бил
# in-block, наша реакция структурно +1 блок). Кулдаун гасит дубли feed/bare/poll-источников
# одного транзита. Откат: PENDING_WS_HANDOFF=0 (см. docs/ROLLBACKS.md).
PENDING_WS_HANDOFF = os.environ.get("PENDING_WS_HANDOFF", "1").lower() not in ("0", "false", "no")
PENDING_WS_CD_SEC = float(os.environ.get("PENDING_WS_CD_SEC", "2.0"))
PENDING_WS_BOARD_MAX_AGE_SEC = float(os.environ.get("PENDING_WS_BOARD_MAX_AGE_SEC", "10.0"))


# канон дробления живёт в chain.execute (общий для проб и fast-lane); ре-экспорт для юнитов
from chain.execute import partial_shares_for_cap  # noqa: E402


def _facts_fresh(now_wall=None):
    """True = факт-сигнал каскада действителен (флаг существует и mtime моложе TTL)."""
    if not SPEC_FIRE_HYBRID:
        return True
    try:
        age = (time.time() if now_wall is None else now_wall) - os.path.getmtime(SPEC_FIRE_FACTS_FLAG)
        return 0 <= age <= SPEC_FIRE_FACTS_TTL_SEC or age < 0   # mtime в будущем = свежий (скью часов)
    except OSError:
        return False
SPEC_FIRE_THRESHOLD = float(os.environ.get("SPEC_FIRE_THRESHOLD", "1.0"))     # ×dev
# Predicted-HF гейт (24.07, ревью + live-данные): цена на транзите ~= текущий Binance mid,
# значит проба имеет смысл ТОЛЬКО если этот транзит способен флипнуть цель:
#   dist_to_flip <= |ret| + MARGIN_DEVS × dev   (аддитивный запас на лаг HF-кэша ~60с,
# USD-approx HF и следующий dev-шаг; НЕ мультипликативный — reach×3 на ret −3% открывал бы
# огонь по цели в 9%, которую ЭТОТ транзит не флипнет). Первый живой день: 6 окон × 10 проб
# по киту dist=8.25% на ret −0.11% — гейт отсёк бы всё ($0.59 и бакет целы).
# Арм-гейт: окно вовсе не стартует, если ближайший жир дальше ARM_MAX_DIST (нет ни петли,
# ни ончейн-фингерпринта по заведомо мёртвым окнам); в настоящий каскад HF-refresh (~60с)
# подтянет dist под потолок — последующие армы окно запустят.
# Откат без кода: SPEC_FIRE_DIST_MARGIN_DEVS=1e9 / SPEC_FIRE_ARM_MAX_DIST=1e9.
# HF цели неизвестен -> fail-open.
SPEC_FIRE_DIST_MARGIN_DEVS = float(os.environ.get("SPEC_FIRE_DIST_MARGIN_DEVS", "2.0"))
SPEC_FIRE_ARM_MAX_DIST = float(os.environ.get("SPEC_FIRE_ARM_MAX_DIST", "0.03"))
# PEAK-OWED fire-policy («го» kelbic 02.08, дизайн ревью): транзит оракула, заряженный на
# пике (|ret| пробил dev), НЕ отменяется отскоком — едет на чейн 22-50с (shadow-леады) и
# садится старой ценой. Старая политика глушила огонь по текущему ret и отдавала такие
# посадки конкурентам («оконная рулетка», гонка cbDOGE 01.08 18:50: пик 18:49, транзит
# +50с). Долг: каждый горячий тик продлевает owed_until на HOLD_SEC и задирает owed_ret до
# пика; серая полоса гистерезиса (|ret| >= thr - OWED_BAND*dev) тоже продлевает — без неё
# дрожание у порога съедало бы таймер («та же рулетка уровнем ниже», ревью). В долге огонь
# живёт с reach по ПИКОВОМУ ret, disarm/falsepos окно не убивают (sticky), confirmed/push/
# stale убивают как раньше. Каждый выстрел, который старая политика пропустила бы, помечен
# [owed] в журнале + stats[owed_probes] — атрибуция бенчмарка, не спор версий.
# Цена: реверты по невыехавшим транзитам (~$0.01/проба), кап проб/бакет прежние.
# Откат: SPEC_FIRE_PEAK_OWED=0 (огонь только по текущему ret, disarm убивает окно).
SPEC_FIRE_PEAK_OWED = os.environ.get("SPEC_FIRE_PEAK_OWED", "1") == "1"
SPEC_FIRE_OWED_HOLD_SEC = float(os.environ.get("SPEC_FIRE_OWED_HOLD_SEC", "75"))
SPEC_FIRE_OWED_BAND = float(os.environ.get("SPEC_FIRE_OWED_BAND", "0.3"))   # доля dev
# Двухфазный каденс (по замеру 24.07: транзит приходит через 22-31с ПОСЛЕ пробития dev —
# OCR-раунд оракула; старый каденс 0.7с выжигал кап за ~7с, за 22с до транзита): первые
# BURST проб быстро (страховка от мгновенного/heartbeat-транзита), дальше медленно — кап 10
# растягивается на ~0.7+8×3.5 ~= 29с, накрывая замеренный лаг. Тюнить по новым транзитам.
SPEC_FIRE_BURST_PROBES = int(os.environ.get("SPEC_FIRE_BURST_PROBES", "2"))
# ХВОСТ 3.5 -> 2.0с и кап 10 -> 24 (27.07, shadow-датасет 871 арм/462 транзита): проба полезна
# ровно в БЛОКЕ транзита, значит ценность = доля покрытых блоков в зоне плотности. Замер
# «первая проба -> транзит» (живые окна): медиана 31с, 6-213с; окна с жиром: мед 40 p75 64с.
# 24×2.0с = ~45с покрытия ОДНОЙ пробой на КАЖДЫЙ блок (было 10×3.5 = 29с и один выстрел на
# ~1.75 блока). Цена $0.29/окно против $0.12 — суточный бакет $10 не замечает (7 окон/19д).
SPEC_FIRE_SLOW_INTERVAL_SEC = float(os.environ.get("SPEC_FIRE_SLOW_INTERVAL_SEC", "2.0"))
# ДЖИТТЕР хвоста: ровное расписание читается конкурентом как подпись слоя (тот же мотив, что
# у WIN-паузы). ±0.3с ломает периодичность почти бесплатно — покрытие блоков не страдает.
SPEC_FIRE_TAIL_JITTER_SEC = float(os.environ.get("SPEC_FIRE_TAIL_JITTER_SEC", "0.3"))
# Пауза после WIN (25.07, ревью-фингерпринт): серия «пробы -> приз» ончейн читается конкурентом
# как учебник нашего слоя. После взятого приза окна не армятся COOLDOWN сек — паттерн рвётся,
# а сам приз уже в кармане (реактивный путь паузой НЕ трогаем, он главный). 0 = выключено.
SPEC_FIRE_WIN_COOLDOWN_SEC = float(os.environ.get("SPEC_FIRE_WIN_COOLDOWN_SEC", "900"))
# #46 (решение владельца 08.08, ревью). Кулдаун победы страховал РЕАКТИВНЫЙ тракт от двойного
# расхода инвентаря и от «палева паттерна проб». Обе премиссы устарели: реакция 0/9794, а пробы
# движка инвентарь не тратят вовсе — страхуемый актив отсутствует, значит страховка чистый
# убыток. Прежнее поведение (любая победа гасит арм ВСЕХ фидов на 900с) в каскаде отдавало
# стене ноги 2+: каждый пуш ре-якорит и дизармит, новой ноге нужен новый arm, а он заблокирован.
#   TRIGGER=combat (дефолт) — взводит только БОЕВАЯ победа с положительным доказательством
#                             захвата; победа пробы (в т.ч. 1-байтовой) кулдаун не ставит.
#   TRIGGER=any             — прежнее «любая победа» (откат).
#   SCOPE=feed (дефолт)     — гасится только фид, чья цель взята; остальные стреляют.
#   SCOPE=global            — прежнее «все фиды» (откат).
# TTL 900с намеренно не трогаем этим пакетом — отдельной задачей после телеметрии.
SPEC_FIRE_COOLDOWN_TRIGGER = os.environ.get("SPEC_FIRE_COOLDOWN_TRIGGER", "combat").strip().lower()
SPEC_FIRE_COOLDOWN_SCOPE = os.environ.get("SPEC_FIRE_COOLDOWN_SCOPE", "feed").strip().lower()
SPEC_FIRE_MIN_PROFIT_BPS = int(os.environ.get("SPEC_FIRE_MIN_PROFIT_BPS", "30"))
SPEC_FIRE_POLL_SEC = float(os.environ.get("SPEC_FIRE_POLL_SEC", "0.15"))
SPEC_FIRE_RECEIPT_TIMEOUT = float(os.environ.get("SPEC_FIRE_RECEIPT_TIMEOUT", "45"))


def _probe_tip_wei(tip_gwei: float) -> int:
    """Tip пробы: min(боевой, кап) — пробы НИКОГДА не наследуют каскадные ступени/эскалацию."""
    return int(min(float(tip_gwei), SPEC_FIRE_TIP_CAP_GWEI) * 1e9)


def _receipt_has_win(r, liquidator_addr: str):
    """Есть ли в квитанции событие Liquidated ОТ НАШЕГО контракта (см. LIQUIDATED_TOPIC0).

    -> True/False, либо None если о логах судить нельзя (поле отсутствует). None НЕ равно False:
    на доске оно означает «не знаю», и вызывающий обязан не объявлять победу, а на пути V1 —
    что вердикт остаётся за статусом. Чужие Liquidated (конкурент в том же блоке) отсекаются
    сверкой адреса эмитента: событие есть у многих, наш контракт — один.
    """
    logs = r.get("logs") if hasattr(r, "get") else None
    if logs is None:
        return None
    want = (liquidator_addr or "").lower()
    for lg in logs:
        try:
            src = lg.get("address") if hasattr(lg, "get") else getattr(lg, "address", None)
            tops = lg.get("topics") if hasattr(lg, "get") else getattr(lg, "topics", None)
            if not tops or str(src or "").lower() != want:
                continue
            t0 = tops[0]
            t0 = t0.hex() if hasattr(t0, "hex") else str(t0)
            if not t0.startswith("0x"):
                t0 = "0x" + t0
            if t0.lower() == LIQUIDATED_TOPIC0:
                return True
        except Exception:
            continue
    return False


class SpecFireLive:
    """Потребитель событий PredictDriver + fire-петля на окно. Все внешние вызовы — через швы."""

    def __init__(self, rpc, cfg, shared, ret_fn, dev_by: dict,
                 build_fn=None, send_fn=None, receipt_fn=None, log_fn=None,
                 now=time.monotonic, sleep=None,
                 volley_accounts_fn=None, volley_send_fn=None):
        self.rpc, self.cfg, self.shared = rpc, cfg, shared
        self.ret_fn = ret_fn                      # label -> текущий return к якорю (None = нет данных)
        self.dev_by = {k.upper(): float(v) for k, v in dev_by.items()}
        self._build_fn = build_fn                 # шов: (mid, borrower, debt_usd) -> {calldata, gas}|None
        self._send_fn = send_fn                   # шов: (calldata, gas) -> hash|None
        self._receipt_fn = receipt_fn             # шов: (hash) -> (status, gas_usd)|None
        self._volley_accounts_fn = volley_accounts_fn   # шов тира: () -> [(addr, key)]
        self._volley_send_fn = volley_send_fn     # шов тира: (rpc,cfg,addr,key,calldata,gas,...) -> hash|None
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._now = now
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._windows: dict = {}                  # feed -> stop Event
        self._armskip_ts: dict = {}               # feed -> ts последнего arm-skip-лога (троттлинг 300с)
        self._cooldown_until = 0.0                # WIN-пауза (SCOPE=global): окна не армим
        self._cooldown_feed: dict = {}            # feed -> ts (SCOPE=feed, дефолт): гаснет только
                                                  # фид, чья цель взята — ноги 2+ каскада живы
        self._arm_ts: list = []                   # ts армленных окон за час (каскад-детект)
        self.stats = {"windows_armed": 0, "probes": 0, "wins": 0,
                      "owed_probes": 0, "sticky_windows": 0,   # peak-owed: would-have-fired учёт
                      "pending_fires": 0,                      # A2: выстрелы по pending-handoff
                      "volley_probes": 0,                      # каскадный тир: ноги залпа (07.08)
                      "lattice_skips": 0,                      # фазовый гейт: тики без огня
                      "lattice_hits": 0, "lattice_misses": 0,  # пуш в фазе / вне фазы (живой валидатор)
                      "board_arms": 0, "board_probes": 0,      # v5 доска: записей плана / 1-байт проб
                      "board_fails": 0}                        # арм не удался -> ноги ушли на V1
        self._push_ts: dict = {}                  # feed -> метки севших пушей (решётка OCR, 07.08)
        self._board: dict = {}                    # v5 доска: {"key": (mid,b), "until": ts, "armed_ts": ts}
        self._board_lock = threading.Lock()       # арм доски — одна запись за раз (nonce главного)
        self._board_arm_fn = None                 # шов юнитов: (calldata, gas, tag) -> hash|None
        self._owed: dict = {}                     # feed -> owed_until (peak-owed, пишет петля окна)
        self._prio: dict = {}                     # feed -> (claim_debt, ts): заявки на слот (пакет B)
        self.probe_board: dict = {}               # feed -> (mid, b, debt, calldata, gas, ts): свежая
                                                  # проба окна для яруса-2 pending (читает PendingDetect)
        self.pending_fire: dict = {}              # feed -> (mid, b, debt, ts): атомарный handoff (A2)
        self.pending_healthy_fn = None            # () -> bool: pending-цикл жив (A3-кипэлайв)
        self._ws_handoff_ts: dict = {}            # feed -> ts последнего WS-handoff (кулдаун дублей)
        self._alert = None                        # (text, key) -> None: канал к ЧЕЛОВЕКУ. Ставится
                                                  # снаружи; нужен там, где состояние само не
                                                  # рассосётся (немота ног по комиссии)
        self._board_probe_cache: dict = {}        # (mid,b) -> (debt, calldata, gas): проба под
                                                  # живым планом, чтобы окна 2..N публиковали
                                                  # доску без пересборки (HIGH N1)
        self._board_next_try: dict = {}           # feed -> ts следующей попытки арма (#57): осечка
                                                  # арма обязана перепробовать БЫСТРО, а не через
                                                  # каденс переарма — окно короче ретрая иначе
                                                  # остаётся без доски, и весь флот нем

    def note_ws_transmit(self, mids, src="ws"):
        """WS-handoff (06.08): recv-петля увидела transmit в ФОРМИРУЕМОМ блоке — если он бьёт
        по рынку доски армленного окна, ставим pending_fire (A2, тот же потребитель в петле
        окна, единый nonce). O(1)-словарные операции, зовётся с нити _process_transmit (не
        recv). mids = рынки транслирующего агрегатора. Возврат True = handoff поставлен."""
        if not PENDING_WS_HANDOFF or not mids:
            return False
        n = self._now()
        hit = False
        mids_l = {str(m).lower() for m in mids}
        for feed in list(self._windows):
            board = self.probe_board.get(feed)
            if not board:
                continue
            mid, b, debt = board[0], board[1], board[2]
            if mid.lower() not in mids_l or n - board[5] > PENDING_WS_BOARD_MAX_AGE_SEC:
                continue
            if n - self._ws_handoff_ts.get(feed, -1e9) < PENDING_WS_CD_SEC:
                continue
            self._ws_handoff_ts[feed] = n
            self.pending_fire[feed] = (mid, b, debt, n)
            hit = True
            self._log("SPECFIRE ws-handoff feed=%s %s/%s src=%s — транзит армленного фида "
                      "в формируемом блоке, огонь окну" % (feed, mid[:10], b[:10], src))
        return hit

    # ---- боевые швы ----
    def _build(self, mid, borrower, debt_usd):
        from chain.multicall import (aggregate3, encode_id_to_market_params_call,
            decode_id_to_market_params, encode_market_call, decode_market,
            encode_position_call, decode_position, encode_price_call, decode_price)
        from chain.simulate import to_assets_up
        from strategy.pnl import lif_from_lltv
        from chain.execute import (expected_seized, direct_swap, encode_liquidate,
                                   _MORPHO_BLUE)
        from chain.prearm import prearm_get
        m32 = self.rpc.to_bytes32(mid)
        r1 = aggregate3(self.rpc, [(_MORPHO_BLUE, encode_id_to_market_params_call(m32)),
                                   (_MORPHO_BLUE, encode_market_call(m32)),
                                   (_MORPHO_BLUE, encode_position_call(m32, borrower))])
        loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(r1[0][1])
        mk = decode_market(r1[1][1]); tba, tbs = mk[2], mk[3]
        bs, coll_amt = decode_position(r1[2][1])
        if bs == 0:
            return "gone"                         # позиция разобрана (конкурент/погашение) — LOSS-стоп
        price = decode_price(aggregate3(self.rpc, [(oracle, encode_price_call())])[0][1])
        repaid_shares = int(bs)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = expected_seized(repaid_assets, lif_from_lltv(lltv_wad / 10**18), price)
        if seized == 0:
            return None
        liq = self.cfg.liquidator_address
        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle,
              "irm": irm, "lltv": lltv_wad}
        fl_max = float(getattr(self.cfg, "fast_lane_max_repaid_usd", 500000.0) or 500000.0)
        swap = None
        swap_meta = None                          # consume-хук (05.08): только direct-сайз списывает кап
        if debt_usd >= fl_max:                    # мега: обрыв одиночного пула -> сперва PREARM (Kyber)
            hit = prearm_get(coll, loan, int(seized))
            if hit is not None:
                swap = hit["swap"]
        if swap is None:
            swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
            if swap is not None:
                swap_meta = (coll, loan, int(seized))
        if swap is None:
            hit = prearm_get(coll, loan, int(seized))
            swap = hit["swap"] if hit is not None else None
        if swap is None and SPEC_FIRE_PARTIAL:
            from chain.execute import route_max_in
            cap = route_max_in(coll, loan)        # дробление (01.08): сейз сверх ёмкости пула
            sp = partial_shares_for_cap(repaid_shares, seized, cap) if cap is not None else 0
            if sp > 0:
                ap = to_assets_up(sp, tba, tbs)
                zp = expected_seized(ap, lif_from_lltv(lltv_wad / 10**18), price)
                sw = direct_swap(coll, loan, zp, liq, liq, repaid_assets=ap)
                if sw is not None:
                    self._log("SPECFIRE probe partial %s/%s: сейз %d > кап %d — режем до "
                              "%d shares" % (mid[:10], borrower[:10], seized, cap, sp))
                    repaid_shares, repaid_assets, seized, swap = sp, ap, zp, sw
                    swap_meta = (coll, loan, int(zp))
        if swap is None:
            return None                           # ни роута, ни прогретой квоты — пробу НЕ строим
        min_profit = int(repaid_assets) * SPEC_FIRE_MIN_PROFIT_BPS // 10000
        calldata = encode_liquidate(mp, borrower, repaid_shares, swap["router"],
                                    swap["calldata"], min_profit)
        # v5 доска: сырые компоненты того же выстрела + порог цены оракула-обёртки.
        # Morpho здоров, пока collateral*price*lltv >= borrowed*1e36*1e18, значит граница
        # price = borrowed*1e54/(collateral*lltv). Долг РАСТЁТ с начислением, т.е. настоящая
        # граница со временем ползёт ВВЕРХ — замороженный порог занижается и начал бы гасить
        # огонь (fail-closed). Поэтому запас: фильтр чуть свободнее правды, лишняя проба
        # дешевле пропущенного приза.
        trigger = 0
        try:
            if coll_amt and lltv_wad:
                _b = int(repaid_assets) * 10**54 // (int(coll_amt) * int(lltv_wad))
                trigger = _b * (10000 + SPEC_FIRE_V2_TRIGGER_MARGIN_BPS) // 10000
        except Exception:
            trigger = 0                           # не посчитали -> 0 = фильтра нет (fail-open)
        return {"calldata": calldata, "gas": int(self.cfg.gas_limit_est * 125 // 100),
                "swap_meta": swap_meta,
                "plan": {"mp": mp, "borrower": borrower, "repaid_shares": repaid_shares,
                         "router": swap["router"], "swap_calldata": swap["calldata"],
                         "min_profit": min_profit, "trigger": int(trigger)}}

    def _consume_cap(self, spent, key, probe):
        """Consume-хук (05.08): ПЕРВАЯ успешная отправка цели списывает её direct-сайз из
        живого капа — резерв «для других» строек (сим каскада 04.08: B ревертил minOut в
        осушенном A пуле). Повторные пробы той же цели не списывают повторно: healthy-реверты
        пул не трогают, а плотный каденс 0.5с обрушил бы кап в ноль за секунды."""
        try:
            sm = probe.get("swap_meta") if isinstance(probe, dict) else None
            if not sm or key in spent:
                return
            from chain.execute import consume_live_cap
            nc = consume_live_cap(sm[0], sm[1], sm[2])
            if nc is not None:
                spent[key] = sm
                self._log("SPECFIRE LIVECAP consume %s->%s: -%d -> %d (%s/%s)" % (
                    sm[0][:8], sm[1][:8], sm[2], nc, key[0][:10], key[1][:10]))
        except Exception:
            pass

    @staticmethod
    def _release_cap(spent, key):
        """Возврат своего резерва перед пересборкой той же цели: свой расход не должен резать
        свой же сайз до партиала. После отправки новой пробы резерв берётся заново."""
        res = spent.pop(key, None)
        if res is not None:
            try:
                from chain.execute import release_live_cap
                release_live_cap(*res)
            except Exception:
                pass

    def _send(self, calldata, gas):
        from chain.execute import send_spec_fire_probe
        from chain.hotpath import _fresh_basefee
        fb = _fresh_basefee()
        tip = _probe_tip_wei(self.cfg.tip_gwei)
        seq = str(getattr(self.cfg, "sequencer_rpc", "") or "")
        bcast = [seq] if (seq and seq.lower() not in ("off", "none", "0")) else None
        return send_spec_fire_probe(self.rpc, self.cfg, calldata, gas, log=log,
                                    max_fee=(fb * 2 + tip) if fb is not None else None, tip=tip,
                                    broadcast_raw_urls=bcast)

    def _receipt(self, h):
        """Ручной полл вместо wait_for_transaction_receipt: его дефолтный poll 0.1с при плотном
        залпе (6-8 сборщиков параллельно) = 60+ req/с в RPC. Раз в RECEIPT_POLL_SEC достаточно —
        Base подтверждает за 2-4с, таймаут ловит только дропы."""
        w3 = self.rpc._web3()
        deadline = self._now() + SPEC_FIRE_RECEIPT_TIMEOUT
        while self._now() < deadline:
            r = None
            try:
                r = w3.eth.get_transaction_receipt(h)
            except Exception:
                pass                              # not found / транспорт — ждём следующий полл
            if r is not None:
                gas_usd = (int(r.get("gasUsed") or 0) * int(r.get("effectiveGasPrice") or 0)
                           * float(self.cfg.eth_price_usd) / 1e18)
                # blk/idx — метрика гонки плотностей: позиция пробы в очереди против
                # транзита/победителя снимается прямо из журнала (ревью 31.07)
                return (int(r.get("status") or 0), gas_usd,
                        r.get("blockNumber"), r.get("transactionIndex"),
                        _receipt_has_win(r, self.cfg.liquidator_address))
            self._sleep(SPEC_FIRE_RECEIPT_POLL_SEC)
        return None

    # ---- выбор жирной цели (top-1 по долгу; та же фид/направление-семантика) ----
    # max_dist (доля, напр. 0.0033): reach-гейт — пропускать цели, чей флип дальше, чем
    # достаёт текущее движение (down: цена залога должна упасть на 1-1/HF; up: долг вырасти
    # на HF-1). Применяется ТОЛЬКО в момент огня — арм окна без гейта, иначе в настоящий
    # каскад окно не запустится вовсе (на арме ret всегда мал по построению).
    def _top_fat(self, feed: str, direction: str, max_dist=None, k=1):
        """-> (top-k список [(mid, b, debt)] по долгу, hold_dist, edge) — hold_dist =
        ближайшая отсечённая гейтом цель; edge = ближайшая-по-dist ДОСЯГАЕМАЯ цель
        (кандидат edge-слота ротации; None = dist ни у кого не вычислим). Пустой
        список = досягаемого жира нет."""
        elig = []
        hold_dist = None
        edge_d, edge_t = None, None
        with self.shared.lock:
            groups = dict(self.shared.groups)
            debt_by = dict(self.shared.debt_by)
            by_id = dict(self.shared.by_id)
            hf_by = dict(getattr(self.shared, "hf_by", {}) or {})
        for mid, borrowers in groups.items():
            m = by_id.get(mid)
            if m is None:
                continue
            cl = _label_of(getattr(m, "collateral_token", ""))
            ll = _label_of(getattr(m, "loan_token", ""))
            if cl == ll or not ((direction == "down" and cl == feed) or (direction == "up" and ll == feed)):
                continue
            for b in borrowers:
                d = float(debt_by.get((mid, b), 0.0) or 0.0)
                if d < SPEC_FIRE_MIN_REPAID_USD:
                    continue
                dist = None
                if max_dist is not None:
                    hf = float(hf_by.get((mid, b), 0.0) or 0.0)
                    if hf > 1.0:                  # HF неизвестен/битый -> fail-open
                        dist = (1.0 - 1.0 / hf) if direction == "down" else (hf - 1.0)
                        if dist > max_dist:
                            if hold_dist is None or dist < hold_dist:
                                hold_dist = dist
                            continue
                    elif 0.0 < hf <= 1.0:         # уже за флипом — ближе кромки не бывает
                        dist = 0.0
                elig.append((mid, b, d))
                if dist is not None and (edge_d is None or dist < edge_d):
                    edge_d, edge_t = dist, (mid, b, d)
        elig.sort(key=lambda t: -t[2])
        return elig[:k], hold_dist, edge_t

    def _bonus_of(self, mid: str) -> float:
        """LIF-1 рынка из covered-снапшота (гейт по призу); 0.0 = неизвестен -> долг-гейт."""
        with self.shared.lock:
            m = self.shared.by_id.get(mid)
        return float(getattr(m, "bonus", 0.0) or 0.0)

    def _prio_ok(self, feed: str, claim: float) -> bool:
        """Пакет B (B1): True = слот наш — нет более богатой СВЕЖЕЙ заявки другого окна.
        Ничья -> round-robin по временным слотам DENSE-интервала (детерминированно, без
        координации). Заявки пишутся окнами GIL-атомарными присваиваниями (без лока —
        ноль общих локов с fire-путём, инвариант A4)."""
        if not SPEC_FIRE_SCHED:
            return True
        n = self._now()
        fresh = {f: d for f, (d, ts) in list(self._prio.items()) if n - ts <= SPEC_FIRE_PRIO_TTL_SEC}
        fresh[feed] = claim
        mx = max(fresh.values())
        if claim < mx:
            return False
        tied = sorted(f for f, d in fresh.items() if d >= mx)
        if len(tied) <= 1:
            return True
        return tied[int(n / max(0.25, SPEC_FIRE_DENSE_INTERVAL_SEC)) % len(tied)] == feed

    def _collect(self, h, mid, borrower, debt_usd, feed=""):
        from chain.execute import specfire_gas_add, mark_recent_send, canary_invalidate_nonce
        res = (self._receipt_fn or self._receipt)(h)
        if res is None:
            canary_invalidate_nonce(self.rpc, self.cfg)   # дроп? nonce вперёд чейна -> инвалидация
            self._log("SPECFIRE probe NO-RECEIPT tx=%s (nonce инвалидирован)" % h)
            return
        status, gas_usd = res[0], res[1]
        pos = (" blk=%s idx=%s" % (res[2], res[3])) if len(res) >= 4 and res[2] is not None else ""
        specfire_gas_add(gas_usd)
        if status == 1:
            self.stats["wins"] += 1
            mark_recent_send(mid, borrower)               # жертва взята — глушим реактивный дубль
            self._set_cooldown(feed, "probe", proven=True)   # #46: скоуп/триггер — ручками env
            if feed:                                      # приз взят — окно не жжёт кап ревертами
                with self._lock:
                    stop = self._windows.pop(feed, None)
                if stop is not None:
                    stop.set()
            log.warning("SPECFIRE WIN!!! %s/%s debt=$%.0fk tx=%s gas=$%.2f%s — предочередь взята "
                        "(окна на паузе %.0fс)",
                        mid[:10], borrower[:10], debt_usd / 1e3, h, gas_usd, pos,
                        SPEC_FIRE_WIN_COOLDOWN_SEC)
        else:
            self._log("SPECFIRE probe revert (healthy, ожидаемо) tx=%s gas=$%.3f%s" % (h, gas_usd, pos))

    # ---- каскадный тир (07.08): залп той же пробой с залповых EOA ----
    def _cascade_active(self) -> bool:
        """Каскад-флаг тира: армы за час >= порога И свежий факт-флаг — ровно тот же гибрид,
        что у бустера бакета (армы = надежда, факты = подтверждение; калибровка 31.07)."""
        n = self._now()
        return (len([t for t in self._arm_ts if n - t < 3600.0]) >= SPEC_FIRE_CASCADE_WINDOWS
                and _facts_fresh())

    def note_push(self, feed, t=None, updated_at=None):
        """Отметить севший оракульный апдейт: буфер меток кормит фазовый гейт И его же
        онлайн-валидатор. Дедуп 5с — confirmed и push об одном апдейте приходят парой.

        #49 (ревью 08.08): метка обязана стоять на времени ОРАКУЛА (`updatedAt`), а не на
        моменте, когда её увидел наш полл. Полл ходит раз в 4с при толерансе фазы ±2с — метки
        были квантованы грубее самого гейта: доля гэпов на решётке падала до ~0.72 против
        порога 0.80 (фича молча мертва), а при флуктуационном подтверждении фаза уезжала до
        4с и `lattice_skips` глушили огонь в истинную секунду транзита. Хуже всего, что
        валидатор hits/misses мерил теми же смещёнными часами и выглядел здоровым.
        Переводим unix-время оракула в монотонный кадр буфера — сравниваются только разности,
        поэтому кадр обязан быть ОДИН."""
        t = self._now() if t is None else float(t)
        if updated_at:
            try:
                _skew = time.time() - float(updated_at)     # возраст апдейта на момент детекта
            except Exception:
                _skew = None
            if _skew is not None and 0.0 <= _skew <= SPEC_FIRE_LATTICE_MAX_SKEW_SEC:
                t = t - _skew                               # момент САМОГО апдейта
                self.stats["lattice_oracle_ts"] = self.stats.get("lattice_oracle_ts", 0) + 1
            else:
                # часы узла/оракула разъехались — честно живём на времени детекта и говорим
                # об этом: молчаливый фолбэк вернул бы ровно тот дефект, что чиним
                self.stats["lattice_skew_bad"] = self.stats.get("lattice_skew_bad", 0) + 1
        ts = self._push_ts.setdefault(feed, [])
        if ts and t - ts[-1] < 5.0:
            return
        if ts and self._lattice_ready(feed):
            # был ли гейт открыт В СЕКУНДУ ПУША — считаем ДО добавления метки. Это и есть
            # закрытие предела «замер на пуш, а не на приз»: счётчик живой и наш.
            key = "lattice_hits" if self._gate_open(feed, now=t) else "lattice_misses"
            self.stats[key] = self.stats.get(key, 0) + 1
            if key == "lattice_misses":
                self._log("SPECFIRE решётка: пуш feed=%s ВНЕ фазы (+%.1fс от узла) — "
                          "попадания %d, промахи %d" % (
                              feed, (t - ts[-1]) % SPEC_FIRE_LATTICE_P,
                              self.stats.get("lattice_hits", 0),
                              self.stats.get("lattice_misses", 0)))
        ts.append(t)
        if len(ts) > 64:
            del ts[:-64]

    def _lattice_frac(self, feed) -> float:
        """Доля гэпов, легших на решётку (±TOL). Считаем ТОЛЬКО по гэпам в пределах горизонта:
        за MAX_AGE фаза уплывает, и такие гэпы не свидетельствуют ни за, ни против."""
        ts = self._push_ts.get(feed) or []
        gaps = [b - a for a, b in zip(ts, ts[1:]) if 0 < b - a <= SPEC_FIRE_LATTICE_MAX_AGE]
        if len(gaps) < SPEC_FIRE_LATTICE_MIN_N:
            return -1.0
        ok = 0
        for g in gaps:
            ph = g % SPEC_FIRE_LATTICE_P
            if min(ph, SPEC_FIRE_LATTICE_P - ph) <= SPEC_FIRE_LATTICE_TOL:
                ok += 1
        return ok / float(len(gaps))

    def _lattice_ready(self, feed) -> bool:
        """Решётка подтверждена НАШИМ потоком: хватает гэпов И доля выше порога. Пока не
        подтверждена — гейт не действует (fail-open), но счётчики уже не считаются."""
        return self._lattice_frac(feed) >= SPEC_FIRE_LATTICE_MIN_FRAC

    def _gate_open(self, feed, now=None) -> bool:
        """Открыт ли фазовый слот. Любое сомнение -> True (стреляем как раньше)."""
        if not SPEC_FIRE_LATTICE_GATE:
            return True
        ts = self._push_ts.get(feed) or []
        if not ts or not self._lattice_ready(feed):
            return True                                   # решётка не доказана нашим потоком
        age = (self._now() if now is None else now) - ts[-1]
        if age < 0 or age > SPEC_FIRE_LATTICE_MAX_AGE:
            return True                                   # фаза уплыла — гейт снимается
        ph = age % SPEC_FIRE_LATTICE_P
        return min(ph, SPEC_FIRE_LATTICE_P - ph) <= SPEC_FIRE_LATTICE_TOL

    def _board_ready(self, mid, b) -> bool:
        """Доска армлена ИМЕННО под эту цель и ещё жива? Только тогда 1-байт проба осмысленна."""
        if not SPEC_FIRE_V2_BOARD:
            return False
        st = self._board
        return bool(st and st.get("key") == (mid, b)
                    and self._now() < st.get("until", 0.0))

    def _arm_board(self, probe, mid, b, feed, debt=0.0) -> bool:
        """Записать armed-план в контракт (owner-tx с главного кошелька). Возврат: доска готова?

        Вызывается ЗАРАНЕЕ (пока окно взведено, но горячая фаза не началась) — плану надо сесть
        в блок ДО того, как полетят однобайтовые ноги, иначе они бьют в пустую доску.
        Идемпотентно: если та же цель уже армлена и не протухла — молчим. Любая осечка = False,
        и залп уходит по V1 с полной калдатой (деградация, а не отказ от огня)."""
        if not SPEC_FIRE_V2_BOARD or not isinstance(probe, dict):
            return False
        plan = probe.get("plan")
        if not plan:
            return False
        if not self._board_lock.acquire(blocking=False):
            return self._board_ready(mid, b)      # другой поток уже пишет — не толкаемся
        try:
            if self._board_ready(mid, b):
                st = self._board
                if self._now() - st.get("armed_ts", 0.0) < SPEC_FIRE_V2_REARM_SEC:
                    return True                   # свежая и та же цель — переписывать нечего
            import chain.execute as _ex
            dl = int(time.time()) + SPEC_FIRE_V2_PLAN_TTL_SEC
            cd = _ex.encode_set_plan(
                plan["mp"], plan["borrower"], plan["repaid_shares"], plan["router"],
                plan["swap_calldata"], plan["min_profit"], dl, plan["trigger"])
            # газ: запись плана пишет KB-скейл роут в сторадж — считаем по длине калдаты,
            # 22 газа/байт с запасом (16 калдата + ~6 на слово стораджа) плюс постоянная часть
            gas = 120_000 + 22 * (len(cd) // 2)
            h = (self._board_arm_fn or (lambda c, g, tag: _ex.send_board_tx(
                self.rpc, self.cfg, c, g, log=log, tag=tag)))(cd, gas, "setPlan")
            if h is None:
                self.stats["board_fails"] += 1
                return False
            self._board = {"key": (mid, b), "armed_ts": self._now(), "debt": float(debt),
                           # доска считается годной только ПОСЛЕ того, как tx могла сесть;
                           # до этого 1-байт пробы ушли бы в пустоту (плана в сторадже нет)
                           "until": self._now() + SPEC_FIRE_V2_PLAN_TTL_SEC - 30.0,
                           "ready_at": self._now() + SPEC_FIRE_V2_BOARD_SETTLE_SEC}
            self.stats["board_arms"] += 1
            self._log("SPECFIRE доска armed feed=%s %s/%s trigger=%s TTL=%dс tx=%s" % (
                feed, mid[:10], b[:10],
                ("%.4g" % (plan["trigger"] / 1e36)) if plan["trigger"] else "нет(fail-open)",
                SPEC_FIRE_V2_PLAN_TTL_SEC, h))
            return True
        except Exception as e:
            self.stats["board_fails"] += 1
            self._log("SPECFIRE доска arm fail feed=%s: %s" % (feed, _diag(e)))
            return False
        finally:
            self._board_lock.release()

    def _board_live(self, mid, b) -> bool:
        """Доска армлена, ПРОТУХНУТЬ не успела И запись уже могла сесть в блок."""
        st = self._board
        return bool(self._board_ready(mid, b) and self._now() >= st.get("ready_at", 0.0))

    def _board_plan_live(self):
        """(mid, b) ЖИВОГО армленного плана (запись успела сесть) или None.

        Ключ права огня движку: keepalive ведётся по цели ПЛАНА, а не по цели текущего тика —
        handoff 07.08 пришёл по $1k-жертве, пока доска держала план по $31k, и проверка по
        цели тика оставила бы движок немым ровно в блоке транзита."""
        k = (self._board or {}).get("key")
        return k if (k is not None and self._board_live(k[0], k[1])) else None

    def _engine_fat_keepalive(self, feed) -> bool:
        """Право огня движку от САМОГО fat-окна (см. SPEC_FIRE_ENGINE_FAT_KEEPALIVE).
        Возврат True = продлили. Зовётся из тика окна и из pending-handoff: огонь ведёт ОКНО,
        а не удача кадансовой пробы — гейт _INFLIGHT глушит пробу главного кошелька на время
        боевой tx, и keepalive из залпа погас бы ровно в горячие секунды каскада."""
        if not SPEC_FIRE_ENGINE_FAT_KEEPALIVE:
            return False
        plan = self._board_plan_live()
        if plan is None:
            return False
        eng = self._engine()
        if eng is None:
            return False
        eng.keepalive(target=(feed, plan[0], plan[1], (self._board or {}).get("debt", 0.0)))
        n = self._now()
        if n - getattr(self, "_fat_ka_logged", 0.0) > 60.0:   # DIAG раз в минуту, не на тик
            self._fat_ka_logged = n
            self._log("SPECFIRE движок: право огня от fat-окна feed=%s план=%s/%s" % (
                feed, str(plan[0])[:10], str(plan[1])[:10]))
        return True

    def _prearm_board(self, feed, direction, dev):
        """Положить план по самой жирной достижимой цели ЗАРАНЕЕ — пока окно взведено, но
        горячая фаза не началась. Именно ради этого запаса времени доска и существует: запись
        успевает сесть, и к моменту огня ноги залпа уже несут 1 байт вместо полной калдаты.
        Крутится в своём треде — петля окна не ждёт RPC сборки."""
        try:
            reach = dev * (SPEC_FIRE_THRESHOLD + SPEC_FIRE_DIST_MARGIN_DEVS)
            tgts, _hold, _edge = self._top_fat(feed, direction, max_dist=reach, k=1)
            if not tgts:
                return                             # жира в досягаемости нет — план ни к чему
            mid, b, d = tgts[0]
            if self._board_ready(mid, b):
                # HIGH N1 (ревью 09.08): ранний выход стоял ДО публикации probe_board, и после
                # фикса #57 он стал ЖИВЫМ путём — окна 2..N каскада доходят сюда гарантированно
                # (план жив по TTL ~870с ⇒ доска «готова» ⇒ публикация пропущена), а запись
                # первого окна снял его же finally. Освежение отметки раз в 4с не спасает: оно
                # обновляет СУЩЕСТВУЮЩУЮ запись, а её нет. Итог был бы прежний: ws-handoff и
                # pending-tier2 выбрасывают уже пойманный транзит — ровно в окнах, ради которых
                # чинили кулдаун. Публикуем из кэша пробы (сборка не нужна, setPlan не уходит).
                _c = self._board_probe_cache.get((mid, b))
                if _c is not None:
                    self.probe_board[feed] = (mid, b, _c[0], _c[1], _c[2], self._now())
                    self._engine()          # склад и прогрев патрона нужны и здесь
                    return
                # кэша нет (рестарт под живым планом) — НЕ выходим: собираем пробу ради
                # публикации; повторного setPlan не будет, дедуп стоит внутри _arm_board.
            probe = (self._build_fn or self._build)(mid, b, d)
            if not isinstance(probe, dict):
                self._arm_retry(feed, "проба не собралась")
            if isinstance(probe, dict):
                if not self._arm_board(probe, mid, b, feed, debt=d):
                    self._arm_retry(feed, "setPlan не подтвердился")
                # #47: ПУБЛИКУЕМ probe_board уже на прогреве. Раньше он заполнялся ТОЛЬКО при
                # пересборке в горячей фазе (|ret|>=1.0*dev), а pending-detect tier-2 и
                # ws-handoff при пустой доске делают return, ВЫБРАСЫВАЯ уже обнаруженный
                # транзит: в классе каскадов, где оракульный базис держит наш ret ниже порога
                # (cbXRP: оракул -0.50% при ret -0.43%), окно молчало сквозь реальный транзит.
                # Прогрев строит ровно тот же probe-dict — публикация здесь открывает оба
                # детект-пути, не трогая порог огня.
                self.probe_board[feed] = (mid, b, d, probe["calldata"], probe["gas"], self._now())
                # кэш пробы под живой план: из него публикуются окна 2..N, не платя за сборку
                self._board_probe_cache = {(mid, b): (d, probe["calldata"], probe["gas"])}
                # СКЛАД НАБИВАЕТСЯ ЗДЕСЬ, а не в первом огне. Движок поднимается лениво, и если
                # ждать первого тика горячей фазы, пул выйдет на пустые лестницы: подписать
                # 5 ног x 40 проб — это ~0.8с молчания ровно на старте окна победы. Арм доски —
                # и есть то самое спокойное время, ради запаса которого доска существует:
                # поднимаем движок вместе с планом, и фоновое пополнение наполняет склад,
                # пока горячая фаза ещё не началась. Стрелять он всё равно не может — права
                # на огонь даёт только keepalive из горячего тика.
                self._engine()
        except Exception as e:
            self._arm_retry(feed, _diag(e))
            self._log("SPECFIRE доска prearm fail feed=%s: %s" % (feed, _diag(e)))

    def _set_cooldown(self, feed, src, proven=False):
        """WIN-пауза по решению владельца (#46): триггер и скоуп — ручками env.

        src: "combat" (наша боевая tx) | "probe" (спек/движок/залп). proven=True означает
        положительное доказательство захвата (Liquidated/repaidAssets>0), а НЕ «status==1» и
        не «uncertain»: фантомная победа, гасящая нас посреди живой возможности, — тот же класс,
        что мёртвый сторож. Возврат: пауза взведена?"""
        if SPEC_FIRE_COOLDOWN_TRIGGER == "combat" and not (src == "combat" and proven):
            return False                          # победа пробы инвентарь не тратит — не гасим
        if SPEC_FIRE_COOLDOWN_TRIGGER != "any" and not proven:
            return False                          # без доказательства захвата паузу не ставим
        until = self._now() + SPEC_FIRE_WIN_COOLDOWN_SEC
        if SPEC_FIRE_COOLDOWN_SCOPE == "global":
            self._cooldown_until = until
        elif feed:
            self._cooldown_feed[feed] = until
        else:
            return False                          # пер-фид без фида гасить нечего
        self._log("SPECFIRE WIN-пауза %s: скоуп=%s src=%s на %.0fс" % (
            feed or "—", SPEC_FIRE_COOLDOWN_SCOPE, src, SPEC_FIRE_WIN_COOLDOWN_SEC))
        return True

    def _cooldown_active(self, feed) -> bool:
        n = self._now()
        if n < self._cooldown_until:              # глобальная пауза (SCOPE=global / откат)
            return True
        return n < self._cooldown_feed.get(feed, 0.0)

    def note_combat_win(self, feed=None, proven=False):
        """Точка входа боевой победы (#46): зовётся из разбора квитанции БОЕВОЙ tx, когда захват
        подтверждён (repaidAssets>0). proven=False (например, SEND-3 uncertain) паузу НЕ ставит —
        иначе фантом глушил бы нас посреди живой возможности."""
        return self._set_cooldown(feed, "combat", proven=proven)

    def _arm_retry(self, feed, why):
        """#57: осечка арма = быстрый переспрос, а не ожидание каденса 240с. Доска — единственная
        точка входа флота: без севшего плана движок не получает права огня вовсе, а V1-деградация
        сидит за тир-гейтом, ложным в решающий блок. Молчаливая осечка = немой флот при здоровом
        на вид движке, поэтому она ещё и громкая (счётчик виден в close-строке окна)."""
        self._board_next_try[feed] = self._now() + SPEC_FIRE_V2_ARM_RETRY_SEC
        self.stats["board_arm_fails"] = self.stats.get("board_arm_fails", 0) + 1
        self._log("SPECFIRE доска: арм НЕ ВЗВЁЛСЯ feed=%s (%s) — переспрос через %.0fс "
                  "(без доски флот нем)" % (feed, why, SPEC_FIRE_V2_ARM_RETRY_SEC))

    # ---- движок огня (#32, 07.08): постоянный пул вместо порождения залпа на тик ----
    def _engine(self):
        """Ленивый подъём движка. Выключен -> None, и путь залпа идёт как прежде бит-в-бит."""
        from chain import fire_engine as _fe
        if not _fe.ENGINE_ON:
            return None
        eng = getattr(self, "_fire_engine", None)
        if eng is not None:
            return eng
        with self._lock:
            eng = getattr(self, "_fire_engine", None)
            if eng is not None:
                return eng
            from chain import execute as _ex
            seq = str(getattr(self.cfg, "sequencer_rpc", "") or "")
            bcast = [seq] if (seq and seq.lower() not in ("off", "none", "0")) else None
            rpc_url = getattr(self.cfg, "rpc_url", None) or seq

            def _legs():
                return [a for a, _k in (self._volley_accounts_fn or _ex.volley_accounts)()]

            def _send(addr):
                return _ex.send_presigned_probe(self.rpc, self.cfg, addr, log=log,
                                                broadcast_raw_urls=bcast)

            def _refill(addr, base_fee=None):
                for a, k in (self._volley_accounts_fn or _ex.volley_accounts)():
                    if a == addr:
                        _ex.presign_ladder(self.rpc, self.cfg, addr, k,
                                           SPEC_FIRE_V2_FIRE_GAS, base_fee=base_fee, log=log)
                        return

            def _audit(addrs):
                """#63: аудит ЦЕНЫ лестниц. База читается ОДИН раз на весь флот (иначе это
                110 запросов вместо одного — урок квоты 09.08), подпись жжём только у ног,
                чья лестница действительно протухла: вздута (гейт патрона закрыт) либо ниже
                нынешней базы (tx не включаема). Обе формы САМИ останавливают огонь, поэтому
                триггер по глубине склада тут не срабатывает никогда."""
                try:
                    bf = int(self.rpc._web3().eth.get_block("latest").get("baseFeePerGas") or 0)
                except Exception as e:
                    self._log("SPECFIRE аудит лестниц: база не прочитана (%s)" % _diag(e))
                    return
                tip = int(float(getattr(self.cfg, "tip_gwei", 0.05)) * 1e9)
                stale = [a for a in (addrs or []) if _ex.ladder_resign_needed(a, bf, tip)]
                if not stale:
                    return
                self._log("SPECFIRE аудит лестниц: %d ног с протухшей ценой (база %.4f gwei) — "
                          "пересборка" % (len(stale), bf / 1e9))
                for a in stale:
                    try:
                        _ex.invalidate_ladder(a)      # иначе compat удержит старую цену
                        _refill(a, base_fee=bf)
                    except Exception as e:
                        self._log("SPECFIRE аудит %s: %s" % (a[:10], _diag(e)))

            col = _fe.ReceiptCollector(
                receipts_fn=lambda hs: _ex.batch_receipts(rpc_url, hs),
                on_result=self._engine_result, on_timeout=self._engine_timeout,
                log=self._log)
            eng = _fe.FireEngine(legs_fn=_legs, send_fn=_send, collector=col,
                                 refill_fn=_refill, depth_fn=_ex.ladder_depth,
                                 fleet_warm_fn=lambda addrs: _ex.volley_bal_warm_fleet(
                                     self.rpc, addrs, log=log),
                                 audit_fn=_audit, log=self._log)
            self._fire_engine = eng
            eng.start()
            return eng

    def _engine_result(self, meta, h, r):
        """Исход пробы движка. Вердикт победы — ТОЛЬКО по событию Liquidated (см. LIQUIDATED_TOPIC0):
        у пробы доски status=1 бывает и у холостой, и у неудачной попытки."""
        from chain.execute import volley_gas_add, volley_bal_charge
        addr, tgt = (meta or (None, None))
        try:
            _wei = int(r.get("gasUsed") or 0) * int(r.get("effectiveGasPrice") or 0)
            gas_usd = _wei * float(self.cfg.eth_price_usd) / 1e18
        except Exception:
            _wei, gas_usd = 0, 0.0
        volley_gas_add(gas_usd)
        volley_bal_charge(addr, _wei)     # #45: патрон списывается по ФАКТУ, не по эстимейту
        if _receipt_has_win(r, self.cfg.liquidator_address) is not True:
            self.stats["board_noop"] = self.stats.get("board_noop", 0) + 1
            return
        feed, mid, b, dbt = (tgt if tgt else ("", "?", "?", 0.0))
        from chain.execute import mark_recent_send
        self.stats["wins"] += 1
        mark_recent_send(mid, b)
        self._set_cooldown(feed, "probe", proven=True)       # #46: победа пробы не гасит флот
        eng = getattr(self, "_fire_engine", None)
        if eng is not None:
            eng.disarm()                 # приз взят — огонь гасим немедленно, не ждём TTL
        if feed:
            with self._lock:
                stop = self._windows.pop(feed, None)
            if stop is not None:
                stop.set()
        log.warning("SPECFIRE WIN!!! [движок %s] %s/%s debt=$%.0fk tx=%s gas=$%.2f — "
                    "план доски исполнен (окна на паузе %.0fс)",
                    (addr or "?")[:10], str(mid)[:10], str(b)[:10], (dbt or 0) / 1e3, h,
                    gas_usd, SPEC_FIRE_WIN_COOLDOWN_SEC)

    def _engine_timeout(self, meta, h):
        """Квитанции нет за таймаут: nonce ноги под вопросом — склад этой ноги сбрасываем,
        иначе вся её лестница висит за возможной дырой."""
        from chain.execute import volley_invalidate_nonce, invalidate_ladder
        addr = (meta or (None, None))[0]
        if addr:
            volley_invalidate_nonce(addr)
            invalidate_ladder(addr)
            self._log("SPECFIRE движок NO-RECEIPT eoa=%s tx=%s — склад ноги сброшен" % (
                addr[:10], h))

    def _volley(self, probe, mid, b, d, feed, simultaneous=False):
        """Залп вслед за успешной пробой главного кошелька: тот же calldata на v4 от каждого
        залпового EOA. Огонь и ожидания — в daemon-тредах: петля окна не тормозится ни на мс.

        Расстановка ног зависит от того, ЗНАЕМ ли мы момент:
          simultaneous=False (вслепую) — сдвиг фазы (i+1)*PHASE. N ног = N участков покрытия,
            модель P(win)=W/τ; синхронный залп здесь дал бы N-кратную избыточность на одном
            участке, т.е. выиграл бы ровно те гонки, что и один EOA — покрытие не растёт;
          simultaneous=True (транзит виден в pending) — момент известен, бьём разом.
        SPEC_FIRE_VOLLEY_PARALLEL=1: нога = свой тред, сдвиг держим мы, а не сеть (замер
        боевого send-пути — у константы). =0 — прежний последовательный цикл бит-в-бит."""
        from chain import execute as _ex
        if not _ex.SPEC_FIRE_VOLLEY or not self._cascade_active():
            return
        accts = (self._volley_accounts_fn or _ex.volley_accounts)()
        if not accts:
            return
        sync = bool(simultaneous) and SPEC_FIRE_VOLLEY_DETECT_SYNC
        # v5 доска: план УЖЕ в контракте под ЭТУ цель и запись успела сесть -> ноги несут 1 байт
        # (цель не палится, газ калдаты ~в 200 раз меньше). Иначе — прежний полный calldata.
        board = self._board_live(mid, b)
        _cd = _ex.V2_PROBE_CALLDATA if board else probe["calldata"]
        _gas = SPEC_FIRE_V2_FIRE_GAS if board else probe["gas"]
        # ДВИЖОК ОГНЯ: план уже в контракте, значит пробе не нужен ни этот тик, ни эти треды —
        # тик лишь ПРОДЛЕВАЕТ право стрелять постоянному пулу. Порождать залп больше не нужно.
        # Только для доски: у V1 калдата зависит от цели, а склад подписан под 1 байт.
        if board:
            eng = self._engine()
            if eng is not None:
                eng.keepalive(target=(feed, mid, b, d))
                return

        def _leg(i, addr, key, mf, tip, bcast, wait_sec):
            try:
                if wait_sec > 0:
                    self._sleep(wait_sec)
                h = (self._volley_send_fn or _ex.send_volley_probe)(
                    self.rpc, self.cfg, addr, key, _cd, _gas,
                    log=log, max_fee=mf, tip=tip, broadcast_raw_urls=bcast)
                if h is None:
                    return
                self.stats["volley_probes"] += 1
                if board:
                    self.stats["board_probes"] += 1
                self._log("SPECFIRE volley leg=%d/%d%s%s eoa=%s feed=%s %s/%s tx=%s" % (
                    i + 1, len(accts), " [sync]" if sync else "", " [доска]" if board else "",
                    addr[:10], feed, mid[:10], b[:10], h))
                threading.Thread(target=self._collect_volley,
                                 args=(h, mid, b, d, feed, addr, board),
                                 daemon=True).start()
            except Exception as e:
                self._log("SPECFIRE volley leg fail feed=%s eoa=%s: %s" % (
                    feed, addr[:10], type(e).__name__))

        def _run():
            try:
                from chain.hotpath import _fresh_basefee
                fb = _fresh_basefee()                     # один фетч на залп, не на ногу
                tip = _probe_tip_wei(self.cfg.tip_gwei)   # тот же tip-кап: FCFS, место не покупается
                mf = (fb * 2 + tip) if fb is not None else None
                seq = str(getattr(self.cfg, "sequencer_rpc", "") or "")
                bcast = [seq] if (seq and seq.lower() not in ("off", "none", "0")) else None
                if not SPEC_FIRE_VOLLEY_PARALLEL:          # прежний путь: сдвиг ПЕРЕД каждой ногой
                    for i, (addr, key) in enumerate(accts):
                        _leg(i, addr, key, mf, tip, bcast,
                             0.0 if sync else SPEC_FIRE_VOLLEY_PHASE_SEC)
                    return
                # Флот: ног может быть десятки, а ядер два. Тред-на-ногу тут душит боевой контур,
                # поэтому ноги режутся на ПАЧКИ — не больше MAX_THREADS тредов на залп. Внутри
                # пачки ноги идут подряд, но КАЖДАЯ держит свой накопленный сдвиг от начала залпа
                # (сдвиг считается по индексу ноги, а не по позиции в пачке) — расстановка фаз
                # остаётся той же, что при треде-на-ногу.
                n = len(accts)
                nthr = max(1, min(SPEC_FIRE_VOLLEY_MAX_THREADS, n))
                t0 = self._now()

                def _batch(idxs):
                    for i in idxs:
                        addr, key = accts[i]
                        want = 0.0 if sync else SPEC_FIRE_VOLLEY_PHASE_SEC * (i + 1)
                        _leg(i, addr, key, mf, tip, bcast, max(0.0, want - (self._now() - t0)))

                for t in range(nthr):
                    threading.Thread(target=_batch, args=(range(t, n, nthr),),
                                     daemon=True, name="volley-b%d" % (t + 1)).start()
            except Exception as e:
                self._log("SPECFIRE volley fail feed=%s: %s" % (feed, type(e).__name__))

        threading.Thread(target=_run, daemon=True, name="volley-%s" % feed).start()

    def _collect_volley(self, h, mid, borrower, debt_usd, feed, addr, board=False):
        """Receipt ноги залпа: газ — в бакет тира; WIN — та же семантика, что у главной пробы
        (дедуп-маркер, кулдаун, окно закрыть); NO-RECEIPT инвалидирует nonce СВОЕГО EOA.

        board=True — проба ДОСКИ: статус tx о победе не говорит ничего (три исхода дают
        status=1, см. LIQUIDATED_TOPIC0), поэтому вердикт выносит только событие Liquidated."""
        from chain.execute import volley_gas_add, volley_invalidate_nonce, mark_recent_send
        res = (self._receipt_fn or self._receipt)(h)
        if res is None:
            volley_invalidate_nonce(addr)
            self._log("SPECFIRE volley NO-RECEIPT eoa=%s tx=%s (nonce EOA инвалидирован)" % (addr[:10], h))
            return
        status, gas_usd = res[0], res[1]
        pos = (" blk=%s idx=%s" % (res[2], res[3])) if len(res) >= 4 and res[2] is not None else ""
        won = res[4] if len(res) >= 5 else None
        volley_gas_add(gas_usd)
        if board and status == 1 and won is not True:
            # холостая проба доски (или неудачная попытка, съеденная catch): приза нет.
            # Молча — таких строк в каскаде будут тысячи.
            self.stats["board_noop"] = self.stats.get("board_noop", 0) + 1
            return
        if status == 1:
            self.stats["wins"] += 1
            mark_recent_send(mid, borrower)               # жертва взята — глушим реактивный дубль
            self._set_cooldown(feed, "probe", proven=True)   # #46
            if feed:
                with self._lock:
                    stop = self._windows.pop(feed, None)
                if stop is not None:
                    stop.set()
            log.warning("SPECFIRE WIN!!! [volley %s] %s/%s debt=$%.0fk tx=%s gas=$%.2f%s — залп "
                        "взял предочередь (окна на паузе %.0fс)",
                        addr[:10], mid[:10], borrower[:10], debt_usd / 1e3, h, gas_usd, pos,
                        SPEC_FIRE_WIN_COOLDOWN_SEC)
        else:
            self._log("SPECFIRE volley revert (healthy) eoa=%s tx=%s gas=$%.3f%s" % (
                addr[:10], h, gas_usd, pos))

    def _window_loop(self, feed: str, direction: str, stop: threading.Event):
        dev = self.dev_by.get(feed, 0.005)
        thr = dev * SPEC_FIRE_THRESHOLD
        band = dev * SPEC_FIRE_OWED_BAND
        probes = 0
        last_fire = 0.0
        last_hold_log = 0.0
        cache = {}      # (mid, b) -> (probe|None, built_ts): кэш пробы, см. SPEC_FIRE_REBUILD_SEC
        cap_spent: dict = {}   # (mid, b) -> swap_meta: свой резерв живого капа (consume-хук 05.08)
        dead = set()    # LOSS-стоп: цели, разобранные конкурентом/погашенные (bs==0 на пересборке)
        nobuild: dict = {}   # фолбэк (01.08): (mid, b) -> ts, до которого цель не пересобираем
        owed_until = 0.0     # peak-owed: до какого момента транзит «заряжен» (0 = долга нет)
        owed_ret = 0.0       # пиковое |ret| горячей фазы — им меряем reach в долге
        was_raw_hot = False
        gaps: list = []      # B3: пробелы проб горячей фазы (p95 в close-строке — метрика-гейт <4с)
        gap_t0 = None
        last_yield_log = 0.0
        fat_mode = False     # EV-плотность (06.08): жир в досягаемости -> каденс 0.12с, кап 600
        next_board_try = 0.0  # v5 доска: когда снова пробовать положить/переложить план
        try:
            while not stop.is_set() and probes < _window_cap(fat_mode):
                pf = self.pending_fire.pop(feed, None)
                if pf is not None and self._now() - pf[3] <= PENDING_FIRE_FRESH_SEC:
                    # A2: handoff от pending-detect — жертва флипнута в ФОРМИРУЕМОМ блоке.
                    # Стреляем вне очереди: без ret-гейта, без интервала, без арбитра (B).
                    # Тот же send-шов (send_spec_fire_probe, единый nonce) — fire-путь не менялся.
                    _pm, _pb, _pd = pf[0], pf[1], pf[2]
                    # ПОДТВЕРЖДЁННЫЙ ТРАНЗИТ — сильнейший сигнал, какой мы умеем видеть, и он
                    # приходит В ТОМ ЧИСЛЕ в окна НИЖЕ порога огня (класс cbXRP: оракул -0.50%
                    # при нашем ret -0.43%) — ровно те, ради которых делался #47. Раньше право
                    # огня здесь висело на fat_mode, который выставляется только в горячей
                    # ветке: детект открыли, а флот в этом блоке оставался нем структурно
                    # (ревью 09.08, CONFIRMED). Откат: SPEC_FIRE_HANDOFF_KEEPALIVE=0.
                    if fat_mode or SPEC_FIRE_HANDOFF_KEEPALIVE:
                        self._engine_fat_keepalive(feed)   # право огня ДО сборки/отправки пробы
                    try:
                        probe, _bts = cache.get((_pm, _pb), (None, 0.0))
                        if not isinstance(probe, dict):
                            self._release_cap(cap_spent, (_pm, _pb))
                            probe = (self._build_fn or self._build)(_pm, _pb, _pd)
                            if isinstance(probe, dict):
                                cache[(_pm, _pb)] = (probe, self._now())
                        if isinstance(probe, dict):
                            h = (self._send_fn or self._send)(probe["calldata"], probe["gas"])
                            if h is not None:
                                self._consume_cap(cap_spent, (_pm, _pb), probe)
                                probes += 1
                                self.stats["probes"] += 1
                                self.stats["pending_fires"] += 1
                                last_fire = self._now()
                                if gap_t0 is not None:
                                    gaps.append(last_fire - gap_t0)
                                gap_t0 = last_fire
                                self._log("SPECFIRE probe #%d feed=%s %s/%s debt=$%.0fk "
                                          "[pending-handoff] tx=%s" % (
                                              probes, feed, _pm[:10], _pb[:10], _pd / 1e3, h))
                                threading.Thread(target=self._collect, args=(h, _pm, _pb, _pd, feed),
                                                 daemon=True).start()
                                if fat_mode:      # тир: залп по подтверждённому флипу (07.08)
                                    # транзит подтверждён в pending -> момент известен: разом
                                    self._volley(probe, _pm, _pb, _pd, feed, simultaneous=True)
                    except Exception as e:
                        self._log("SPECFIRE pending-handoff fail feed=%s: %s" % (feed, type(e).__name__))
                    stop.wait(SPEC_FIRE_POLL_SEC)
                    continue
                r = None
                try:
                    r = self.ret_fn(feed)
                except Exception:
                    pass
                raw_hot = (r is not None and ((direction == "down" and r <= -thr)
                                              or (direction == "up" and r >= thr)))
                _n = self._now()
                if raw_hot:
                    owed_ret = max(owed_ret, abs(r))
                    owed_until = _n + SPEC_FIRE_OWED_HOLD_SEC
                elif (owed_until > _n and r is not None
                        and ((direction == "down" and r <= -(thr - band))
                             or (direction == "up" and r >= thr - band))):
                    owed_until = _n + SPEC_FIRE_OWED_HOLD_SEC   # серая полоса: долг продлевается
                owed_fire = (SPEC_FIRE_PEAK_OWED and not raw_hot
                             and owed_until > _n and owed_ret > 0.0)
                if was_raw_hot and not raw_hot and owed_fire:
                    self._log("SPECFIRE peak-owed feed=%s: отскок с пика %.2f%% — транзит заряжен, "
                              "огонь ещё %.0fс (полоса %.2f%%)" % (
                                  feed, owed_ret * 100.0, owed_until - _n, (thr - band) * 100.0))
                was_raw_hot = raw_hot
                self._owed[feed] = owed_until if SPEC_FIRE_PEAK_OWED else 0.0
                hot = raw_hot or owed_fire
                _pbd = self.probe_board.get(feed)
                if (_pbd is not None and _n - _pbd[5] > SPEC_FIRE_BOARD_RESTAMP_SEC
                        and self._board_plan_live() is not None):
                    # Потребители (tier-2, ws-handoff) гейтят ВОЗРАСТ доски 10с, а переиздать её
                    # прогрев может не раньше 240с и не может вовсе, пока план армлен под ту же
                    # цель (TTL 870с) — фикс #47 жил ~4% окна (ревью 09.08, CONFIRMED). Пока план
                    # ЖИВ on-chain, проба валидна: обновляем ОТМЕТКУ, без RPC и без пересборки.
                    self.probe_board[feed] = _pbd[:5] + (_n,)
                if SPEC_FIRE_V2_BOARD and _n >= self._board_next_try.get(feed, next_board_try):
                    # доску армим ЗАРАНЕЕ и переармливаем по каденсу (роут протухает так же,
                    # как у обычной пробы). Свой тред: сборка плана не крадёт мс у огня.
                    # #57: расписание живёт в _board_next_try — осечка арма подтянет его к
                    # быстрому ретраю, а не оставит окно без доски на весь каденс 240с.
                    self._board_next_try[feed] = _n + SPEC_FIRE_V2_REARM_SEC
                    threading.Thread(target=self._prearm_board, args=(feed, direction, dev),
                                     daemon=True, name="board-%s" % feed).start()
                if hot and gap_t0 is None:
                    gap_t0 = self._now()               # B3: старт горячей фазы — лаг до первой пробы тоже пробел
                elif not hot:
                    gap_t0 = None
                if SPEC_FIRE_DENSE:                    # вся горячая фаза плотно (гонка плотностей, 31.07)
                    iv = _dense_interval(fat_mode)     # EV-плотность: жир решает каденс (06.08)
                elif probes < SPEC_FIRE_BURST_PROBES:
                    iv = SPEC_FIRE_INTERVAL_SEC
                else:                                  # хвост с джиттером (см. SPEC_FIRE_TAIL_JITTER_SEC)
                    _j = SPEC_FIRE_TAIL_JITTER_SEC
                    iv = max(0.3, SPEC_FIRE_SLOW_INTERVAL_SEC + (random.uniform(-_j, _j) if _j > 0 else 0.0))
                if self.pending_healthy_fn is not None:
                    try:
                        if self.pending_healthy_fn():  # A3: pending жив и детектит — on-chain пробы
                            iv = max(iv, PENDING_KEEPALIVE_SEC)   # в кипэлайв (газ ~7× вниз)
                    except Exception:
                        pass
                if hot and self._now() - last_fire >= iv and not self._gate_open(feed):
                    # фазовый гейт: секунда доказуемо холостая (пуш садится на узел решётки).
                    # Патроны не тратим — когда слот откроется, условие интервала уже выполнено
                    # и проба уйдёт сразу, т.е. бюджет сам стягивается в живые секунды.
                    # #64 (ревью 09.08, CONFIRMED): гейт задумывался как экономия ГАЗА кадансовых
                    # проб главного кошелька, но стоял ВЫШЕ единственной точки права огня в тике,
                    # и потому глушил ПЛОТНОСТЬ: на решётке 30с закрытая фаза 26с, а TTL права
                    # огня 1.5с — стена флота гасла до транзита, включая замеренные ~4% пушей
                    # ВНЕ узла. Право огня выдаём и в закрытую секунду: пробы движка стоят
                    # ~$0.0004 и держатся своими бакетами и полом патрона.
                    if fat_mode and SPEC_FIRE_LATTICE_KEEPALIVE:
                        self._engine_fat_keepalive(feed)
                    self.stats["lattice_skips"] += 1
                    stop.wait(SPEC_FIRE_POLL_SEC)
                    continue
                if hot and self._now() - last_fire >= iv:
                    _eff_r = max(abs(r or 0.0), owed_ret if owed_fire else 0.0)
                    reach = _eff_r + SPEC_FIRE_DIST_MARGIN_DEVS * dev   # в долге меряем по пику
                    _depth = max(SPEC_FIRE_TOPK, SPEC_FIRE_FALLBACK_DEPTH if SPEC_FIRE_BUILD_FALLBACK else 0)
                    tgts, hold_dist, edge = self._top_fat(feed, direction, max_dist=reach, k=_depth)
                    if not SPEC_FIRE_EDGE_SLOT:
                        edge = None
                    if dead:
                        tgts = [t for t in tgts if (t[0], t[1]) not in dead]
                        if edge is not None and (edge[0], edge[1]) in dead:
                            edge = None
                    if not tgts and edge is not None:
                        tgts = [edge]              # весь top-K мёртв, кромка жива — окно держит её
                    _fat_now = _tgts_fat(tgts, bonus_of=self._bonus_of)
                    if _fat_now and not fat_mode:
                        self._log("SPECFIRE ev-density feed=%s: жир $%.0fk в досягаемости — "
                                  "каденс %.2fс, кап %d" % (
                                      feed, max(t[2] for t in tgts) / 1e3,
                                      SPEC_FIRE_FAT_INTERVAL_SEC, SPEC_FIRE_FAT_CAP))
                    fat_mode = _fat_now
                    if fat_mode:
                        self._engine_fat_keepalive(feed)
                    if not tgts:
                        if hold_dist is None:
                            break                              # жир ушёл из набора — окно нам не нужно
                        if self._now() - last_hold_log >= 10.0:   # жир есть, но флип вне досягаемости
                            last_hold_log = self._now()           # движения — держим окно, НЕ жжём бакет
                            self._log("SPECFIRE hold feed=%s: жир вне досягаемости (ближ. dist=%.2f%% > reach=%.2f%%)" % (
                                feed, hold_dist * 100.0, reach * 100.0))
                        stop.wait(SPEC_FIRE_POLL_SEC)
                        continue
                    _t0 = self._now()                  # пакет B: заявка = жир первой ДОСТУПНОЙ цели
                    _avail = [t for t in tgts          # (nobuild-скамейка не претендует — иначе
                              if not (SPEC_FIRE_BUILD_FALLBACK                # чужое окно голодает
                                      and nobuild.get((t[0], t[1]), 0.0) > _t0)]   # по нашей несобираемой)
                    _claim = _avail[0][2] if _avail else tgts[0][2]
                    self._prio[feed] = (_claim, _t0)
                    if not self._prio_ok(feed, _claim):
                        if _t0 - last_yield_log >= 10.0:
                            last_yield_log = _t0
                            self._log("SPECFIRE yield feed=%s: слот у более богатого окна "
                                      "(наш жир $%.0fk)" % (feed, _claim / 1e3))
                        stop.wait(SPEC_FIRE_POLL_SEC)
                        continue
                    # плотность ротируется по top-K как раньше; хвост списка — ТОЛЬКО фолбэк
                    # на небилдящиеся (01.08: топ-цель «нет свопа» не должна монополизировать тик).
                    # edge-слот (06.08): ближайшая-по-dist цель входит в head наравне с жиром
                    head = tgts[:SPEC_FIRE_TOPK]
                    tail = tgts[SPEC_FIRE_TOPK:]
                    if edge is not None and all(t[:2] != edge[:2] for t in head):
                        head = head + [edge]
                        tail = [t for t in tail if t[:2] != edge[:2]]
                    _rot = probes % len(head)
                    order = head[_rot:] + head[:_rot] + tail
                    sent = False
                    for mid, b, d in order:
                        _t = self._now()
                        if SPEC_FIRE_BUILD_FALLBACK and nobuild.get((mid, b), 0.0) > _t:
                            continue                           # на скамейке — пересборка через RETRY_SEC
                        try:
                            probe, built_ts = cache.get((mid, b), (None, 0.0))
                            if probe is None or _t - built_ts >= SPEC_FIRE_REBUILD_SEC:
                                self._release_cap(cap_spent, (mid, b))
                                probe = (self._build_fn or self._build)(mid, b, d)
                                if probe == "gone":   # LOSS-стоп (ревью 31.07): жертву разобрали —
                                    dead.add((mid, b))    # кап не догорает ревертами по мёртвой цели
                                    cache.pop((mid, b), None)
                                    self._log("SPECFIRE loss-stop feed=%s %s/%s: позиция разобрана "
                                              "(конкурент/погашение) — цель снята" % (feed, mid[:10], b[:10]))
                                    continue
                                cache[(mid, b)] = (probe, self._now())
                                if isinstance(probe, dict):
                                    # доска проб (A1 ярус-2): pending-цикл дергает eth_call
                                    # ЭТОЙ пробы по формируемому блоку — та же семантика
                                    self.probe_board[feed] = (mid, b, d, probe["calldata"],
                                                              probe["gas"], self._now())
                            if probe is not None:
                                h = (self._send_fn or self._send)(probe["calldata"], probe["gas"])
                                if h is not None:
                                    self._consume_cap(cap_spent, (mid, b), probe)
                                    probes += 1
                                    self.stats["probes"] += 1
                                    last_fire = self._now()
                                    if gap_t0 is not None:
                                        gaps.append(last_fire - gap_t0)
                                    gap_t0 = last_fire
                                    _om = ""
                                    if owed_fire:              # would-have-fired: старая политика молчала бы
                                        self.stats["owed_probes"] += 1
                                        _om = " [owed пик=%.2f%% ещё %.0fс]" % (
                                            owed_ret * 100.0, max(0.0, owed_until - self._now()))
                                    if edge is not None and (mid, b) == (edge[0], edge[1]):
                                        _om += " [edge]"       # проба edge-слота (кромка по dist)
                                    self._log("SPECFIRE probe #%d feed=%s %s/%s debt=$%.0fk ret=%.4f%%%s tx=%s" % (
                                        probes, feed, mid[:10], b[:10], d / 1e3, (r or 0) * 100.0, _om, h))
                                    threading.Thread(target=self._collect, args=(h, mid, b, d, feed),
                                                     daemon=True).start()
                                    if fat_mode:  # тир: жир И каскад -> залп той же пробой (07.08)
                                        self._volley(probe, mid, b, d, feed)
                                sent = True                    # слот тика израсходован отправкой
                                break
                            nobuild[(mid, b)] = self._now() + SPEC_FIRE_NOBUILD_RETRY_SEC
                            self._log("SPECFIRE probe skip feed=%s %s/%s: нет свопа (роут/prearm)%s" % (
                                feed, mid[:10], b[:10],
                                " — фолбэк к следующей цели" if SPEC_FIRE_BUILD_FALLBACK else ""))
                            if not SPEC_FIRE_BUILD_FALLBACK:
                                break                          # прежнее поведение: тик сгорает
                        except Exception as e:
                            self._log("SPECFIRE probe fail feed=%s: %s" % (feed, type(e).__name__))
                            stop.wait(1.0)
                            break
                    if not sent:
                        stop.wait(2.0)                         # весь список без свопа — не молотить чтениями
                stop.wait(SPEC_FIRE_POLL_SEC)
        finally:
            self._owed.pop(feed, None)   # окно закрыто — долг не переживает петлю
            self._prio.pop(feed, None)   # заявка на слот снята (пакет B)
            self.probe_board.pop(feed, None)
            # РЕГРЕССИЯ #57 (ревью 09.08, CONFIRMED): расписание арма переехало в словарь
            # ИНСТАНСА и переживало закрытие окна — окна 2..N того же фида в пределах 240с не
            # грелись ВОВСЕ (ни доски, ни probe_board), то есть детект-пути выбрасывали уже
            # пойманный транзит. В каскаде каждый пуш ре-якорит фид, и это ровно окна 2..N.
            # Лишних setPlan это не создаёт: дедуп «свежая и та же цель» стоит в _arm_board.
            self._board_next_try.pop(feed, None)
            _g = sorted(gaps)
            # close-строка: B3-метрика (gap p95 горячей фазы) + кумулятивные счётчики (C5:
            # owed_probes/sticky_windows/pending_fires видны grep'ом без маркеров)
            # #45: отказы пути движка — в ту же close-строку. Невидимый отказ = отказ, который
            # никто не чинит: нога, замолчавшая на полу или без кэша патрона, до этого была
            # неотличима от «окно было пустым».
            try:
                from chain.execute import volley_probe_skips, volley_maxfee_muted
                _fs, _us = volley_probe_skips()
                _mm = len(volley_maxfee_muted())
            except Exception:
                _fs = _us = _mm = -1
            if _mm > 0 and self._alert is not None:
                # У2 (решение kelbic 09.08): немота ног по комиссии = состояние, которое
                # само не рассосётся (долив или ручка MAXFEE_RESERVE_MULT) и которое иначе
                # читается как «не было целей». Антифлуд держит Alerter по ключу.
                self._alert("\U0001F507 флот: %d ног немы ПО КОМИССИИ (резерв gas x maxFee "
                            "съел патрон) — долив ноги или MAXFEE_RESERVE_MULT" % _mm,
                            "leg-maxfee-mute")
            self._log("SPECFIRE window close feed=%s probes=%d gap_p95=%s "
                      "patron_floor_skips=%d patron_unknown_skips=%d leg_maxfee_mute=%d "
                      "stats=%s" % (
                          feed, probes,
                          ("%.1fс" % _g[max(0, int(len(_g) * 0.95) - 1)]) if _g else "—",
                          _fs, _us, _mm, self.stats))

    def handle_event(self, ev: dict):
        try:
            kind = ev.get("event")
            feed = (ev.get("feed") or "").upper()
            if kind == "arm":
                direction = ev.get("dir") or "down"
                if self._cooldown_active(feed):            # WIN-пауза (#46: по умолчанию пер-фид)
                    return None
                best, hold_dist, _ = self._top_fat(feed, direction, max_dist=SPEC_FIRE_ARM_MAX_DIST)
                if not best:
                    if hold_dist is not None:              # жир есть, но заведомо вне окна — не палим
                        _n = self._now()                   # ни петлю, ни ончейн-паттерн
                        if _n - self._armskip_ts.get(feed, -1e9) >= 300.0:
                            self._armskip_ts[feed] = _n
                            self._log("SPECFIRE arm-skip feed=%s: ближ. жир dist=%.2f%% > потолок %.1f%%" % (
                                feed, hold_dist * 100.0, SPEC_FIRE_ARM_MAX_DIST * 100.0))
                    return None                            # окна без (досягаемого) жира не запускают петлю
                with self._lock:
                    old = self._windows.pop(feed, None)
                    if old is not None:
                        old.set()
                    stop = threading.Event()
                    self._windows[feed] = stop
                    self.stats["windows_armed"] += 1
                _n = self._now()                           # каскад-детект (гибрид): армы И факты
                self._arm_ts = [t for t in self._arm_ts if _n - t < 3600.0] + [_n]
                if len(self._arm_ts) >= SPEC_FIRE_CASCADE_WINDOWS:
                    if _facts_fresh():
                        from chain.execute import specfire_cascade_boost
                        if specfire_cascade_boost() and _n - self._armskip_ts.get("_boost", -1e9) >= 300.0:
                            self._armskip_ts["_boost"] = _n
                            self._log("SPECFIRE каскад-детект: %d окон/час + факт-флаг — бакет поднят "
                                      "до каскадного (TTL 1ч, продлевается армами)" % len(self._arm_ts))
                    elif _n - self._armskip_ts.get("_nofacts", -1e9) >= 300.0:
                        self._armskip_ts["_nofacts"] = _n
                        self._log("SPECFIRE каскад-армы (%d/час) БЕЗ факт-флага — бустер молчит "
                                  "(fail-closed, штатный бакет)" % len(self._arm_ts))
                self._log("SPECFIRE LIVE window feed=%s dir=%s — жир на прицеле, огонь при |ret|>=%.3f%%" % (
                    feed, direction, self.dev_by.get(feed, 0.005) * SPEC_FIRE_THRESHOLD * 100.0))
                threading.Thread(target=self._window_loop, args=(feed, direction, stop),
                                 daemon=True, name="specfire-%s" % feed).start()
            elif kind in ("confirmed", "push", "disarm", "falsepos", "stale"):
                if kind in ("confirmed", "push"):
                    self.note_push(feed, updated_at=ev.get("updated_at"))
                    # решётка OCR: метка севшего апдейта — по времени ОРАКУЛА (#49)
                if (SPEC_FIRE_PEAK_OWED and kind in ("disarm", "falsepos")
                        and self._owed.get(feed, 0.0) > self._now()
                        and feed in self._windows):
                    # sticky: транзит заряжен на пике — отскок предикта окно не убивает;
                    # confirmed/push (транзит сел) и stale (фид умер) закрывают как раньше
                    self.stats["sticky_windows"] += 1
                    self._log("SPECFIRE sticky feed=%s: %s при заряженном транзите — окно живёт "
                              "ещё %.0fс" % (feed, kind, self._owed[feed] - self._now()))
                    return None
                with self._lock:
                    stop = self._windows.pop(feed, None)
                if stop is not None:
                    stop.set()
        except Exception:
            log.exception("spec-fire live: событие %s упало (слой изолирован)", ev.get("event"))
        return None
