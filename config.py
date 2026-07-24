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
    predict_shadow: bool = False          # PREDICT-слой (Binance WS + anchor/return, порт Katana v5):
                                          # SHADOW меряет lead/FP, ничего не стреляет (23.07)
    price_from_feed: bool = False         # Layer1: feed-decoded price triggers _process_transmit (OFF until measured)
    hot_min_repaid_usd: float = 75.0     # hot path fires on reaction prizes >= this (repaid USD).
                                         # 23.07: 450->75 — при tip 0.05 выстрел стоит ~$0.1, пыль $50-100
                                         # с бонусом $4-10 стала +EV (в паре с MIN_PROFIT_USD=2)
    tip_mid_repaid_usd: float = 2000.0   # динамический tip (fee-сортировка ВНУТРИ фб-кадра в давку —
    tip_mid_gwei: float = 2.0            # фэт-разведка 45д): приз >= mid -> страховой tip_mid_gwei,
    tip_big_repaid_usd: float = 20000.0  # >= big -> tip_big_gwei (каскадные короли бидят 22-59);
    tip_big_gwei: float = 30.0           # рутина остаётся на tip_gwei (канарейка: позиция не страдает)
    positions_from_cache: bool = False    # P1-6: слой 2 потребление (OFF до калибровки cache-divergence)
    fast_lane: bool = False               # P1-9: fast-lane без sim (OFF; контракт гарантирует исход)
    fast_lane_min_repaid_usd: float = 1000.0  # fast-lane только на призах >= этого repaid USD
    fast_lane_max_repaid_usd: float = 500000.0  # ...и <= этого: выше — sim-путь (импакт выхода
                                                # на мега-сайзе должен считаться, не угадываться;
                                                # direct cbBTC(500) @$500k imp 0.95% < LIF 4.4%)
    fast_lane_max_consec_reverts: int = 3     # II.6 circuit breaker: N подряд ревертов fast-lane -> off ТОЛЬКО fast-lane (фолбэк sim)
    batch_liquidate: bool = False             # мультибатч: N жертв в ОДНОЙ tx (каскад). OFF -> путь бит-в-бит старый
    batch_max_items: int = 5                  # макс items в одной batch-tx (хвост -> следующими tx в пределах max_inflight)
    parallel_broadcast: bool = False          # залп: >1 юнита -> подписать ВСЕ, raw в секвенсер ОДНИМ залпом,
                                              # Alchemy-нога журналом ПОСЛЕ (юнит u+1 не ждёт RTT юнита u).
                                              # OFF/1 юнит -> цикл бит-в-бит старый
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
        # Один источник правды по дефолтам — поля dataclass выше (рассинхрон 23.07:
        # f("TIP_GWEI", 3.0) против tip_gwei=0.05 — выпадение ключа из .env молча
        # вернуло бы устаревший литерал). Здесь только маппинг ENV-ключ -> поле.
        g = os.environ.get
        d = cls()

        def s(key, default=""):
            v = g(key)
            return v if v not in (None, "") else default

        def i(key, default):
            v = g(key)
            return int(float(v)) if v not in (None, "") else int(default)

        def f(key, default):
            v = g(key)
            return float(v) if v not in (None, "") else float(default)

        def b(key):
            return (g(key) or "").lower() in ("1", "true", "yes", "on")

        return cls(
            rpc_url=s("RPC_URL"),
            chain=s("CHAIN", d.chain),
            chain_id=i("CHAIN_ID", d.chain_id),
            wallet_address=s("WALLET_ADDRESS"),
            wallet_key=s("WALLET_KEY"),
            liquidator_address=s("LIQUIDATOR_ADDRESS"),
            mode=s("MODE", d.mode),
            covered_markets_path=s("COVERED_MARKETS_PATH", d.covered_markets_path),
            rescan_interval_sec=f("RESCAN_INTERVAL_SEC", d.rescan_interval_sec),
            exclude_oracles=s("EXCLUDE_ORACLES"),
            min_profit_usd=f("MIN_PROFIT_USD", d.min_profit_usd),
            tip_gwei=f("TIP_GWEI", d.tip_gwei),
            gas_limit_est=i("GAS_LIMIT_EST", d.gas_limit_est),
            eth_price_usd=f("ETH_PRICE_USD", d.eth_price_usd),
            max_daily_loss_usd=f("MAX_DAILY_LOSS_USD", d.max_daily_loss_usd),
            max_daily_gas_usd=f("MAX_DAILY_GAS_USD", d.max_daily_gas_usd),
            max_inflight=i("MAX_INFLIGHT", d.max_inflight),
            tg_bot_token=s("TG_BOT_TOKEN"),
            tg_admin_id=i("TG_ADMIN_ID", d.tg_admin_id),
            alert_antiflood_sec=i("ALERT_ANTIFLOOD_SEC", d.alert_antiflood_sec),
            bot_tag=s("LIQ_BOT_TAG", d.bot_tag),
            poll_interval_sec=f("POLL_INTERVAL_SEC", d.poll_interval_sec),
            loop_mode=s("LOOP_MODE", d.loop_mode),
            block_source=s("BLOCK_SOURCE", d.block_source),
            block_wss_url=s("BLOCK_WSS_URL", d.block_wss_url),
            candidate_refresh_sec=f("CANDIDATE_REFRESH_SEC", d.candidate_refresh_sec),
            hot_path=b("HOT_PATH"),
            predict_shadow=b("PREDICT_SHADOW"),
            price_from_feed=b("PRICE_FROM_FEED"),
            hot_min_repaid_usd=f("HOT_MIN_REPAID_USD", d.hot_min_repaid_usd),
            tip_mid_repaid_usd=f("TIP_MID_REPAID_USD", d.tip_mid_repaid_usd),
            tip_mid_gwei=f("TIP_MID_GWEI", d.tip_mid_gwei),
            tip_big_repaid_usd=f("TIP_BIG_REPAID_USD", d.tip_big_repaid_usd),
            tip_big_gwei=f("TIP_BIG_GWEI", d.tip_big_gwei),
            positions_from_cache=b("POSITIONS_FROM_CACHE"),
            fast_lane=b("FAST_LANE"),
            fast_lane_min_repaid_usd=f("FAST_LANE_MIN_REPAID_USD", d.fast_lane_min_repaid_usd),
            fast_lane_max_repaid_usd=f("FAST_LANE_MAX_REPAID_USD", d.fast_lane_max_repaid_usd),
            fast_lane_max_consec_reverts=i("FAST_LANE_MAX_CONSEC_REVERTS", d.fast_lane_max_consec_reverts),
            batch_liquidate=b("BATCH_LIQUIDATE"),
            batch_max_items=i("BATCH_MAX_ITEMS", d.batch_max_items),
            parallel_broadcast=b("PARALLEL_BROADCAST"),
            canary_probe=b("CANARY_PROBE"),
            canary_max_probes=i("CANARY_MAX_PROBES", d.canary_max_probes),
            canary_min_interval_sec=f("CANARY_MIN_INTERVAL_SEC", d.canary_min_interval_sec),
            canary_max_gas_usd=f("CANARY_MAX_GAS_USD", d.canary_max_gas_usd),
            preconf_rpc=s("PRECONF_RPC", d.preconf_rpc),
            sequencer_rpc=s("SEQUENCER_RPC", d.sequencer_rpc),
            candidates_snapshot_path=s("CANDIDATES_SNAPSHOT_PATH", d.candidates_snapshot_path),
            db_path=s("DB_PATH", d.db_path),
            log_level=s("LOG_LEVEL", d.log_level),
        )
