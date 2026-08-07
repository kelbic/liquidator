"""Явка: честный знаменатель win-rate (задача #31).

«0 побед из 9794» — знаменатель ни о чём: 9794 это НАБЛЮДЁННЫЕ события, а не гонки, в которых
мы участвовали. Вопрос, на который отвечает этот разбор, ровно один и он решает стратегию:

    когда на наших рынках случалась ликвидация — у нас в этот момент было открыто окно,
    и если было, стреляли ли мы?

Три исхода и три разных диагноза:
  * ОСПОРЕНО (окно открыто, probes>0) — мы были в гонке и проиграли. Лечится скоростью.
  * ОКНО БЫЛО, НЕ СТРЕЛЯЛИ (probes=0) — детект сработал, гейт не пустил. Лечится гейтом.
  * ОКНА НЕ БЫЛО                      — мы события не увидели. Лечится детектом.
Пока эти три не разделены, «мы медленные» и «нас там не было» неразличимы, а лечат их
противоположными вещами.

Время берём в epoch С ОБЕИХ сторон: `journalctl -o short-unix` против unix-timestamp API.
Сравнение локальных строк времени с epoch — классический источник тихого сдвига на часовой пояс.

    python -m analysis.attendance [дней] [min_repaid_usd]

ПРЕДЕЛ, который надо знать до чтения вывода: строка `SPECFIRE window close` появилась в логе
только 04.08 — за более ранние сутки окна не закрываются и явка не считается. Скрипт сам
печатает фактическую глубину и НЕ выдаёт молчание за ноль.
"""
from __future__ import annotations
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from analysis.contestation import _gql, _loan_price, _Q          # noqa: E402

OPEN_RE = re.compile(r"^(\d+)\.\d+.*SPECFIRE LIVE window feed=(\S+)")
CLOSE_RE = re.compile(r"^(\d+)\.\d+.*SPECFIRE window close feed=(\S+) probes=(\d+)")
HOLD_RE = re.compile(r"^(\d+)\.\d+.*SPECFIRE hold feed=(\S+).*dist=([\d.]+)%.*reach=([\d.]+)%")
# ВТОРОЙ БОЕВОЙ ПУТЬ. Первая версия разбора считала только окна SPECFIRE и отнесла каскад
# 06.08 23:35 ($67.6k, четыре ликвидации в одном блоке) к «окна не было» — хотя горячий путь
# отработал полностью: HOT ready FAST по четырём целям и `dispatch: sent n=4`. Залповый флот и
# горячий диспатч — РАЗНЫЕ пути, и мерить явку по одному из них значит записывать проигранную
# гонку в неувиденные. Инструмент, который мислейблит, хуже отсутствующего.
DISPATCH_RE = re.compile(r"^(\d+)\.\d+.*dispatch: sent nonce=(\d+) n=(\d+)")
READY_RE = re.compile(r"^(\d+)\.\d+.*HOT ready FAST \S+ hf=")
# Каскад разбирается за секунды, а метка времени ликвидации — метка БЛОКА: допуск должен
# покрывать разбор блока и лаг индексатора, но не соседнее событие.
HOT_TOL_SEC = 90


def _journal(days: int):
    """Лента окон из журнала. epoch с обеих сторон — никакого разбора локального времени."""
    out = subprocess.run(
        ["journalctl", "-u", "liquidator-bot", "-o", "short-unix", "--no-pager",
         "--since", "%d days ago" % days],
        capture_output=True, text=True, errors="replace").stdout
    opens, closes, holds, disp, ready = [], [], [], [], []
    for ln in out.splitlines():
        m = OPEN_RE.match(ln)
        if m:
            opens.append((int(m.group(1)), m.group(2)))
            continue
        m = CLOSE_RE.match(ln)
        if m:
            closes.append((int(m.group(1)), m.group(2), int(m.group(3))))
            continue
        m = HOLD_RE.match(ln)
        if m:
            holds.append((int(m.group(1)), m.group(2), float(m.group(3)), float(m.group(4))))
            continue
        m = DISPATCH_RE.match(ln)
        if m:
            disp.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
            continue
        m = READY_RE.match(ln)
        if m:
            ready.append(int(m.group(1)))
    return opens, closes, holds, disp, ready


# Потолок длительности окна. Строка `SPECFIRE window close` появилась в логе только 04.08, а
# рестартов бота — десятки: закрытий втрое меньше открытий, и НАИВНАЯ сшивка (FIFO без потолка)
# склеивает закрытие 04.08 с открытием 31.07. Такое «окно» длиной в сутки накрывает любую
# ликвидацию и превращает разбор в машину, способную выдать только «оспорено» — то есть в
# ответ, не зависящий от данных. Замер по журналу: реальное окно живёт 20с-3мин.
MAX_WIN_SEC = 1800


