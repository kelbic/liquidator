"""Движок огня: постоянный пул тредов, держащий каденс проб по пред-подписанным лестницам (#32).

ПОЧЕМУ ДВИЖОК, А НЕ ГЕЙТ «ЗАЛП В ПОЛЁТЕ». Нынешняя схема порождает залп на КАЖДЫЙ тик окна и
режет ноги на пачки тредов. Наблюдаемая плотность 13 проб/блок рождается именно из ПЕРЕКРЫТИЯ
залпов: предыдущий ещё летит, когда стартует следующий. Поэтому «не начинать залп, пока
предыдущий в полёте» плотность не поднял бы, а срезал — гейт лечит симптом (взрыв тредов),
убивая механизм. Правильная форма — заменить порождение на ПОСТОЯННЫЙ пул: N тредов живут всё
время, а тик окна лишь продлевает им право стрелять (keepalive).

ЧТО ЭТО ДАЁТ ПО ЗАМЕРУ (07.08). Рассылка пред-подписанной пробы в тёплую сессию секвенсера
держит тред 32.5мс (p50) против 60мс у синхронной ноги Alchemy. 16 тредов x 1/0.0325 =
~493 отправки/с = ~986 проб в блок Base против нынешних 267 (насыщение окна победы — 1480).

ПОТОЛОК ЗАДАЁТ НЕ РАССЫЛКА, А ПОДПИСЬ — И ЭТО ДВА РАЗНЫХ ПОТОЛКА:
  ВСПЛЕСК ограничен только рассылкой: лестницы — это СКЛАД, набитый в спокойное время
    (106 ног x 40 проб = 4240 готовых байтов). Пока склад не пуст, подписи в контуре нет вовсе;
  ПОДДЕРЖАНИЕ ограничено CPU подписи: 4мс на пробу, то есть 200 проб/с = 0.8 ядра ПОСТОЯННО,
    сколько лестницу ни углубляй. Pre-sign не уменьшает работу подписи — он уносит её с
    горячего пути (латентность отправки 64мс -> 32.5мс), но суммарный CPU остаётся.
    !! ЭТИ ЧИСЛА СНЯТЫ НА NativeECCBackend (чистый Python) — 13.08 выяснилось, что в боевом
    venv Шарлотты НЕТ coincurve, и подпись шла чистым Python все эти недели. С coincurve
    (пакет COIN, requirements.txt) замер на боевой форме пробы: 0.557мс вместо 3.67мс,
    то есть 200 проб/с = 0.11 ядра, а потолок ПОДДЕРЖАНИЯ ~1800 подп/с на ядро.
    СЛЕДСТВИЕ ДЛЯ КОНСТРУКЦИИ: «подпись — второй потолок» перестаёт связывать вовсе.
    Связывающий предел теперь ОДИН и он внешний: ставка приёма секвенсера ~423 отправки/с
    (дрель #69). ENGINE_MAX_RATE=200 ниже него и НЕ трогается этим пакетом — решение о
    новом значении принимает владелец, а не следствие из комментария.
Отсюда конструкция: склад набивается ФОНОМ (пополнение — отдельный тред, уступающий огню), а
окно победы стреляет со склада. Если склад кончился — движок просто идёт медленнее, а не встаёт.

ГАРДЫ (у нас есть боевые инциденты ровно на эти грабли):
  * keepalive с коротким TTL: перестал тикать источник (окно закрыто, петля встала, процесс
    занят) -> огонь гаснет сам за TTL. Отказ в сторону МОЛЧАНИЯ, а не бесконечной стрельбы;
  * потолок темпа (token bucket): безлимитный цикл рассылки — это ровно тот самопальный
    RPC-шторм, который мы себе уже устраивали (109x429);
  * КОЛЛЕКТОР КВИТАНЦИЙ — ОДИН ТРЕД на весь движок. Тред-на-квитанцию при сотнях отправок в
    секунду это тот же взрыв тредов, который здесь и чинится, только с другого конца.

ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ: SPEC_FIRE_ENGINE=0 -> ни одного треда не создаётся, путь залпа
работает бит-в-бит как прежде.
"""
import os
import threading
import time

