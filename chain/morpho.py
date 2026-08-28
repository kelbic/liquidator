"""Find Morpho Blue positions at risk (HF <= ceiling) on Base.

Phase 1 (monitor): enumeration via the Morpho GraphQL API — read-only, stdlib HTTP,
no on-chain calls, no extra deps. HF here is the USD approximation
    HF = collateralUsd * lltv / borrowAssetsUsd
enough to FLAG candidates; the precise oracle HF + revert/profit gate lives in
chain/simulate (next step). For execute later this scan moves on-chain (lower
latency, no API rate-limit dependency — the API itself warns against hard deps on it).

lltv is taken from the Market objects (immutable, from the Dune scan), not from the
API, so the position query only needs `marketId` to map a position back to its lltv.
"""
from __future__ import annotations
import json
import logging
import os
import urllib.request
import urllib.error
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

from strategy.scanner import Market

# Morpho Blue singleton — same deterministic CREATE2 address on every chain incl. Base.
# Verify on BaseScan before execute. Used by the on-chain leg (simulate, Phase 2).
MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

MORPHO_API_URL = "https://api.morpho.org/graphql"

# Floor: позиции с долгом в USD ниже этого — пыль: невыгодны всем (бонус < газ+флор) и
# Morpho ревертит 0x81ceff30 на ~нулевом долге. Режем на стадии кандидата, чтобы они не
# засоряли hot set / simulations. (Бэклог STATE; в Config не тянем — хватает дефолта модуля.)
MIN_DEBT_USD = float(os.environ.get("MIN_DEBT_USD", "1.0"))   # 28.08: порог в ИСТОЧНИКЕ кандидатов; тренировка снимает через env

# Мега-рынки (CBBTC_PLAN 07-09): >10000 заёмщиков -> merged-пагинация упирается в API-лимит
# skip<=10000 и РОНЯЕТ весь candidate refresh; orderBy:HealthFactor у API нет (400). Такие рынки
# фетчатся ОТДЕЛЬНО, bounded: top-N по долгу (BorrowShares desc). near-risk концентрируется в
# высоких рангах долга: на cbBTC/USDC ВСЕ 25 near-risk (HF<=1.10, debt>=$450) в top-3000 (замер).
MEGA_MARKETS = frozenset({
    "0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836",   # cbBTC/USDC $1.23B
    # 27.07: WETH/USDC $70.2M — тысячи заёмщиков. В обычной merged-пагинации он увёл skip за
    # API-кап 10000 и УРОНИЛ весь candidate refresh, а после страховочного капа съедал ~75
    # кандидатов у ОСТАЛЬНЫХ рынков (усечение по хвосту). Как мега: bounded top-3000 по долгу
    # + серверный HF-фильтр — и чужие рынки снова видны целиком.
    "0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda",   # WETH/USDC $70.2M
    # 28.07: cbXRP/USDC $34.6M, 965 позиций HF<=1.15 (ритейл-толпа) — тот же класс, что WETH:
    # без bounded top-3000 + серверного HF-фильтра merged-пагинация уводит skip за кап 10000.
    "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109",   # cbXRP/USDC $34.6M
})
MEGA_FETCH_MAX_PAGES = 3   # top-3000 по долгу; при упоре в кап пишется предупреждение в лог
# Жёсткий предел пагинации Morpho API: skip<=10000. 27.07 добавление WETH/USDC ($70.2M, тысячи
# заёмщиков) увело пагинацию за него -> BAD_USER_INPUT -> исключение УРОНИЛО candidate refresh
# целиком (набор жил только на warm-start-снапшоте). Позиции идут по borrowShares desc, т.е.
# усечение режет САМЫЕ МЕЛКИЕ долги — терпимо; падение refresh'а — нет.
API_SKIP_MAX = 10000

