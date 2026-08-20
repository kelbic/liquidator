"""Сторож квоты CU: журнал Шарлотт → медиана мгновенных окон за час → ИНБОКС АГЕНТА.

НАПРАВЛЕНИЕ (доктрина владельца 03.08, notify.py): квоту разбирает АГЕНТ — триаж
«правда/прогрев», перестрайп потребителей, ротация между СВОИМИ ключами. Человеку — только
вердикт «нужен НОВЫЙ ключ/оплата» (критерий HIL-1), через эскалацию инбокса. Прямой TG-пейдж
бота (hotpath, CU_ALERT_TG) погашен 20.08 после трёх заходов ложных пейджей kelbic за сутки;
путь в боте сохранён как env-резерв (CU_ALERT_TG=1 возвращает).

ЧИСЛИТЕЛЬ — ВТОРАЯ, НЕЗАВИСИМАЯ МЕХАНИКА ТРИАЖА (не «синхронизировать» с ботовой).
Бот меряет скользящую сумму часа: она насыщается бут-прогревом (два захода фиксов 20.08,
ex-boot-вид). Здесь — ДВА РАЗНЫХ ЧИСЛА ДЛЯ ДВУХ РАЗНЫХ ВОПРОСОВ (разбор 20.08 ~19:5xZ):

  (1) ВСПЛЕСК (диагностика, НЕ приговор) — среднее мгновенных 2-мин окон БЕЗ бут-окон за
      прогон. Бут виден по полю самой строки (скольз.<16мин). verdicts().
  (2) КВОТА (приговор) — ИНТЕГРАЛ: сумма реально потраченных CU / покрытое время по хвосту
      >= CU_WATCH_TRAIL_H часов, накопленному МЕЖДУ прогонами в cu-quota.history.jsonl.
      ВКЛЮЧАЯ бут-окна. quota_verdicts().

ПОЧЕМУ ТАК, а не «среднее часа» как было до 20.08 ~19:5xZ (оба предела замерены, не изречены):
  * ГОРИЗОНТ. Квота — месячная, час — не месяц. Замер по 6ч живого журнала: почасовой темп
    #94bf гулял 19.1…32.3M/мес, и 5 из 23 скользящих 70-мин окон (22%) перешагивали порог 26
    при истинном шестичасовом темпе 22.2M из 30M. Порог, который на норме звонит в 22%
    случаев, — это не порог (канон emptiness-alarm-needs-measured-rate: метрику прогнать по
    своей истории). Драйвер пилы известен и законен: prearm._read_target масштабируется
    числом армленных целей, то есть метрика СОБЫТИЙНАЯ.
  * БУТ. Выкидывая бут-окна из ПРИГОВОРА, сторож выкидывал реально списанные провайдером CU:
    за те же 6ч бут = 54% всего расхода ключа A (126230 из 234931 CU) при ~16 рестартах/сут.
    Ключ A по честной сумме шёл 28.4M/мес против 17.5M «ex-boot» — то есть ближе всех к
    квоте был именно тот ключ, по которому сторож молчал ПО ПОСТРОЕНИЮ. Ex-boot честен для
    вопроса «устойчив ли перерасход» и ЛОЖЕН для вопроса «выжжем ли квоту».
  * Оценщик (среднее, не медиана) при этом ПРОВЕРЕН И ОПРАВДАН, не тронут: за 6ч ex-boot
    среднее 22.1M против честной суммы 22.2M — расхождение 0.5%.

ЧЕГО ЭТОТ СТОРОЖ НЕ ВИДИТ ПО ПОСТРОЕНИЮ — ДВА ПРЕДЕЛА (названы, а не умолчаны):

(1) СМЕНА РЕЖИМА ВНУТРИ ХВОСТА. Интеграл усредняет по времени и по построению не отличает
    «ключ жжёт много» от «ключ жёг много ДО перестрайпа потребителей». Разбор 20.08 21:07:
    #3133 показал 36.8M за хвост 6.7ч, но раскидка квот увела volley_bal_warm/_fleet/_audit
    с A на B в 20:19, то есть 5.5 из 6.7 часов интеграла описывали КОНФИГУРАЦИЮ, КОТОРОЙ
    УЖЕ НЕТ (после неё A шёл 10.1M). Лечится не кодом, а порядком разбора, поэтому едет в
    ТЕКСТ тревоги: прежде чем судить темп — сверить атрибуцию ПО ВРЕМЕНИ (кто на каком ключе
    сидел в начале хвоста и кто в конце), а не только её срез на последнем окне в поле «Топ».
    Автодетектор режима сознательно НЕ строится: он резал бы историю, проверенную честной, и
    глушил бы собственный канал тревог — ровно тот отказ, что описан в каноне 20.08.

(2) КВОТА НА АККАУНТ, А СРАВНЕНИЕ ПО КЛЮЧУ. Если два ключа делят аккаунт, связывающий предел —
их СУММА, и per-key сторож не зазвонит никогда. Развилка по ключу C поднята владельцу
20.08 (HIL cu-quota:key-c-account); URL аккаунт не кодирует — агентом не решается.

ГДЕ ЗАПУСКАЕТСЯ: агентская машина (вотчеры — не на боевой, урок 30.07). Чтение журнала —
ssh read-only; ключи/секреты не читаются, идентификаторы хостов уже обезличены (#crc).

Запуск:  python3 -m analysis.cu_quota_watch          # прогон (крон, раз в час)
Ручки:   CU_WATCH_PROJ_M (порог, M CU/мес; дефолт 26)
         CU_WATCH_MIN_WINDOWS (минимум окон для диагностики всплеска; дефолт 20)
         CU_WATCH_FAIL_STREAK (слепых прогонов подряд до тревоги о самом стороже; дефолт 3)
         CU_WATCH_TRAIL_H (хвост, по которому судится квота; дефолт 24)
         CU_WATCH_MIN_TRAIL_H (сколько накопить, чтобы вообще судить; дефолт 6)
         CU_WATCH_KEEP_H (сколько держать историю; дефолт 168)
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WATCH_DIR = os.path.expanduser("~/.fleet-watch")
STATE_PATH = os.path.join(WATCH_DIR, "cu-quota.state")
HISTORY_PATH = os.path.join(WATCH_DIR, "cu-quota.history.jsonl")
SOURCE = "cu-quota"
HOST = os.environ.get("CU_WATCH_SSH", "root@185.173.146.134")
QUOTA_M = 30.0
SEC_MONTH = 30 * 24 * 3600.0
WINDOW_SEC_FALLBACK = 120.0   # каденс CU-строки бота, если длительность не выводится из строки

# Строка бота (chain/hotpath, каждые ~120с на ключ):
#   INFO CU base-mainnet.g.alchemy.com#3133: 866 CU/окно = 7 CU/s (проекция 18.7M/мес,
#   скольз.26мин 50.8M/мес) | eth_call@prearm.py:_read_target×38 ...
_LINE = re.compile(r"INFO CU ([a-z0-9.\-]+#[0-9a-f]{4}): (\d+) CU/окно .*?проекция ([0-9.]+)M/мес,\s*"
                   r"скольз\.(\d+)мин[^|]*\|\s*(.*)$")
# Время берём ISO-штампом ИЗ САМОГО СООБЩЕНИЯ (в нём есть год, в префиксе journald — нет).
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_PID = re.compile(r"python\[(\d+)\]")
BOOT_SPAN_MIN = 16      # скольз-спан < 16 мин => окно первых минут после рестарта (бут)


def _line_epoch(line):
    """Секунды эпохи из ISO-штампа строки; None — если штампа нет (парс не обязан падать)."""
    m = _ISO.search(line)
    if not m:
        return None
    try:
        return time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 1, -1))
    except (ValueError, OverflowError):
        return None


def _window_sec(cu, proj_m):
    """Длительность окна — ИЗ САМОЙ СТРОКИ: она называет и сколько CU, и с каким темпом.
    cu / (proj CU/s) = секунды. Захардкоженный каденс был бы лишним допущением: уедет
    CU_ROLL/каденс бота — интеграл поедет молча. Фолбэк только когда проекция нулевая."""
    rate = float(proj_m) * 1e6 / SEC_MONTH
    if rate <= 0 or cu <= 0:
        return WINDOW_SEC_FALLBACK
    return cu / rate


def parse_cu_windows(journal_text, *, since_epoch=None):
    """host -> {"projs": [проекции НЕ-бут окон], "boot": сколько бут-окон выкинуто,
    "top": последняя атрибуция, "cu": ВСЕ потраченные CU (вкл. бут), "cu_boot": из них
    бутовые, "sec": покрытое этими окнами время, "n_all": окон всего, "last": макс. эпоха}.
    Только квотуемые хосты (alchemy). Бут-окно опознаётся по полю самой строки: скольз-спан
    растёт с нуля после рестарта, <16 мин = прогрев.

    "projs"/"boot" — для ДИАГНОСТИКИ ВСПЛЕСКА (ex-boot, как было). "cu"/"sec" — для ПРИГОВОРА
    ПО КВОТЕ: провайдер списывает и бут тоже, поэтому в интеграл он входит.

    since_epoch: окна со штампом <= него пропускаются. Прогоны берут 70 минут при каденсе
    в час, то есть ~10 минут КАЖДОГО прогона уже посчитаны предыдущим; без отсечки хвост
    двоил бы эти окна. Строки без разбираемого штампа отсечке не подлежат (не молчим о них:
    парс не обязан падать от смены формата journald)."""
    out = {}
    for line in journal_text.splitlines():
        m = _LINE.search(line)
        if not m:
            continue
        host, cu, proj = m.group(1), int(m.group(2)), float(m.group(3))
        span_min, top = int(m.group(4)), m.group(5).strip()
        if "alchemy" not in host:
            continue
        ep = _line_epoch(line)
        if since_epoch is not None and ep is not None and ep <= since_epoch:
            continue
        d = out.setdefault(host, {"projs": [], "boot": 0, "top": "", "cu": 0, "cu_boot": 0,
                                  "sec": 0.0, "n_all": 0, "last": None})
        d["cu"] += cu
        d["sec"] += _window_sec(cu, proj)
        d["n_all"] += 1
        if ep is not None:
            d["last"] = ep if d["last"] is None else max(d["last"], ep)
        if span_min < BOOT_SPAN_MIN:
            d["boot"] += 1
            d["cu_boot"] += cu
        else:
            d["projs"].append(proj)
        d["top"] = top or d["top"]
    return out


def count_restarts(journal_text):
    """Сколько РАЗНЫХ процессов бота видно в куске журнала. Рестарт — первоклассный член
    расхода (каждый бут гонит presign_ladder/_pending_nonce/volley_bal_warm), поэтому едет
    в текст тревоги: без него «жжёт много» не отличить от «сегодня много деплоили»."""
    return len({m.group(1) for m in (_PID.search(x) for x in journal_text.splitlines()) if m})


def verdicts(per_host, *, threshold, min_windows):
    """[(host, mean, n_windows, top)] — только хосты, где СРЕДНЕЕ не-бут окон >= порога.
    Квота жжётся суммой => среднее; бут-окна уже выкинуты парсом по полю скольз-спана."""
    out = []
    for host, d in sorted(per_host.items()):
        n = len(d["projs"])
        if n < min_windows:
            continue
        avg = statistics.fmean(d["projs"])
        if avg >= threshold:
            out.append((host, avg, n, d["top"]))
    return out


def load_history(path=None, *, keep_sec=None, now=None):
    """Список записей истории, свежее keep_sec. Битые строки пропускаются молча — история
    вспомогательна, её порча не должна ослеплять сторож (слепота — отдельная ветка)."""
    path = path or HISTORY_PATH
    now = float(now if now is not None else time.time())
    keep_sec = float(keep_sec if keep_sec is not None else 168 * 3600)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and now - float(r.get("ts", 0)) <= keep_sec:
                    out.append(r)
    except OSError:
        return []
    return out


def append_history(rec, path=None, *, keep_sec=None, now=None):
    """Дописать прогон и подрезать хвост. Пишем через чтение-в-память (канон
    file-write-truncates: open(w) усекает ДО чтения)."""
    path = path or HISTORY_PATH
    recs = load_history(path, keep_sec=keep_sec, now=now)
    recs.append(rec)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
    except OSError:
        pass
    return recs


def history_cutoff(recs):
    """Верхняя граница уже посчитанного (максимум по "until"). Отсекает перекрытие прогонов."""
    tops = [float(r["until"]) for r in recs if r.get("until") is not None]
    return max(tops) if tops else None


def quota_verdicts(recs, *, threshold, trail_sec, min_trail_sec, now=None):
    """ПРИГОВОР ПО КВОТЕ — интеграл, а не мгновенный темп.

    [(host, rate_M, covered_sec, cu_total)] по хвосту trail_sec: сумма ВСЕХ потраченных CU
    (вкл. бут) делить на покрытое время. Пока покрыто меньше min_trail_sec — НЕ СУДИМ ВОВСЕ
    (возвращаем пусто): огрызок в час и был причиной ложных тревог. Это ровно тот случай,
    когда незнание обязано закрывать гард, а не разрешать выстрел.

    НАЗВАННЫЙ ПРЕДЕЛ (чтобы не читался как месячный факт): знаменатель — время, КОГДА БОТ
    РАБОТАЛ, а не стенные часы. Простой между рестартами в него не входит, поэтому на
    рестарто-обильных сутках оценка выше «стенной» на скважность (20.08: 30.0M против 28.4M,
    ~8% при 5 рестартах за 6ч). Выбрано намеренно и в эту сторону: (а) стенной знаменатель
    считал бы burn-free временем и ПРОПУЩЕННЫЕ прогоны сторожа, то есть занижал бы тем
    сильнее, чем хуже работает сам сторож — недопустимая обратная связь; (б) у гарда квоты
    ошибка в сторону раннего звонка дешевле. Рестарты едут в текст тревоги, чтобы разбирающий
    видел скважность сразу, а не переоткрывал её.
    """
    now = float(now if now is not None else time.time())
    agg = {}
    for r in recs:
        if now - float(r.get("ts", 0)) > trail_sec:
            continue
        for host, d in (r.get("hosts") or {}).items():
            a = agg.setdefault(host, {"cu": 0.0, "sec": 0.0})
            a["cu"] += float(d.get("cu", 0))
            a["sec"] += float(d.get("sec", 0))
    out = []
    for host, a in sorted(agg.items()):
        if a["sec"] < min_trail_sec:
            continue
        rate = a["cu"] / a["sec"] * SEC_MONTH / 1e6
        if rate >= threshold:
            out.append((host, rate, a["sec"], a["cu"]))
    return out


def trail_summary(recs, *, trail_sec, now=None):
    """host -> (rate_M, covered_sec) по хвосту, БЕЗ порога — для журнальной строки штиля.
    Тихий прогон обязан печатать то же число, которым судит, иначе «тихо» недоказуемо."""
    now = float(now if now is not None else time.time())
    agg = {}
    for r in recs:
        if now - float(r.get("ts", 0)) > trail_sec:
            continue
        for host, d in (r.get("hosts") or {}).items():
            a = agg.setdefault(host, [0.0, 0.0])
            a[0] += float(d.get("cu", 0))
            a[1] += float(d.get("sec", 0))
    return {h: ((cu / sec * SEC_MONTH / 1e6) if sec > 0 else 0.0, sec) for h, (cu, sec) in agg.items()}


def _fetch_journal():
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", HOST,
         "journalctl -u liquidator-bot --since '-70 minutes' --no-pager | grep 'INFO CU ' || true"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("ssh/journalctl rc=%d: %s" % (r.returncode, (r.stderr or "")[:120]))
    return r.stdout


def _fleet_notify():
    sys.path.insert(0, WATCH_DIR)
    try:
        from notify import notify  # noqa: PLC0415
        return notify
    except Exception:  # noqa: BLE001
        return None


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st, f)
    except Exception:  # noqa: BLE001
        pass


def run(*, fetch=None, notify_fn=None, out=print, now=None, history_path=None):
    threshold = float(os.environ.get("CU_WATCH_PROJ_M", "26"))
    min_windows = int(os.environ.get("CU_WATCH_MIN_WINDOWS", "20"))
    fail_streak_n = int(os.environ.get("CU_WATCH_FAIL_STREAK", "3"))
    trail_sec = float(os.environ.get("CU_WATCH_TRAIL_H", "24")) * 3600.0
    min_trail_sec = float(os.environ.get("CU_WATCH_MIN_TRAIL_H", "6")) * 3600.0
    keep_sec = float(os.environ.get("CU_WATCH_KEEP_H", "168")) * 3600.0
    history_path = history_path or HISTORY_PATH
    notify_fn = notify_fn or _fleet_notify()
    st = _load_state()
    now_s = float(now if now is not None else time.time())

    try:
        text = (fetch or _fetch_journal)()
        # Отсечка перекрытия: считаем только окна новее уже учтённых прошлым прогоном.
        prev = load_history(history_path, keep_sec=keep_sec, now=now_s)
        per_host = parse_cu_windows(text, since_epoch=history_cutoff(prev))
        total_windows = sum(len(d["projs"]) for d in per_host.values())
        if total_windows == 0:
            raise RuntimeError("0 CU-окон распарсено (бот молчит? формат уехал?)")
    except Exception as e:  # noqa: BLE001
        # Слепой прогон. Одиночный ssh-фейл — транзиент (раз в час их будет достаточно,
        # чтобы заспамить); тревога о САМОМ стороже — только серия подряд (Ш23а).
        st["fails"] = int(st.get("fails", 0)) + 1
        _save_state(st)
        out("cu-quota: слепой прогон %d/%d: %s" % (st["fails"], fail_streak_n, str(e)[:160]))
        if st["fails"] >= fail_streak_n and notify_fn is not None:
            notify_fn("💸 сторож квоты CU СЛЕП %d прогонов подряд (%s) — журнал Шарлотт "
                      "не читается или формат CU-строк уехал. Мёртвый сторож хуже "
                      "отсутствующего: проверить ssh/формат." % (st["fails"], str(e)[:120]),
                      source=SOURCE, hil=False, key="cu-quota:blind", dedup_sec=21600)
        return 1

    st["fails"] = 0
    st["last_ok"] = now_s
    _save_state(st)

    restarts = count_restarts(text)
    recs = append_history({
        "ts": now_s,
        "until": max([d["last"] for d in per_host.values() if d["last"] is not None] or [None])
                 if any(d["last"] is not None for d in per_host.values()) else None,
        "restarts": restarts,
        "hosts": {h: {"cu": d["cu"], "sec": round(d["sec"], 1), "cu_boot": d["cu_boot"],
                      "n": d["n_all"], "boot": d["boot"]} for h, d in per_host.items()},
    }, history_path, keep_sec=keep_sec, now=now_s)

    burst = {h: (statistics.fmean(d["projs"]), len(d["projs"]))
             for h, d in per_host.items() if len(d["projs"]) >= min_windows}
    trail = trail_summary(recs, trail_sec=trail_sec, now=now_s)
    alarms = quota_verdicts(recs, threshold=threshold, trail_sec=trail_sec,
                            min_trail_sec=min_trail_sec, now=now_s)

    for host, rate, sec, cu_total in alarms:
        days = QUOTA_M / rate * 30.0 if rate > 0 else 0.0
        b_txt = ("всплеск часа (ex-boot) %.0fM/%d окон" % burst[host]) if host in burst else "всплеск не судим"
        # ПОРОГ — В ТЕКСТЕ ТРЕВОГИ (разбор 20.08). Две тревоги 17:01 «жжёт 16M/20M» при
        # боевом пороге 26 были позитивным контролем с заниженным CU_WATCH_PROJ_M, но по
        # СВОЕМУ ТЕКСТУ отличить их от настоящих было нельзя: разбирающий доказывал
        # происхождение археологией по времени коммита сторожа. Число, по которому вынесен
        # приговор, обязано ехать вместе с приговором — иначе тревога не самодостаточна.
        # 20.08 (2): вместе с приговором едет и ГОРИЗОНТ, и рестарты — «31M за час» и
        # «31M за сутки» требуют разных действий, а по прежнему тексту были неразличимы.
        msg = ("💸 КВОТА RPC (ИНТЕГРАЛ хвоста %.1fч, вкл. бут-окна, порог %.0fM): %s жжёт "
               "%.0fM CU/мес при квоте %.0fM (~%.0f сут). Рестартов в окне: %d. %s\n"
               "Топ: %s\n"
               "РАЗБОР АГЕНТУ: это НЕ пиковый час — темп держится на хвосте. Перестрайп "
               "потребителей / ротация своих ключей. Человека звать ТОЛЬКО если вердикт "
               "«нужен новый ключ/оплата» (HIL-1). Отправки/sim не трогать.\n"
               "ПРЕДЕЛ 1: интеграл СЛЕП К ПЕРЕСТРАЙПУ — темп мог быть набран составом "
               "потребителей, которого уже нет. До вердикта сверить атрибуцию ПО ВРЕМЕНИ "
               "(начало хвоста против конца), а не только «Топ» последнего окна.\n"
               "ПРЕДЕЛ 2: сравнение ПО КЛЮЧУ, а квота Alchemy — НА АККАУНТ; если ключи делят "
               "аккаунт, связывающий предел — их сумма, и здесь он не виден."
               % (sec / 3600.0, threshold, host, rate, QUOTA_M, days, restarts, b_txt,
                  (per_host.get(host, {}).get("top", "") or "")[:160]))
        if notify_fn is None:
            out("cu-quota: КАНАЛ НЕДОСТУПЕН (~/.fleet-watch/notify.py), текст ниже")
            out(msg)
        else:
            verdict = notify_fn(msg, source=SOURCE, hil=False,
                                key="cu-quota:%s" % host, dedup_sec=21600)
            out("cu-quota: %s интеграл %.1fM за %.1fч (n=%d рестартов) -> %s"
                % (host, rate, sec / 3600.0, restarts, verdict))
    if not alarms:
        # Тихий прогон печатает ТО ЖЕ число, которым судит (интеграл), и рядом всплеск —
        # иначе «тихо» неотличимо от «нечем судить», а это разные состояния (канон
        # «мёртвый сторож хуже отсутствующего»: тишина требует положительного основания).
        parts = []
        for h in sorted(per_host):
            rate, sec = trail.get(h, (0.0, 0.0))
            mark = "" if sec >= min_trail_sec else "*копим"
            bm = ("всплеск %.1fM" % burst[h][0]) if h in burst else "всплеск —"
            parts.append("%s=%.1fM/хв%.1fч%s(%s,бут%d)"
                         % (h, rate, sec / 3600.0, mark, bm, per_host[h]["boot"]))
        out("cu-quota: тихо (порог %.0fM, хвост %.0fч, мин %.0fч, рестартов %d): %s"
            % (threshold, trail_sec / 3600.0, min_trail_sec / 3600.0, restarts, ", ".join(parts)))
    return 0


if __name__ == "__main__":
    sys.exit(run())
