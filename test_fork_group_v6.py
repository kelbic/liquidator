"""ФОРК-ПРИЁМКА МУЛЬТИЦЕЛЕВОЙ ГРУППЫ §9 (#133) НА БОЕВОМ v6-КОНТРАКТЕ BASE.

ПРЕДМЕТ. Юнит-стенд (`test_group_assembler.py`) доказал арифметику и проводку на моделях. Здесь
доказывается ДРУГОЕ и более дорогое: что собранная группа СРАБАТЫВАЕТ НА ЦЕПИ — обе цели одного
пула берутся подряд, и цель, исполняемая ВТОРОЙ (по уже сдвинутой цене), выходит выше своего
пола. Пока этого нет, §9 остаётся формулой, а не механизмом.

ЭТАЛОННЫЙ ЭПИЗОД (наше вскрытие 17.08, docs/STATE.md). Блок 50086380, транзит оракула cbADA на
idx 1156 (answer 0.17296119), победитель 0xac5c4ccf… на idx 1157 ОДНОЙ tx взял ДВЕ ликвидации
одного рынка — 0xF79D2080… (repaid 932.872434 USDC, seized 6077.223844 cbADA) и 0x8f983125…
(repaid 416.923127, seized 2716.057492). Наша доска в тот момент несла ОДНУ цель (это видно на
самом форке: `groupInfo(0).n == 1` на боевом контракте) — ровно тот разрыв, который закрывает
#133. Все числа эпизода восстановлены С ЖИВОЙ ЦЕПИ (Liquidate-логи блока), а не из записи.

КАНОНЫ СТЕНДА (идиома test_fork_v6d4.py):
 * ПОЗИТИВНЫЙ КОНТРОЛЬ ПЕРВЫМ: пока форк не подтвердил АРХИВНОЕ состояние (USDC totalSupply,
   код Morpho/v6, обе позиции с долгом, равным призу победителя), остальные числа не читаются.
 * ВЕРДИКТ С КВИТАНЦИЙ: ликвидация засчитывается по Morpho.Liquidate + Liquidated контракта,
   выход цели — по repaidAssets события ПЛЮС дельта баланса владельца (контракт выметает всё
   владельцу), а не по полям плана. Хэш эффектом не является.
 * ЭТАЛОН — СЫРОЙ СТОРАДЖ: арм группы сверяется чтением слотов `_targets`, а не только view.
 * НЕЗАВИСИМЫЙ ПРОХОД: minOut в калдате сверяется с `same_pool_min_outs` по котировкам, СНЯТЫМ
   ОТДЕЛЬНО (свой вызов квотера), а не с тем, что вернул сборщик.
 * ДИФФЕРЕНЦИАЛЬНЫЙ КРАСНЫЙ: тот же сценарий в ДЕФОЛТНОМ режиме (disjoint) обязан дать группу
   n=1 и ОДНУ ликвидацию — иначе «две ликвидации» не отличить от «стенд не умеет ловить разницу».
 * ЧИСТОЕ СОСТОЯНИЕ НА КАЖДЫЙ ПОРЯДОК: перед каждым прогоном откат к снимку, и сам откат
   доказывается положительно (позиция цели восстановлена), а не принимается на веру.

ЗАПУСК (нужен anvil; форк ОБЯЗАН быть свежий — транзит переигрывается один раз):
    anvil --fork-url https://mainnet.base.org --fork-block-number 50086379 --port 8555 --silent &
    FORK_RPC=http://127.0.0.1:8555 python3 test_fork_group_v6.py
Без FORK_RPC печатает SKIP и выходит с нулём. Ненулевой код возврата = провал приёмки.
"""
import os
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

RPC_URL = os.environ.get("FORK_RPC", "")
if not RPC_URL:
    print("SKIP: FORK_RPC не задан (нужен anvil-форк, см. докстринг)")
    sys.exit(0)

import requests                                            # noqa: E402
from eth_abi import decode as abi_decode                   # noqa: E402
from eth_account import Account                            # noqa: E402
from eth_utils import keccak                               # noqa: E402

import chain.board_v6 as bd                                # noqa: E402
import chain.execute as ex                                 # noqa: E402
import chain.route_depth as rd                             # noqa: E402
import chain.spec_fire_live as sfl_mod                     # noqa: E402
from chain.rpc import BaseRpc                              # noqa: E402
from chain.spec_fire_live import SpecFireLive              # noqa: E402

