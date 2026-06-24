"""Environment-driven config. Import is side-effect-free; call Config.from_env().
Пустые env-значения (KEY= в .env) трактуются как «не задано» -> дефолт."""
from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass
class Config:
    rpc_url: str = ""
    chain: str = "base"
    chain_id: int = 8453
    wallet_address: str = ""
    wallet_key: str = ""                 # private key; used ONLY when MODE=execute
    liquidator_address: str = ""         # deployed Liquidator contract
    mode: str = "monitor"
    covered_markets_path: str = "covered_markets.json"
    rescan_interval_sec: float = 21600.0
    exclude_oracles: str = ""
    min_profit_usd: float = 5.0          # floor on NET (profit - gas/tip cost) before sending
    tip_gwei: float = 3.0                # competitive priority fee we bid (beats observed ~2.0 cap)
    gas_limit_est: int = 500000          # ~gas used per liquidation, for the net-cost estimate
    eth_price_usd: float = 1730.0        # ETH/USD to value gas/tip cost (keep ~current; tip dominates)
    max_daily_loss_usd: float = 50.0
    max_daily_gas_usd: float = 100.0
    max_inflight: int = 5            # cap on parallel sends per block (cascade)
    tg_bot_token: str = ""
    tg_admin_id: int = 0
    alert_antiflood_sec: int = 600
    poll_interval_sec: float = 2.0
    loop_mode: str = "poll"
    block_source: str = "flashblocks"   # block trigger: flashblocks (~2s earlier) | newheads (fallback)
    candidate_refresh_sec: float = 60.0
    hot_path: bool = False                # preconf-triggered hot path ON/OFF (OFF until ARM)
    hot_min_repaid_usd: float = 450.0    # hot path fires only on reaction prizes >= this (repaid USD)
    preconf_rpc: str = "https://mainnet-preconf.base.org"   # pre-confirmed price source for the hot path
    db_path: str = "liquidator.db"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        g = os.environ.get

        def s(key, default=""):
            v = g(key)
            return v if v not in (None, "") else default

        def i(key, default):
            v = g(key)
            return int(v) if v not in (None, "") else int(default)

        def f(key, default):
            v = g(key)
            return float(v) if v not in (None, "") else float(default)

        return cls(
            rpc_url=s("RPC_URL"),
            chain=s("CHAIN", "base"),
            chain_id=i("CHAIN_ID", 8453),
            wallet_address=s("WALLET_ADDRESS"),
            wallet_key=s("WALLET_KEY"),
            liquidator_address=s("LIQUIDATOR_ADDRESS"),
            mode=s("MODE", "monitor"),
            covered_markets_path=s("COVERED_MARKETS_PATH", "covered_markets.json"),
            rescan_interval_sec=f("RESCAN_INTERVAL_SEC", 21600.0),
            exclude_oracles=s("EXCLUDE_ORACLES"),
            min_profit_usd=f("MIN_PROFIT_USD", 5.0),
            tip_gwei=f("TIP_GWEI", 3.0),
            gas_limit_est=i("GAS_LIMIT_EST", 500000),
            eth_price_usd=f("ETH_PRICE_USD", 1730.0),
            max_daily_loss_usd=f("MAX_DAILY_LOSS_USD", 50.0),
            max_daily_gas_usd=f("MAX_DAILY_GAS_USD", 100.0),
            max_inflight=i("MAX_INFLIGHT", 5),
            tg_bot_token=s("TG_BOT_TOKEN"),
            tg_admin_id=i("TG_ADMIN_ID", 0),
            alert_antiflood_sec=i("ALERT_ANTIFLOOD_SEC", 600),
            poll_interval_sec=f("POLL_INTERVAL_SEC", 2.0),
            loop_mode=s("LOOP_MODE", "poll"),
            block_source=s("BLOCK_SOURCE", "flashblocks"),
            candidate_refresh_sec=f("CANDIDATE_REFRESH_SEC", 60.0),
            hot_path=(s("HOT_PATH", "").lower() in ("1", "true", "yes", "on")),
            hot_min_repaid_usd=f("HOT_MIN_REPAID_USD", 450.0),
            preconf_rpc=s("PRECONF_RPC", "https://mainnet-preconf.base.org"),
            db_path=s("DB_PATH", "liquidator.db"),
            log_level=s("LOG_LEVEL", "INFO"),
        )
