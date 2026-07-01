#!/usr/bin/env python3
"""gap_verdict.py — Layer 0 вердикт по рычагу (read-only, stdlib). v3: trigger-floor fix + swapcut.

ЕДИНИЦА: входные позиции (trigger/winner/our) в ОДНОЙ согласованной единице — либо приближённый
индекс флешблока (восстановление по накопл. газ-позиции: cum_gas/block_gas*10, ±1 шумно — груб. форк),
либо tx-индекс в блоке (gap_profile, точно, on-chain). L задаётся в той же единице.
ФИЗИКА (trigger-floor): позиция НЕ ликвидируема до применения оракул-апдейта → наш минимум = флешблок
ТРИГГЕРА. Чистая меж-флешблочная победа: trig + max(0, our_reaction − L) ≤ winner − 1.
  our_reaction = our − trig = g + wr ;  winner = trig + wr ;  g = our − win.
  → чистая победа ⟺ g ≤ L−1 И wr ≥ 1 (если wr=0, победитель в флешблоке триггера → максимум ТАЙ).
L = снимаемое срезом, в единице входа. Дефолт по STATE: прямой-пул своп убирает quote 415мс из
пайплайна 704→~450 → ~254мс ≈ 1.27 флешблока. (Вариант A = воспроизвести Kyber-calldata — СТЕНА:
серверный routeID/ri. Рычаг STATE = ПРЯМОЙ своп через один глубокий пул, публичный ABI, локальная calldata.)

Бакеты:
  no-show         winner пуст → ликвидации не было. no_show_reason; НЕ авто-возможность.
  detect/funnel   winner есть, our_status=funnel → не диспатчили (детект/funnel). Рычаг: ДЕТЕКТ. G не считаем.
  filtered        winner есть, our_status=filtered → осознанно пропустили. Не addressable.
  --- contested (our_status=contested, our из рецепта) ---
  slot1-fee       g ≤ 0          → сели ≤ победителя, но проиграли → fee/ordering ИЛИ наша tx реверта. Не латентность.
  swapcut-clean   wr≥1 И g≤L−1   → срез выводит на ≥1 раньше → ЧИСТАЯ победа (прямой-пул своп, STATE шаг 2).
  slot1-tie       (wr=0 И g≤L) ИЛИ (wr≥1 И L−1<g≤L) → срез упирается в флешблок победителя → fee-тай (на Base обложен).
  late-increment  wr≥1 И g>L     → сняв срез, всё ещё позади → co-location/доп.срез (позднее приращение).
  structural      wr=0 И g>L     → победитель в флешблоке триггера, срез не дотягивает даже до тая → ПОТЕРЯНО.

ВХОДНОЙ КОНТРАКТ (CSV, --in): flip_id, trigger_fb_abs, winner_fb_abs(пусто=no-show), prize_usd,
  our_status(contested|funnel|filtered), our_fb_abs(обязат. при contested), no_show_reason(опц.).
Запуск:  python3 gap_verdict.py --in flips.csv [--l-fb 1.27]
Глобальной оценки НЕТ: дельты только на гонявшихся (иначе детект-фейлы маскируются под латентность).
"""
import argparse, csv, sys
from collections import defaultdict


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


ap = argparse.ArgumentParser()
ap.add_argument("--in", dest="inp", required=True)
ap.add_argument("--l-fb", dest="l_fb", type=float, default=(704 - 450) / 200)  # STATE: прямой-пул срез ~254мс
a = ap.parse_args()
L = a.l_fb

with open(a.inp, newline="") as fh:
    rows = list(csv.DictReader(fh))
if not rows:
    sys.exit("пустой CSV")

B = defaultdict(lambda: {"n": 0, "usd": 0.0})
gfb = []
reasons = defaultdict(lambda: {"n": 0, "usd": 0.0})
bad = []

