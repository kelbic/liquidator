"""Сторож квоты CU: журнал Шарлотт → медиана мгновенных окон за час → ИНБОКС АГЕНТА.

НАПРАВЛЕНИЕ (доктрина владельца 03.08, notify.py): квоту разбирает АГЕНТ — триаж
«правда/прогрев», перестрайп потребителей, ротация между СВОИМИ ключами. Человеку — только
вердикт «нужен НОВЫЙ ключ/оплата» (критерий HIL-1), через эскалацию инбокса. Прямой TG-пейдж
бота (hotpath, CU_ALERT_TG) погашен 20.08 после трёх заходов ложных пейджей kelbic за сутки;
путь в боте сохранён как env-резерв (CU_ALERT_TG=1 возвращает).

ЧИСЛИТЕЛЬ — ВТОРАЯ, НЕЗАВИСИМАЯ МЕХАНИКА ТРИАЖА (не «синхронизировать» с ботовой).
Бот меряет скользящую сумму часа: она насыщается бут-прогревом (два захода фиксов 20.08,
ex-boot-вид). Здесь — СРЕДНЕЕ мгновенных 2-мин окон БЕЗ бут-окон: бут-окно опознаётся по
полю самой строки (скольз.<16мин = первые минуты после рестарта). Среднее, а не медиана,
намеренно: квота жжётся СУММОЙ, и пилообразный расход (route_depth-квоты) медиана бы
занижала — именованный предел закрыт выбором оценщика, а не сноской.

ГДЕ ЗАПУСКАЕТСЯ: агентская машина (вотчеры — не на боевой, урок 30.07). Чтение журнала —
ssh read-only; ключи/секреты не читаются, идентификаторы хостов уже обезличены (#crc).

Запуск:  python3 -m analysis.cu_quota_watch          # прогон (крон, раз в час)
Ручки:   CU_WATCH_PROJ_M (порог медианы, M CU/мес; дефолт 26)
         CU_WATCH_MIN_WINDOWS (минимум окон для суда; дефолт 20)
         CU_WATCH_FAIL_STREAK (слепых прогонов подряд до тревоги о самом стороже; дефолт 3)
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
SOURCE = "cu-quota"
HOST = os.environ.get("CU_WATCH_SSH", "root@185.173.146.134")
QUOTA_M = 30.0

# Строка бота (chain/hotpath, каждые ~120с на ключ):
#   INFO CU base-mainnet.g.alchemy.com#3133: 866 CU/окно = 7 CU/s (проекция 18.7M/мес,
#   скольз.26мин 50.8M/мес) | eth_call@prearm.py:_read_target×38 ...
_LINE = re.compile(r"INFO CU ([a-z0-9.\-]+#[0-9a-f]{4}): .*?проекция ([0-9.]+)M/мес,\s*"
                   r"скольз\.(\d+)мин[^|]*\|\s*(.*)$")
BOOT_SPAN_MIN = 16      # скольз-спан < 16 мин => окно первых минут после рестарта (бут)


def parse_cu_windows(journal_text):
    """host -> {"projs": [проекции НЕ-бут окон], "boot": сколько бут-окон выкинуто,
    "top": последняя атрибуция}. Только квотуемые хосты (alchemy). Бут-окно опознаётся по
    полю самой строки: скольз-спан растёт с нуля после рестарта, <16 мин = прогрев."""
    out = {}
    for line in journal_text.splitlines():
        m = _LINE.search(line)
        if not m:
            continue
        host, proj, span_min, top = m.group(1), float(m.group(2)), int(m.group(3)), m.group(4).strip()
        if "alchemy" not in host:
            continue
        d = out.setdefault(host, {"projs": [], "boot": 0, "top": ""})
        if span_min < BOOT_SPAN_MIN:
            d["boot"] += 1
        else:
            d["projs"].append(proj)
        d["top"] = top or d["top"]
    return out


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


def run(*, fetch=None, notify_fn=None, out=print, now=None):
    threshold = float(os.environ.get("CU_WATCH_PROJ_M", "26"))
    min_windows = int(os.environ.get("CU_WATCH_MIN_WINDOWS", "20"))
    fail_streak_n = int(os.environ.get("CU_WATCH_FAIL_STREAK", "3"))
    notify_fn = notify_fn or _fleet_notify()
    st = _load_state()

    try:
        text = (fetch or _fetch_journal)()
        per_host = parse_cu_windows(text)
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
    st["last_ok"] = float(now if now is not None else time.time())
    _save_state(st)

    alarms = verdicts(per_host, threshold=threshold, min_windows=min_windows)
    for host, med, n, top in alarms:
        days = QUOTA_M / med * 30.0 if med > 0 else 0.0
        # ПОРОГ — В ТЕКСТЕ ТРЕВОГИ (разбор 20.08). Две тревоги 17:01 «жжёт 16M/20M» при
        # боевом пороге 26 были позитивным контролем с заниженным CU_WATCH_PROJ_M, но по
        # СВОЕМУ ТЕКСТУ отличить их от настоящих было нельзя: разбирающий доказывал
        # происхождение археологией по времени коммита сторожа. Число, по которому вынесен
        # приговор, обязано ехать вместе с приговором — иначе тревога не самодостаточна.
        msg = ("💸 КВОТА RPC (среднее часа без бут-окон, %d окон, порог %.0fM): %s жжёт "
               "%.0fM CU/мес при квоте %.0fM (~%.0f сут). Топ: %s\n"
               "РАЗБОР АГЕНТУ: прогрев уже отсеян — перестрайп потребителей "
               "/ ротация своих ключей. Человека звать ТОЛЬКО если вердикт «нужен новый "
               "ключ/оплата» (HIL-1). Отправки/sim не трогать."
               % (n, threshold, host, med, QUOTA_M, days, top[:160]))
        if notify_fn is None:
            out("cu-quota: КАНАЛ НЕДОСТУПЕН (~/.fleet-watch/notify.py), текст ниже")
            out(msg)
        else:
            verdict = notify_fn(msg, source=SOURCE, hil=False,
                                key="cu-quota:%s" % host, dedup_sec=21600)
            out("cu-quota: %s среднее %.1fM (n=%d) -> %s" % (host, med, n, verdict))
    if not alarms:
        hosts = ", ".join("%s=%.1fM(n=%d,бут%d)" % (h, statistics.fmean(d["projs"]) if d["projs"] else 0.0,
                                                     len(d["projs"]), d["boot"])
                          for h, d in sorted(per_host.items()))
        out("cu-quota: тихо (порог %.0fM): %s" % (threshold, hosts))
    return 0


if __name__ == "__main__":
    sys.exit(run())
