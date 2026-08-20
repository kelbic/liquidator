"""#138: РАСПРЕДЕЛЕНИЕ ЛИКВИДАЦИЙ ПО РАЗМЕРУ (Base Morpho, ~30 суток) — вход гейта из ЗАМЕРА.

ЗАЧЕМ. `SPEC_FIRE_MIN_REPAID_USD` = $500k отсекает кандидатов ДО всякой работы, и решение
опустить порог принимается не «на глаз», а по распределению: сколько событий и сколько ВАЛОВОГО
приза живёт в каждом классе размера, и сколько кандидатов каждый порог добавит в hotset.
Канон «вход гейта — от замеренного распределения, а не от плана».

ИСТОЧНИК — ЦЕПЬ, не API: события `Liquidate` Morpho Blue, чанки по 10 000 блоков (предел
публичного узла). API Morpho здесь не годится ещё и потому, что нам нужен `transactionIndex`
(прокси «покупки головы блока»), которого в нём нет.

ЧТО СЧИТАЕТСЯ ПО КАЖДОМУ БАКЕТУ:
  * число событий (за окно и в пересчёте на месяц);
  * суммарный ВАЛОВЫЙ приз = repaid_usd × бонус рынка (LIF−1 по lltv) — это верхняя граница
    того, что вообще можно было взять, ДО газа и слиппажа;
  * доля событий, взятых с ГОЛОВЫ блока (transactionIndex <= 1) — дешёвый прокси «стоячего
    конкурента»: он не реагирует на транзит, он покупает голову. Прокси НАЗВАН прокси.

ПРЕДЕЛЫ (названы, а не спрятаны):
  * цена займа в USD: стейблы приняты за $1.00, остальные оценены квотером ПРЯМЫХ роутов на
    ГОЛОВЕ цепи (не на момент события) — для классификации по бакетам этого достаточно, для
    точной выручки нет;
  * рынок без прямого роута займа = «не оценён», его объём печатается ОТДЕЛЬНОЙ строкой, а не
    растворяется в бакетах;
  * «покрытые рынки» берутся из feed_to_market.json репозитория (боевой covered_markets.json —
    артефакт машины, в дереве его нет).

ЗАПУСК: RPC_URL=https://mainnet.base.org python3 analysis/liq_class_histogram.py [дней] [--all]
"""
import json
import os
import pathlib
import sys
import time
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests
from eth_abi import encode as abi_encode
from eth_utils import keccak

RPC = os.environ.get("RPC_URL", "https://mainnet.base.org")
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
ONLY_COVERED = "--all" not in sys.argv
CHUNK = 10_000
BLOCK_SEC = 2.0
T_LIQ = "0x" + keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)").hex()
BUCKETS = [(0, 50), (50, 200), (200, 500), (500, 1000), (1000, 5000),
           (5000, 50_000), (50_000, float("inf"))]
S = requests.Session()


def rpc(m, p, tries=8):
    last = None
    for _ in range(tries):
        r = S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                   timeout=180).json()
        if "error" not in r:
            return r["result"]
        last = r["error"]
        time.sleep(1.5)                    # публичный узел лимитирует темп — ждём, а не врём
    raise RuntimeError("%s: %s" % (m, last))


def call(to, data, block="latest"):
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": data}, block])[2:])


def sel(sig):
    return "0x" + keccak(text=sig)[:4].hex()


def bucket_of(usd):
    for lo, hi in BUCKETS:
        if lo <= usd < hi:
            return (lo, hi)
    return BUCKETS[-1]


def fmt(lo, hi):
    return "$%s-%s" % (int(lo), "∞" if hi == float("inf") else int(hi))


# --- 1. СБОР СОБЫТИЙ -----------------------------------------------------------------------
head = int(rpc("eth_blockNumber", []), 16)
span = int(DAYS * 86400 / BLOCK_SEC)
start = head - span
print("окно: блоки %d..%d (~%d суток, %d чанков по %d)" % (start, head, DAYS,
                                                           span // CHUNK + 1, CHUNK))
events = []
b = start
t0 = time.time()
while b <= head:
    to = min(b + CHUNK - 1, head)
    got = rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to), "address": MORPHO,
                               "topics": [T_LIQ]}])
    for lg in got:
        d = bytes.fromhex(lg["data"][2:])
        events.append({
            "mid": lg["topics"][1],
            "borrower": "0x" + lg["topics"][3][-40:],
            "repaid": int.from_bytes(d[0:32], "big"),
            "seized": int.from_bytes(d[64:96], "big"),
            "bad_debt": int.from_bytes(d[96:128], "big"),
            "idx": int(lg["transactionIndex"], 16),
            "block": int(lg["blockNumber"], 16),
        })
    b = to + 1
    if (b - start) % (CHUNK * 20) == 0:
        print("   ...%d/%d блоков, событий %d (%.0fс)" % (b - start, span, len(events),
                                                          time.time() - t0))