LIVE = "https://mainnet.base.org"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
V6 = "0x8F92CAc0b6586F834DE33cF31819db0EbF6e52b9"       # боевой v6 (деплой в блоке 50038448)
MID = "0xd7520ad198b497b6eb75bc690268f4597630dbc12e305e9d4105843bab36e41d"
CBADA = "0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ORACLE = "0x35d87a743d1f2f7cafb42d855dc1c5df857ce45f"
LLTV = 625000000000000000
A = "0xf79d2080fe7bff8835f82f5604d6d7bc2128a7e7"          # жирная цель эпизода
B = "0x8f9831252ac6bc69e882dcf839af8ca5e9648ed9"          # вторая цель ТОГО ЖЕ рынка
TRANSIT = "0xaab6526df37fc37aba6f7372ff0c311bc7425ba5be00144af2151ce22967e033"
WINNER_TX = "0xe7d1f0d427a992f718486319d09ccc3e418a391a22edc186df0d06fa1fc8789b"
# Числа победителя (сняты Liquidate-логами блока 50086380 — сверяются с нашими планами).
EPISODE = {A: {"repaid": 932872434, "seized": 6077223844},
           B: {"repaid": 416923127, "seized": 2716057492}}
USDC_SUPPLY_REF = 4_212_119_641_731_880                     # позитивный контроль архивности

S = requests.Session()
RESULTS, FAILS = [], []


def verdict(point, what, ok, extra=""):
    RESULTS.append((point, what, bool(ok)))
    if not ok:
        FAILS.append(point)
    print("  %s %-6s %s%s" % ("[OK]  " if ok else "[FAIL]", point, what,
                              ("\n            | " + str(extra)) if extra else ""))
    return ok


def rpc(m, p, url=RPC_URL):
    r = S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=300).json()
    if "error" in r:
        raise RuntimeError("%s: %s" % (m, r["error"]))
    return r["result"]


def sel(sig):
    return "0x" + keccak(text=sig)[:4].hex()


def call(to, data, block="latest"):
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": data}, block])[2:])


def head():
    return int(rpc("eth_blockNumber", []), 16)


def wait_receipt(h, tries=60):
    """Квитанция обязана быть ПРОЧИТАНА, а не предположена: anvil на форке через публичный
    апстрим отвечает не мгновенно, и «receipt is None» здесь означало бы ложный красный."""
    for _ in range(tries):
        r = rpc("eth_getTransactionReceipt", [h])
        if r is not None:
            r = dict(r)
            r["status"] = int(r["status"], 16)
            r["blockNumber"] = int(r["blockNumber"], 16)
            r["gasUsed"] = int(r["gasUsed"], 16)
            return r
        time.sleep(0.5)
    raise RuntimeError("квитанция %s не пришла" % h)


def balance_of(token, who):
    return int.from_bytes(call(token, sel("balanceOf(address)") + "0" * 24 + who[2:]), "big")


def price():
    return int.from_bytes(call(ORACLE, sel("price()")), "big")


def position(who, block="latest"):
    d = call(MORPHO, sel("position(bytes32,address)") + MID[2:] + "0" * 24 + who[2:], block)
    return int.from_bytes(d[32:64], "big"), int.from_bytes(d[64:96], "big")   # shares, collateral


def market():
    mk = call(MORPHO, sel("market(bytes32)") + MID[2:])
    return int.from_bytes(mk[64:96], "big"), int.from_bytes(mk[96:128], "big")   # tba, tbs


def healthy(who):
    """Здоровье позиции по КАНОНУ Morpho из СЫРОГО состояния цепи (свой расчёт, не view бота):
    collateral*price*lltv >= borrowed*1e36*1e18."""
    shares, coll = position(who)
    tba, tbs = market()
    debt = shares * tba // tbs if tbs else 0
    return coll * price() * LLTV >= debt * 10 ** 36 * 10 ** 18, debt


