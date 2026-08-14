"""Юниты тир-чистой сборки батчей (27.07, «пылевой догруз»): assemble_units / assign_tier /
marginal_item_cost_usd + интеграция через dispatch_liquidations (send_fn-мок, без сети).
Мотив: батч платит fee жирнейшего члена -> пылинка в юните кита ехала бы на tip 30
(~$200+ маржинального газа против приза $10-20). Гоняется в песочнице."""
import sys
sys.path.insert(0, ".")
import chain.execute as ex

TESTKEY = "0x" + "11" * 32
LIQ = "0x" + "cc" * 20

BASE = int(0.05e9)
MID = int(2e9)
BIG = int(30e9)


class _Cfg:
    wallet_key = TESTKEY
    liquidator_address = LIQ
    tip_gwei = 0.05
    tip_mid_gwei = 2.0
    tip_big_gwei = 30.0
    eth_price_usd = 1730.0
    gas_limit_floor = 4_000_000
    batch_liquidate = True
    batch_max_items = 5
    batch_ev_margin = 2.0
    sequencer_rpc = "off"
    parallel_broadcast = False
    fast_lane_max_consec_reverts = 3


cfg = _Cfg()


def row(i, net, gas=None, tip=None):
    prep = {"net_usd": net, "calldata": "0xdead"}
    if gas:
        prep["gas"] = gas
    if tip:
        prep["tip"] = tip
        prep["max_fee"] = 2 * 10_000_000 + tip     # 2×basefee(10 мгвей) + tip, как в hot-path
    return (f"0xm{i}", f"0xb{i}", prep)


def tiers_of(units):
    out = []
    for u in units:
        ts = {int(p.get("tip") or BASE) for _, _, p in u}
        assert len(ts) == 1, f"юнит смешивает ярусы: {ts}"
        out.append(ts.pop())
    return out


# 1) чисто базовый вход (блок-луп, без max_fee) — порядок бит-в-бит со старой сборкой
rows = [row(i, net) for i, net in enumerate([7, 30, 12, 9, 25, 8, 40, 6])]
units, dropped = ex.assemble_units(rows, cfg, max_inflight=5)
old_sorted = sorted(rows, key=lambda r: r[2]["net_usd"], reverse=True)
assert units == [old_sorted[:5], old_sorted[5:]] and not dropped
print("UNIT_OK: базовый вход — сборка бит-в-бит со старой (net desc, чанки по 5)")

# 2) смешанный вход -> тир-чистые юниты, дорогой ярус первым, пыль хвостом
whale = row(0, 1000.0, gas=2_500_000, tip=BIG)
mids = [row(1, 80.0, gas=2_500_000, tip=MID), row(2, 60.0, gas=2_500_000, tip=MID)]
dust = [row(i, 10.0) for i in (3, 4, 5)]
units, dropped = ex.assemble_units([*dust, whale, *mids], cfg, max_inflight=5)
assert tiers_of(units) == [BIG, MID, BASE], tiers_of(units)
assert [len(u) for u in units] == [1, 2, 3] and not dropped
assert units[2] == dust, "пыль обязана уйти хвостовым юнитом на базовом tip"
print("UNIT_OK: тир-чистые юниты, порядок BIG->MID->BASE (киты гонятся, пыль хвостом)")

# 3) демоция: $100 net на tip 30 (каскадная эскалация) не отбивает 2×газ@30 (~$328) -> едет на MID
esc = row(9, 100.0, gas=2_500_000, tip=BIG)
cost_big = ex.marginal_item_cost_usd(esc[2], cfg, BIG)
cost_mid = ex.marginal_item_cost_usd(esc[2], cfg, MID)
assert 100.0 < 2 * cost_big and 100.0 >= 2 * cost_mid, (cost_big, cost_mid)
units, dropped = ex.assemble_units([whale, esc], cfg, max_inflight=5)
assert tiers_of(units) == [BIG, MID] and not dropped
demoted = units[1][0][2]
assert demoted["tip"] == MID and demoted["max_fee"] == 2 * 10_000_000 + MID
assert esc[2]["tip"] == BIG, "исходный prep обязан остаться нетронутым (копия при демоции)"
print("UNIT_OK: демоция BIG->MID с переписанным fee, исходный prep не мутирован")

# 4) EV-дроп: net $0.30 не отбивает 2×газ даже на базовом tip (~$0.87 при floor 4M)
tiny = row(7, 0.30)
cost_base = ex.marginal_item_cost_usd(tiny[2], cfg, BASE)
assert 0.30 < 2 * cost_base < 5.0, cost_base    # порог ниже min_profit $5 -> сейчас не гейт
units, dropped = ex.assemble_units([whale, tiny], cfg, max_inflight=5)
assert [len(u) for u in units] == [1] and len(dropped) == 1
assert dropped[0]["reason"].startswith("ev-cutoff") and dropped[0]["sent"] is False
print("UNIT_OK: EV-дроп ниже базового яруса -> skip-строка 'ev-cutoff' (порог < min_profit)")

# 5) кап max_inflight держит китов, пыль деферится
many_dust = [row(10 + i, 10.0) for i in range(12)]      # 3 пыле-юнита
units, _ = ex.assemble_units([whale, *many_dust], cfg, max_inflight=2)
assert tiers_of(units) == [BIG, BASE] and len(units) == 2
assert units[0][0] == whale, "кит обязан пережить кап max_inflight"
print("UNIT_OK: кап max_inflight — киты первыми, лишняя пыль deferred")

# 6) интеграция dispatch_liquidations (batch ON, send_fn-мок): нонсы по ярусам, дроп в results
class _FakeAccountNS:
    def from_key(self, key):
        from eth_account import Account          # реально доступен в песочнице
        return Account.from_key(key)
class _FakeEth:
    account = _FakeAccountNS()
    def get_transaction_count(self, addr, blk="pending"):
        return 0
class _FakeW3:
    eth = _FakeEth()
class _FakeRpc:
    def _web3(self):
        return _FakeW3()

sent_txds = []
def _send_fn(rpc, key, txd, wait=False, min_tip_wei=0):
    sent_txds.append(dict(txd))
    return {"hash": "0x%02d" % len(sent_txds)}

ex._RECENT_SENDS.clear(); ex._LOCAL_NONCE.clear()
res = ex.dispatch_liquidations(_FakeRpc(), cfg, [row(20, 10.0), whale, tiny], log=None,
                               max_inflight=5, send_fn=_send_fn, wait_receipts=False)
assert len(sent_txds) == 2, sent_txds                      # кит + пылинка, tiny дропнут
assert [t["nonce"] for t in sent_txds] == [0, 1]
assert sent_txds[0].get("maxPriorityFeePerGas") == BIG     # нонс 0 = кит на своём fee
assert "maxFeePerGas" not in sent_txds[1], "пылинка блок-лупа едет дефолтным fee (tip 0.05)"
by_reason = {r.get("reason", ""): r for r in res if not r["sent"]}
assert any(k.startswith("ev-cutoff") for k in by_reason), res
assert sum(1 for r in res if r["sent"]) == 2
print("UNIT_OK: dispatch — кит нонс 0 на своём fee, пыль нонс 1 дефолтным, ev-дроп в results")

print("=== ВСЕ BATCH-TIER ЮНИТЫ ПРОШЛИ ===")
