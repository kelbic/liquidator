"""HEALTHY-RACE POLICY (П.1 пакета kelbic/ревьюера 22.08). Дефолт ВЫКЛ: REACTIVE_HEALTHY_RACE=0.

Инсайт эпизода 05:11Z: `HF_est <= 0.997` при sim «position is healthy» — не труп и не ошибка,
а ЛАГ ОРАКУЛА: наш эстиматор цену уже видел, цепь ещё нет. Мы стоим РАНЬШЕ флипа — самая
дорогая позиция в гонке; ровно такие 4 цели ($586–4436) отданы конкурентам (REFUSAL-LOSS=4,
сверено цепью). Правило: сим-реверт «healthy» не РЕЖЕТ цель, а переводит в БИД-режим, если
ВСЕ гейты открыты. Экономика: промах = газ ($7–9, on-chain guard min_profit держит слип),
попадание $300+; безубыток P>2.5%, априор P 0.15–0.25 до калибровки первыми 20 бидами.

Состояния: NORMAL (сим ок — обычный путь; сюда не попадает), HEALTHY_RACE (бид), CORPSE
(никогда не стрелять), SKIP (гейт закрыт — причина в reason).

Фильтр трупов — по ИНДЕКСУ ЦЕПИ (замер #35: 30/43 healthy-race были трупами): недавний
Liquidate по (mid, borrower) = труп. Это ОДНОВРЕМЕННО и «cooldown победителя» — отдельного
реестра не требуется: победитель виден только через Liquidate-лог, который индекс и хранит.
shares==0 отсекается ещё в prep («no debt (cleared)») и продублирован здесь гардом.

Бакет: HR_BUDGET_USD/сутки (UTC), ОТДЕЛЬНЫЙ от SPEC_FIRE_MAX_GAS_USD и каскадных капов;
списывается ОЦЕНКОЙ промаха при биде (fail-closed: переоценить трату лучше, чем прожечь
бюджет молча). Исчерпан -> SKIP до UTC-полуночи; при 80% reason несёт маркер для алерта.

Прибор сбоку: классификация чистая (юнит-тестируема), отказ любого ленивого импорта читается
как «сигнала нет» и ЗАКРЫВАЕТ гейт (незнание обязано закрывать гард).
"""
import json
import os
import threading
import time

HR_ON = os.environ.get("REACTIVE_HEALTHY_RACE", "0").lower() in ("1", "true", "yes", "on")
HR_HF_MAX = float(os.environ.get("HR_HF_MAX", "0.997"))
HR_FRESH_SEC = float(os.environ.get("HR_FRESH_SEC", "60"))
HR_EPISODE_TIP_WEI = int(os.environ.get("HR_EPISODE_TIP_WEI", "3000000"))   # >=3 мгвей = эскалация
HR_EPISODE_PENDING_SEC = float(os.environ.get("HR_EPISODE_PENDING_SEC", "10"))
HR_MIN_PRIZE_USD = float(os.environ.get("HR_MIN_PRIZE_USD", "300"))
HR_BUDGET_USD = float(os.environ.get("REACTIVE_HEALTHY_BUDGET_USD", "20"))
HR_MISS_COST_USD = float(os.environ.get("HR_MISS_COST_USD", "9"))           # оценка списания на бид
HR_CORPSE_LOOKBACK = int(os.environ.get("HR_CORPSE_LOOKBACK_BLOCKS", "150"))  # ~5 мин Base
HR_JOURNAL = os.environ.get("HR_JOURNAL", "data/healthy_race_bids.jsonl")

_LOCK = threading.Lock()
_BUCKET = {"day": None, "spent": 0.0, "alert80": False}
COUNTERS = {"hr_bids": 0, "hr_hits": 0, "hr_misses": 0,
            "hr_corpse_filtered": 0, "hr_budget_skips": 0, "hr_gate_skips": 0}


