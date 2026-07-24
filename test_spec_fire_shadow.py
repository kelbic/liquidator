"""Юниты SPEC-FIRE shadow: выбор целей по фиду/направлению, peg-исключение, счётчики. Без сети."""
import sys
import threading
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
from chain.spec_fire import SpecFireShadow

LOGS = []


def mk(collateral, loan):
    return types.SimpleNamespace(collateral_token=collateral, loan_token=loan)


shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={
        "0xbtc_usdc": ["0xA", "0xB"],       # cbBTC/USDC — BTC-driven (down)
        "0xpeg": ["0xC"],                   # LBTC/cbBTC — peg, обе ноги BTC -> исключён
        "0xbtc_weth": ["0xD"],              # cbBTC/WETH — down=BTC, up=ETH
        "0xusr": ["0xE"],                   # USR/USDC — вне фидов
    },
    debt_by={("0xbtc_usdc", "0xA"): 600_000.0, ("0xbtc_usdc", "0xB"): 90_000.0,
             ("0xpeg", "0xC"): 2_000_000.0, ("0xbtc_weth", "0xD"): 120_000.0,
             ("0xusr", "0xE"): 4_000_000.0},
    by_id={"0xbtc_usdc": mk("cbBTC", "USDC"), "0xpeg": mk("LBTC", "cbBTC"),
           "0xbtc_weth": mk("cbBTC", "WETH"), "0xusr": mk("USR", "USDC")},
)
sf = SpecFireShadow(cfg=None, shared=shared, fat_usd=500_000, log_fn=LOGS.append)

# 1) BTC down: cbBTC/USDC (2) + cbBTC/WETH (1); peg и USR исключены; fat=1 ($600k)
r = sf.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
assert r == {"targets": 3, "fat": 1}, r
assert "targets=3" in LOGS[-1] and "fat500k=1" in LOGS[-1] and "0xbtc_us/0xA:$600k" in LOGS[-1], LOGS[-1]
print("UNIT_OK: BTC down — 3 цели, peg/USR исключены, жир найден")

# 2) confirmed закрывает окно и инкрементит счётчики
sf.handle_event({"event": "confirmed", "feed": "BTC", "lead_s": 42.1})  # боевой ключ = lead_s
assert sf.stats == {"windows": 1, "with_targets": 1, "with_fat": 1, "confirmed_with_targets": 1}, sf.stats
assert "lead=42.1s" in LOGS[-1] and "confirmed_hit=1" in LOGS[-1], LOGS[-1]
print("UNIT_OK: confirmed — lead и счётчики в логе")

# 3) ETH up: только cbBTC/WETH (долг в WETH дорожает); cbBTC/USDC не задет
r = sf.handle_event({"event": "arm", "feed": "ETH", "dir": "up"})
assert r == {"targets": 1, "fat": 0}, r
print("UNIT_OK: ETH up — ловит рынок с WETH-долгом, USDC-рынки не задеты")

# 4) disarm освобождает окно без confirmed_hit
sf.handle_event({"event": "disarm", "feed": "ETH"})
assert sf.stats["confirmed_with_targets"] == 1 and "release feed=ETH kind=disarm" in LOGS[-1]
print("UNIT_OK: disarm — release, hit-счётчик не растёт")

# 5) ETH down: ETH-залоговых рынков в наборе нет -> окно пустое (targets=0)
r = sf.handle_event({"event": "arm", "feed": "ETH", "dir": "down"})
assert r == {"targets": 0, "fat": 0}
assert sf.stats["windows"] == 3 and sf.stats["with_targets"] == 2
print("UNIT_OK: пустое окно считается, но не в with_targets")

# 6) мусорное событие не бросает (изоляция внутри handle_event)
assert sf.handle_event({"event": "arm"}) is not None    # feed="" -> 0 целей, не исключение
assert sf.handle_event({"weird": 1}) is None
print("UNIT_OK: мусор не роняет хендлер")

print("=== ВСЕ SPEC-FIRE-SHADOW ЮНИТЫ ПРОШЛИ ===")
