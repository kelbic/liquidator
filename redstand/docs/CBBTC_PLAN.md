# CBBTC_PLAN — точный план добавления cbBTC/USDC (готов к исполнению любой моделью/сессией)

> Составлен 2026-07-09 после полного préflight на Charlotte. Все числа — измерены, не оценены.
> Исполнять по шагам; money-path — соблюдать дисциплину проверок (py_compile/import/AST/symtable/smoke)
> и СТАДИЙНЫЙ деплой с замером+откатом. НЕ включать в один заход с другими изменениями.

## ПОДТВЕРЖДЁННЫЕ ФАКТЫ (préflight 07-09, chain+API)
- **mid** = `0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836` (cbBTC/USDC, lltv 0.86, bonus 4.38%)
- **borrow** = $1.23B (крупнейший рынок протокола; STATE-цифра «$69M» устарела). >11000 активных заёмщиков.
- **oracle-обёртка** = `0x663becd10dae6c4a3dcd89f1d76c1174199639b9`; BASE_FEED_1 = `0x64c911996d3c6ac71f9b455b1e8e7266bcbd848f`;
  underlying aggregator (resolve_feeds найдёт) = `0x0e3dc8a6a86d2f6f5f67b373a047c267fb1fc3e6`; SCALE_FACTOR = 1e26.
- **SHADOW SCALE = 1e26 — ПОДТВЕРЖДЁН БИТ-В-БИТ**: `answer(8-dec) * 1e26 == oracle.price()`, остаток 0. coll 8-dec (как cbDOGE/cbLTC).
- **NEAR-RISK (сустейн)**: HF≤1.10 И debt≥$450 = **25 позиций**, HF≤1.05 = 0. Кэш слоя 2 переварит тривиально.
- **МАРШРУТ**: cbBTC→WETH(500)→USDC(500) UniV3 SwapRouter02. Квота QuoterV2: $2k→$2009, $30k→$30103 (~0.3% impact). Fee 500 чуть лучше 3000.
- **Ликвидность 30д**: 482 ликв / $15.8M, ≥$450: 204, ≥$2k: 143 (≈7 призов/день ≥$450). Контест: 58 ботов.
- **БЛОКЕР**: текущий `_fetch_borrower_positions` тянет всех заёмщиков merged и падает на API-лимите skip≤10000.
  Morpho API НЕ поддерживает orderBy:HealthFactor (400). Решение — bounded per-market fetch top-N по долгу.

## ПРАВКА 1 — chain/morpho.py: bounded fetch для мега-рынков (КРИТИЧНАЯ, без неё ломается refresh)
Рядом с `MIN_DEBT_USD` добавить:
```python
MEGA_MARKETS = frozenset({"0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836"})  # cbBTC/USDC: 11000+ заёмщиков -> bounded per-market fetch
MEGA_FETCH_MAX_PAGES = 3   # top-3000 по долгу; near-risk найдены в top-3000 (измерено 07-09). Log при упоре в кап.
```
Добавить функцию (рядом с `_fetch_borrower_positions`):
```python
def _fetch_market_bounded(key, api_url, max_pages=MEGA_FETCH_MAX_PAGES, page=1000, timeout=15):
    """Bounded fetch ОДНОГО мега-рынка: top (max_pages*page) заёмщиков по долгу (BorrowShares desc),
    стоп на supplier-хвосте (borrowShares==0). Не пагинирует за API-лимит skip<=10000. near-risk
    концентрируется в высоких рангах долга -> кап ловит призы >=$450 (измерено: 25 near-risk в top-3000)."""
    out, skip = [], 0
    for _ in range(max_pages):
        batch = _query_page([key], skip, page, api_url, timeout)
        for it in batch:
            if _int((it.get("state") or {}).get("borrowShares")) == 0:
                return out
            out.append(it)
        if len(batch) < page:
            break
        skip += page
    return out
```
Заменить тело `positions_at_risk` (партиция normal/mega, merge):
```python
def positions_at_risk(markets, hf_ceiling=1.0, min_debt_usd=MIN_DEBT_USD, api_url=MORPHO_API_URL):
    keys = [m.market_id for m in markets if m.market_id]
    if not keys:
        return []
    lltv_by_key = {m.market_id: m.lltv for m in markets if m.market_id}
    mega_lc = {m.lower() for m in MEGA_MARKETS}
    normal = [k for k in keys if k.lower() not in mega_lc]
    mega = [k for k in keys if k.lower() in mega_lc]
    items = _fetch_borrower_positions(normal, api_url) if normal else []
    for mk in mega:
        items += _fetch_market_bounded(mk, api_url)
    return parse_positions({"data": {"marketPositions": {"items": items}}}, lltv_by_key, hf_ceiling, min_debt_usd)
```