# HF-ДОПОЛНЕНИЕ к bounded-fetch (07-10, measured-переоткрытие по образцу cbBTC): серверный
# where healthFactor_lte РАБОТАЕТ (CBBTC_PLAN пробовал только orderBy: HealthFactor -> 400).
# Закрывает ранговую слепоту капа: top-3000 режет по ДОЛГУ (rank-3000 = $82.5k на 07-10) и был бы
# НАВСЕГДА слеп к near-risk ниже капа И за API-skip-лимитом 10000 (замер 07-10: в рангах 3001-10000
# ноль позиций HF<=1.10, но 57 с HF<=1.12 и 186 с HF<=1.15 при долгах $11k-82k — в BTC-краш они
# уходят под 1.0 невидимыми). Порог = клиентский потолок 1.10 (main.HF_API_CEILING) + запас 0.05
# на расхождение server-HF (оракул) vs USD-approx (loan=USDC -> в норме <1%). Объём 07-10:
# hf<=1.15 = 1368 строк (1.20 -> 3196, 1.25 -> 4898). Ordered by BorrowShares desc -> упор в
# кап/бюджет режет ПЫЛЬ, не китов. Сбой добавки НЕ роняет refresh (try/except в positions_at_risk).
# Серверного пола по долгу НЕТ (borrowAssetsUsd_gte -> 400, проверено) — пол клиентский (parse).
# 1.15 -> 1.20 (04.08, потолок клиента поднят до 1.15 — запас +0.05 сохраняем; замер 07-10:
# 1.20 = 3196 строк, потому и страниц 3 -> 4, чтобы кап не резал систематически)
MEGA_HF_FILTER = float(os.environ.get("MEGA_HF_FILTER", "1.20"))
MEGA_HF_MAX_PAGES = int(os.environ.get("MEGA_HF_MAX_PAGES", "4"))
MEGA_HF_BUDGET_SEC = 8.0   # мягкий бюджет добавки: API-затык не должен раздувать refresh-цикл

# marketUniqueKey is keccak(loanToken,collateralToken,oracle,irm,lltv); token/oracle
# addresses differ per chain, so Base keys can't collide with other chains -> no chainId
# filter needed. `market { marketId }` is the position's market id (== the key we filter
# by). TODO: paginate via skip if a market ever exceeds `first`.
_POSITIONS_QUERY = """
query Positions($keys: [String!], $first: Int!, $skip: Int!) {
  marketPositions(first: $first, skip: $skip, orderBy: BorrowShares, orderDirection: Desc,
                  where: { marketUniqueKey_in: $keys }) {
    items {
      user { address }
      market { marketId }
      healthFactor
      state { collateral collateralUsd borrowAssets borrowAssetsUsd borrowShares }
    }
  }
}
"""

# Тот же запрос + серверный HF-фильтр (только для MEGA_MARKETS — см. MEGA_HF_FILTER выше).
_POSITIONS_HF_QUERY = _POSITIONS_QUERY.replace(
    "where: { marketUniqueKey_in: $keys }",
    "where: { marketUniqueKey_in: $keys, healthFactor_lte: %.2f }" % MEGA_HF_FILTER)
assert _POSITIONS_HF_QUERY != _POSITIONS_QUERY, "HF-query: where-анкор не найден (изменился _POSITIONS_QUERY?)"


@dataclass
class Position:
    market_id: str
    borrower: str
    health_factor: float
    debt_assets: int
    collateral_assets: int
    debt_usd: float = 0.0
    collateral_usd: float = 0.0


def _num(x, default=0.0) -> float:
    return float(x) if x not in (None, "") else float(default)


def _int(x, default=0) -> int:
    return int(x) if x not in (None, "") else int(default)


def health_factor(collateral_usd: float, borrow_usd: float, lltv: float) -> float:
    """USD-approx HF. >1 healthy, <=1 liquidatable. No debt -> +inf."""
    if borrow_usd <= 0:
        return float("inf")
    return collateral_usd * lltv / borrow_usd


