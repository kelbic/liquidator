"""ФОРК-ПРИЁМКА ПРЯМОГО РОУТА cbBTC->WETH НА ЭПИЗОДЕ ПОТЕРИ 50191667 (задача А).

ПРЕДМЕТ. 19.08, блок 50191667: приз $3905 на рынке 0x5dffffc7 (cbBTC залог / WETH долг) забрал
0x218172c1 с idx 1 ГОЛОВЫ блока. Наш слой №3 в том разборе: пары (cbBTC,WETH) в карте роутов НЕ
БЫЛО — fast-lane невозможен, план жил на Kyber, и цель была НЕДОСТУПНА группе §9 (#133) по
построению (пул Kyber-плана неизвестен). Здесь доказывается, что запись в карту закрывает ровно
это, и доказывается ДИФФЕРЕНЦИАЛЬНО: один и тот же форк, один и тот же `_build`, разница только
в наличии записи.

КАНОНЫ (идиома test_fork_v6d4.py / test_fork_group_v6.py):
 * ПОЗИТИВНЫЙ КОНТРОЛЬ ФОРКА ПЕРВЫМ: пока архивность не подтверждена числами эпизода (долг
   жертвы = repaid победителя), остальное не читается.
 * КРАСНЫЙ-НА-БАЗЕ НА ТОМ ЖЕ ОБЪЕКТЕ: запись вынимается из карты, и тот же `_build` обязан
   отдать Kyber-план (swap_meta=None) — то самое состояние, в котором мы проиграли.
 * ДЕНЕЖНЫЙ ПУТЬ — С КВИТАНЦИИ: Morpho.Liquidate + Liquidated контракта + профит владельцу.
   Хэш эффектом не является.

ЗАПУСК (форк ОБЯЗАН быть свежий):
    anvil --fork-url https://mainnet.base.org --fork-block-number 50191666 --port 8557 --silent &
    FORK_RPC=http://127.0.0.1:8557 python3 test_fork_route_cbbtc_weth.py
Без FORK_RPC печатает SKIP и выходит с нулём.
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
    print("SKIP: FORK_RPC не задан (нужен anvil-форк 50191666, см. докстринг)")
    sys.exit(0)

import requests                                            # noqa: E402
from eth_account import Account                            # noqa: E402
from eth_utils import keccak                               # noqa: E402

import chain.board_v6 as bd                                # noqa: E402
import chain.execute as ex                                 # noqa: E402
import chain.prearm as pr                                  # noqa: E402
import chain.route_depth as rd                             # noqa: E402
import chain.spec_fire_live as sfl_mod                     # noqa: E402
from chain.rpc import BaseRpc                              # noqa: E402
from chain.spec_fire_live import SpecFireLive              # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
V6 = "0x8F92CAc0b6586F834DE33cF31819db0EbF6e52b9"
MID = "0x5dffffc7d75dc5abfa8dbe6fad9cbdadf6680cbe1428bafe661497520c84a94c"
BOR = "0x1a1e7a3b925476a3d81fddaa113dceb94257e88a"
ORACLE = "0x10b95702a0ce895972c91e432c4f7e19811d320e"
LLTV = 915000000000000000
CBBTC, WETH = ex._CBBTC, ex._UNIV3_WETH
KEY = (CBBTC.lower(), WETH.lower())
# Числа победителя (Liquidate-лог блока 50191667, tx 0x82afd4f5…, idx 1)
WIN_REPAID = 1_839_273_459_101_730_693           # 1.839273 WETH
WIN_SEIZED = 6_027_321                           # 0.06027321 cbBTC
WIN_TX = "0x82afd4f5519abdd55cecf8622455df35559916ac6ad2fcdf3196ad498e837895"

S = requests.Session()
RESULTS, FAILS = [], []


def verdict(point, what, ok, extra=""):
    RESULTS.append((point, what, bool(ok)))
    if not ok:
        FAILS.append(point)
    print("  %s %-6s %s%s" % ("[OK]  " if ok else "[FAIL]", point, what,
                              ("\n            | " + str(extra)) if extra else ""))
    return ok


def rpc(m, p):
    r = S.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
               timeout=300).json()
    if "error" in r:
        raise RuntimeError("%s: %s" % (m, r["error"]))
    return r["result"]


def sel(sig):
    return "0x" + keccak(text=sig)[:4].hex()


def call(to, data):
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": data}, "latest"])[2:])


def wait_receipt(h, tries=60):
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


def position():
    d = call(MORPHO, sel("position(bytes32,address)") + MID[2:] + "0" * 24 + BOR[2:])
    return int.from_bytes(d[32:64], "big"), int.from_bytes(d[64:96], "big")


def market():
    mk = call(MORPHO, sel("market(bytes32)") + MID[2:])
    return int.from_bytes(mk[64:96], "big"), int.from_bytes(mk[96:128], "big")


def price():
    return int.from_bytes(call(ORACLE, sel("price()")), "big")


def balance_of(token, who):
    return int.from_bytes(call(token, sel("balanceOf(address)") + "0" * 24 + who[2:]), "big")


T_MORPHO_LIQ = keccak(text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
T_LIQUIDATED = keccak(text="Liquidated(address,address,uint256)")
T_PLANFAILED = keccak(text="PlanFailed(bytes4)")


def logs_of(rec, topic, addr=None):
    t = "0x" + topic.hex()
    return [lg for lg in rec["logs"] if lg["topics"] and lg["topics"][0].lower() == t
            and (addr is None or lg["address"].lower() == addr.lower())]


print("=" * 100)
print("ФОРК-ПРИЁМКА РОУТА cbBTC->WETH на эпизоде потери 50191667 ($3905, рынок 0x5dffffc7)")
print("=" * 100)
FORK_BLOCK = int(rpc("eth_blockNumber", []), 16)
print("форк %s: блок %d" % (RPC_URL, FORK_BLOCK))

# --- П0. ПОЗИТИВНЫЙ КОНТРОЛЬ ---------------------------------------------------------------
print("\nП0. ПОЗИТИВНЫЙ КОНТРОЛЬ: форк отдаёт АРХИВНОЕ состояние блока 50191666")
_code_m = len(rpc("eth_getCode", [MORPHO, "latest"])) // 2 - 1
_code_v = len(rpc("eth_getCode", [V6, "latest"])) // 2 - 1
SH, COL = position()
TBA, TBS = market()
PX = price()
DEBT = SH * TBA // TBS if TBS else 0
HEALTHY = COL * PX * LLTV >= DEBT * 10 ** 36 * 10 ** 18
verdict("П0.1", "код Morpho=%dБ, код v6=%dБ, цена оракула=%.4e" % (_code_m, _code_v, PX),
        _code_m > 10000 and _code_v > 10000 and PX > 0)
verdict("П0.2", "позиция жертвы СТОИТ: shares=%d залог=%d долг=%.9f WETH (repaid победителя "
        "%.9f)" % (SH, COL, DEBT / 1e18, WIN_REPAID / 1e18),
        SH > 0 and COL > 0 and abs(DEBT - WIN_REPAID) / WIN_REPAID < 0.001,
        "дельта к призу %.6f%%" % ((DEBT / WIN_REPAID - 1) * 100))
verdict("П0.3", "позиция УЖЕ ЗА ФЛИПОМ на блоке форка (её и взяли головой следующего) — "
        "здорова=%s" % HEALTHY, not HEALTHY,
        "залог×цена×lltv=%.4e против долг×1e54=%.4e" % (COL * PX * LLTV, DEBT * 10 ** 54))

# --- СТЕНД ------------------------------------------------------------------------------------
bd.SPEC_FIRE_V6 = True
sfl_mod.SPEC_FIRE_V2_BOARD = True
sfl_mod.SPEC_FIRE_MIN_REPAID_USD = 100.0
RPCC = BaseRpc(RPC_URL, 8453)
CFG = types.SimpleNamespace(liquidator_address=V6, gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=2241.0,
                            fast_lane_max_repaid_usd=500_000.0, wallet_key=None)
DEBT_USD = DEBT / 1e18 * 2241.0
SHARED = types.SimpleNamespace(lock=threading.Lock(), groups={MID: [BOR]}, by_id={},
                               debt_by={(MID, BOR): DEBT_USD}, hf_by={(MID, BOR): 0.999})
LOGS = []
OWNER = "0x" + call(V6, sel("owner()"))[12:].hex()
rpc("anvil_impersonateAccount", [OWNER])
rpc("anvil_setBalance", [OWNER, hex(10 ** 19)])


def send_owner(calldata, gas):
    h = rpc("eth_sendTransaction", [{"from": OWNER, "to": V6, "data": calldata,
                                     "gas": hex(int(gas))}])
    return wait_receipt(h)


def new_sf():
    sf = SpecFireLive(rpc=RPCC, cfg=CFG, shared=SHARED, ret_fn=lambda l: 0.0,
                      dev_by={"BTC": 0.003}, log_fn=LOGS.append, now=time.monotonic,
                      sleep=lambda s: None)
    sf._v6_call_fn = lambda to, data: call(to, data)
    sf._v6_send_fn = send_owner
    sf._v6_head_fn = lambda: int(rpc("eth_blockNumber", []), 16)
    sf._arm_retry = lambda *a, **k: None
    return sf


# --- ЗАМЕР СЕЙЗА ЭТОГО ФОРКА (нужен обеим веткам: прогретая квота Kyber кладётся В ПОЛОСЕ
#     [0.97..1.0] от запрошенного — иначе `prearm_get` её законно не отдаст, и «Kyber-ветка»
#     оказалась бы не измерена, а просто не достигнута).
_SEIZED_FORK = (new_sf()._build(MID, BOR, DEBT_USD) or {}).get("plan", {}).get("seized", 0)

# --- П1. КРАСНЫЙ-НА-БАЗЕ: без записи в карте — ровно то состояние, в котором мы проиграли ------
print("\nП1. КРАСНЫЙ-НА-БАЗЕ: вынимаем запись из карты, тот же `_build` на том же форке")
_saved = ex._SWAP_ROUTES.pop(KEY)
pr.prearm_clear()
try:
    _p_nokyber = new_sf()._build(MID, BOR, DEBT_USD)
    verdict("П1.1", "без роута И без прогретой квоты Kyber плана НЕТ ВООБЩЕ (%s)"
            % ("None" if _p_nokyber is None else type(_p_nokyber).__name__),
            _p_nokyber is None)
    # Боевое состояние 19.08: PREARM грел квоту Kyber, и план собирался — но АГРЕГАТНЫЙ.
    # Кладём прогретую квоту в тот же кэш, из которого её берёт `_build` (chain.prearm),
    # калдату не подделываем — она в этой ветке и не исполняется, предмет здесь swap_meta.
    pr.prearm_put(CBBTC, WETH, {"router": "0x" + "ab" * 20, "calldata": "0x" + "cd" * 200,
                                "amount_in": int(_SEIZED_FORK * 0.99)},
                  mid=MID, borrower=BOR)
    _p_base = new_sf()._build(MID, BOR, DEBT_USD)
    verdict("П1.2", "с прогретой квотой план собирается, но АГРЕГАТНЫЙ: swap_meta=None — "
            "fast-lane невозможен, и §9 такую цель в группу не берёт",
            isinstance(_p_base, dict) and _p_base.get("swap_meta") is None,
            "план: %s" % ("нет" if not isinstance(_p_base, dict) else
                          "seized=%d repaid=%d" % (_p_base["plan"]["seized"],
                                                   _p_base["plan"]["repaid_assets"])))
    verdict("П1.3", "без роута §9-котировщик пары = None (цель ГРУППЕ недоступна по построению)",
            rd.make_cumulative_quote_fn(RPCC, CBBTC, WETH, [WIN_SEIZED]) is None)
finally:
    ex._SWAP_ROUTES[KEY] = _saved
    pr.prearm_clear()

# --- П2. С ЗАПИСЬЮ: план становится ПРЯМЫМ -----------------------------------------------------
print("\nП2. С ЗАПИСЬЮ В КАРТЕ: тот же вызов на том же форке")
SF = new_sf()
PROBE = SF._build(MID, BOR, DEBT_USD)
PL = PROBE["plan"] if isinstance(PROBE, dict) else {}
verdict("П2.1", "план ПРЯМОЙ (swap_meta есть) — fast-lane открыт",
        isinstance(PROBE, dict) and PROBE.get("swap_meta") is not None,
        "seized=%d (победитель %d) repaid=%.9f WETH minProfit=%d"
        % (PL.get("seized", 0), WIN_SEIZED, PL.get("repaid_assets", 0) / 1e18,
           PL.get("min_profit", 0)))
verdict("П2.2", "сейз нашего плана сходится с сейзом победителя (<2%): воспроизведён ТОТ эпизод",
        PL.get("seized") and abs(PL["seized"] - WIN_SEIZED) / WIN_SEIZED < 0.02,
        "%d vs %d (%.3f%%)" % (PL.get("seized", 0), WIN_SEIZED,
                               (PL.get("seized", 1) / WIN_SEIZED - 1) * 100))
verdict("П2.3", "роутер плана = роутер записи карты, калдата собрана боевым direct_swap",
        PL.get("router") == _saved["router"] and str(PL.get("swap_calldata", "")).startswith("0x"))

# --- П3. СИНЕРГИЯ С §9: пара стала котируемой ЖИВЫМ квотером -----------------------------------
print("\nП3. СИНЕРГИЯ С #133: цель этого рынка стала ГОДНА в same-pool группу")
_amts = [PL["seized"], PL["seized"] * 3, PL["seized"] * 5]     # состав «три цели рынка»
_qf = rd.make_cumulative_quote_fn(RPCC, CBBTC, WETH, _amts)
verdict("П3.1", "живой §9-котировщик пары строится (то, чего Kyber-план дать не может)",
        _qf is not None)
if _qf is not None:
    _rows = bd.same_pool_min_outs(_amts, _qf,
                                 floors=[int(PL["repaid_assets"] * 1.003)] * 3)
    verdict("П3.2", "худший порядок для состава из 3 целей СЧИТАЕТСЯ и проходит пол",
            all(r["ok"] for r in _rows),
            "; ".join("solo=%d worst=%d потеря=%dbps" % (r["solo_out"], r["worst_out"],
                                                         r["loss_bps"]) for r in _rows))

# --- П4. ДЕНЕЖНЫЙ ПУТЬ НА ЦЕПИ ------------------------------------------------------------------
SNAP = rpc("evm_snapshot", [])
print("\nП4. ДЕНЕЖНЫЙ ПУТЬ: арм v6 боевым путём + выстрел ноги, вердикт с КВИТАНЦИИ")
send_owner(bd.encode_clear_hot(), 100_000)         # на форке жива боевая доска — снимаем (стенд)
ARMED = SF._arm_board(PROBE, MID, BOR, "BTC", debt=DEBT_USD)
G = int(bd.SPEC_FIRE_V6_GROUP)
_o, _dl, _gate, _n = bd.decode_group_info(call(V6, bd.encode_group_info_call(G)))
verdict("П4.1", "группа армлена (n=%d) и указатель наведён" % _n, bool(ARMED) and _n == 1,
        LOGS[-1] if LOGS else "")
LEG = Account.create()
rpc("anvil_setBalance", [LEG.address, hex(10 ** 18)])
send_owner(bd.encode_set_authorized_batch([LEG.address], True), 300_000)
_bal0 = balance_of(WETH, OWNER)
_tx = {"to": V6, "data": "0xff", "chainId": 8453, "gas": 3_000_000,
       "nonce": int(rpc("eth_getTransactionCount", [LEG.address, "pending"]), 16),
       "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
REC = wait_receipt(rpc("eth_sendRawTransaction",
                       ["0x" + LEG.sign_transaction(_tx).raw_transaction.hex()]))
_bal1 = balance_of(WETH, OWNER)
_liq = logs_of(REC, T_MORPHO_LIQ, MORPHO)
_pf = logs_of(REC, T_PLANFAILED, V6)
_profit = _bal1 - _bal0
_repaid = _seized = 0
if _liq:
    d = bytes.fromhex(_liq[0]["data"][2:])
    _repaid, _seized = int.from_bytes(d[0:32], "big"), int.from_bytes(d[64:96], "big")
verdict("П4.2", "ЛИКВИДАЦИЯ ПРОШЛА одной пробой флота: repaid=%.9f WETH seized=%.8f cbBTC "
        "профит=%.9f WETH газ=%d" % (_repaid / 1e18, _seized / 1e8, _profit / 1e18,
                                     REC["gasUsed"]),
        bool(_liq) and _profit > 0 and logs_of(REC, T_LIQUIDATED, V6),
        "PlanFailed=%s" % (("0x" + _pf[0]["data"][2:10]) if _pf else "нет"))
verdict("П4.3", "профит покрыл minProfit плана (гард контракта пройден, а не обойдён)",
        _profit >= int(PL.get("min_profit", 0)),
        "профит=%d minProfit=%d" % (_profit, PL.get("min_profit", 0)))
_sh_after, _ = position()
verdict("П4.4", "позиция жертвы РАЗОБРАНА (shares %d -> %d) — приз, ушедший 0x218172c1"
        % (SH, _sh_after), _sh_after < SH, "tx победителя эпизода: %s" % WIN_TX[:20])

# --- П5. ВЕТКА «ВЕСЬ ЗАЛОГ» (cap_repaid_shares) — ТА, ЧТО ЧАЩЕ ВСЕГО НА lltv 0.915 -------------
# В самом эпизоде кап НЕ сработал (залог 6 416 775 > сейза 6 021 293), то есть форк-приёмка
# выше проверила только «обычную» ветку. Здесь состояние ПОДКРУЧИВАЕТСЯ (залог урезан прямой
# записью в сторадж Morpho), чтобы полная заявка не влезала в залог и `_cap_repaid_shares`
# обязан был её урезать. Смысл проверки: РЕВЕРТ допустим (гонку и так не берём), ПОТЕРЯ — нет;
# Panic 0x11 (0x4e487b71) в этой ветке называется как есть, а не прячется в «не сработало».
print("\nП5. ВЕТКА «ВЕСЬ ЗАЛОГ»: залог урезан, заявка обязана быть УРЕЗАНА, а не панично упасть")
assert rpc("evm_revert", [SNAP]) in (True, "0x1"), "откат к снимку не прошёл"
SNAP = rpc("evm_snapshot", [])


def _pos_slot():
    """Слот packed(borrowShares|collateral<<128) позиции. Раскладка НЕ берётся из памяти:
    перебираем слоты mapping'а и принимаем ТОЛЬКО тот, что совпал с position() до бита."""
    from eth_abi import encode as _enc
    for base in range(0, 10):
        inner = keccak(_enc(["bytes32", "uint256"], [bytes.fromhex(MID[2:]), base]))
        s = int.from_bytes(keccak(_enc(["address", "bytes32"], [BOR, inner])), "big") + 1
        raw = int(rpc("eth_getStorageAt", [MORPHO, hex(s), "latest"]), 16)
        if (raw & ((1 << 128) - 1)) == SH and (raw >> 128) == COL:
            return s
    return None


