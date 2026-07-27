"""ОТЧЁТ ПО SHADOW-ДАТАСЕТУ predict-слоя (27.07): превратить каденс из «замерено на 6 окнах»
в «подтверждено на сотнях». Офлайн, только чтение журнала — ноль сети, ноль риска.

Отвечает на вопросы, ради которых слой и строился:
  1. РАСПРЕДЕЛЕНИЕ lead (арм -> транзит) — гистограмма, а не одно число;
  2. FP-доля: релиз по falsepos/stale против подтверждённого транзита;
  3. добыча: сколько окон вообще имели цели/жир (иначе фора не конвертируется);
  4. ГЕЙТ: какая доля окон с жиром прошла бы арм-гейт (dist<=потолок) — главный вопрос;
  5. каденс: покрывает ли текущая раскладка проб (burst + хвост) реальные lead'ы.

  journalctl -u liquidator-bot --no-pager > /tmp/all.log
  python analysis/shadow_report.py /tmp/all.log
"""
import re
import sys
from collections import Counter, defaultdict

RE_ARM = re.compile(r"SPECFIRE shadow arm feed=(\w+) dir=(\w+) targets=(\d+) debt=\$(\d+)k fat500k=(\d+)")
RE_OUT = re.compile(r"SPECFIRE shadow outcome feed=(\w+) lead=([\d.]+)s targets=(\d+) fat=(\d+) debt=\$(\d+)k")
RE_REL = re.compile(r"SPECFIRE shadow release feed=(\w+) kind=(\w+) targets=(\d+)")
RE_SKIP = re.compile(r"SPECFIRE arm-skip feed=(\w+): ближ\. жир dist=([\d.]+)%")
RE_LIVE = re.compile(r"SPECFIRE LIVE window feed=(\w+) dir=(\w+).*?\|ret\|>=([\d.]+)%")
RE_PROBE = re.compile(r"SPECFIRE probe #(\d+) feed=(\w+) \S+ debt=\$(\d+)k ret=(-?[\d.]+)%")
RE_TS = re.compile(r"^\w{3} \d+ [\d:]+ \S+ python\[\d+\]: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+)")

# текущая раскладка огня (chain/spec_fire_live.py): burst проб быстро, дальше хвост
BURST_N, BURST_IV, TAIL_IV, CAP = 2, 0.7, 3.5, 10
COVERAGE_SEC = BURST_N * BURST_IV + (CAP - BURST_N) * TAIL_IV


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100.0))]


def bar(n, total, width=34):
    return "█" * int(round(width * n / max(1, total)))