def _utc_day(now):
    return int(now // 86400)


def episode_active(now=None):
    """Эпизод жив: класс эскалации в кольце ЛИБО свежий pending-детект. Отказ сигнала = НЕТ."""
    try:
        from chain.hotpath import dominant_tip_wei
        if int(dominant_tip_wei(now) or 0) >= HR_EPISODE_TIP_WEI:
            return True
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from chain.pending_detect import last_handoff_age
        if last_handoff_age() is not None and last_handoff_age() <= HR_EPISODE_PENDING_SEC:
            return True
    except Exception:                                    # noqa: BLE001
        pass
    return False


def corpse_by_index(market_id, borrower, lookback=None):
    """Труп по индексу: Liquidate по (mid, borrower) в последних lookback блоках.
    Индекс не поднят/выключен -> None (сигнала нет; вызывающий закрывает гейт)."""
    try:
        from chain.chain_index import get_index
        idx = get_index()
        if idx is None:
            return None
        return idx.liquidated_recently(market_id, borrower,
                                       lookback=int(lookback or HR_CORPSE_LOOKBACK))
    except Exception:                                    # noqa: BLE001
        return None


def classify(market_id, borrower, hf_est, prize_net_usd, data_age_s=0.0,
             borrow_shares=1, episode=None, corpse=None, now=None):
    """Чистая классификация: все аргументы-сигналы можно подать снаружи (стенд), дефолты
    добывают их сами. Возврат (state, reason): HEALTHY_RACE / CORPSE / SKIP."""
    now = time.time() if now is None else now
    if not HR_ON:
        return ("SKIP", "off")
    if borrow_shares <= 0:
        with _LOCK:
            COUNTERS["hr_corpse_filtered"] += 1
        return ("CORPSE", "shares=0")
    c = corpse if corpse is not None else corpse_by_index(market_id, borrower)
    if c is None:
        with _LOCK:
            COUNTERS["hr_gate_skips"] += 1
        return ("SKIP", "индекс слеп — незнание закрывает гейт")
    if c:
        with _LOCK:
            COUNTERS["hr_corpse_filtered"] += 1
        return ("CORPSE", "Liquidate в индексе (труп/добит победителем)")
    if hf_est is None or hf_est > HR_HF_MAX:
        with _LOCK:
            COUNTERS["hr_gate_skips"] += 1
        return ("SKIP", "hf_est=%s > %.3f (воды не видим)" % (
            "%.4f" % hf_est if hf_est is not None else "?", HR_HF_MAX))
    if data_age_s > HR_FRESH_SEC:
        with _LOCK:
            COUNTERS["hr_gate_skips"] += 1
        return ("SKIP", "данные протухли (%.0fс > %.0fс)" % (data_age_s, HR_FRESH_SEC))
    ep = episode if episode is not None else episode_active(now)
    if not ep:
        with _LOCK:
            COUNTERS["hr_gate_skips"] += 1
        return ("SKIP", "эпизод не активен (нет эскалации/pending)")
    if prize_net_usd is None or prize_net_usd < HR_MIN_PRIZE_USD:
        with _LOCK:
            COUNTERS["hr_gate_skips"] += 1
        return ("SKIP", "приз $%.0f < $%.0f" % (prize_net_usd or 0.0, HR_MIN_PRIZE_USD))
    with _LOCK:
        day = _utc_day(now)
        if _BUCKET["day"] != day:
            _BUCKET.update({"day": day, "spent": 0.0, "alert80": False})
        if _BUCKET["spent"] + HR_MISS_COST_USD > HR_BUDGET_USD:
            COUNTERS["hr_budget_skips"] += 1
            return ("SKIP", "бакет исчерпан ($%.2f/$%.2f) — OFF до UTC-полуночи"
                    % (_BUCKET["spent"], HR_BUDGET_USD))
    return ("HEALTHY_RACE", "лаг оракула: hf=%.4f приз=$%.0f" % (hf_est, prize_net_usd))


def note_bid(market_id, borrower, prize_usd, tip_wei, hf_est, txh=None, now=None):
    """Списание бакета оценкой промаха + журнал калибровки P (первые исходы сверяются
    авто-отчётом по хэшам). Возврат: строка-предупреждение о 80% бакета либо None."""
    now = time.time() if now is None else now
    warn = None
    with _LOCK:
        day = _utc_day(now)
        if _BUCKET["day"] != day:
            _BUCKET.update({"day": day, "spent": 0.0, "alert80": False})
        _BUCKET["spent"] += HR_MISS_COST_USD
        COUNTERS["hr_bids"] += 1
        if not _BUCKET["alert80"] and _BUCKET["spent"] >= 0.8 * HR_BUDGET_USD:
            _BUCKET["alert80"] = True
            warn = "HR бюджет 80%%: $%.2f из $%.2f за сутки" % (_BUCKET["spent"], HR_BUDGET_USD)
    try:
        os.makedirs(os.path.dirname(HR_JOURNAL) or ".", exist_ok=True)
        with open(HR_JOURNAL, "a") as f:
            f.write(json.dumps({"ts": now, "mid": market_id, "borrower": borrower,
                                "prize_usd": round(float(prize_usd), 2), "tip_wei": int(tip_wei),
                                "hf": round(float(hf_est), 5), "tx": txh}) + "\n")
    except Exception:                                    # noqa: BLE001
        pass
    return warn


def note_outcome(hit):
    with _LOCK:
        COUNTERS["hr_hits" if hit else "hr_misses"] += 1


def hr_tip_wei(default_wei):
    """Tip бида = зеркало ТЕКУЩЕГО класса эскалации, не пол: на полу при +140 порядок
    гарантированно проигран, класс выводит в ничейную полосу. Сигнала нет -> default."""
    try:
        from chain.hotpath import dominant_tip_wei
        d = int(dominant_tip_wei() or 0)
        return max(d, int(default_wei))
    except Exception:                                    # noqa: BLE001
        return int(default_wei)


def hf_estimate(collateral, price, lltv_wad, borrow_assets):
    """HF по данным prep'а: collateral (ед. коллатерала) x price (loan/coll, 1e36) x lltv /
    долг в loan-единицах. Нечисло/ноль долга -> None (гейт закроется)."""
    try:
        if borrow_assets <= 0:
            return None
        coll_loan = int(collateral) * int(price) // 10**36
        return (coll_loan * int(lltv_wad) / 1e18) / float(borrow_assets)
    except Exception:                                    # noqa: BLE001
        return None
