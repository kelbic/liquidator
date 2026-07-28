"""Юнит-тест ORACLE_LEG_OVERRIDES (28.07: JitoSOL/USDC был невидим watch — оракул-обёртка без
Chainlink-геттеров, конкуренты забрали 2 ликвидации на SOL-дампе). Проверяет: (A) _resolve_legs
отдаёт ноги из оверрайда БЕЗ вызова геттеров, (B) _resolve_one — primary из оверрайда, (C) чужой
оракул идёт обычным путём (геттеры зовутся, оверрайд не липнет), (D) lookup нечувствителен к
регистру (с чейна адрес приходит checksummed), (E) resolve_feeds группирует рынок под ключом
оверрайда. Без сети: rpc замокан."""
import sys
sys.path.insert(0, ".")
import chain.feeds as feeds

JITO_ORACLE = "0x78F772F5Fcc03256260cC1165e34Da9bb3f9BE1C"   # checksummed, как отдаёт чейн
SOL_AGG = "0x43151d66d653ad888bf909f006a01792d5f33243"
MID = "0x09276541cfecb6920a80679a1deced4dde3ae64bf5fc2c9c1f9c21e0c152e1a5"
OTHER_ORACLE = "0x" + "11" * 20


class _StubRpc:
    """Morpho отдаёт oracle; ЛЮБОЙ другой contract() — попытка звать геттеры ног (учитывается)."""

    def __init__(self, oracle):
        self.oracle = oracle
        self.leg_calls = []

    def to_bytes32(self, h):
        return h

    def contract(self, address, abi):
        from chain.morpho import MORPHO_BLUE_ADDRESS
        stub = self
        if address == MORPHO_BLUE_ADDRESS:
            class _Call:
                def call(self):
                    return ("loan", "coll", stub.oracle, "irm", 0)

            class _Fns:
                def idToMarketParams(self, mid):
                    return _Call()

            class _Morpho:
                functions = _Fns()
            return _Morpho()
        stub.leg_calls.append(address)
        raise RuntimeError("нет такой функции")   # _try_addr глотает -> нога None


# A) оверрайд короткого замыкания: ноги из таблицы, геттеры НЕ зовутся
rpc = _StubRpc(JITO_ORACLE)
primary, legs = feeds._resolve_legs(rpc, MID)
assert primary == SOL_AGG and legs == [SOL_AGG], (primary, legs)
assert rpc.leg_calls == [], "оверрайд обязан отработать ДО геттеров: %s" % rpc.leg_calls
print("UNIT_OK: _resolve_legs отдаёт оверрайд без вызова геттеров")

# B) _resolve_one — primary из оверрайда
rpc = _StubRpc(JITO_ORACLE)
assert feeds._resolve_one(rpc, MID) == SOL_AGG
assert rpc.leg_calls == []
print("UNIT_OK: _resolve_one отдаёт primary из оверрайда")

# C) чужой оракул: обычный путь (геттеры зовутся, все ноги None -> (None, []))
rpc = _StubRpc(OTHER_ORACLE)
primary, legs = feeds._resolve_legs(rpc, MID)
assert (primary, legs) == (None, []), (primary, legs)
assert rpc.leg_calls, "без оверрайда геттеры обязаны зваться"
print("UNIT_OK: чужой оракул идёт обычным путём")

# D) регистр: таблица в lower, с чейна checksummed — уже покрыто (A); дублируем явно
assert JITO_ORACLE.lower() in feeds.ORACLE_LEG_OVERRIDES
assert JITO_ORACLE != JITO_ORACLE.lower(), "тест обязан гонять checksummed-адрес"
print("UNIT_OK: lookup по lower() при checksummed-входе")


# E) resolve_feeds: рынок группируется под ключом оверрайда
class _Mkt:
    def __init__(self, mid):
        self.market_id = mid


fmap = feeds.resolve_feeds(_StubRpc(JITO_ORACLE), [_Mkt(MID)])
assert fmap == {SOL_AGG: [MID]}, fmap
assert feeds.primary_map().get(MID) == SOL_AGG
print("UNIT_OK: resolve_feeds группирует рынок под ключом оверрайда, primary_map согласован")

print("=== ВСЕ ORACLE-LEG-OVERRIDE ЮНИТЫ ПРОШЛИ ===")