def main(path):
    arms, outs, rels, skips, lives, probes = [], [], [], [], [], []
    for line in open(path, errors="ignore"):
        m = RE_ARM.search(line)
        if m:
            arms.append((m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5))))
            continue
        m = RE_OUT.search(line)
        if m:
            outs.append((m.group(1), float(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))))
            continue
        m = RE_REL.search(line)
        if m:
            rels.append((m.group(1), m.group(2), int(m.group(3))))
            continue
        m = RE_SKIP.search(line)
        if m:
            skips.append((m.group(1), float(m.group(2))))
            continue
        m = RE_LIVE.search(line)
        if m:
            lives.append((m.group(1), m.group(2), float(m.group(3))))
            continue
        m = RE_PROBE.search(line)
        if m:
            probes.append((int(m.group(1)), m.group(2), int(m.group(3)), float(m.group(4))))

    print("=" * 78)
    print("SHADOW-ОТЧЁТ: %d армов, %d подтверждённых транзитов, %d релизов, %d проб" % (
        len(arms), len(outs), len(rels), len(probes)))
    print("=" * 78)

    # ---- 1. РАСПРЕДЕЛЕНИЕ LEAD ----
    leads = [o[1] for o in outs]
    print("\n1) LEAD (арм -> транзит), n=%d" % len(leads))
    if leads:
        print("   p10=%.0fс  p25=%.0fс  МЕДИАНА=%.0fс  p75=%.0fс  p90=%.0fс  max=%.0fс" % (
            pct(leads, 10), pct(leads, 25), pct(leads, 50), pct(leads, 75), pct(leads, 90), max(leads)))
        buckets = [(0, 10), (10, 20), (20, 30), (30, 45), (45, 60), (60, 90),
                   (90, 150), (150, 300), (300, 10 ** 9)]
        for lo, hi in buckets:
            n = sum(1 for x in leads if lo <= x < hi)
            if n:
                label = "%d-%ds" % (lo, hi) if hi < 10 ** 9 else "%d+s" % lo
                print("   %-9s %4d (%4.1f%%) %s" % (label, n, 100.0 * n / len(leads), bar(n, len(leads))))
        # ключевой вывод для каденса
        cov = sum(1 for x in leads if x <= COVERAGE_SEC)
        print("   -> текущее покрытие огня ~%.0fс (burst %d×%.1f + хвост %d×%.1f) накрывает %.0f%% lead'ов"
              % (COVERAGE_SEC, BURST_N, BURST_IV, CAP - BURST_N, TAIL_IV, 100.0 * cov / len(leads)))
        for target in (50, 75, 90):
            print("      чтобы накрыть %d%% — нужно покрытие >= %.0fс (%.1fс/проба при капе %d)"
                  % (target, pct(leads, target), pct(leads, target) / CAP, CAP))

    # ---- 2. FP против подтверждённых ----
    kinds = Counter(r[1] for r in rels)
    total_end = len(outs) + sum(kinds.values())
    print("\n2) ИСХОД ОКНА, n=%d" % total_end)
    print("   %-12s %4d (%4.1f%%) %s" % ("транзит", len(outs), 100.0 * len(outs) / max(1, total_end),
                                         bar(len(outs), total_end)))
    for k, n in kinds.most_common():
        print("   %-12s %4d (%4.1f%%) %s" % (k, n, 100.0 * n / max(1, total_end), bar(n, total_end)))

    # ---- 3. ДОБЫЧА ----
    print("\n3) ДОБЫЧА В ОКНАХ (фора конвертируется только при наличии целей)")
    with_t = sum(1 for o in outs if o[2] > 0)
    with_f = sum(1 for o in outs if o[3] > 0)
    print("   транзитов с целями (HF<=потолок API): %d/%d = %.1f%%" % (with_t, len(outs),
                                                                      100.0 * with_t / max(1, len(outs))))
    print("   транзитов с ЖИРОМ >=$500k          : %d/%d = %.1f%%" % (with_f, len(outs),
                                                                      100.0 * with_f / max(1, len(outs))))
    arms_fat = sum(1 for a in arms if a[4] > 0)
    print("   армов с жиром на прицеле           : %d/%d = %.1f%%" % (arms_fat, len(arms),
                                                                      100.0 * arms_fat / max(1, len(arms))))

    # ---- 4. ГЕЙТ ----
    print("\n4) АРМ-ГЕЙТ predicted-HF (главный вопрос: сколько окон вообще боеспособны)")
    print("   окон с жиром (кандидаты на огонь) : %d" % arms_fat)
    print("   из них ОТКРЫЛИ огонь (LIVE window): %d" % len(lives))
    print("   отсечено гейтом dist>потолок      : %d (лог троттлится 300с/фид — НИЖНЯЯ оценка)"
          % len(skips))
    if arms_fat:
        print("   -> доля боеспособных окон: %.1f%% (%d из %d)" % (
            100.0 * len(lives) / arms_fat, len(lives), arms_fat))
    if skips:
        d = [s[1] for s in skips]
        print("   дистанция отсечённого жира: медиана %.2f%%, min %.2f%%, max %.2f%%" % (
            pct(d, 50), min(d), max(d)))
        near = sum(1 for x in d if x <= 4.0)
        print("   из них «почти дотянулись» (dist<=4%%): %d (%.0f%%)" % (near, 100.0 * near / len(d)))

    # ---- 5. ПО ФИДАМ ----
    print("\n5) ПО ФИДАМ (где живёт фора)")
    by_feed = defaultdict(lambda: [0, 0, 0, []])
    for a in arms:
        by_feed[a[0]][0] += 1
        if a[4] > 0:
            by_feed[a[0]][1] += 1
    for o in outs:
        by_feed[o[0]][2] += 1
        by_feed[o[0]][3].append(o[1])
    print("   %-6s %6s %8s %8s %10s" % ("фид", "армов", "с жиром", "транзит", "медиана lead"))
    for f, (na, nf, no, ls) in sorted(by_feed.items(), key=lambda kv: -kv[1][0]):
        print("   %-6s %6d %8d %8d %10s" % (f, na, nf, no, ("%.0fс" % pct(ls, 50)) if ls else "—"))

    # ---- 6. ЖИВОЙ ОГОНЬ ----
    if probes:
        rets = [abs(p[3]) for p in probes]
        print("\n6) ЖИВОЙ ОГОНЬ: %d проб, |ret| медиана %.3f%% (порог dev), max %.3f%%" % (
            len(probes), pct(rets, 50), max(rets)))
        print("   проб на окно: %s" % dict(Counter(p[0] for p in probes).most_common(3)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/all.log")