T_MORPHO_LIQ = keccak(text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
T_LIQUIDATED = keccak(text="Liquidated(address,address,uint256)")
T_PLANEXEC = keccak(text="PlanExecuted(address,uint256)")
T_PLANFAILED = keccak(text="PlanFailed(bytes4)")


def logs_of(rec, topic, addr=None):
    t = "0x" + topic.hex()
    return [lg for lg in rec["logs"] if lg["topics"] and lg["topics"][0].lower() == t
            and (addr is None or lg["address"].lower() == addr.lower())]


def liq_of(rec):
    """(borrower, repaidAssets, seized) по каждому Morpho.Liquidate квитанции."""
    out = []
    for lg in logs_of(rec, T_MORPHO_LIQ, MORPHO):
        d = bytes.fromhex(lg["data"][2:])
        out.append(("0x" + lg["topics"][3][-40:],
                    int.from_bytes(d[0:32], "big"), int.from_bytes(d[64:96], "big")))
    return out


# ==================================================================================================
print("=" * 100)
print("ФОРК-ПРИЁМКА #133: мультицелевая группа same-pool на боевом v6 (эпизод 17.08, блок 50086380)")
print("=" * 100)
FORK_BLOCK = head()
CHAIN_TS = int(rpc("eth_getBlockByNumber", [hex(FORK_BLOCK), False])["timestamp"], 16)
print("форк %s: блок %d, block.timestamp %d" % (RPC_URL, FORK_BLOCK, CHAIN_TS))

# --- П0. ПОЗИТИВНЫЙ КОНТРОЛЬ ФОРКА ------------------------------------------------------------
print("\nП0. ПОЗИТИВНЫЙ КОНТРОЛЬ: апстрим отдаёт АРХИВНОЕ состояние 50086379")
_supply = int.from_bytes(call(USDC, sel("totalSupply()")), "big")
_code_m = len(rpc("eth_getCode", [MORPHO, "latest"])) // 2 - 1
_code_v = len(rpc("eth_getCode", [V6, "latest"])) // 2 - 1
verdict("П0.1", "USDC totalSupply=%.4e, код Morpho=%dБ, код v6=%dБ" % (_supply, _code_m, _code_v),
        _supply == USDC_SUPPLY_REF and _code_m > 10000 and _code_v > 10000,
        "форк на архиве; пустой апстрим дал бы нули и весь стенд был бы измерением своей заглушки")
PX0 = price()
_ok_pos = True
for who in (A, B):
    sh, col = position(who)
    hh, debt = healthy(who)
    _match = abs(debt - EPISODE[who]["repaid"]) <= 5          # <=5 микро-USDC: начисление за блок
    _ok_pos = _ok_pos and sh > 0 and col > 0 and hh and _match
    print("      %s: shares=%d collateral=%d долг=%.6f USDC (приз победителя %.6f) здорова=%s"
          % (who[:10], sh, col, debt / 1e6, EPISODE[who]["repaid"] / 1e6, hh))
verdict("П0.2", "обе цели эпизода СТОЯТ на форке, долг каждой = приз победителя, обе ЗДОРОВЫ до "
        "транзита", _ok_pos, "цена оракула %d" % PX0)
_g0, _cnt, _until = bd.decode_hot_info(call(V6, bd.encode_hot_info_call()))
_o, _dl, _gate, _n = bd.decode_group_info(call(V6, bd.encode_group_info_call(_g0)))
verdict("П0.3", "боевая доска в момент эпизода несла n=%d цель(и) на этом же рынке — тот самый "
        "разрыв, который закрывает #133" % _n,
        _n == 1 and _o.lower() == ORACLE.lower(),
        "hotInfo g0=%d count=%d until=%d; groupInfo.oracle=%s" % (_g0, _cnt, _until, _o))

# --- СТЕНД: боевой SpecFireLive против форка ---------------------------------------------------
bd.SPEC_FIRE_V6 = True
sfl_mod.SPEC_FIRE_V2_BOARD = True
# Порог долга — стендовый: цели эпизода стоят $933 и $417, боевой гейт $500k их бы не увидел
# вовсе. Это НЕ подгонка §9 (порог отбирает КАНДИДАТОВ, а не считает запас) — но факт назван:
# на боевом пороге эта группа не собралась бы, и это отдельный вопрос к настройке, не к #133.
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = 100.0
RPCC = BaseRpc(RPC_URL, 8453)
CFG = types.SimpleNamespace(liquidator_address=V6, gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=3000.0,
                            fast_lane_max_repaid_usd=500_000.0, wallet_key=None)
DEBT_USD = {A: EPISODE[A]["repaid"] / 1e6, B: EPISODE[B]["repaid"] / 1e6}
SHARED = types.SimpleNamespace(lock=threading.Lock(), groups={MID: [A, B]}, by_id={},
                               debt_by={(MID, A): DEBT_USD[A], (MID, B): DEBT_USD[B]},
                               hf_by={(MID, A): 1.004, (MID, B): 1.0038})
LOGS = []


def new_sf():
    sf = SpecFireLive(rpc=RPCC, cfg=CFG, shared=SHARED, ret_fn=lambda l: 0.0,
                      dev_by={"ADA": 0.01}, log_fn=LOGS.append, now=time.monotonic,
                      sleep=lambda s: None)
    sf._v6_call_fn = lambda to, data: call(to, data)
    sf._v6_send_fn = send_owner
    sf._v6_head_fn = head
    sf._arm_retry = lambda *a, **k: None
    return sf


OWNER = "0x" + call(V6, sel("owner()"))[12:].hex()
rpc("anvil_impersonateAccount", [OWNER])
rpc("anvil_setBalance", [OWNER, hex(10 ** 19)])


def send_owner(calldata, gas):
    """ШОВ ТРАНСПОРТА (и только он): owner-tx уходит от ИМПЕРСОНИРОВАННОГО владельца боевого
    контракта. Ключей в стенде нет и быть не может; вся логика арма (кодирование, приёмка по
    цепи, гард отставания узла) исполняется НАСТОЯЩАЯ."""
    h = rpc("eth_sendTransaction", [{"from": OWNER, "to": V6, "data": calldata, "gas": hex(int(gas))}])
    return wait_receipt(h)


# --- П1. НАСТОЯЩИЙ _build обеих целей ----------------------------------------------------------
print("\nП1. НАСТОЯЩИЙ SpecFireLive._build против форка (обе цели, прямой роут cbADA->USDC)")
SF = new_sf()
PROBES = {w: SF._build(MID, w, DEBT_USD[w]) for w in (A, B)}
_ok_b = True
for w in (A, B):
    p = PROBES[w]
    ok = isinstance(p, dict) and p.get("swap_meta") is not None
    _ok_b = _ok_b and ok
    if ok:
        pl = p["plan"]
        print("      %s: seized=%d (эпизод %d) repaid=%d (эпизод %d) shares=%d minProfit=%d"
              % (w[:10], pl["seized"], EPISODE[w]["seized"], pl["repaid_assets"],
                 EPISODE[w]["repaid"], pl["repaid_shares"], pl["min_profit"]))
verdict("П1.1", "обе цели собрались ПРЯМЫМ роутом (swap_meta есть, Kyber не потребовался)", _ok_b)
_close = all(abs(PROBES[w]["plan"]["seized"] - EPISODE[w]["seized"]) / EPISODE[w]["seized"] < 0.01
             for w in (A, B))
verdict("П1.2", "сейз наших планов сходится с сейзом победителя (<1%): воспроизведён ТОТ эпизод",
        _close, "; ".join("%s %d vs %d" % (w[:8], PROBES[w]["plan"]["seized"], EPISODE[w]["seized"])
                          for w in (A, B)))

# --- П2. НАСТОЯЩИЙ _assemble_group в режиме same-pool ------------------------------------------
print("\nП2. НАСТОЯЩИЙ _assemble_group (SPEC_FIRE_V6_POOL_MODE=same-pool)")
bd.SPEC_FIRE_V6_POOL_MODE = bd.POOL_MODE_SAME_POOL
OWNER_PROBE = PROBES[A]
EXTRAS = SF._assemble_group("ADA", "down", MID, A, OWNER_PROBE)
PLANS = [OWNER_PROBE["plan"]] + list(EXTRAS or [])
verdict("П2.1", "группа собрана: целей %d (владелец + %d экстра)" % (len(PLANS), len(PLANS) - 1),
        EXTRAS is not None and len(PLANS) == 2 and PLANS[1]["borrower"].lower() == B.lower(),
        "счётчики: %s" % {k: v for k, v in SF.stats.items() if k.startswith("group_")})
_keys_ok = all(p.get("pool") and p.get("amount_in") and p.get("floor") for p in PLANS) \
    and callable(PLANS[0].get("quote_fn")) and PLANS[1].get("quote_fn") is None
verdict("П2.2", "ключи двери §9 у ВСЕХ планов, котировщик — у владельца", _keys_ok,
        "; ".join("%s amount_in=%d floor=%d" % (p["borrower"][:8], p["amount_in"], p["floor"])
                  for p in PLANS))
_rows_door = bd.assert_group_min_out_safe(
    [p["pool"] for p in PLANS], amounts_in=[p["amount_in"] for p in PLANS],
    floors=[p["floor"] for p in PLANS], quote_fn=PLANS[0]["quote_fn"])
verdict("П2.3", "БОЕВАЯ дверь §9 группу пропускает (не отбивает и не молчит)",
        all(r["ok"] for r in _rows_door),
        "; ".join("worst=%d floor=%d minOut=%d" % (r["worst_out"], r["floor"], r["min_out"])
                  for r in _rows_door))


def min_out_of(plan):
    """amountOutMinimum ИЗ КАЛДАТЫ плана (роут cbADA->USDC — стиль v1/Slipstream: deadline
    третьим полем)."""
    raw = bytes.fromhex(plan["swap_calldata"][10:])
    return abi_decode(["(bytes,address,uint256,uint256,uint256)"], raw)[0][4]


# --- П3. НЕЗАВИСИМЫЙ ЭТАЛОН minOut -------------------------------------------------------------
print("\nП3. ЭТАЛОН ОТДЕЛЬНЫМ ПРОХОДОМ: свои котировки -> same_pool_min_outs -> сверка с калдатой")
_amts = [p["amount_in"] for p in PLANS]
_want = sorted({sum(_amts)} | {sum(_amts) - x for x in _amts} | set(_amts))
_table = rd.cumulative_quotes(RPCC, CBADA, USDC, _want)     # СВОЙ вызов квотера, не тот же объект
HR = max(0, int(os.environ.get("SPEC_FIRE_SAME_POOL_HEADROOM_BPS", "1300")))
_ref = bd.same_pool_min_outs(_amts, lambda x: _table[int(x)], floors=[p["floor"] for p in PLANS],
                             headroom_bps=HR)
_baked = [min_out_of(p) for p in PLANS]
verdict("П3.1", "minOut в калдате КАЖДОЙ цели совпал с независимым расчётом до wei (запас %dbps)"
        % HR, _baked == [r["min_out"] for r in _ref],
        "в калдате %s | эталон %s" % (_baked, [r["min_out"] for r in _ref]))
verdict("П3.2", "боевой запас 1300bps шире глубины роута => клэмп полом связывает: minOut = пол; "
        "и КРИТЕРИЙ §9 (worst >= пола) при этом выполнен с настоящим запасом",
        all(r["min_out"] == r["floor"] and r["worst_out"] >= r["floor"] for r in _ref),
        "; ".join("solo=%d worst=%d floor=%d minOut=%d потеря группировки=%dbps"
                  % (r["solo_out"], r["worst_out"], r["floor"], r["min_out"], r["loss_bps"])
                  for r in _ref))

# --- П4. КРАСНЫЙ-НА-БАЗЕ (дефолтный режим) -----------------------------------------------------
print("\nП4. ДИФФЕРЕНЦИАЛЬНЫЙ КРАСНЫЙ: тот же вход в ДЕФОЛТНОМ режиме disjoint")
bd.SPEC_FIRE_V6_POOL_MODE = bd.POOL_MODE_DISJOINT
_sf2 = new_sf()
_probe_a2 = _sf2._build(MID, A, DEBT_USD[A])                 # свежая проба: планы мутируются
_extras2 = _sf2._assemble_group("ADA", "down", MID, A, _probe_a2)
verdict("П4.1", "в disjoint сборщик не собирает НИЧЕГО (группа остаётся n=1) — это и есть "
        "состояние боевой конфигурации до #133", _extras2 is None,
        "счётчики: %s" % {k: v for k, v in _sf2.stats.items() if k.startswith("group_")})
bd.SPEC_FIRE_V6_POOL_MODE = bd.POOL_MODE_SAME_POOL

# --- П5. АРМ ГРУППЫ НА БОЕВОМ КОНТРАКТЕ --------------------------------------------------------
print("\nП5. АРМ ГРУППЫ на боевом v6 (setGroup+setHot от владельца), приёмка — ЧТЕНИЕМ С ЦЕПИ")
# Подготовка стенда (не предмет замера): боевая доска форка несёт ЖИВОЙ указатель, и перезарядка
# группы внутри живого спана законно ревертит HotSpanLive. В бою это снимает сам `_arm_board_v6`,
# но его гард сравнивает `time.time()` с `until` С ЦЕПИ — на форке часы цепи отстали на двое
# суток, и сравнение даёт «указатель мёртв» там, где контракт считает его живым. Снимаем явно.
send_owner(bd.encode_clear_hot(), 100_000)
# Стэш кладём В ТОЙ ЖЕ ФОРМЕ, что и `_prearm_board`: (id объекта плана владельца, экстры).
# Идентичность — не украшение: она отсекает экстры ЧУЖОГО прогрева, выбравшего того же владельца.
SF._group_extra = {(MID, A): (id(OWNER_PROBE["plan"]), EXTRAS)}
ARMED = SF._arm_board(OWNER_PROBE, MID, A, "ADA", debt=DEBT_USD[A])
verdict("П5.1", "арм прошёл БОЕВЫМ путём `_arm_board` -> `_arm_board_v6` (стэш группы съеден "
        "consume-once)", bool(ARMED) and SF._group_extra == {},
        "журнал: %s" % (LOGS[-1] if LOGS else ""))
G = int(bd.SPEC_FIRE_V6_GROUP)
_o, _dl, _gate, _n = bd.decode_group_info(call(V6, bd.encode_group_info_call(G)))
verdict("П5.2", "groupInfo с цепи: n=%d (ждём 2), oracle рынка, gate=%.4e" % (_n, _gate),
        _n == 2 and _o.lower() == ORACLE.lower() and _gate > 0)


def raw_target(g, i):
    """(borrower, repaidShares) ИЗ СЫРОГО СТОРАДЖА: _targets — mapping в слоте 59, 7 слотов
    на цель. Эталон приёмки: число, которое контракт сообщает о себе через view, приёмкой не
    является (раскладка та же, что в test_fork_v6d4.py)."""
    base = int.from_bytes(keccak(bytes.fromhex("%064x%064x" % (g, 59))), "big") + 7 * i
    b = int(rpc("eth_getStorageAt", [V6, hex(base), "latest"]), 16)
    packed = int(rpc("eth_getStorageAt", [V6, hex(base + 1), "latest"]), 16)
    return "0x%040x" % b, packed & ((1 << 128) - 1)


_raw = [raw_target(G, i) for i in range(2)]
verdict("П5.3", "СЫРОЙ СТОРАДЖ несёт ОБЕ цели с нашими repaidShares (эталон, а не view)",
        _raw[0][0].lower() == A.lower() and _raw[1][0].lower() == B.lower()
        and _raw[0][1] == PLANS[0]["repaid_shares"] and _raw[1][1] == PLANS[1]["repaid_shares"],
        "%s" % (_raw,))
_h = bd.decode_hot_info(call(V6, bd.encode_hot_info_call()))
verdict("П5.4", "горячий указатель наведён на нашу группу (g0=%d count=%d until=%d)" % _h,
        _h[0] == G and _h[1] == 1 and _h[2] == _dl)

# --- П6. ТРАНЗИТ ОРАКУЛА -----------------------------------------------------------------------
print("\nП6. ПЕРЕИГРАННЫЙ НАСТОЯЩИЙ ТРАНЗИТ (tx %s… из блока 50086380)" % TRANSIT[:14])
_raw_tx = None
for _ in range(5):
    for _m in ("debug_getRawTransaction", "eth_getRawTransactionByHash"):
        try:
            _raw_tx = rpc(_m, [TRANSIT], LIVE)
        except Exception:
            _raw_tx = None
        if _raw_tx:
            break
    if _raw_tx:
        break
    time.sleep(3)
assert _raw_tx, "сырую tx транзита не отдали — без неё цели не станут захватываемыми"
_before = {w: healthy(w)[0] for w in (A, B)}
_rec_t = wait_receipt(rpc("eth_sendRawTransaction", [_raw_tx]))
PX1 = price()
_after = {w: healthy(w)[0] for w in (A, B)}
verdict("П6.1", "транзит сел (status=%d, газ %d), цена оракула %.6e -> %.6e (%.3f%%)"
        % (_rec_t["status"], _rec_t["gasUsed"], PX0, PX1, (PX1 / PX0 - 1) * 100),
        _rec_t["status"] == 1 and PX1 < PX0)
verdict("П6.2", "ОБЕ цели были ЗДОРОВЫ до транзита и стали ЗАХВАТЫВАЕМЫ после (позитивный "
        "контроль состояния, расчёт свой — из сырого состояния цепи)",
        all(_before.values()) and not any(_after.values()),
        "до: %s | после: %s" % (_before, _after))

# --- ноги: две штуки с РАЗНЫМ стартом кольца (§2.4: start = keccak(sender) % n) ----------------
def start_of(addr, n):
    return int.from_bytes(keccak(bytes.fromhex(addr[2:])), "big") % n


LEG = {}
while len(LEG) < 2:
    a = Account.create()
    LEG.setdefault(start_of(a.address, 2), a)
for a in LEG.values():
    rpc("anvil_setBalance", [a.address, hex(10 ** 18)])
send_owner(bd.encode_set_authorized_batch([a.address for a in LEG.values()], True), 300_000)
print("      ноги: старт0=%s старт1=%s (авторизованы owner-tx)"
      % (LEG[0].address[:12], LEG[1].address[:12]))


def fire(leg):
    """НАСТОЯЩАЯ проба флота: 1 байт 0xFF (сентинел горячего указателя) с ноги. Вердикт и все
    числа — С КВИТАНЦИИ."""
    tx = {"to": V6, "data": "0xff", "chainId": 8453, "gas": 3_000_000,
          "nonce": int(rpc("eth_getTransactionCount", [leg.address, "pending"]), 16),
          "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
    signed = leg.sign_transaction(tx)
    return wait_receipt(rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()]))


