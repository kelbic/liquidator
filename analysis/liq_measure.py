"""
Bridge: Dune (Base / Morpho Blue) measured stats -> liq_model.Config -> PnL.

Workflow:
  1) Run morpho_base_liq_scan.sql on Dune, save it, note the query_id.
  2) Either fetch_dune(query_id, DUNE_API_KEY)  OR  export CSV and pd.read_csv it.
  3) markets_to_config(df, lookback_weeks) -> a Config built from MEASURED numbers.
  4) simulate(cfg) -> annual PnL distribution.

markets_to_config is pure pandas/numpy and is unit-tested at the bottom with
SYNTHETIC data (NOT real Base numbers) so you can verify the mapping offline.
"""
import os
from dataclasses import replace

import numpy as np
import pandas as pd

from liq_model import Config, simulate, summary


# ---- 1. Fetch from Dune (needs API access; not runnable in a sandbox) ----
def fetch_dune(query_id: int, api_key: str | None = None) -> pd.DataFrame:
    from dune_client.client import DuneClient          # pip install dune-client
    client = DuneClient(api_key or os.environ["DUNE_API_KEY"])
    res = client.get_latest_result(query_id)
    return pd.DataFrame(res.result.rows)


# ---- 2. Heuristic newcomer win-probability (NOT a measurement — a proxy) ----
def _win_prob_heuristic(top1_share_pct, realized_net_pct, lif_bonus_pct):
    """Fragmented winners + fat surviving bonus => contestable (high win_prob).
    One dominant address + thin surviving bonus => saturated PGA you'll lose (low)."""
    frag = 1.0 - top1_share_pct / 100.0                       # 0 = a whale owns it, 1 = fragmented
    keep = np.clip(realized_net_pct / max(lif_bonus_pct, 1e-9), 0, 1)  # bonus surviving competition
    return float(np.clip(0.15 + 0.6 * frag * keep, 0.05, 0.9))


# ---- 3. Measured per-market table -> model Config (PURE / testable) ----
def markets_to_config(df: pd.DataFrame, lookback_weeks: float,
                      min_liqs: int = 5,
                      min_median_usd: float = 1500,
                      min_realized_net_pct: float = 2.0,
                      base_config: Config = Config()):
    d = df.copy()
    d["n_liqs"] = d["n_liqs"].astype(int)
    # how much of the theoretical bonus is lost to slippage / oracle-lag on the seized collateral
    d["slippage_drag_pct"] = d["lif_bonus_pct"] - d["realized_gross_pct"]

    # coverable universe: enough activity, size worth the gas, collateral actually exitable,
    # and not so saturated that the competition tax leaves nothing.
    cover = d[(d["n_liqs"] >= min_liqs)
              & (d["median_repaid_usd"] >= min_median_usd)
              & (d["realized_net_pct"] >= min_realized_net_pct)].copy()
    if cover.empty:
        raise ValueError("No coverable markets after filters — loosen thresholds or widen lookback.")

    cover["win_prob"] = [
        _win_prob_heuristic(r.top1_share_pct, r.realized_net_pct, r.lif_bonus_pct)
        for r in cover.itertuples()
    ]

    n = cover["n_liqs"].to_numpy()
    tot = n.sum()
    wavg = lambda col: float((cover[col].to_numpy() * n).sum() / tot)

    cfg = replace(
        base_config,
        baseline_opps_per_week=tot / lookback_weeks,
        size_median_usd=float(np.median(np.repeat(cover["median_repaid_usd"].to_numpy(), n))),
        bonus_mean=wavg("lif_bonus_pct") / 100.0,
        slippage_mean=float(np.clip(wavg("slippage_drag_pct") / 100.0, 0.0005, None)),
        win_prob_baseline=wavg("win_prob"),
        bribe_frac_baseline=float(np.clip(wavg("tip_as_pct_of_bonus") / 100.0, 0, 1)),
    )
    return cfg, cover.sort_values("total_repaid_usd", ascending=False)


if __name__ == "__main__":
    # SYNTHETIC self-test (NOT real Base data) — proves the pipeline end to end.
    synthetic = pd.DataFrame([
        # thin niche, fat surviving margin (the target)
        dict(market_id="0xNICHE", collateral_token="0xtok1", lif_bonus_pct=9.0, n_liqs=40,
             n_liquidators=3, top1_share_pct=45, median_repaid_usd=6000, total_repaid_usd=260000,
             realized_gross_pct=8.4, tip_as_pct_of_bonus=12, median_tip_usd=6.0, realized_net_pct=7.9),
        # saturated blue-chip: big size & volume but tip eats the bonus -> excluded by filter
        dict(market_id="0xBLUE", collateral_token="0xtok2", lif_bonus_pct=5.0, n_liqs=300,
             n_liquidators=14, top1_share_pct=72, median_repaid_usd=42000, total_repaid_usd=1.4e7,
             realized_gross_pct=4.8, tip_as_pct_of_bonus=78, median_tip_usd=410, realized_net_pct=1.1),
        # mid niche, ok margin (kept)
        dict(market_id="0xMID", collateral_token="0xtok3", lif_bonus_pct=7.0, n_liqs=80,
             n_liquidators=6, top1_share_pct=55, median_repaid_usd=12000, total_repaid_usd=1.1e6,
             realized_gross_pct=6.2, tip_as_pct_of_bonus=35, median_tip_usd=70, realized_net_pct=5.0),
    ])

    cfg, cover = markets_to_config(synthetic, lookback_weeks=26)
    print("Coverable markets (after filters, sorted by volume):")
    print(cover[["market_id", "lif_bonus_pct", "n_liqs", "top1_share_pct", "median_repaid_usd",
                 "tip_as_pct_of_bonus", "realized_net_pct", "win_prob"]].to_string(index=False))

    print("\nConfig derived from MEASURED stats:")
    for k in ["baseline_opps_per_week", "size_median_usd", "bonus_mean",
              "slippage_mean", "win_prob_baseline", "bribe_frac_baseline"]:
        print(f"  {k:24} = {getattr(cfg, k):.4f}")

    print("\nPnL with measured params:")
    annual, ab, ac = simulate(cfg)
    summary(annual, ab, ac)