for i, r in enumerate(rows, 1):
    trig = f(r.get("trigger_fb_abs"))
    win = f(r.get("winner_fb_abs"))
    prize = f(r.get("prize_usd")) or 0.0
    status = (r.get("our_status") or "").strip().lower()
    our = f(r.get("our_fb_abs"))
    reason = (r.get("no_show_reason") or "unknown").strip().lower()

    if win is None:
        B["no-show"]["n"] += 1; B["no-show"]["usd"] += prize
        reasons[reason]["n"] += 1; reasons[reason]["usd"] += prize
        continue
    if status == "funnel":
        if our is not None:
            bad.append((i, "funnel, но our_fb_abs задан")); continue
        B["detect/funnel"]["n"] += 1; B["detect/funnel"]["usd"] += prize; continue
    if status == "filtered":
        B["filtered"]["n"] += 1; B["filtered"]["usd"] += prize; continue
    if status != "contested":
        bad.append((i, f"our_status='{status}' (ожид contested/funnel/filtered)")); continue
    if our is None or trig is None:
        bad.append((i, "contested, но нет our_fb_abs/trigger_fb_abs")); continue

    g = our - win
    wr = win - trig
    gfb.append(g)
    if g <= 0:
        b = "slot1-fee"
    elif wr == 0:
        b = "slot1-tie" if g <= L else "structural"
    else:
        if g <= L - 1:
            b = "swapcut-clean"
        elif g <= L:
            b = "slot1-tie"
        else:
            b = "late-increment"
    B[b]["n"] += 1; B[b]["usd"] += prize

WON = [b for b in ("detect/funnel", "filtered", "slot1-fee", "swapcut-clean",
                   "slot1-tie", "late-increment", "structural") if b in B]
won_usd = sum(B[b]["usd"] for b in WON) or 1.0
BUILD = ("swapcut-clean", "detect/funnel", "late-increment")

LEVER = {
    "no-show": "— (проверь причину)",
    "detect/funnel": "ФИКС ДЕТЕКЦИИ (не латентность)",
    "filtered": "— (сами пропустили)",
    "slot1-fee": "fee/ordering или наша реверта (не латентность)",
    "swapcut-clean": "ПРЯМОЙ-ПУЛ СВОП-СРЕЗ (STATE шаг 2) → чистая победа",
    "slot1-tie": "Слот-1 fee-тай (на Base ОБЛОЖЕН) — low-value",
    "late-increment": "co-location / доп. срез",
    "structural": "ПОТЕРЯНО (победитель в флешблоке триггера)",
}
order = ["no-show", "detect/funnel", "filtered", "slot1-fee", "swapcut-clean",
         "slot1-tie", "late-increment", "structural"]
print(f"=== gap_verdict v3: {len(rows)} флипов | L={L:.2f} (чистая победа: g≤{L-1:.2f} И wr≥1) ===")
print(f"{'бакет':<16}{'n':>4}{'repaid$':>13}{'%won':>7}  рычаг")
for b in order:
    if b not in B:
        continue
    v = B[b]
    pw = f"{v['usd']/won_usd*100:>5.0f}%" if b in WON else "    —"
    print(f"  {b:<14}{v['n']:>4}{v['usd']:>13,.0f}{pw:>7}  {LEVER[b]}")

if gfb:
    gs = sorted(gfb)
    print(f"\nG (контестированные): n={len(gs)} min={gs[0]:.1f} med={gs[len(gs)//2]:.1f} max={gs[-1]:.1f}")

swc = B.get("swapcut-clean", {}).get("usd", 0.0)
det = B.get("detect/funnel", {}).get("usd", 0.0)
fee = B.get("slot1-fee", {}).get("usd", 0.0) + B.get("slot1-tie", {}).get("usd", 0.0)
late = B.get("late-increment", {}).get("usd", 0.0)
struct = B.get("structural", {}).get("usd", 0.0)
print(f"\n--- вердикт (доли от won-конкурентами = ${won_usd:,.0f}) ---")
print(f"  SWAPCUT-clean (чистая, STATE шаг 2): {swc/won_usd*100:>4.0f}%  (${swc:,.0f})")
print(f"  ДЕТЕКТ/funnel:                      {det/won_usd*100:>4.0f}%  (${det:,.0f})")
print(f"  late-increment (co-loc):            {late/won_usd*100:>4.0f}%  (${late:,.0f})")
print(f"  Слот-1 fee/тай (обложен Base):      {fee/won_usd*100:>4.0f}%  (${fee:,.0f})  low-value")
print(f"  STRUCTURAL (ПОТЕРЯНО):              {struct/won_usd*100:>4.0f}%  (${struct:,.0f})  <- не addressable")
bl = {b: B[b]["usd"] for b in BUILD if b in B}
if bl:
    top = max(bl, key=bl.get)
    print(f"  -> доминирующий BUILD-рычаг: {top} — {LEVER[top]}")

if reasons:
    print("\nno-show по причинам (НЕ авто-возможность — проверить):")
    for rn, v in sorted(reasons.items(), key=lambda kv: -kv[1]["usd"]):
        print(f"  {rn:<14}{v['n']:>3}  ${v['usd']:,.0f}")
if bad:
    print(f"\n[!] {len(bad)} битых строк:")
    for i, msg in bad[:10]:
        print(f"  стр{i}: {msg}")
