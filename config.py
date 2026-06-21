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
    mode: str = "monitor"
    covered_markets_path: str = "covered_markets.json"
    min_profit_usd: float = 5.0
    max_daily_loss_usd: float = 50.0
    max_daily_gas_usd: float = 100.0
    max_inflight: int = 1
    tg_bot_token: str = ""
    tg_admin_id: int = 0
    alert_antiflood_sec: int = 600
    poll_interval_sec: float = 2.0
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
            mode=s("MODE", "monitor"),
            covered_markets_path=s("COVERED_MARKETS_PATH", "covered_markets.json"),
            min_profit_usd=f("MIN_PROFIT_USD", 5.0),
            max_daily_loss_usd=f("MAX_DAILY_LOSS_USD", 50.0),
            max_daily_gas_usd=f("MAX_DAILY_GAS_USD", 100.0),
            max_inflight=i("MAX_INFLIGHT", 1),
            tg_bot_token=s("TG_BOT_TOKEN"),
            tg_admin_id=i("TG_ADMIN_ID", 0),
            alert_antiflood_sec=i("ALERT_ANTIFLOOD_SEC", 600),
            poll_interval_sec=f("POLL_INTERVAL_SEC", 2.0),
            db_path=s("DB_PATH", "liquidator.db"),
            log_level=s("LOG_LEVEL", "INFO"),
        )
