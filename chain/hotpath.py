"""Hot path (Phase 2, PRECONF-triggered). A SECOND trigger beside the block loop: on a real `transmit`
to one of our aggregators, recompute the affected candidates' HF on the PRE-CONFIRMED price, and for a
position that flips liquidatable + passes the preconf-pending sim + the net floor + the narrow reaction
filter (`hot_min_repaid_usd`, see config.py for the live value — this was hardcoded as "$2k+" here and
went stale when the floor was lowered to $450), prepare and dispatch a liquidation. Reuses the
battle-tested prepare/dispatch building blocks; ONLY the sourcing (preconf price for sizing +
preconf-pending sim) and the trigger differ.

Latency shape: the async recv loop only DETECTS a transmit (precise 94-form match) and spawns a worker
thread; all slow work (preconf read -> flip -> KyberSwap quote -> sim -> dispatch) runs off the recv
loop so detection stays responsive. Cross-path nonce safety (this path + the block loop both send from
one wallet) is handled by the send-lock inside dispatch_liquidations (added in C2).

DORMANT until wired into block_driven_loop behind cfg.hot_path. No behavior change to the existing loop.
"""
from __future__ import annotations
import logging
import os
import threading
import time

# Модульный импорт, а не внутрифункциональный: redact_clip зовут шесть разных функций, и
# локальный импорт в одной из них оставил бы остальные с NameError (поймано статическим
# тестом test_unresolved_names.py, #35 — ровно тот случай, ради которого он писался).
# Цикла нет: chain/rpc.py ничего из chain/hotpath.py не импортирует.
from chain.rpc import redact_clip
# #110: модульный импорт по той же причине — вызовов два (подача блока и печать счётчиков),
# и локальный импорт в одном оставил бы второй с NameError. Цикла нет: chain/chain_index.py
# из chain/hotpath.py ничего не берёт.
from chain import chain_index as _chain_index

log = logging.getLogger(__name__)

MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
FB_URL = "wss://mainnet.flashblocks.base.org/ws"
PRECONF_RPC_DEFAULT = "https://mainnet-preconf.base.org"
# ---- бюджет публичного preconf (24.07, инцидент «degraded 18x windows») ----
# Публичный preconf = per-IP токен-бакет по ВЕСУ (~1 тяжёлый pending-вызов/с, замер 21-07 и
# 24-07: 2 из 5 aggregate3@pending с шагом 0.4с -> 429). Инверсия приоритетов: ФОНОВЫЙ полл
# (36 оракулов КАЖДЫЙ блок) выжирал бакет, и РЕАКТИВНЫЙ путь (хедж цены/позиций на транзите)
# ловил 429 -> терял ~1.8с форы в спорных гонках, т.е. бэкап-триггер душил главный.
# Лечение БЕЗ денег (Alchemy НЕ подходит: его `pending` = уже СОБРАННЫЙ блок — замер 24.07:
# txs=114 байт-в-байт равен latest публичного preconf, тогда как настоящий preconf отдаёт блок
# В СБОРКЕ (42 tx, 6.5M из 19M gas) — форы не даёт, а переключение выжгло фри-квоту 21-07).
# ПРАВИЛО: полл пропускает preconf ТОЛЬКО в REACTIVE_QUIET-тени реактивного чтения — то есть
# ровно там, где он и так получал 429 (и приходил к latest, но ПОСЛЕ оплаченного отказа).
# Форы НЕ теряет никто: вне тени полл читает pending как раньше; в тени — latest без 429-платы.
# MIN_GAP=0 (выключен) — резерв на случай, если тени не хватит: поднять env'ом без кода.
# Откат без кода: PRECONF_REACTIVE_QUIET_SEC=0.
# ---- STAGE-ТАЙМИНГ (25.07, замечание ревьювера #1): атрибуция ВРЕМЕНИ фоном, а не кампанией.
# 45мс налога middleware нашлись атрибуцией РАСХОДА; следующий «собственный налог» должен всплыть
# сам. Копим p50/p95 по стадиям в скользящем часе и печатаем строкой в hot stats.
# Стадии: detect (транзит->спавн), price, read (позиции), flips (CPU), prep (sim+сборка),
# sign (подпись залпа), volley (raw в секвенсер), e2e.
_STAGE_LOCK = threading.Lock()
_STAGES: dict = {}
STAGE_WINDOW_SEC = float(os.environ.get("STAGE_WINDOW_SEC", "3600"))
CU_ALERT_PROJ_M = float(os.environ.get("CU_ALERT_PROJ_M", "26"))   # проекция M CU/мес -> TG-будильник
# Сколько ПОДРЯД окон превышения нужно, чтобы будить ЧЕЛОВЕКА (20.08: три ложных
# пейджа за утро на спайках рестарта/прогрева — журнальный маркер остаётся на 1 окне).
CU_ALERT_STREAK_N = int(os.environ.get("CU_ALERT_STREAK_N", "3"))
# Сколько СЕКУНД накопленных замеров нужно ДЛЯ ПЕЙДЖА (журнальному маркеру хватает
# CU_ALERT_MIN_SPAN_SEC=900). Окно проекции — час (CU_ROLL_WINDOW_SEC), и на 15 минутах
# число описывает не месяц, а ПЕРВЫЕ 15 МИНУТ ПОСЛЕ РЕСТАРТА: подпись 220 лестниц,
# прогрев балансов флота, резолв фидов. 20.08 таких рестартов было восемь, и человек
# получил пять ложных пейджей (60M/24M против факта 19.3M/16.1M). Требуем ~полный час:
# «сторож, слепнущий от рестарта, сторожит рестарты» — поэтому гейт на СПАНЕ ДАННЫХ,
# а не на аптайме процесса.
CU_ALERT_PAGE_MIN_SPAN_SEC = float(os.environ.get("CU_ALERT_PAGE_MIN_SPAN_SEC", "3000"))
# Порог эскалации бэкоффа, при котором обрывы hot-path будят ЧЕЛОВЕКА (#4 = ~24с ожидания,
# то есть три подряд неудачных возврата). Быстрые удачные реконнекты остаются в журнале.
HOT_WS_PAGE_BACKOFF_N = int(os.environ.get("HOT_WS_PAGE_BACKOFF_N", "4"))
# 21.08: живая подача tip транзита в кольцо зеркала прямо из recv-петли (см. хук у
# FORWARD_SEL). 0 = только прежняя индексная подача (полные сканы, с лагом).
MIRROR_LIVE_FEED = os.environ.get("MIRROR_LIVE_FEED", "1").lower() not in ("0", "false", "no")


def _stage_frame(bn, ts=None):
    """П.7: метка «кадр суб-блока принят». Прибор сбоку — отказ не трогает петлю."""
    try:
        from chain.stage_probe import mark_frame
        mark_frame(bn, ts)
    except Exception:                                        # noqa: BLE001
        pass


def _stage_feed(feed, stage, ts=None):
    """П.7: метка стадии по фиду (detect/handoff) — пробы ещё нет."""
    try:
        from chain.stage_probe import mark_feed
        mark_feed(feed, stage, ts)
    except Exception:                                        # noqa: BLE001
        pass
# Сколько измеренного времени должно накопиться, прежде чем порогу можно верить. Это замена
# защёлке «не раньше 6ч аптайма»: та глушила сторожа рестартами (07.08 — 21 рестарт, 0 тревог
# в сутки максимального расхода), а требование span глушится только отсутствием ИЗМЕРЕНИЙ.
CU_ALERT_MIN_SPAN_SEC = float(os.environ.get("CU_ALERT_MIN_SPAN_SEC", "900"))
# #117 (19.08) КАНАЛ ТРЕВОГИ КВОТЫ. Замер владельца: маркер сработал 44 раза за 7 суток и НИ
# РАЗУ не дошёл до человека — прибор исправен, сломан канал «журнал→человек». Это ровно класс
# «мёртвый сторож хуже отсутствующего»: строка в журнале, которую никто не читает, неотличима
# от молчания. Решение 02.08 («CU — в журнал, не в TG») принималось против ПРЕДВЕСТНИКА, и
# тогда было верным; здесь другое: исчерпание квоты требует ДЕЙСТВИЯ ЧЕЛОВЕКА (ротация ключа,
# страйп, докупка) и попадает в четыре законных случая канона «алерты только там, где нужен
# человек». Бот сам эту беду не отрабатывает.
# Против флапа, которого боялись 02.08: латч НА КЛЮЧ и на СУТКИ (один пейдж в сутки на ключ),
# плюс прежние два условия — скользящая проекция (не спайк одного окна) и накопленный span
# (не бут-спайк). Откат: CU_ALERT_TG=0 (остаётся прежняя журнальная строка).
#
# 20.08 ~20:0xZ, РУЧКА ОТКАТА НЕ РАБОТАЛА: в .env стояло `CU_ALERT_TG=0  # комментарий`, а
# systemd EnvironmentFile инлайн-комментарий НЕ срезает (режет только строки, начинающиеся с
# #). В процесс приезжало значение "0  # 20.08: квота -> инбокс...", оно не совпадало ни с
# одним из ("0","false","no","off") — и глушилка читалась как ВКЛЮЧЕНО. Выключенный тумблер
# ПЕЙДЖИЛ: kelbic получил квотные пейджи 18:10 и 19:18 после того, как отключение было
# объявлено сделанным (проверено рестартом, а не эффектом — канон «хэш tx не эффект»).
# Чинится ОБОИМИ концами: (1) значение режется по первому '#' и пробелам; (2) НЕПОНЯТОЕ
# значение ЗАКРЫВАЕТ гард, а не открывает — канон «незнание обязано закрывать гард»: у
# глушилки цена ошибочного «включено» (спам человеку) выше цены ошибочного «выключено»
# (строка остаётся в журнале, её читает агентский сторож).
def _env_flag(name, default):
    """Булев env-флаг: инлайн-# срезан, непонятое значение = ВЫКЛЮЧЕНО (гард закрыт)."""
    raw = (os.environ.get(name) or "").split("#", 1)[0].strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    logging.getLogger(__name__).warning(
        "%s=%r не разобран как булев — считаю ВЫКЛЮЧЕНО (незнание закрывает гард)", name, raw[:40])
    return False


CU_ALERT_TG = _env_flag("CU_ALERT_TG", True)
CU_ALERT_PAGE_SEC = float(os.environ.get("CU_ALERT_PAGE_SEC", "86400"))   # 1 пейдж/сутки на ключ
CU_QUOTA_M = float(os.environ.get("CU_QUOTA_M", "30"))                    # квота ключа, M CU/мес
# Латчи — МОДУЛЬНЫЕ и ПЕР-ХОСТОВЫЕ, и то и другое исправляет дефекты прежнего `last_cu_alert`:
#   (а) он был ЛОКАЛЬНОЙ переменной hot_loop => self-heal-перезапуск обнулял защёлку (тот же
#       класс, что защёлка «6ч аптайма» 07.08: сторож глохнет ровно от рестартов);
#   (б) он был ОДИН НА ВСЕ ХОСТЫ => с тремя ключами шумный ключ глушил маркер соседнего на 6ч.
_CU_MARK_TS: dict = {}    # host -> monotonic последней ЖУРНАЛЬНОЙ строки маркера
_CU_PAGE_TS: dict = {}    # host -> monotonic последнего ПЕЙДЖА человеку
_CU_OVER_STREAK: dict = {}  # host -> сколько ПОДРЯД окон превышения (гейт пейджа)
CU_MARK_SEC = float(os.environ.get("CU_MARK_SEC", "21600"))               # прежние 6ч, теперь на ключ


def stage_note(name: str, ms: float, now=None):
    t = time.monotonic() if now is None else now
    with _STAGE_LOCK:
        d = _STAGES.setdefault(name, [])
        d.append((t, float(ms)))
        if len(d) > 4000:                     # страховка от разрастания на длинном каскаде
            del d[:len(d) - 4000]


