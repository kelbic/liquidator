"""Юниты covered-рескана (28.07: рескан молча откатил ручную правку владельца — WETH/USDC).
Проверяет: (A) WETH/USDC в INCLUDE_MARKETS переживает потолок MAX_BORROW_USD, (B) не-allowlist
мега-рынок по-прежнему режется, (C) rescan_change_msg даёт причину по каждому изменению
(потолок / исчез из API / вытеснен из топ-N / allowlist-добавление). Без сети/TG."""
import sys
sys.path.insert(0, ".")
from analysis.build_covered_markets import (INCLUDE_MARKETS, MAX_BORROW_USD, rescan_change_msg,
                                            select_markets)

assert "alerts" not in sys.modules, "build_covered_markets обязан быть чист от транспорта"

WETH_MID = "0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda"
MEGA_MID = "0x" + "aa" * 32
SMALL_MID = "0x" + "bb" * 32


def item(mid, col, loan, borrow, lltv=860000000000000000, oracle="0x" + "77" * 20):
    return {"marketId": mid, "collateralAsset": {"symbol": col}, "loanAsset": {"symbol": loan},
            "state": {"borrowAssetsUsd": borrow}, "lltv": lltv, "oracleAddress": oracle}


# A) WETH/USDC: займ $70.8M > потолка $30M, но allowlist держит его в отборе
assert WETH_MID in INCLUDE_MARKETS
items = [item(WETH_MID, "WETH", "USDC", 70_800_000),
         item(MEGA_MID, "FAT", "USDC", 99_000_000),
         item(SMALL_MID, "cbDOGE", "USDC", 240_000)]
got = {m["market_id"] for m in select_markets(items)}
assert WETH_MID in got, "allowlist обязан переживать потолок $%.0fM" % (MAX_BORROW_USD / 1e6)
print("UNIT_OK: WETH/USDC в отборе несмотря на займ $70.8M > потолка")

# B) не-allowlist мега режется как раньше
assert MEGA_MID not in got and SMALL_MID in got
print("UNIT_OK: чужой мега-рынок ($99M) по-прежнему за бортом")

# C) причины в алерте: потолок / исчез из API / вытеснен из топ-N / allowlist
old_pairs = {MEGA_MID: "FAT/USDC", "0x" + "cc" * 32: "GONE/USDC", SMALL_MID: "cbDOGE/USDC"}
new_pairs = {WETH_MID: "WETH/USDC"}
msg = rescan_change_msg(
    items, old_pairs, new_pairs,
    added={WETH_MID},
    removed={MEGA_MID, "0x" + "cc" * 32, SMALL_MID})
assert "+WETH/USDC" in msg and "allowlist" in msg, msg
assert "-FAT/USDC" in msg and "потолка" in msg, msg
assert "-GONE/USDC" in msg and "исчез из API" in msg, msg
assert "-cbDOGE/USDC" in msg and "вытеснен из топ-N" in msg, msg
print("UNIT_OK: rescan_change_msg — причина по каждому изменению")
print("  пример:", msg)

print("=== ВСЕ COVERED-RESCAN ЮНИТЫ ПРОШЛИ ===")