def parse_positions(payload: dict, lltv_by_key: dict, hf_ceiling: float = 1.0, min_debt_usd: float = MIN_DEBT_USD) -> list[Position]:
    """marketPositions payload -> at-risk Positions (HF <= ceiling), most-at-risk first.
    lltv_by_key maps marketId -> lltv (fraction). Pure: unit-tested on synthetic data."""
    if payload.get("errors"):
        raise RuntimeError(f"Morpho API errors: {payload['errors']}")
    items = (((payload.get("data") or {}).get("marketPositions") or {}).get("items")) or []
    out: list[Position] = []
    for it in items:
        st = it.get("state") or {}
        market_id = ((it.get("market") or {}).get("marketId")) or ""
        lltv = lltv_by_key.get(market_id, 0.0)
        if lltv <= 0:
            continue  # lltv не передан для этого рынка -> не оцениваем (безопасно)
        borrow_usd = _num(st.get("borrowAssetsUsd"))
        if borrow_usd < min_debt_usd:
            continue  # пыль/нулевой долг (< MIN_DEBT_USD): невыгодно + ревертит 0x81ceff30; не тащим
        collateral_usd = _num(st.get("collateralUsd"))
        if collateral_usd < borrow_usd:
            continue  # залог < долга -> безнадёжный долг (изъятие не покрывает погашение); не наш
        hf = health_factor(collateral_usd, borrow_usd, lltv)
        # 31.07: УНИЯ двух оценок. USD-деривация зависит от того, КАК API ценит экзотику в
        # долларах: ночью 30->31.07 API сменил прайсинг msETH (маркет вместо фундаментала,
        # долг -18%) -> деривация прыгнула 1.04->1.25 через потолок 1.10, и кластер wstETH/msETH
        # с ON-CHAIN HF 1.006-1.015 ВЫПАЛ из кандидатов (реактивный путь ослеп на главном
        # фронте). Поле healthFactor — независимая оценка того же индексера; берём кандидата,
        # если ХОТЬ ОДНА <= потолка: недовключение = слепота к цели, перевключение бесплатно
        # (боевой ассессмент on-chain всё равно решает). Храним минимум (ближайшую к флипу).
        hf_api = _num(it.get("healthFactor"))
        if hf <= hf_ceiling or (0 < hf_api <= hf_ceiling):
            out.append(Position(
                market_id=market_id,
                borrower=((it.get("user") or {}).get("address") or ""),
                health_factor=min(hf, hf_api) if hf_api > 0 else hf,
                debt_assets=_int(st.get("borrowAssets")),
                collateral_assets=_int(st.get("collateral")),
                debt_usd=borrow_usd,
                collateral_usd=_num(st.get("collateralUsd")),
            ))
    out.sort(key=lambda p: p.health_factor)
    return out


_RETRYABLE_HTTP = (429, 500, 502, 503, 504)


