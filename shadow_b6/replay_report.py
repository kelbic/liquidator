"""Отчёт реплея: recall по 104 событиям, разбор промахов на «вселенная» и «математика»,
ложные выстрелы контрольного набора по порогам."""
import json, os, sys, collections
HERE = os.path.dirname(os.path.abspath(__file__))
D = f"{HERE}/data"
det = json.load(open(f"{D}/replay_detect.json"))
uni = json.load(open(f"{D}/universe_replay.json"))
M = json.load(open(f"{HERE}/markets_tracked.json"))
b6 = json.load(open(f"{D}/b6_events.json"))

# --- полнота ---
hit = miss_uni = miss_math = 0
rows = []
miss_rows = []
act_by_key = {}
for r in det:
    named = {x["user"].lower() for x in r["named"]}
    dec = int(M[r["mid"]]["loan_dec"])
    for a in r["actual"]:
        u = a["user"].lower()
        key = (r["mid"], u, r["b_rel"])
        if key in act_by_key:
            continue
        act_by_key[key] = True
        in_uni = u in {x.lower() for x in uni.get(r["mid"], [])}
        if u in named:
            hit += 1
            rows.append((a["usd"] or 0, "hit", r["pair"], r["b_rel"], u))
        elif not in_uni:
            miss_uni += 1
            miss_rows.append((a["usd"] or 0, "ПРОПУСК ВСЕЛЕННОЙ", r["pair"], r["b_rel"], u))
        else:
            miss_math += 1
            miss_rows.append((a["usd"] or 0, "ПРОПУСК МАТЕМАТИКИ", r["pair"], r["b_rel"], u))
tot = hit + miss_uni + miss_math
print(f"=== ПОЛНОТА (M1) на призовых блоках B+6 ===")
print(f"событий (уникальных рынок+заёмщик+блок): {tot}")
print(f"  НАЗВАНО             : {hit}  = {hit/tot*100:.1f}%")
print(f"  пропуск ВСЕЛЕННОЙ   : {miss_uni}  = {miss_uni/tot*100:.1f}%  (заёмщика нет в списке; живая тень строит вселенную полностью)")
print(f"  пропуск МАТЕМАТИКИ  : {miss_math} = {miss_math/tot*100:.1f}%")
usd_hit = sum(u for u, k, *_ in rows if k == "hit")
usd_miss = sum(u for u, *_ in miss_rows)
print(f"  ПО ДЕНЬГАМ: названо ${usd_hit:,.0f} из ${usd_hit+usd_miss:,.0f} = {usd_hit/(usd_hit+usd_miss)*100:.2f}%")
by_size = collections.Counter()
for u, k, *_ in rows + [(x[0], "miss") + tuple(x[2:]) for x in miss_rows]:
    b = "<100" if u < 100 else ("100-1k" if u < 1000 else ">1k")
    by_size[(b, k if k == "hit" else "miss")] += 1
print("  по размеру:", {k: v for k, v in sorted(by_size.items())})
if miss_rows:
    print("\n  промахи (топ по $):")
    for u, k, pair, b, usr in sorted(miss_rows, reverse=True)[:12]:
        print(f"    ${u:>9.0f} {k:>20} {pair:>13} blk {b} {usr[:12]}")

# --- ложные срабатывания в detect-проходе (назвали, но никто не ликвидировал) ---
print("\n=== НАЗВАНО, НО НЕ ЛИКВИДИРОВАНО (в тех же призовых блоках) ===")
fp = []
for r in det:
    act = {a["user"].lower() for a in r["actual"]}
    dec = int(M[r["mid"]]["loan_dec"])
    for x in r["named"]:
        if x["user"].lower() not in act:
            fp.append((x["gross_usd"], r["pair"], r["b_rel"], x["repay_usd"]))
print(f"  таких названных: {len(fp)}; из них бонус >= $20: {sum(1 for g,*_ in fp if g>=20)}; >= $100: {sum(1 for g,*_ in fp if g>=100)}")
for g, pair, b, rep in sorted(fp, reverse=True)[:8]:
    print(f"    бонус ${g:>8.0f} погашение ${rep:>9.0f} {pair:>13} blk {b}")

# --- контроль: раунды без ликвидаций ---
cp = f"{D}/replay_control.json"
if os.path.exists(cp):
    ctl = json.load(open(cp))
    n_named = [(x["gross_usd"], r["pair"], r["b_rel"]) for r in ctl for x in r["named"]]
    print(f"\n=== КОНТРОЛЬНЫЙ НАБОР (раунды 15.09 без ликвидации в B+6) ===")
    print(f"  оценок (раунд,рынок): {len(ctl)}; названо позиций: {len(n_named)}")
    for thr in (0, 20, 100, 1000):
        k = sum(1 for g, *_ in n_named if g >= thr)
        print(f"    ложных выстрелов с бонусом >= ${thr:>5}: {k}")
    if n_named:
        print("    топ ложных по бонусу:", [(round(g, 2), p) for g, p, _b in sorted(n_named, reverse=True)[:5]])
# --- модель цены ---
errs = [abs(r.get("price_model_err") or 0) for r in det if r.get("price_model_err") is not None]
print(f"\n=== МОДЕЛЬ ЦЕНЫ (позитивный контроль) ===")
print(f"  сверок с фактическим oracle.price() на B_rel: {len(errs)}; max |ошибка|: {max(errs) if errs else 'нет'}")
