"""Бэктест ОСНОВАНИЯ «свежее окно» по СВОЕЙ истории (канон emptiness-alarm-needs-measured-rate).

ЗАЧЕМ. 21.08 в ветку остатка добавлено третье основание — свежее окно (2 последних прогона).
Основание, которое звонит чаще, чем происходит событие, — это не основание, а шум; прежде
чем поверить новой ветке, метрику обязаны прогнать по уже накопленной истории и назвать долю
срабатываний на НОРМЕ. Ровно так 20.08 был отвергнут порог по одиночному часу (22% ложных).

ЧТО СЧИТАЕТСЯ. Ветка остатка звонит, когда days_zero < days_reset, то есть когда
    темп > остаток / (суток до сброса)  ==  КРИТИЧЕСКИЙ ТЕМП.
Для каждого исторического прогона считаем три темпа (хвост 24ч / книга / свежее окно) и
смотрим, кто из них перешагнул критический темп своего момента.

НАЗВАННЫЙ ПРЕДЕЛ (не переобещать): якорь остатка есть ТОЛЬКО с 21.08 18:53Z — владельческое
чтение дашборда. Месяц-к-дате ДО этого момента не восстановим ничем, поэтому критический темп
считается по ОДНОМУ снимку остатка (18:53Z) на всём горизонте истории. Это делает бэктест
оценкой ЧАСТОТЫ СРАБАТЫВАНИЯ ОСНОВАНИЯ на реальном профиле нагрузки, а НЕ переигрыванием
прошлых приговоров. Второй предел: история — 33ч, это меньше месяца, и о сезонности суток
она говорит ровно столько, сколько в ней суток.

Запуск: python3 -m analysis.cu_quota_backtest
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.cu_quota_watch import (  # noqa: E402
    FRESH_MIN_SEC, MIN_BOOK_SEC, fresh_summary, parked_hosts, trail_summary,
)

WATCH_DIR = os.path.expanduser("~/.fleet-watch")


def _rate_day(recs, *, trail_sec, now):
    return {h: r * 1e6 / 30.0 / 1.0 for h, (r, _s, _since) in
            trail_summary(recs, trail_sec=trail_sec, now=now).items()}


def main():
    recs = [json.loads(x) for x in open(os.path.join(WATCH_DIR, "cu-quota.history.jsonl"),
                                        encoding="utf-8") if x.strip()]
    anchor = json.load(open(os.path.join(WATCH_DIR, "cu-quota.anchor.json"), encoding="utf-8"))
    ledger = json.load(open(os.path.join(WATCH_DIR, "cu-quota.ledger.json"), encoding="utf-8"))
    quota = float(anchor["quota_cu"])
    from analysis.cu_quota_watch import next_reset  # noqa: PLC0415

    recs.sort(key=lambda r: float(r["ts"]))
    hosts = sorted(anchor["hosts"])
    fires = {h: {"хвост24ч": 0, "книга": 0, "свежее": 0, "n": 0, "парк": 0} for h in hosts}
    rows = []
    for i in range(1, len(recs)):
        now = float(recs[i]["ts"])
        window = [r for r in recs[: i + 1]]
        trail = _rate_day(window, trail_sec=24 * 3600.0, now=now)
        fresh = fresh_summary(window, now=now)
        d_reset = max(1e-9, (next_reset(now, anchor.get("reset", "calendar")) - now) / 86400.0)
        # ГАРД ПАРКОВКИ (22.08) меряется ЗДЕСЬ ЖЕ, а не утверждается: он снимает с приговора
        # ключ, по которому бот положительно не ходит, и разницу «до/после» обязан показать
        # тот же прогон по истории, что оправдывал свежее окно.
        parked = parked_hosts(window, hosts)
        for h in hosts:
            a = anchor["hosts"][h]
            mtd = float(a["mtd_cu"]) + float((ledger.get("hosts", {}).get(h) or {}).get("cu", 0))
            crit = (quota - mtd) / d_reset          # критический темп, CU/сут
            f_rate, f_sec, _ = fresh.get(h, (0.0, 0.0, None))
            t_rate = trail.get(h, 0.0)
            if f_sec < FRESH_MIN_SEC:
                continue
            fires[h]["n"] += 1
            if t_rate > crit:
                fires[h]["хвост24ч"] += 1
            if f_rate > crit:
                fires[h]["свежее"] += 1
                if h not in parked:
                    fires[h]["парк"] += 1
            rows.append((now, h, t_rate, f_rate, crit))

    print("КРИТИЧЕСКИЙ ТЕМП (остаток на 21.08 18:53Z / суток до сброса), CU/сут:")
    for h in hosts:
        a = anchor["hosts"][h]
        mtd = float(a["mtd_cu"]) + float((ledger.get("hosts", {}).get(h) or {}).get("cu", 0))
        print("  %s: остаток %.1fM" % (h, (quota - mtd) / 1e6))
    print("\nДОЛЯ СРАБАТЫВАНИЙ ОСНОВАНИЯ ПО ИСТОРИИ (%d прогонов):" % len(recs))
    for h in hosts:
        d = fires[h]
        if not d["n"]:
            continue
        print("  %s: свежее %d/%d (%.0f%%), хвост24ч %d/%d (%.0f%%), "
              "свежее С ГАРДОМ ПАРКОВКИ %d/%d (%.0f%%)"
              % (h, d["свежее"], d["n"], 100.0 * d["свежее"] / d["n"],
                 d["хвост24ч"], d["n"], 100.0 * d["хвост24ч"] / d["n"],
                 d["парк"], d["n"], 100.0 * d["парк"] / d["n"]))
    print("\nПОСЛЕДНИЕ 8 ТОЧЕК (темп CU/сут против критического):")
    for now, h, t, f, crit in rows[-24:]:
        if h != "base-mainnet.g.alchemy.com#94bf":
            continue
        import time as _t
        print("  %s %s хвост=%7.0fk свежее=%7.0fk крит=%7.0fk %s"
              % (_t.strftime("%d %H:%M", _t.gmtime(now)), h.split("#")[1], t / 1e3, f / 1e3,
                 crit / 1e3, "<<< ЗВОНИТ (свежее)" if f > crit else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
