"""Юниты unwrap-хопа (29.07, «го» kelbic): обёртка без DEX-ликвидности -> redeem -> своп
подлежащим. Без сети: rate-чтение замокано. Проверяет: (A) swap_plan для обычного коллатерала
не меняет ничего (нулевой unwrap), (B) для wsuperOETHb котирует ПОДЛЕЖАЩИМ с haircut и кэшем
rate, (C) encode_liquidate кодирует НОВУЮ 7-аргументную сигнатуру (селектор сменился, unwrap в
хвосте), (D) _prep_to_item_tuple добавляет unwrap_target (дефолт — ноль)."""
import sys
sys.path.insert(0, ".")
from eth_utils import keccak

import chain.execute as ex

WSUPER = "0x7FcD174E80f264448ebeE8c88a7C4476AAF58Ea6"
SUPER = "0xdbfefd2e8460a6ee4955a68582f85708baea60a3"
RATE = 1256_092_000_000_000_000       # ~1.2561, как на 29.07


class _StubRpc:
    def __init__(self):
        self.calls = 0

    def eth_call(self, tx, block="latest"):
        self.calls += 1
        assert tx["to"].lower() == WSUPER.lower()
        assert tx["data"].startswith("0x07a2d13a")        # convertToAssets(uint256)
        return RATE.to_bytes(32, "big")


# A) обычный коллатерал: план свопа нетронут, unwrap нулевой
rpc = _StubRpc()
tok, amt, unwrap = ex.swap_plan(rpc, "0x" + "aa" * 20, 10**18)
assert (tok, amt, unwrap) == ("0x" + "aa" * 20, 10**18, ex.ZERO_ADDR)
assert rpc.calls == 0, "не-обёртка не должна читать rate"
print("UNIT_OK: обычный коллатерал — план не тронут, RPC не дёргается")

# B) wsuperOETHb: токен = подлежащий, размер = seized*rate*haircut, rate кэшируется
ex._unwrap_rate_cache.clear()
rpc = _StubRpc()
seized = 5 * 10**18
tok, amt, unwrap = ex.swap_plan(rpc, WSUPER, seized)
assert tok == SUPER and unwrap == WSUPER
expected = int(seized * RATE // 10**18 * ex._UNWRAP_HAIRCUT)
assert abs(amt - expected) <= 1, (amt, expected)
assert amt < seized * RATE // 10**18, "haircut обязан недобирать (излишек = пыль, недобор = revert)"
ex.swap_plan(rpc, WSUPER, seized)
assert rpc.calls == 1, "rate обязан кэшироваться (TTL 1ч), а не читаться каждый вызов"
print("UNIT_OK: wsuperOETHb — котируем подлежащим %.4f от seized, rate из кэша" % (amt / seized))

# C) encode_liquidate: новая сигнатура (7 аргументов), unwrap кодируется в хвост
mp = {"loanToken": "0x" + "01" * 20, "collateralToken": WSUPER, "oracle": "0x" + "02" * 20,
      "irm": "0x" + "03" * 20, "lltv": 860000000000000000}
cd = ex.encode_liquidate(mp, "0x" + "0b" * 20, 123, "0x" + "0c" * 20, "0xdeadbeef", 7, WSUPER)
sel_new = keccak(text=ex._LIQUIDATE_SIG)[:4].hex()
sel_old = keccak(text="liquidate((address,address,address,address,uint256),"
                      "address,uint256,address,bytes,uint256)")[:4].hex()
assert cd[2:10] == sel_new != sel_old
assert WSUPER[2:].lower() in cd.lower(), "unwrap-адрес обязан быть в calldata"
cd0 = ex.encode_liquidate(mp, "0x" + "0b" * 20, 123, "0x" + "0c" * 20, "0xdeadbeef", 7)
assert cd0[2:10] == sel_new, "дефолт тоже кодирует новую сигнатуру (нулевой unwrap)"
print("UNIT_OK: encode_liquidate — новый селектор %s, unwrap в calldata, дефолт = ноль" % sel_new)

# D) батч-tuple: unwrap_target добавлен, дефолт — ноль
prep = {"mp": mp, "borrower": "0x" + "0b" * 20, "repaid_shares": 1,
        "swap_target": "0x" + "0c" * 20, "swap_calldata": "0xdeadbeef", "min_profit": 0}
t = ex._prep_to_item_tuple(prep)
assert len(t) == 7 and t[6] == ex.ZERO_ADDR
prep["unwrap_target"] = WSUPER
assert ex._prep_to_item_tuple(prep)[6] == WSUPER
assert "address,bytes,uint256,address)[]" in ex._BATCH_LIQUIDATE_SIG.replace("\n", "")
print("UNIT_OK: батч-tuple 7 полей, unwrap в хвосте, дефолт — ноль")

print("=== ВСЕ UNWRAP-ХОП ЮНИТЫ ПРОШЛИ ===")
