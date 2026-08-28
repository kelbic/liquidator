"""28.08: ДОСКА МЕРТВА 6 ЧАСОВ — потолок газа одной tx (2^24, EIP-7825).

Замер на боевом RPC одноразовым ключом: gas 16 777 216 проходит до проверки баланса, 16 777 217 —
«gas limit too high» (-32003), от 30M — «exceeds max transaction gas limit». Модель лимита арма на
8 целях давала 17 222 095 ⇒ узел отвергал КАЖДЫЙ setGroup (14:08→20:xx: 43 отказа, 0 армов после
19:49), а диагностика звала это «арм сгорел» (status_of(None)=0) при НУЛЕ сгоревших в цепи.
Реальный расход 28 армов дня (роуты ~450 Б): 1 цель 97–173k, 7 целей 1.62M.

Стенд зовёт БОЕВЫЕ arm_gas_limit / arm_gas_expected / arm_fit_count / arm_group, не их копии.
"""
import sys
sys.path.insert(0, ".")
import chain.board_v6 as b
from eth_utils import to_checksum_address

CAP = 1 << 24
MP = {"loanToken": to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
      "collateralToken": to_checksum_address("0xcb585250f852C6c6bf90434AB21A00f02833a4af"),
      "oracle": to_checksum_address("0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE"),
      "irm": to_checksum_address("0x46415998764C29aB2a25CbeA6254146D50D22687"),
      "lltv": 625000000000000000}
ADDR = "0x" + "5f" * 20


def T(n, rb):
    return [b.make_target("0x%040x" % (i + 1), repaid_shares=7, swap_target="0x" + "51" * 20,
                          swap_calldata=bytes(rb), min_profit=0, unwrap_target="0x" + "00" * 20,
                          trigger_price=0) for i in range(n)]


# --- 1. потолок = замеренный; малые группы — бит-в-бит с боевыми лимитами дня (цепь 28.08) -------
assert b.TX_GAS_CAP == 16_777_216, b.TX_GAS_CAP
assert b.arm_gas_limit(1, 450) == 2_152_761      # блок 50571399: lim 2 152 761
assert b.arm_gas_limit(2, 450) == 4_305_523      # блок 50576796: lim 4 305 523, used 225 429
assert b.arm_gas_limit(7, 450) == 15_069_333     # блок 50571824: lim 15 069 333, used 1 617 999
print("UNIT_OK 1: потолок 16 777 216; лимиты 1/2/7 целей бит-в-бит с севшими армами дня")

# --- 2. ГЛАВНОЕ: 8 целей — модель 17 222 095 > потолка, лимит срезан, расход влезает ------------
assert b._arm_gas_raw(8, 450) == 17_222_095, b._arm_gas_raw(8, 450)
assert b.arm_gas_limit(8, 450) == CAP
assert b.arm_gas_expected(8, 450) <= CAP and b.arm_gas_expected(8, 2048) <= CAP
assert b.arm_gas_expected(8, 2048) >= 12_757_108 * 0.99   # эталон модели — измерение (8, 2КБ)
assert b.arm_fit_count(T(8, 450)) == 8, "сегодняшние восьмёрки обязаны влезать целиком"
print("UNIT_OK 2: 8 целей/450 Б: лимит 17 222 095 -> 16 777 216, расход %d влезает, группа целая"
      % b.arm_gas_expected(8, 450))

# --- 3. жирные роуты: расход > потолка -> урезание с хвоста до влезающей, не до нуля --------------
n = b.arm_fit_count(T(8, 4096))
assert 1 <= n < 8, n
assert b.arm_gas_expected(n, 4096) <= CAP < b.arm_gas_expected(n + 1, 4096), (n, b.arm_gas_expected(n, 4096))
assert b.arm_fit_count(T(1, 20_000)) == 1, "одна цель остаётся всегда — её судит узел"
print("UNIT_OK 3: роут 4096 Б: 8 -> %d целей (ожидаемый расход %d <= потолка)" % (n, b.arm_gas_expected(n, 4096)))

# --- 4. arm_group НЕ ШЛЁТ невлезающую группу (иначе сгоревший арм на всю tx, #84) --------------
sent, called = [], []
ok, rec, why = b.arm_group(lambda cd, g: sent.append(g) or {"status": 1},
                           lambda to, d: called.append(1) or b"", ADDR, 0, MP, 1_900_000_000, 0,
                           T(8, 4096))
assert ok is False and rec is None and not sent and not called, (ok, rec, sent, called)
assert "не влезает в потолок" in why and "НЕ ОТПРАВЛЕНО" in why, why
print("UNIT_OK 4: невлезающая группа — отказ БЕЗ отправки и без чтений: %s" % why[:60])

# --- 5. send_fn вернул None -> «tx НЕ ушла», а не «арм сгорел» (подмена, давшая ×20 ложных) -------
ok, rec, why = b.arm_group(lambda cd, g: None, lambda to, d: called.append(1) or b"", ADDR, 0, MP,
                           1_900_000_000, 0, T(2, 64))
assert ok is False and rec is None and not called, (ok, rec, called)
assert "НЕ ушла" in why and "сгорел" not in why.split("НЕ сгоревший")[0], why
print("UNIT_OK 5: None от send_fn = «tx НЕ ушла», чтений цепи нет: %s" % why[:60])

# --- 6. влезающая группа уходит с ЛИМИТОМ <= потолка (сам лимит — в send_fn) ---------------------
sent = []
b.arm_group(lambda cd, g: sent.append(g) or None, lambda to, d: b"", ADDR, 0, MP, 1_900_000_000, 0, T(8, 450))
assert sent == [CAP], sent
print("UNIT_OK 6: восьмёрка уходит с лимитом %d (ровно потолок)" % sent[0])

# --- 7. НЕГАТИВНЫЙ КОНТРОЛЬ: потолок поднят -> старое поведение бит-в-бит -----------------------
_saved = b.TX_GAS_CAP
b.TX_GAS_CAP = 10 ** 9
assert b.arm_gas_limit(8, 450) == 17_222_095 and b.arm_fit_count(T(8, 4096)) == 8
b.TX_GAS_CAP = _saved
assert b.arm_gas_limit(8, 450) == CAP
print("UNIT_OK 7: без потолка модель отдаёт прежние 17 222 095 — режет именно потолок")

# --- 8. ПРОВОДКА: _v6_arm урезает ДО §9 и урезает plans ВМЕСТЕ с targets -------------------------
src = open("chain/spec_fire_live.py").read()
i = src.index("targets = [self._v6_target(p) for p in plans]")
j = src.index("assert_group_min_out_safe", i)
seg = src[i:j]
assert "_v6mod.arm_fit_count(targets)" in seg, "арм-путь не зовёт arm_fit_count до §9"
assert "targets, plans = targets[:_fit], plans[:_fit]" in seg, "plans обязаны урезаться вместе с targets"
assert "группа урезана" in seg, "урезание обязано печататься"
print("UNIT_OK 8: проводка — arm_fit_count стоит между сборкой targets и §9, режет обе структуры")
print("=== ПОТОЛОК GAS TX: 8/8 ===")