def stage_report(now=None) -> str:
    """'detect 4/12 price 0/1 read 87/294 ...' = p50/p95 мс за окно. Пусто -> ''."""
    t = time.monotonic() if now is None else now
    out = []
    with _STAGE_LOCK:
        for name in ("detect", "price", "read", "flips", "prep", "sign", "volley", "e2e"):
            vals = [v for (ts, v) in _STAGES.get(name, []) if t - ts <= STAGE_WINDOW_SEC]
            if not vals:
                continue
            vals.sort()
            p50 = vals[len(vals) // 2]
            p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
            out.append("%s %.0f/%.0f" % (name, p50, p95))
        for name in list(_STAGES):            # чистка хвостов вне окна
            _STAGES[name] = [(ts, v) for (ts, v) in _STAGES[name] if t - ts <= STAGE_WINDOW_SEC]
    return " ".join(out)


def CU_TOP_KEY(kv):
    """Сортировка по ВКЛАДУ в CU (n × цена), а не по числу вызовов. Ключ может быть
    'method@caller' (CU_ATTRIBUTION=1) — цену берём по части до '@'."""
    from chain.rpc import CU_COST, redact_clip
    return CU_COST.get(str(kv[0]).split("@", 1)[0], 20) * kv[1]


PRECONF_POLL_MIN_GAP = float(os.environ.get("PRECONF_POLL_MIN_GAP_SEC", "0"))
PRECONF_REACTIVE_QUIET = float(os.environ.get("PRECONF_REACTIVE_QUIET_SEC", "1.2"))
# Дробление fast-lane (01.08, «го» kelbic после гонки cbDOGE): полный сейз сверх капа
# прямого роута раньше ронял fast в Kyber-block-loop (+0.4-0.9с, протухшая квота = panic) —
# теперь режем repaid_shares до ёмкости пула, fast остаётся 0 RTT. Откат REACTIVE_PARTIAL=0.
REACTIVE_PARTIAL = os.environ.get("REACTIVE_PARTIAL", "1").lower() not in ("0", "false", "no")
# Хедж preconf-СИМА (06.08, prep=1 13:30:56): 429/таймаут preconf в горячие секунды транзита —
# транспортный отказ, не он-чейн вердикт, а skip терял жертву (взял конкурент). Путь ЦЕНЫ
# хеджирован дочиткой с 23.07 — сим получает тот же паттерн: одиночный ретрай через
# confirmed-rpc (block=latest; после транзита confirmed уже видит флип). Откат HOT_SIM_CONFIRMED_RETRY=0.
HOT_SIM_CONFIRMED_RETRY = os.environ.get("HOT_SIM_CONFIRMED_RETRY", "1").lower() not in ("0", "false", "no")
# ЗАПАС ВНИЗ НА ЗАЯВКЕ (#121, 14.08; доказано форк-репро блока 49965022 + транзит idx853):
# заявка строилась как repaidShares == borrowShares — РОВНО граница underflow'а Morpho
# (`position[id][b].borrowShares -= repaidShares`). Замер на форке НАШЕЙ боевой калдатой:
# ask == bs -> OK, профит $17.55; ask == bs+1 -> Panic 0x11. Значит ЛЮБОЕ уменьшение долга
# между снимком позиции и симом — хоть на 1 wei (repay заёмщика, частичная ликвидация
# конкурента) — переводило ВЫИГРЫШНЫЙ приз в `bucket=panic-underflow` -> `HOT skip`.
# Окно, где позиция ещё ликвидируема, шириной (1-HF)*shares (на цели 14.08 — 0.51%), то
# есть ВЕСЬ полезный диапазон дрейфа попадал в панику. Канон «незнание обязано УМЕНЬШАТЬ
# заявку»: просим строго меньше долга. Цена — тот же процент от бонуса (10bps = $0.02 на
# призе $17.55, замерено форком), польза — приз вместо отказа. Откат HOT_ASK_SHAVE_BPS=0.
ASK_SHAVE_BPS = int(os.environ.get("HOT_ASK_SHAVE_BPS", "10") or 0)
# ЗОНД ПАНИКИ (#121): у имени «Panic 0x11» в этом проекте УЖЕ ТРИ разных источника, и бакет
# `panic-underflow` их СЛИВАЕТ — форк-репро 14.08 показал, что одну и ту же панику дают (1) долг
# заёмщика ниже заявки, (2) залог ниже сейза, (3) `lastUpdate > block.timestamp` у рассинхронного
# preconf-состояния (тогда паника летит из _accrueInterest ДО проверки здоровья, и позиция на
# самом деле ЗДОРОВА). Зонд дочитывает confirmed-состояние в ОТДЕЛЬНОМ треде — решение о skip
# уже принято, денежный путь не трогается — и называет источник поимённо. Откат HOT_PANIC_PROBE=0.
HOT_PANIC_PROBE = os.environ.get("HOT_PANIC_PROBE", "1").lower() not in ("0", "false", "no")
# «ОТКАЗ ПРИ ЧУЖОЙ ПОБЕДЕ» (#123, 14.08; вопрос kelbic «почему о cbADA не пришёл отчёт в TG»).
# Уведомитель Ш16 подключён к рентгену ОТПРАВЛЕННЫХ tx — отказы (sim-revert -> HOT skip)
# невидимы ему ПО ПОСТРОЕНИЮ: 10 отказов за 7 дней прошли молча, все в денежные моменты.
# Здесь: лёгкий регистр отказов (сбоку от денежного пути, всё под except) + сверка с
# Liquidate-событиями фонового индекса (#110). Совпадение в TTL-окне = «мы отказались, а
# конкурент цель забрал» — редкое (~1.4/день) и самое ценное для вскрытия событие; после #121
# каждое такое = либо отданный приз, либо проигранный бэкран (различит panic-probe).
# Канал АРМИРУЕТСЯ ЯВНО в main.py (set_on_liquidate) — по умолчанию закрыт (канон notify_fn).
# Откат REFUSAL_LOSS_ALERT=0 (регистр не пишется, арм в main сам отключается).
REFUSAL_LOSS_ALERT = os.environ.get("REFUSAL_LOSS_ALERT", "1").lower() not in ("0", "false", "no")
REFUSAL_TTL_SEC = float(os.environ.get("REFUSAL_LOSS_TTL_SEC", "600"))
_REFUSAL_REG: dict = {}          # (mid_lower, borrower_lower) -> (ts, bucket, debt_usd)
# Дыра #123, вскрытая каскадом cbXRP 14.08 22:23 (25 ликвидаций в ОДНОМ блоке): индекс
# обрабатывает Liquidate раньше, чем отказ записан в регистр, и одноблочная гонка молчит.
# Фикс — ВСТРЕЧНЫЙ матчинг: вотчер копит недавние чужие победы в кольце, а _note_refusal при
# записи отказа сам смотрит в кольцо (окно REFUSAL_RECENT_WIN_SEC). Кольцо и алерт — сбоку.
REFUSAL_RECENT_WIN_SEC = float(os.environ.get("REFUSAL_RECENT_WIN_SEC", "30"))
_RECENT_LIQ: list = []           # [(ts, mid_lower, borrower_lower, caller, tx)] — кольцо 64
_RL_SINK = None                  # (alerter, cfg) — ставит make_refusal_loss_watcher (явный арм)
# ИНКРЕМЕНТАЛЬНЫЙ HOT-DISPATCH («го» kelbic 07.08, дефект №3 вскрытия каскада 06.08): 11 preps
# по ~0.6с СЕРИЕЙ + один батч-dispatch после всех = first-ready ждал хвост 7.8с. Окно победы
# замерено 0.5-2.2мс — батч-барьер это структурная сдача гонки. ON: готовая цель уходит в
# dispatch НЕМЕДЛЕННО (kill-switch перед каждым сендом; дедуп _RECENT_SENDS исключает дубль),
# хвост цикла продолжает готовить следующих. Теряем кросс-целевую сборку юнитов (киты первыми
# нонсами) — латентность дороже ордеринга (наш промах = +785мс). Откат HOT_DISPATCH_INCREMENTAL=0
# = прежний батч бит-в-бит.
HOT_DISPATCH_INCREMENTAL = os.environ.get("HOT_DISPATCH_INCREMENTAL", "1").lower() not in ("0", "false", "no")
# СТОРОЖ ЖИВОСТИ PREDICT (после самодедлока 23-26.08). Пороги названы от НАБЛЮДЁННОГО:
# пушей оракула ~528/сутки (22/час) при рабочем предикте события идут пачками ежечасно;
# 20 пушей БЕЗ единого события — состояние, которого в норме не бывает. Тишина 30 мин
# отделяет штиль от смерти: в живом штиле события всё равно есть (stale/recovered/bootstrap).
PREDICT_SILENCE_PUSHES = int(os.environ.get("PREDICT_SILENCE_PUSHES", "20"))
PREDICT_SILENCE_SEC = float(os.environ.get("PREDICT_SILENCE_SEC", "1800"))
_PRED_WATCH = {"pushes": 0, "born": time.time()}
# Р1: ПАРАЛЛЕЛЬНАЯ ПОДГОТОВКА ЦЕЛЕЙ ХВОСТА КАСКАДА.
#
# ЗАМЕР. Каскад 11.08 06:38 — 30 ликвидаций в двух блоках (49820471/49820472). Голову забирает
# «стена» (пул >=1133 EOA, 712-796 tx в блок на tip транзита), с ней не конкурируем; ХВОСТ
# (2-я..N-я цель) берёт «объёмщик» мелкими деньгами — 43 события за 10 дней, ниша без стены.
# Цена подготовки ОДНОЙ цели — 632мс, и она СЕТЕВАЯ (eth_call + Kyber-квота; STATE §12.08
# ночь-3), не CPU. Каскад живёт 2-4с => последовательный цикл успевал 6 целей из 26 (замерено
# дрелью test_cascade_tail_parallel.py на этом самом коде), то есть хвост был недостижим.
#
# ПОЧЕМУ ТРЕДЫ, А НЕ ПРОЦЕССЫ: ждём сокет, GIL при этом отпущен. Пул ОДИН на процесс и
# переиспользуется — Thread() на вызов уже пробивал cgroup-кап и ронял hot_loop.
# Откат REACTIVE_PREP_THREADS=1 снимает МЕХАНИЗМ (цели готовятся в вызывающем треде тем же
# циклом), а не сжимает параметр.
REACTIVE_PREP_THREADS = int(os.environ.get("REACTIVE_PREP_THREADS", "8") or 8)
_PREP_THREADS_MAX = 32          # защита от опечатки в env: 500 тредов положат 1-ядерную машину
_PREP_POOL = None
_PREP_POOL_N = 0
_PREP_POOL_LOCK = threading.Lock()


def _prep_threads():
    try:
        return max(1, min(int(REACTIVE_PREP_THREADS), _PREP_THREADS_MAX))
    except Exception:                                        # noqa: BLE001
        return 1                                             # мусор в ручке = прежнее поведение


def _prep_pool(n):
    """Общий пул на процесс; None = пул не поднялся (вызывающий идёт прежним последовательным путём)."""
    global _PREP_POOL, _PREP_POOL_N
    with _PREP_POOL_LOCK:
        if _PREP_POOL is not None and _PREP_POOL_N >= n:
            return _PREP_POOL
        try:
            from concurrent.futures import ThreadPoolExecutor
            _old, _PREP_POOL = _PREP_POOL, ThreadPoolExecutor(max_workers=n,
                                                              thread_name_prefix="hot-prep")
            _PREP_POOL_N = n
            if _old is not None:
                _old.shutdown(wait=False)
        except Exception:                                    # noqa: BLE001
            _PREP_POOL, _PREP_POOL_N = None, 0
        return _PREP_POOL


def run_prep_batch(cand, fn, log=None):
    """Подготовить цели пулом тредов. fn обязана сама ловить свои исключения (см. _prep_one).

    len(cand)==1 или ручка=1 -> ровно прежний последовательный вызов в ТОМ ЖЕ треде: никакого
    пула, никакого фьючерса, поведение бит-в-бит. Отказ пула тоже падает в этот путь — огонь
    не гасим ни при какой поломке параллелизма."""
    if not cand:
        return
    n = _prep_threads()
    pool = _prep_pool(n) if (n > 1 and len(cand) > 1) else None
    if pool is None:
        for c in cand:
            fn(*c)
        return
    futs = []
    try:
        for c in cand:
            futs.append(pool.submit(fn, *c))
    except Exception as e:                                   # noqa: BLE001
        if log is not None:
            log.error("HOT prep-pool submit failed (%s) — хвост готовим последовательно",
                      type(e).__name__)
        for c in cand[len(futs):]:
            fn(*c)
    for f in futs:
        try:
            f.result()
        except Exception:                                    # noqa: BLE001
            pass                                             # fn всё ловит; это страховка от смерти воркера


# Р1: ЗАЯВКА «ОДНА ЦЕЛЬ — ОДНА ПОДГОТОВКА».
#
# Рынок может висеть под ДВУМЯ агрегаторами (multi-leg, 07-23: `agg=` в DIAG — это атрибуция
# ноги-триггера). Обе ноги, транслируя в одном блоке, спавнят по своему _process_transmit и
# видят один и тот же (mid, borrower). Последовательная подготовка держала окно перекрытия
# узким; параллельная растягивает его на весь каскад. Цена дубля названа в коде ниже прямо:
# «2-я tx сожгла бы nonce+газ», плюс 632мс сети на уже оплаченную цель.
# TTL: тред, умерший не сняв заявку, не имеет права запереть заёмщика до перезапуска процесса.
# ВТОРАЯ ЛИНИЯ ОСТАЁТСЯ ПРЕЖНЕЙ и заявку НЕ дублирует: дубль ОТПРАВКИ ловит `_RECENT_SENDS`
# под `_SEND_LOCK` в dispatch_liquidations. Заявка снимается сразу после подготовки (держать её
# до квитанции нельзя — добой той же позиции в следующем блоке законен и обычен).
_PREP_CLAIM = {}
_PREP_CLAIM_LOCK = threading.Lock()
_PREP_CLAIM_TTL = float(os.environ.get("REACTIVE_PREP_CLAIM_TTL_SEC", "20"))


def claim_prep(key, now=None):
    """True = заявка НАША (мы и платим за подготовку). False = цель уже готовит кто-то другой."""
    t = float(now if now is not None else time.monotonic())
    with _PREP_CLAIM_LOCK:
        for k in [k for k, ts in _PREP_CLAIM.items() if t - ts > _PREP_CLAIM_TTL]:
            _PREP_CLAIM.pop(k, None)
        if key in _PREP_CLAIM:
            return False
        _PREP_CLAIM[key] = t
        return True


def release_prep(key):
    with _PREP_CLAIM_LOCK:
        _PREP_CLAIM.pop(key, None)


# Р1/#108: ФИЛЬТР ТРУПОВ ПЕРЕД ДОРОГОЙ ПОДГОТОВКОЙ.
#
# ЗАМЕР #108: 30 из 43 «healthy-race» были ТРУПАМИ — цель ликвидировали до того, как наш сим
# досчитал (fast-lane: 47% ревертов при 0 побед). В каскаде это дороже вдвойне: каждая мёртвая
# цель съедает 632мс из 4-секундного бюджета, то есть отнимает ЖИВУЮ цель хвоста.
#
# Источник — фоновый индекс цепи (#110, в бою с 14.08): он уже прочитал блоки и знает события
# Liquidate Morpho. Горячий путь только ЧИТАЕТ его память — НИ ОДНОГО сетевого вызова.
#
# ВЕРДИКТ ВЫНОСИМ ТОЛЬКО ДОКАЗУЕМЫЙ. 88.7% ликвидаций частичные, и частичная HF не чинит
# (repaid r со сейзом r*(1+lif) оставляет HF<1 — потому добой в два блока и обычен), поэтому
# «видели Liquidate» трупом НЕ является. Труп — два случая:
#   (1) списан безнадёжный долг (bad_debt_shares > 0): позиция в Morpho обнуляется целиком;
#   (2) погашено не меньше, чем ВЕСЬ borrow_shares нашего снапшота.
# ЧЕСТНЫЙ ПРЕДЕЛ правила (2): если наш снапшот СВЕЖЕЕ индекса (уже после добоя), remainder
# меньше repaid и мы пропустим ещё живую цель. Промах требует одновременно свежего снапшота И
# погашения >=50% — то есть цели, которую конкурент жуёт ПРЯМО СЕЙЧАС; это ровно та гонка,
# которую #108 замерил как 30 проигрышей из 43. Промах направлен в сторону замера, не против.
# Незнание (индекса нет / протух / пары нет) НЕ блокирует: незнание как ЗАПРЕТ гасило бы огонь.
REACTIVE_CORPSE_FILTER = os.environ.get("REACTIVE_CORPSE_FILTER", "1").lower() not in ("0", "false", "no")
_CORPSE_UNKNOWN_LOG_TS = 0.0
_CORPSE_UNKNOWN_LOG_GAP = 60.0


def corpse_pairs(stats=None, log=None):
    """{(mid, borrower): (repaid_shares, bad_debt_shares)} из ПАМЯТИ индекса, либо None = НЕ ЗНАЮ.

    Зовём ОДИН раз на рынок, а не на цель: в каскаде это одна копия словаря пар вместо 26."""
    global _CORPSE_UNKNOWN_LOG_TS
    if not REACTIVE_CORPSE_FILTER:
        return None
    why = ""
    try:
        idx = _chain_index.get_index()
        if idx is None:
            why = "индекса нет (CHAIN_INDEX=0 либо узел не задан)"
        else:
            p = idx.corpses()
            if p is None:
                why = "индекс протух (fresh=False)"
            else:
                # ФОРМА КЛЮЧА (канон hx() в индексе): у заёмщика ДВА источника и ДВЕ формы —
                # Morpho API отдаёт checksum-адрес (он и лежит в shared.groups), лог цепи —
                # нижний регистр. Несимметричная нормализация даёт множества, которые НЕ
                # ПЕРЕСЕКАЮТСЯ НИКОГДА: фильтр был бы структурным нулём, а «corpse=0» читалось
                # бы как «трупов не было». Нормализуем ОБЕ стороны, а не одну.
                return {(str(k[0]).lower(), str(k[1]).lower()): v for k, v in p.items()}
    except Exception as e:                                   # noqa: BLE001
        why = "%s: %s" % (type(e).__name__, redact_clip(e, 60))
    if stats is not None:
        stats["f_corpse_unk"] = stats.get("f_corpse_unk", 0) + 1
    _n = time.monotonic()
    if log is not None and _n - _CORPSE_UNKNOWN_LOG_TS > _CORPSE_UNKNOWN_LOG_GAP:
        _CORPSE_UNKNOWN_LOG_TS = _n
        log.info("HOT corpse-filter: НЕ ЗНАЮ (%s) — фильтр пропущен, цели готовим как раньше", why)
    return None


# ОГРЫЗОК ПОСЛЕ НАШЕГО CAP-ВЫСТРЕЛА (20.08). Кап-ветка забирает почти весь залог и оставляет
# пыль: позиция ПЕРЕЖИВАЕТ выстрел (долг остаётся, залога нет) и дальше вечно жжёт подготовку —
# греется, готовится, ревертит. Прежние два признака трупа её не берут: bad_debt=0 (Morpho
# ничего не списал), repaid < borrow_shares (мы просили меньше долга ПО ПОСТРОЕНИЮ кап-ветки).
#
# ПОРОГ — ИЗ РАСПРЕДЕЛЕНИЯ НАШЕГО ЖЕ ШЕЙВА, а не с потолка: кап целится в collateral минус
# max(ASK_SHAVE_BPS, живой дрейф) плюс 1-3 единицы округления => остаток НАШЕГО выстрела
# заведомо <= ~10 bps исходного залога. Берём вдвое (20 bps) — и это на порядки ниже остатка
# ЧУЖОЙ частичной ликвидации (88.7% ликвидаций частичные, типичный остаток — десятки процентов).
#
# ПРАВИЛО ПРИМЕНЯЕТСЯ ТОЛЬКО К НАШИМ ПОДТВЕРЖДЁННЫМ CAP-ВЫСТРЕЛАМ. Чужая частичная ликвидация
# с малым остатком трупом НЕ объявляется: ложный труп = отдать живую цель без боя, и это
# дороже лишней подготовки. Поэтому нужны ОБА факта: (1) мы строили кап-план на эту цель и
# знаем её предвыстрельный залог; (2) наш выстрел ПОДТВЕРЖДЁН (квитанция/лог протокола).
CAP_CORPSE_MAX_BPS = float(os.environ.get("HOT_CAP_CORPSE_MAX_BPS", "20"))
CAP_CORPSE_TTL_SEC = float(os.environ.get("HOT_CAP_CORPSE_TTL_SEC", "86400"))
# СВЕЖЕСТЬ НАМЕРЕНИЯ (блокер линзы, 20.08). Кап-план строится при КАЖДОЙ сборке, а гонки мы в
# основном проигрываем — значит намерения копятся, и без срока годности ЛЮБОЙ поздний выстрел по
# той же цели превратил бы многодневный снимок залога в «факт». Тогда живая цель, чей залог с тех
# пор изменился на порядок, хоронится по ЧУЖОМУ масштабу — ровно тот ложный труп, которого мы
# боимся больше лишней подготовки. 300с: снимок и выстрел в одном окне (окно spec-fire ~90с,
# реактивный путь — секунды), всё, что старше, — не про этот выстрел.
CAP_PLAN_FRESH_SEC = float(os.environ.get("HOT_CAP_PLAN_FRESH_SEC", "300"))
_CAP_PLANS: dict = {}      # (mid,borrower) -> (coll_before, ts): кап-план ПОСТРОЕН
_CAP_SHOTS: dict = {}      # (mid,borrower) -> (coll_before, ts): наш кап-выстрел ПОДТВЕРЖДЁН
_CAP_LOCK = threading.Lock()


def _cap_key(mid, borrower):
    return (str(mid).lower(), str(borrower).lower())


def note_cap_plan(mid, borrower, coll_before, now=None):
    """Кап-план ПОСТРОЕН: запоминаем предвыстрельный залог. Ещё не выстрел — только намерение."""
    try:
        c = int(coll_before)
    except Exception:                                        # noqa: BLE001
        return
    if c <= 0:
        return
    with _CAP_LOCK:
        _CAP_PLANS[_cap_key(mid, borrower)] = (c, float(now if now is not None else time.time()))


def note_cap_shot_confirmed(mid, borrower, now=None) -> bool:
    """НАШ выстрел по этой цели подтверждён (квитанция/лог Morpho с нашим caller). Переводит
    намерение в факт. Возврат True = цель была кап-планом (иначе ничего не делаем: обычный
    выстрел огрызка не оставляет)."""
    k = _cap_key(mid, borrower)
    n = float(now if now is not None else time.time())
    with _CAP_LOCK:
        # ЧИСТКА ОБОИХ РЕЕСТРОВ. Намерения копятся быстрее фактов (гонки мы в основном
        # проигрываем), и именно непочищенный `_CAP_PLANS` был механизмом ложного трупа.
        for _k in [x for x, v in _CAP_PLANS.items() if n - v[1] > CAP_PLAN_FRESH_SEC]:
            del _CAP_PLANS[_k]
        for _k in [x for x, v in _CAP_SHOTS.items() if n - v[1] > CAP_CORPSE_TTL_SEC]:
            del _CAP_SHOTS[_k]
        rec = _CAP_PLANS.pop(k, None)
        if rec is None:
            return False
        if n - rec[1] > CAP_PLAN_FRESH_SEC:
            return False        # намерение протухло: этот выстрел — не про тот снимок залога
        _CAP_SHOTS[k] = (rec[0], n)
        return True


def cap_corpse(key, collateral, now=None):
    """Огрызок ПОСЛЕ НАШЕГО подтверждённого кап-выстрела? PURE относительно цепи (только память).

    Возврат (труп?, доля остатка в bps | None)."""
    if key is None or collateral is None:
        return False, None
    n = float(now if now is not None else time.time())
    with _CAP_LOCK:
        rec = _CAP_SHOTS.get(_cap_key(key[0], key[1]))
    if rec is None or n - rec[1] > CAP_CORPSE_TTL_SEC:
        return False, None
    before = int(rec[0])
    if before <= 0:
        return False, None
    left_bps = int(collateral) * 10_000.0 / before
    return (left_bps <= CAP_CORPSE_MAX_BPS), left_bps


def corpse_verdict(row, borrow_shares, key=None, collateral=None):
    """PURE. True = ДОКАЗАННЫЙ труп. row = (repaid_shares, bad_debt_shares) из индекса либо None.

    Третий признак (20.08): огрызок ПОСЛЕ НАШЕГО кап-выстрела — см. `cap_corpse`. Он не
    требует row вовсе: индекс о нём ничего особенного не знает (bad_debt=0, repaid<bs), факт
    целиком наш."""
    if key is not None and collateral is not None and cap_corpse(key, collateral)[0]:
        return True
    if not row:
        return False
    try:
        repaid, bad = int(row[0]), int(row[1])
        bs = int(borrow_shares) if borrow_shares is not None else 0
    except Exception:                                        # noqa: BLE001
        return False
    if bad > 0:
        return True
    return bs > 0 and repaid >= bs


_SIM_TRANSPORT_MARKS = ("429", "Too Many Requests", "timed out", "timeout", "Connection",
                        "Max retries", "502", "503", "504")


def _sim_transport_err(err: str) -> bool:
    """Транспортная ошибка сима (не реверт контракта) -> кандидат на дочитку confirmed."""
    e = err or ""
    return any(m in e for m in _SIM_TRANSPORT_MARKS)


_PRECONF_BUDGET_LOCK = threading.Lock()
_PRECONF_LAST_REACTIVE = -1e9
_PRECONF_LAST_POLL = -1e9


def preconf_mark_reactive(now=None):
    """Реактивный путь занял канал preconf — полл обязан уступить."""
    global _PRECONF_LAST_REACTIVE
    with _PRECONF_BUDGET_LOCK:
        _PRECONF_LAST_REACTIVE = time.monotonic() if now is None else now


_PX_PRE_OK = 0        # реактивные чтения цены: preconf успел (фора взята)
_PX_PRE_FAIL = 0      # ...не успел/429 -> отдали confirmed (фора потеряна) — ЭТО и есть датчик
                      # деградации после того, как полл перестал жечь бакет (poll_fb -> ~0).


def px_preconf_note(ok: bool):
    global _PX_PRE_OK, _PX_PRE_FAIL
    with _PRECONF_BUDGET_LOCK:
        if ok:
            _PX_PRE_OK += 1
        else:
            _PX_PRE_FAIL += 1


def px_preconf_take():
    """(ok, fail) за окно + сброс — для hot stats/алерта."""
    global _PX_PRE_OK, _PX_PRE_FAIL
    with _PRECONF_BUDGET_LOCK:
        o, f = _PX_PRE_OK, _PX_PRE_FAIL
        _PX_PRE_OK = _PX_PRE_FAIL = 0
        return o, f


def preconf_poll_allowed(now=None) -> bool:
    """Можно ли фоновому поллу тратить preconf-бюджет. Чистая при переданном now (юнит)."""
    global _PRECONF_LAST_POLL
    t = time.monotonic() if now is None else now
    with _PRECONF_BUDGET_LOCK:
        if t - _PRECONF_LAST_REACTIVE < PRECONF_REACTIVE_QUIET:
            return False
        if t - _PRECONF_LAST_POLL < PRECONF_POLL_MIN_GAP:
            return False
        _PRECONF_LAST_POLL = t
        return True


HOT_THROTTLE_SEC = 0.25   # min seconds between hot-path spawns PER aggregator. The bare match fires on
                          # frequent price-READ txs; this bounds preconf-RPC reads + thread spawns.
                          # Transmit cadence is minutes, so 0.25s loses no real update; raise if the
                          # public preconf endpoint rate-limits (a throttled/None read simply skips).
HOT_STATS_SEC = 120       # emit hot-path counters (spawn + gate outcomes) every N seconds. Lets us see
                          # detection is ALIVE without waiting for a flip (the gate is silent on readers).

# 2a: idToMarketParams is immutable per market for its whole life. resolve_meta() already decodes
# it fully but only kept (oracle, lltv); this cache keeps loan/coll/irm too so prepare_hot can skip
# its own confirmed-RPC re-fetch of the same data. Module-level by design (not threaded through
# meta/_process_transmit/kwargs) to avoid touching the existing meta shape used by the gate-check
# and _process_transmit's unpacking — zero blast radius outside resolve_meta/prepare_hot.
_FULL_PARAMS_CACHE: dict = {}

# Ground-truth полного горячего набора (A3): journal получает только счётчик (hotset n=.. src=..),
# сам набор (borrower:bs пары) пишется сюда — сводить по адресу ликвидированного на каскаде разводит
# L (в дампе с bs=0) / B (отсутствует) / A (ok=False в drop-логе) / A' (src=confirmed). Дёшево на диск,
# не топит journal в каскаде (pspawn до 145). Ротацию файла — на владельце (logrotate/ручной rm).
_HOTSET_LOG_PATH = "/root/liquidator/hotset.log"


def _dump_hotset(mid, src, tba, tbs, pos, dropped):
    """Append одной строки: block-набор borrower:bs (+ dropped ok=False адреса). Best-effort, не
    роняет горячий путь при ошибке записи (диск полон/права)."""
    try:
        parts = " ".join("%s:%d" % (b[:10], bs) for (b, bs, _col) in pos)
        drp = (" DROPPED=" + ",".join(d[:10] for d in dropped)) if dropped else ""
        with open(_HOTSET_LOG_PATH, "a") as _f:
            _f.write("%.3f mid=%s src=%s n=%d tba=%d tbs=%d | %s%s\n"
                     % (time.time(), mid[:10], src, len(pos), tba, tbs, parts, drp))
    except Exception:
        pass


# ---- pure helpers (unit-tested) ----

_BF_HIST: dict = {}          # bn -> fb-baseFee (последние ~16 блоков)
_BF_LAST_LOG = 0.0           # троттл лога 60с
_BF_GAPS = 0                 # разрывы bn между fb-записями с последнего лога (= пропуски index-0)
_BF_PREV_BN = None
_BF_LATEST = None            # (bn, fb_wei, monotonic_ts) — последний index-0 baseFee (SEND-2)


def _fresh_basefee(max_age_sec=6.0):
    """SEND-2 гард свежести: fb-baseFee, если последний index-0 кадр не старше 3 блоков (6с),
    иначе None -> prep не несёт fee -> send_tx идёт старым Alchemy-путём. Защита от «фид жив,
    кадры не идут» (gap60s-класс: 0.017% в покое, каскад неизвестен — watch-item). Пишет слот
    фид-петля, читает тред prepare_hot: подмена кортежа атомарна (GIL), гонка максимум на один
    кадр ~200мс — гард переваривает."""
    if _BF_LATEST is None:
        return None
    _bn, fb, ts = _BF_LATEST
    if time.monotonic() - ts > max_age_sec:
        return None
    return fb


def _basefee_probe(*, bn, fb, rpc, log):
    """5a (LOG-ONLY, перед SEND-2): fb-baseFee из flashblock-хедера vs Alchemy get_block(N) по
    НОМЕРУ блока (лаг 3 — блок к этому моменту подтверждён) — тот же аксессор, что в send_tx
    (execute.py get_block().baseFeePerGas). SEND-2 ПЕРЕНОСИТ send-путь на fb-источник; этот замер
    решает: чисто (eq=True, gap~0) -> SEND-2 как спроектирован; иначе -> fallback max(fb,last_alch)
    или fb x1.25. gap60s = пропущенные index-0 кадры (недоступность источника). Вызывается только
    из фид-петли (один тред) -> глобалы без лока; сравнение — в daemon-треде (Alchemy-RTT вне петли).
    После разрыва bn лог откладывается, пока tgt в дыре (счётчик gap не теряется) — юнит."""
    global _BF_LAST_LOG, _BF_GAPS, _BF_PREV_BN, _BF_LATEST
    if _BF_PREV_BN is not None and bn > _BF_PREV_BN + 1:
        _BF_GAPS += bn - _BF_PREV_BN - 1
    _BF_PREV_BN = bn
    _BF_HIST[bn] = fb
    _BF_LATEST = (bn, fb, time.monotonic())
    for k in [k for k in _BF_HIST if k < bn - 16]:
        _BF_HIST.pop(k, None)
    now = time.monotonic()
    if now - _BF_LAST_LOG < 60.0:
        return
    tgt = bn - 3
    fb_t = _BF_HIST.get(tgt)
    if fb_t is None:                 # прогрев (<3 блоков) / пропуск tgt: троттл не жжём, повтор блоком позже
        return
    _BF_LAST_LOG = now
    gaps = _BF_GAPS; _BF_GAPS = 0
    def _cmp():
        alch = None
        try:
            alch = int(rpc._web3().eth.get_block(tgt).get("baseFeePerGas") or 0)
        except Exception:
            pass
        log.info("DIAG basefee bn=%d fb=%s alch=%s eq=%s gap60s=%d",
                 tgt, fb_t, alch, (fb_t is not None and alch is not None and fb_t == alch), gaps)
    threading.Thread(target=_cmp, daemon=True).start()


_SHADOW_SCALE = {"cbXRP": 10**28, "cbDOGE": 10**26, "cbADA": 10**28,   # immutable оракулов (STATE Идея 2)
                 "cbLTC": 10**26,   # P1-8, ИСПРАВЛЕНО 07-08: STATE-строка «цена cbADA/cbLTC ×1e28» была
                                    # НЕВЕРНА для cbLTC — живой shadow-чек дал match=False ровно 100×
                                    # (px_preconf/answer=1e26), decimals с чейна: cbLTC=8 (как cbDOGE→1e26),
                                    # cbADA=6→1e28. Формула: 1e36·10^(loanDec−collDec)/1e8_answer.
                 "cbBTC": 10**26}   # CBBTC_PLAN 07-09: SCALE_FACTOR оракула = 1e26, подтверждён БИТ-В-БИТ
                                    # (answer*1e26 == price(), остаток 0). Гейт: первый live shadow-чек
                                    # match=False -> убрать строку немедленно (poll-путь безопасен без неё).


def _build_shadow_map(feeds, meta, markets, primary=None):
    """{aggregator_lower: (symbol, scale, oracle)} ТОЛЬКО для одно-фид money-маркетов (SCALE известен).
    Чистая (юнит). Ротацию агрегатора закрывает часовой re-resolve, пересобирающий карту вместе с
    feeds/meta; между ротацией и пересборкой shadow просто перестаёт матчить (log-only, безвредно).
    CBBTC_PLAN: ТОЛЬКО loan=USDC — _SHADOW_SCALE выведен для USD-quote (answer*scale==price
    USD-оракула); один агрегатор могут делить рынки с РАЗНЫМ quote (cbBTC/USDC и cbBTC/EURC оба
    резолвятся в cbBTC/USD-фид), и посадить USD-scale на EURC-оракул = неверный px_hint +
    вечный match=False. Не-USDC рынки остаются на poll-детекции (как и было).
    07-23 multi-leg: рынок числится и под extra-ногами (BF2/QF1/QF2) — membership под ЧУЖИМ agg
    не должен получать наш _SHADOW_SCALE (cbBTC-scale на ожившей USDC/USD-ноге в депег = ложный
    px_hint в feed-спавн). primary = feeds.primary_map() ({mid: BF1-agg}); None = до-multi-leg
    поведение (тесты/офлайн)."""
    out = {}
    by_id = {m.market_id: m for m in markets}
    for agg, mids in feeds.items():
        for mid in mids:
            if primary is not None and primary.get(mid) != agg:
                continue                     # extra-нога: scale чужого BF1-фида сюда не сажаем
            m = by_id.get(mid); om = meta.get(mid)
            if not m or not om:
                continue
            if getattr(m, "loan_token", "") != "USDC":
                continue
            scale = _SHADOW_SCALE.get(getattr(m, "collateral_token", None))
            if scale:
                out[agg] = (m.collateral_token, scale, om[0])
                break
    return out


def _shadow_feed_check(*, sym, agg, answer, scale, oracle, preconf_rpc, price_fn, t0, log):
    """SHADOW слоя 1 (план п.4): цена, декодированная ИЗ ФИДА (~0.7мс), против цены оракула на
    preconf (~350мс RTT) на том же транзите. ЛОГ-ONLY — не действует, ничего не спавнит дальше.
    px_preconf изредка может быть СТАРОЙ ценой (чтение обогнало применение pending) — benign-гонка,
    НЕ ошибка декодера; ошибка декодера = px_feed не равен НИ старой, НИ новой цене оракула.
    Правило флипа PRICE_FROM_FEED — в STATE (Идея 2, слой 1)."""
    px_feed = answer * scale
    px_pre = None
    try:
        px_pre = price_fn(preconf_rpc, oracle)
    except Exception:
        px_pre = None
    log.info("DIAG shadow-feed mkt=%s agg=%s answer=%d px_feed=%d px_preconf=%s match=%s dt=%.0fms",
             sym, agg[:10], answer, px_feed, px_pre,
             px_pre is not None and px_pre == px_feed, (time.perf_counter() - t0) * 1000.0)


def _feed_spawn_gate(oracle, bn, feed_handled):
    """PURE (юнит): ровно ОДИН feed-спавн на (oracle, bn). Тот же transmit-tx появляется в
    нескольких флэшблок-суб-кадрах -> shadow-декод раньше спавнил _process_transmit на каждом
    (наблюдалось 2 спавна с dt=3мс, 07-08 20:18). До fast-lane это смягчал sim; без sim каждый
    реальный флип давал бы 2-3 слепые отправки. Возврат True = спавнить (и (oracle,bn) помечен);
    False = дубль, не спавнить. feed_handled чистится на смене блока (как раньше)."""
    if (oracle, bn) in feed_handled:
        return False
    feed_handled.add((oracle, bn))
    return True


def _price_moved(agg, px, last_price):
    """True if px differs from the last preconf price seen for agg (or first sighting); updates
    last_price. The bare aggregator match also fires on price-READ txs, so this gates the heavy
    multicall to REAL transmits: a reader AFTER a transmit returns the new price (delta -> proceed);
    a reader with no transmit returns the same price (skip). px is None (unreadable) -> skip."""
    if px is None:
        return False
    prev = last_price.get(agg)
    last_price[agg] = px
    return prev is None or px != prev


def _flips(price, lltv_wad, tba, tbs, positions):
    """PURE flip recompute on the preconf price — identical to the armed assess (chain.simulate.health_from).
    positions: [(borrower, borrow_shares, collateral, debt_usd)] -> [(borrower, HealthReport, debt_usd)]."""
    from chain.simulate import MarketContext, health_from
    ctx = MarketContext(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                        total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
    out = []
    for (b, bs, col, du) in positions:
        hr = health_from(ctx, int(bs), int(col))
        if hr.liquidatable:
            out.append((b, hr, du))
    return out


# ---- каскадный tip-режим (авто-подъём ступени в ДАВКУ) ----
# FCFS-рутина позицией за tip не платит (канарейка 23.07: +7..+36 in-block на 0.05 gwei), НО в
# каскадную давку победители бидят 22-59 gwei (фэт-разведка 45д, блоки 47803xxx: оператор 56.7 gwei
# -> idx 1154 против своих же 0.0125 -> idx 1171) = fee-сортировка ВНУТРИ флешблок-кадра при заторе.
# Держать высокий tip постоянно = жечь ~$26/выстрел зря (страховка ~$0.87/gwei на 500k*1730). Поэтому
# ступени поднимаются на ОДНУ ВРЕМЕННО, когда давка реальна СЕЙЧАС: (а) >=CASCADE_MIN_XMITS разных
# агрегаторов транслировали в одном блоке (широкое движение) ИЛИ (б) submit-xray зафиксировал проигрыш
# победителю с tip ВЫШЕ нашего (fee-сортировка наблюдалась). Оба армят TTL-окно эскалации.
CASCADE_TTL_SEC = float(os.environ.get("CASCADE_TTL_SEC", "45"))       # окно эскалации после сигнала давки
CASCADE_MIN_XMITS = int(os.environ.get("CASCADE_MIN_XMITS", "2"))       # разных агрегаторов в блоке = давка
_CASCADE_LOCK = threading.Lock()
_CASCADE_UNTIL = 0.0            # monotonic-дедлайн: пока now < него — поднимаем ступень на одну
_CASCADE_REASON = ""
_XMIT_BLOCK = None             # блок, для которого считаем распределённые транзиты
_XMIT_AGGS: set = set()        # разные агрегаторы-транзиты в _XMIT_BLOCK


def note_transmit_for_cascade(agg, block, now=None, log=None):
    """Вызывается на КАЖДЫЙ реальный транзит. >=CASCADE_MIN_XMITS разных агрегаторов в ОДНОМ блоке =
    широкая давка -> взвести эскалацию на CASCADE_TTL_SEC. Возврат True = давка взведена этим вызовом."""
    global _XMIT_BLOCK, _XMIT_AGGS, _CASCADE_UNTIL, _CASCADE_REASON
    now = time.monotonic() if now is None else now
    with _CASCADE_LOCK:
        if block != _XMIT_BLOCK:
            _XMIT_BLOCK, _XMIT_AGGS = block, set()
        _XMIT_AGGS.add(agg)
        if len(_XMIT_AGGS) >= CASCADE_MIN_XMITS and now >= _CASCADE_UNTIL - CASCADE_TTL_SEC + 1.0:
            _CASCADE_UNTIL = now + CASCADE_TTL_SEC
            _CASCADE_REASON = "%d транзитов в блоке %s" % (len(_XMIT_AGGS), block)
            if log:
                log.info("DIAG cascade ARM (%s) -> tip +1 ступень на %.0fс", _CASCADE_REASON, CASCADE_TTL_SEC)
            return True
    return False


def note_race_loss_for_cascade(winner_tip_gwei, our_tip_gwei, now=None, log=None):
    """submit-xray нашёл победителя. Если его tip ВЫШЕ нашего — fee-сортировка реальна сейчас ->
    взвести эскалацию. tip'ы в gwei (float). Возврат True = давка взведена."""
    global _CASCADE_UNTIL, _CASCADE_REASON
    try:
        wt, ot = float(winner_tip_gwei), float(our_tip_gwei)
    except (TypeError, ValueError):
        return False
    if wt <= ot:
        return False
    now = time.monotonic() if now is None else now
    with _CASCADE_LOCK:
        _CASCADE_UNTIL = now + CASCADE_TTL_SEC
        _CASCADE_REASON = "проигрыш tip=%.4f>%.4f" % (wt, ot)
        if log:
            log.info("DIAG cascade ARM (%s) -> tip +1 ступень на %.0fс", _CASCADE_REASON, CASCADE_TTL_SEC)
    return True


def cascade_active(now=None):
    """True, пока живо TTL-окно эскалации."""
    now = time.monotonic() if now is None else now
    with _CASCADE_LOCK:
        return now < _CASCADE_UNTIL


# ---- WS-handoff в spec-fire (разбор pending-детекта 06.08) ----
# Транзит, увиденный recv-петлёй в формируемом блоке, — самый ранний сигнал в процессе
# (~200мс суб-кадры). Хук отдаёт его spec-fire: армленное окно с готовой пробой стреляет
# вне конвейера реактива (see spec_fire_live.note_ws_transmit). Модульный шов по образцу
# failover.set_windows_probe; отсутствие хука = поведение до 06.08.
_WS_TRANSMIT_HOOK = None


def set_ws_transmit_hook(fn):
    """fn(mids, src) — вызывается на каждый детект транзита из _process_transmit."""
    global _WS_TRANSMIT_HOOK
    _WS_TRANSMIT_HOOK = fn


_TIP_LADDER_LEGACY = os.environ.get("TIP_LADDER_LEGACY", "0").lower() in ("1", "true", "yes")


# ============ Ш28: ИЗМЕРЕННЫЙ КЛАСС tip ДЛЯ БОЕВОГО ПУТИ ============
#
# ЖИВАЯ ГОНКА 11.08 14:51, вскрытие по цепи. Победитель: блок 49835262, idx 309, tip
# 1 600 000 wei — РОВНО доминирующий класс блока (75.2% транзакций там же). Мы: блок 49835264
# (на ДВА блока позже), idx 99, tip 500 000 000 wei = 0.5 gwei = **312 крат поля**.
# Приз: три цели, крупнейшая $15 594 погашения при net >= $207. Наш итог: −$0.11 и −$0.47×2.
#
# То есть в денежном тракте мы делали ровно то, за что уже заплатили дефектом #91, только
# грубее: переплата не покупает место, она телепортирует в ГОЛОВУ суб-блока — туда, где цена
# ещё старая и позиция здорова. Пробы мы сегодня в класс привели (Ш24), боевой путь — нет.
#
# Канон STATE «tip места не покупает» верен МЕЖДУ суб-блоками и ровно поэтому лестницу
# сплющили 31.07. Но остаток 0.05/0.5 gwei внутри суб-блока продолжает нас выносить.
#
# КЛАСС ИЗМЕРЯЕТСЯ, А НЕ УГАДЫВАЕТСЯ: eth_feeHistory p50 сверен с модой полного блока до
# последней цифры на четырёх известных блоках (Ш24-А). Живёт значение медианно ~2 секунды,
# поэтому держим его свежим фоном, а горячий путь только ЧИТАЕТ кэш — ни одного сетевого
# вызова в момент решения.
_DOM_TIP_WEI = 0
_DOM_TIP_TS = 0.0
_DOM_TIP_LOCK = threading.Lock()
_DOM_TIP_TTL = float(os.environ.get("COMBAT_TIP_TTL_SEC", "45"))
_DOM_TIP_POLL = float(os.environ.get("COMBAT_TIP_POLL_SEC", "10"))
# Границы здравого смысла: ниже пола рынка не платим (не сядем), выше — уже переплата, ради
# которой всё и затевалось. Замер 5407 блоков: класс жил в 1.0-1.8M, выбросы до 17.7M.
_DOM_TIP_MIN = int(os.environ.get("COMBAT_TIP_MIN_WEI", "1000000"))
_DOM_TIP_MAX = int(os.environ.get("COMBAT_TIP_MAX_WEI", "20000000"))
_COMBAT_TIP_LEGACY = os.environ.get("COMBAT_TIP_LEGACY", "0") == "1"
_DOM_TIP_THREAD = None


def note_dominant_tip(wei, now=None):
    """Записать измеренный класс. Значения вне здравых границ ОТБРАСЫВАЕМ, а не клэмпим:
    клэмп превратил бы аномалию узла в «правдоподобную» цену на денежном пути."""
    global _DOM_TIP_WEI, _DOM_TIP_TS
    try:
        w = int(wei)
    except Exception:                                        # noqa: BLE001
        return False
    if not (_DOM_TIP_MIN <= w <= _DOM_TIP_MAX):
        return False
    with _DOM_TIP_LOCK:
        _DOM_TIP_WEI = w
        _DOM_TIP_TS = float(now if now is not None else time.monotonic())
    return True


def dominant_tip_wei(now=None):
    """Свежий измеренный класс или 0. НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: протухшее значение не
    возвращаем вовсе, вызывающий откатывается на прежнее поведение."""
    with _DOM_TIP_LOCK:
        w, ts = _DOM_TIP_WEI, _DOM_TIP_TS
    if not w:
        return 0
    n = float(now if now is not None else time.monotonic())
    return w if (n - ts) <= _DOM_TIP_TTL else 0


def start_tip_sampler(url, log=None):
    """Фоновый замер класса. Ходит НЕ по боевому ключу — это диагностика, а не деньги."""
    global _DOM_TIP_THREAD
    if _DOM_TIP_THREAD is not None or not url:
        return
    import json as _json
    import urllib.request as _u

    def _loop():
        while True:
            try:
                body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_feeHistory",
                                    "params": [12, "latest", [50]]}).encode()
                rq = _u.Request(url, data=body, headers={"content-type": "application/json",
                                                         "User-Agent": "curl/8.5"})
                with _u.urlopen(rq, timeout=8) as r:
                    j = _json.loads(r.read())
                rw = (j.get("result") or {}).get("reward") or []
                vals = [int(x[0], 16) for x in rw if x]
                if vals:
                    vals.sort()
                    note_dominant_tip(vals[len(vals) // 2])
            except Exception:
                pass                                          # молча: это не денежный путь
            time.sleep(_DOM_TIP_POLL)

    _DOM_TIP_THREAD = threading.Thread(target=_loop, daemon=True, name="tip-class")
    _DOM_TIP_THREAD.start()
    if log:
        log.info("TIP-CLASS: замер класса поднят (каждые %.0fс, TTL %.0fс)",
                 _DOM_TIP_POLL, _DOM_TIP_TTL)


def dynamic_tip_gwei(cfg, debt_usd, now=None):
    """PURE-ish (now инжектируем). ЛЕСТНИЦА СПЛЮЩЕНА (31.07, решение владельца по гонке 14:07):
    FCFS обслуживает по прибытию — tip места не покупает (наши 30 gwei легли блоком ПОЗЖЕ
    победителя с 0.0034), ступени по размеру приза страховали несуществующий риск: страховка
    ~$26 на big, промах $6.45 вместо $0.02, а net-гейт душил мелкие призы этой ценой. Теперь
    всегда base; одна ступень tip_step_gwei (~$0.4 на выстрел) — ТОЛЬКО в активную каскад-давку,
    где fee-сортировка доказуемо реальна (>=2 агрегаторов в блоке или проигрыш победителю с tip
    выше нашего). Сэкономленное — в бюджет spec-проб: недостающий блок покупается плотностью
    предочереди, не чаевыми. Откат: TIP_LADDER_LEGACY=1 = прежняя (base,mid,big)+1-в-давку."""
    base = float(cfg.tip_gwei)
    if _TIP_LADDER_LEGACY:
        du = float(debt_usd or 0.0)
        mid = float(getattr(cfg, "tip_mid_gwei", 2.0))
        big = float(getattr(cfg, "tip_big_gwei", 30.0))
        if du >= getattr(cfg, "tip_big_repaid_usd", 20000.0):
            stage = 2
        elif du >= getattr(cfg, "tip_mid_repaid_usd", 2000.0):
            stage = 1
        else:
            stage = 0
        if cascade_active(now):
            stage = min(stage + 1, 2)
        return (base, mid, big)[stage]
    # Ш28: платим ИЗМЕРЕННЫЙ класс — и в каскаде тоже. Победитель гонки 14:51 стоял ровно на
    # классе (1 600 000 wei) ВО ВРЕМЯ каскада и взял приз, а наши 0.5 gwei вынесли нас в голову
    # блока. Ступень «в давку» страховала риск, которого нет: место покупается позицией
    # относительно транзита, а не размером чаевых.
    _dom = dominant_tip_wei(now)
    if _dom and not _COMBAT_TIP_LEGACY:
        return _dom / 1e9
    # НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: класс не измерен или протух -> прежнее поведение бит-в-бит.
    if cascade_active(now):
        return max(base, float(getattr(cfg, "tip_step_gwei", 0.5)))
    return base


def _net_gate(profit_wei, debt_usd, debt_assets, cfg):
    """PURE: profit/cost/net in USD — как floor в prepare_liquidation, но cost по ДИНАМИЧЕСКОМУ
    tip: крупный приз платит страховой tip, и гейт обязан видеть эту цену."""
    profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
    cost_usd = cfg.gas_limit_est * dynamic_tip_gwei(cfg, debt_usd) * cfg.eth_price_usd / 1e9
    return profit_usd, cost_usd, profit_usd - cost_usd


# ============ Р1б/#120: TIP ХВОСТА — ОТДЕЛЬНЫЙ РЕЖИМ ============
#
# Замер суб-блоков 49965023 (14.08): flashblocks публикует суб-блок ПОСЛЕ закрытия ⇒ весь
# реактивный конвейер (detect -> prepare 632мс -> dispatch) прибывает в ПОЗДНИЕ суб-блоки,
# где новая цена уже действует. Опасность Ш28 («переплата телепортирует в голову суб-блока
# со старой ценой») к этому пути НЕ применима по построению — она про суб-блок ТРАНЗИТА,
# в который реакция физически не входит. А класс 0.001-0.02 gwei проигрывает сортировку
# хвоста реактору (~4 gwei, 35% событий) ВСЕГДА: внутри суб-блока прибытия tip покупает ранг.
#
# Tip хвоста платим ОТ ЭКОНОМИКИ ЦЕЛИ (канон «вход гейта — от субъекта»): не выше
# REACTIVE_TAIL_TIP_GWEI (класс реактора), не выше доли REACTIVE_TAIL_TIP_FRAC от gross-приза
# (мелкий хвост не переплачивает за гонку, которую при живом реакторе всё равно проиграет),
# не ниже измеренного класса рынка (пол). Гейт net считает cost ТЕМ ЖЕ tip — гейт и tx не
# расходятся. Откат REACTIVE_TAIL_TIP_GWEI=0 — прежнее поведение бит-в-бит.
_TAIL_TIP_GWEI = float(os.environ.get("REACTIVE_TAIL_TIP_GWEI", "4.0") or 0.0)
_TAIL_TIP_FRAC = float(os.environ.get("REACTIVE_TAIL_TIP_FRAC", "0.25"))


def tail_tip_gwei(cfg, profit_usd, debt_usd, now=None):
    """Tip реактивного (хвостового) выстрела. PURE-ish. Ручка =0 -> dynamic бит-в-бит."""
    base = dynamic_tip_gwei(cfg, debt_usd) if now is None else dynamic_tip_gwei(cfg, debt_usd, now)
    if _TAIL_TIP_GWEI <= 0:
        return base
    try:
        gas = float(getattr(cfg, "gas_limit_est", 0) or 0)
        eth = float(getattr(cfg, "eth_price_usd", 0) or 0)
        afford = ((_TAIL_TIP_FRAC * max(0.0, float(profit_usd or 0.0)) * 1e9 / (gas * eth))
                  if gas > 0 and eth > 0 else 0.0)
    except Exception:                                        # noqa: BLE001
        afford = 0.0                                         # незнание НЕ повышает ставку
    return max(base, min(_TAIL_TIP_GWEI, afford))


def _net_gate_tail(profit_wei, debt_usd, debt_assets, cfg):
    """(profit, cost, net, tip_gwei) для реактивного пути: cost по tip ХВОСТА, и наружу
    отдаётся ТО ЖЕ число tip, которым будет подписан выстрел."""
    profit_usd = profit_wei * debt_usd / debt_assets if debt_assets else 0.0
    tip = tail_tip_gwei(cfg, profit_usd, debt_usd)
    cost_usd = cfg.gas_limit_est * tip * cfg.eth_price_usd / 1e9
    return profit_usd, cost_usd, profit_usd - cost_usd, tip


# ---- on-chain reads ----

def read_preconf_price(preconf_rpc, oracle):
    """oracle.price() at block='pending' on the preconf RPC = the pre-confirmed Morpho price. None on fail."""
    from chain.simulate import ORACLE_ABI
    try:
        return int(preconf_rpc.contract(oracle, ORACLE_ABI).functions.price().call(block_identifier="pending"))
    except Exception:
        return None


_PX_HEDGE_SEC = float(os.environ.get("PX_HEDGE_TIMEOUT_SEC", "0.4"))
# #101 (10.08): вторая нога хеджа — ТОЛЬКО в деградацию, а не всегда.
# Замер за 6 часов: px_pre=1/1, 2/2, 3/3 — preconf отвечает в 100% случаев, промахов НОЛЬ.
# Значит confirmed-нога, летящая параллельно в боевой Alchemy, не пригодилась НИ РАЗУ, а её
# результат выбрасывается — это ровно половина eth_call горячего пути. А eth_call даёт 74%
# суточного расхода (672.2K из 909.1K) при скользящей проекции 61-64M/мес против лимита 30M,
# и именно он топит боевой ключ: COMBAT-FAILOVER флапал раз в 6 минут.
# Теперь: штатно идёт ОДНА нога (preconf) с тем же таймаутом; промах включает режим деградации
# на PX_HEDGE_DEGRADE_SEC, и в нём возвращается прежний параллельный хедж бит-в-бит.
# Цена риска названа честно: при ВНЕЗАПНОЙ смерти preconf первое чтение потеряет таймаут
# (0.4с) прежде чем поднимем confirmed. Сейчас за эту страховку платят ВСЕ чтения подряд.
# Откат: PX_HEDGE_ALWAYS=1 — обе ноги всегда, как было.
_PX_HEDGE_ALWAYS = os.environ.get("PX_HEDGE_ALWAYS", "0").lower() in ("1", "true", "yes")
_PX_HEDGE_DEGRADE_SEC = float(os.environ.get("PX_HEDGE_DEGRADE_SEC", "60"))
_PX_HEDGE_DEGRADED_UNTIL = 0.0


def px_hedge_degraded(now=None) -> bool:
    """Идём ли сейчас обеими ногами (preconf промахнулся недавно)."""
    n = time.monotonic() if now is None else now
    return _PX_HEDGE_ALWAYS or n < _PX_HEDGE_DEGRADED_UNTIL


def px_hedge_note_miss(now=None):
    """Промах preconf -> держим обе ноги ближайшие PX_HEDGE_DEGRADE_SEC."""
    global _PX_HEDGE_DEGRADED_UNTIL
    n = time.monotonic() if now is None else now
    _PX_HEDGE_DEGRADED_UNTIL = n + _PX_HEDGE_DEGRADE_SEC
_POS_HEDGE_SEC = float(os.environ.get("POS_HEDGE_TIMEOUT_SEC", "0.5"))


def _leg_once(price_fn, r, oracle, ev, box):
    try:
        box.append(price_fn(r, oracle))
    except Exception:
        pass
    finally:
        ev.set()


# Ш31: ВТОРАЯ НОГА ЦЕНЫ — НА РЕЗЕРВНЫЙ КЛЮЧ (решение kelbic).
#
# Вторую ногу снимать НЕЛЬЗЯ — это проверено и подтверждено замером: preconf отвечает лишь в
# 40.3% случаев (3459 из 8579 за сутки), поэтому одноногий режим удлинил бы денежный путь на
# таймаут в шести случаях из десяти. Но она же — крупнейший потребитель БОЕВОГО ключа:
# read_preconf_price(s) даёт 206 вызовов из ~650, около трети, и её результат выбрасывается,
# когда preconf успел.
#
# Отсюда развязка: нога остаётся, но идёт по РЕЗЕРВНОМУ ключу Alchemy. Он уже оплачен, лежит
# в COMBAT_FAILOVER_URLS и до сих пор простаивал — использовался только как аварийное
# переключение «всё или ничего», а не как вторая полоса. Итог: страховка сохранена, а с
# боевого ключа уходит треть нагрузки, из-за которой 429 убивали цели ДО гонки (за сутки 11
# отказов из 45, среди них приз $18 026).
#
# Пусто -> прежнее поведение бит-в-бит (обе ноги на боевом).
_PX_CONFIRMED_RPC = None
_PX_CONFIRMED_LOCK = threading.Lock()


def px_confirmed_rpc(rpc):
    """RPC для confirmed-ноги: резервный ключ, если задан, иначе боевой (прежнее поведение)."""
    url = os.environ.get("PX_CONFIRMED_RPC_URL", "").strip()
    if not url:
        return rpc
    global _PX_CONFIRMED_RPC
    with _PX_CONFIRMED_LOCK:
        if _PX_CONFIRMED_RPC is None:
            try:
                from chain.rpc import BaseRpc, assert_chain_id
                _cid = int(os.environ.get("CHAIN_ID", "8453"))
                _h = BaseRpc(url, _cid, request_timeout=8)
                # #117 гард сети. Ложится на уже существующий сентинел False («навсегда откат на
                # боевой»): цена confirmed-ноги с ЧУЖОЙ цепи — не лишний расход, а неверная цена
                # на денежном пути. None (узел молчит) кандидата НЕ хоронит: сентинел вечный, а
                # молчание на буте временно — вечное решение по временному признаку было бы
                # хуже самой болезни.
                _PX_CONFIRMED_RPC = False if assert_chain_id(
                    _h, _cid, what="PX_CONFIRMED (резервный ключ)") is False else _h
            except Exception:
                _PX_CONFIRMED_RPC = False          # не смогли -> навсегда откат на боевой
    return _PX_CONFIRMED_RPC or rpc


def read_price_hedged(preconf_rpc, rpc, oracle, price_fn=read_preconf_price):
    """Хедж-чтение цены оракула: preconf-pending (лид ~1.8с) и confirmed стартуют ПАРАЛЛЕЛЬНО,
    preconf ждём не дольше _PX_HEDGE_SEC. Штатно preconf отвечает ~100мс -> берём его значение,
    поведение прежнее. В деградацию (429-trickle ~2.05с; канарейка 23.07 probe dt_spawn=2267мс
    БЕЗ hotset-строк = гейт/префетч висели на цене, все рынки в f_noprice) отдаём confirmed:
    лид теряем, детект/гонку/тред — нет. Старая цена с confirmed безопасна: нет флипа -> skip
    (как раньше при None), флип и по старой -> жертва легитимна. Оба легга через price_fn."""
    if rpc is None:
        return price_fn(preconf_rpc, oracle)
    rpc = px_confirmed_rpc(rpc)      # Ш31: confirmed-нога — на резервный ключ, если задан
    if not px_hedge_degraded():
        # ШТАТНО: одна нога. Таймаут тот же, поэтому латентная гарантия не меняется —
        # меняется только то, что мы больше не платим за неиспользуемую страховку.
        # ДЕФЕКТ МОЕЙ ЖЕ ПРАВКИ, пойман замером через 10 минут после раскатки: здесь не звался
        # preconf_mark_reactive(), а он уступает канал preconf горячему чтению, отодвигая фоновый
        # полл. Без него полл конкурировал с чтением, preconf не укладывался в 0.4с и промахи
        # выросли с НУЛЯ до 45% (px_pre 7/13, 10/18, 11/20 против прежних 1/1, 2/2, 3/3).
        # Каждый промах в одноногом режиме дороже, чем в старом: там confirmed уже летел
        # параллельно, здесь он стартует ПОСЛЕ таймаута.
        preconf_mark_reactive()
        ev1, box1 = threading.Event(), []
        threading.Thread(target=_leg_once, args=(price_fn, preconf_rpc, oracle, ev1, box1),
                         daemon=True).start()
        ev1.wait(timeout=_PX_HEDGE_SEC)
        if box1 and box1[0] is not None:
            px_preconf_note(True)
            return box1[0]
        px_preconf_note(False)
        px_hedge_note_miss()          # с этого момента и ещё минуту идём обеими ногами
        try:
            return price_fn(rpc, oracle)
        except Exception:
            return box1[0] if box1 else None
    ev_p, box_p = threading.Event(), []
    ev_c, box_c = threading.Event(), []

    def _leg(r, ev, box):
        try:
            box.append(price_fn(r, oracle))
        except Exception:
            pass
        finally:
            ev.set()

    preconf_mark_reactive()                  # канал preconf занят реактивом -> фоновый полл уступает
    threading.Thread(target=_leg, args=(preconf_rpc, ev_p, box_p), daemon=True).start()
    threading.Thread(target=_leg, args=(rpc, ev_c, box_c), daemon=True).start()
    ev_p.wait(timeout=_PX_HEDGE_SEC)
    if box_p and box_p[0] is not None:
        px_preconf_note(True)
        return box_p[0]
    px_preconf_note(False)                   # фора потеряна на ЭТОМ чтении — датчик деградации
    ev_c.wait(timeout=2.0)
    if box_c and box_c[0] is not None:
        return box_c[0]
    return box_p[0] if box_p else None      # confirmed молчит -> preconf мог дозреть после капа


def read_positions(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
    """market totals + each position -> (tba, tbs, [(borrower, bs, col)]). ONE aggregate3.
    Reads on preconf-pending (same source as the flip price + sim) when preconf_rpc is given, so the
    reaction flip is visible ~1.8s before confirmed; falls back to rpc-latest on degraded preconf.
    src_out (opt. mutable dict): records which source served the read ('preconf'/'confirmed') so the
    caller can flag A' (silent confirmed-fallback -> stale debt -> false flip). Return shape unchanged."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    b32 = rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))]
    for b in borrowers:
        calls.append((MORPHO_BLUE, encode_position_call(b32, b)))
    res = None; _src = "confirmed"
    if preconf_rpc is not None:
        # 07-23 хедж (как у цены, см. read_price_hedged): preconf-aggregate3 под троттлом отбивается
        # ~2.05с (429-trickle) — до хеджа это был реактивный столл для рынков ВНЕ кэша позиций
        # (0x84662b4f, 112 канд. minHF=1.0001: trig=poll read=2342мс). Обе ноги параллельно,
        # preconf ждём <=_POS_HEDGE_SEC (штатно тяжёлый read ~145мс -> его значение, поведение
        # прежнее); не успел/невалиден -> confirmed (позиции медленные, confirmed-latest ~= pending).
        _ev_p, _box_p = threading.Event(), []
        _ev_c, _box_c = threading.Event(), []

        def _leg(_r, _bid, _ev, _box):
            try:
                _box.append(aggregate3(_r, calls, block_identifier=_bid))
            except Exception as _e:
                _box.append(_e)                              # исключение в бокс: отличать fail от «ещё летит»
            finally:
                _ev.set()
        # #117: ГЕЙТ ВТОРОЙ НОГИ ЗДЕСЬ НЕ СТАВИТСЯ — ЗАМЕР ЗАПРЕТИЛ (20.08). Соблазн очевиден:
        # у соседа `read_price_hedged` такой гейт стоит и экономит, а здесь confirmed-нога
        # тоже выбрасывается ветвью `res = _rp`. Но экономия живёт ровно на тех чтениях, где
        # preconf ПОБЕЖДАЕТ, а по ПОЗИЦИЯМ он побеждает почти никогда: 129 чтений из 4500 за
        # сутки (2.9%), 5680 из 48700 за всю историю hotset.log (11.7%) — остальное `src=
        # confirmed`, то есть тяжёлый multicall в preconf не укладывается в 0.5с (тот же
        # компьют-кап, что описан на ~1290: 6 промахов из 7 в канарейке 23.07). Гейт дал бы
        # ~0.1-0.4M CU/мес экономии и +70..+500мс латентности на 88-97% РЕАКТИВНЫХ чтений:
        # где экономия — там налога нет, где налог — там экономии нет. Второе: признак
        # `px_hedge_degraded()` меряет здоровье preconf на ЛЁГКОМ ценовом вызове и о промахах
        # позиций не узнаёт никогда (`px_hedge_note_miss` зовёт только ценовой путь) —
        # чужой сигнал не самокорректируется. Обе ноги стартуют параллельно, как и было.
        preconf_mark_reactive()              # см. read_price_hedged: реактив главнее фонового полла
        threading.Thread(target=_leg, args=(preconf_rpc, "pending", _ev_p, _box_p), daemon=True).start()
        threading.Thread(target=_leg, args=(rpc, None, _ev_c, _box_c), daemon=True).start()
        _ev_p.wait(timeout=_POS_HEDGE_SEC)
        _rp = _box_p[0] if _box_p else None
        if isinstance(_rp, list) and _rp and _rp[0][0] and _rp[0][1]:
            res = _rp; _src = "preconf"
        else:
            _ev_c.wait(timeout=10.0)
            _rc = _box_c[0] if _box_c else None
            if not isinstance(_rc, list):                    # A': confirmed-нога упала/молчит -> как fallback-fail
                # 07-23: 429-шторм на confirmed убивал ВЕСЬ transmit-воркер необработанным HTTPError
                # (preconf-ветка была обёрнута, фолбэк — нет) = потерянные рынки транзита в каскад.
                # None -> вызывающий считает f_norp и продолжает соседей (как при пустом ответе).
                log.info("DIAG read_positions fallback-fail market=%s err=%s", mid[:10],
                         str(_rc)[:60] if _rc is not None else "timeout")
                return None
            res = _rc
    else:
        try:
            res = aggregate3(rpc, calls)                     # confirmed-only путь (stale-дочитка кэша и тесты)
        except Exception as e:
            log.info("DIAG read_positions fallback-fail market=%s err=%s", mid[:10], redact_clip(e, 60))
            return None
    if src_out is not None:
        src_out["src"] = _src
    if not res or not res[0][0] or not res[0][1]:
        return None
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    pos = []; _dropped = []
    for b, (ok, data) in zip(borrowers, res[1:]):
        if ok and data:
            bs, col = decode_position(data)
            pos.append((b, bs, col))
        else:
            _dropped.append(b)
            log.info("DIAG read_positions drop %s/%s ok=%s data_len=%d",
                     mid[:10], b[:10], ok, len(data) if data else 0)
    _dump_hotset(mid, _src, tba, tbs, pos, _dropped)          # A3: full set to disk, counter to journal
    return tba, tbs, pos


# ---- Layer 2 (Идея 2): continuous background position cache, money-markets only ----
# Scope: только рынки из execute.ROUTE_COLLATERALS (P1-8: +cbLTC/cbBTC; сустейн валидирован на одном рынке,
# 340 вызовов/блок; полный охват всех ~40 рынков и поведение ПОД каскадом НЕ провалидированы,
# см. STATE.md "РАЗБИВКА — симметрия с декодером ЧАСТИЧНАЯ"). Любой рынок вне кэша падает в
# read_positions без изменений — нулевой риск для непокрытых рынков.

_POSITION_CACHE_LOCK = threading.Lock()
_POSITION_CACHE: dict = {}  # {market_id: (tba, tbs, {borrower: (bs, col, last_ok_ts)})}
_POSITION_CACHE_MAX_AGE_SEC = 6.0  # ~3 cycles at the observed dt=1.9-2.4s/5-market cycle


def _cache_coverage_stats(borrowers, cached_positions, now):
    """PURE: for a live-read borrower list, how many would have been found in the position cache
    (in_cache) vs never-seen, how old the found ones are, and WHICH ones were missing (short
    addresses, capped) — so a missing borrower can be cross-referenced by address against the
    drop-log, distinguishing 'cache hasn't caught up yet' from 'this borrower fails in both paths
    for the same underlying reason'. No RTT, no side effects — pure comparison against whatever's
    already in memory. Used to measure never-seen frequency under real transmit load WITHOUT
    switching the hot path to consume the cache (see STATE.md)."""
    ages = [now - cached_positions[b][2] for b in borrowers if b in cached_positions]
    missing = [b[:10] for b in borrowers if b not in cached_positions]
    in_cache = len(ages)
    avg_age = sum(ages) / len(ages) if ages else 0.0
    max_age = max(ages) if ages else 0.0
    return in_cache, len(borrowers), avg_age, max_age, missing[:10]


def refresh_position_cache(cache_rpc, mid, borrowers):
    """Background per-block refresh for ONE money-market. A borrower whose position() call in the
    batch comes back ok=False/empty is logged (refresh-miss) but their PREVIOUS cache entry (if any)
    is left untouched — not wiped, not refreshed. Freshness is enforced downstream by age at read
    time (read_positions_cached), not by dropping on a single missed cycle.
    RPC-split (07-21): positions are slow-moving, so this bulk refresh runs on the general/confirmed
    `cache_rpc` (RPC_URL) at block='latest' — NOT on preconf. The preconf per-IP compute cap only
    tolerates ~1 heavy call/s; keeping the ~5-20/s refresh off preconf leaves that budget for the
    price poll (read_preconf_prices, ~0.5/s) which is the one thing that actually needs the ~1.8s lead."""
    from chain.multicall import aggregate3, encode_market_call, decode_market, encode_position_call, decode_position
    if not borrowers:
        return
    b32 = cache_rpc.to_bytes32(mid)
    calls = [(MORPHO_BLUE, encode_market_call(b32))] + [(MORPHO_BLUE, encode_position_call(b32, b)) for b in borrowers]
    try:
        res = aggregate3(cache_rpc, calls, block_identifier="latest")
    except Exception as e:
        log.info("DIAG position-cache refresh-fail market=%s err=%s", mid[:10], redact_clip(e, 80))
        return
    if not res or not res[0][0] or not res[0][1]:
        log.info("DIAG position-cache refresh-fail market=%s reason=market-call-failed", mid[:10])
        return
    m = decode_market(res[0][1]); tba, tbs = m[2], m[3]
    now = time.time()
    with _POSITION_CACHE_LOCK:
        prev = _POSITION_CACHE.get(mid)
        positions = dict(prev[2]) if prev else {}
        for b, (ok, data) in zip(borrowers, res[1:]):
            if ok and data:
                bs, col = decode_position(data)
                positions[b] = (bs, col, now)
            else:
                log.info("DIAG position-cache refresh-miss market=%s borrower=%s ok=%s", mid[:10], b[:10], ok)
        _POSITION_CACHE[mid] = (tba, tbs, positions)


# 27.08: потолок возраста кэша, по которому ДЕЙСТВУЕМ при провале живого чтения (каскад 15:04).
_HOT_STALE_FALLBACK_SEC = float(os.environ.get("HOT_STALE_FALLBACK_SEC", "600"))


def read_positions_cached(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
    """Drop-in for read_positions (same signature — swaps in via _process_transmit's reads_fn=
    default, no call-site change needed). Market not yet in _POSITION_CACHE (not money-market-
    covered, or not refreshed yet) -> unchanged fallback to the original live read_positions.
    Market IS cached: a borrower's entry younger than _POSITION_CACHE_MAX_AGE_SEC is served from
    cache (zero RTT); a borrower missing entirely, or present but older than the threshold, is
    EXCLUDED from this transmit's candidates + logged (A-with-age-bound: trust recent cache,
    never act on data past the freshness window), everyone else in the same market is served
    normally, zero RTT."""
    with _POSITION_CACHE_LOCK:
        cached = _POSITION_CACHE.get(mid)
    if cached is None:
        return read_positions(rpc, mid, borrowers, preconf_rpc=preconf_rpc, src_out=src_out)
    tba, tbs, positions = cached
    if src_out is not None:
        src_out["src"] = "cache"
    now = time.time()
    pos = []
    stale = []
    # 07-23: при refresh-гейте 1/30с (см. _CACHE_REFRESH_MIN_INTERVAL) запись старше 6с — НОРМА,
    # а не исключение; исключать заёмщика из transmit-кандидатов = пропущенная жертва в гонке.
    # Свежие отдаём из кэша (~1мс), stale/missing дочитываем ЖИВЬЁМ одним aggregate3 на
    # CONFIRMED (~70-130мс): preconf-компьют-кап на тяжёлый multicall отдаёт 429 за ~2.05с
    # (замер 23.07; канарейка tip=0.05: 6 промахов из 7 = ровно этот столл, dt_spawn 2.2-2.55с
    # при ack 55-69мс -> мимо блока на КАЖДОМ транзите с жертвой). Preconf-лид остаётся у цены
    # (~0-124мс, лёгкий вызов) — флип двигает цена, позиции медленные. Только если живое
    # чтение упало — старое поведение (stale-skip), чтобы не действовать по протухшим данным.
    for b in borrowers:
        p = positions.get(b)
        if p is None:
            stale.append(b)
            continue
        bs, col, last_ok_ts = p
        if now - last_ok_ts <= _POSITION_CACHE_MAX_AGE_SEC:
            pos.append((b, bs, col))
        else:
            stale.append(b)
    if stale:
        try:
            live = read_positions(rpc, mid, stale, preconf_rpc=None, src_out=src_out)
        except Exception as e:
            live = None
            log.info("DIAG position-cache live-subset fail market=%s n=%d err=%s",
                     mid[:10], len(stale), redact_clip(e, 60))
        if live is not None:
            tba, tbs, live_pos = live                        # живые totals свежее кэшевых
            pos.extend(live_pos)
            if src_out is not None:
                src_out["src"] = "cache+" + str(src_out.get("src") or "live")
        else:
            # 27.08 — КАСКАД 15:04 (19 жертв, $ дня): 18 из 19 жертв БЫЛИ в кандидатах и были
            # выброшены ЗДЕСЬ строкой «stale-skip: live-fail» (443 за 80с) — живое чтение упало
            # (боевой ключ в 429 от нашего же шквала), и гейт «не действовать по протухшим
            # данным» отдал весь каскад конкурентам. Цена действия по кэшу — реверт «healthy»
            # ~$0.002; цена пропуска — приз. Поэтому при провале живого чтения ДЕЙСТВУЕМ ПО
            # КЭШУ (возраст <= HOT_STALE_FALLBACK_SEC; позиции меняются медленно, флип двигает
            # цена — она живая), выбрасываем только тех, кого в кэше нет вовсе.
            used, dropped = 0, 0
            for b in stale:
                p = positions.get(b)
                if p is not None and now - p[2] <= _HOT_STALE_FALLBACK_SEC:
                    pos.append((b, p[0], p[1]))
                    used += 1
                else:
                    dropped += 1
                    log.info("DIAG position-cache stale-skip market=%s borrower=%s reason=live-fail-nocache",
                             mid[:10], b[:10])
            if used:
                log.info("DIAG position-cache stale-USE market=%s n=%d (выброшено %d) reason=live-fail "
                         "age<=%ds — действуем по кэшу, страховка = реверт на цепи", mid[:10], used,
                         dropped, int(_HOT_STALE_FALLBACK_SEC))
            if src_out is not None:
                src_out["src"] = "cache+stale"
    return tba, tbs, pos


_CACHE_REFRESH_THREAD = None      # re-entrancy guard (план п.3): один цикл за раз
_CACHE_REFRESH_STARTED = 0.0      # monotonic старт живого цикла (для busy-skip running=)
_CACHE_SKIP_N = 0                 # skip'ов с последнего лога (троттл 60с)
_CACHE_SKIP_LOG_TS = 0.0
# 07-23: refresh каждые ~2с тяжёлыми aggregate3 выжигал burst-бакет RPC_URL -> 429-шторм душил
# block-assess (~2% блоков) и nonce-fetch. 1 цикл/30с возвращает бакет боевым путям; потребитель
# кэша (POSITIONS_FROM_CACHE=1) при stale-записи дочитывает живьём (см. read_positions_cached),
# так что редкий refresh НЕ теряет жертв. 0 = старое поведение (каждый блок).
_CACHE_REFRESH_MIN_INTERVAL = float(os.environ.get("POSITION_CACHE_MIN_INTERVAL_SEC", "30"))
# 05.08 bg-CU: цикл 30с×22 рынка = ~40M CU/мес на bg-ключе — главный кормилец собственного
# 429-шторма, при том что записи живут до 6с (раздача), а цикл 30с -> хит-рейт <=20%: в штиль
# это резерв-слой (stale-фолбэк при live-fail), не ускоритель. Двухскоростной каденс: штиль =
# QUIET (120с), predict-armed окно (prearm жив) = боевой MIN (30с) через cache_boost() — PRE-ARM
# главный рычаг (lead med 67с = 2 цикла прогрева до транзита). Откат: QUIET=0 (одна скорость).
_CACHE_QUIET_INTERVAL = float(os.environ.get("POSITION_CACHE_QUIET_INTERVAL_SEC", "120"))
_CACHE_BOOST_UNTIL = 0.0
# ревью 05.08 (а): в predict-armed окне кэш = боевые данные (sizing fast-lane) — буст-циклы
# едут на боевом ключе (Alchemy №2, 429=0), штиль остаётся на bg (там кэш = резерв-слой,
# ошибки терпимы). Нагрузка на боевой: ~22 вызова/30с ТОЛЬКО в окнах. Откат =0 — всё на bg.
_CACHE_COMBAT_BOOST = os.environ.get("POSITION_CACHE_COMBAT_RPC", "1").lower() not in ("0", "false", "no")
# 07.08 ЗАМЕР ПРОТИВ ЗАМЫСЛА. «~22 вызова/30с ТОЛЬКО в окнах» — так задумано; по журналу за
# сутки вышло 45 399 вызовов = 35M CU/мес на боевом ключе при лимите 30M (53% всего расхода
# Alchemy). Причина не в логике, а в частоте: prearm-петля зовёт cache_boost() БЕЗУСЛОВНО на
# каждом проходе и продлевает буст на 90с, а predict-окна идут почти встык -> буст держался
# 72% суток. Слово «окно» в замысле означало событие более редкое, чем оно оказалось.
# Бюджет — секунды скользящего часа, в которые разрешён боевой каденс. Сверх бюджета цикл
# уходит на ТИХИЙ каденс и на фоновый ключ: это РЕЗАНИЕ расхода, а не перекладывание его на
# соседний ключ (dRPC оплачен не щедрее). Настоящее окно живёт ~2 мин = 4 цикла, окон ~3.3/ч
# => ~13 циклов/ч против бюджета в 20: редкие окна обслуживаются полностью, круглосуточный
# помол — нет. 0 = без бюджета (прежнее поведение).
_CACHE_COMBAT_BUDGET_SEC_H = float(os.environ.get("POSITION_CACHE_COMBAT_BUDGET_SEC_H", "600"))
_CACHE_COMBAT_SPEND: list = []            # monotonic'ы циклов, съеденных боевым ключом за час
_CACHE_BUDGET_LOG_TS = 0.0


def cache_boost(sec: float = 90.0):
    """Продлить боевой каденс кэша позиций (зовёт prearm-окно каждый проход, ~5с)."""
    global _CACHE_BOOST_UNTIL
    _CACHE_BOOST_UNTIL = max(_CACHE_BOOST_UNTIL, time.monotonic() + sec)


def _spawn_cache_refresh(*, cache_rpc, shared, bn, combat_rpc=None):
    """Re-entrancy guard for the Layer-2 driver (plan #3). The driver used to be spawned as a NEW
    thread EVERY block while one cycle takes 1.9-2.4s ~= block time: any RTT degradation =>
    overlapping cycles => thread pile-up => self-load on the battle preconf exactly at cascade
    peak (the plausible trigger of the A' confirmed-fallback). One cycle at a time: if the
    previous one is still alive, skip this block. busy-skip is throttled to 1 line/60s with an
    accumulated count — n/running are the live signal of preconf strain (correlate with
    src=confirmed). Called only from the single feed-loop thread => no lock around check-then-set."""
    global _CACHE_REFRESH_THREAD, _CACHE_REFRESH_STARTED, _CACHE_SKIP_N, _CACHE_SKIP_LOG_TS
    from chain.failover import bg_shed
    if bg_shed():
        return                                    # бой уехал на ключ bg — фон уступает (ревью 02.08, усл.3)
    global _CACHE_BUDGET_LOG_TS
    _iv = _CACHE_REFRESH_MIN_INTERVAL             # двухскоростной каденс (05.08): штиль -> QUIET
    _boosting = time.monotonic() < _CACHE_BOOST_UNTIL
    if _boosting and _CACHE_COMBAT_BUDGET_SEC_H > 0:
        # Бюджет считаем в СЕКУНДАХ БОЕВОГО КАДЕНСА, а не в числе циклов: интервал настраиваемый,
        # и счёт по циклам разъехался бы с расходом при первой же смене MIN.
        _cut = time.monotonic() - 3600.0
        while _CACHE_COMBAT_SPEND and _CACHE_COMBAT_SPEND[0] < _cut:
            _CACHE_COMBAT_SPEND.pop(0)
        _spent = len(_CACHE_COMBAT_SPEND) * max(1.0, _CACHE_REFRESH_MIN_INTERVAL)
        if _spent >= _CACHE_COMBAT_BUDGET_SEC_H:
            _boosting = False                     # бюджет часа выбран -> тихий каденс и фон-ключ
            if time.monotonic() - _CACHE_BUDGET_LOG_TS >= 600.0:
                _CACHE_BUDGET_LOG_TS = time.monotonic()
                log.info("DIAG cache-boost бюджет часа выбран (%.0fс из %.0fс) — каденс %.0fс "
                         "на фоновом ключе", _spent, _CACHE_COMBAT_BUDGET_SEC_H,
                         _CACHE_QUIET_INTERVAL)
    if _CACHE_QUIET_INTERVAL > _iv and not _boosting:
        _iv = _CACHE_QUIET_INTERVAL
    if (_iv > 0 and _CACHE_REFRESH_STARTED
            and time.monotonic() - _CACHE_REFRESH_STARTED < _iv):
        return                                    # интервал-гейт (см. _CACHE_REFRESH_MIN_INTERVAL): тихий skip
    t = _CACHE_REFRESH_THREAD
    if t is not None and t.is_alive():
        _CACHE_SKIP_N += 1
        now = time.monotonic()
        if now - _CACHE_SKIP_LOG_TS >= 60.0:
            log.info("DIAG cache-refresh busy-skip n=%d running=%.1fs bn=%d",
                     _CACHE_SKIP_N, now - _CACHE_REFRESH_STARTED, bn)
            _CACHE_SKIP_N = 0
            _CACHE_SKIP_LOG_TS = now
        return
    _use_rpc = cache_rpc                          # ревью 05.08 (а): буст-цикл -> боевой ключ
    if _CACHE_COMBAT_BOOST and combat_rpc is not None and _boosting:
        _use_rpc = combat_rpc
        _CACHE_COMBAT_SPEND.append(time.monotonic())   # платит бюджет только БОЕВОЙ ключ
    _CACHE_REFRESH_STARTED = time.monotonic()
    _CACHE_REFRESH_THREAD = threading.Thread(target=_refresh_position_caches, daemon=True,
                                             kwargs=dict(cache_rpc=_use_rpc, shared=shared, bn=bn))
    _CACHE_REFRESH_THREAD.start()


def _refresh_position_caches(*, cache_rpc, shared, bn):
    """Per-block driver (Layer 2, measurement phase — see STATE.md 'C temporary'). Refreshes
    _POSITION_CACHE for every money-market (UNIV3_PATHS collateral) via refresh_position_cache.
    Writes _POSITION_CACHE and logs refresh-miss/-fail + cycle timing. NOTE 07-23: the cache IS
    consumed when POSITIONS_FROM_CACHE=1 (P1-6, live in prod) — read_positions_cached serves fresh
    entries and live-reads stale ones. One market's decode/network hiccup does not stop the cycle
    for the others."""
    from chain.execute import ROUTE_COLLATERALS
    with shared.lock:
        groups = dict(shared.groups)
    _t0 = time.time()
    n_markets = 0
    for mid, borrowers in groups.items():
        params = _FULL_PARAMS_CACHE.get(mid)
        if not params:
            continue
        _loan, coll, _oracle, _irm, _lltv = params
        if coll.lower() not in ROUTE_COLLATERALS:    # P1-8: +cbLTC/cbBTC (Slipstream-маршруты)
            continue
        try:
            refresh_position_cache(cache_rpc, mid, borrowers)
            n_markets += 1
        except Exception:
            log.exception("position-cache refresh error market=%s", mid[:10])
    if n_markets:
        log.info("DIAG position-cache cycle markets=%d dt=%.1fs block=%s", n_markets, time.time() - _t0, bn)


def read_preconf_prices(preconf_rpc, oracles, rpc_fallback=None, block_identifier="pending"):
    """ONE aggregate3 at block='pending' on the preconf RPC -> ({oracle: price|None}, n_fallback). Reads
    ALL our oracles each block; covers markets whose aggregator never surfaces in the tx stream (the money
    markets cbXRP/cbDOGE/cbADA), which the bare match structurally misses. If rpc_fallback is given, any
    oracle that came back None (preconf degraded/rate-limited) is re-read on `latest` in ONE extra batch
    (latest is as fast and identical between transmits -> fills holes losing only the ~1.8s preconf lead).
    n_fallback = how many oracles were filled from latest (0 = preconf healthy)."""
    from chain.multicall import aggregate3, encode_price_call, decode_price
    if not oracles:
        return {}, 0
    calls = [(o, encode_price_call()) for o in oracles]
    try:
        res = aggregate3(preconf_rpc, calls, block_identifier=block_identifier)
        out = {o: (decode_price(data) if (ok and data) else None) for o, (ok, data) in zip(oracles, res)}
    except Exception:
        out = {o: None for o in oracles}
    n_fb = 0
    if rpc_fallback is not None:
        missing = [o for o, v in out.items() if v is None]
        if missing:
            try:
                fb = aggregate3(rpc_fallback, [(o, encode_price_call()) for o in missing])
                for o, (ok, data) in zip(missing, fb):
                    if ok and data:
                        px = decode_price(data)
                        if px is not None:
                            out[o] = px
                            n_fb += 1
            except Exception:
                pass
    return out, n_fb


def _poll_changed(prices, poll_seen, oracle2agg, feed_handled=None, bn=None):
    """PURE: given {oracle: price|None}, update poll_seen (baseline on first sighting) and return the
    set of aggregators whose oracle price MOVED. First sighting -> no spawn; None price -> skipped.
    Layer1 dedup: if feed_handled contains (oracle, bn), the feed already spawned this transmit ->
    still update poll_seen baseline (so next block compares correctly) but DON'T re-add to changed."""
    changed = set()
    for o, px in prices.items():
        if px is None:
            continue
        prev = poll_seen.get(o)
        poll_seen[o] = px
        if prev is not None and px != prev:
            if feed_handled is not None and (o, bn) in feed_handled:
                continue                        # feed already spawned this (oracle, block) -> skip poll dup
            changed.add(oracle2agg.get(o))
    changed.discard(None)
    return changed


# --- #139: одиночный стоячий воркер поблочного опроса цен (latest-wins) -------------------------
# Факт боя 18.08 02:56:36: recv-петля спавнила отдельный поток _poll_prices НА КАЖДЫЙ
# блок. В тишине это ~56 тредов; в шторм кап TasksMax=256 пробивался и hot_loop падал
# (RuntimeError: can't start new thread) в самый дорогой момент. Замена — ровно паттерн
# _chain_index.offer_block: recv только КЛАДЁТ (bn, конфиг) и будит ЕДИНСТВЕННЫЙ тред; занят
# воркер — промежуточные bn перезатираются последним (очереди НЕТ). Тред один на процесс:
# self-heal hot_loop переустанавливает конфиг атомарно, второго воркера не создаёт.
_POLL_WORKER_LOCK = threading.Lock()
_POLL_WORKER_THREAD = None
_POLL_WORKER_WAKE = threading.Event()
_POLL_WORKER_STOP = threading.Event()
_POLL_STATE = None      # (bn, kwargs) — последняя поданная работа; ребайнд кортежа атомарен под GIL

# Сетевые вызовы опроса ОБЯЗАНЫ иметь жёсткий таймаут. Thread-на-блок терпел зависший опрос
# (следующий блок спавнил свежий тред); одиночный воркер — нет: зависший read_preconf_prices
# заморозил бы ВЕСЬ поблочный триггер. read_preconf_prices делает максимум ДВА последовательных
# aggregate3 (preconf + добор latest), поэтому 2×таймаут обязан лежать ЗАМЕТНО ниже времени блока
# (~2с). 0.8с ⇒ ≤1.6с на худший опрос. Клоны ОТДЕЛЬНЫЕ: боевой preconf_rpc и _bg_rpc (цикл кэша
# штатно живёт 1.9-2.4с) свой таймаут сохраняют — их мы не трогаем.
_POLL_RPC_TIMEOUT_SEC = float(os.environ.get("HOT_POLL_RPC_TIMEOUT_SEC", "0.8"))


def _poll_rpc(base):
    """Клон боевого RPC с жёстким пер-запросным таймаутом ДЛЯ ПОБЛОЧНОГО ОПРОСА (см. #139).
    None -> None (вызыватель падает на прежний путь)."""
    if base is None:
        return None
    try:
        from chain.rpc import BaseRpc
        return BaseRpc(base.rpc_url, getattr(base, "chain_id", 8453),
                       request_timeout=_POLL_RPC_TIMEOUT_SEC)
    except Exception:
        return base            # клон не удался -> прежний rpc, опрос жив (таймаут провайдера по умолчанию)


def offer_poll_block(bn, **kwargs):
    """Точка входа из recv-петли: только присвоение и Event.set (сети нет, лока опроса нет).

    latest-wins: занятой воркер увидит лишь ПОСЛЕДНИЙ bn, промежуточные перезатрутся — очередь не
    копится. Конфиг (kwargs: feeds/meta/spawn_kwargs после ре-резолва) заменяется АТОМАРНО одним
    ребайндом кортежа — self-heal hot_loop подаёт свежий конфиг, воркер берёт его на следующем
    обороте, старые kwargs не залипают."""
    global _POLL_STATE
    _POLL_STATE = (bn, kwargs)
    _ensure_poll_worker()
    _POLL_WORKER_WAKE.set()


def _ensure_poll_worker():
    """Ленивый старт единственного воркера под локом. Идемпотентно: self-heal hot_loop зовёт
    offer_poll_block заново, второй тред не создаётся."""
    global _POLL_WORKER_THREAD
    t = _POLL_WORKER_THREAD
    if t is not None and t.is_alive():
        return
    with _POLL_WORKER_LOCK:
        t = _POLL_WORKER_THREAD
        if t is not None and t.is_alive():
            return
        _POLL_WORKER_STOP.clear()
        _POLL_WORKER_THREAD = threading.Thread(target=_poll_worker_loop, daemon=True, name="hot-poll")
        _POLL_WORKER_THREAD.start()


def _poll_worker_loop():
    """Единственный потребитель заявок опроса. Ошибка ОДНОГО опроса не имеет права убить воркер —
    мёртвый стоячий воркер хуже Thread-на-блок (тот хоть на следующем блоке оживал)."""
    while not _POLL_WORKER_STOP.is_set():
        _POLL_WORKER_WAKE.wait()
        _POLL_WORKER_WAKE.clear()
        if _POLL_WORKER_STOP.is_set():
            break
        job = _POLL_STATE                       # атомарное чтение последней заявки (latest-wins)
        if not job:
            continue
        _bn, _kw = job
        try:
            _poll_prices(bn=_bn, **_kw)         # ГЛОБАЛ (позднее связывание): тест подменяет транспорт
        except Exception:
            try:
                (_kw.get("log") or log).exception("hot-poll worker: опрос bn=%s упал", _bn)
            except Exception:
                pass


def _poll_prices(*, preconf_rpc, rpc_fallback, feeds, meta, poll_seen, stats, spawn_kwargs, bn, log, feed_handled=None):
    """Per-block trigger: batch-read all oracle preconf prices; spawn a (gated) _process_transmit for
    each aggregator whose price moved. The agg-level gate dedups vs any bare-match spawn. This is what
    gives cbXRP/cbDOGE/cbADA a detection trigger at all (they never surface in the flashblock stream)."""
    oracle2agg = {}
    for agg, mids in feeds.items():
        for mid in mids:
            om = meta.get(mid)
            if om:
                oracle2agg[om[0]] = agg
    if preconf_poll_allowed():
        prices, n_fb = read_preconf_prices(preconf_rpc, list(oracle2agg), rpc_fallback=rpc_fallback)
        n_lat = 0
    else:                                   # уступаем канал реактиву: latest напрямую, БЕЗ 429-платы
        prices, n_fb = read_preconf_prices(rpc_fallback, list(oracle2agg),
                                           rpc_fallback=None, block_identifier=None)
        n_lat = 1
    if stats is not None:
        stats["poll"] += 1
        stats["poll_none"] += sum(1 for v in prices.values() if v is None)
        stats["poll_fb"] += n_fb
        stats["poll_lat"] = stats.get("poll_lat", 0) + n_lat
    changed = _poll_changed(prices, poll_seen, oracle2agg, feed_handled=feed_handled, bn=bn)
    if stats is not None:
        stats["pspawn"] += len(changed)
    for agg in changed:
        threading.Thread(target=_process_transmit, args=(agg, bn, "poll", time.time()),
                         kwargs=spawn_kwargs, daemon=True).start()


def resolve_meta(rpc, markets):
    """{mid: (oracle, lltv_wad)} via ONE aggregate3 of idToMarketParams (immutable). Also fills
    _FULL_PARAMS_CACHE (loan/coll/irm too) so prepare_hot can skip its own params re-fetch (2a)."""
    from chain.multicall import aggregate3, encode_id_to_market_params_call, decode_id_to_market_params
    mids = [m.market_id for m in markets if m.market_id]
    calls = [(MORPHO_BLUE, encode_id_to_market_params_call(rpc.to_bytes32(mid))) for mid in mids]
    out = {}
    for mid, (ok, data) in zip(mids, aggregate3(rpc, calls)):
        if ok and data:
            loan, coll, oracle, irm, lltv = decode_id_to_market_params(data)
            out[mid] = (oracle, lltv)
            _FULL_PARAMS_CACHE[mid] = (loan, coll, oracle, irm, lltv)
    return out


# ---- preconf-sourced prepare (mirror of execute.prepare_liquidation; 2 diffs only) ----

def _note_refusal(market_id, borrower, bucket, debt_usd):
    """#123: запомнить наш отказ от цели. Прибор СБОКУ от денежного пути: любая ошибка глотается,
    запись — одна операция словаря. Нормализация обеих сторон в lower — канон corpse_pairs
    (несимметричная нормализация = множества, которые не пересекаются никогда)."""
    if not REFUSAL_LOSS_ALERT:
        return
    try:
        _k = (str(market_id).lower(), str(borrower).lower())
        _now = time.time()
        _REFUSAL_REG[_k] = (_now, str(bucket)[:32], float(debt_usd or 0.0))
        if len(_REFUSAL_REG) > 512:                     # регистр не растёт бесконечно
            _cut = _now - REFUSAL_TTL_SEC
            for _kk in [k for k, v in _REFUSAL_REG.items() if v[0] < _cut]:
                _REFUSAL_REG.pop(_kk, None)
        # Встречный матчинг (одноблочная гонка): победа могла пройти индекс ДО этой записи
        if _RL_SINK is not None:
            for _ts, _m, _b, _caller, _tx in reversed(_RECENT_LIQ):
                if _now - _ts > REFUSAL_RECENT_WIN_SEC:
                    break
                if (_m, _b) == _k:
                    from chain.execute import _is_ours
                    _al, _cfg = _RL_SINK
                    if not _is_ours(_caller, _caller, _cfg):
                        _REFUSAL_REG.pop(_k, None)      # один алерт на отказ
                        _al.send_async(
                            "\U0001FA78 ОТКАЗАЛИСЬ — ЗАБРАЛ КОНКУРЕНТ (одноблочная гонка):\n"
                            "рынок %s заёмщик %s долг $%.0f | отказ %s | победитель %s tx %s"
                            % (_k[0][:10], _k[1][:10], float(debt_usd or 0.0),
                               str(bucket)[:24], str(_caller)[:12], str(_tx)[:14]),
                            key="refusal-loss")
                    break
    except Exception:
        pass


def make_refusal_loss_watcher(alerter, cfg):
    """#123: фабрика колбэка для chain_index.set_on_liquidate — ЕДИНСТВЕННАЯ точка, где регистр
    отказов встречается с Liquidate-событиями цепи. Зовётся из треда индекса; всё под except —
    прибор не имеет права ронять ни индекс, ни огонь. Своя победа (контракт/флот/кошелёк,
    _is_ours) сообщением не считается. Один алерт на отказ (запись выбывает при совпадении);
    антифлуд ключа refusal-loss остаётся в Alerter."""
    global _RL_SINK
    _RL_SINK = (alerter, cfg)                           # арм встречного матчинга (одноблочные)

    def _cb(bn, liq_events):
        try:
            _now0 = time.time()                         # кольцо недавних побед — ДО раннего выхода
            for ev in (liq_events or []):
                if isinstance(ev, dict):
                    _RECENT_LIQ.append((_now0, str(ev.get("mid") or "").lower(),
                                        str(ev.get("borrower") or "").lower(),
                                        str(ev.get("caller") or ""), str(ev.get("tx") or "")))
            del _RECENT_LIQ[:-64]
            if not _REFUSAL_REG:
                return
            from chain.execute import _is_ours
            now = time.time()
            hits = []
            for ev in (liq_events or []):
                if not isinstance(ev, dict):                # мусор не обрывает соседние события
                    continue
                k = (str(ev.get("mid") or "").lower(), str(ev.get("borrower") or "").lower())
                v = _REFUSAL_REG.get(k)
                if v is None:
                    continue
                ts, bucket, debt = v
                if now - ts > REFUSAL_TTL_SEC:
                    _REFUSAL_REG.pop(k, None)
                    continue
                caller = str(ev.get("caller") or "")
                _REFUSAL_REG.pop(k, None)               # один алерт (или своя победа) на отказ
                if _is_ours(caller, caller, cfg):
                    continue                            # взяли сами — не «чужая победа»
                hits.append("рынок %s заёмщик %s долг $%.0f | наш отказ %.0fс назад (%s) | "
                            "победитель %s tx %s" % (k[0][:10], k[1][:10], float(debt or 0.0),
                                                     now - ts, bucket, caller[:12],
                                                     str(ev.get("tx") or "")[:14]))
            if hits:
                alerter.send_async("\U0001FA78 ОТКАЗАЛИСЬ — ЗАБРАЛ КОНКУРЕНТ (блок %d):\n%s"
                                   % (bn, "\n".join(hits[:4])), key="refusal-loss")
                log.info("REFUSAL-LOSS блок %d: %d совпадений отказ->чужая победа", bn, len(hits))
        except Exception:
            log.exception("refusal-loss: прибор упал (индекс и огонь не затронуты)")
    return _cb


def _panic_probe(rpc, market_id, borrower, mid, ask_shares, ask_seized):
    """#121: назвать источник Panic 0x11 поимённо. Тред-демон, СБОКУ от денежного пути (skip уже
    решён), всё под except — прибор не имеет права влиять на огонь. Печатает confirmed-позицию
    против нашей заявки: `debt<ask` (долг упал ниже заявки), `coll<seize` (залог ниже сейза),
    `gone` (долга нет), `neither` (ни то ни другое ⇒ паника НЕ из вычитаний Morpho — смотреть
    рассинхрон preconf: lastUpdate против block.timestamp)."""
    def _run():
        try:
            from chain.multicall import aggregate3, encode_position_call, decode_position, encode_market_call, decode_market
            rp = aggregate3(rpc, [(MORPHO_BLUE, encode_position_call(mid, borrower)),
                                  (MORPHO_BLUE, encode_market_call(mid))])
            bs_now, coll_now = decode_position(rp[0][1])
            m_now = decode_market(rp[1][1])
            if int(bs_now) == 0:
                why = "gone"
            elif int(bs_now) < int(ask_shares):
                why = "debt<ask"
            elif int(coll_now) < int(ask_seized):
                why = "coll<seize"
            else:
                why = "neither(preconf-desync?)"
            log.info("DIAG panic-probe %s/%s why=%s ask_shares=%d bs_now=%d (delta=%d) ask_seized=%d coll_now=%d lastUpdate=%d",
                     market_id[:10], borrower[:10], why, int(ask_shares), int(bs_now),
                     int(ask_shares) - int(bs_now), int(ask_seized), int(coll_now), int(m_now[4]))
        except Exception:
            pass
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


def _diag_revert_bucket(err: str) -> str:
    """Классифицирует sim-revert в бакет для DIAG. Чистая (под юнит)."""
    e = (err or "").lower()
    if "0x11" in e or "panic" in e:
        return "panic-underflow"
    if "0x81ceff30" in e:
        return "swapfailed"
    if "healthy" in e or "position is heal" in e:
        return "healthy-race"
    if "0x08c379a0" in e:
        return "error-string"
    return "other"


def _seized_exact(repaid_shares, lif_wad, price, tba, tbs):
    """P1-7: seizedAssets ТОЧНО по канону Morpho._liquidate (ветка repaidShares>0):
    toAssetsDown -> wMulDown(LIF) -> mulDivDown(SCALE, price), все floor. Прежний expected_seized
    считал от to_assets_up (репэй-сторона) — оценка сверху на wei-округлениях; для amount_in свопа
    нужен ТОЧНЫЙ приход залога (amount_in > баланса = SwapFailed), для капа — точное сравнение
    с collateral (иначе Panic 0x11)."""
    from chain.simulate import to_assets_down
    from chain.execute import ORACLE_PRICE_SCALE, WAD
    assets_down = to_assets_down(repaid_shares, tba, tbs)
    return (assets_down * lif_wad // WAD) * ORACLE_PRICE_SCALE // price


def _shave_ask(borrow_shares, bps=None):
    """#121: заявка ОБЯЗАНА быть строго меньше долга заёмщика. Morpho вычитает
    `position.borrowShares -= repaidShares` БЕЗ zeroFloorSub — равенство есть граница, и любое
    уменьшение долга между снимком и исполнением даёт Panic 0x11 (проверено на форке: bs -> OK,
    bs+1 -> Panic 0x11). Просить полный кэшированный borrowShares значит ставить заявку ровно на
    эту границу. Чистая (юнит), запас в bps, откат HOT_ASK_SHAVE_BPS=0 -> прежнее поведение.
    Пыль (bs<=1) не режем: 0 shares Morpho отвергает как INCONSISTENT_INPUT."""
    bs = int(borrow_shares)
    b = ASK_SHAVE_BPS if bps is None else int(bps)
    if bs <= 1 or b <= 0:
        return bs
    return max(1, bs - max(1, bs * b // 10_000))


CAP_SHAVE_MAX_BPS = float(os.environ.get("HOT_CAP_SHAVE_MAX_BPS", "100"))
CAP_DRIFT_SAFETY = float(os.environ.get("HOT_CAP_DRIFT_SAFETY", "1.5"))


def _cap_repaid_shares(borrow_shares, collateral, price, lif_wad, tba, tbs, shave_bps=None,
                       drift_fn=None):
    """P1-7: max repaidShares, при котором seized <= collateral в арифметике канона — глубокая
    позиция (залог не покрывает долг+бонус) раньше давала Panic 0x11 (collateral -= seized) ->
    sim-revert -> skip, и главные призы каскадов терялись. Кап ДО сайзинга свопа (запрет STATE
    «НЕ капаем» относился к пост-хок капу ПОСЛЕ расчёта свопа — там mismatch). При капе забирается
    ПОЧТИ весь залог: остаток пыли остаётся у заёмщика, позиция переживает выстрел. (Прежняя
    формулировка «забирается весь залог -> Morpho списывает остаток долга (socialization)» БЫЛА
    ЛОЖНОЙ: обнуления залога не случалось никогда — и до правки остаток был >=1 единица в 100%
    из 6000 прогонов линзы. Огрызок позиции — НАЗВАННЫЙ ХВОСТ, отдельная задача.) Это НЕ закрытый
    seizedAssets-режим: вход контракта прежний (repaidShares). Floor-инверсия + верификация вперёд
    точной формулой; чистая (юнит).

    #121 НА ЗАЛОГОВОЙ ГРАНИЦЕ (20.08, форк-репро в test_fork_route_cbbtc_weth П5): целиться в
    `seized <= collateral` ЗНАЧИТ СТАВИТЬ ЗАЯВКУ НА ГРАНИЦУ — ровно тот класс, что #121 закрыл на
    долговой стороне. Замер: залог 3 000 000, наш seized 2 999 999 (запас 1 единица = 0.0033 bps),
    БОЕВАЯ tx -> Panic 0x11; с запасом -> ликвидация проходит.
    ПОЧЕМУ ГРАНИЦА НЕ ДЕРЖИТСЯ, хотя формула у нас ТОЧНАЯ (та же, что в Morpho): расходятся не
    формулы, а ВХОДЫ. `Morpho.liquidate` первым делом делает `_accrueInterest`, и toAssetsDown
    считается уже по ВЫРОСШЕМУ totalBorrowAssets. Плюс до трёх единиц floor-округления на цепочке
    toAssetsDown -> wMulDown -> mulDivDown.
    ОКНО НАЧИСЛЕНИЯ — НЕ НАШ TTL, А СВОЙСТВО РЫНКА: `block.timestamp - market.lastUpdate`. Его
    никто не обязан обновлять, и на тихих рынках оно огромно (замер 20.08: целевой 0x5dffffc7 —
    4714с без accrual, LBTC/cbBTC — 9.8 СУТОК). Хуже того, худший случай КОРРЕЛИРУЕТ с самой
    кап-веткой: подводный рынок = утилизация под 100% = ставка у потолка кривой И никто не
    трогает accrual. Поэтому запас берётся ОТ ЖИВОГО ОКНА, а не от константы:
        shave_bps = max(ASK_SHAVE_BPS, 10000 * rate_per_sec * elapsed / 1e18 * CAP_DRIFT_SAFETY)
    `drift_fn()` -> (elapsed_sec, rate_per_sec_wad) читается ЛЕНИВО и только в кап-ветке (на
    нормальном пути RTT не появляется вовсе; в пачке доски он бесплатен).
    НЕЗНАНИЕ ЗАКРЫВАЕТ ГАРД: drift_fn нет / чтение упало / посчитанный запас > CAP_SHAVE_MAX_BPS
    (дефолт 100 bps) — кап-ветка НЕ СТРОИТСЯ (возврат 0, вызывающий уходит прежним путём). Дрейф,
    который не покрывается разумным недобором, это не «рискнём», а «мы не знаем цены заявки».
    Явный `shave_bps` (стенд/откат) заменяет фиксированную часть и снимает требование живого окна.
    ВЕТКА БЕЗ КАПА (весь долг помещается) — БИТ-В-БИТ прежняя: запас применяется ТОЛЬКО к
    залоговой границе внутри кап-ветки, ранний выход сравнивается с ПОЛНЫМ collateral.
    """
    from chain.simulate import to_shares_down
    from chain.execute import ORACLE_PRICE_SCALE, WAD
    bs = int(borrow_shares)
    if bs <= 0:
        return 0
    if _seized_exact(bs, lif_wad, price, tba, tbs) <= collateral:
        return bs                                            # весь долг помещается — кап не нужен
    # --- ниже ТОЛЬКО cap-ветка: граница отодвигается от залога на запас #121 ---
    b = float(ASK_SHAVE_BPS if shave_bps is None else int(shave_bps))
    drift_bps = None
    if drift_fn is not None:
        try:
            _d = drift_fn()
            if _d is not None:
                _el, _rate = int(_d[0]), int(_d[1])
                if _el >= 0 and _rate >= 0:
                    drift_bps = 10_000.0 * _rate * _el / 1e18 * CAP_DRIFT_SAFETY
        except Exception:                                    # noqa: BLE001
            drift_bps = None
    if drift_bps is None:
        if shave_bps is None:
            return 0            # живого окна начисления нет и ручка не задана — ветку не строим
    else:
        b = max(b, drift_bps)
    if b > CAP_SHAVE_MAX_BPS:
        return 0                # дрейф не укладывается в разумный недобор — ветку не строим
    col = int(collateral)
    if b > 0 and col > 1:
        col = max(1, col - max(1, int(col * b / 10_000.0)))
    cap_assets = (col * price // ORACLE_PRICE_SCALE) * WAD // lif_wad
    rs = min(to_shares_down(cap_assets, tba, tbs), bs)
    for _ in range(4):                                       # floor-инверсия может промахнуться на wei
        if rs <= 0:
            return 0
        if _seized_exact(rs, lif_wad, price, tba, tbs) <= col:
            return rs
        rs -= max(1, rs // 10**6)
    return 0


_GAS_CACHE: dict = {}   # market_id -> padded gas от последнего успешного preconf-estimate (P0-4).
                        # Газ-профиль стабилен per-market (тот же UniV3-путь, та же форма tx);
                        # send всегда шлёт max(cached, cfg.gas_limit_est) — лимит с запасом бесплатен.


def prepare_hot(rpc, preconf_rpc, cfg, market_id, borrower, debt_usd, debt_assets, price, slippage_bps=100,
                state=None):
    """Mirror of execute.prepare_liquidation, PRECONF-sourced — diverges from it in more than the two
    points below; this is illustrative, not a complete list (params sourcing, swap routing, and extra
    diagnostic/guard reads have also diverged — check the function body, not this docstring, for the
    current full set):
      (1) the seize is sized on the PRE-CONFIRMED `price` (passed in, read once by the caller), not a
          fresh latest read — so `expected_seized` matches what the contract seizes at execution;
      (2) the simulate is gated against PRECONF-PENDING (simulate_tx(preconf_rpc, ..., block='pending'))
          where the position is already liquidatable, instead of latest (still healthy pre-confirm).
    Same {ok, ...} shape as the armed prepare."""
    try:
        from chain.multicall import (aggregate3, encode_id_to_market_params_call, decode_id_to_market_params,
            encode_market_call, decode_market, encode_position_call, decode_position)
        from chain.simulate import to_assets_up
        from chain.execute import kyber_swap, direct_swap, encode_liquidate, simulate_tx, account_for
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq:
            return {"ok": False, "reason": "LIQUIDATOR_ADDRESS unset"}
        w3 = rpc._web3()
        bot = account_for(w3, cfg.wallet_key).address
        mid = rpc.to_bytes32(market_id)

        # market-PARAMS (статичные адреса рынка, иммутабельны) — из кэша resolve_meta (2a), без RTT.
        # Fallback на confirmed-RPC fetch, если кэш ещё не наполнен (до первого resolve_meta).
        _cached = _FULL_PARAMS_CACHE.get(market_id)
        if _cached:
            loan, coll, oracle, irm, lltv_wad = _cached
        else:
            rp_params = aggregate3(rpc, [(MORPHO_BLUE, encode_id_to_market_params_call(mid))])
            loan, coll, oracle, irm, lltv_wad = decode_id_to_market_params(rp_params[0][1])
        # market(tba/tbs)+position(borrow_shares): P0-4 — штатно приходят от caller'а (state=), который
        # миллисекундами раньше прочитал их тем же read_positions из ТОГО ЖЕ preconf-pending. Повторное
        # чтение здесь было чистым дублем (~350мс RTT). Fallback-чтение остаётся для вызова без state.
        if state is not None:
            tba, tbs, borrow_shares, collateral_dbg = state
            _snap_src = "caller"
        else:
            try:
                rp_state = aggregate3(preconf_rpc, [(MORPHO_BLUE, encode_market_call(mid)),
                                                    (MORPHO_BLUE, encode_position_call(mid, borrower))],
                                      block_identifier="pending")
                _preconf_ok = rp_state[0][0] and rp_state[1][0]
            except Exception:
                _preconf_ok = False
            if _preconf_ok:
                m = decode_market(rp_state[0][1]); tba, tbs = m[2], m[3]
                borrow_shares, collateral_dbg = decode_position(rp_state[1][1])
                _snap_src = "preconf"
            else:
                rp_fb = aggregate3(rpc, [(MORPHO_BLUE, encode_market_call(mid)),
                                         (MORPHO_BLUE, encode_position_call(mid, borrower))])
                m = decode_market(rp_fb[0][1]); tba, tbs = m[2], m[3]
                borrow_shares, collateral_dbg = decode_position(rp_fb[1][1])
                _snap_src = "rpc-fallback"
        if borrow_shares == 0:
            return {"ok": False, "reason": "no debt (cleared)"}
        # ДИАГНОСТИКА расхождения preconf vs confirmed — В ТРЕДЕ (P0-4: было синхронно = 150-300мс
        # Alchemy-RTT В горячем пути ради лога; вывод тот же, задержки ноль).
        def _diag_divergence(_bs_pre=int(borrow_shares), _src=_snap_src):
            try:
                _rp_conf = aggregate3(rpc, [(MORPHO_BLUE, encode_position_call(mid, borrower))])
                _bs_conf, _ = decode_position(_rp_conf[0][1])
                if _bs_conf != _bs_pre:
                    log.info("DIAG divergence %s/%s src=%s preconf_bs=%d confirmed_bs=%d ratio=%.2f",
                             market_id[:10], borrower[:10], _src, _bs_pre, _bs_conf,
                             (_bs_conf / _bs_pre if _bs_pre else 0))
            except Exception:
                pass
        threading.Thread(target=_diag_divergence, daemon=True).start()

        _lif = lif_from_lltv(lltv_wad / 10**18)
        _lif_wad = int(round(_lif * 10**18))
        # P1-7: кап на глубокой позиции (seized>collateral был Panic 0x11 -> skip); seized ТОЧНО
        # по канону (toAssetsDown), а не оценкой сверху — amount_in свопа = реальный приход залога.
        def _drift_hot():
            """ЖИВОЕ окно начисления рынка. ЛЕНИВО: зовётся только если кап-ветка реально нужна
            (глубокая позиция), поэтому на нормальном пути RTT не появляется. Один aggregate3 на
            обе величины: market (lastUpdate) и IRM.borrowRateView."""
            from chain.multicall import (encode_borrow_rate_view_call, decode_borrow_rate_view)
            _mp5 = (loan, coll, oracle, irm, int(lltv_wad))
            _r = aggregate3(rpc, [(MORPHO_BLUE, encode_market_call(mid))])
            _mk = decode_market(_r[0][1])
            _r2 = aggregate3(rpc, [(irm, encode_borrow_rate_view_call(_mp5, _mk))])
            if not _r2[0][0]:
                return None
            _now = int(time.time())
            return (max(0, _now - int(_mk[4])), decode_borrow_rate_view(_r2[0][1]))

        repaid_shares = _cap_repaid_shares(_shave_ask(int(borrow_shares)), int(collateral_dbg), int(price),
                                           _lif_wad, tba, tbs, drift_fn=_drift_hot)
        if repaid_shares <= 0:
            return {"ok": False, "reason": "cap: no repayable shares (collateral~0 либо окно "
                                           "начисления рынка не читается/слишком велико)"}
        if repaid_shares < int(borrow_shares):
            # КАП-ВЕТКА: запоминаем ПРЕДВЫСТРЕЛЬНЫЙ залог. Огрызок после нашего выстрела иначе
            # неотличим от живой цели (bad_debt=0, repaid<bs) и жёг бы подготовку вечно.
            note_cap_plan(market_id, borrower, collateral_dbg)
            log.info("DIAG cap %s/%s bs=%d -> repaid_shares=%d (deep position: почти весь залог, "
                     "остаток пыли остаётся у заёмщика)",
                     market_id[:10], borrower[:10], int(borrow_shares), repaid_shares)
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = _seized_exact(repaid_shares, _lif_wad, int(price), tba, tbs)   # (1) preconf price, канон-точно
        if seized == 0:
            log.info("DIAG seized=0 %s/%s repaid_shares=%d repaid_assets=%d collateral=%d tba=%d tbs=%d price=%d lltv=%d",
                     market_id[:10], borrower[:10], repaid_shares, repaid_assets, collateral_dbg, tba, tbs, int(price), lltv_wad)
            return {"ok": False, "reason": "seized=0"}

        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        # 29.07 unwrap-хоп: обёртки без DEX-ликвидности котируются ПОДЛЕЖАЩИМ (redeem в контракте
        # v3 до свопа); rate кэширован (TTL 1ч) — RTT в горячем пути не добавляется.
        from chain.execute import swap_plan
        swap_token, swap_amount, unwrap = swap_plan(rpc, coll, seized)
        swap = direct_swap(swap_token, loan, swap_amount, liq, liq, repaid_assets=repaid_assets)
        # consume-хук (05.08): метаданные direct-сайза для списания живого капа на отправке
        _swap_meta = (swap_token, loan, int(swap_amount)) if swap is not None else None
        if swap is None:                       # market outside direct routes -> Kyber fallback (unchanged)
            swap = kyber_swap(swap_token, loan, swap_amount, liq, liq, slippage_bps=slippage_bps)

        # P0-4: guard-read свежести УДАЛЁН — он дублировал sim: тот же preconf-pending источник, а sim
        # ниже ревертит (Panic 0x11) на том же устаревшем долге и даёт тот же skip. Отдельное чтение
        # стоило ~350мс RTT в горячем пути и ловило только окно «между guard и sim» ~0мс шириной.

        cd0 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], 0, unwrap)
        # P0-4: gas per-market из кэша; при промахе estimate_gas идёт ПАРАЛЛЕЛЬНО sim (оба preconf,
        # cd0/cd1 по газу эквивалентны — отличаются одним сравнением minProfit) -> первый выстрел по
        # рынку платит max(sim, estimate) вместо суммы, последующие — только sim.
        _cached_gas = _GAS_CACHE.get(market_id)
        _gas_holder: dict = {}
        _est_thread = None
        if _cached_gas is None:
            def _do_estimate():
                try:
                    _g = preconf_rpc._web3().eth.estimate_gas({"from": bot, "to": liq, "data": cd0},
                                                              block_identifier="pending")
                    _gas_holder["gas"] = int(_g) * 12 // 10
                except Exception:
                    pass
            _est_thread = threading.Thread(target=_do_estimate, daemon=True)
            _est_thread.start()
        sim = simulate_tx(preconf_rpc, liq, bot, cd0, block="pending")        # (2) preconf-pending gate
        if not sim["ok"] and HOT_SIM_CONFIRMED_RETRY and _sim_transport_err(str(sim.get("error") or "")):
            log.info("HOT sim-хедж %s/%s: транспорт preconf (%s) — дочитка confirmed",
                     market_id[:10], borrower[:10], str(sim.get("error"))[:60])
            sim = simulate_tx(rpc, liq, bot, cd0, block="latest")
        if not sim["ok"]:
            # DIAG: логируем ВСЕ sim-revert'ы (не только Panic/seized) с бакетом причины + calldata+блок для форка.
            try:
                _diag_blk = rpc._web3().eth.block_number
            except Exception:
                _diag_blk = 0
            _diag_bucket = _diag_revert_bucket(str(sim.get("error", "")))
            # 27.08: отказ СВОПНОЙ стороны запоминается для HEALTHY-RACE. Он приходит на ОДНОМ
            # тике, а HR-ставка рождается на СЛЕДУЮЩЕМ (инцидент 16:15: SwapFailed в 16:15:13,
            # ставка в 16:15:54) — без памяти знание о сломанном выходе не доживает до решения.
            # CannotRepay (0x2206a00f) — наш собственный гард «свопа не хватило на погашение»;
            # он попадает в бакет "other", поэтому ловим селектор явно, а не по бакету.
            _serr = str(sim.get("error", "")).lower()
            if _diag_bucket == "swapfailed" or "0x2206a00f" in _serr:
                try:
                    from chain import healthy_race as _hr_note
                    _hr_note.note_swap_fail(market_id, borrower,
                                            "CannotRepay" if "0x2206a00f" in _serr else "SwapFailed")
                except Exception:                                    # noqa: BLE001
                    pass                                             # учёт сбоку: не трогает огонь
            if _diag_bucket == "panic-underflow" and HOT_PANIC_PROBE:
                _panic_probe(rpc, market_id, borrower, mid, repaid_shares, seized)
            _note_refusal(market_id, borrower, _diag_bucket, debt_usd)   # #123: отказ в регистр
            log.info("DIAG sim-revert %s/%s bucket=%s err=%s | repaid_shares=%d repaid_assets=%d seized=%d collateral=%d tba=%d tbs=%d price=%d lltv=%d lif=%.4f block=%d cd=%s",
                     market_id[:10], borrower[:10], _diag_bucket, str(sim["error"])[:40], repaid_shares, repaid_assets, seized,
                     collateral_dbg, tba, tbs, int(price), lltv_wad, lif_from_lltv(lltv_wad / 10**18), _diag_blk, cd0.hex() if isinstance(cd0, (bytes, bytearray)) else str(cd0))
            # П.1 HEALTHY-RACE (22.08, дефолт ВЫКЛ): «healthy» при hf_est<=0.997 = лаг оракула,
            # мы РАНЬШЕ флипа. Гейт (свежесть/труп/эпизод/приз/бакет) — в chain.healthy_race;
            # on-chain страховка — min_profit 25% премии (труп/слип ревертит, цена = газ).
            if _diag_bucket == "healthy-race":
                try:
                    from chain import healthy_race as _hr
                    _hr_ba = to_assets_up(int(borrow_shares), tba, tbs)
                    _hr_hf = _hr.hf_estimate(collateral_dbg, price, lltv_wad, _hr_ba)
                    _hr_prize = (repaid_assets * debt_usd / debt_assets) * (
                        lif_from_lltv(lltv_wad / 10**18) - 1.0) if debt_assets else 0.0
                    # episode=True ПО ПОСТРОЕНИЮ (линза 22.08): prepare_hot зовётся только из
                    # обработки транзита секунды назад — это и есть «pending<10с» спеки. Ждать
                    # dominant_tip нельзя: в 05:11 класс эскалации въехал в кольцо ПОСТФАКТУМ,
                    # гейт был бы закрыт ровно в момент, ради которого фича строилась.
                    # Execute-путь (не транзит-контекст) остаётся на dominant_tip/pending.
                    _hr_state, _hr_why = _hr.classify(market_id, borrower, _hr_hf, _hr_prize,
                                                      data_age_s=0.0,
                                                      borrow_shares=int(borrow_shares),
                                                      episode=True)
                    if _hr_state == "HEALTHY_RACE":
                        _hr_minp = max(1, int(repaid_assets
                                              * (lif_from_lltv(lltv_wad / 10**18) - 1.0) * 0.25))
                        _hr_cd = encode_liquidate(mp, borrower, repaid_shares, swap["router"],
                                                  swap["calldata"], _hr_minp, unwrap)
                        _hr_tip = _hr.hr_tip_wei(int(cfg.tip_gwei * 1e9))
                        _hr_gas = int(getattr(cfg, "gas_limit_floor", 3_000_000) or 3_000_000)
                        log.info("HR bid target=%s/%s prize=$%.0f tip_class=%d reason=%s",
                                 market_id[:10], borrower[:10], _hr_prize, _hr_tip, _hr_why)
                        _hr_warn = _hr.note_bid(market_id, borrower, _hr_prize, _hr_tip, _hr_hf)
                        if _hr_warn:
                            log.warning("HR %s", _hr_warn)
                        _hr_prep = {"ok": True, "healthy_race": True, "calldata": _hr_cd,
                                    "gas": _hr_gas, "net_usd": _hr_prize,
                                    "profit_usd": _hr_prize, "cost_usd": _hr.HR_MISS_COST_USD,
                                    "mp": mp, "borrower": borrower,
                                    "repaid_shares": repaid_shares,
                                    "swap_target": swap["router"],
                                    "swap_calldata": swap["calldata"], "min_profit": _hr_minp,
                                    "debt_usd": debt_usd, "debt_assets": debt_assets,
                                    "unwrap_target": unwrap, "swap_meta": _swap_meta,
                                    "tip": _hr_tip}
                        _hr_fb = _fresh_basefee()
                        if _hr_fb is not None:
                            _hr_prep["max_fee"] = _hr_fb * 2 + _hr_tip
                        return _hr_prep
                    log.info("HR skip target=%s/%s state=%s reason=%s",
                             market_id[:10], borrower[:10], _hr_state, _hr_why)
                except Exception as _hr_e:                       # noqa: BLE001
                    log.info("HR gate error (закрыт): %s", type(_hr_e).__name__)
            return {"ok": False, "reason": f"preconf sim revert: {sim['error']}"}
        profit_wei = sim["profit"]
        # Р1б/#120: гейт и tip хвоста — ОДНО число (cost посчитан тем tip, которым стреляем)
        profit_usd, cost_usd, net_usd, _tail_tip = _net_gate_tail(profit_wei, debt_usd, debt_assets, cfg)
        if net_usd < cfg.min_profit_usd:
            _note_refusal(market_id, borrower, "net-below-min", debt_usd)   # #123: отказ в регистр
            return {"ok": False, "net_usd": net_usd,
                    "reason": f"net ${net_usd:.2f} (profit ${profit_usd:.2f} - cost ${cost_usd:.2f}) < min ${cfg.min_profit_usd:.2f}"}

        min_profit_final = profit_wei * 95 // 100
        cd1 = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"],
                               min_profit_final, unwrap)
        # gas: из per-market кэша (0 RTT) либо от параллельного preconf-estimate выше. Явный gas в prep
        # по-прежнему нужен, иначе send_tx оценит на confirmed-Alchemy, где reaction-флип ещё healthy ->
        # estimate_gas ревертит -> tx НЕ уходит. Лимит всегда max(..., cfg.gas_limit_est) — запас бесплатен.
        _floor = int(getattr(cfg, "gas_limit_floor", 3_000_000) or 3_000_000)   # см. config: форк 25.07
        if _cached_gas is not None:
            _gas_limit = max(_cached_gas, _floor)
        else:
            if _est_thread is not None:
                _est_thread.join(timeout=2.0)
            _est = _gas_holder.get("gas")
            if _est is not None:
                _GAS_CACHE[market_id] = _est
                _gas_limit = max(_est, _floor)
            else:
                _gas_limit = _floor            # estimate не ответил (preconf 429) -> НЕ 500k, иначе OOG
        _prep = {"ok": True, "calldata": cd1, "gas": _gas_limit, "net_usd": net_usd, "profit_usd": profit_usd, "cost_usd": cost_usd,
                 # сырые компоненты для batch-энкодера + debt для per-item USD в receipt-цикле (prep["calldata"] = одиночный вызов)
                 "mp": mp, "borrower": borrower, "repaid_shares": repaid_shares, "swap_target": swap["router"],
                 "swap_calldata": swap["calldata"], "min_profit": min_profit_final, "debt_usd": debt_usd,
                 "debt_assets": debt_assets, "unwrap_target": unwrap, "swap_meta": _swap_meta}
        _fb = _fresh_basefee()
        if _fb is not None:                                   # SEND-2: fee из fb-хедера (0 Alchemy-RTT в send)
            _tip = int(_tail_tip * 1e9)                       # Р1б/#120: тот же tip, которым считался гейт
            _prep["max_fee"] = _fb * 2 + _tip                 # та же формула base*2+tip, но base = pending-блок из фида
            _prep["tip"] = _tip
        return _prep
    except Exception as e:
        return {"ok": False, "reason": f"error: {type(e).__name__}: {redact_clip(e, 120)}"}


def prepare_fast(rpc, preconf_rpc, cfg, market_id, borrower, debt_usd, debt_assets, price, state):
    """P1-9 fast-lane (флаг cfg.fast_lane, OFF по умолчанию): подготовка БЕЗ sim и БЕЗ estimate —
    ноль RTT, путь decode->send ~150-250мс. Безопасность НЕ на sim, а на контрактах: Morpho
    HEALTHY_POSITION ревертит до трансферов, наш minProfit/SwapFailed/CannotRepay ревертят своп-
    исход; цена промаха = газ реверта (~центы при Base-fee), учитывается kill-switch'ем как loss.
    Условия (иначе {ok:False} и обычный prepare_hot с sim): state от caller; params в кэше;
    маршрут direct_swap (Kyber в fast-lane запрещён — серверный RTT); fee из fb-хедера свежий
    (SEND-2); gas из per-market кэша либо cfg.gas_limit_est.
    minProfit аналитический: 50% теоретического бонуса repaid*(LIF-1) — запас на слиппаж/старение."""
    try:
        from chain.simulate import to_assets_up
        from chain.execute import direct_swap, encode_liquidate, WAD
        from strategy.pnl import lif_from_lltv

        liq = cfg.liquidator_address
        if not liq or state is None:
            return {"ok": False, "reason": "fast: no state/liquidator"}
        _fb = _fresh_basefee()
        if _fb is None:
            return {"ok": False, "reason": "fast: fb-basefee stale"}
        params = _FULL_PARAMS_CACHE.get(market_id)
        if not params:
            return {"ok": False, "reason": "fast: params not cached"}
        loan, coll, oracle, irm, lltv_wad = params
        tba, tbs, borrow_shares, collateral = state
        if borrow_shares <= 0:
            return {"ok": False, "reason": "fast: no debt"}
        _lif = lif_from_lltv(lltv_wad / 10**18)
        _lif_wad = int(round(_lif * 10**18))
        # fast-lane — путь БЕЗ RTT по построению, поэтому живое окно начисления здесь не читается:
        # drift_fn не даётся, и кап-ветка НЕ строится (возврат 0) -> уходим в prepare_hot, где и
        # sim есть, и один лишний aggregate3 допустим. Глубокая позиция теряет fast-lane, но не
        # получает заявку с непосчитанным запасом.
        repaid_shares = _cap_repaid_shares(_shave_ask(int(borrow_shares)), int(collateral), int(price),
                                           _lif_wad, tba, tbs)
        if repaid_shares <= 0:
            return {"ok": False, "reason": "fast: cap=0 (кап-ветка требует живого окна "
                                           "начисления — уходим в prepare_hot)"}
        repaid_assets = to_assets_up(repaid_shares, tba, tbs)
        seized = _seized_exact(repaid_shares, _lif_wad, int(price), tba, tbs)
        if seized == 0:
            return {"ok": False, "reason": "fast: seized=0"}
        swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
        if swap is None and REACTIVE_PARTIAL:
            from chain.execute import route_max_in, partial_shares_for_cap
            _cap = route_max_in(coll, loan)   # дробление (01.08): сейз сверх ёмкости пула
            _sp = partial_shares_for_cap(repaid_shares, seized, _cap) if _cap is not None else 0
            if _sp > 0:
                repaid_shares = _sp
                repaid_assets = to_assets_up(_sp, tba, tbs)
                seized = _seized_exact(_sp, _lif_wad, int(price), tba, tbs)
                swap = direct_swap(coll, loan, seized, liq, liq, repaid_assets=repaid_assets)
                if swap is not None:
                    log.info("FAST partial %s/%s: сейз > кап %d — режем до %d shares",
                             market_id[:10], borrower[:10], _cap, _sp)
        if swap is None:
            return {"ok": False, "reason": "fast: no direct route"}
        min_profit = repaid_assets * (_lif_wad - WAD) // WAD // 2
        if min_profit <= 0:
            return {"ok": False, "reason": "fast: zero bonus"}
        # Р1б/#120: гейт и tip хвоста — одно число
        profit_usd, cost_usd, net_usd, _tail_tip = _net_gate_tail(min_profit, debt_usd, debt_assets, cfg)
        if net_usd < cfg.min_profit_usd:
            _note_refusal(market_id, borrower, "fast-net-below-min", debt_usd)   # #123: отказ в регистр
            return {"ok": False, "reason": f"fast: net ${net_usd:.2f} < min (analytic floor)"}
        mp = {"loanToken": loan, "collateralToken": coll, "oracle": oracle, "irm": irm, "lltv": lltv_wad}
        cd = encode_liquidate(mp, borrower, repaid_shares, swap["router"], swap["calldata"], min_profit)
        _tip = int(_tail_tip * 1e9)                           # Р1б/#120: тот же tip, которым считался гейт
        return {"ok": True, "calldata": cd,
                "gas": max(_GAS_CACHE.get(market_id, 0),
                           int(getattr(cfg, "gas_limit_floor", 3_000_000) or 3_000_000)),
                "net_usd": net_usd, "profit_usd": profit_usd, "cost_usd": cost_usd,
                "max_fee": _fb * 2 + _tip, "tip": _tip, "fast": True,
                # сырые компоненты для batch-энкодера + debt для per-item USD в receipt-цикле;
                # swap_meta — direct-сайз для consume-хука живого капа (fast-lane всегда direct)
                "mp": mp, "borrower": borrower, "repaid_shares": repaid_shares, "swap_target": swap["router"],
                "swap_calldata": swap["calldata"], "min_profit": min_profit, "debt_usd": debt_usd,
                "debt_assets": debt_assets, "swap_meta": (coll, loan, int(seized))}
    except Exception as e:
        return {"ok": False, "reason": f"fast error: {type(e).__name__}: {redact_clip(e, 80)}"}


# ---- per-transmit worker (reaction filter + kill-switch + dispatch) ----

def hot_min_repaid(cfg):
    return float(getattr(cfg, "hot_min_repaid_usd", 2000.0))


_ANSWER_UPDATED_SIG = None   # keccak(AnswerUpdated(int256,uint256,uint256)); лениво

# Поправка 07-17 п.1: медиана «+46» считалась ТОЛЬКО по попаданиям в блок; 37% MISSED-BLOCK
# (+1 блок = полный проигрыш) в неё не входили. Копим ОБА исхода за кампанию + tx-count блока
# (перевод «позиции -> мс» раньше был не обоснован кодом). Под своим локом (пишут bg-треды).
_CANARY_STATS_LOCK = threading.Lock()
_CANARY_HIT_DELTAS: list = []   # in-block дельты
_CANARY_MISSES = 0              # canary_blk > transmit_blk


def _canary_measure(rpc, canary_hash, agg, transmit_block, cfg, log):
    """⭐ Bg: дождаться receipt канарейки -> учесть газ в СВОЙ бакет -> найти tx-index транслята
    (AnswerUpdated на agg в transmit_block, узкий getLogs) -> ДЕЛЬТА canary_idx - transmit_idx.
    Малая/отриц. дельта = попадаем в блок первыми = выигрываем гонку. Read-only после отправки."""
    global _ANSWER_UPDATED_SIG, _CANARY_MISSES
    from chain.execute import canary_record_gas, canary_invalidate_nonce, canary_stats
    from web3 import Web3
    try:
        w3 = rpc._web3()
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(canary_hash, timeout=45)
        except Exception:
            canary_invalidate_nonce(rpc, cfg)   # дропнута -> _LOCAL_NONCE впереди чейна -> инвалидировать (money-safety)
            log.info("DIAG canary NO-RECEIPT (dropped?) -> nonce invalidated")
            return
        cblk, cidx = rcpt.get("blockNumber"), rcpt.get("transactionIndex")
        gas_usd = int(rcpt.get("gasUsed") or 0) * int(rcpt.get("effectiveGasPrice") or 0) * cfg.eth_price_usd / 1e18
        canary_record_gas(gas_usd)
        if _ANSWER_UPDATED_SIG is None:
            from eth_utils import keccak
            _ANSWER_UPDATED_SIG = "0x" + keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
        from chain.rpc import get_logs_fb
        tidx = None
        for _att in range(4):                  # getLogs отстаёт от receipt (лаг пропагации логов) -> ретрай
            logs = get_logs_fb(w3, {"fromBlock": transmit_block, "toBlock": transmit_block,
                                    "address": Web3.to_checksum_address(agg), "topics": [_ANSWER_UPDATED_SIG]})
            if logs:
                tidx = logs[0].get("transactionIndex"); break
            time.sleep(2.0)
        probes, spent = canary_stats()
        if tidx is not None and cblk == transmit_block:
            try:                                   # tx-count блока: «позиции -> мс» с опорой, не на глаз
                _blk_txs = int(w3.eth.get_block_transaction_count(transmit_block))
            except Exception:
                _blk_txs = -1
            with _CANARY_STATS_LOCK:
                _CANARY_HIT_DELTAS.append(cidx - tidx)
                _hits = sorted(_CANARY_HIT_DELTAS); _miss = _CANARY_MISSES
            log.info("DIAG canary DELTA delta=%d canary_idx=%d transmit_idx=%d block=%d blk_txs=%d gas$=%.3f probe=%d spent$=%.2f | run: hits=%d miss=%d med_hit=%d",
                     cidx - tidx, cidx, tidx, transmit_block, _blk_txs, gas_usd, probes, spent,
                     len(_hits), _miss, _hits[len(_hits) // 2])
        elif tidx is not None:
            with _CANARY_STATS_LOCK:
                _CANARY_MISSES += 1
                _hits_n = len(_CANARY_HIT_DELTAS); _miss = _CANARY_MISSES
            log.info("DIAG canary MISSED-BLOCK canary_blk=%d transmit_blk=%d (+%d поздно) canary_idx=%d gas$=%.3f probe=%d | run: hits=%d miss=%d",
                     cblk, transmit_block, cblk - transmit_block, cidx, gas_usd, probes, _hits_n, _miss)
        else:
            log.info("DIAG canary no-AnswerUpdated agg=%s blk=%d canary_idx=%d gas$=%.3f probe=%d",
                     agg[:10], transmit_block, cidx, gas_usd, probes)
    except Exception as e:
        log.info("DIAG canary measure fail: %s", type(e).__name__)


def _maybe_canary(agg, block, subidx, rpc, cfg, log, t0=None):
    """⭐ Пробить канарейку, ЕСЛИ флаг ON, это РЕАЛЬНЫЙ транслят (не периодический poll — у того нет
    AnswerUpdated в блоке, дельту мерить не против чего) и в цикле НЕТ ликвидации/inflight (ноль помех).
    Гейты кап/интервал/газ — атомарно в send_canary. Замер в фоне -> не тормозит hot-путь.
    Поправка 07-17: канарейка летит БОЕВЫМ send-путём (fee из fb-хедера + SEND-3 raw в секвенсер) —
    пробы 1-100 шли Alchemy-путём с 2 fee-RTT и без секвенсера, т.е. мерили оверхед, которого у
    реальной ликвидации нет. dt_spawn (t0=спавн транзита) + ack в логе = разложение мс по п.1 поправки."""
    if not getattr(cfg, "canary_probe", False) or subidx == "poll":
        return
    from chain.execute import send_canary, current_inflight
    if current_inflight() > 0:                    # боевая tx в полёте -> не соревноваться за nonce
        return
    _fb = _fresh_basefee()
    # Ш30: канарейка обязана платить ТО ЖЕ, что боевой путь. Здесь стоял cfg.tip_gwei
    # напрямую (0.05 gwei), поэтому после Ш28 прибор мерил ЧУЖОЙ класс: боевые tx платят
    # измеренный класс (~0.0016 gwei), а канарейка — в тридцать раз больше, и её delta
    # описывала позицию, которой у боевой tx нет. Замер прибором, настроенным не на тот
    # объект, — канон, на котором мы уже горели (оракульная цена вместо цены выхода).
    # Побочно канарейка дешевеет в ~30 раз.
    _tip = int(dynamic_tip_gwei(cfg, 0.0) * 1e9)
    _seq = str(getattr(cfg, "sequencer_rpc", "") or "")
    _bcast = [_seq] if (_seq and _seq.lower() not in ("off", "none", "0")) else None
    _ss = time.perf_counter()
    h = send_canary(rpc, cfg, log,
                    max_fee=(_fb * 2 + _tip) if _fb is not None else None, tip=_tip,
                    broadcast_raw_urls=_bcast)
    if h:
        # trig=feed (money-путь, price=0ms) vs bare-match idx (в dt_spawn сидит gate-RTT ~110-130мс) —
        # без атрибуции кампания несравнима: медиану надо считать ПО feed-пробам отдельно.
        log.info("DIAG canary sent block=%d trig=%s agg=%s dt_spawn=%s ack=%.0fms fees=%s seq=%s",
                 block, subidx, agg[:10],
                 ("%.0fms" % ((time.time() - t0) * 1000.0)) if t0 is not None else "?",
                 (time.perf_counter() - _ss) * 1000.0,
                 "fb" if _fb is not None else "alchemy", "on" if _bcast else "off")
        threading.Thread(target=_canary_measure, args=(rpc, h, agg, block, cfg, log), daemon=True).start()


def _process_transmit(agg, block, subidx, t, *, rpc, preconf_rpc, cfg, feeds, meta, shared,
                      store, guard, alerter, log, last_price=None, stats=None,
                      price_fn=read_preconf_price, reads_fn=read_positions,
                      prepare_fn=prepare_hot, dispatch_fn=None,
                      px_hint=None, hint_oracle=None):
    """For one detected transmit: per affected market, read the preconf price, recompute flips, keep only
    reaction prizes (repaid >= hot_min_repaid), check the SHARED kill-switch, prepare_hot, and dispatch
    the passers. Injectable seams (price_fn/reads_fn/prepare_fn/dispatch_fn) for unit tests. Returns the
    dispatch results (list) for logging/tests."""
    from strategy.guard import GuardState
    from chain.execute import fast_lane_disabled, pop_fast_lane_trip   # II.6 circuit breaker
    if dispatch_fn is None:
        from chain.execute import dispatch_liquidations as dispatch_fn  # noqa: F811
    if reads_fn is read_positions and getattr(cfg, "positions_from_cache", False):
        # P1-6 потребление слоя 2 (флаг POSITIONS_FROM_CACHE, OFF по умолчанию): только для
        # ДЕФОЛТНОГО seam (инжектированные reads_fn тестов не трогаем). read_positions_cached
        # сам падает в живой read_positions для рынков вне кэша — нулевой риск непокрытым.
        reads_fn = read_positions_cached
    floor = hot_min_repaid(cfg)
    _gate_px = {}                                            # 2b: цена оракула, прочитанная gate -> переиспользовать в цикле
    if px_hint is not None and hint_oracle is not None:
        # P0-3: feed-декод (FORWARD_SEL точной формы) = сам transmit, gate ему не нужен. Раньше gate ниже
        # ЧИТАЛ preconf (~350мс) и ПЕРЕЗАПИСЫВАЛ хинт; при preconf-лаге (~0.5с) старая цена давала
        # moved=False -> feed-спавн умирал, а poll этого блока уже дедупнут через feed_handled -> триггер
        # уезжал на блок позже. Теперь: хинт = цена цикла, baseline обновляем, gate НЕ читаем (0 RTT).
        _gate_px[hint_oracle] = px_hint
        if last_price is not None:
            _price_moved(agg, px_hint, last_price)           # baseline для последующих bare-match gate'ов
        if stats is not None:
            stats["proceed"] += 1
    note_transmit_for_cascade(agg, block, log=log)   # давка: >=2 агрегаторов в блоке -> tip +1 ступень
    if _WS_TRANSMIT_HOOK is not None:
        try:                            # handoff вне очереди: окно с готовой пробой не ждёт конвейера
            _WS_TRANSMIT_HOOK(feeds.get(agg) or (), subidx)
        except Exception:
            pass                        # handoff опционален — реактивный путь не задевается
    with shared.lock:
        groups = dict(shared.groups); debt_by = dict(shared.debt_by); debt_assets_by = dict(shared.debt_assets_by)

    # Поправка 07-17 п.2 (остаточный preconf-RTT): оракулы рынков этого agg вне _gate_px читались
    # ПОСЛЕДОВАТЕЛЬНО внутри цикла (~110мс каждый из Charlotte) -> рынок №2/№3 ждал СУММУ чужих RTT
    # (наблюдалось в журнале: price=0ms -> 114ms -> 110ms), и канарейка стреляла после всех. Теперь
    # все недостающие цены стартуют параллельно ЗДЕСЬ; цикл ждёт только СВОЙ оракул (Event).
    # Hint-рынок не ждёт ничего (цена уже в _gate_px). 07-23: префетч поднят ВЫШЕ гейта — гейт
    # потребляет префетч rep-оракула вместо собственного RTT (было последовательно: в деградацию
    # preconf гейт 0.4с-кап + префетч 0.4с-кап = dt_spawn 867мс; параллельно = один кап ~0.5с,
    # в норме ~100мс вместо ~200). Цена: reader-tx (гейт скопирует) тратит спавн лёгких чтений.
    _px_prefetch = {}                            # oracle -> (Event, [px|None])
    for _mid_pf in feeds.get(agg, []):
        _om_pf = meta.get(_mid_pf)
        if not _om_pf or not groups.get(_mid_pf):
            continue
        _o_pf = _om_pf[0]
        if _o_pf in _gate_px or _o_pf in _px_prefetch:
            continue
        _ev_pf, _box_pf = threading.Event(), []
        _px_prefetch[_o_pf] = (_ev_pf, _box_pf)

        def _pf_read(_o=_o_pf, _ev=_ev_pf, _box=_box_pf):
            try:
                _box.append(read_price_hedged(preconf_rpc, rpc, _o, price_fn=price_fn))
            except Exception:
                pass
            finally:
                _ev.set()
        threading.Thread(target=_pf_read, daemon=True).start()

    # price-change gate: the bare match also fires on price-READ txs. Read ONE representative oracle
    # preconf price; if it has not moved since last sighting this was a reader, not a transmit -> skip
    # the 340-call multicall + flips. This is what makes the bare match viable on the hot path.
    # 07-23: rep берётся из УЖЕ летящего префетча (гейт ‖ префетч, см. коммент выше); свой
    # хедж-RTT — только если rep-рынок без кандидатов (не префетчится).
    if not (px_hint is not None and hint_oracle is not None) and last_price is not None:
        rep = next((meta[m][0] for m in feeds.get(agg, []) if meta.get(m)), None)
        if rep is None:
            return []
        if rep in _px_prefetch:
            _ev_g, _box_g = _px_prefetch[rep]
            _ev_g.wait(timeout=3.0)                          # хедж внутри префетча сам капит (0.4с+confirmed)
            px = _box_g[0] if _box_g else None
        else:
            px = read_price_hedged(preconf_rpc, rpc, rep, price_fn=price_fn)
        if px is not None:
            _gate_px[rep] = px                               # 2b: рынок с этим оракулом не будет читать 2-й раз
        moved = _price_moved(agg, px, last_price)
        if stats is not None:                                # health buckets (none=preconf unreadable)
            stats["none" if px is None else ("proceed" if moved else "skip")] += 1
        if not moved:
            return []

    ready, meta_by = [], {}
    _canary_fired = False                        # ранний выстрел на hint-рынке (см. ниже) — один на транзит
    _t_read_total = 0.0; _t_flips_total = 0.0   # DIAG latency: read=340-call, sim=prepare_hot
    _prep_acc = {"ms": 0.0}                      # Р1: сумма ПО ТРЕДАМ подготовки (может превышать wall)
    _rdy_lock = threading.Lock()                 # ready/meta_by/учёт — из нескольких prep-тредов
    _incr_results = []                           # инкрементальный dispatch (HOT_DISPATCH_INCREMENTAL)
    _incr_state = {"blocked": None, "t_send": 0.0, "threads": [], "rlock": threading.Lock(),
                   "glock": threading.Lock()}    # Р1: kill-switch спрашивают N тредов разом

    def _dispatch_ready_now(item):
        """Готовая цель уходит немедленно; kill-switch перед КАЖДЫМ сендом (та же GuardState,
        что у батч-пути). Блок = стоп дальнейших сендов этого транзита (алерт один раз).
        Сенд+receipt — в своём треде: wait_receipts внутри dispatch (до ~4с на tx, боевой замер
        07.08) НЕ должен тормозить симы следующих целей (иначе вернули бы половину барьера);
        nonce-серийность держит _SEND_LOCK внутри dispatch. Receipts дожидаемся в конце (журнал)."""
        if _incr_state["blocked"]:
            return
        from chain.execute import current_inflight, specfire_gas_today
        today = store.realized_today()
        blocked = guard.blocked_reason(GuardState(realized_net_today=today["net"],
                                                  gas_spent_today=today["gas"] + specfire_gas_today(),
                                                  inflight=current_inflight()))
        if blocked:
            # Р1: защёлку и пейдж ставит ПЕРВЫЙ увидевший — N параллельных prep-тредов
            # спрашивают гард одновременно, и без этого лока один kill-switch дал бы N
            # одинаковых тревог (алерт — учёт, он обязан висеть сбоку и не множиться).
            # #52: ТРАНЗИЕНТНУЮ причину (нет свободных слотов inflight) НЕ защёлкиваем — слоты
            # освобождаются на квитанции за секунды, а латч выключал хвост каскада на ТОМ САМОМ
            # транзите, который и есть FCFS-гонка. Защёлкиваются только терминальные причины
            # (дневные капы): они до полуночи UTC не рассосутся, и там латч — это защита.
            if guard.is_transient(blocked):
                _incr_state["skipped_transient"] = _incr_state.get("skipped_transient", 0) + 1
                if _incr_state.get("skipped_transient", 0) % 20 == 1:
                    log.info("HOT incr: слот занят (%s) — цель пропущена, латч НЕ ставим "
                             "(пропусков %d)", blocked, _incr_state["skipped_transient"])
                return
            with _incr_state["glock"]:
                if _incr_state["blocked"]:
                    return
                _incr_state["blocked"] = blocked
            log.error("HOT kill switch engaged: %s — инкрементальный dispatch остановлен", blocked)
            alerter.send_async("\U0001F6D1 hot path halted: %s" % blocked, key="hot-killswitch")
            return

        def _run(it=item):
            _ds1 = time.perf_counter()
            _incr_state["t_prep_wall"] = time.time()   # СТАДИИ dispatch: prep кончился ЗДЕСЬ
            try:                                       # (боевой путь по умолчанию — инкрементальный)
                r = dispatch_fn(rpc, cfg, [it], log=log, max_inflight=cfg.max_inflight)
            except Exception as e:                # bare-тред: исключение не должно пропасть молча
                log.error("HOT incr-dispatch error %s/%s: %s: %s",
                          it[0][:10], it[1][:10], type(e).__name__, redact_clip(e, 80))
                r = []
            with _incr_state["rlock"]:
                _incr_results.extend(r)           # t_send = СУММА по тредам (может превышать wall)
                _incr_state["t_send"] += (time.perf_counter() - _ds1) * 1000.0
        _th = threading.Thread(target=_run, daemon=True)
        _incr_state["threads"].append(_th)
        _th.start()

    def _prep_ms(sec):
        """УЧЁТ СБОКУ: собственный try — счётчик не имеет права уронить или задержать огонь
        (13.08: счётчик атрибуции внутри _prearm_board под широким except молча гасил арм)."""
        try:
            with _rdy_lock:
                _prep_acc["ms"] += sec * 1000.0
        except Exception:                                    # noqa: BLE001
            pass

    def _ready_now(_mid, b, prep, hr, du, fast=False):
        """Готовая цель: сперва ОТПРАВКА, журнал ПОСЛЕ. Порядок не косметический — учёт,
        стоящий ПЕРЕД сендом, у нас уже гасил арм."""
        with _rdy_lock:
            ready.append((_mid, b, prep)); meta_by[(_mid, b)] = (hr, du)
        if HOT_DISPATCH_INCREMENTAL:
            _dispatch_ready_now((_mid, b, prep))
        try:
            log.info("HOT ready%s %s/%s hf=%.4f repaid~$%.0f net%s$%.2f block=%d subidx=%s",
                     " FAST" if fast else "", _mid[:10], b[:10], hr.hf, du,
                     ">=" if fast else "=", prep.get("net_usd", 0.0), block, subidx)
        except Exception:                                    # noqa: BLE001
            pass

    for mid in feeds.get(agg, []):
        om = meta.get(mid)
        borrowers = groups.get(mid, [])
        if not om or not borrowers:
            if stats is not None: stats["f_nocand"] += 1
            continue
        oracle, lltv_wad = om
        _pxs = time.perf_counter()
        if oracle in _gate_px:                               # 2b: gate уже прочитал этот оракул -> без 2-го RTT
            price = _gate_px[oracle]
        elif oracle in _px_prefetch:                         # 07-17: параллельный префетч стартовал до цикла
            _ev_w, _box_w = _px_prefetch[oracle]
            _ev_w.wait(timeout=5.0)
            price = _box_w[0] if _box_w else None
        else:                                                # страховка (префетч покрывает штатно)
            price = read_price_hedged(preconf_rpc, rpc, oracle, price_fn=price_fn)
        _t_price_last = (time.perf_counter() - _pxs) * 1000.0
        stage_note("price", _t_price_last)
        if stats is not None:                                # 24.07: ДЕНЕЖНЫЙ путь = цена из flashblocks-WS
            k = "px_free" if _t_price_last < 5.0 else "px_rpc"   # (декод/гейт/префетч, 0мс) без RPC вообще.
            stats[k] = stats.get(k, 0) + 1                   # px_rpc = чтения, которым preconf реально нужен.
        if price is None:
            if stats is not None: stats["f_noprice"] += 1
            continue
        _rs = time.perf_counter()
        _src_out = {}
        rp = reads_fn(rpc, mid, borrowers, preconf_rpc=preconf_rpc, src_out=_src_out)
        _rsrc = _src_out.get("src", "?")
        with _POSITION_CACHE_LOCK:
            _cached = _POSITION_CACHE.get(mid)
        if _cached is not None:
            _in_cache, _total, _avg_age, _max_age, _missing = _cache_coverage_stats(borrowers, _cached[2], time.time())
            log.info("DIAG cache-coverage market=%s in_cache=%d/%d avg_age=%.1fs max_age=%.1fs missing=%s",
                     mid[:10], _in_cache, _total, _avg_age, _max_age, ",".join(_missing) if _missing else "-")
        _t_read_last = (time.perf_counter() - _rs) * 1000.0; _t_read_total += _t_read_last
        stage_note("read", _t_read_last)
        if not rp:
            if stats is not None: stats["f_norp"] += 1
            continue
        tba, tbs, pos = rp
        if _cached is not None and _rsrc != "cache":
            # P1-6 log-only КАЛИБРОВКА (гейт потребления по чеклисту STATE): значения кэша vs живое
            # чтение на том же транзите. mism=0 на потоке -> кэш можно включать (POSITIONS_FROM_CACHE=1).
            _nw = time.time(); _same = 0; _mm = []
            for (_b2, _bs2, _col2) in pos:
                _c2 = _cached[2].get(_b2)
                if _c2 is None or _nw - _c2[2] > _POSITION_CACHE_MAX_AGE_SEC:
                    continue
                if _c2[0] == _bs2 and _c2[1] == _col2:
                    _same += 1
                else:
                    _mm.append(_b2[:8])
            log.info("DIAG cache-divergence market=%s same=%d mism=%d tba_eq=%s tbs_eq=%s bad=%s",
                     mid[:10], _same, len(_mm), _cached[0] == tba, _cached[1] == tbs,
                     ",".join(_mm[:5]) if _mm else "-")
        log.info("DIAG hotset mid=%s src=%s trig=%s agg=%s n=%d block=%d",   # agg= атрибуция ноги-триггера (multi-leg 07-23)
                 mid[:10], _rsrc, subidx, agg[:10], len(pos), block)  # full set on disk
        positions = [(b, bs, col, debt_by.get((mid, b), 0.0)) for (b, bs, col) in pos]
        pos_by = {b: (bs, col) for (b, bs, col) in pos}      # P0-4: state для prepare_hot без повторного RTT
        _fls = time.perf_counter()                           # 07-17 п.1: CPU _flips по непруненному набору — мерить
        flipped = _flips(price, lltv_wad, tba, tbs, positions)
        _t_flips_last = (time.perf_counter() - _fls) * 1000.0; _t_flips_total += _t_flips_last
        stage_note("flips", _t_flips_last)
        if not flipped:
            if stats is not None: stats["f_noflip"] += 1
            if positions:                                    # DIAG: what state did we read? (preconf-source check)
                from chain.simulate import MarketContext as _MC, health_from as _hf
                _ctx = _MC(oracle="", price=int(price), lltv_wad=int(lltv_wad),
                           total_borrow_assets=int(tba), total_borrow_shares=int(tbs))
                _c = [(bb, bs, _hf(_ctx, bs, col).hf) for (bb, bs, col, _du) in positions]
                _bb, _bs, _hv = min(_c, key=lambda x: x[2])
                log.info("DIAG noflip %s/%s minHF=%.4f bshares=%d px=%d n=%d src=%s trig=%s price=%.0fms read=%.0fms flips=%.1fms e2e=%.0fms",
                         mid[:10], _bb[:10], _hv, _bs, int(price), len(_c), _rsrc, subidx, _t_price_last, _t_read_last,
                         _t_flips_last, (time.time() - t) * 1000.0)
        # Р1: ОТБОР ЦЕЛЕЙ (дёшево, последовательно) -> ПОДГОТОВКА (632мс сети, параллельно).
        # Порог floor и фильтр трупов стоят ДО дорогой работы: 632мс, потраченные на пыль или
        # на труп, отнимают из 4-секундного бюджета каскада ровно одну ЖИВУЮ цель хвоста.
        _cand, _corpses, _asked = [], None, False
        for (b, hr, du) in flipped:
            if du < floor:                                   # reaction filter: $2k+ only
                if stats is not None: stats["f_floor"] += 1
                continue
            _bc = pos_by.get(b)
            if not _asked:                                   # #110: снимок пар из ПАМЯТИ индекса,
                _asked = True                                # без сети, и только когда есть ЗА ЧТО
                _corpses = corpse_pairs(stats=stats, log=log)   # платить (транзит без флипов не платит)
            _row = _corpses.get((str(mid).lower(), str(b).lower())) if _corpses is not None else None
            # Третий признак трупа (огрызок нашего кап-выстрела) индекса НЕ ТРЕБУЕТ: факт целиком
            # наш, поэтому вердикт спрашивается и при _row=None. Ключ и живой залог — отсюда.
            _ckey = (mid, b)
            _ccol = _bc[1] if _bc else None
            if corpse_verdict(_row, _bc[0] if _bc else None, key=_ckey, collateral=_ccol):
                if stats is not None:
                    stats["f_corpse"] = stats.get("f_corpse", 0) + 1
                _stub, _left = cap_corpse(_ckey, _ccol)
                if _stub:
                    log.info("HOT corpse-skip %s/%s: ОГРЫЗОК нашего кап-выстрела (залога осталось "
                             "%.2f bps от предвыстрельного, порог %.0f) — подготовку не платим",
                             mid[:10], b[:10], _left, CAP_CORPSE_MAX_BPS)
                else:
                    log.info("HOT corpse-skip %s/%s: индекс видел добой (repaid=%s bad_debt=%s, "
                             "наш borrow_shares=%s) — 632мс подготовки не платим",
                             mid[:10], b[:10], _row[0] if _row else "?",
                             _row[1] if _row else "?", _bc[0] if _bc else "?")
                continue
            if stats is not None: stats["f_prep"] += 1
            _cand.append((b, hr, du, _bc))

        def _prep_one(b, hr, du, _bc, _mid=mid, _price=price, _tba=tba, _tbs=tbs):
            """Подготовка ОДНОЙ цели — прежнее тело цикла, плюс заявка и свой try/finally.

            Заявка: рынок бывает под ДВУМЯ агрегаторами (multi-leg), обе ноги спавнят свой
            транзит и видят одну цель; prepare НЕ дублируем (2-я tx сожгла бы nonce+газ).
            try: одна упавшая цель не имеет права унести ВЕСЬ хвост каскада — раньше исключение
            из подготовки выходило наружу и убивало остальные цели транзита."""
            _key = (_mid, b)
            if not claim_prep(_key):
                if stats is not None:
                    stats["f_claim"] = stats.get("f_claim", 0) + 1
                log.info("HOT prep-claim %s/%s: цель уже готовит другая нога — дубль не платим",
                         _mid[:10], b[:10])
                return
            try:
                # II.6 пер-парный брейкер: спрашиваем про СВОЮ пару, а не «выключен ли путь
                # вообще». Глобальный рубеж внутри всё равно перекрывает всё; параметры рынка
                # берём из того же кэша, что и prepare_fast (RTT не добавляется).
                _fl_pm = _FULL_PARAMS_CACHE.get(_mid)
                _fl_pair = (_fl_pm[1], _fl_pm[0]) if _fl_pm else None   # (coll, loan)
                if (getattr(cfg, "fast_lane", False) and not fast_lane_disabled(_fl_pair)   # II.6: breaker -> фолбэк sim
                        and _bc and du >= getattr(cfg, "fast_lane_min_repaid_usd", 1000.0)
                        and du <= getattr(cfg, "fast_lane_max_repaid_usd", 500000.0)):  # мега-сайз -> sim
                    _fs = time.perf_counter()                # P1-9: fast-lane БЕЗ sim; при ok обычный
                    fprep = prepare_fast(rpc, preconf_rpc, cfg, _mid, b, du,
                                         debt_assets_by.get((_mid, b), 0), _price,
                                         (_tba, _tbs, _bc[0], _bc[1]))
                    _prep_ms(time.perf_counter() - _fs)
                    if fprep.get("ok"):
                        _ready_now(_mid, b, fprep, hr, du, fast=True)
                        return
                    log.info("DIAG fast-lane fallback %s/%s: %s", _mid[:10], b[:10], str(fprep.get("reason"))[:60])
                _ps = time.perf_counter()
                # P0-4: (tba,tbs,bs,col) из read_positions этого же
                prep = prepare_fn(rpc, preconf_rpc, cfg, _mid, b, du,   # триггера (мс назад, тот же preconf) -> prepare_hot
                                  debt_assets_by.get((_mid, b), 0), _price,   # не перечитывает market/position (~-350мс)
                                  state=((_tba, _tbs, _bc[0], _bc[1]) if _bc else None))
                _prep_ms(time.perf_counter() - _ps)
                if prep.get("ok"):
                    _ready_now(_mid, b, prep, hr, du)
                else:
                    log.info("HOT skip %s/%s: %s", _mid[:10], b[:10], str(prep.get("reason"))[:80])
            except Exception as e:                           # noqa: BLE001
                log.error("HOT prep error %s/%s: %s: %s", _mid[:10], b[:10],
                          type(e).__name__, redact_clip(e, 80))
            finally:
                release_prep(_key)                           # упавшая подготовка НЕ запирает заёмщика

        run_prep_batch(_cand, _prep_one, log=log)

        if hint_oracle is not None and oracle == hint_oracle and not ready and not _canary_fired:
            # 07-17: канарейка стреляет В ТОЧКЕ, где fast-lane отправил бы hint-рынок, а не после
            # обработки всех соседей agg (пробы 1-100 включали до ~220мс чужих preconf-RTT).
            _canary_fired = True
            _maybe_canary(agg, block, subidx, rpc, cfg, log, t0=t)

    if not ready:
        if not _canary_fired:                              # bare-match транзиты (без hint) — как раньше, после цикла
            _maybe_canary(agg, block, subidx, rpc, cfg, log, t0=t)   # ⭐ нет ликвидации -> пробить гонку
        return []

    if HOT_DISPATCH_INCREMENTAL:                             # сенды уже ушли по готовности (см. выше);
        for _th in _incr_state["threads"]:                   # ждём только receipts для журнала (кап >
            _th.join(timeout=150.0)                          # 120с receipt-таймаута dispatch)
        results = _incr_results
        _t_send = _incr_state["t_send"]
        _t_prep_wall = _incr_state.get("t_prep_wall")   # СТАДИИ: метка prep инкрементального пути
    else:
        from chain.execute import current_inflight, specfire_gas_today
        today = store.realized_today()                       # SHARED kill-switch with the block loop
        blocked = guard.blocked_reason(GuardState(realized_net_today=today["net"],
                                                  gas_spent_today=today["gas"] + specfire_gas_today(),
                                                  inflight=current_inflight()))   # P0: был хардкод 0 — inflight-гейт мёртв
        if blocked:
            log.error("HOT kill switch engaged: %s — %d ready NOT dispatched", blocked, len(ready))
            alerter.send_async(f"\U0001F6D1 hot path halted: {blocked}", key="hot-killswitch")
            return []

        _ds = time.perf_counter()
        _t_prep_wall = time.time()                   # СТАДИИ dispatch: prep-фаза кончилась ЗДЕСЬ
        results = dispatch_fn(rpc, cfg, ready, log=log, max_inflight=cfg.max_inflight)
        _t_send = (time.perf_counter() - _ds) * 1000.0
    _fl_trip = pop_fast_lane_trip()                # II.6: breaker сработал в receipt-цикле dispatch -> TG-пейдж
    if _fl_trip:
        alerter.send_async(_fl_trip, key="fastlane-breaker")
    from chain.execute import pop_lost_race        # Ш16: деньги прошли мимо -> сообщение
    _lr = pop_lost_race()
    if _lr:
        alerter.send_async(_lr, key="lost-race")
    from chain.execute import pop_batch_empty_trip
    _be_trip = pop_batch_empty_trip()              # мультибатч: серия пустых батчей (nOk=0) -> TG-пейдж
    if _be_trip:
        alerter.send_async(_be_trip, key="batch-empty")
    _t_prep_total = _prep_acc["ms"]              # Р1: СУММА по prep-тредам (не wall-clock)
    stage_note("prep", _t_prep_total); stage_note("e2e", (time.time() - t) * 1000.0)
    if _t_send:
        stage_note("send", _t_send)
    log.info("DIAG timing read=%.0fms flips=%.0fms sim=%.0fms send=%.0fms e2e=%.0fms n_ready=%d",
             _t_read_total, _t_flips_total, _t_prep_total, _t_send, (time.time() - t) * 1000.0, len(ready))
    for r in results:
        hr, du = meta_by.get((r["market_id"], r["borrower"]), (None, 0.0))
        status = (f"submitted:{r.get('status')} net${r.get('net_usd',0):.2f}" if r["sent"]
                  else f"skip:{str(r.get('reason'))[:50]}")
        store.log_action(market_id=r["market_id"], borrower=r["borrower"], mode=cfg.mode,
                         tx_hash=r.get("hash"), net_usd=r.get("net_usd", 0.0), gas_usd=r.get("gas_usd", 0.0), status=status)
        log.info("HOT ACTIONABLE %s/%s status=%s tx=%s", r["market_id"][:10], r["borrower"][:10], status, r.get("hash"))
        # СТАДИИ dispatch-тракта (пост-мортем $128k): следующая такая гонка даёт адрес стадии
        # вместо гипотезы. detect = спавн обработки транзита (t), prep = конец сборки ready.
        # Прибор сбоку: без меток строка честно молчит, отказ глотается.
        if r.get("sent") and r.get("hash"):
            try:
                from chain.stage_probe import stamp_dispatch as _sd2, profile as _pf2, \
                    format_dispatch as _fd2
                _sd2(r["hash"], {"detect": t, "prep": _t_prep_wall})   # None-метки не пишутся
                _dl, _dworst = _fd2(_pf2(r["hash"]))
                if _dl != "—":
                    log.info("HOT СТАДИИ tx=%s: %s | топ-виновник: %s%s",
                             str(r["hash"])[:14], _dl, _dworst[0] or "—",
                             (" %.0fмс" % _dworst[1]) if _dworst[1] is not None else "")
            except Exception:                        # noqa: BLE001
                pass
        alerter.send_async(f"\U0001F525 HOT {cfg.mode} {r['borrower'][:10]} {status}", key=f"hot:{r['market_id']}:{r['borrower']}")
    return results


# ---- async flashblock subscriber (detect + spawn) ----

def _init_resolve_retry(fn, *args, what="", tries=14, base=0.5, cap=10.0, log=None, empty_ok=True):
    """Init-резолв hot_loop на keyed-RPC с rate-лимитом (напр. dRPC Free = 100 req/s): при старте бот
    выстреливает залп разом (candidate refresh + mega-fetch из block-loop + resolve) — на миг >лимита →
    429. Ретраим с backoff (капнут cap): залп схлынет и повтор пройдёт, поток hot_loop НЕ умирает.
    tries×cap подобраны пережить mega-fetch залп (~8с, но повторяется каждые ~30с) — 14 попыток,
    backoff 0.5→10с ≈ до ~110с суммарно. При живом dRPC резолв проходит с 1-й попытки (нулевая задержка).
    На Alchemy (500 req/s) 429 не приходил, потому хрупкость не вылезала (инцидент 07-21/22)."""
    for i in range(tries):
        try:
            out = fn(*args)
            if out or empty_ok:
                return out
            # empty_ok=False: resolve_feeds глотает пер-рыночные ошибки и в шторм отдаёт {} БЕЗ
            # исключения (07-23: «0 aggregators») — считаем пустоту отказом и ретраим как 429.
            raise RuntimeError("empty resolve result")
        except Exception as e:
            if i == tries - 1:
                if not empty_ok:
                    if log is not None:
                        log.warning("hot_loop init %s: пусто после %d попыток (%s) — стартую слепым, "
                                    "ре-резолв подберёт ускоренным циклом", what, tries, type(e).__name__)
                    return {}
                raise
            wait = min(base * (2 ** i), cap)
            if log is not None:
                log.warning("hot_loop init %s: %s — retry %d/%d через %.1fс (rate-limit burst)",
                            what, type(e).__name__, i + 1, tries, wait)
            time.sleep(wait)


async def hot_loop(rpc, preconf_rpc, cfg, shared, store, guard, alerter, log, stop, markets,
                   ws_factory=None):
    """Subscribe to Flashblocks; on a PRECISE transmit to one of our aggregators, spawn a worker thread
    that runs _process_transmit (the recv loop itself does no slow work). Re-resolves feeds/meta on a
    cadence (rotation). DORMANT until block_driven_loop starts this behind cfg.hot_path."""
    import asyncio
    import json
    import websockets
    import brotli
    from chain.feeds import (resolve_feeds, extract_txs, block_number_of, decode_forward_answer,
                             FORWARD_SEL, basefee_of, primary_map, tip_from_raw)
    from chain.execute import note_transit_tip as _ntt_live   # живое кольцо: импорт ВНЕ петли
    # ФОНОВЫЕ чтения -> бесплатный публичный RPC (CU-замер 25.07: фоновые читатели съедали 5×
    # квоту Alchemy). Пусто/ошибка конструктора -> прежний rpc, поведение как раньше.
    _bg_rpc = rpc
    try:
        _bg_url = str(getattr(cfg, "bg_rpc_url", "") or "")
        if _bg_url and _bg_url != cfg.rpc_url:
            from chain.rpc import BaseRpc as _BR, guard_chain_id as _gci
            _bg_rpc = _gci(_BR(_bg_url, cfg.chain_id, request_timeout=10), cfg.chain_id,
                           "bg-ключ фоновых чтений", fallback=rpc, log=log)   # #117 гард сети
            # 05.08: только хост — полный URL нёс ключ в журнал (гигиена ключей, как crc-тег CU)
            log.info("фоновые чтения -> %s… (Alchemy остаётся денежному пути)",
                     _bg_url.split("//")[-1].split("/")[0])
    except Exception:
        _bg_rpc = rpc
    # #117: буст-циклы кэша позиций (<=10 560 eth_call/сут) — на ТРЕТИЙ ключ. Слой по построению
    # резервный (в штиль это stale-фолбэк, потребитель при промахе дочитывает живьём), поэтому
    # 429 на C стоит нам refresh-fail, а не блока. Пусто/отказ => боевой rpc, бит-в-бит как было.
    try:
        from chain.rpc import build_combat_read_rpc
        _c_rpc = build_combat_read_rpc(cfg, rpc, log=log)
    except Exception:
        _c_rpc = rpc

    def _default_ws():
        # 20.08: чтения суб-блоков — с ВЫДЕЛЕННОГО адреса (WS_READ_SRC_IP). Замер трёх точек
        # доказал троттлинг flashblocks ПО АДРЕСУ: основной IP 26-78 сообщений/мин с обрывами,
        # второй адрес той же машины в те же минуты — 330/мин без единого. Пусто = как было.
        import ssl as _ssl
        from chain.feeds import ws_bound_socket as _wbs
        _sock = _wbs(FB_URL)
        if _sock is None:
            return websockets.connect(FB_URL, open_timeout=20, ping_interval=20, max_size=None)
        return websockets.connect(FB_URL, sock=_sock, ssl=_ssl.create_default_context(),
                                  server_hostname="mainnet.flashblocks.base.org",
                                  open_timeout=20, ping_interval=20, max_size=None)
    ws_factory = ws_factory or _default_ws

    from chain.rpc import BaseRpc
    from chain.rpc import guard_chain_id as _gci_fb                              # #117 гард сети
    _fb_resolve_rpc = _gci_fb(
        BaseRpc(os.environ.get("FEEDS_FALLBACK_RPC", "https://mainnet.base.org"),
                getattr(rpc, "chain_id", 8453), request_timeout=8.0),
        getattr(rpc, "chain_id", 8453), "FEEDS_FALLBACK_RPC", fallback=rpc, log=log)
    # #139: поблочный опрос — на КЛОНАХ с жёстким таймаутом (боевой preconf_rpc и _bg_rpc свой
    # таймаут сохраняют: цикл кэша штатно живёт 1.9-2.4с на _bg_rpc, ему тесная граница вредна).
    _poll_preconf_rpc = _poll_rpc(preconf_rpc)
    _poll_fb_rpc = _poll_rpc(_bg_rpc)
    feeds = _init_resolve_retry(resolve_feeds, _bg_rpc, markets, rpc,   # 25.07: резолв (283 лёгких
                                # вызова × 2 на рестарт + раз в 6ч) — фон, ему не место на платном
                                what="feeds", log=log, empty_ok=False)

    def _resolve_meta_fb():
        # 17:44 23.07: init-meta исчерпала 14 ретраев за ~8.4мин шторма и УРОНИЛА hot_loop
        # (self-heal рестартил его циклом). Симметрия с f96854d: фолбэк-эндпоинт + empty_ok=False
        # ниже = слепой старт вместо смерти; слепая meta ре-резолвится ускоренно (120с, bg).
        try:
            return resolve_meta(_bg_rpc, markets)      # 25.07: meta иммутабельна — чистый фон
        except Exception:
            try:
                return resolve_meta(rpc, markets)
            except Exception:
                return resolve_meta(_fb_resolve_rpc, markets)
    meta = _init_resolve_retry(_resolve_meta_fb, what="meta", log=log, empty_ok=False)
    agg_set = set(feeds)
    shadow_map = _build_shadow_map(feeds, meta, markets, primary=primary_map())   # SHADOW слоя 1 (log-only спавн-гейт)
    log.info("DIAG shadow-feed map %s", {a[:10]: s for a, (s, _sc, _o) in shadow_map.items()})
    # 07-23: init-резолв конкурирует со стартовым залпом (mega-fetch жжёт оба эндпоинта) и приходит
    # ЧАСТИЧНЫМ (23 ключа против 27 в спокойный замер) — первый ре-резолв назначаем через 180с
    # (залп улёгся, ноги добираются), дальше обычный каденс. Guard «пусто не затирает» стоит ниже.
    last_resolve = time.monotonic() - max(0.0, cfg.rescan_interval_sec - 180.0)
    log.info("hot path: %d aggregators / %d markets (preconf=%s, tip=%.1f gwei, floor repaid $%.0f)",
             len(agg_set), len(meta), preconf_rpc.rpc_url, cfg.tip_gwei, hot_min_repaid(cfg))
    _seq_url = str(getattr(cfg, "sequencer_rpc", "") or "")   # 07-17: тёплый keep-alive к секвенсеру ДО
    if _seq_url and _seq_url.lower() not in ("off", "none", "0"):   # первого выстрела (холодный TLS ~2-3 RTT)
        from chain.execute import warm_broadcast
        warm_broadcast([_seq_url])

    last_spawn: dict = {}        # agg -> last spawn time.time() (throttle; survives ws reconnects)
    last_price: dict = {}        # agg -> last preconf price of representative oracle (change-gate)
    stats = {"spawn": 0, "skip": 0, "proceed": 0, "none": 0,    # hot-path health; emitted every HOT_STATS_SEC
             "poll": 0, "pspawn": 0, "poll_none": 0, "poll_fb": 0,  # poll_fb=oracles filled from latest fallback
             "f_nocand": 0, "f_noprice": 0, "f_norp": 0, "f_noflip": 0, "f_floor": 0, "f_prep": 0}  # funnel proceed->prepare_hot
    last_stats = time.monotonic()
    poll_seen: dict = {}         # oracle -> last preconf price (poll-local; baseline -> spawn only on move)
    feed_handled: set = set()    # Layer1: (oracle, bn) spawned by feed this block -> poll skips dup; cleared on block change
    last_poll_block = None       # one preconf-price poll per block
    fb_windows = 0               # consecutive stats windows with poll_fb>0 (preconf degradation streak)
    _fb_backoff_n = 0            # подряд-неудачные reconnect'ы flashblocks -> экспон. backoff. У flashblocks
                                 # per-IP лимит на ЧАСТОТУ коннектов: фикс-3с реконнект при транзиентном сбое
                                 # Base превращал его в самоподдерживающийся 429-шторм на час (инцидент 07-21).
    last_fb_alert = 0.0          # monotonic of last degradation alert (anti-flood)
    # #117: защёлки CU переехали в МОДУЛЬНЫЕ пер-хостовые _CU_MARK_TS/_CU_PAGE_TS. Здесь их
    # держать было нельзя: локальная переменная обнулялась при self-heal-перезапуске hot_loop,
    # то есть сторож слеп ровно от рестартов — та самая ошибка 07.08, от которой ушли, заменив
    # «6ч аптайма» на накопленный span, и которая тихо вернулась через область видимости.
    recon_ts = []                # monotonic'ы реконнектов hot-path ws; одиночный разрыв публичного
    last_recon_alert = 0.0       # Flashblocks-WS = НОРМА (роняет соединение раз в час-два) -> TG молчит,
                                 # алерт ТОЛЬКО на серию (3+ за 180с = реальная нестабильность фида/сети)
    _re_thread = None            # 07-23: ре-резолв УШЁЛ ИЗ recv-петли — inline-резолв глушил hot-path
    _re_box = None               # на всю длительность (спокойный замер multi-leg: 184с петля не читает
    _re_started = 0.0            # WS -> триггеры мертвы). Фоновый тред кладёт результат в box,
                                 # петля коммитит готовое; один резолв за раз (как _spawn_cache_refresh).
                                 # _re_started: orphan-гард — Thread-245 висел 7+ мин на чёрнодырном
                                 # эндпоинте (per-recv timeout переживает trickle), блокируя рескан.
    while not stop.is_set():
        try:
            async with ws_factory() as ws:
                _fb_backoff_n = 0            # коннект установлен -> сброс backoff
                while not stop.is_set():
                    # слепой старт (feeds ИЛИ meta пусты) ре-резолвится ускоренно (120с), не через 6ч rescan
                    _rescan_iv = cfg.rescan_interval_sec if (feeds and meta) else min(cfg.rescan_interval_sec, 120.0)
                    if (_re_thread is not None and _re_thread.is_alive()
                            and time.monotonic() - _re_started > 300.0):
                        # висящий резолв (чёрная дыра эндпоинта) не должен блокировать рескан:
                        # бросаем daemon-тред (его box осиротел — коммит его не прочтёт), повтор ~120с
                        log.warning("re-resolve завис >300с — бросаю тред, повтор ~120с (карта прежняя, %d aggs)",
                                    len(agg_set))
                        _re_thread = None; _re_box = None
                        last_resolve = time.monotonic() - max(0.0, _rescan_iv - 120.0)
                    if _re_thread is not None and not _re_thread.is_alive() and _re_box is not None:
                        _res = _re_box.get("res")
                        log.info("DIAG re-resolve commit res=%s dt=%.0fs",
                                 "ok" if _res else "none", time.monotonic() - _re_started)
                        _re_thread = None; _re_box = None
                        if _res:
                            new_feeds, new_meta = _res
                            last_resolve = time.monotonic()
                            if (new_feeds or not feeds) and (new_meta or not meta):
                                feeds = new_feeds; meta = new_meta; agg_set = set(feeds)
                                shadow_map = _build_shadow_map(feeds, meta, markets, primary=primary_map())   # ротация агрегатора
                                log.info("re-resolve: %d watch keys / %d market-legs",
                                         len(agg_set), sum(len(v) for v in feeds.values()))
                            else:
                                # шторм на обоих эндпоинтах: пустой картой ЖИВУЮ не затираем
                                log.warning("re-resolve пуст (feeds=%d meta=%d) — держу старую карту (%d aggs)",
                                            len(new_feeds), len(new_meta), len(agg_set))
                        else:
                            # резолв упал целиком (исключение в bg): повтор через ~120с, не через 6ч
                            last_resolve = time.monotonic() - max(0.0, _rescan_iv - 120.0)
                            log.warning("re-resolve упал (шторм) — повтор ~120с, карта прежняя (%d aggs)", len(agg_set))
                    if (time.monotonic() - last_resolve >= _rescan_iv
                            and not (_re_thread is not None and _re_thread.is_alive())):
                        _re_box = {}
                        _re_started = time.monotonic()
                        log.info("DIAG re-resolve spawn iv=%.0f since_last=%.0fs",
                                 _rescan_iv, time.monotonic() - last_resolve)

                        def _bg_resolve(box=_re_box, cur_meta=meta):
                            try:
                                nf = resolve_feeds(_bg_rpc, markets, rpc)
                            except Exception:
                                box["res"] = None
                                return
                            try:
                                nm = resolve_meta(rpc, markets)
                            except Exception:
                                # meta иммутабельна per market (idToMarketParams) — 429 на одном
                                # aggregate3 рескана не должен блокировать коммит карты ног
                                # (17:33 23.07: час тихих ретраев именно из-за этого)
                                try:
                                    nm = resolve_meta(_fb_resolve_rpc, markets)
                                except Exception:
                                    nm = cur_meta
                            box["res"] = (nf, nm)
                        _re_thread = threading.Thread(target=_bg_resolve, daemon=True)
                        _re_thread.start()
                    if time.monotonic() - last_stats >= HOT_STATS_SEC:
                        try:                       # учёт CU (25.07): кто жжёт квоту Alchemy
                            from chain.rpc import rpc_usage_take, rpc_usage_cu, cu_rolling_add
                            _snap = rpc_usage_take()
                            _win = max(1e-9, time.monotonic() - last_stats)
                            for _host, _ms in sorted(_snap.items()):
                                _cu = rpc_usage_cu({_host: _ms})
                                # #117: 6 -> 10. Ключ учёта становится `метод@вызыватель`, когда
                                # включён CU_ATTRIBUTION=1 (env, кода не требует — см. rpc.py
                                # _caller_tag). Тогда один метод дробится на СТОЛЬКО строк,
                                # сколько у него вызывателей, и шестёрка вытесняла хвост: на
                                # ключе A один eth_call даёт минимум четыре разных вызывателя
                                # (PREARM, буст кэша, confirmed-нога read_positions, патрон
                                # флота). Ради чего заводится атрибуция, тем срез её и глушил.
                                _top = sorted(_ms.items(), key=lambda kv: -CU_TOP_KEY(kv))[:10]
                                _proj = _cu / _win * 86400 * 30 / 1e6
                                _roll = cu_rolling_add(_host, _cu, _win)
                                log.info("CU %s: %d CU/окно = %.0f CU/s (проекция %.1fM/мес, "
                                         "скольз.%.0fмин %.1fM/мес) | %s",
                                         _host, _cu, _cu / _win, _proj, _roll[1] / 60.0, _roll[0],
                                         " ".join("%s×%d" % (m, n) for m, n in _top))
                                # CU-маркер квоты — ЖУРНАЛ, не TG (02.08, kelbic: сообщения без
                                # действия; сыпал бут-спайками после каждого рестарта и штормовой
                                # утечкой ассесса на №2). Доктрина: алерт = исход, не предвестник;
                                # исход покрыт — смерть №2 по квоте = COMBAT-FAILOVER свитч со
                                # своей TG-строкой. Порядок эвакуации из STATE остаётся в маркере.
                                # Порог берём со СКОЛЬЗЯЩЕЙ величины, а не с одного окна: замер
                                # 07.08 — max проекции расходится с фактом в 6.7-40 раз (одиночное
                                # окно спайкует), а СРЕДНЕЕ проекций совпадает с фактом на 0.1-1.5%.
                                # 20.08: скользящей ОДНОГО момента мало для ПЕЙДЖА ЧЕЛОВЕКУ.
                                # За утро тревога по квоте пришла kelbic ТРИЖДЫ (37M, 25M, 50M),
                                # а факт по сумме окон был 24M/14.6M — спайки давали рестарт,
                                # прогрев балансов 220 ног и волна пересборки лестниц. Журнальный
                                # МАРКЕР остаётся на прежнем пороге (он ничего не будит), а
                                # человека будим только когда превышение ДЕРЖИТСЯ: K подряд окон.
                                # Канон «алерты только там, где нужен человек»: разовый всплеск
                                # решается сам, устойчивый расход — нет.
                                _over = ("alchemy" in _host and _roll[0] >= CU_ALERT_PROJ_M
                                         and _roll[1] >= CU_ALERT_MIN_SPAN_SEC)
                                _streak = _CU_OVER_STREAK.get(_host, 0) + 1 if _over else 0
                                _CU_OVER_STREAK[_host] = _streak
                                # 20.08 (2-й заход): ПЕЙДЖ судит по скользящей БЕЗ бут-прогрева
                                # (cu_rolling_ex_boot) — спан-гейт откладывал пейдж ровно до
                                # тика, где окно ЕЩЁ содержало прогрев (16:27:53 — оба ключа
                                # ложно, устойчивый темп был вдвое ниже). Насыщенный числитель
                                # чинится числителем (Ш23а). Журнальный маркер — по полной.
                                from chain.rpc import cu_rolling_ex_boot as _cu_exb
                                _roll_pg = _cu_exb(_host)
                                _page_worthy = (_over and _streak >= CU_ALERT_STREAK_N
                                                and _roll_pg[0] >= CU_ALERT_PROJ_M
                                                and _roll_pg[1] >= CU_ALERT_PAGE_MIN_SPAN_SEC)
                                _now_cu = time.monotonic()
                                _top3 = " ".join("%s×%d" % (m, n) for m, n in _top[:3])
                                if _over and _now_cu - _CU_MARK_TS.get(_host, -1e9) > CU_MARK_SEC:
                                    _CU_MARK_TS[_host] = _now_cu
                                    _proj = _roll[0]
                                    log.warning(
                                        "CU-квота (журнальный маркер): %s проекция %.0fM/мес >= %.0fM — "
                                        "порядок эвакуации: 1) ассессмент+кэш на dRPC, 2) predict-полл, "
                                        "3) диагностика; отправки/sim НЕ трогать. Топ: %s",
                                        _host, _proj, CU_ALERT_PROJ_M, _top3)
                                # #117: ПЕЙДЖ ЧЕЛОВЕКУ. Отдельная защёлка (сутки на ключ), потому
                                # что журнальный маркер и вызов человека — разные по цене вещи:
                                # маркер полезен часто, будильник обязан звонить редко и по делу.
                                # Канал — тот же alerter, что у refusal-loss/kill-switch; он
                                # ПЕРЕДАН в hot_loop явно, происхождение не угадывается (канон
                                # notify_fn: забыл прокинуть -> молчание, а не самодельный канал).
                                if (_page_worthy and CU_ALERT_TG and alerter is not None
                                        and _now_cu - _CU_PAGE_TS.get(_host, -1e9) > CU_ALERT_PAGE_SEC):
                                    _CU_PAGE_TS[_host] = _now_cu
                                    try:
                                        # число в пейдже — то же, по которому решали (без прогрева)
                                        _days = (CU_QUOTA_M / _roll_pg[0] * 30.0) if _roll_pg[0] > 0 else 0.0
                                        alerter.send_async(
                                            "\U0001F4B8 КВОТА RPC: %s жжёт %.0fM CU/мес при квоте "
                                            "%.0fM — при таком темпе ключ кончится за ~%.0f сут.\n"
                                            "Топ: %s\n"
                                            "Нужен ЧЕЛОВЕК: ротация/страйп/докупка ключа. Порядок "
                                            "эвакуации: 1) ассессмент+кэш на dRPC, 2) predict-полл, "
                                            "3) диагностика. Отправки/sim НЕ трогать."
                                            % (_host, _roll_pg[0], CU_QUOTA_M, _days, _top3),
                                            key="cu-quota-%s" % _host)
                                        log.info("CU-квота: пейдж человеку отправлен (%s), "
                                                 "следующий не раньше чем через %.0fч",
                                                 _host, CU_ALERT_PAGE_SEC / 3600.0)
                                    except Exception:
                                        log.exception("CU-квота: пейдж НЕ ушёл — маркер остаётся "
                                                      "только в журнале")
                        except Exception:
                            pass
                        # #110: индекс цепи — ЕДИНИЦЫ РАЗНЫЕ и разделены намеренно. blocks —
                        # покрытые блоки; full/light — из них прочитанные целиком и по логам;
                        # gaps — потерянные окна (каждое отнимает право сказать «tx не села»);
                        # fails — отказы узла; age/fresh — протухание. Индекса нет — строка
                        # тоже есть: молчание не должно быть неотличимо от «всё хорошо».
                        try:
                            _ci = _chain_index.get_index()
                            if _ci is None:
                                log.info("CHAIN-INDEX: НЕ ПОДНЯТ (исходы проб — только поштучные "
                                         "квитанции; позиции и tip транзита не измеряются)")
                            else:
                                _s = _ci.snapshot()
                                # квитанции=... — СПОСОБНОСТЬ УЗЛА, названная вслух. Без неё
                                # index_hits=0 в строке #109 ниже нечитаем: «узел не умеет
                                # eth_getBlockReceipts», «мы не спрашивали» и «наших tx в
                                # блоках не было» дают ОДИН И ТОТ ЖЕ ноль.
                                log.info("CHAIN-INDEX: блоков=%d (полных %d / лёгких %d) дыр=%d "
                                         "отказов=%d beyond=%d shed=%d | наших tx=%d транзитов=%d "
                                         "побед=%d трупов=%d | голова=%s потолок=%s возраст=%.1fс "
                                         "свежесть=%s тред=%s квитанции=%s | ЗАМЕР: %.2f зап/с "
                                         "~%d CU/сут",
                                         _s["blocks"], _s["full"], _s["light"], _s["gaps"],
                                         _s["fails"], _s["beyond_head"], _s["shed"], _s["ours"],
                                         _s["transit"], _s["winners"], _s["pairs"], _s["last"],
                                         _s["conf"], _s["age"], _s["fresh"], _ci.alive(),
                                         _s.get("rcpt_probe", "?"), _s["rps"], _s["cu_day"])
                                if not _ci.alive():
                                    log.error("CHAIN-INDEX: ТРЕД МЁРТВ — источник правды об "
                                              "исходах проб потерян, счётчики приёмки лгут")
                                if _s["blocks"] <= 0:
                                    log.error("CHAIN-INDEX: блоков=0 при живом треде — цепь НЕ "
                                              "читается (потолок=%s, отказов=%d, beyond=%d). "
                                              "Все счётчики позиций и исходов ничего не значат",
                                              _s["conf"], _s["fails"], _s["beyond_head"])
                        except Exception:
                            pass
                        # ЖИВОСТЬ PREDICT (после самодедлока 23-26.08: 0 армов при 528 пушах
                        # оракула в сутки, и МОЛЧАНИЕ НИКОГО НЕ БУДИЛО — «ошибок нет» читалось
                        # как «всё хорошо»). Позитивный контроль встроен в саму метрику: пуши
                        # оракула — наблюдаемое СОБЫТИЕ ПОЛЯ, арм — наша реакция на него.
                        # Пушей много, а армов ноль ⇒ верх сигнального тракта мёртв (дедлок,
                        # мёртвый WS, застрявший хендлер) — громко и в инбокс агента.
                        try:
                            from chain.predict import last_event_ts as _pred_ts
                            _pn = _PRED_WATCH["pushes"]
                            _age = time.time() - (_pred_ts() or _PRED_WATCH["born"])
                            if _pn >= PREDICT_SILENCE_PUSHES and _age >= PREDICT_SILENCE_SEC:
                                log.error("PREDICT МЁРТВ: пушей оракула %d за %.0f мин, армов/"
                                          "событий НОЛЬ (%.0f мин тишины). Верх сигнального "
                                          "тракта не работает: окна не армятся, залпов нет — "
                                          "проверь дедлок (py-spy dump) и живость Binance WS",
                                          _pn, (time.time() - _PRED_WATCH["born"]) / 60.0,
                                          _age / 60.0)
                                _PRED_WATCH["born"] = time.time()   # не звеним чаще раза в окно
                                _PRED_WATCH["pushes"] = 0
                            elif _age < PREDICT_SILENCE_SEC:
                                _PRED_WATCH["born"] = time.time()
                                _PRED_WATCH["pushes"] = 0
                        except Exception:
                            pass
                        # #109 ЗА СУТКИ: экономия поштучных квитанций. polls — СЕТЕВЫЕ поллы,
                        # polls_saved — партии, закрытые индексом целиком, index_hits — исходы,
                        # взятые из индекса. Это и есть приёмка пакета в деньгах запросов.
                        try:
                            from chain.fire_engine import collector_stats as _cst
                            _c9 = _cst()
                            if _c9["polls"] or _c9["polls_saved"] or _c9["tracked"]:
                                _tot = max(1, _c9["polls"] + _c9["polls_saved"])
                                log.info("#109 квитанции: polls=%d polls_saved=%d (%.0f%% партий "
                                         "закрыл индекс) index_hits=%d из resolved=%d | "
                                         "batch_fail=%d index_fail=%d cu=%.0f",
                                         _c9["polls"], _c9["polls_saved"],
                                         100.0 * _c9["polls_saved"] / _tot, _c9["index_hits"],
                                         _c9["resolved"], _c9["batch_fail"], _c9["index_fail"],
                                         _c9["cu"])
                        except Exception:
                            pass
                        _stg = stage_report()
                        if _stg:
                            log.info("STAGE p50/p95 мс (окно %.0fч): %s", STAGE_WINDOW_SEC / 3600.0, _stg)
                        _px_ok, _px_fail = px_preconf_take()
                        log.info("hot stats %ds: spawn=%d pspawn=%d poll=%d poll_lat=%d px_free=%d/%d px_pre=%d/%d | gate proceed=%d skip=%d none=%d poll_none=%d poll_fb=%d | funnel nocand=%d noprice=%d norp=%d noflip=%d floor=%d corpse=%d/unk%d claim=%d prep=%d",
                                 int(time.monotonic() - last_stats), stats["spawn"], stats["pspawn"], stats["poll"],
                                 stats.get("poll_lat", 0),
                                 stats.get("px_free", 0), stats.get("px_free", 0) + stats.get("px_rpc", 0),
                                 _px_ok, _px_ok + _px_fail,
                                 stats["proceed"], stats["skip"], stats["none"], stats["poll_none"], stats["poll_fb"],
                                 stats["f_nocand"], stats["f_noprice"], stats["f_norp"], stats["f_noflip"], stats["f_floor"],
                                 # Р1: corpse= срезано фильтром трупов, unk= окон, где индекс НЕ ЗНАЛ
                                 # (unk высокий при corpse=0 = фильтр мёртв, а не «трупов нет»),
                                 # claim= дублей подготовки, снятых заявкой multi-leg.
                                 stats.get("f_corpse", 0), stats.get("f_corpse_unk", 0),
                                 stats.get("f_claim", 0), stats["f_prep"])
                        # порог: >3 фолбэка/поллинг ≈ >8% чтений цены потеряли фору = РЕАЛЬНАЯ cap-деградация
                        # (исходный preconf-кап был 1457/окно=67%); микро-остаток спайков фида (≤2%, ~48/окно)
                        # НЕ будит — иначе ложно «мелькает» в норме (07-22, poll_fb калибр. на катастрофу, не на 1-2 оракула)
                        # 24.07 РЕ-КАЛИБРОВКА (инцидент «degraded 18x windows» = ложная тревога): poll_fb
                        # мерил РЕЗЕРВНЫЙ полл (пыльные cbXRP/cbDOGE/cbADA), а денежный путь берёт цену
                        # из flashblocks-WS за 0мс — замер 90 мин: 35/38 спавнов price=0ms, позиции 74/77
                        # из кэша, e2e p50=2мс. Публичный preconf ужался до ~0.2 pending-вызова/с (проба
                        # 24.07: 8 из 10 отказов даже в темпе 1/с) — это НОРМА среды, а не наша поломка.
                        # Будим ТОЛЬКО если пострадал денежный путь: цена реально пошла через RPC (px_rpc
                        # заметная доля) И этот RPC не даёт форы. Иначе — тишина (деградация косметическая).
                        _px_tot = stats.get("px_free", 0) + stats.get("px_rpc", 0)
                        _money_hit = _px_tot >= 4 and stats.get("px_rpc", 0) * 4 > _px_tot   # >25% цен через RPC
                        _px_bad = (_px_ok + _px_fail) >= 4 and _px_fail * 2 > (_px_ok + _px_fail)
                        fb_windows = fb_windows + 1 if (_money_hit and _px_bad) else 0
                        if fb_windows >= 3 and time.monotonic() - last_fb_alert > 1800:
                            last_fb_alert = time.monotonic()
                            try:
                                alerter.send_async("preconf degraded %dx windows: ДЕНЕЖНЫЙ путь потерял фору "
                                             "(px_free %d/%d, preconf ok %d/%d) — WS-декод цены сломался, "
                                             "не резерв. check flashblocks WS + preconf RPC."
                                             % (fb_windows, stats.get("px_free", 0), _px_tot,
                                                _px_ok, _px_ok + _px_fail))
                            except Exception:
                                pass
                        for _k in list(stats):
                            stats[_k] = 0
                        last_stats = time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        txt = brotli.decompress(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                        d = json.loads(txt)
                    except Exception:
                        continue
                    bn = block_number_of(d)
                    if bn is None:
                        continue
                    if bn != last_poll_block:                 # one preconf-price poll per block (money markets)
                        last_poll_block = bn
                        # #110: подача блока индексу. Тут же, где уже стоит per-block драйвер,
                        # и ровно тем же способом — БЕЗ работы в recv-петле: индекс только
                        # запоминает номер и будит свой единственный тред.
                        _chain_index.offer_block(bn)
                        _stage_frame(bn)                    # П.7: кадр суб-блока принят
                        feed_handled.clear()                  # Layer1: dedup set is per-block -> clear on block change
                        _bf = basefee_of(d)                   # 5a: index-0 кадр несёт base-хедер
                        if _bf is not None:
                            _basefee_probe(bn=bn, fb=_bf, rpc=rpc, log=log)
                        # #139: БЕЗ Thread-на-блок. Кладём (bn, конфиг) единственному стоячему
                        # воркеру и будим его (latest-wins). Опрос-читатель — на клонах с жёстким
                        # таймаутом; spawn_kwargs денежного пути держит БОЕВОЙ preconf_rpc/rpc.
                        offer_poll_block(bn, preconf_rpc=_poll_preconf_rpc, rpc_fallback=_poll_fb_rpc,
                                         feeds=feeds, meta=meta, poll_seen=poll_seen, stats=stats, log=log,
                                         feed_handled=feed_handled,
                                         spawn_kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                           feeds=feeds, meta=meta, shared=shared,
                                                           store=store, guard=guard, alerter=alerter,
                                                           log=log, last_price=last_price, stats=stats))
                        # Layer 2, measurement phase (C temporary — see STATE.md): fills _POSITION_CACHE +
                        # logs refresh-miss/cycle timing. Does NOT touch reads_fn — read_positions_cached
                        # is not wired into _process_transmit yet, so this has zero effect on hot-path behavior.
                        _spawn_cache_refresh(cache_rpc=_bg_rpc, shared=shared, bn=bn,   # штиль -> bg-RPC (CU-замер 25.07);
                                             combat_rpc=_c_rpc)                         # буст-окно -> ключ C (#117; без C = боевой)
                    idx = d.get("index"); txs = extract_txs(d); now = time.time()
                    for _raw, _to in txs:                        # SHADOW слоя 1: декод transmit из фида (log-only)
                        if _raw and FORWARD_SEL in _raw:
                            _dec = decode_forward_answer(_raw)
                            # 21.08 ЖИВОЕ КОЛЬЦО ЗЕРКАЛА: tip транзита — в кольцо ЗДЕСЬ,
                            # ~200мс от события, а не полным сканом индекса с лагом (класс
                            # эпизода съезжал быстрее колец: tip-match=0 в 4/4 утром).
                            # Дедуп на (agg, bn) — transmit виден в нескольких суб-кадрах,
                            # повторы исказили бы моду кольца. Сбоку от денег: O(десятков
                            # байт) + try в note_transit_tip; отказ = кольцо не пополнилось.
                            # Откат: MIRROR_LIVE_FEED=0 (индексная подача остаётся как была).
                            # Линза 21.08: гейт ПО КАРТЕ НАШИХ агрегаторов (agg_set) — как
                            # у shadow-ветки. Голый _dec кормил бы кольцо селектором-двойником
                            # из чужих calldata и форвардами не-наших фидов; docstring
                            # decode_forward_answer прямо требует этот фильтр от вызывающего.
                            if (_dec and MIRROR_LIVE_FEED and _dec[0] in agg_set
                                    and ("m", _dec[0], bn) not in feed_handled):
                                feed_handled.add(("m", _dec[0], bn))
                                # 28.08 (B): bn едет ВМЕСТЕ с сэмплом — кросс-дедуп с индексной
                                # подачей общий для обоих путей (свой feed_handled ловил только
                                # повтор внутри живой ветки; второй приход того же транзита из
                                # индекса удваивал вес класса в кольце).
                                _ntt_live(tip_from_raw(_raw, basefee=_fresh_basefee()),
                                          agg=_dec[0], bn=bn)
                            if _dec and _dec[0] in shadow_map:
                                _sym, _scale, _oracle = shadow_map[_dec[0]]
                                if cfg.price_from_feed and _feed_spawn_gate(_oracle, bn, feed_handled):
                                    # Layer1 LIVE: feed price triggers _process_transmit + dedups poll;
                                    # gate = один спавн на (oracle,bn) — transmit виден в неск. суб-кадрах
                                    _stage_feed(_sym, "detect")   # П.7: транзит опознан НАШИМ
                                    _PRED_WATCH["pushes"] += 1   # сторож живости predict
                                    log.info("DIAG feed-spawn mkt=%s oracle=%s bn=%s px_hint=%d",
                                             _sym, _oracle[:10], bn, _dec[1] * _scale)
                                    threading.Thread(target=_process_transmit,
                                                     args=(_dec[0], bn, "feed", time.time()),
                                                     kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                                 feeds=feeds, meta=meta, shared=shared,
                                                                 store=store, guard=guard, alerter=alerter,
                                                                 log=log, last_price=last_price, stats=stats,
                                                                 px_hint=_dec[1] * _scale, hint_oracle=_oracle),
                                                     daemon=True).start()
                                threading.Thread(target=_shadow_feed_check, daemon=True,
                                                 kwargs=dict(sym=_sym, agg=_dec[0], answer=_dec[1],
                                                             scale=_scale, oracle=_oracle,
                                                             preconf_rpc=preconf_rpc,
                                                             price_fn=read_preconf_price,
                                                             t0=time.perf_counter(), log=log)).start()
                    for agg in agg_set:                          # BARE match: ANY mention of the aggregator
                        if now - last_spawn.get(agg, 0.0) < HOT_THROTTLE_SEC:
                            continue                             # per-agg throttle: bound preconf reads/spawns
                        if any(raw_ and (agg[2:] in raw_) for raw_, _ in txs):
                            last_spawn[agg] = now
                            stats["spawn"] += 1
                            if len(agg) != 42:               # watch-паттерн (не адрес): спавны редкие по построению
                                log.info("DIAG pattern-spawn %s bn=%s", agg, bn)
                            threading.Thread(target=_process_transmit, args=(agg, bn, idx, now),
                                             kwargs=dict(rpc=rpc, preconf_rpc=preconf_rpc, cfg=cfg,
                                                         feeds=feeds, meta=meta, shared=shared, store=store,
                                                         guard=guard, alerter=alerter, log=log,
                                                         last_price=last_price, stats=stats),
                                             daemon=True).start()
        except Exception as e:
            _fb_backoff_n += 1
            _wait = min(3 * (2 ** (_fb_backoff_n - 1)), 60)      # 3,6,12,24,48,60,60… — не долбим кап на частоту
            log.warning("hot path ws error (%s) — reconnecting in %.0fs (backoff #%d)", type(e).__name__, _wait, _fb_backoff_n)
            _now = time.monotonic()
            recon_ts = [t for t in recon_ts if _now - t <= 180.0] + [_now]   # окно 180с
            # 20.08: гейт пейджа перенесён с ЧИСЛА попыток на ИХ БЕЗУСПЕШНОСТЬ. Серия быстрых
            # реконнектов, каждый из которых УДАЛСЯ, человеку не адресована: 20.08 апстрим
            # flashblocks сам замолкал на 30-60с (независимый ws-клиент с той же машины: 46
            # сообщений за 60с вместо ~250), бот переподключался, hot-path работал. Настоящая
            # беда — когда вернуться НЕ ВЫХОДИТ, и это видно по эскалации бэкоффа: #4 = ждём
            # 24с, то есть три подряд неудачных захода. Журнал пишет каждую попытку, как и был.
            if (_fb_backoff_n >= HOT_WS_PAGE_BACKOFF_N and len(recon_ts) >= 3
                    and _now - last_recon_alert > 600.0):
                last_recon_alert = _now
                alerter.send_async("\U0001F6A8 hot-path НЕ ВОССТАНАВЛИВАЕТСЯ: %d обрывов за 3 мин, "
                                   "бэкофф #%d (ждём %.0fс) — поток суб-блоков потерян"
                                   % (len(recon_ts), _fb_backoff_n, _wait), key="hot-wsserr")
            await asyncio.sleep(_wait)