FLOORS = {}          # НЕЗАВИСИМЫЙ пол цели: repaid С КВИТАНЦИИ + minProfit по той же формуле
MINOUT = {PLANS[0]["borrower"].lower(): _baked[0], PLANS[1]["borrower"].lower(): _baked[1]}


def run_order(tag, order):
    """Один порядок исполнения группы. Возврат: {borrower: (repaid, seized, profit, out, газ)}."""
    got = {}
    for leg_i in order:
        who = A if leg_i == 0 else B
        bal0 = balance_of(USDC, OWNER)
        rec = fire(LEG[leg_i])
        bal1 = balance_of(USDC, OWNER)
        liq = liq_of(rec)
        pf = logs_of(rec, T_PLANFAILED, V6)
        if not liq:
            print("      [%s] нога старт%d: ЛИКВИДАЦИИ НЕТ (PlanFailed=%s, логов %d, газ %d)"
                  % (tag, leg_i, ("0x" + pf[0]["data"][2:10]) if pf else "нет",
                     len(rec["logs"]), rec["gasUsed"]))
            continue
        b_, repaid, seized = liq[0]
        profit = bal1 - bal0
        got[b_.lower()] = (repaid, seized, profit, repaid + profit, rec["gasUsed"])
        print("      [%s] нога старт%d -> цель %s: repaid=%.6f seized=%.6f профит=%.6f "
              "выход=%.6f газ=%d" % (tag, leg_i, b_[:10], repaid / 1e6, seized / 1e6,
                                     profit / 1e6, (repaid + profit) / 1e6, rec["gasUsed"]))
    return got


