"""SPEC-FIRE SHADOW (24.07): живой замер ДОБЫЧИ в predict-окнах — ничего не шлёт.

Гейт снят данными: бэктест каскада 24-25.06 (87% ликвидаций / $12.9M при армленном окне,
lead med 67с p25 38с) + live spec-canary DELTA=4 (предочередь [0,5] достижима). Прежде чем
строить живой огонь, shadow копит боевую статистику на НАШЕМ candidate set: сколько окон
приходит с целями (и с жиром >= SPEC_FIRE_FAT_USD), какой lead, сколько целей в окне.
Корреляция «цель флипнула» — офлайн по журналу (SPECFIRE-строки рядом с HOT ready).
Live-огонь будет ОТДЕЛЬНЫМ флагом и отдельным кодом — этот модуль read-only по shared.

Дизайн-инсайт бэктеста: lead ~минута => главный рычаг PRE-ARM (подготовка позиций/calldata
заранее), спам — только последние секунды. Shadow меряет обе величины: lead окна (held_s
у confirmed) и наличие целей в момент arm.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("liq.spec_fire")

FAT_USD = float(os.environ.get("SPEC_FIRE_FAT_USD", "500000"))

# токен -> predict-лейбл (какой фид двигает цену токена). Стейблы/фиат -> None.
# Пары, где ОБЕ ноги на одном фиде (wstETH/WETH, LBTC/cbBTC), — peg/rate, фид их НЕ флипает.
_TOKEN2LABEL = {
    "CBBTC": "BTC", "LBTC": "BTC",
    "WETH": "ETH", "CBETH": "ETH", "WEETH": "ETH", "WSTETH": "ETH",
    "WSUPEROETHB": "ETH", "MSETH": "ETH",
    "CBDOGE": "DOGE", "CBADA": "ADA", "CBLTC": "LTC", "CBXRP": "XRP",
    "SOL": "SOL", "USOL": "SOL", "JITOSOL": "SOL",
}


def _label_of(token: str) -> str | None:
    return _TOKEN2LABEL.get((token or "").upper())


class SpecFireShadow:
    """Потребитель событий PredictDriver (on_event). Только логи/счётчики, ноль отправок."""

    def __init__(self, cfg, shared, fat_usd: float | None = None, log_fn=None):
        self.cfg, self.shared = cfg, shared
        self.fat_usd = FAT_USD if fat_usd is None else float(fat_usd)
        self._log = log_fn or (lambda m: log.info("%s", m))
        self._lock = threading.Lock()
        self._windows: dict = {}     # feed label -> {"targets": n, "fat": n, "debt": usd}
        self.stats = {"windows": 0, "with_targets": 0, "with_fat": 0, "confirmed_with_targets": 0}

    # ---- выбор целей: рынок «ведóм фидом», если ведома ровно ОДНА нога ----
    def _targets(self, feed: str, direction: str):
        n = fat = 0
        debt = 0.0
        top: list = []
        with self.shared.lock:
            groups = dict(self.shared.groups)
            debt_by = dict(self.shared.debt_by)
            by_id = dict(self.shared.by_id)
        for mid, borrowers in groups.items():
            m = by_id.get(mid)
            if m is None:
                continue
            cl = _label_of(getattr(m, "collateral_token", ""))
            ll = _label_of(getattr(m, "loan_token", ""))
            if cl == ll:                      # peg/rate или не-наш фид с обеих сторон
                continue
            # down: дешевеет ЗАЛОГ (collateral на фиде); up: дорожает ДОЛГ (loan на фиде)
            if not ((direction == "down" and cl == feed) or (direction == "up" and ll == feed)):
                continue
            for b in borrowers:
                d = float(debt_by.get((mid, b), 0.0) or 0.0)
                n += 1
                debt += d
                if d >= self.fat_usd:
                    fat += 1
                    top.append((mid, b, d))
        top.sort(key=lambda t: -t[2])
        return n, fat, debt, top[:3]

    def handle_event(self, ev: dict):
        try:
            kind = ev.get("event")
            feed = (ev.get("feed") or "").upper()
            if kind == "arm":
                n, fat, debt, top = self._targets(feed, ev.get("dir") or "down")
                with self._lock:
                    self._windows[feed] = {"targets": n, "fat": fat, "debt": debt}
                    self.stats["windows"] += 1
                    if n:
                        self.stats["with_targets"] += 1
                    if fat:
                        self.stats["with_fat"] += 1
                self._log("SPECFIRE shadow arm feed=%s dir=%s targets=%d debt=$%.0fk fat%dk=%d%s" % (
                    feed, ev.get("dir"), n, debt / 1e3, self.fat_usd / 1e3, fat,
                    (" top=[" + ",".join("%s/%s:$%.0fk" % (m[:8], b[:8], d / 1e3) for m, b, d in top) + "]")
                    if top else ""))
                return {"targets": n, "fat": fat}
            if kind in ("confirmed", "push", "disarm", "falsepos", "stale"):
                with self._lock:
                    w = self._windows.pop(feed, None)
                    if kind == "confirmed" and w and w["targets"]:
                        self.stats["confirmed_with_targets"] += 1
                if kind == "confirmed":
                    self._log("SPECFIRE shadow outcome feed=%s lead=%ss targets=%d fat=%d debt=$%.0fk | "
                              "run: win=%d with_targets=%d with_fat=%d confirmed_hit=%d" % (
                                  feed, ev.get("lead_s"), (w or {}).get("targets", 0), (w or {}).get("fat", 0),
                                  (w or {}).get("debt", 0.0) / 1e3, self.stats["windows"],
                                  self.stats["with_targets"], self.stats["with_fat"],
                                  self.stats["confirmed_with_targets"]))
                elif w is not None:
                    self._log("SPECFIRE shadow release feed=%s kind=%s targets=%d" % (feed, kind, w["targets"]))
                return {"released": kind}
        except Exception:
            log.exception("spec-fire shadow: событие %s упало (shadow, боевой путь не задет)", ev.get("event"))
        return None