def _query_page(keys: list[str], skip: int, first: int, api_url: str, timeout: int,
                retries: int = 3, query: str = _POSITIONS_QUERY) -> list:
    """One page of marketPositions (ordered by borrowShares desc; query подменяем для
    HF-фильтрованной формы — тело/ретраи общие). I/O with retry-on-transient
    (timeout / URLError / ConnectionError / 429 / 5xx) so a single API hiccup doesn't drop a whole
    scan cycle. Non-transient (HTTP 4xx, GraphQL errors) propagate immediately.
    ConnectionError (вкл. ConnectionResetError [Errno 104] — обрыв TLS в ssl.read, инцидент 07-09)
    — подкласс OSError, НЕ URLError, и urllib пробрасывает его голым мимо URLError-ветки; ловим
    явно. Запрос идемпотентен (read-only GraphQL) -> повтор безопасен."""
    body = json.dumps({"query": query,
                       "variables": {"keys": keys, "first": first, "skip": skip}}).encode()
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(api_url, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
            if payload.get("errors"):
                raise RuntimeError(f"Morpho API errors: {payload['errors']}")
            return (((payload.get("data") or {}).get("marketPositions") or {}).get("items")) or []
        except urllib.error.HTTPError as e:
            if e.code not in _RETRYABLE_HTTP or attempt == retries - 1:
                raise
            last_exc = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == retries - 1:
                raise
            last_exc = e
        time.sleep(0.5 * (2 ** attempt))
    raise last_exc


def _fetch_borrower_positions(keys: list[str], api_url: str, page: int = 1000,
                              max_pages: int = 20, timeout: int = 15,
                              cycle_deadline: float = 30.0) -> list:
    """All BORROWER positions across markets, no truncation. Because positions are ordered
    by borrowShares desc, suppliers (borrowShares==0) sort last -> stop at the first zero.
    Single first:1000 would have capped at one big market; this paginates all of them.
    cycle_deadline caps TOTAL wall-time: a bad API can stack timeout*retry across pages into
    minutes (measured: 5 hangs >60s/3d, max 1767s = 30min blind hot-set). On deadline, return
    what we have (first pages = largest debts) rather than block the refresh thread for minutes."""
    out: list = []
    skip = 0
    _t0 = time.monotonic()
    for _ in range(max_pages):
        if time.monotonic() - _t0 > cycle_deadline:
            break                               # budget exhausted -> return partial (biggest debts first)
        if skip > API_SKIP_MAX:                 # 27.07: жёсткий предел Morpho API (skip<=10000).
            log.warning("borrower-positions: упёрлись в API-кап skip>%d (набор УСЕЧЁН по %d строк; "
                        "добавлен крупный рынок?) — дальше идут мелкие долги", API_SKIP_MAX, skip)
            break                               # иначе BAD_USER_INPUT рушил ВЕСЬ candidate refresh
        batch = _query_page(keys, skip, page, api_url, timeout)
        for it in batch:
            if _int((it.get("state") or {}).get("borrowShares")) == 0:
                return out                      # reached the supplier tail -> done
            out.append(it)
        if len(batch) < page:
            break
        skip += page
    return out


def _fetch_market_bounded(key: str, api_url: str, max_pages: int = MEGA_FETCH_MAX_PAGES,
                          page: int = 1000, timeout: int = 15) -> list:
    """Bounded fetch ОДНОГО мега-рынка (CBBTC_PLAN): top (max_pages*page) заёмщиков по долгу
    (BorrowShares desc), стоп на supplier-хвосте (borrowShares==0). НЕ пагинирует к API-лимиту
    skip<=10000. Кап может отрезать глубокий хвост мелких долгов — near-risk с призом живут в
    верхних рангах (замер 07-09: 25/25 в top-3000); упор в кап логируется как watch-сигнал."""
    out: list = []
    skip = 0
    for _ in range(max_pages):
        batch = _query_page([key], skip, page, api_url, timeout)
        for it in batch:
            if _int((it.get("state") or {}).get("borrowShares")) == 0:
                return out
            out.append(it)
        if len(batch) < page:
            return out
        skip += page
    log.info("DIAG mega-fetch cap market=%s top-%d исчерпан (заёмщиков больше) — хвост отрезан",
             key[:10], max_pages * page)
    return out


def _fetch_market_hf_filtered(key: str, api_url: str, max_pages: int = MEGA_HF_MAX_PAGES,
                              page: int = 1000, timeout: int = 15,
                              budget_sec: float = MEGA_HF_BUDGET_SEC) -> list:
    """HF-дополнение ОДНОГО мега-рынка: ВСЕ позиции с server-HF<=MEGA_HF_FILTER, БЕЗ ранговой
    слепоты (where-фильтр видит и ранги за API-skip-лимитом 10000, недоступные bounded-пути).
    Ordered by BorrowShares desc -> упор в кап/бюджет теряет пыль, не китов. Пол по долгу —
    клиентский (parse_positions): серверного нет (400)."""
    out: list = []
    skip = 0
    _t0 = time.monotonic()
    for _ in range(max_pages):
        batch = _query_page([key], skip, page, api_url, timeout, query=_POSITIONS_HF_QUERY)
        for it in batch:
            if _int((it.get("state") or {}).get("borrowShares")) == 0:
                return out
            out.append(it)
        if len(batch) < page:
            return out
        skip += page
        if time.monotonic() - _t0 > budget_sec:
            log.info("DIAG mega-HF budget market=%s %.0fs исчерпан на %d строках — хвост (пыль) отрезан",
                     key[:10], budget_sec, len(out))
            return out
    log.info("DIAG mega-HF cap market=%s top-%d исчерпан — хвост (пыль) отрезан",
             key[:10], max_pages * page)
    return out


def positions_at_risk(markets: list[Market], hf_ceiling: float = 1.0, min_debt_usd: float = MIN_DEBT_USD,
                      api_url: str = MORPHO_API_URL) -> list[Position]:
    """At-risk positions across covered markets, via the Morpho API. Мега-рынки (MEGA_MARKETS)
    фетчатся отдельно bounded-путём — merged-пагинация с ними упиралась в API skip-лимит и
    роняла ВЕСЬ candidate refresh (CBBTC_PLAN, правка 1)."""
    keys = [m.market_id for m in markets if m.market_id]
    if not keys:
        return []
    lltv_by_key = {m.market_id: m.lltv for m in markets if m.market_id}
    mega_lc = {m.lower() for m in MEGA_MARKETS}
    normal = [k for k in keys if k.lower() not in mega_lc]
    mega = [k for k in keys if k.lower() in mega_lc]
    items = _fetch_borrower_positions(normal, api_url) if normal else []
    for mk in mega:
        bounded = _fetch_market_bounded(mk, api_url)
        items = items + bounded
        try:    # аддитивная HF-добавка: любой её сбой -> предупреждение, bounded-набор уже в items
            extra = _fetch_market_hf_filtered(mk, api_url)
        except Exception as e:
            log.warning("mega HF-supplement сбой market=%s (%s) — цикл на bounded-наборе", mk[:10], e)
            continue
        seen = {((it.get("user") or {}).get("address") or "").lower() for it in bounded}
        added = [it for it in extra if ((it.get("user") or {}).get("address") or "").lower() not in seen]
        items = items + added
        log.info("DIAG mega HF-supplement market=%s fetched=%d added=%d (мимо top-%d)",
                 mk[:10], len(extra), len(added), MEGA_FETCH_MAX_PAGES * 1000)
    return parse_positions({"data": {"marketPositions": {"items": items}}}, lltv_by_key, hf_ceiling, min_debt_usd)
