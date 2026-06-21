"""
Liquidation strategy — unit-economics Monte Carlo
Target venue: Morpho Blue (Ethereum / Base), long-tail isolated markets.

Why Morpho for the tail:
  - 100% of the Liquidation Incentive Factor (LIF) goes to the liquidator (protocol takes 0).
  - Built-in free flash loan in the singleton -> ~0 capital, no flash-loan fee.
  - Close factor = 1 -> repay 100% of debt in one tx.
  - Permissionless isolated markets -> many thin markets the big bots ignore.
LIF: ~1.05 (5%) on high-LLTV blue-chip markets, up to 1.15 (15%) on low-LLTV exotic ones.

This model does NOT conjure profit. It turns YOUR measured parameters
(opportunity frequency, sizes, win-rate vs competition, slippage on the seized
collateral) into an annual PnL distribution. Replace every value in Config with
numbers you measure on-chain (see the measurement notes that accompany this file).
"""
from dataclasses import dataclass
import numpy as np


@dataclass
class Config:
    # --- opportunity arrival ---
    baseline_opps_per_week: float = 3.0   # reachable tail liquidations in calm markets
    crash_days_per_year: float = 6.0      # number of genuine high-vol days per year
    crash_opps_per_day: float = 15.0      # opportunities clustered on one crash day

    # --- size of each opportunity: debt repaid, USD (lognormal) ---
    size_median_usd: float = 8000.0
    size_sigma: float = 1.1               # lognormal shape; higher = fatter tail of big ones

    # --- bonus = LIF - 1 (normal, clipped to [1%, 15%]) ---
    bonus_mean: float = 0.07
    bonus_std: float = 0.02

    # --- swap slippage selling seized collateral (fraction of seized value) ---
    slippage_mean: float = 0.008          # 0.8% on reasonably liquid collateral
    slippage_std: float = 0.006
    slippage_crash_mult: float = 2.5      # collateral is harder to dump in a crash

    # --- competition (the crux) ---
    win_prob_baseline: float = 0.5        # thin markets: you actually land it
    win_prob_crash: float = 0.15          # crash: everyone is hunting, you lose most races
    bribe_frac_baseline: float = 0.20     # share of residual paid to the builder to win
    bribe_frac_crash: float = 0.70

    # --- costs ---
    gas_usd_baseline: float = 0.5         # Base L2; on L1 use ~15-40 (and ~100-300 in crash)
    gas_usd_crash: float = 3.0
    flashloan_fee: float = 0.0            # Morpho built-in flash loan = 0

    n_years: int = 50000
    seed: int = 7


def _net_per_won(debt, bonus, slippage, gas, bribe_frac, flashloan_fee):
    """Net USD profit on liquidations you actually win."""
    gross_bonus = debt * bonus                     # the incentive you capture
    seized_value = debt * (1.0 + bonus)            # collateral you must sell
    swap_cost = seized_value * slippage
    fl_cost = debt * flashloan_fee
    residual = gross_bonus - swap_cost - gas - fl_cost
    bribe = np.where(residual > 0, bribe_frac * residual, 0.0)  # pay to win
    return residual - bribe


def _leg(rng, n, mu, sigma, cfg, slip_mean, slip_std, gas_usd, win_prob, bribe_frac):
    if n <= 0:
        return 0.0
    debt = rng.lognormal(mu, sigma, n)
    bonus = np.clip(rng.normal(cfg.bonus_mean, cfg.bonus_std, n), 0.01, 0.15)
    slip = np.clip(rng.normal(slip_mean, slip_std, n), 0.0005, None)
    gas = np.full(n, gas_usd)
    won = rng.random(n) < win_prob
    net = _net_per_won(debt, bonus, slip, gas, bribe_frac, cfg.flashloan_fee)
    return float(np.sum(net[won]))


def simulate(cfg: Config):
    rng = np.random.default_rng(cfg.seed)
    mu = np.log(cfg.size_median_usd)  # median of lognormal = exp(mu)

    annual = np.zeros(cfg.n_years)
    annual_base = np.zeros(cfg.n_years)
    annual_crash = np.zeros(cfg.n_years)

    for y in range(cfg.n_years):
        n_base = rng.poisson(cfg.baseline_opps_per_week * 52)
        n_crash_days = rng.poisson(cfg.crash_days_per_year)
        n_crash = rng.poisson(cfg.crash_opps_per_day * n_crash_days) if n_crash_days > 0 else 0

        pnl_base = _leg(rng, n_base, mu, cfg.size_sigma, cfg,
                        cfg.slippage_mean, cfg.slippage_std, cfg.gas_usd_baseline,
                        cfg.win_prob_baseline, cfg.bribe_frac_baseline)
        pnl_crash = _leg(rng, n_crash, mu, cfg.size_sigma, cfg,
                         cfg.slippage_mean * cfg.slippage_crash_mult,
                         cfg.slippage_std * cfg.slippage_crash_mult, cfg.gas_usd_crash,
                         cfg.win_prob_crash, cfg.bribe_frac_crash)

        annual_base[y] = pnl_base
        annual_crash[y] = pnl_crash
        annual[y] = pnl_base + pnl_crash

    return annual, annual_base, annual_crash


def summary(annual, annual_base, annual_crash):
    p = lambda a, q: np.percentile(a, q)
    print(f"  P10 / P50 / P90 annual PnL  : ${p(annual,10):,.0f} / ${p(annual,50):,.0f} / ${p(annual,90):,.0f}")
    print(f"  Mean annual PnL             : ${annual.mean():,.0f}")
    print(f"  Share of years net-negative : {100*np.mean(annual<0):.1f}%")
    print(f"  Median  baseline / crash    : ${p(annual_base,50):,.0f} / ${p(annual_crash,50):,.0f}")
    print(f"  Mean    baseline / crash    : ${annual_base.mean():,.0f} / ${annual_crash.mean():,.0f}")


if __name__ == "__main__":
    cfg = Config()
    annual, ab, ac = simulate(cfg)
    print("=== BASE CASE (ILLUSTRATIVE defaults — replace with measured values) ===")
    summary(annual, ab, ac)

    print("\n=== Sensitivity: baseline win-probability (the make-or-break number) ===")
    for wp in [0.2, 0.35, 0.5, 0.7, 0.9]:
        a, _, _ = simulate(Config(win_prob_baseline=wp))
        print(f"  win_prob_baseline={wp:<4} ->  P50=${np.percentile(a,50):>9,.0f}   mean=${a.mean():>9,.0f}")

    print("\n=== Sensitivity: median opportunity size (USD debt repaid) ===")
    for sz in [3000, 8000, 20000, 50000]:
        a, _, _ = simulate(Config(size_median_usd=sz))
        print(f"  size_median=${sz:<6} ->  P50=${np.percentile(a,50):>9,.0f}   mean=${a.mean():>9,.0f}")

    print("\n=== Sensitivity: bribe fraction in calm markets (competition intensity) ===")
    for bf in [0.1, 0.3, 0.5, 0.8]:
        a, _, _ = simulate(Config(bribe_frac_baseline=bf))
        print(f"  bribe_frac_baseline={bf:<4} ->  P50=${np.percentile(a,50):>9,.0f}   mean=${a.mean():>9,.0f}")