_slot = _pos_slot()
verdict("П5.1", "слот позиции найден и СОВПАЛ с position() до бита (позитивный контроль "
        "раскладки, а не вера в неё)", _slot is not None,
        "slot=%s" % (hex(_slot) if _slot else "НЕ НАЙДЕН"))
if _slot is not None:
    _new_col = 3_000_000                                  # вдвое меньше полного сейза
    rpc("anvil_setStorageAt", [MORPHO, hex(_slot),
                               "0x%064x" % ((_new_col << 128) | SH)])
    _sh2, _col2 = position()
    verdict("П5.2", "состояние подкручено: залог %d -> %d при том же долге (полная заявка "
            "теперь НЕ влезает)" % (COL, _col2), _col2 == _new_col and _sh2 == SH)
    SNAP5 = rpc("evm_snapshot", [])
    _leg2 = Account.create()
    rpc("anvil_setBalance", [_leg2.address, hex(10 ** 18)])

    def _fire_cap(tag):
        """Один прогон cap-ветки: сборка боевым `_build` -> арм -> проба флота. Всё с квитанции."""
        _pr = new_sf()._build(MID, BOR, DEBT_USD)
        _pl = _pr["plan"] if isinstance(_pr, dict) else {}
        if not _pl:
            return None, {}, None
        send_owner(bd.encode_clear_hot(), 100_000)
        new_sf()._arm_board(_pr, MID, BOR, "BTC", debt=DEBT_USD)
        # Заправка и авторизация — ВНУТРИ прогона: откат к снимку снимает и то и другое, а
        # «нет газа у ноги» выглядело бы как «фикс не работает».
        rpc("anvil_setBalance", [_leg2.address, hex(10 ** 18)])
        send_owner(bd.encode_set_authorized_batch([_leg2.address], True), 300_000)
        _b0 = balance_of(WETH, OWNER)
        _tx = {"to": V6, "data": "0xff", "chainId": 8453, "gas": 3_000_000,
               "nonce": int(rpc("eth_getTransactionCount", [_leg2.address, "pending"]), 16),
               "maxFeePerGas": int(2e9), "maxPriorityFeePerGas": int(1e6)}
        _rc = wait_receipt(rpc("eth_sendRawTransaction",
                               ["0x" + _leg2.sign_transaction(_tx).raw_transaction.hex()]))
        _liq = logs_of(_rc, T_MORPHO_LIQ, MORPHO)
        _pf = logs_of(_rc, T_PLANFAILED, V6)
        _res = {"profit": balance_of(WETH, OWNER) - _b0, "gas": _rc["gasUsed"],
                "sel": ("0x" + _pf[0]["data"][2:10]) if _pf else None,
                "repaid": 0, "seized": 0}
        if _liq:
            _d = bytes.fromhex(_liq[0]["data"][2:])
            _res["repaid"] = int.from_bytes(_d[0:32], "big")
            _res["seized"] = int.from_bytes(_d[64:96], "big")
        print("      [%s] заявка shares=%d сейз(план)=%d -> %s | PlanFailed=%s | профит=%.9f "
              "WETH | газ=%d" % (tag, _pl["repaid_shares"], _pl["seized"],
                                 "ЛИКВИДАЦИЯ" if _liq else "огня нет", _res["sel"] or "нет",
                                 _res["profit"] / 1e18, _res["gas"]))
        return bool(_liq), _res, _pl

    # --- ЖИВОЕ ОКНО НАЧИСЛЕНИЯ ЭТОГО РЫНКА — С ЦЕПИ (эталон ассерта, а не константа стенда) ---
    _mk_raw = call(MORPHO, sel("market(bytes32)") + MID[2:])
    _last_update = int.from_bytes(_mk_raw[128:160], "big")
    _blk_ts = int(rpc("eth_getBlockByNumber", [hex(FORK_BLOCK), False])["timestamp"], 16)
    _elapsed = _blk_ts - _last_update
    _mp5 = (WETH, CBBTC, ORACLE, "0x46415998764c29ab2a25cbea6254146d50d22687", LLTV)
    from chain.multicall import encode_borrow_rate_view_call, decode_borrow_rate_view
    _mk6 = tuple(int.from_bytes(_mk_raw[i * 32:(i + 1) * 32], "big") for i in range(6))
    _rate_ps = decode_borrow_rate_view(call(_mp5[3], "0x" + encode_borrow_rate_view_call(
        _mp5, _mk6).hex()))
    _drift_bps_live = 10_000.0 * _rate_ps * _elapsed / 1e18
    verdict("П5.3", "ЖИВОЕ окно начисления рынка снято С ЦЕПИ: elapsed=%dс (lastUpdate=%d), "
            "ставка %.3f%% годовых => дрейф tba %.4f bps"
            % (_elapsed, _last_update, _rate_ps * 31_536_000 / 1e18 * 100, _drift_bps_live),
            _elapsed > 0 and _rate_ps > 0,
            "окно — свойство РЫНКА, а не нашего TTL")

    # --- КРАСНЫЙ-НА-БАЗЕ: ПРЕЖНЯЯ формула кэпа (граница = полный залог) ------------------------
    import chain.hotpath as _hp
    _saved_cap = _hp._cap_repaid_shares

    def _legacy_cap(borrow_shares, collateral, price, lif_wad, tba, tbs, **_kw):
        from chain.simulate import to_shares_down
        from chain.execute import ORACLE_PRICE_SCALE, WAD
        _bs = int(borrow_shares)
        if _bs <= 0:
            return 0
        if _hp._seized_exact(_bs, lif_wad, price, tba, tbs) <= collateral:
            return _bs
        _ca = (collateral * price // ORACLE_PRICE_SCALE) * WAD // lif_wad
        _rs = min(to_shares_down(_ca, tba, tbs), _bs)
        for _ in range(4):
            if _rs <= 0:
                return 0
            if _hp._seized_exact(_rs, lif_wad, price, tba, tbs) <= collateral:
                return _rs
            _rs -= max(1, _rs // 10 ** 6)
        return 0

    _hp._cap_repaid_shares = _legacy_cap
    try:
        _ok_red, _res_red, _pl_red = _fire_cap("до фикса")
    finally:
        _hp._cap_repaid_shares = _saved_cap
    verdict("П5.3b", "КРАСНЫЙ-НА-БАЗЕ: ПРЕЖНЯЯ формула ставит заявку НА ГРАНИЦУ залога (сейз %d "
            "при залоге %d) и боевая tx падает Panic 0x11"
            % (_pl_red.get("seized", 0), _col2),
            _ok_red is False and _res_red.get("sel") == "0x4e487b71",
            "PlanFailed=%s" % (_res_red.get("sel") or "нет"))
    verdict("П5.4", "КРАСНЫЙ: паника — это НОЛЬ ДЕНЕГ (и счёт в брейкер), а не мягкий промах",
            _res_red.get("profit", -1) == 0)

    # --- ЗЕЛЁНЫЙ: тот же форк, то же состояние, боевой запас #121 на залоговой границе --------
    assert rpc("evm_revert", [SNAP5]) in (True, "0x1"), "откат к снимку cap-ветки не прошёл"
    SNAP5 = rpc("evm_snapshot", [])
    _sh3, _col3 = position()
    verdict("П5.5", "откат состояния доказан: залог снова %d, доля не тронута" % _col3,
            _col3 == _new_col and _sh3 == SH)
    _ok_gr, _res_gr, _pl_gr = _fire_cap("после фикса")
    _undershoot = _pl_red.get("seized", 0) - _pl_gr.get("seized", 0)
    verdict("П5.6", "ЗЕЛЁНЫЙ: та же cap-ветка ПРОХОДИТ — repaid=%.9f WETH seized=%.8f cbBTC "
            "профит=%.9f WETH газ=%d" % (_res_gr.get("repaid", 0) / 1e18,
                                         _res_gr.get("seized", 0) / 1e8,
                                         _res_gr.get("profit", 0) / 1e18, _res_gr.get("gas", 0)),
            bool(_ok_gr) and _res_gr.get("profit", 0) > 0)
    _sh_after2, _col_after2 = position()
    verdict("П5.7", "позиция РАЗОБРАНА капом: shares %d -> %d, залог %d -> %d"
            % (SH, _sh_after2, _col3, _col_after2),
            _sh_after2 < SH and _col_after2 < _col3)
    _applied_bps = _undershoot * 10_000.0 / _col3
    verdict("П5.8", "ЦЕНА ЗАПАСА НАЗВАНА ЧИСЛОМ: недобор пыли на дне залога %d единиц (%.2f bps "
            "залога) — против ноля при панике"
            % (_undershoot, _applied_bps),
            _undershoot > 0 and _applied_bps < 20.0)
    verdict("П5.9", "ПРИМЕНЁННЫЙ ЗАПАС ПОКРЫВАЕТ ЖИВОЕ ОКНО ЭТОГО РЫНКА: %.2f bps против дрейфа "
            "%.4f bps (покрытие %.0fx при требуемых 1.5x)"
            % (_applied_bps, _drift_bps_live,
               _applied_bps / max(_drift_bps_live, 1e-9)),
            _applied_bps >= _drift_bps_live * 1.5)

print("\n" + "=" * 100)
for p, what, ok in RESULTS:
    print("%-6s %-7s %s" % (p, "ПРОШЁЛ" if ok else "ПРОВАЛ", what))
print("=" * 100)
if FAILS:
    print("ФОРК-ПРИЁМКА РОУТА ПРОВАЛЕНА: %s" % ", ".join(FAILS))
    sys.exit(1)
print("ФОРК-ПРИЁМКА РОУТА ПРОШЛА: %d/%d" % (len(RESULTS), len(RESULTS)))
