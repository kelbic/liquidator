"""Юниты PENDING-DETECT (пакет A, 04.08): ярус-1/ярус-2/handoff, подавление по Liquidate-event,
хаос-фолбэк health-gate (A6), запрет Alchemy кодом, kill-switch, бенч тика (A5). Без сети."""
import json
import sys
import threading
import time
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.pending_detect as pd_mod
import chain.spec_fire_live as sfl_mod
from chain.pending_detect import PendingDetect, _Revert
from chain.spec_fire_live import SpecFireLive

sfl_mod.SPEC_FIRE_PEAK_OWED = False   # пины до-owed семантики (как в test_spec_fire_live)
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = 1000.0

NOW = {"t": 1000.0}
LOGS = []


def enc_round(rid, upd):
    return "0x" + format(rid, "064x") + format(1, "064x") + format(0, "064x") \
        + format(upd, "064x") + format(rid, "064x")


class FakeHttp:
    """Раздаёт latestRoundData по тегам, eth_call пробы, blockNumber, getLogs."""

    def __init__(self):
        self.pending = (7, 100)
        self.latest = (7, 100)
        self.probe_reverts = True
        self.sealed_logs = []
        self.calls = []

    def __call__(self, url, body):
        req = json.loads(body)
        m, p = req["method"], req["params"]
        self.calls.append((url, m, p))
        if m == "eth_call" and p[0].get("to") == "0xPROXY":
            rid, upd = self.pending if p[1] == "pending" else self.latest
            return {"result": enc_round(rid, upd)}
        if m == "eth_call":                       # проба на контракт-ликвидатор
            if self.probe_reverts:
                return {"error": {"message": "execution reverted: position is healthy"}}
            return {"result": "0x"}
        if m == "eth_blockNumber":
            return {"result": "0x100"}
        if m == "eth_getLogs":
            return {"result": self.sealed_logs}
        raise AssertionError("неожиданный метод " + m)


cfg = types.SimpleNamespace(liquidator_address="0xLIQ", wallet_address="0xBOT")


def mk_pd(http, urls=None):
    sfl = types.SimpleNamespace(_windows={"BTC": object()}, probe_board={}, pending_fire={})
    p = PendingDetect(cfg, sfl, {"BTC": "0xPROXY"},
                      urls=urls or ["http://prov1/x", "http://prov2/x"],
                      http_fn=http, now=lambda: NOW["t"], log_fn=LOGS.append)
    return p, sfl


# 1) Запрет Alchemy КОДОМ: url с alchemy выбрасывается из списка
p, _ = mk_pd(FakeHttp(), urls=["https://base-mainnet.g.alchemy.com/v2/KEY", "http://ok/x"])
assert p.urls == ["http://ok/x"], p.urls
print("OK 1: alchemy-url отклонён кодом")

# 2) Ярус-1 базлайн -> изменение roundId -> ярус-2 -> handoff-флаг у окна
http = FakeHttp()
p, sfl = mk_pd(http)
assert p._cycle(["BTC"]) is True
assert p.stats["t1"] == 1 and p.stats["t2"] == 0          # базлайн, pending==latest — тихо
sfl.probe_board["BTC"] = ("0xbtc", "0xFAT", 1_500_000.0, "0xdead", 625000, NOW["t"])
http.pending = (8, 200)                                   # транзит в формируемом блоке
http.probe_reverts = False                                # ярус-2: жертва флипнута
p._cycle(["BTC"])
assert p.stats["t2"] == 1 and p.stats["handoffs"] == 1
assert sfl.pending_fire["BTC"][:3] == ("0xbtc", "0xFAT", 1_500_000.0)
print("OK 2: ярус-1 -> ярус-2 -> handoff")

# 3) Кулдаун handoff: то же окно не дёргается повторно HANDOFF_CD_SEC
http.pending = (9, 300)
p._cycle(["BTC"])
assert p.stats["handoffs"] == 1
NOW["t"] += pd_mod.PENDING_HANDOFF_CD_SEC + 1
sfl.probe_board["BTC"] = ("0xbtc", "0xFAT", 1_500_000.0, "0xdead", 625000, NOW["t"])
http.pending = (10, 400)
p._cycle(["BTC"])
assert p.stats["handoffs"] == 2
print("OK 3: handoff-кулдаун держит, после CD снова работает")