ENGINE_ON = os.environ.get("SPEC_FIRE_ENGINE", "0").lower() not in ("0", "false", "no")
ENGINE_THREADS = int(os.environ.get("SPEC_FIRE_ENGINE_THREADS", "8"))
# Потолок темпа. Начинаем ЗАМЕТНО ниже модельных 493/с: обкатка идёт на 5 стволах, и цена
# ошибки в темпе — шторм 429 по боевому ключу, а не недобор плотности.
ENGINE_MAX_RATE = float(os.environ.get("SPEC_FIRE_ENGINE_MAX_RATE", "200"))
ENGINE_BURST = float(os.environ.get("SPEC_FIRE_ENGINE_BURST", "40"))
# TTL keepalive: ~блок Base (2с) с запасом. Больше — огонь переживает закрытие окна;
# меньше — обычная задержка тика гасит огонь в середине горячей фазы.
ENGINE_TTL_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_TTL_SEC", "1.5"))
ENGINE_IDLE_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_IDLE_SEC", "0.05"))
# Ш26: СТОЯЧЕЕ ПРИСУТСТВИЕ («долго и тонко» вместо вспышки).
#
# Замер 11.08, блоки 49820459..49820472: стена жила 5 блоков / 10 секунд (1155 проб, 231 на
# блок при нужных ПОБЕДИТЕЛЮ восьми), затем 8 блоков / 16 секунд пустоты — и ровно там прошли
# 30 ликвидаций. В блоках с деньгами наших tx НОЛЬ. Причина ровно в TTL выше: право огня гаснет
# через 1.5с после последнего тика, то есть стена привязана к СОБЫТИЮ и умирает вместе с ним.
#
# Поэтому после прекращения тиков держим ТОНКИЙ поток ещё HOLD_SEC. Арифметика: 4 пробы/с на
# 180с = 720 проб (~$0.15), покрытие 90 блоков вместо пяти, темп в 60 раз ниже лимита приёма —
# те самые 901 отказ «over rate limit» мы устраивали себе сами, вываливая боезапас за 10 секунд.
#
# Отдельный БУРСТ для фазы удержания — замечание kelbic про непрерывность: общий бурст 40
# позволяет 16 тредам выплеснуть накопленное разом, и «тонкий поток» пошёл бы рывками с
# паузами. Пауза, попавшая на транзит, снова обнуляет присутствие. Держим бурст 2.
ENGINE_HOLD_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_HOLD_SEC", "180"))
# Ш24: темп поднят 4 -> 16. Флот разделён на ЧЕТЫРЕ тира tip, поэтому в классе решающего
# блока стоит примерно четверть ног. При 16/с блок Base (2с) несёт 32 пробы, из них ~8 в
# классе — ровно столько было у победителей в призовом окне (8, 8, 3, 1). При прежних 4/с
# попавший тир давал бы лишь 2 пробы на блок.
# Цена: 180с × 16/с × 8 окон = 23 040 проб/сутки ≈ $4.6 при дневном капе $100.
# Потолок приёма (замер: отказы начались при ~115/с) не задевается с семикратным запасом.
ENGINE_HOLD_RATE = float(os.environ.get("SPEC_FIRE_ENGINE_HOLD_RATE", "16"))
ENGINE_HOLD_BURST = float(os.environ.get("SPEC_FIRE_ENGINE_HOLD_BURST", "2"))
# Э1б (14.08): ДИСТ-ГЕЙТ HOLD ВО ВРЕМЕНИ. Э1a срезал hold 16->1 ($142->$9/день), но #118
# показал: burst опаздывает на >=1 суб-блок ПО ПОСТРОЕНИЮ (триггер = публикация суб-блока),
# в окно транзита встаёт только СТОЯЧАЯ плотность — а деньги Ш26 приходили через ~16с ПОСЛЕ
# смерти окна, ровно в утончённом хвосте. Возврат: первые NEAR_SEC после угасания горячей
# фазы hold держит боевой темп NEAR_RATE, остаток хвоста — тонкий ENGINE_HOLD_RATE.
# Сигнал близости — ТОЛЬКО собственный таймстамп движка (_until): дистанция во времени от
# последнего тика окна. Никаких чужих фактов (урок 8ea97ca: гейт по чужому подтверждённому
# факту в игре одного блока ложен по построению). ЦЕНА (честная арифметика 14.08, а не «доли
# доллара» — та оценка ОТОЗВАНА): hold-фаза жила ~713с за 1.2ч ⇒ ≤80 хвостов/сутки;
# дефолты КОНСЕРВАТИВНЫ по Правилу денег (kelbic дважды отклонял дорогие ручки):
# NEAR_SEC=15 × 8/с × ≤80 = ≤9.6k проб/сутки ≈ ВЕРХНЯЯ ОЦЕНКА ~$7/день сверх $9 базы;
# ~4 пробы в tip-классе на блок (диапазон победителей 3-8). Подъём до 16/с×20с (≈$19/день,
# ~8 в классе) — одна строка .env ПОСЛЕ замера фактической цены счётчиком sent_hold_near.
# NEAR_SEC=15 покрывает замер Ш26 («деньги через ~16с» — впритык, это осознанный компромисс).
# Откат SPEC_FIRE_ENGINE_HOLD_NEAR_RATE=0 — Э1a бит-в-бит.
ENGINE_HOLD_NEAR_RATE = float(os.environ.get("SPEC_FIRE_ENGINE_HOLD_NEAR_RATE", "8"))
ENGINE_HOLD_NEAR_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_HOLD_NEAR_SEC", "15"))
# Ш29: ЗАЛП ПО ФАКТУ ТРАНЗИТА.
#
# ВСКРЫТИЕ блока 49834406 (11.08). Призовое окно [436..455] заняли ДВАДЦАТЬ транзакций ОДНОГО
# адреса 0x7de3002da4, все на tip 1 000 000 — том же классе, что и наши пробы. Наши двадцать
# стояли на 407..426, то есть класс мы угадали верно и окно ОБХВАТИЛИ, но встали на 29 позиций
# раньше: транзит лёг на 435, ровно между их серией и нашей.
#
# Наши пробы шли РОВНЫМ потоком (~4мс между соседними). Конкурент уместил двадцать транзакций
# МЕЖДУ двумя нашими соседними — то есть выбросил залп за миллисекунды, синхронно с транзитом.
# Разница не в темпе и не в цене, а в МОМЕНТЕ: он бьёт точечно по факту прихода транзита.
#
# Жетоны залпа держим ОТДЕЛЬНО от ведра. Ведро КЛАМПИТ остаток к burst-капу
# (`min(burst, ...)`), и в фазе удержания кап равен 2 — добавленные в ведро жетоны залпа были
# бы срезаны в тот же миг. Отдельный счётчик клампу не подвержен и тратится первым.
ENGINE_TRANSIT_BURST = float(os.environ.get("SPEC_FIRE_ENGINE_TRANSIT_BURST", "24"))
ENGINE_TRANSIT_BURST_MAX = float(os.environ.get("SPEC_FIRE_ENGINE_TRANSIT_BURST_MAX", "40"))
# Пополнение склада: ниже какой глубины лестницу дотачивать и как часто смотреть.
#
# #112 (14.08): ПОРОГ ПОДНЯТ 12 -> 39, полоса гистерезиса [39,40] СУЖЕНА почти в ноль.
# Раньше 12 при ёмкости склада 40 давал пилу с амплитудой ~28 проб: сток удержания
# (ENGINE_HOLD_RATE=16/с × POLL=0.5с = 8 проб/проход) роняет очередь на 8 за проход, и между
# пересечением порога и серийным доливом склад проваливался до ~8 проб — мгновенная плотность
# ноги в трогах падала на 80% (замерено дрелью R2 пакета #112). Пила была ТЕРПИМА, пока подпись
# стоила 4.27мс/проба (native): узкая полоса означала бы доточку почти каждый проход, а каждая
# доточка жгла CPU. С coincurve (COIN) подпись стоит 0.473мс — в 9 раз дешевле, и доточка
# «40−38 = 2 подписи» стоит ~1мс. Значит полосу можно сузить до самого верха склада, держа
# мгновенную плотность у потолка.
#
# ЧИСЛО ВЫВЕДЕНО, НЕ ВЗЯТО НА ВЕРУ. Три предела зажимают порог:
#   • снизу (пол глубины, дрель R2): в троге склад не должен падать ниже 0.75×DEPTH=30 при
#     стоке удержания 8/проход; первый под-пороговый сэмпл = DEPTH−сток = 40−8 = 32 ≥ 30 при
#     любом REFILL_AT∈[33,40);
#   • сверху-мягко (пропускная горячего цикла, дрель A1): при синхронизации флота проход
#     доливает N×(DEPTH−REFILL_AT+1) подписей. С прежним бюджетом 250 порог 39 держал это в
#     одном проходе; после пересчёта бюджета под p99 (125, см. ENGINE_SIGN_BUDGET) полный спрос
#     синхронизации 110×2=220 в один проход НЕ влезает и делится на два — граница ослаблена с
#     «влезть в проход» до «делиться безопасно» (остаток недобит лишь по глубине). Порог 39
#     оставлен: его пинают ЖЁСТКИЕ пределы ниже (пол глубины ≥33 и сходимость R1 ≤39);
#   • сверху-жёстко (сходимость, дрель R1): предикат требует depth ≥ REFILL_AT ПОСЛЕ стока
#     одной пробы (нога садится на DEPTH−1=39), поэтому REFILL_AT ≤ 39 — иначе полный склад
#     после первого выстрела считается недобитым и флот «не сходится» вечно.
# Пересечение [39,39] => 39. Откат к пиле: SPEC_FIRE_ENGINE_REFILL_AT=12.
ENGINE_REFILL_AT = int(os.environ.get("SPEC_FIRE_ENGINE_REFILL_AT", "39"))
ENGINE_REFILL_POLL_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_REFILL_POLL_SEC", "0.5"))
# #112 (14.08): ПОТОЛОК ПОДПИСИ ЗА ОДИН ПРОХОД депт-долива — связывающий предел горячего цикла
# на одном ядре. Серийный for по ногам ниже порога не имеет верхнего лимита по построению:
# при синхронизации флота (все ноги пересекли порог разом) один проход подписал бы N×(DEPTH−d),
# на 110 ногах это сотни подписей за проход, и такой проход задушил бы огонь. Бюджет = сколько
# подписей проход ДЕПТ-ДОЛИВА (не аудита цены — тот пересобирает протухшее НЕМЕДЛЕННО) сжигает.
# ЧИСЛО ОТ ХВОСТА ПОДПИСИ, А НЕ ОТ МЕДИАНЫ (#112 REVIEW: MAJOR-2, 14.08). Горячий цикл терпит
# ~150мс/проход. Ранняя редакция брала 250 от p50=0.473мс (150/0.473=317, 250 «с запасом») —
# но потолок обязан держаться на ХВОСТЕ: под GIL-конкуренцией восьми воркеров огня проход идёт
# не по медиане, а по p99. Замер coincurve 21.0.0 (5000 подписей боевой формы): p50=0.507мс,
# p90=0.71мс, p99=1.10мс, max=6.1мс. При p99 старые 250 подписей = 275мс — почти 2× лимита,
# инвариант «худший проход < 150мс» на хвосте НЕ держался. Берём 125: 125×p99=137мс < 150мс,
# и 125 > замеренного УСТОЙЧИВОГО худшего прохода (дрель A1: 114) — потолок не режет штатный
# долив, но связывает на хвосте. Полная синхронизация флота (спрос 220 = N×(DEPTH−REFILL_AT+1))
# теперь делится на ДВА прохода (безопасно: остаток недобит лишь по глубине, проба для огня
# есть, по цене не протух). Остаточный перебор учёта по дельте — не более старой глубины
# ПОСЛЕДНЕЙ ноги (см. named_limit «учёт по дельте недооценивает») и только в ХОЛОДНОЙ набивке,
# не в горячем окне. Авто-рекалибровки под цену подписи нет: sign_backend_probe меряет p50 и
# НИКЕМ не потребляется — потолок крутится ВРУЧНУЮ. Откат (снять потолок): =0.
ENGINE_SIGN_BUDGET = int(os.environ.get("SPEC_FIRE_ENGINE_SIGN_BUDGET", "125"))
COLLECT_POLL_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_POLL_SEC", "1.0"))
# Каденс флотского прогрева патрона. Величина меняется только когда нога стреляет (расход
# книжится по квитанции) или когда её доливают — чаще одного мультиколла в минуту незачем.
ENGINE_WARM_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_WARM_SEC", "60"))
# Каденс аудита ЦЕНЫ лестниц (#63): базу читаем один раз на весь флот, подпись жжём только
# у тех ног, чья лестница действительно протухла по цене. ХОЛОСТОЙ каденс (флот не в окне) —
# редкий, чтобы не капать get_block в боевой ключ круглосуточно (уроки квоты 07-09.08).
ENGINE_AUDIT_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_AUDIT_SEC", "120"))
# #112 (14.08): БЫСТРЫЙ каденс ценового аудита, действует ТОЛЬКО когда движок в окне (armed
# или в фазе удержания). Триггер глубины СЛЕП к ценовому протуханию (протухшая лестница не
# расходуется -> глубина не падает -> долив не зовётся), поэтому при скачке базы В ОКНЕ полный,
# но невключаемый склад флота лечился бы лишь холостым 120с-аудитом — 18с немоты в 2-4с каскаде
# (замерено дрелью R1). В окне base читается один раз на весь флот КАЖДЫЕ ~1с (дёшево: один
# get_block, пересборку жжём только у реально протухших ног), поэтому каскад ловится за проход.
# Вне окна каденс остаётся 120с (квота). Первый проход движка аудирует ВСЕГДА (см. _audit_ts
# в __init__): свежеподнятый движок обязан свериться с полем до первого выстрела.
# Откат к единому редкому каденсу: SPEC_FIRE_ENGINE_PRICE_AUDIT_SEC=120.
ENGINE_PRICE_AUDIT_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_PRICE_AUDIT_SEC", "1.0"))
COLLECT_TIMEOUT_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_TIMEOUT_SEC", "180"))
COLLECT_BATCH = int(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_BATCH", "64"))
# Возраст, младше которого квитанцию спрашивать бессмысленно (блок Base ~2с).
COLLECT_MIN_AGE_SEC = float(os.environ.get("SPEC_FIRE_ENGINE_COLLECT_MIN_AGE_SEC", "2.0"))
# Цена eth_getTransactionReceipt в CU — для учёта расхода коллектора (он ходит батчем
# мимо обычного пути, поэтому считает себя сам).
CU_PER_RECEIPT = float(os.environ.get("SPEC_FIRE_ENGINE_CU_PER_RECEIPT", "15"))


_COLLECTORS = []                    # живые коллекторы процесса (в бою он один)


def collector_stats():
    """Сводные счётчики коллекторов процесса — для суточной приёмки #109.

    Отдаёт polls (СЕТЕВЫЕ поллы), polls_saved (партии, закрытые индексом целиком) и
    index_hits (исходы, взятые из индекса). Без этой тройки «экономия запросов» остаётся
    утверждением, а не замером.
    """
    out = {"polls": 0, "polls_saved": 0, "index_hits": 0, "tracked": 0, "resolved": 0,
           "timeouts": 0, "batch_fail": 0, "index_fail": 0, "cu": 0.0}
    for c in list(_COLLECTORS):
        for k in out:
            try:
                out[k] += (c.stats.get(k, 0) or 0)
            except Exception:
                pass
    return out


class ReceiptCollector:
    """ОДИН тред на все квитанции движка: партиями спрашивает исходы и раздаёт их обработчику.

    Почему не тред-на-квитанцию (как в прежнем залпе): при 200 отправках/с это 200 новых тредов
    в секунду на четырёхъядерной машине — тот самый эффект наблюдателя, на который у нас есть
    задокументированные инциденты. Партия из одного запроса стоит столько же, сколько один
    полл, а покрывает сотни проб.

    Швы: receipts_fn(hashes) -> {hash: receipt|None}; on_result(meta, receipt); on_timeout(meta);
    index_fn(hashes) -> {hash: receipt} — ИНДЕКС ЦЕПИ (#110), спрашивается ПЕРВЫМ.

    Почему индекс первым (#109). Поштучная квитанция — самый дорогой способ узнать то, что уже
    лежит в блоке: 63k подзапросов за 2 часа, 889 отказов за сутки и — главное — ЛОЖЬ под 429
    («NO-RECEIPT» у замайненных проб). Индекс читает блок ОДИН раз на всех, поэтому у него
    исход бесплатен, а у сети спрашивается только остаток. index_fn=None (ручка отката) делает
    путь бит-в-бит прежним.
    """

    def __init__(self, receipts_fn, on_result, on_timeout=None, log=None,
                 poll_sec=None, timeout_sec=None, batch=None, now=None, sleep=None,
                 min_age_sec=None, index_fn=None, max_pending=None):
        self._receipts_fn = receipts_fn
        self._index_fn = index_fn
        self._on_result = on_result
        self._on_timeout = on_timeout
        self._log = log
        self._poll = COLLECT_POLL_SEC if poll_sec is None else float(poll_sec)
        self._timeout = COLLECT_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
        self._batch = COLLECT_BATCH if batch is None else int(batch)
        self._min_age = COLLECT_MIN_AGE_SEC if min_age_sec is None else float(min_age_sec)
        self._max_pending = None if max_pending is None else int(max_pending)
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._pending = {}          # hash -> (meta, deadline)
        self._stop = threading.Event()
        self._thr = None
        self.stats = {"tracked": 0, "resolved": 0, "timeouts": 0, "polls": 0, "cu": 0.0,
                      "index_hits": 0, "polls_saved": 0}
        _COLLECTORS.append(self)    # приёмка «за сутки» (#109) читается из hot-stats, а не
        #                             только из close-строки окна, которого может и не быть

    def track(self, h, meta):
        """-> True если хэш под надзором (включая дубль), False при переполнении очереди
        (П.3 22.08: backpressure — вызывающий обязан сам не потерять денежный инвариант,
        напр. фолбэком в легаси-тред). max_pending=None = прежнее поведение бит-в-бит."""
        with self._lock:
            if h in self._pending:
                return True
            if self._max_pending is not None and len(self._pending) >= self._max_pending:
                self.stats["overflow"] = self.stats.get("overflow", 0) + 1
                return False
            self._pending[h] = (meta, self._now() + self._timeout, self._now())
            self.stats["tracked"] += 1
            return True

    def pending(self):
        with self._lock:
            return len(self._pending)

    def start(self):
        if self._thr is not None:
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._loop, daemon=True, name="fire-collect")
        self._thr.start()

    def stop(self):
        self._stop.set()

    def tick(self):
        """Один проход. Вынесен из петли, чтобы тест мог прогнать его без тредов и без времени."""
        with self._lock:
            if not self._pending:
                return 0
            # #66: не опрашиваем хэши МОЛОЖЕ блока — квитанции у них быть не может по
            # построению (Base ~2с), а стоят они те же CU. Без этого 200 проб/с давали
            # ~3000 CU/с в боевой ключ ровно в окне победы: коллектор сам себе устраивал
            # 429 в тот момент, когда квитанции побед и нужны.
            _now = self._now()
            hashes = [h for h, e in self._pending.items()
                      if _now - (e[2] if len(e) > 2 else 0.0) >= self._min_age][:self._batch]
            if not hashes:
                return 0
        got = {}
        ask = hashes
        if self._index_fn is not None:
            # #110: сперва ИНДЕКС. Он читает блок раз на всех, поэтому исход отсюда стоит ноль
            # запросов и ноль CU. Ключей в ответе меньше запрошенного — это «остальных не
            # знаю», и остаток честно уходит в сеть: индекс не имеет права никого хоронить.
            try:
                _idx = self._index_fn(hashes) or {}
            except Exception as e:                    # индекс лёг -> работаем как до него
                _idx = {}
                self.stats["index_fail"] = self.stats.get("index_fail", 0) + 1
                if self._log:
                    self._log("FIRE collect index fail: %s %s (спрашиваем сеть как прежде)"
                              % (type(e).__name__, str(e)[:120]))
            if _idx:
                got.update({h: r for h, r in _idx.items() if r is not None})
                self.stats["index_hits"] = self.stats.get("index_hits", 0) + len(got)
                ask = [h for h in hashes if h not in got]
        if not ask:
            # Партия закрыта индексом целиком: сети не касаемся вовсе. Ровно это и есть #109 —
            # 63k подзапросов за два часа превращаются в один общий разбор блока.
            self.stats["polls_saved"] = self.stats.get("polls_saved", 0) + 1
        self.stats["cu"] = self.stats.get("cu", 0.0) + len(ask) * CU_PER_RECEIPT
        try:
            if ask:
                got.update(self._receipts_fn(ask) or {})
        except Exception as e:
            # РАННИЙ ВЫХОД ЗДЕСЬ БЫЛ РЕГРЕССИЕЙ (#101, поймано ревью 12.08). Снятие брони #93
            # живёт в цикле НИЖЕ (_on_timeout -> volley_gas_settle), поэтому `return 0` на
            # отказе означал: оба узла молчат -> брони не снимаются НИКОГДА -> гейт
            # CASCADE_TIER_GAS_USD закрыт до рестарта или полуночи UTC. Прежняя редакция
            # самоисцелялась за 180с ровно потому, что batch_receipts возвращал {} и цикл
            # всё равно отрабатывал таймауты.
            # Незнание обязано закрывать гард, а НЕ запирать наш собственный огонь: отказ узла
            # — это «не знаю про квитанции», а не «держи бронь вечно». Дедлайн считает время,
            # а не ответы узла, поэтому цикл ниже исполняется в ЛЮБОМ случае с пустым `got`.
            self.stats["batch_fail"] = self.stats.get("batch_fail", 0) + 1
            if self._log:
                # ПРИЧИНА, а не только тип: отказ узла квитанций приезжает
                # ReceiptsUnavailable(host: причина), и по одному имени класса не понять,
                # какой контур лёг (в тексте только хост — ключ живёт в пути URL).
                self._log("FIRE collect batch fail: %s %s (таймауты обрабатываются)"
                          % (type(e).__name__, str(e)[:140]))
        else:
            if ask:
                self.stats["polls"] += 1   # СЕТЕВОЙ полл, а не проход тика: партию, закрытую
                #                            индексом, считать поллом = завысить свой же расход
        done = 0
        now = self._now()
        for h in hashes:
            r = got.get(h)
            if r is None:
                with self._lock:
                    ent = self._pending.get(h)
                # Квитанции нет. Это НЕ приговор: на Base блок 2с, а дроп мы объявляем только
                # по истечении таймаута — иначе инвалидировали бы nonce живой ноги на ровном месте.
                if ent is not None and now >= ent[1]:
                    with self._lock:
                        self._pending.pop(h, None)
                    self.stats["timeouts"] += 1
                    if self._on_timeout:
                        try:
                            self._on_timeout(ent[0], h)
                        except Exception as e:
                            # L1 р-8: МОЛЧАЛИВЫЙ except НА ДЕНЕЖНОМ ПУТИ — ДЕФЕКТ САМ ПО СЕБЕ.
                            # Обработчик таймаута выносит вердикт победы NO-RECEIPT и защищает
                            # nonce отставшей ноги; голый `pass` делал невидимым и его отказ, и
                            # его причину — ровно поэтому «учёт перед деньгами» прожил здесь
                            # три раунда. Глотать бросок мы обязаны (один битый хэш не имеет
                            # права уронить тред коллектора), а молчать о нём — нет.
                            self.stats["timeout_handler_fail"] = self.stats.get(
                                "timeout_handler_fail", 0) + 1
                            if self._log:
                                try:
                                    self._log("FIRE collect timeout handler fail tx=%s: %s: %s"
                                              % (h, type(e).__name__, str(e)[:140]))
                                except Exception:
                                    pass
                continue
            with self._lock:
                ent = self._pending.pop(h, None)
            if ent is None:
                continue
            self.stats["resolved"] += 1
            done += 1
            try:
                self._on_result(ent[0], h, r)
            except Exception as e:
                # ХВОСТ L1 р-8, ВТОРОЙ БРАТ. По образцу пути таймаута (c2fdc92): отказ
                # обработчика ИСХОДА — того самого, что выносит вердикт победы по квитанции и
                # снимает бронь #93, — печатался БЕЗ ХЭША и без текста, одним именем типа.
                # Разобрать по такой строке нечего: какая проба легла и почему — неизвестно, а
                # хэш уже снят с `_pending`, то есть другого следа не будет ни от кого.
                # Глотать бросок мы обязаны (одна битая квитанция не имеет права уронить тред
                # коллектора), молчать о нём — нет.
                self.stats["result_handler_fail"] = self.stats.get(
                    "result_handler_fail", 0) + 1
                if self._log:
                    try:
                        self._log("FIRE collect handler fail tx=%s: %s: %s"
                                  % (h, type(e).__name__, str(e)[:140]))
                    except Exception:
                        pass
        return done

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._sleep(self._poll)


