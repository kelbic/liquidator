-- ============================================================================
-- Morpho Blue liquidation scanner — BASE
-- Output: one row per market with the stats that parameterize liq_model.Config
--         (opportunity frequency, sizes, exact bonus, competition tax, saturation).
-- ============================================================================
-- VERIFY BEFORE RUNNING (this was written without live Dune access — the LOGIC is
-- the point; swap names to match Dune's current schema):
--   1) Decoded namespace. In Dune's data explorer search "morpho" + base.
--      Assumed:  morpho_blue_base.MorphoBlue_evt_Liquidate  /  _evt_CreateMarket
--      Columns map by MEANING even if the table prefix differs.
--   2) Liquidate event (Morpho Blue) columns used: id, caller, borrower,
--      repaidAssets (loan-token units), seizedAssets (collateral-token units).
--   3) Price grain: uses prices.usd minute grain. On DuneSQL you may need
--      prices.minute / prices.day; join on (blockchain, contract_address, minute).
--   4) Tip column: prefers transactions.priority_fee_per_gas; else
--      gas_price - blocks.base_fee_per_gas. Keep whichever your schema exposes.
-- ============================================================================

WITH params AS (
    SELECT INTERVAL '180' DAY AS lookback
),

-- 1) Market census: id -> tokens + LLTV, plus the EXACT bonus (LIF - 1).
--    Morpho:  LIF = min(1.15, 1 / (1 - 0.3*(1 - LLTV))).  lltv stored as WAD (1e18).
markets AS (
    SELECT
        cm.id                                                       AS market_id,
        cm.loanToken                                                AS loan_token,
        cm.collateralToken                                          AS collateral_token,
        cm.lltv / 1e18                                              AS lltv,
        LEAST(1.15, 1.0 / (1.0 - 0.3 * (1.0 - cm.lltv / 1e18))) - 1.0 AS bonus
    FROM morpho_blue_base.MorphoBlue_evt_CreateMarket cm
),

-- 2) Liquidations in window + the tx tip (the Base "bribe" = priority fee to sequencer).
liqs AS (
    SELECT
        l.id                                                        AS market_id,
        l.evt_block_time                                            AS ts,
        l.evt_tx_hash                                               AS tx_hash,
        l.caller                                                    AS liquidator,
        l.repaidAssets                                              AS repaid_raw,
        l.seizedAssets                                              AS seized_raw,
        t.gas_used                                                  AS gas_used,
        COALESCE(t.priority_fee_per_gas,
                 t.gas_price - b.base_fee_per_gas)                  AS tip_per_gas
    FROM morpho_blue_base.MorphoBlue_evt_Liquidate l
    JOIN base.transactions t ON t.hash         = l.evt_tx_hash
    LEFT JOIN base.blocks  b ON b.number       = t.block_number
    CROSS JOIN params p
    WHERE l.evt_block_time > now() - p.lookback
),

-- 3) USD enrichment (loan + collateral + ETH for the tip).
priced AS (
    SELECT
        q.market_id, q.ts, q.liquidator,
        m.collateral_token, m.bonus, m.lltv,
        (q.repaid_raw / power(10, pl.decimals)) * pl.price          AS repaid_usd,
        (q.seized_raw / power(10, pc.decimals)) * pc.price          AS seized_usd,
        (q.tip_per_gas * q.gas_used / 1e18) * peth.price            AS tip_usd
    FROM liqs q
    JOIN markets m  ON m.market_id = q.market_id
    JOIN prices.usd pl   ON pl.blockchain='base' AND pl.contract_address=m.loan_token
                        AND pl.minute = date_trunc('minute', q.ts)
    JOIN prices.usd pc   ON pc.blockchain='base' AND pc.contract_address=m.collateral_token
                        AND pc.minute = date_trunc('minute', q.ts)
    JOIN prices.usd peth ON peth.blockchain='base' AND peth.symbol='WETH'
                        AND peth.minute = date_trunc('minute', q.ts)
),

-- 4) Add per-liquidator share for the concentration metric.
withshare AS (
    SELECT p.*,
        count(*) OVER (PARTITION BY market_id, liquidator) * 1.0
          / count(*) OVER (PARTITION BY market_id)                  AS liq_share
    FROM priced p
)

-- 5) Per-market table -> feeds liq_model.Config.
SELECT
    market_id,
    collateral_token,
    round(avg(bonus) * 100, 2)                                      AS lif_bonus_pct,     -- exact (from LLTV)
    count(*)                                                        AS n_liqs,            -- frequency
    count(distinct liquidator)                                      AS n_liquidators,
    round(max(liq_share) * 100, 1)                                  AS top1_share_pct,    -- competition concentration
    round(approx_percentile(repaid_usd, 0.5), 0)                   AS median_repaid_usd, -- size
    round(sum(repaid_usd), 0)                                       AS total_repaid_usd,
    round(avg((seized_usd - repaid_usd)
              / nullif(repaid_usd,0)) * 100, 2)                     AS realized_gross_pct,-- vs lif_bonus_pct => slippage/oracle-lag drag
    round(avg(tip_usd
              / nullif(seized_usd - repaid_usd, 0)) * 100, 1)       AS tip_as_pct_of_bonus,-- the competition tax => bribe_frac
    round(approx_percentile(tip_usd, 0.5), 2)                      AS median_tip_usd,
    round(avg((seized_usd - repaid_usd - tip_usd)
              / nullif(repaid_usd,0)) * 100, 2)                     AS realized_net_pct   -- saturation signal (what survives)
FROM withshare
GROUP BY market_id, collateral_token
HAVING count(*) >= 5
ORDER BY total_repaid_usd DESC