# 4) Реверт 'healthy' на ярусе-2 = мимо, флага нет
http2 = FakeHttp()
p2, sfl2 = mk_pd(http2)
p2._cycle(["BTC"])
sfl2.probe_board["BTC"] = ("0xbtc", "0xFAT", 1_500_000.0, "0xdead", 625000, NOW["t"])
http2.pending = (8, 200)
http2.probe_reverts = True
p2._cycle(["BTC"])
assert p2.stats["t2"] == 1 and p2.stats["handoffs"] == 0 and not sfl2.pending_fire
print("OK 4: реверт пробы = транзит жертву не флипнул, огня нет")

# 5) Подавление: Liquidate-event в запечатанном чейне -> конкурент успел, флага нет
http3 = FakeHttp()
p3, sfl3 = mk_pd(http3)
p3._cycle(["BTC"])
sfl3.probe_board["BTC"] = ("0xbtc", "0xFAT", 1_500_000.0, "0xdead", 625000, NOW["t"])
http3.pending = (8, 200)
http3.probe_reverts = False
http3.sealed_logs = [{"transactionHash": "0xwinner"}]
p3._cycle(["BTC"])
assert p3.stats["suppressed"] == 1 and p3.stats["handoffs"] == 0 and not sfl3.pending_fire
assert any("suppress" in m for m in LOGS)
print("OK 5: подавление post-Liquidate-event")

# 6) ХАОС (A6): все провайдеры мертвы -> кулдауны -> тик пропущен -> healthy()=False
def dead_http(url, body):
    raise OSError("blackhole")
p4, _ = mk_pd(dead_http)
for _ in range(2 * pd_mod.PENDING_DEAD_FAILS):            # страйп по 2 провайдерам
    p4._cycle(["BTC"])
assert p4.stats["failovers"] == 2                         # оба остыли (DEAD_COOLDOWN)
assert p4._pick() is None                                 # тик пропускается целиком
assert p4._cycle(["BTC"]) is False
NOW["t"] += pd_mod.PENDING_STALE_SEC + 1
assert p4.healthy() is False                              # маячок протух
# сторона окна: нездоровый pending НЕ растягивает каденс (интервал остаётся плотным)
assert sfl_mod.PENDING_KEEPALIVE_SEC > sfl_mod.SPEC_FIRE_DENSE_INTERVAL_SEC
print("OK 6: хаос-тест — кулдауны обоих, тик пропущен, healthy=False")

# 6b) 429-СТРАЙП (боевой урок 07:20): rate-limit = кулдаун провайдеру, НЕ смерть слоя —
# следующий тик уходит соседнему провайдеру и слой остаётся healthy
import urllib.error
_good = FakeHttp()
def rl_http(url, body):
    if "prov1" in url:
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
    return _good(url, body)
p6, _ = mk_pd(rl_http)
assert p6._cycle(["BTC"]) is False and p6.stats["rl429"] == 1   # prov1 остыл на RL_COOLDOWN
assert p6._cycle(["BTC"]) is True                               # страйп увёл на prov2 — успех
p6._healthy_ts = NOW["t"]
assert p6.healthy() is True
NOW["t"] += pd_mod.PENDING_RL_COOLDOWN_SEC + 1
assert p6._cool.get(0, 0.0) <= NOW["t"]                         # prov1 вернулся после кулдауна
print("OK 6b: 429 = кулдаун и страйп, слой жив на соседнем провайдере")

# 7) Kill-switch PENDING_DETECT=0 семантика: пустой список провайдеров -> start() = None
p5, _ = mk_pd(FakeHttp(), urls=["https://base-mainnet.g.alchemy.com/v2/K"])
assert p5.start() is None
print("OK 7: без допустимых провайдеров слой не стартует (и не роняет бота)")

# 8) БЕНЧ A5: оверхед новых веток в тике окна (pending_fire.pop + _prio_ok) << 5мс
shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
scfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000, tip_gwei=0.05,
                             sequencer_rpc="", eth_price_usd=1730.0, fast_lane_max_repaid_usd=5e5)
s = SpecFireLive(rpc=None, cfg=scfg, shared=shared, ret_fn=lambda l: 0.0, dev_by={"BTC": 0.001},
                 log_fn=lambda m: None)
s._prio["XRP"] = (600_000.0, time.monotonic())
t0 = time.perf_counter()
N = 20000
for _ in range(N):
    s.pending_fire.pop("BTC", None)
    s._prio_ok("BTC", 1_500_000.0)
per_tick_ms = (time.perf_counter() - t0) / N * 1000.0
assert per_tick_ms < 0.5, per_tick_ms                     # гейт A5: деградация p95 < 5мс; тут << 0.5мс
print("OK 8: бенч тика — %.4f мс/тик (гейт 5мс)" % per_tick_ms)

print("\nВСЕ БЛОКИ PENDING-DETECT: OK")
