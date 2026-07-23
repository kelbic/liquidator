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
    tip_gwei: float = 0.05               # Base-секвенсер = FCFS (разбор блока 49015040 23.07: победители
                                         # 0.003 gwei на idx+2 от транзита, 10 gwei — на idx 5255; тот же
                                         # вывод у WC 18.07) — tip НЕ покупает позицию, только жжёт газ:
                                         # промах-реверт при 6 gwei стоил $1.31, при 0.05 — ~$0.01, и это
                                         # открывает спекулятивный огонь. 0.05 = запас над толпой 0.003
                                         # на случай fee-сортировки ВНУТРИ флешблок-кадра (op-rbuilder).
    gas_limit_est: int = 500000          # ~gas used per liquidation, for the net-cost estimate
    eth_price_usd: float = 1730.0        # ETH/USD to value gas/tip cost (keep ~current; tip dominates)
    max_daily_loss_usd: float = 50.0
    max_daily_gas_usd: float = 100.0
    max_inflight: int = 5            # cap on parallel sends per block (cascade)
    tg_bot_token: str = ""
    tg_admin_id: int = 0
    alert_antiflood_sec: int = 600
    bot_tag: str = ""                     # префикс алертов в общем TG-чате флота ("[base] ...") — LIQ_BOT_TAG
    poll_interval_sec: float = 2.0
    loop_mode: str = "poll"
    block_source: str = "flashblocks"   # block trigger: flashblocks (~2s earlier) | newheads (fallback)
    block_wss_url: str = ""             # явный WSS для newheads-fallback (пусто = https→wss от rpc_url); BLOCK_WSS_URL
    candidate_refresh_sec: float = 60.0
    hot_path: bool = False                # preconf-triggered hot path ON/OFF (OFF until ARM)
    price_from_feed: bool = False         # Layer1: feed-decoded price triggers _process_transmit (OFF until measured)
    hot_min_repaid_usd: float = 450.0    # hot path fires only on reaction prizes >= this (repaid USD)
    positions_from_cache: bool = False    # P1-6: слой 2 потребление (OFF до калибровки cache-divergence)
    fast_lane: bool = False               # P1-9: fast-lane без sim (OFF; контракт гарантирует исход)
    fast_lane_min_repaid_usd: float = 1000.0  # fast-lane только на призах >= этого repaid USD
    fast_lane_max_consec_reverts: int = 3     # II.6 circuit breaker: N подряд ревертов fast-lane -> off ТОЛЬКО fast-lane (фолбэк sim)
    batch_liquidate: bool = False             # мультибатч: N жертв в ОДНОЙ tx (каскад). OFF -> путь бит-в-бит старый
    batch_max_items: int = 5                  # макс items в одной batch-tx (хвост -> следующими tx в пределах max_inflight)
    canary_probe: bool = False                # ⭐ canary: замер inclusion-гонки минимальной tx (OFF; ARM на кампанию)
    canary_max_probes: int = 100              # стоп кампании после N проб
    canary_min_interval_sec: float = 120.0    # не чаще 1 пробы / N сек (разброс по условиям)
    canary_max_gas_usd: float = 25.0          # СВОЙ газ-кап канарейки (НЕ в kill-switch) -> стоп кампании
    preconf_rpc: str = "https://mainnet-preconf.base.org"   # pre-confirmed price source for the hot path
    sequencer_rpc: str = "https://mainnet-sequencer.base.org"  # SEND-3: параллельный raw-broadcast; "off" = выкл
    candidates_snapshot_path: str = "candidates_snapshot.json"  # warm-start: последний candidate set (P0-1)
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
            bot_tag=s("LIQ_BOT_TAG", ""),
            poll_interval_sec=f("POLL_INTERVAL_SEC", 2.0),
            loop_mode=s("LOOP_MODE", "poll"),
            block_source=s("BLOCK_SOURCE", "flashblocks"),
            block_wss_url=s("BLOCK_WSS_URL", ""),
            candidate_refresh_sec=f("CANDIDATE_REFRESH_SEC", 60.0),
            hot_path=(s("HOT_PATH", "").lower() in ("1", "true", "yes", "on")),
            price_from_feed=(s("PRICE_FROM_FEED", "").lower() in ("1", "true", "yes", "on")),
            hot_min_repaid_usd=f("HOT_MIN_REPAID_USD", 450.0),
            positions_from_cache=(s("POSITIONS_FROM_CACHE", "").lower() in ("1", "true", "yes", "on")),
            fast_lane=(s("FAST_LANE", "").lower() in ("1", "true", "yes", "on")),
            fast_lane_min_repaid_usd=f("FAST_LANE_MIN_REPAID_USD", 1000.0),
            fast_lane_max_consec_reverts=int(f("FAST_LANE_MAX_CONSEC_REVERTS", 3)),
            batch_liquidate=(s("BATCH_LIQUIDATE", "").lower() in ("1", "true", "yes", "on")),
            batch_max_items=int(f("BATCH_MAX_ITEMS", 5)),
            canary_probe=(s("CANARY_PROBE", "").lower() in ("1", "true", "yes", "on")),
            canary_max_probes=int(f("CANARY_MAX_PROBES", 100)),
            canary_min_interval_sec=f("CANARY_MIN_INTERVAL_SEC", 120.0),
            canary_max_gas_usd=f("CANARY_MAX_GAS_USD", 25.0),
            preconf_rpc=s("PRECONF_RPC", "https://mainnet-preconf.base.org"),
            sequencer_rpc=s("SEQUENCER_RPC", "https://mainnet-sequencer.base.org"),
            candidates_snapshot_path=s("CANDIDATES_SNAPSHOT_PATH", "candidates_snapshot.json"),
            db_path=s("DB_PATH", "liquidator.db"),
            log_level=s("LOG_LEVEL", "INFO"),
        )