print("собрано событий: %d за %.0fс" % (len(events), time.time() - t0))

# --- 2. ПАРАМЕТРЫ РЫНКОВ -------------------------------------------------------------------
from chain.execute import _SWAP_ROUTES, _UNIV3_USDC, _pack_path      # noqa: E402
from chain.route_depth import AERO_QUOTER, UNIV3_QUOTER, lif_bonus_pct  # noqa: E402

STABLES = {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6, 1.0),
           "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": ("EURC", 6, 1.08),
           "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC", 6, 1.0)}
_mp_cache, _px_cache = {}, {}


def market_params(mid):
    if mid not in _mp_cache:
        d = call(MORPHO, sel("idToMarketParams(bytes32)") + mid[2:])
        _mp_cache[mid] = ("0x" + d[12:32].hex(), "0x" + d[44:64].hex(),
                          int.from_bytes(d[128:160], "big"))     # loan, coll, lltv
    return _mp_cache[mid]


def token_meta(tok):
    """(символ, decimals, цена USD | None). None = не оценён (нет прямого роута)."""
    t = tok.lower()
    if t in _px_cache:
        return _px_cache[t]
    if t in STABLES:
        _px_cache[t] = STABLES[t]
        return _px_cache[t]
    try:
        dec = int.from_bytes(call(tok, sel("decimals()")), "big")
    except Exception:
        dec = 18
    px = None
    r = _SWAP_ROUTES.get((t, _UNIV3_USDC.lower()))
    if r is not None:
        q = AERO_QUOTER if r["style"] == "v1" else UNIV3_QUOTER
        one = 10 ** dec
        try:
            out = call(q, "0x" + (keccak(text="quoteExactInput(bytes,uint256)")[:4]
                                  + abi_encode(["bytes", "uint256"], [r["path"], one])).hex())
            px = int.from_bytes(out[:32], "big") / 1e6
        except Exception:
            px = None
    _px_cache[t] = (t[:10], dec, px)
    return _px_cache[t]


COVERED = set()
try:
    _f2m = json.load(open(pathlib.Path(__file__).resolve().parent.parent / "feed_to_market.json"))
    for _v in _f2m.values():
        for _m in (_v.get("markets") or []):
            COVERED.add(_m.lower())
except Exception as e:
    print("feed_to_market.json не прочитан (%s) — считаем ВСЕ рынки Base" % e)
print("покрытых рынков в карте: %d; режим: %s" % (len(COVERED),
                                                  "только покрытые" if ONLY_COVERED else "все Base"))

# --- 3. ГИСТОГРАММА ------------------------------------------------------------------------
hist = defaultdict(lambda: {"n": 0, "gross": 0.0, "head": 0, "borrowers": set(), "repaid": 0.0})
unpriced = {"n": 0, "mids": set()}
skipped_market = 0
for e in events:
    mid = e["mid"].lower()
    if ONLY_COVERED and COVERED and mid not in COVERED:
        skipped_market += 1
        continue
    loan, _coll, lltv = market_params(e["mid"])
    _sym, dec, px = token_meta(loan)
    if px is None:
        unpriced["n"] += 1
        unpriced["mids"].add(mid[:12])
        continue
    usd = e["repaid"] / (10 ** dec) * px
    bonus = lif_bonus_pct(lltv / 1e18) / 100.0
    k = bucket_of(usd)
    h = hist[k]
    h["n"] += 1
    h["repaid"] += usd
    h["gross"] += usd * bonus
    h["head"] += 1 if e["idx"] <= 1 else 0
    h["borrowers"].add(e["borrower"])

scale = 30.0 / DAYS                        # приведение к «в месяц»
print("\n%s" % ("=" * 104))
print("ГИСТОГРАММА ЛИКВИДАЦИЙ ПО РАЗМЕРУ ЗАЙМА (repaid), окно %d суток, %s"
      % (DAYS, "покрытые рынки" if ONLY_COVERED else "весь Base"))
print("=" * 104)
print("%-14s %8s %10s %14s %14s %10s %10s" % ("бакет", "событий", "в месяц", "repaid, $",
                                              "gross, $/мес", "с головы", "заёмщиков"))
tot_n = tot_gross = 0
for lo, hi in BUCKETS:
    h = hist.get((lo, hi))
    if not h:
        print("%-14s %8d %10.1f %14s %14s %10s %10s" % (fmt(lo, hi), 0, 0, "-", "-", "-", "-"))
        continue
    tot_n += h["n"]
    tot_gross += h["gross"] * scale
    print("%-14s %8d %10.1f %14.0f %14.0f %9.0f%% %10d"
          % (fmt(lo, hi), h["n"], h["n"] * scale, h["repaid"], h["gross"] * scale,
             100.0 * h["head"] / h["n"], len(h["borrowers"])))