## ПРАВКА 2 — analysis/build_covered_markets.py: INCLUDE_MARKETS (переживает 6ч rescan)
Рядом с `MAX_BORROW_USD`:
```python
INCLUDE_MARKETS = frozenset({"0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836"})  # cbBTC/USDC: >30M, но достижим после срезки латентности (07-09)
```
В `select_markets`, заменить границу borrow:
```python
        included = (key or "").lower() in {m.lower() for m in INCLUDE_MARKETS}
        if not included and (borrow < min_borrow_usd or borrow > max_borrow_usd):
            continue
```
(`key` уже вычислен строкой ниже — ПЕРЕНЕСТИ `key = it.get("marketId")` ВЫШЕ проверки borrow, иначе NameError.)
Перед `return rows[:max_markets]` гарантировать включение:
```python
    selected = rows[:max_markets]
    inc_lc = {m.lower() for m in INCLUDE_MARKETS}
    have = {m["market_id"].lower() for m in selected}
    for m in rows:
        if m["market_id"].lower() in inc_lc and m["market_id"].lower() not in have:
            selected.append(m); have.add(m["market_id"].lower())
    return selected
```

## ПРАВКА 3 — chain/execute.py: маршрут cbBTC/USDC
В словарь `_SWAP_ROUTES` (или сразу после его литерала) добавить (`_CBBTC`, `_UNIV3_USDC`, `_UNIV3_WETH`, `UNIV3_SWAP_ROUTER`, `_pack_path` уже определены):
```python
_SWAP_ROUTES[(_CBBTC, _UNIV3_USDC)] = {"router": UNIV3_SWAP_ROUTER, "style": "02",
                                        "path": _pack_path(_CBBTC, 500, _UNIV3_WETH, 500, _UNIV3_USDC)}
```
Следствие: cbBTC входит в `ROUTE_COLLATERALS` -> кэш слоя 2 обновляет его каждый блок (ОК, 25 near-risk).

## ПРАВКА 4 — chain/hotpath.py: shadow scale
В `_SHADOW_SCALE` добавить `"cbBTC": 10**26` (coll 8-dec; SCALE_FACTOR=1e26 подтверждён бит-в-бит 07-09).

## ПРОВЕРКИ (перед деплоем)
- `python3 -m py_compile chain/morpho.py analysis/build_covered_markets.py chain/execute.py chain/hotpath.py`
- runtime-import под venv (web3): `import chain.morpho, chain.execute, chain.hotpath, analysis.build_covered_markets`
- AST-затенение + symtable free-names на всех 4 файлах (шаблон из docs/WORKFLOW.md).
- smoke: (a) `_SWAP_ROUTES[(cbBTC_lc, usdc_lc)]` даёт router=SwapRouter02, path содержит cbBTC|000064|WETH|000064|USDC;
  (b) `positions_at_risk` с mega-mid не падает и не пагинирует >3 стр (мок `_query_page`);
  (c) `select_markets` с borrow>30M и mid в INCLUDE -> рынок в выдаче;
  (d) `_SHADOW_SCALE["cbBTC"]==10**26`.

## ДЕПЛОЙ (стадийный, с замером и откатом)
1. Применить 4 правки, прогнать проверки, commit+push (Вена origin), bundle+scp на Charlotte.
2. Регенерить covered_markets.json НА Charlotte:
   `cd /root/liquidator && env $(grep -vE '^#|^WALLET_KEY=|^$' .env | xargs) ./venv/bin/python -m analysis.build_covered_markets covered_markets.json 50000 30000000 41`
   Проверить: cbBTC/USDC присутствует (`grep 9103c3b4 covered_markets.json`).
3. `systemctl restart liquidator-bot`; журнал: warm-start, 0 ошибок, `hot path: N aggregators / N markets` (+cbBTC).
4. ЗАМЕР (в течение ~10 мин):
   - `DIAG candidate refresh n=` вырос на ~25 (не сломался: не должно быть 'candidate refresh error' / API skip>10000).
   - `DIAG position-cache cycle markets=6 dt=` — должно остаться <1.0с (было 0.6-0.8с на 5).
   - `DIAG shadow-feed mkt=cbBTC ... match=True` при первом транзите — LIVE-подтверждение scale (как cbLTC).
   - Первый cbBTC transmit: `src=cache/preconf`, read= тайминг.
5. WATCH (первые сутки): первый `HOT ready` на cbBTC -> `submit-xray` дельта (выигрываем ли против 58 ботов);
   серии `liquidate revert` (одиночные норма; при fast-lane серия 3+ -> ревертов газ; kill-switch $60/$100 бэкстоп).

## ОТКАТ
- Быстрый: убрать cbBTC-строку из covered_markets.json на Charlotte + restart (rescan через <6ч вернёт её из INCLUDE —
  поэтому для стойкого отката убрать mid из `INCLUDE_MARKETS` в коде, redeploy).
- Полный: `git revert <commit>` + redeploy + regen covered_markets.json.

## РИСКИ / ГЕЙТЫ
- **Контест 58 ботов** -> много ревертов. Гейт: kill-switch $60 loss/$100 gas день (≈25-50 ревертов при 6 gwei). Не трогать.
- **shadow scale live**: если `match=False` при первом транзите — НЕМЕДЛЕННО убрать `"cbBTC"` из `_SHADOW_SCALE`
  (poll-путь на живой price() всё равно работает, безопасен), разобрать. (Ожидание: match=True, scale подтверждён бит-в-бит.)
- **bounded fetch cap=3000**: может пропустить near-risk глубже top-3000. Лог при упоре в кап; при пропуске призов — поднять MEGA_FETCH_MAX_PAGES.
- **fast-lane на cbBTC**: призы ≥$1000 идут через fast-lane (без sim). Scale подтверждён -> цена верна -> ложных выстрелов нет. Дедуп (357b73e) защищает от двойной отправки.
