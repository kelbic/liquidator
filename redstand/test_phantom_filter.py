"""Юниты фантом-фильтра covered («го» kelbic 30.07: скам-рынок elephanToken под тикером RSS
надул займ $1M, вошёл в топ-N и вытеснил настоящий рынок — отбор по займу геймится).
Проверяет: (A) статический PHANTOM_MARKETS режется в select_markets, (B) причина в алерте,
(C) чистая _is_constant_oracle, (D) динамический filter_phantoms: вердикт, кэш (без повторных
проб), fail-open на транспорте, неприкосновенность allowlist, kill-switch PHANTOM_FILTER=0.
Без сети/TG: пробы инжектятся."""
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")
from analysis.build_covered_markets import (INCLUDE_MARKETS, PHANTOM_MARKETS,
                                            _is_constant_oracle, filter_phantoms,
                                            rescan_change_msg, select_markets)

assert "alerts" not in sys.modules, "build_covered_markets обязан быть чист от транспорта"

RSS_NEW = "0x40ac09f34c5bc0b0b6d9b5f1ec1b97a6a149ff6278104797c9cb740453a2b794"
WETH_MID = "0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda"
LIVE_MID = "0x" + "11" * 32
SCAM_MID = "0x" + "22" * 32


def item(mid, col, loan, borrow, oracle="0x" + "77" * 20, addr="0x" + "88" * 20):
    return {"marketId": mid, "collateralAsset": {"symbol": col, "address": addr, "decimals": 18},
            "loanAsset": {"symbol": loan}, "state": {"borrowAssetsUsd": borrow},
            "lltv": 770000000000000000, "oracleAddress": oracle}


# A) статический фантом с жирным займом не получает слот; сосед — получает
got = {m["market_id"] for m in select_markets([item(RSS_NEW, "RSS", "USDC", 1_000_000),
                                               item(LIVE_MID, "cbDOGE", "USDC", 240_000)])}
assert RSS_NEW not in got and LIVE_MID in got
assert RSS_NEW in PHANTOM_MARKETS
print("UNIT_OK: статический PHANTOM_MARKETS режется в select_markets")

# B) причина удаления в алерте — и статическая, и динамическая (phantom_reasons)
msg = rescan_change_msg([item(RSS_NEW, "RSS", "USDC", 1_000_000)],
                        {RSS_NEW: "RSS/USDC"}, {}, added=set(), removed={RSS_NEW})
assert "-RSS/USDC" in msg and "отсеян как фантом" in msg and "elephanToken" in msg, msg
msg2 = rescan_change_msg([item(SCAM_MID, "FAKE", "USDC", 900_000)],
                         {SCAM_MID: "FAKE/USDC"}, {}, added=set(), removed={SCAM_MID},
                         phantom_reasons={SCAM_MID: "оракул-константа, выхода нет"})
assert "отсеян как фантом: оракул-константа" in msg2, msg2
print("UNIT_OK: rescan_change_msg даёт фантом-причину (статика + динамика)")

# C) детектор константного оракула
ORC = "0xAbCd" + "00" * 18
assert _is_constant_oracle([], ORC)
assert _is_constant_oracle([{"address": ORC.lower(), "storageKeys": ["0x0"]}], ORC)
assert not _is_constant_oracle([{"address": ORC, "storageKeys": []},
                                {"address": "0x" + "99" * 20, "storageKeys": ["0x1"]}], ORC)
print("UNIT_OK: _is_constant_oracle")

# D) динамический фильтр: вердикт + кэш + fail-open + allowlist + kill-switch
tmpdir = tempfile.mkdtemp()
cache_path = os.path.join(tmpdir, "phantom_cache.json")
records = [{"market_id": SCAM_MID, "collateral_token": "FAKE", "loan_token": "USDC"},
           {"market_id": LIVE_MID, "collateral_token": "cbDOGE", "loan_token": "USDC"},
           {"market_id": WETH_MID, "collateral_token": "WETH", "loan_token": "USDC"}]
items = [item(SCAM_MID, "FAKE", "USDC", 900_000), item(LIVE_MID, "cbDOGE", "USDC", 240_000),
         item(WETH_MID, "WETH", "USDC", 70_000_000)]
calls = []


def probe(it):
    calls.append(it["marketId"])
    if it["marketId"] == SCAM_MID:
        return "оракул-константа + Kyber-выхода нет (out $0.00 за 1000 FAKE)"
    return ""


kept, reasons, fresh = filter_phantoms(records, items, "http://x", cache_path,
                                       probe_fn=probe, now=1000.0)
kept_ids = {r["market_id"] for r in kept}
assert SCAM_MID not in kept_ids and LIVE_MID in kept_ids and WETH_MID in kept_ids
assert SCAM_MID in reasons and fresh == {SCAM_MID}
assert WETH_MID in INCLUDE_MARKETS and WETH_MID not in calls, "allowlist не пробуется вообще"
print("UNIT_OK: фантом отсеян, живой и allowlist остались (allowlist без пробы)")

# кэш: повторный прогон — ноль новых проб, фантом уже НЕ «свежий» (без спама в TG)
calls.clear()
kept2, reasons2, fresh2 = filter_phantoms(records, items, "http://x", cache_path,
                                          probe_fn=probe, now=2000.0)
assert not calls and SCAM_MID in reasons2 and fresh2 == set()
print("UNIT_OK: кэш вердиктов — повторный рескан без проб и без 🕳-алерта")

# TTL фантома истёк -> перепроба (выход мог появиться, mGLO-урок)
calls.clear()
filter_phantoms(records, items, "http://x", cache_path, probe_fn=probe,
                now=2000.0 + 8 * 86400)
assert SCAM_MID in calls, "фантом обязан перепроверяться после PHANTOM_RECHECK_SEC"
print("UNIT_OK: перепроба фантома после TTL")

# fail-open: проба падает транспортом -> рынок остаётся, вердикт не кэшируется
def probe_boom(it):
    raise RuntimeError("rpc down")


kept3, reasons3, fresh3 = filter_phantoms(
    [{"market_id": "0x" + "33" * 32, "collateral_token": "X", "loan_token": "USDC"}],
    [item("0x" + "33" * 32, "X", "USDC", 100_000)], "http://x",
    os.path.join(tmpdir, "c2.json"), probe_fn=probe_boom, now=1.0)
assert len(kept3) == 1 and not reasons3 and not fresh3
print("UNIT_OK: fail-open — транспортный сбой пробы не выкидывает рынок")

# kill-switch
os.environ["PHANTOM_FILTER"] = "0"
kept4, r4, f4 = filter_phantoms(records, items, "http://x", cache_path, probe_fn=probe)
assert len(kept4) == len(records) and not r4 and not f4
del os.environ["PHANTOM_FILTER"]
print("UNIT_OK: PHANTOM_FILTER=0 отключает фильтр (откат без деплоя)")

# кэш-файл валиден
cc = json.load(open(cache_path))
assert cc[SCAM_MID]["phantom"] is True and cc[LIVE_MID]["phantom"] is False
print("=== ВСЕ PHANTOM-ЮНИТЫ ПРОШЛИ ===")