print("-" * 104)
print("ИТОГО: %d событий (%.0f/мес), валовый приз %.0f $/мес" % (tot_n, tot_n * scale, tot_gross))
if unpriced["n"]:
    print("НЕ ОЦЕНЕНО (нет прямого роута займа): %d событий, рынки %s"
          % (unpriced["n"], ", ".join(sorted(unpriced["mids"])[:6])))
if skipped_market:
    print("вне покрытых рынков (не считались): %d событий" % skipped_market)

# --- 4. ЦЕНА ПОДГОТОВКИ И РЕКОМЕНДАЦИЯ -----------------------------------------------------
# Цена окна: пробы флота на полу + прогрев. Числа боевые: газ пробы 54 710 (замер STATE:652),
# пол tip 1.0 mgwei, ~1200 совпадающих проб окна, ETH $1730 -> см. execute._mirror_cost_usd.
PROBE_GAS, TIP_FLOOR, PROBES, ETH = 54_710, 1_000_000, 1200, 1730.0
WIN_RATE = float(os.environ.get("LIQ_HIST_WIN_RATE", "0.10"))   # реалистичная доля побед
win_cost = PROBES * TIP_FLOOR * PROBE_GAS * ETH / 1e18
print("\nЦЕНА ПОДГОТОВКИ ОКНА (пробы на полу): %.4f $ (%d проб × %d wei × %d газа при ETH $%d)"
      % (win_cost, PROBES, TIP_FLOOR, PROBE_GAS, ETH))
print("порог обязан пускать класс, где gross/мес × win-rate %.0f%% > цены подготовки" % (WIN_RATE * 100))
print("\n%-14s %12s %12s %14s" % ("порог", "целей/мес", "gross/мес $", "ожидание $/мес"))
cum_n = cum_gross = 0.0
cum_by_thr = {}
for lo, hi in reversed(BUCKETS):
    h = hist.get((lo, hi))
    if h:
        cum_n += h["n"] * scale
        cum_gross += h["gross"] * scale
    cum_by_thr[lo] = (cum_n, cum_gross)
    print("%-14s %12.1f %12.0f %14.1f" % ("от $%d" % lo, cum_n, cum_gross, cum_gross * WIN_RATE))

# --- 5. СНИМОК ЖИВЫХ ПОЗИЦИЙ: сколько КАНДИДАТОВ добавит каждый порог ----------------------
# Поток ликвидаций отвечает «сколько денег в классе», но не «сколько целей придётся держать в
# hotset». Второе — свойство СТОЯЩЕЙ популяции, и его берём снимком.
# ИСТОЧНИК И ЕГО ПРЕДЕЛЫ: Morpho GraphQL (индексатор, НЕ цепь) — цена в USD его собственная, лаг
# индексации неизвестен, позиции без оценки (null) считаются отдельно. Для вопроса «порядок
# величины кандидатов» этого достаточно; HF на экзотике по-прежнему читаем только с цепи.
try:
    from analysis.competition_report import _gql
    _PQ = """query($first:Int!,$skip:Int!,$cid:[Int!],$mk:[String!]){
      marketPositions(first:$first,skip:$skip,
                      where:{chainId_in:$cid,marketUniqueKey_in:$mk,borrowShares_gte:1},
                      orderBy:BorrowShares,orderDirection:Desc){
        items{ market{ marketId } state{ borrowAssetsUsd } } } }"""
    mk_list = sorted(COVERED) if COVERED else []
    pos_hist = defaultdict(int)
    pos_null = 0
    skip_i = 0
    while mk_list and skip_i <= 5000:
        r = _gql(_PQ, {"first": 100, "skip": skip_i, "cid": [8453], "mk": mk_list})
        items = (((r.get("data") or {}).get("marketPositions") or {}).get("items")) or []
        if not items:
            break
        for it in items:
            v = ((it.get("state") or {}).get("borrowAssetsUsd"))
            if v is None:
                pos_null += 1
                continue
            pos_hist[bucket_of(float(v))] += 1
        skip_i += 100
    tot_pos = sum(pos_hist.values())
    print("\nСНИМОК ЖИВЫХ ПОЗИЦИЙ С ДОЛГОМ (Morpho API, покрытые рынки): %d позиций"
          "%s" % (tot_pos, (", без оценки USD: %d" % pos_null) if pos_null else ""))
    print("%-14s %10s %14s %16s" % ("бакет", "позиций", "кандидатов", "к hotset от порога"))
    _cum = 0
    for lo, hi in reversed(BUCKETS):
        _cum += pos_hist.get((lo, hi), 0)
        print("%-14s %10d %14s %16d" % (fmt(lo, hi), pos_hist.get((lo, hi), 0), "", _cum))
except Exception as e:                                       # noqa: BLE001
    print("\nснимок позиций не получен (%s) — рекомендация опирается только на поток событий" % e)