class FireEngine:
    """Постоянный пул тредов огня. Тик окна лишь ПРОДЛЕВАЕТ право стрелять (keepalive)."""

    def __init__(self, legs_fn, send_fn, collector=None, refill_fn=None, depth_fn=None,
                 log=None, threads=None, rate=None, ttl=None, now=None, sleep=None,
                 fleet_warm_fn=None, audit_fn=None):
        """legs_fn() -> [addr,...] (ноги со склада); send_fn(addr) -> hash|None (рассылка пробы);
        refill_fn(addr) -> None (фоновое пополнение склада); depth_fn(addr) -> int (остаток);
        fleet_warm_fn([addr,...]) -> None (освежить патрон ВСЕГО флота одним вызовом);
        audit_fn([addr,...]) -> None (проверить ЦЕНУ лестниц; см. #63)."""
        self._legs_fn = legs_fn
        self._send_fn = send_fn
        self._refill_fn = refill_fn
        self._depth_fn = depth_fn
        self._fleet_warm_fn = fleet_warm_fn
        self._warm_ts = 0.0
        self._audit_fn = audit_fn
        # #112: аудит цены обязан сработать на ПЕРВОМ проходе — свежеподнятый движок сверяет
        # склад с полем до первого выстрела (в бою time.monotonic() уже даёт это; в сим-часах,
        # стартующих с 0, без отрицательной затравки первый проход ждал бы полный каденс).
        # self._now ещё не присвоено — берём тот же источник времени напрямую.
        self._audit_ts = ((now or time.monotonic)()
                          - max(ENGINE_AUDIT_SEC, ENGINE_PRICE_AUDIT_SEC) - 1.0)
        self._collector = collector
        self._log = log
        self._nthreads = ENGINE_THREADS if threads is None else int(threads)
        self._rate = ENGINE_MAX_RATE if rate is None else float(rate)
        self._ttl = ENGINE_TTL_SEC if ttl is None else float(ttl)
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._until = 0.0
        self._hold_until = 0.0   # Ш26: фаза удержания
        self._burst_tokens = 0.0  # Ш29: жетоны залпа по транзиту (вне ведра, без клампа)
        self._target = None        # (feed, mid, borrower, debt_usd) — чей план сейчас на доске
        self._rr = 0
        self._tokens = ENGINE_BURST
        self._tok_ts = self._now()
        self._stop = threading.Event()
        self._threads = []
        self.stats = {"sent": 0, "skipped_rate": 0, "skipped_dry": 0, "armed_sec": 0.0}

    # ---- управление ----
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for i in range(max(1, self._nthreads)):
            t = threading.Thread(target=self._worker, daemon=True, name="fire-%d" % (i + 1))
            t.start()
            self._threads.append(t)
        if self._refill_fn is not None:
            t = threading.Thread(target=self._refill_loop, daemon=True, name="fire-refill")
            t.start()
            self._threads.append(t)
        if self._collector is not None:
            self._collector.start()
        if self._log:
            self._log("FIRE движок поднят: %d тредов, потолок %.0f отправок/с, TTL %.1fс" % (
                self._nthreads, self._rate, self._ttl))
            # #136: темп режут ДВЕ независимые ручки — потолок движка (наш бюджет) и пейсер
            # эндпоинта в execute (квота приёма секвенсера, замер 11.08: ~115/с устойчиво).
            # Связывающий — меньший; подъём SPEC_FIRE_ENGINE_MAX_RATE без VOLLEY_PACER_RATE
            # молча уйдёт в pacer_dry. Строка ниже делает связывающий потолок видимым на старте.
            try:
                from chain.execute import _VOLLEY_PACER_ON, _PACER_RATE
                if _VOLLEY_PACER_ON:
                    self._log("FIRE потолки: движок %.0f/с, пейсер эндпоинта %.0f/с — "
                              "связывающий %.0f/с" % (self._rate, _PACER_RATE,
                                                      min(self._rate, _PACER_RATE)))
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        with self._lock:
            self._until = 0.0
            self._hold_until = 0.0     # Ш26: остановка гасит и удержание, иначе флот стрелял
            #                            бы ещё 3 минуты после команды «стоп»
        if self._collector is not None:
            self._collector.stop()

    def keepalive(self, target=None, ttl=None):
        """Тик окна: продлить право стрелять. Перестали звать — ГОРЯЧАЯ фаза гаснет за TTL,
        но присутствие держится ещё ENGINE_HOLD_SEC тонким потоком (Ш26)."""
        with self._lock:
            n = self._now()
            self._until = n + (self._ttl if ttl is None else float(ttl))
            self._hold_until = n + ENGINE_HOLD_SEC
            if target is not None:
                self._target = target

    def disarm(self):
        with self._lock:
            self._until = 0.0
            self._hold_until = 0.0
            self._target = None

    def armed(self) -> bool:
        """Право стрелять: горячая фаза ИЛИ фаза удержания.

        Ш26: раньше здесь стоял только `_until`, и стена умирала через 1.5с после последнего
        тика. Деньги приходили через 16 секунд после этого."""
        with self._lock:
            n = self._now()
            return n < self._until or n < self._hold_until

    def in_hot(self) -> bool:
        """Горячая фаза (полный темп) против фазы удержания (тонкий ровный поток)."""
        with self._lock:
            return self._now() < self._until

    def in_hold_near(self) -> bool:
        """Э1б: ближний хвост удержания — боевой темп в первые NEAR_SEC после угасания
        горячей фазы (деньги Ш26 приходили через ~16с после смерти окна)."""
        with self._lock:
            n = self._now()
            return (not n < self._until) and ENGINE_HOLD_NEAR_RATE > 0 and n < self._until + ENGINE_HOLD_NEAR_SEC

    # ---- внутреннее ----
    def transit_burst(self, n=None):
        """Ш29: одноразовый ЗАЛП по подтверждённому транзиту.

        Зовётся из ветки pending-handoff, где жертва флипнута в ФОРМИРУЕМОМ блоке — то есть
        момент транзита ИЗВЕСТЕН, а не предсказан. Выдаёт пачку жетонов сверх ведра, чтобы
        шестнадцать уже существующих тредов выбросили пробы за миллисекунды, а не размазали
        их по 4мс, как делает ровный поток.

        НОВЫХ ТРЕДОВ НЕ СОЗДАЁТ (урок инцидента 15:41: треды на горячем пути пробили cgroup-кап
        и уронили hot_loop). Кап держит экспозицию: даже при потоке событий больше
        ENGINE_TRANSIT_BURST_MAX жетонов в воздухе не окажется."""
        add = float(ENGINE_TRANSIT_BURST if n is None else n)
        if add <= 0:
            return 0.0
        with self._lock:
            self._burst_tokens = min(ENGINE_TRANSIT_BURST_MAX, self._burst_tokens + add)
            self.stats["transit_bursts"] = self.stats.get("transit_bursts", 0) + 1
            return self._burst_tokens

    def _take_token(self) -> bool:
        """Token bucket: общий на все треды потолок отправок/с.

        Ш26: у фазы удержания СВОИ темп и бурст. Бурст здесь важнее темпа — с общим бурстом 40
        шестнадцать тредов выплёскивают накопленное разом, и «тонкий поток» идёт рывками с
        паузами между ними. Пауза, попавшая на транзит, обнуляет присутствие ровно так же, как
        обнуляла его вспышка на 10 секунд."""
        with self._lock:
            # Ш29: жетоны залпа тратятся ПЕРВЫМИ и мимо ведра — ведро кламповало бы их к
            # burst-капу фазы (в удержании он равен 2), и залп исчез бы в тот же миг.
            if self._burst_tokens >= 1.0:
                self._burst_tokens -= 1.0
                self.stats["sent_burst"] = self.stats.get("sent_burst", 0) + 1
                # БЁРСТ-В-ЩЕЛЬ (20.08): помечаем ЭТОТ тред — ниже стоит ВТОРОЕ ведро (пейсер
                # эндпоинта), и к транзиту оно пусто по построению. Без пометки залп движка
                # упирался в капельницу 110/с и размазывался на весь суб-блок (замер: жетоны
                # тратились все, а первый наш tx приходил на медиану +12 от транзита).
                # Тред тот же, что позовёт send_fn (один воркер = одна проба). Отказ пометки
                # = обычная отправка: незнание НЕ открывает резерв.
                try:
                    from chain.execute import pacer_mark_urgent
                    pacer_mark_urgent()
                except Exception:                        # noqa: BLE001
                    pass
                return True
            n = self._now()
            hot = n < self._until
            if hot:
                rate = self._rate
            elif ENGINE_HOLD_NEAR_RATE > 0 and n < self._until + ENGINE_HOLD_NEAR_SEC:
                # Э1б: ближний хвост (дистанция во времени от последнего тика окна) — боевой
                # темп; деньги Ш26 живут здесь (~16с после смерти окна). Бурст остаётся
                # HOLD_BURST=2: поток обязан быть РОВНЫМ, вспышка = пауза = дыра на транзите.
                rate = ENGINE_HOLD_NEAR_RATE
            else:
                rate = ENGINE_HOLD_RATE
            burst = ENGINE_BURST if hot else ENGINE_HOLD_BURST
            self._tokens = min(burst, self._tokens + (n - self._tok_ts) * rate)
            self._tok_ts = n
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    def _next_leg(self):
        legs = self._legs_fn() or []
        if not legs:
            return None
        with self._lock:
            i = self._rr % len(legs)
            self._rr += 1
        return legs[i]

    def _worker(self):
        while not self._stop.is_set():
            if not self.armed():
                self._sleep(ENGINE_IDLE_SEC)
                continue
            if not self._take_token():
                self.stats["skipped_rate"] += 1
                self._sleep(ENGINE_IDLE_SEC)
                continue
            addr = self._next_leg()
            if addr is None:
                self.stats["skipped_dry"] += 1
                self._sleep(ENGINE_IDLE_SEC)
                continue
            with self._lock:
                tgt = self._target
                hot_at_send = self._now() < self._until
            try:
                h = self._send_fn(addr)
            except Exception as e:
                if self._log:
                    self._log("FIRE отправка ноги %s: %s" % (addr[:10], type(e).__name__))
                self._sleep(ENGINE_IDLE_SEC)
                continue
            if h is None:
                # склад пуст либо гейт закрыт — не крутим пустой цикл на полной скорости
                self.stats["skipped_dry"] += 1
                self._sleep(ENGINE_IDLE_SEC)
                continue
            self.stats["sent"] += 1
            # ПРИБОР КАДЕНСА (26.08): межпробные дельты ОТПРАВОК ДВИЖКА. Строку на пробу
            # движок не пишет намеренно (журнал стал бы второй нагрузкой), поэтому ритм
            # предочереди из логов не восстановим — а именно он решает, стоим ли мы в
            # СУБ-БЛОКЕ или только в окне. Кольцо капировано, считается p50/p90 в close-строке.
            # Сбоку от денег: только запись числа под тем же локом, ноль сети.
            try:
                _n = self._now()
                with self._lock:
                    _prev = self.stats.get("_last_sent_ts")
                    self.stats["_last_sent_ts"] = _n
                    if _prev:
                        _r = self.stats.setdefault("send_gap_ring", [])
                        _r.append(_n - _prev)
                        del _r[:-400]
            except Exception:                                # noqa: BLE001
                pass
            if not hot_at_send:
                self.stats["sent_hold"] = self.stats.get("sent_hold", 0) + 1
                if self.in_hold_near():                       # Э1б: приёмка ближнего хвоста
                    self.stats["sent_hold_near"] = self.stats.get("sent_hold_near", 0) + 1
            if self._collector is not None:
                self._collector.track(h, (addr, tgt))

    def _refill_loop(self):
        """Склад набивается ФОНОМ и УСТУПАЕТ огню: подпись это CPU (4мс/проба на native,
        0.56мс с coincurve — пакет COIN), и в горячей фазе она нужна не нам. Пополняем только
        те ноги, у которых остаток просел.

        МЕХАНИЗМ «уступать огню» НЕ снимается вместе с подешевевшей подписью: он держит не
        только CPU, но и порядок (пополнение не лезет в лестницу, из которой стреляют)."""
        while not self._stop.is_set():
            try:
                # #112: ПОТОЛОК ПОДПИСИ ЗА ПРОХОД. Долив серийный и без верхнего лимита по
                # построению; при синхронизации флота проход подписал бы сотни проб и задушил
                # бы огонь на одном ядре. Считаем подписи по ДЕЛЬТЕ глубины (refill_fn ничего
                # не возвращает, а движок не знает DEPTH) и, исчерпав бюджет, откладываем
                # остаток ног на следующий проход — они недобиты лишь по ГЛУБИНЕ (проба для
                # огня ещё есть), не протухли по цене, поэтому пауза безопасна. Ценовой аудит
                # (ниже) бюджету НЕ подчинён: протухшую лестницу надо пересобрать немедленно.
                spent = 0
                for addr in (self._legs_fn() or []):
                    if self._stop.is_set():
                        break
                    if ENGINE_SIGN_BUDGET > 0 and spent >= ENGINE_SIGN_BUDGET:
                        break
                    d = self._depth_fn(addr) if self._depth_fn else 0
                    if d < ENGINE_REFILL_AT:
                        self._refill_fn(addr)
                        if ENGINE_SIGN_BUDGET > 0 and self._depth_fn is not None:
                            spent += max(0, self._depth_fn(addr) - d)
                # Патрон ноги протухает САМ ПО СЕБЕ, а полный склад пополнения не зовёт:
                # между fat-окнами (недели) кэш стареет, и на старте окна fail-closed гейт
                # заглушил бы ВЕСЬ флот разом. Освежаем фоном и ОДНИМ вызовом на весь флот —
                # пер-ноговое чтение стоило бы ~7 запросов/с круглосуточно в боевой ключ.
            except Exception as e:
                if self._log:
                    self._log("FIRE пополнение склада: %s" % type(e).__name__)
            try:
                # #63: аудит ЦЕНЫ лестниц — свой каденс и свой try. Триггер «глубина < порога»
                # не срабатывает, когда лестница протухла по цене: протухание САМО останавливает
                # огонь, очередь не расходуется, глубина не падает. Без этого прохода нога с
                # вздутой (или невалидной) лестницей нема до рестарта.
                # #112: В ОКНЕ (armed/удержание) каденс БЫСТРЫЙ. ЧТО он лечит за проход —
                # ВКЛЮЧАЕМОСТЬ (немоту), а НЕ полный 3x-запас. Скачок базы НЕ выносит ноги из
                # игры сразу: множитель maxFee (MAXFEE_RESERVE_MULT=20) против дрейфа <=12.5%/блок
                # держит старую лестницу включаемой ~25 блоков (~50с), поэтому огонь не молкнет —
                # это тот «за проход», что был нужен против 18с немоты 120с-каденса (дрель R1).
                # ВОССТАНОВЛЕНИЕ 3x-запаса — отдельная, НАМЕРЕННО размазанная величина: аудит-бёрст
                # капнут SPEC_FIRE_AUDIT_MAX_RESIGN (spec_fire_live @216) ради защиты ядра (R7),
                # поэтому полный репрайс флота идёт ~ceil(N_stale/кап) проходов (110/5≈22 ≈ 24с в
                # окне) — это НАЗВАННЫЙ ПРЕДЕЛ (named_limits), а не то, что доказывает R1: R1 гоняет
                # НЕкапнутый путь (полный репрайс за проход, 2.581с) и капнутый агрегат НЕ покрывает.
                # Деградация в ЗАПАСЕ (2x вместо 3x ≈ 6 блоков форы включаемости), НЕ в огне:
                # включаемость держится 110/110 весь репрайс (замер lens2r2/demo_cap_cadence).
                # Вне окна — редкий 120с (квота get_block в боевой ключ). _audit_ts затравлен
                # так, что ПЕРВЫЙ проход аудирует всегда (см. __init__).
                _in_window = self._now() < max(self._until, self._hold_until)
                # Ш24b v2: движок — ЕДИНСТВЕННЫЙ, кто знает про окно, и он же зовёт аудит.
                # Пока окно живо, СОСТАВ зеркального портфеля заморожен: смена тарифа означает
                # пересигнацию 110 лестниц (28-32с), а приз живёт в transit+1 (~2с) — успеть
                # нельзя, заплатить можно. Статические причины пересборки (полоса maxFee, байт
                # пробы) не затронуты: они не зависят от состава.
                try:
                    from chain.execute import note_hot_phase as _nhp
                    _nhp(_in_window, now=self._now(), hold_sec=max(2.0, ENGINE_PRICE_AUDIT_SEC * 3))
                except Exception:
                    pass
                _audit_cadence = ENGINE_PRICE_AUDIT_SEC if _in_window else ENGINE_AUDIT_SEC
                if self._audit_fn is not None and (
                        self._now() - self._audit_ts) >= _audit_cadence:
                    self._audit_ts = self._now()
                    self._audit_fn(self._legs_fn() or [])
            except Exception as e:
                if self._log:
                    self._log("FIRE аудит лестниц: %s" % type(e).__name__)
            try:
                # СВОЙ try: прогрев патрона не должен отменяться осечкой пополнения склада
                # (одна нога с 429 оставляла бы весь флот без свежего патрона до следующего
                # круга — а через MAX_AGE это немота всего флота)
                if self._fleet_warm_fn is not None and (
                        self._now() - self._warm_ts) >= ENGINE_WARM_SEC:
                    self._warm_ts = self._now()
                    self._fleet_warm_fn(self._legs_fn() or [])
            except Exception as e:
                if self._log:
                    self._log("FIRE прогрев патрона: %s" % type(e).__name__)
            self._sleep(ENGINE_REFILL_POLL_SEC)
