"""АТРИБУЦИЯ CU ПО ПОТРЕБИТЕЛЯМ И ПО ВРЕМЕНИ — прибор для триажа тревог cu-quota.

ЗАЧЕМ. Тревога сторожа квоты (analysis/cu_quota_watch.py) обязана везти ПРЕДЕЛ 1: «интеграл
СЛЕП К ПЕРЕСТРАЙПУ — до вердикта сверить атрибуцию ПО ВРЕМЕНИ (начало хвоста против конца), а
не только «Топ» последнего окна». Требование было записано 20.08, а ПРИБОРА под него не было:
три триажа подряд (20.08, 21.08 19:07, 21.08 23:07) сверяли атрибуцию глазами по хвосту
journalctl, то есть по последним строкам — ровно тем срезом, который предел и запрещает.
Здесь атрибуция считается: кто сколько CU съел за окно и КАК ЭТО МЕНЯЛОСЬ во времени.

ЧЕМ СЧИТАЕТ. Поле «Топ» CU-строки бота — это СЧЁТЧИКИ ВЫЗОВОВ (method@file:func×N), не CU.
Цена берётся из боевой таблицы chain.rpc.CU_COST (та же, которой считает сам бот), а не
переписывается сюда: разошедшаяся копия прайса врала бы молча (канон live-source-vs-repo-copy).

ЧЕГО НЕ УМЕЕТ (называю, а не умалчиваю):
  * Сумма по «Топ» < CU/окно строки: поле обрезано ботом до N позиций. Прибор ВСЕГДА печатает
    «покрытие» — долю CU/окно, объяснённую топом. Низкое покрытие = вывод о долях непригоден.
  * Атрибуция только БОТОВАЯ. Если по ключу ходит кто-то ещё, здесь его не видно (канон
    own-meter-blind-path: свой счётчик слеп к необслуживаемому пути).

Запуск:
  python3 -m analysis.cu_attrib < journal.txt            # весь дамп
  python3 -m analysis.cu_attrib --split 22:00 < dump.txt # ДО/ПОСЛЕ момента (UTC ЧЧ:ММ)
  ssh HOST "journalctl -u liquidator-bot --since '-3 hours' --no-pager | grep 'INFO CU '" \
      | python3 -m analysis.cu_attrib --split 22:00
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chain.rpc import CU_COST  # noqa: E402  боевой прайс, не копия

_LINE = re.compile(r"INFO CU ([a-z0-9.\-]+(?:#[0-9a-f]{4})?): (\d+) CU/окно .*?\|\s*(.*)$")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_ITEM = re.compile(r"([A-Za-z_]+)@([^\s×]+)×(\d+)")
DEFAULT_CU = 20   # тот же дефолт, что в chain/rpc.py:_cu_of — незнакомый метод не бесплатен


def parse(text):
    """[(hhmm, host, cu_window, [(method, consumer, n, cu), ...])] по строкам дампа."""
    rows = []
    for line in text.splitlines():
        m = _LINE.search(line)
        if not m:
            continue
        t = _ISO.search(line)
        hhmm = "%s:%s" % (t.group(4), t.group(5)) if t else "??:??"
        items = [(meth, cons, int(n), CU_COST.get(meth, DEFAULT_CU) * int(n))
                 for meth, cons, n in _ITEM.findall(m.group(3))]
        rows.append((hhmm, m.group(1), int(m.group(2)), items))
    return rows


def aggregate(rows, hosts=None):
    """host -> {"cu": сумма CU/окно, "top": {потребитель: cu}, "windows": n}."""
    out = {}
    for hhmm, host, cu, items in rows:
        if hosts and host not in hosts:
            continue
        a = out.setdefault(host, {"cu": 0, "top": {}, "windows": 0})
        a["cu"] += cu
        a["windows"] += 1
        for meth, cons, _n, icu in items:
            key = "%s@%s" % (meth, cons)
            a["top"][key] = a["top"].get(key, 0) + icu
    return out


def _report(title, agg, out=print):
    for host in sorted(agg):
        a = agg[host]
        top_cu = sum(a["top"].values())
        cov = (100.0 * top_cu / a["cu"]) if a["cu"] else 0.0
        out("\n%s %s: %d CU за %d окон (~%.1fM CU/мес), покрытие топом %.0f%%"
            % (title, host, a["cu"], a["windows"],
               a["cu"] / max(1, a["windows"]) / 120.0 * 2592000 / 1e6, cov))
        if cov < 80:
            out("   ⚠ ПОКРЫТИЕ НИЖЕ 80%: поле «Топ» обрезано, доли ниже НЕПРИГОДНЫ для вывода")
        for cons, cu in sorted(a["top"].items(), key=lambda kv: -kv[1])[:12]:
            out("   %7d CU  %5.1f%%  %s" % (cu, 100.0 * cu / max(1, top_cu), cons))


def main(argv=None, text=None, out=print):
    argv = list(argv if argv is not None else sys.argv[1:])
    split = None
    if "--split" in argv:
        split = argv[argv.index("--split") + 1]
    rows = parse(text if text is not None else sys.stdin.read())
    if not rows:
        out("cu-attrib: 0 CU-строк разобрано — не тот дамп или формат уехал")
        return 1
    out("cu-attrib: %d окон, %s..%s UTC" % (len(rows), rows[0][0], rows[-1][0]))
    if split:
        before = [r for r in rows if r[0] < split]
        after = [r for r in rows if r[0] >= split]
        out("\n===== ДО %s (%d окон) =====" % (split, len(before)))
        _report("ДО", aggregate(before), out=out)
        out("\n===== ПОСЛЕ %s (%d окон) =====" % (split, len(after)))
        _report("ПОСЛЕ", aggregate(after), out=out)
    else:
        _report("", aggregate(rows), out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
