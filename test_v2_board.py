"""Юниты обвязки v5 stateful-board (07.08, «го по плану» kelbic).

Контракт v5 (V5Plan.t.sol, 15 тестов) уже доказал поведение НА ЦЕПИ. Здесь — БОТ:
кто и когда кладёт план, и при каких условиях нога залпа имеет право нести 1 байт.

ГЛАВНОЕ, ЧТО ТУТ ПРОВЕРЯЕТСЯ:
  1. SPEC_FIRE_V2_BOARD=0 -> V1 БИТ-В-БИТ: ни одной новой tx, ноги несут полную калдату.
     (флаг по умолчанию выключен — у боевого v4 нет fallback, 1-байт проба там ревертнёт);
  2. окно оседания: пока запись setPlan могла не сесть в блок, ноги ОБЯЗАНЫ идти по V1 —
     иначе однобайтовая проба бьёт в пустой сторадж и приз уходит молча;
  3. доска действует только на СВОЮ цель (ротация top-K не должна стрелять чужим планом);
  4. осечка арма деградирует к V1, а не гасит огонь.
Без сети.
"""
import sys
import threading
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.spec_fire_live as sfl_mod
from chain.spec_fire_live import SpecFireLive
import chain.execute as ex_mod

sfl_mod.SPEC_FIRE_PEAK_OWED = False

LOGS = []
CLOCK = {"t": 100000.0}
SENT = []            # ноги залпа: (addr, calldata, gas)
ARMED = []           # записи доски: (calldata, gas, tag)


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l, bonus=0.05)


def _addr(tag):                                    # валидные 20 байт: кодировщик ABI настоящий
    return "0x" + (tag * 40)[:40]


FAT = _addr("f")
shared = types.SimpleNamespace(lock=threading.Lock(), groups={"0xbtc": [FAT]},
                               debt_by={("0xbtc", FAT): 1_500_000.0},
                               by_id={"0xbtc": mk("cbBTC", "USDC")})
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000, tip_gwei=0.05,
                            sequencer_rpc="off", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)

PLAN = {"mp": {"loanToken": _addr("1"), "collateralToken": _addr("2"), "oracle": _addr("3"),
               "irm": _addr("4"), "lltv": 860000000000000000},
        "borrower": FAT, "repaid_shares": 12345, "router": _addr("5"),
        "swap_calldata": "0x" + "ab" * 800, "min_profit": 7, "trigger": 2 * 10**36}
PROBE = {"calldata": "0x" + "cd" * 900, "gas": 625_000, "swap_meta": None, "plan": PLAN}


def make(accts=(("0xEA01", "k1"), ("0xEA02", "k2"))):
    sf = SpecFireLive(rpc=None, cfg=cfg, shared=shared, ret_fn=lambda lbl: 0.0,
                      dev_by={"BTC": 0.0010},
                      build_fn=lambda m, b, d: PROBE,
                      send_fn=lambda c, g: "0xh",
                      receipt_fn=lambda h: (0, 0.011), log_fn=LOGS.append,
                      now=lambda: CLOCK["t"], sleep=lambda s: None,
                      volley_accounts_fn=lambda: list(accts),
                      volley_send_fn=lambda rpc, cfg_, addr, key, cd, gas, **kw: (
                          SENT.append((addr, cd, gas)), "0xv%d" % len(SENT))[1])
    sf._board_arm_fn = lambda cd, gas, tag: (ARMED.append((cd, gas, tag)), "0xarm")[1]
    sf._collect_volley = lambda *a, **k: None      # квитанции не нужны, сети нет
    sf._arm_ts = [sf._now()] * 4                   # каскад-флаг тира открыт (как в test_volley_tier)
    sf._cascade_active = lambda: True              # факт-флаг живёт на диске — здесь не про него
    return sf


