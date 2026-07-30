"""Build covered_markets.json from the Morpho API (Base) — the monitor's market set,
WITHOUT a Dune dependency.

Selection: markets with real borrowing (liquidations are possible and worth the gas) AND
a liquidation bonus worth more than slippage, bounded count to keep the scan light and
within positions_at_risk's page. Competition is NOT scored here — the monitor measures it
live (a position that vanishes before the next scan = contested; one that lingers = our
tail edge), which later informs a stricter Dune-based selection.

Run on a host that can reach api.morpho.org (the VPS); stdlib only, plain `python3`.
Usage:
    python3 analysis/build_covered_markets.py [OUT_PATH] [MIN_BORROW_USD] [MAX_MARKETS]
Defaults: OUT_PATH=covered_markets.json  MIN_BORROW_USD=50000  MAX_MARKETS=40
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MORPHO_API_URL = "https://api.morpho.org/graphql"
BASE_CHAIN_ID = 8453
DEFAULT_SLIPPAGE = 0.01   # conservative placeholder; refined per-market by monitor/Dune later
MIN_BONUS = 0.01          # drop markets whose LIF bonus <= this (uneconomical: bonus < slippage)
MAX_BORROW_USD = 30_000_000   # exclude mega-cap markets: contested (we won't win) AND huge position counts

# CBBTC_PLAN 07-09: точечное переоткрытие мега-рынков ПОСЛЕ срезки латентности (2.5-3с -> 50-100мс).
# Вердикт «contested, we won't win» был вынесен при старом пути; cbBTC/USDC = 482 ликв/$15.8M за 30д
# (204 приза >=$450). Включается ИМЕННО перечислением — НЕ снятием MAX_BORROW_USD (иначе затащим
# WETH/USDC и прочие мега-рынки без решения). Сустейн закрыт MEGA_MARKETS bounded-фетчем (morpho.py).
INCLUDE_MARKETS = frozenset({
    "0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836",   # cbBTC/USDC
    # WETH/USDC (EXOTIC_COVERAGE_SCAN, «го» владельца 27.07): $70M займа, оракул = наш же
    # ETH/USD-фид, 199 ликв/$820k repaid/90д; топ-3 по объёму молчат с начала июня. 28.07
    # рескан молча откатил ручную правку covered (потолок $30M) — решения владельца живут
    # ЗДЕСЬ, ручные правки JSON рескан не переживают.
    "0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda",   # WETH/USDC
    # cbXRP/USDC-мега («го» владельца 28.07): $34.6M займа, 8 ликвидаций/~$11.6k repaid за
    # один день, бонус 12.7%. Цена включения замерена: watch-ключ ТОТ ЖЕ, что у малого
    # cbXRP-рынка (0x92a7c3a5, уже в shadow-декоде) — hot-путь не трогаем; +965 позиций
    # HF<=1.15 в кандидат-сет (фоновый CU). ОБЯЗАН быть и в MEGA_MARKETS (morpho.py) —
    # без bounded-фетча пагинация роняет candidate refresh (инцидент WETH 27.07).
    "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109",   # cbXRP/USDC $34.6M
})
# collateral our swap venue (KyberSwap) cannot exit -> liquidation would revert; don't waste a slot.
# Pendle PT tokens trade on Pendle's own AMM, not the DEXs Kyber aggregates.
EXIT_BLOCKED_COLLATERAL = ("PT-",)

# Скам-рынки Morpho (permissionless-создание): мусорный токен под знакомым тикером, оракул-константа
# (price() без внешних зависимостей), Kyber-выхода нет. «Займ» надувается ради входа в топ-N и
# дренажа лендеров — слот покрытия им не отдаём, добычи там не бывает (залог не продать).
# 30.07: оба «RSS/USDC» = elephanToken (новый 0x7a30…, старый 0x5063…), оракулы прибиты к 1.0.
PHANTOM_MARKETS = {
    "0x40ac09f34c5bc0b0b6d9b5f1ec1b97a6a149ff6278104797c9cb740453a2b794":
        "elephanToken под тикером RSS: оракул-константа 1.0, Kyber-выхода нет",
    "0xa4ec527128b425ee3fcb7f60eca37677b63b3d003345ec2a72ef6a2e72da53fc":
        "elephanToken под тикером RSS (старый слот): оракул-константа, выхода нет",
}
_PHANTOM_LC = {k.lower(): v for k, v in PHANTOM_MARKETS.items()}

_MARKETS_QUERY = """
query Markets($chains: [Int!], $first: Int!, $skip: Int!) {
  markets(first: $first, skip: $skip, orderBy: BorrowAssetsUsd, orderDirection: Desc,
          where: { chainId_in: $chains }) {
    items {
      marketId
      oracleAddress
      lltv
      loanAsset { symbol decimals address }
      collateralAsset { symbol decimals address }
      state { borrowAssetsUsd supplyAssetsUsd }
    }
  }
}
"""


def _query(chain_id, skip, first, api_url, timeout):
    body = json.dumps({"query": _MARKETS_QUERY,
                       "variables": {"chains": [chain_id], "first": first, "skip": skip}}).encode()
    req = urllib.request.Request(api_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise RuntimeError(f"Morpho API errors: {payload['errors']}")
    return (((payload.get("data") or {}).get("markets") or {}).get("items")) or []


def fetch_markets(chain_id=BASE_CHAIN_ID, api_url=MORPHO_API_URL, page=100, max_pages=5, timeout=30):
    """Base markets ordered by borrow desc, paginated; top markets land in the first pages."""
    items, skip = [], 0
    for _ in range(max_pages):
        batch = _query(chain_id, skip, page, api_url, timeout)
        items.extend(batch)
        if len(batch) < page:
            break
        skip += page
    return items


def _lif_bonus(lltv: float) -> float:
    """Morpho LIF - 1 (the liquidation bonus), exact from lltv."""
    if not (0.0 < lltv < 1.0):
        return 0.0
    return min(1.15, 1.0 / (1.0 - 0.3 * (1.0 - lltv))) - 1.0


def _lltv_fraction(raw) -> float:
    """API lltv may be WAD (e.g. 860000000000000000) or already a fraction. Normalize."""
    v = float(raw or 0)
    return v / 1e18 if v > 100 else v


def select_markets(items, min_borrow_usd=50_000.0, max_borrow_usd=MAX_BORROW_USD, max_markets=40,
                   default_slippage=DEFAULT_SLIPPAGE, min_bonus=MIN_BONUS, exclude_oracles=None,
                   exclude_collateral_prefixes=EXIT_BLOCKED_COLLATERAL):
    """Pure: filter (real borrow, valid collateral, bonus>slippage) + map -> records.
    Carries a private _borrow_usd for inspection/sort; stripped before JSON."""
    inc_lc = {m.lower() for m in INCLUDE_MARKETS}
    rows = []
    for it in items:
        st = it.get("state") or {}
        borrow = float(st.get("borrowAssetsUsd") or 0)
        key = it.get("marketId")
        included = (key or "").lower() in inc_lc          # CBBTC_PLAN: allowlist обходит borrow-границы
        if not included and (key or "").lower() in _PHANTOM_LC:
            continue                            # скам-рынок: слот не выдаём (allowlist выше эвристик)
        if not included and (borrow < min_borrow_usd or borrow > max_borrow_usd):
            continue
        col = it.get("collateralAsset") or {}
        loan = it.get("loanAsset") or {}
        if not key or not col.get("symbol"):   # idle/unrecognized-collateral markets -> skip
            continue
        csym = col.get("symbol") or ""
        if any(csym.upper().startswith(pre.upper()) for pre in (exclude_collateral_prefixes or ())):
            continue                            # collateral our swap can't exit (e.g. Pendle PT)
        if exclude_oracles and (it.get("oracleAddress") or "").lower() in exclude_oracles:
            continue                            # SVR/OEV-recapture oracle -> bonus leaks to protocol
        lltv = _lltv_fraction(it.get("lltv"))
        bonus = _lif_bonus(lltv)
        if bonus <= min_bonus:                  # uneconomical (bonus <= slippage) -> skip
            continue
        rows.append({
            "market_id": key,
            "collateral_token": col.get("symbol") or "",
            "loan_token": loan.get("symbol") or "",
            "lltv": round(lltv, 6),
            "bonus": round(bonus, 6),
            "expected_slippage": default_slippage,
            "_borrow_usd": round(borrow, 0),
        })
    rows.sort(key=lambda m: m["_borrow_usd"], reverse=True)
    selected = rows[:max_markets]
    # CBBTC_PLAN: allowlist-рынок обязан попасть в выдачу, даже если top-N вытеснил его
    have = {m["market_id"].lower() for m in selected}
    for m in rows:
        mlc = m["market_id"].lower()
        if mlc in inc_lc and mlc not in have:
            selected.append(m)
            have.add(mlc)
    return selected


def to_json_records(selected) -> list:
    """Strip private fields -> clean Market(**rec)-compatible records."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in selected]