def _windows(opens, closes, max_win: int = MAX_WIN_SEC):
    """Сшить открытия с закрытиями по фиду.

    Закрытие берёт САМОЕ СВЕЖЕЕ незакрытое открытие того же фида (не самое старое): потерянные
    закрытия — норма при рестарте, и FIFO в этих условиях сшивает заведомо чужие пары. Всё, что
    старше потолка, отбрасывается как потерянное, а не растягивается.
    """
    pend, wins, dropped = {}, [], 0
    ev = [(t, "o", f, 0) for t, f in opens] + [(t, "c", f, p) for t, f, p in closes]
    ev.sort(key=lambda x: (x[0], x[1] == "c"))
    for t, kind, feed, probes in ev:
        q = pend.setdefault(feed, [])
        n0 = len(q)
        q[:] = [t0 for t0 in q if t - t0 <= max_win]      # протухшие открытия — выбросить
        dropped += n0 - len(q)
        if kind == "o":
            q.append(t)
        else:
            t0 = q.pop() if q else t                       # самое свежее, не самое старое
            wins.append({"feed": feed, "t0": t0, "t1": t, "probes": probes, "open_ended": False})
    now = int(time.time())
    for feed, q in pend.items():
        for t0 in q:
            # незакрытое окно тоже ограничено потолком: «до сих пор открыто» для окна недельной
            # давности — не факт, а артефакт пропавшей строки закрытия
            wins.append({"feed": feed, "t0": t0, "t1": min(now, t0 + max_win),
                         "probes": None, "open_ended": True})
    wins.sort(key=lambda w: w["t0"])
    return wins, dropped


def _feed_matches(feed: str, pair: str) -> bool:
    """Фид цены против пары рынка: XRP <-> cbXRP/USDC. Сопоставляем по символу залога, а не по
    имени рынка целиком — обёртки (cb*/w*) иначе никогда не совпадут."""
    coll = (pair.split("/")[0] or "").lower()
    f = feed.lower()
    return f in coll or coll.lstrip("cbwx").startswith(f)


