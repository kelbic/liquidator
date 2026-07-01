"""Сравнивает 40 покрытых рынков (covered_markets.json) с market_id, упомянутыми в
feed_to_market.json (карта фидов, которые отслеживает bare-match) — напрямую по market_id, без
резолва оракулов через RPC. Если рынок покрыт (candidate-fetch его видит), но его market_id нигде
не встречается в feed_to_market.json — bare-match не узнает транзит по этому рынку никогда,
независимо от того, что происходит дальше в воронке (read_positions и т.д. до него не дойдёт).
Read-only, чистое сравнение множеств, RPC не требуется.
Запуск: cd /root/liquidator && /root/liquidator/venv/bin/python -m analysis.oracle_coverage_check
"""
import sys, json
sys.path.insert(0, ".")
from config import Config
from strategy.scanner import load_covered_markets

cfg = Config.from_env()
markets = load_covered_markets(cfg.covered_markets_path)
covered_ids = {m.market_id.lower() for m in markets}

with open("feed_to_market.json") as f:
    f2m = json.load(f)

tracked_ids = set()
pair_by_id = {}
for feed_addr, info in f2m.items():
    for mid in info.get("markets", []):
        mid_l = mid.lower()
        tracked_ids.add(mid_l)
        pair_by_id[mid_l] = info.get("pair", "?")

missing = covered_ids - tracked_ids
print(f"Покрыто рынков: {len(covered_ids)}")
print(f"Market_id, встречающихся в feed_to_market.json (23 фида): {len(tracked_ids)}")
print(f"Покрытых, но НЕ отслеживаемых bare-match'ем: {len(missing)}\n")

by_id_full = {m.market_id.lower(): m for m in markets}
for mid in sorted(missing):
    m = by_id_full[mid]
    print(f"  {mid[:12]}...  coll={m.collateral_token[:12]}...  loan={m.loan_token[:12]}...")

print(f"\nЕсли uniBTC/USDC (или его market_id) в этом списке — вот и ответ, почему bare-match")
print(f"молчал на нём в 18:47: рынок покрыт API-опросом кандидатов, но не подписан на фид,")
print(f"который транслирует его цену. Это разрыв РАНЬШЕ read_positions, отдельный от A/B.")