def volley(sf, mid="0xbtc", b=FAT):
    SENT.clear()
    sf._volley(PROBE, mid, b, 1_500_000.0, "BTC", simultaneous=True)
    for t in threading.enumerate():               # залп живёт в тредах — дождаться
        if t is not threading.current_thread() and t.name.startswith("volley"):
            t.join(timeout=2.0)


def arm_ok(sf, mid="0xbtc", b=FAT):
    ARMED.clear()
    return sf._arm_board(PROBE, mid, b, "BTC")


# ---- 1. ФЛАГ ВЫКЛЮЧЕН = V1 БИТ-В-БИТ ----
sfl_mod.SPEC_FIRE_V2_BOARD = False
sf = make()
assert arm_ok(sf) is False, "выключенный флаг обязан запрещать запись плана"
assert ARMED == [], "при V2_BOARD=0 не должно уйти НИ ОДНОЙ управляющей tx"
volley(sf)
assert len(SENT) == 2, SENT
assert all(cd == PROBE["calldata"] and g == PROBE["gas"] for _, cd, g in SENT), \
    "при V2_BOARD=0 ноги обязаны нести прежнюю полную калдату"
print("UNIT_OK: SPEC_FIRE_V2_BOARD=0 — V1 бит-в-бит, новых tx нет")

# ---- 2. Флаг включён: план кладётся, но доска НЕ СРАЗУ боевая (окно оседания) ----
sfl_mod.SPEC_FIRE_V2_BOARD = True
sf = make()
assert arm_ok(sf) is True
assert len(ARMED) == 1 and ARMED[0][2] == "setPlan", ARMED
_cd, _gas, _ = ARMED[0]
assert _cd.startswith("0x") and len(_cd) > 1600, "план обязан нести роут целиком"
assert _gas > 120_000, "газ записи считается от длины калдаты"
assert sf.stats["board_arms"] == 1
# ...запись только что ушла: сесть не успела -> ноги идут по V1
assert sf._board_ready("0xbtc", FAT) is True, "доска помечена армленной"
assert sf._board_live("0xbtc", FAT) is False, "но боевой ещё не считается"
volley(sf)
assert all(cd == PROBE["calldata"] for _, cd, _g in SENT), \
    "до оседания записи 1-байт проба ушла бы в пустой сторадж — обязан быть V1"
print("UNIT_OK: до оседания setPlan ноги идут по V1 (проба не бьёт в пустую доску)")

# ---- 3. Оседание прошло -> 1 байт ----
CLOCK["t"] += sfl_mod.SPEC_FIRE_V2_BOARD_SETTLE_SEC + 0.1
assert sf._board_live("0xbtc", FAT) is True
volley(sf)
assert len(SENT) == 2, SENT
assert all(cd == ex_mod.V2_PROBE_CALLDATA for _, cd, _g in SENT), SENT
assert all(g == sfl_mod.SPEC_FIRE_V2_PROBE_GAS for _, _c, g in SENT), SENT
assert sf.stats["board_probes"] == 2, sf.stats
assert len(ex_mod.V2_PROBE_CALLDATA) == 4 and ex_mod.V2_PROBE_CALLDATA != "0x00", \
    "проба = один НЕнулевой байт (нулевая калдата читается как простой перевод)"
print("UNIT_OK: после оседания ноги несут 1 байт и дешёвый газ")

# ---- 4. Доска действует ТОЛЬКО на свою цель ----
volley(sf, mid="0xETH", b="0xOTHER")
assert all(cd == PROBE["calldata"] for _, cd, _g in SENT), \
    "чужая цель обязана идти по V1 — иначе выстрелим чужим планом"
print("UNIT_OK: доска не распространяется на другую цель (ротация top-K безопасна)")

# ---- 5. Идемпотентность: та же цель в окне переарма не переписывается ----
ARMED.clear()
assert sf._arm_board(PROBE, "0xbtc", FAT, "BTC") is True
assert ARMED == [], "свежая доска по той же цели — вторая запись это сожжённый газ"
CLOCK["t"] += sfl_mod.SPEC_FIRE_V2_REARM_SEC + 1.0
assert sf._arm_board(PROBE, "0xbtc", FAT, "BTC") is True
assert len(ARMED) == 1, "по истечении каденса роут обязан переармливаться"
print("UNIT_OK: переарм по каденсу, без лишних записей внутри окна")