def run(cfg, days: int = 7, min_repaid_usd: float = 1000.0) -> None:
    from strategy.scanner import load_covered_markets

    markets = load_covered_markets(cfg.covered_markets_path)
    by_id = {m.market_id.lower() for m in markets}
    since = int(time.time()) - days * 86400
    price_cache: dict = {}

    rows, skip = [], 0
    while skip <= 4000:
        r = _gql(_Q, {"first": 100, "skip": skip, "cid": [cfg.chain_id]})
        if r.get("errors"):
            print("API errors:", r["errors"])
            return
        batch = (((r.get("data") or {}).get("marketTransactions") or {}).get("items")) or []
        if not batch:
            break
        stop = False
        for it in batch:
            ts = int(it["timestamp"])
            if ts < since:
                stop = True
                break
            mk = it.get("market") or {}
            if (mk.get("marketId") or "").lower() not in by_id:
                continue
            d = it.get("data") or {}
            la = mk.get("loanAsset") or {}
            dec = int(la.get("decimals") or 18)
            price = _loan_price(la.get("address", ""), cfg.chain_id, price_cache)
            rows.append({
                "ts": ts, "liq": (d.get("liquidator") or "").lower(),
                "pair": "%s/%s" % ((mk.get("collateralAsset") or {}).get("symbol", "?"),
                                   la.get("symbol", "?")),
                "repaid_usd": int(d.get("repaidAssets") or 0) / 10 ** dec * price})
        if stop:
            break
        skip += 100

    big = [r for r in rows if r["repaid_usd"] >= min_repaid_usd]
    opens, closes, holds, disp, ready = _journal(days)
    wins, dropped = _windows(opens, closes)
    med = sorted(w["t1"] - w["t0"] for w in wins if not w["open_ended"])

    print("=== ЯВКА за %dд: наши %d рынков, порог $%s ===" % (days, len(markets), f"{min_repaid_usd:,.0f}"))
    print("ликвидаций всего: %d | крупных: %d" % (len(rows), len(big)))
    print("окон в журнале: открытий %d, закрытий %d, сшито %d (незакрытых %d, протухших %d)"
          % (len(opens), len(closes), len(wins), sum(1 for w in wins if w["open_ended"]), dropped))
    # Медиана длительности — проверка самой сшивки: окно в часы означает склейку чужих пар,
    # а не долгое окно, и тогда любому «оспорено» ниже верить нельзя.
    if med:
        print("длительность сшитого окна: медиана %ds, max %ds (потолок %ds)"
              % (med[len(med) // 2], med[-1], MAX_WIN_SEC))

    # Глубина журнала — НАЗВАТЬ, а не подразумевать: строка close появилась 04.08, и за более
    # ранние сутки «окна не было» означало бы «лога не было», то есть враньё в пользу детекта.
    if wins:
        depth_h = (int(time.time()) - min(w["t0"] for w in wins)) / 3600.0
        print("глубина ленты окон: %.1f ч (раньше строки закрытия окна в логе нет)" % depth_h)
        cutoff = min(w["t0"] for w in wins)
    else:
        print("ЛЕНТЫ ОКОН НЕТ ВОВСЕ — явку считать не на чем, вывод ниже был бы ложным нулём")
        return
    if not big:
        print("крупных ликвидаций за окно нет — знаменатель пуст, считать нечего.")
        return

    fired = sum(1 for w in wins if (w["probes"] or 0) > 0)
    print("окон со стрельбой: %d из %d (%.0f%%), проб суммарно: %d"
          % (fired, len(wins), 100.0 * fired / len(wins), sum(w["probes"] or 0 for w in wins)))

    print("горячий путь: диспатчей %d, HOT ready FAST %d" % (len(disp), len(ready)))

    cats = {"оспорено": [], "окно было, не стреляли": [], "окна не было": [], "вне ленты": []}
    for r in big:
        if r["ts"] < cutoff:
            cats["вне ленты"].append(r)
            continue
        r["hot"] = [d for d in disp if abs(d[0] - r["ts"]) <= HOT_TOL_SEC]
        hit = [w for w in wins if w["t0"] <= r["ts"] <= w["t1"] and _feed_matches(w["feed"], r["pair"])]
        if r["hot"] or any((w["probes"] or 0) > 0 for w in hit):
            cats["оспорено"].append(r)      # залп ИЛИ горячий диспатч — оба суть участие в гонке
        elif hit:
            cats["окно было, не стреляли"].append(r)
        else:
            cats["окна не было"].append(r)

    print("\n--- ЧЕСТНЫЙ ЗНАМЕНАТЕЛЬ ---")
    inside = len(big) - len(cats["вне ленты"])
    for k in ("оспорено", "окно было, не стреляли", "окна не было"):
        n = len(cats[k])
        print("  %-24s %3d  (%s)" % (k, n, "%.0f%%" % (100.0 * n / inside) if inside else "—"))
    if cats["вне ленты"]:
        print("  %-24s %3d  (лента окон не достаёт — НЕ засчитано ни в одну сторону)"
              % ("вне ленты", len(cats["вне ленты"])))

    if cats["оспорено"]:
        print("\n  win-rate по ОСПОРЕННЫМ: 0/%d  <- вот настоящий знаменатель" % len(cats["оспорено"]))
        for r in cats["оспорено"][:12]:
            # каким путём участвовали — залповым или горячим: лечатся они по-разному
            how = "горячий n=%d" % sum(d[2] for d in r["hot"]) if r.get("hot") else "залп"
            print("    %s %-16s $%-9s [%-12s] победитель %s"
                  % (time.strftime("%m-%d %H:%M", time.gmtime(r["ts"])), r["pair"],
                     "%.0f" % r["repaid_usd"], how, r["liq"][:14]))
    else:
        print("\n  ОСПОРЕННЫХ ГОНОК НЕТ: мы не проигрывали — нас в этих гонках не было.")
        print("  «0 побед» на таком знаменателе не измеряет ни скорость, ни флот.")

    if cats["окно было, не стреляли"]:
        print("\n--- ОКНО БЫЛО, ГЕЙТ НЕ ПУСТИЛ (лечится гейтом, не скоростью) ---")
        for r in cats["окно было, не стреляли"][:10]:
            near = [h for h in holds if abs(h[0] - r["ts"]) <= 120]
            g = (" ближайший hold: dist=%.2f%% reach=%.2f%%" % (near[0][2], near[0][3])) if near else ""
            print("    %s %-16s $%s%s" % (time.strftime("%m-%d %H:%M", time.gmtime(r["ts"])),
                                          r["pair"], "%.0f" % r["repaid_usd"], g))

    if cats["окна не было"]:
        print("\n--- ОКНА НЕ БЫЛО (лечится детектом) ---")
        for r in cats["окна не было"][:10]:
            print("    %s %-16s $%-10s победитель %s"
                  % (time.strftime("%m-%d %H:%M", time.gmtime(r["ts"])), r["pair"],
                     "%.0f" % r["repaid_usd"], r["liq"][:14]))


if __name__ == "__main__":
    from config import Config
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    mr = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
    run(Config.from_env(), d, mr)