def _removal_reason(mid, items_by_id, exclude_oracles=None,
                    min_borrow_usd=50_000.0, max_borrow_usd=MAX_BORROW_USD,
                    phantom_reasons=None) -> str:
    """Почему select_markets не взял рынок (зеркало его фильтров, для алерта). Чистая."""
    if phantom_reasons and mid in phantom_reasons:
        return "отсеян как фантом: %s" % phantom_reasons[mid]
    if mid.lower() in _PHANTOM_LC:
        return "отсеян как фантом: %s" % _PHANTOM_LC[mid.lower()]
    it = items_by_id.get(mid.lower())
    if it is None:
        return "исчез из API"
    borrow = float(((it.get("state") or {}).get("borrowAssetsUsd")) or 0)
    if borrow > max_borrow_usd:
        return "займ $%.1fM > потолка $%.0fM" % (borrow / 1e6, max_borrow_usd / 1e6)
    if borrow < min_borrow_usd:
        return "займ $%.0fk < пола $%.0fk" % (borrow / 1e3, min_borrow_usd / 1e3)
    if exclude_oracles and (it.get("oracleAddress") or "").lower() in exclude_oracles:
        return "оракул исключён (SVR/OEV)"
    csym = ((it.get("collateralAsset") or {}).get("symbol") or "")
    if any(csym.upper().startswith(p.upper()) for p in EXIT_BLOCKED_COLLATERAL):
        return "выход заблокирован (%s)" % csym
    if _lif_bonus(_lltv_fraction(it.get("lltv"))) <= MIN_BONUS:
        return "бонус <= слиппеджа"
    return "вытеснен из топ-N (займ $%.0fk)" % (borrow / 1e3)


USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
KYBER_ROUTES_URL = "https://aggregator-api.kyberswap.com/base/api/v1/routes"
PHANTOM_RECHECK_SEC = 7 * 86400    # фантом: выход может появиться (mGLO-урок) — перепроверяем
LIVE_RECHECK_SEC = 30 * 86400      # живой рынок фантомом не становится — редкая перепроверка


def _is_constant_oracle(access_list, oracle) -> bool:
    """Pure: price() оракула-константы не читает ничьих слотов, кроме своих собственных."""
    o = (oracle or "").lower()
    return not any((e.get("address") or "").lower() != o for e in (access_list or []))


def probe_market_phantom(it, rpc_url, timeout=20):
    """-> причина-строка, если рынок фантом (оракул-константа И нет Kyber-выхода), иначе "".
    Транспортные сбои поднимаются исключениями — вызыватель решает (fail-open). Дешёвая
    последовательность: accessList (1 RPC) отсекает живые оракулы ДО похода в Kyber."""
    oracle = it.get("oracleAddress") or ""
    col = it.get("collateralAsset") or {}
    if not oracle or not col.get("address"):
        return ""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_createAccessList",
                       "params": [{"to": oracle, "data": "0xa035b1fe"}, "latest"]}).encode()
    req = urllib.request.Request(rpc_url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "curl/8.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())
    al = (res.get("result") or {}).get("accessList")
    if res.get("error") or al is None:
        raise RuntimeError("accessList: %s" % str(res.get("error"))[:60])
    if not _is_constant_oracle(al, oracle):
        return ""
    dec = int(col.get("decimals") or 18)
    qs = urllib.parse.urlencode({"tokenIn": col["address"], "tokenOut": USDC_BASE,
                                 "amountIn": str(1000 * 10 ** dec)})
    try:
        with urllib.request.urlopen(urllib.request.Request(
                KYBER_ROUTES_URL + "?" + qs, headers={"User-Agent": "curl/8.5"}),
                timeout=timeout) as r:
            d = json.loads(r.read())
        out_usd = float(((d.get("data") or {}).get("routeSummary") or {}).get("amountOutUsd") or 0)
    except urllib.error.HTTPError as e:
        if e.code == 404:          # Kyber «token not found» (mGLO-стиль) = выхода нет
            out_usd = 0.0
        else:
            raise
    if out_usd >= 1.0:
        return ""
    return ("оракул-константа (0 внешних зависимостей) + Kyber-выхода нет "
            "(out $%.2f за 1000 %s)" % (out_usd, col.get("symbol") or "?"))


def filter_phantoms(records, items, rpc_url, cache_path, log_fn=None, probe_fn=None, now=None):
    """Динамический фантом-фильтр поверх select_markets («го» kelbic 30.07: скам-рынок
    elephanToken-«RSS» надул займ $1M и вошёл в топ-N — отбор по займу геймится). Рынок с
    оракулом-константой И без Kyber-выхода слот покрытия не получает. Вердикты кэшируются в
    cache_path; сбой пробы = fail-open (рынок остаётся, вердикт не пишем). Allowlist не
    фильтруется никогда (решения владельца выше эвристик). Откат: PHANTOM_FILTER=0.
    -> (kept_records, {mid: причина}, свежепомеченные mid)."""
    if os.environ.get("PHANTOM_FILTER", "1").strip().lower() in ("0", "false", "off"):
        return records, {}, set()
    log = log_fn or (lambda _m: None)
    now = time.time() if now is None else now
    probe = probe_fn or (lambda it: probe_market_phantom(it, rpc_url))
    try:
        with open(cache_path) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    items_by_id = {(it.get("marketId") or "").lower(): it for it in items}
    inc_lc = {m.lower() for m in INCLUDE_MARKETS}
    kept, reasons, fresh, dirty = [], {}, set(), False
    for rec in records:
        mid = rec["market_id"]
        mlc = mid.lower()
        if mlc in inc_lc:
            kept.append(rec)
            continue
        ent = cache.get(mlc)
        ttl = PHANTOM_RECHECK_SEC if (ent or {}).get("phantom") else LIVE_RECHECK_SEC
        if ent is None or now - float(ent.get("ts") or 0) >= ttl:
            it = items_by_id.get(mlc)
            if it is None:
                kept.append(rec)
                continue
            try:
                why = probe(it)
            except Exception as e:  # noqa: BLE001 — транспорт: fail-open, без кэша
                log("phantom probe fail %s: %s" % (mid[:10], type(e).__name__))
                kept.append(rec)
                continue
            ent = {"phantom": bool(why), "reason": why, "ts": now}
            cache[mlc] = ent
            dirty = True
            if why:
                fresh.add(mid)
        if ent.get("phantom"):
            reasons[mid] = ent.get("reason") or "фантом"
            log("phantom filter: %s/%s %s… отсеян (%s)" % (
                rec.get("collateral_token"), rec.get("loan_token"), mid[:10], reasons[mid]))
        else:
            kept.append(rec)
    if dirty:
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(cache, fh)
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return kept, reasons, fresh


# Списанные цели: их приход/уход из покрытия — рутина, не событие (kelbic 29.07: mGLO вышел за
# потолок -> TG-алерт был спамом, у mGLO свой вотчер оживления на Вене). Лог остаётся всегда.
RESCAN_ALERT_QUIET = frozenset({"mGLO"})


def rescan_change_msg(items, old_pairs, new_pairs, added, removed, exclude_oracles=None,
                      phantom_reasons=None) -> str:
    """Текст TG-алерта об изменении covered-листа ресканом (28.07: рескан молча откатил ручную
    правку владельца — изменение листа обязано быть видимым, с причиной). Изменения по парам из
    RESCAN_ALERT_QUIET в TG не идут; пусто = не слать (лог-строка рескана есть всегда). Чистая."""
    items_by_id = {(it.get("marketId") or "").lower(): it for it in items}

    def _quiet(pair):
        return (pair or "").split("/")[0] in RESCAN_ALERT_QUIET

    parts = []
    for mid in sorted(added):
        if _quiet(new_pairs.get(mid)):
            continue
        it = items_by_id.get(mid.lower()) or {}
        borrow = float(((it.get("state") or {}).get("borrowAssetsUsd")) or 0)
        why = "allowlist" if mid.lower() in {m.lower() for m in INCLUDE_MARKETS} \
            else "займ $%.0fk, вошёл в отбор" % (borrow / 1e3)
        parts.append("+%s %s… (%s)" % (new_pairs.get(mid, "?/?"), mid[:10], why))
    for mid in sorted(removed):
        if _quiet(old_pairs.get(mid)):
            continue
        parts.append("-%s %s… (%s)" % (old_pairs.get(mid, "?/?"), mid[:10],
                                       _removal_reason(mid, items_by_id, exclude_oracles,
                                                       phantom_reasons=phantom_reasons)))
    if not parts:
        return ""
    return "\U0001F4CB rescan covered: " + " | ".join(parts)


def main(argv):
    out_path = argv[1] if len(argv) > 1 else "covered_markets.json"
    min_borrow = float(argv[2]) if len(argv) > 2 else 50_000.0
    max_borrow = float(argv[3]) if len(argv) > 3 else MAX_BORROW_USD
    max_markets = int(argv[4]) if len(argv) > 4 else 40

    items = fetch_markets()
    selected = select_markets(items, min_borrow_usd=min_borrow, max_borrow_usd=max_borrow, max_markets=max_markets)
    records = to_json_records(selected)

    print(f"Base markets fetched: {len(items)}; selected (${min_borrow:,.0f}<=borrow<=${max_borrow:,.0f}, bonus>{MIN_BONUS*100:.0f}%, top {max_markets}): {len(records)}")
    print(f"{'collateral/loan':<24}{'lltv':>7}{'bonus%':>8}{'borrow_usd':>16}")
    for m in selected:
        print(f"{(m['collateral_token']+'/'+m['loan_token']):<24}{m['lltv']:>7.3f}{m['bonus']*100:>8.2f}{m['_borrow_usd']:>16,.0f}")

    with open(out_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\nwrote {len(records)} markets -> {out_path}")


if __name__ == "__main__":
    main(sys.argv)