SNAP = rpc("evm_snapshot", [])
print("\nП7. ВЫСТРЕЛ ГРУППЫ, ПОРЯДОК [A,B] (владелец первым, вторая цель свопает по СДВИНУТОЙ цене)")
R_AB = run_order("A,B", [0, 1])
_ok_ab = len(R_AB) == 2
for w in (A, B):
    r = R_AB.get(w.lower())
    if r is None:
        continue
    floor_ind = r[0] + r[0] * sfl_mod.SPEC_FIRE_MIN_PROFIT_BPS // 10000   # пол СЧИТАЕМ САМИ
    FLOORS[w.lower()] = floor_ind
    _ok_ab = _ok_ab and r[3] >= floor_ind and r[3] >= MINOUT[w.lower()]
verdict("П7.1", "ДВЕ ликвидации одного рынка прошли по группе (эпизод воспроизведён)", _ok_ab,
        "; ".join("%s выход=%d пол(независимый)=%d minOut(калдата)=%d"
                  % (w[:8], R_AB[w.lower()][3], FLOORS.get(w.lower(), 0), MINOUT[w.lower()])
                  for w in (A, B) if w.lower() in R_AB))
_last = R_AB.get(B.lower())
verdict("П7.2", "ПОСЛЕДНЯЯ цель порядка (B) вышла ВЫШЕ своего пола, исполняясь по уже сдвинутой "
        "цене — то самое свойство, ради которого §9 и считает худший порядок",
        _last is not None and _last[3] >= FLOORS.get(B.lower(), 1 << 255),
        None if _last is None else "выход=%d пол=%d запас=%d" % (
            _last[3], FLOORS[B.lower()], _last[3] - FLOORS[B.lower()]))