# ---- 6. Протухание доски -> назад на V1 ----
CLOCK["t"] += sfl_mod.SPEC_FIRE_V2_PLAN_TTL_SEC
assert sf._board_live("0xbtc", FAT) is False, "TTL вышел — доске веры нет"
volley(sf)
assert all(cd == PROBE["calldata"] for _, cd, _g in SENT), "протухшая доска -> V1"
print("UNIT_OK: протухшая доска возвращает залп на V1")

# ---- 7. Осечка арма деградирует к V1, огонь НЕ гасится ----
sf = make()
sf._board_arm_fn = lambda cd, gas, tag: None       # send вернул None (гейт _INFLIGHT / RPC)
assert sf._arm_board(PROBE, "0xbtc", FAT, "BTC") is False
assert sf.stats["board_fails"] == 1, sf.stats
volley(sf)
assert len(SENT) == 2 and all(cd == PROBE["calldata"] for _, cd, _g in SENT), \
    "осечка доски обязана деградировать к V1, а не отменять залп"
print("UNIT_OK: осечка арма — деградация к V1, залп всё равно летит")

# ---- 8. Исключение внутри арма не роняет петлю ----
sf = make()
def _boom(cd, gas, tag):
    raise RuntimeError("rpc down")
sf._board_arm_fn = _boom
assert sf._arm_board(PROBE, "0xbtc", FAT, "BTC") is False
assert any("arm fail" in m for m in LOGS), "осечка обязана быть видна в логе"
volley(sf)
assert len(SENT) == 2, "огонь продолжается"
print("UNIT_OK: исключение в арме поймано, залп не пострадал")

# ---- 9. Плана нет в пробе (старый кэш) -> арма нет, V1 ----
sf = make()
assert sf._arm_board({"calldata": "0xaa", "gas": 1}, "0xbtc", FAT, "BTC") is False
print("UNIT_OK: проба без плана не армит доску")

# ---- 10. triggerPrice: считается из формулы здоровья, с запасом ВВЕРХ ----
# Morpho здоров, пока collateral*price*lltv >= borrowed*1e36*1e18. Долг растёт с начислением,
# значит настоящая граница ползёт вверх — замороженный порог обязан быть ЧУТЬ ВЫШЕ правды,
# иначе фильтр начнёт гасить огонь по живой цели (fail-closed).
borrowed, coll_amt, lltv = 170 * 10**18, 100 * 10**18, 860000000000000000
boundary = borrowed * 10**54 // (coll_amt * lltv)
trig = boundary * (10000 + sfl_mod.SPEC_FIRE_V2_TRIGGER_MARGIN_BPS) // 10000
assert trig > boundary, "порог обязан быть выше границы (fail-open к огню)"
assert trig < boundary * 101 // 100, "но не настолько, чтобы фильтр перестал фильтровать"
print("UNIT_OK: triggerPrice = граница здоровья + запас в сторону огня")

# ---- 11. Кодировщик setPlan: селектор верный, роут внутри ----
cd = ex_mod.encode_set_plan(PLAN["mp"], PLAN["borrower"], PLAN["repaid_shares"], PLAN["router"],
                            PLAN["swap_calldata"], PLAN["min_profit"], 1800000000, PLAN["trigger"])
from eth_utils import keccak
assert cd[:10] == "0x" + keccak(text=ex_mod._SET_PLAN_SIG)[:4].hex(), cd[:10]
assert "ab" * 400 in cd, "swapCallData обязан доехать в калдате целиком"
print("UNIT_OK: encode_set_plan — селектор и роут на месте")

sfl_mod.SPEC_FIRE_V2_BOARD = False
print("ПРОШЛИ: обвязка v5 stateful-board")