print("\nП8. ХУДШИЙ ПОРЯДОК: откат к снимку и ОБРАТНЫЙ порядок [B,A]")
assert rpc("evm_revert", [SNAP]) in (True, "0x1"), "откат к снимку не прошёл"
SNAP = rpc("evm_snapshot", [])
_sh_a, _ = position(A)
verdict("П8.0", "откат ДОКАЗАН положительно: позиция A восстановлена (shares=%d), цена оракула "
        "осталась пост-транзитной" % _sh_a, _sh_a > 0 and price() == PX1)
R_BA = run_order("B,A", [1, 0])
_ok_ba = len(R_BA) == 2
for w in (A, B):
    r = R_BA.get(w.lower())
    if r is None:
        continue
    floor_ind = r[0] + r[0] * sfl_mod.SPEC_FIRE_MIN_PROFIT_BPS // 10000
    _ok_ba = _ok_ba and r[3] >= floor_ind and r[3] >= MINOUT[w.lower()]
verdict("П8.1", "в ОБРАТНОМ порядке снова ДВЕ ликвидации, и каждая выше своего пола", _ok_ba,
        "; ".join("%s выход=%d пол=%d" % (w[:8], R_BA[w.lower()][3],
                                          R_BA[w.lower()][0] + R_BA[w.lower()][0] * 30 // 10000)
                  for w in (A, B) if w.lower() in R_BA))
_lastA = R_AB.get(A.lower())
_lastA2 = R_BA.get(A.lower())
verdict("П8.2", "цена группировки ИЗМЕРЕНА: выход цели зависит от места в порядке, и оба места "
        "выше пола (это и есть содержание §9)",
        _lastA is not None and _lastA2 is not None,
        "A: первым=%d последним=%d (дельта %d); B: последним=%d первым=%d (дельта %d)"
        % (_lastA[3], _lastA2[3], _lastA2[3] - _lastA[3], R_AB[B.lower()][3], R_BA[B.lower()][3],
           R_BA[B.lower()][3] - R_AB[B.lower()][3]))

# САМАЯ ДОРОГАЯ СВЕРКА ВСЕГО СТЕНДА: предсказание §9 против ЖИВОЙ ликвидности. worst_out
# считался ПО КОТИРОВКАМ (разность двух квот, минус арифметический зазор Д3) — то есть моделью.
# Здесь та же величина измеряется ИСПОЛНЕНИЕМ: цель, отстрелянная ПОСЛЕДНЕЙ, свопает по реально
# сдвинутому пулу. Модель обязана быть НИЖНЕЙ границей: измеренный выход >= предсказанного worst.
# Если бы она границей не была, §9 разрешал бы группы, которые в бою ревертят по minOut.
_meas = {A.lower(): R_BA.get(A.lower()), B.lower(): R_AB.get(B.lower())}   # каждый — в роли последнего
_pred = {PLANS[0]["borrower"].lower(): _ref[0], PLANS[1]["borrower"].lower(): _ref[1]}
_bound_ok = all(_meas[k] is not None and _meas[k][3] >= _pred[k]["worst_out"] for k in _meas)
verdict("П8.3", "ПРЕДСКАЗАНИЕ §9 — НИЖНЯЯ ГРАНИЦА НА ЖИВОЙ ЛИКВИДНОСТИ: измеренный выход "
        "цели-в-конце >= worst_out, посчитанного по котировкам", _bound_ok,
        "; ".join("%s измерено=%d предсказано=%d (запас модели %+d, %.1f ppm)"
                  % (k[:8], _meas[k][3], _pred[k]["worst_out"],
                     _meas[k][3] - _pred[k]["worst_out"],
                     (_meas[k][3] - _pred[k]["worst_out"]) * 1e6 / _pred[k]["worst_out"])
                  for k in _meas if _meas[k]))

print("\nП9. КРАСНЫЙ-НА-БАЗЕ НА ЦЕПИ: та же доска, но группа собрана в ДЕФОЛТНОМ режиме (n=1)")
assert rpc("evm_revert", [SNAP]) in (True, "0x1"), "откат к снимку не прошёл"
SNAP = rpc("evm_snapshot", [])
send_owner(bd.encode_clear_hot(), 100_000)
_sf3 = new_sf()
_probe3 = _sf3._build(MID, A, DEBT_USD[A])
_ok3 = _sf3._arm_board(_probe3, MID, A, "ADA", debt=DEBT_USD[A])        # extra_plans пуст
_o3, _dl3, _g3, _n3 = bd.decode_group_info(call(V6, bd.encode_group_info_call(G)))
R_ONE = run_order("n=1", [0, 1])
_sh_b, _ = position(B)
verdict("П9.1", "без сборщика доска несёт n=%d, флот берёт РОВНО ОДНУ цель, вторая остаётся "
        "живой (shares=%d) — приз конкурента" % (_n3, _sh_b),
        bool(_ok3) and _n3 == 1 and len(R_ONE) == 1 and A.lower() in R_ONE and _sh_b > 0,
        "взято: %s" % list(R_ONE))
verdict("П9.2", "разница ДОКАЗАНА дифференциально: одна и та же цепь, один и тот же контракт, "
        "одни и те же цели — режим группы решает, одна ликвидация или две",
        len(R_AB) == 2 and len(R_BA) == 2 and len(R_ONE) == 1)

# ==================================================================================================
print("\n" + "=" * 100)
for p, what, ok in RESULTS:
    print("%-6s %-7s %s" % (p, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
print("=" * 100)
if FAILS:
    print("ФОРК-ПРИЁМКА #133 ПРОВАЛЕНА: %s" % ", ".join(FAILS))
    sys.exit(1)
print("ФОРК-ПРИЁМКА #133 ПРОШЛА: %d/%d" % (len(RESULTS), len(RESULTS)))
