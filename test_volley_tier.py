"""Юниты каскадного тира (07.08, «го» kelbic): залп той же пробой с залповых EOA.
Инварианты: активация СТРОГО fat_mode И каскад (армы>=4/час И факты — fail-closed);
фазовый сдвиг перед каждой ногой; тот же calldata; газ ног — в бакет тира, kill-switch
(specfire_gas_today) видит сумму; WIN ноги = WIN окна (дедуп, кулдаун, окно закрыто);
SPEC_FIRE_VOLLEY=0 -> бит-в-бит один EOA; execute-гейты до любого RPC. Без сети."""
import os
import sys
import threading
import time
import types

sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
import chain.spec_fire_live as sfl_mod
import chain.execute as ex
from chain.spec_fire_live import SpecFireLive

sfl_mod.SPEC_FIRE_PEAK_OWED = False   # тестируем тир, не owed-семантику

LOGS = []


def mk(c, l):
    return types.SimpleNamespace(collateral_token=c, loan_token=l, bonus=0.05)


shared = types.SimpleNamespace(
    lock=threading.Lock(),
    groups={"0xbtc": ["0xFAT"]},
    debt_by={("0xbtc", "0xFAT"): 1_500_000.0},
    by_id={"0xbtc": mk("cbBTC", "USDC")},
)
cfg = types.SimpleNamespace(liquidator_address="0xLIQ", gas_limit_est=500_000,
                            tip_gwei=0.05, sequencer_rpc="off", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0)

V_ACCTS = [("0xEOA2", "k2"), ("0xEOA3", "k3")]
VLEGS = []          # ноги залпа: (addr, calldata, gas, tip)
RECEIPT = {"v": (0, 0.011)}
sent = []


def v_accounts():
    return V_ACCTS


def v_send(rpc, cfg_, addr, key, calldata, gas, log=None, max_fee=None, tip=None,
           broadcast_raw_urls=None):
    VLEGS.append((addr, calldata, gas, tip))
    return "0xv%d" % len(VLEGS)


def build_fn(mid, b, d):
    return {"calldata": "0xdeadbeef", "gas": 625_000}


def send_fn(calldata, gas):
    sent.append(calldata)
    return "0xh%d" % len(sent)


def receipt_fn(h):
    return RECEIPT["v"]


def make(ret_v=None, **kw):
    kw.setdefault("build_fn", build_fn)
    kw.setdefault("send_fn", send_fn)
    kw.setdefault("receipt_fn", receipt_fn)
    kw.setdefault("log_fn", LOGS.append)
    kw.setdefault("volley_accounts_fn", v_accounts)
    kw.setdefault("volley_send_fn", v_send)
    rv = ret_v or {"v": 0.0}
    return SpecFireLive(rpc=None, cfg=cfg, shared=shared,
                        ret_fn=lambda lbl: rv["v"], dev_by={"BTC": 0.0010}, **kw)


PROBE = {"calldata": "0xdeadbeef", "gas": 625_000}


class SyncThread:                      # детерминизм: "тред" исполняется в момент .start()
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._t, self._a, self._k = target, args, (kwargs or {})

    def start(self):
        self._t(*self._a, **self._k)

    def join(self, timeout=None):
        pass


# ---- Часть A: _volley детерминированно (SyncThread) ----
_real_threading = sfl_mod.threading
_orig_facts = sfl_mod._facts_fresh
sfl_mod.threading = types.SimpleNamespace(Thread=SyncThread, Lock=_real_threading.Lock,
                                          Event=_real_threading.Event)
SLEEPS = []
try:
    # 1. каскад-флага нет (армов мало) -> тир молчит
    sf = make(sleep=SLEEPS.append)
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC")
    assert not VLEGS, VLEGS
    print("UNIT_OK: без каскад-флага залп молчит")

    # 2. армы есть, факты протухли -> молчит (fail-closed гибрида)
    sf._arm_ts = [sf._now()] * 4
    sfl_mod._facts_fresh = lambda now_wall=None: False
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC")
    assert not VLEGS, VLEGS
    print("UNIT_OK: армы без факт-флага — залп молчит (fail-closed)")

    # 3. каскад активен -> ноги по всем EOA: фазовый сдвиг ПЕРЕД каждой, тот же calldata,
    #    tip капнут, газ ног -> бакет тира, kill-switch видит сумму
    sfl_mod._facts_fresh = lambda now_wall=None: True
    g0, s0 = ex.volley_gas_today(), ex.specfire_gas_today()
    SLEEPS.clear()
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC")
    assert [a for a, _, _, _ in VLEGS] == ["0xEOA2", "0xEOA3"], VLEGS
    assert all(c == "0xdeadbeef" and g == 625_000 for _, c, g, _ in VLEGS)
    assert all(t == int(0.05e9) for *_, t in VLEGS)          # tip = min(боевой, кап)
    # сдвиг НАКОПЛЕННЫЙ: нога i ждёт (i+1)*PHASE от начала залпа. Прежний цикл спал PHASE
    # перед каждой, но добавлял к шагу блокировку send_tx (замер: 60мс p50) — расписание
    # задавала сеть. Тред на ногу => фаза снова наша.
    # ...и с приходом ФЛОТА (07.08) сдвиг стал КОМПЕНСИРОВАННЫМ: нога спит не «свой шаг», а
    # «свой шаг минус уже прошедшее с начала залпа». Иначе внутри пачки (ноги режутся на
    # пачки, чтобы 70 тредов не задушили два ядра) сдвиги накапливались бы поверх времени
    # отправки — и расписание снова задавала бы сеть. Отсюда допуск, а не равенство.
    _want = [sfl_mod.SPEC_FIRE_VOLLEY_PHASE_SEC * (i + 1) for i in range(2)]
    assert len(SLEEPS) == 2 and all(0 <= w - s < 0.02 for s, w in zip(SLEEPS, _want)), SLEEPS
    assert abs(ex.volley_gas_today() - g0 - 0.022) < 1e-9    # 2 ноги × $0.011
    assert abs(ex.specfire_gas_today() - s0 - 0.022) < 1e-9  # kill-switch видит залп
    assert sf.stats["volley_probes"] == 2
    print("UNIT_OK: залп по всем EOA — фаза/calldata/tip/бакет/kill-switch")

    # 4. SPEC_FIRE_VOLLEY=0 -> бит-в-бит молчание
    ex.SPEC_FIRE_VOLLEY = False
    VLEGS.clear()
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC")
    assert not VLEGS
    ex.SPEC_FIRE_VOLLEY = True
    print("UNIT_OK: откат SPEC_FIRE_VOLLEY=0 глушит тир")

    # 5. WIN ноги: окно закрыто, кулдаун взведён, реактивный дубль заглушён
    RECEIPT["v"] = (1, 0.05)
    VLEGS.clear()
    evt = _real_threading.Event()
    sf._windows["BTC"] = evt
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC")
    assert evt.is_set() and "BTC" not in sf._windows
    # #46: победа ноги гасит ТОЛЬКО свой фид и только под откатом триггера; окно всё
    # равно закрыто (приз взят), а соседние фиды каскада остаются живыми
    assert not sf._cooldown_active("DOGE"), "победа по BTC погасила DOGE (#46)"
    with ex._SEND_LOCK:
        assert ("0xbtc", "0xFAT") in ex._RECENT_SENDS
    assert sf.stats["wins"] >= 1
    RECEIPT["v"] = (0, 0.011)
    print("UNIT_OK: WIN ноги закрывает окно (дедуп + кулдаун)")

    # 6. откат SPEC_FIRE_VOLLEY_PARALLEL=0 -> прежний цикл: сдвиг ПЕРЕД каждой ногой
    sfl_mod.SPEC_FIRE_VOLLEY_PARALLEL = False
    VLEGS.clear()
    SLEEPS.clear()
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC")
    assert SLEEPS == [sfl_mod.SPEC_FIRE_VOLLEY_PHASE_SEC] * 2, SLEEPS
    assert len(VLEGS) == 2, VLEGS
    sfl_mod.SPEC_FIRE_VOLLEY_PARALLEL = True
    print("UNIT_OK: откат SPEC_FIRE_VOLLEY_PARALLEL=0 — прежняя лесенка")

    # 7. по детекту момент ИЗВЕСТЕН -> ноги разом, ни одного сдвига
    VLEGS.clear()
    SLEEPS.clear()
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC", simultaneous=True)
    assert SLEEPS == [], SLEEPS
    assert len(VLEGS) == 2, VLEGS
    print("UNIT_OK: залп по детекту (simultaneous) — без фазового сдвига")

    # 8. откат SPEC_FIRE_VOLLEY_DETECT_SYNC=0 -> лесенка возвращается даже по детекту
    sfl_mod.SPEC_FIRE_VOLLEY_DETECT_SYNC = False
    VLEGS.clear()
    SLEEPS.clear()
    sf._volley(PROBE, "0xbtc", "0xFAT", 1.5e6, "BTC", simultaneous=True)
    # ...и с приходом ФЛОТА (07.08) сдвиг стал КОМПЕНСИРОВАННЫМ: нога спит не «свой шаг», а
    # «свой шаг минус уже прошедшее с начала залпа». Иначе внутри пачки (ноги режутся на
    # пачки, чтобы 70 тредов не задушили два ядра) сдвиги накапливались бы поверх времени
    # отправки — и расписание снова задавала бы сеть. Отсюда допуск, а не равенство.
    _want = [sfl_mod.SPEC_FIRE_VOLLEY_PHASE_SEC * (i + 1) for i in range(2)]
    assert len(SLEEPS) == 2 and all(0 <= w - s < 0.02 for s, w in zip(SLEEPS, _want)), SLEEPS
    sfl_mod.SPEC_FIRE_VOLLEY_DETECT_SYNC = True
    print("UNIT_OK: откат SPEC_FIRE_VOLLEY_DETECT_SYNC=0 — лесенка по детекту")
finally:
    sfl_mod.threading = _real_threading
    sfl_mod._facts_fresh = _orig_facts
    sfl_mod.SPEC_FIRE_VOLLEY_PARALLEL = True
    sfl_mod.SPEC_FIRE_VOLLEY_DETECT_SYNC = True

# ---- Часть B: execute-гейты (до любого RPC — rpc=None это доказывает) ----
ex._VOLLEY_DAY = int(time.time() // 86400)
ex._VOLLEY_GAS_USD = ex.CASCADE_TIER_GAS_USD + 1.0
assert ex.send_volley_probe(None, cfg, "0xEOA2", "k2", "0x", 100000) is None
ex._VOLLEY_GAS_USD = 0.0
print("UNIT_OK: полный $-бакет тира глушит ногу до RPC")

ex._INFLIGHT += 1
try:
    assert ex.send_volley_probe(None, cfg, "0xEOA2", "k2", "0x", 100000) is None
finally:
    ex._INFLIGHT -= 1
print("UNIT_OK: боевая tx в полёте — нога молчит")

assert ex.send_volley_probe(None, cfg, "0xNOPE", "k", "0x", 100000) is None
print("UNIT_OK: незагруженный EOA (нет лока) — нога молчит")

# ---- ГЕЙТ ПО ХУДШЕМУ СЛУЧАЮ vs РАСХОД ПО ФАКТУ (замер 07.08, #32) ----
# Лимит газа пробы доски считается от ВЫИГРЫША (3M), а тратит она ~45k. Если списывать с
# кэша баланса весь лимит, нога замолкает после ~28 проб — два блока, то есть флот глушит
# себя ровно в каскаде. Здесь проверяется РАЗВЕДЁННОСТЬ этих величин: гейт по худшему,
# декремент по факту.
_ADDR = "0xDEEP"
ex._VOLLEY_LOCKS[_ADDR] = _real_threading.Lock()
ex._VOLLEY_NONCE[_ADDR] = 7          # nonce задан: _pending_nonce тянет web3, которого тут нет
_MAXFEE = int(0.06e9)
_LIMIT = 3_000_000
_sent_n = [0]


class _FakeW3:
    class eth:
        @staticmethod
        def get_balance(a):
            raise AssertionError("баланс обязан браться из кэша, а не с RPC в горячем пути")


class _FakeRpc:
    chain_id = 8453

    def _web3(self):
        return _FakeW3()


def _fake_send_tx(rpc, key, tx, **kw):
    _sent_n[0] += 1
    return {"hash": "0xdeep%d" % _sent_n[0]}


_orig_send_tx = ex.send_tx
ex.send_tx = _fake_send_tx
try:
    # (1) нога с балансом НИЖЕ пола+худшего случая обязана молчать: выигрыш ей не оплатить
    ex._VOLLEY_BAL[_ADDR] = [ex._VOLLEY_BAL_FLOOR_WEI + _LIMIT * _MAXFEE - 1, time.monotonic()]
    assert ex.send_volley_probe(_FakeRpc(), cfg, _ADDR, "k", "0x01", _LIMIT,
                                max_fee=_MAXFEE, tip=int(0.05e9)) is None,         "нога, не способная оплатить полный выигрыш, обязана молчать"
    print("UNIT_OK: гейт по ХУДШЕМУ случаю — нога без денег на выигрыш не стреляет")

    # (2) боевая нога (0.008 ETH) обязана выдержать ЗАЛП, а не 28 проб
    ex._VOLLEY_BAL[_ADDR] = [int(0.008e18), time.monotonic()]
    _sent_n[0] = 0
    _fired = 0
    for _ in range(200):
        if ex.send_volley_probe(_FakeRpc(), cfg, _ADDR, "k", "0x01", _LIMIT,
                                max_fee=_MAXFEE, tip=int(0.05e9)) is None:
            break
        _fired += 1
    # 13 проб/блок на ногу: меньше 100 подряд = нога садится быстрее, чем живёт окно
    assert _fired >= 100, ("нога замолчала после %d проб — при лимите от выигрыша "
                           "декремент обязан считаться по факту, иначе флот глушит себя" % _fired)
    print("UNIT_OK: расход по ФАКТУ — нога держит %d проб подряд (лимит газа 3M)" % _fired)

    # (3) позитивный контроль: списание всё же происходит (иначе тест прошёл бы и на нуле)
    assert ex._VOLLEY_BAL[_ADDR][0] < int(0.008e18), "декремент обязан быть, просто не по лимиту"
    print("UNIT_OK: позитивный контроль — баланс кэша всё-таки убывает")
finally:
    ex.send_tx = _orig_send_tx
    ex._VOLLEY_BAL.pop(_ADDR, None)
    ex._VOLLEY_NONCE.pop(_ADDR, None)
    ex._VOLLEY_LOCKS.pop(_ADDR, None)


_saved_env = {k: os.environ.get(k) for k in
              ("SPEC_FIRE_EOA_KEY_2", "SPEC_FIRE_EOA_KEY_3",
               "SPEC_FIRE_EOA_KEY_4", "SPEC_FIRE_EOA_KEY_5")}
try:
    from eth_account import Account
    os.environ["SPEC_FIRE_EOA_KEY_2"] = "0x" + "11" * 32
    os.environ["SPEC_FIRE_EOA_KEY_3"] = "0x" + "22" * 32
    os.environ.pop("SPEC_FIRE_EOA_KEY_4", None)
    os.environ["SPEC_FIRE_EOA_KEY_5"] = "0x" + "33" * 32   # за пропуском — не берём
    ex._VOLLEY_ACCTS = None
    accs = ex.volley_accounts()
    assert len(accs) == 2, accs
    assert accs[0][0] == Account.from_key("0x" + "11" * 32).address
    assert all(a in ex._VOLLEY_LOCKS for a, _ in accs)
    print("UNIT_OK: volley_accounts: env-ключи подряд до пропуска, адреса деривнуты")
finally:
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    ex._VOLLEY_ACCTS = None

# ---- Часть C: интеграция через _window_loop (реальные треды) ----
# каскад активен + жир $1.5M -> каждая проба главного кошелька тянет залп 2 ног;
# без каскада — ровно ноль ног при живых пробах
_orig_facts = sfl_mod._facts_fresh
sfl_mod._facts_fresh = lambda now_wall=None: True
try:
    RET = {"v": 0.0}
    sent.clear()
    VLEGS.clear()
    sf = make(ret_v=RET)
    sf._arm_ts = [sf._now()] * 3          # +1 арм ниже = порог 4
    sf.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
    time.sleep(0.2)
    RET["v"] = -0.0011
    time.sleep(1.0)
    sf.handle_event({"event": "push", "feed": "BTC"})
    time.sleep(0.5)                       # дать залповым тредам дожать ноги
    n_main, n_legs = len(sent), len(VLEGS)
    assert n_main >= 3, n_main
    assert n_legs == 2 * n_main, (n_main, n_legs)
    assert all(c == "0xdeadbeef" for _, c, _, _ in VLEGS)
    print("UNIT_OK: интеграция — залп 2 ног на каждую пробу (main=%d legs=%d)" % (n_main, n_legs))

    sent.clear()
    VLEGS.clear()
    RET2 = {"v": 0.0}
    sf2 = make(ret_v=RET2)                # армов мало -> каскада нет -> ноль ног
    sf2.handle_event({"event": "arm", "feed": "BTC", "dir": "down"})
    time.sleep(0.2)
    RET2["v"] = -0.0011
    time.sleep(0.7)
    sf2.handle_event({"event": "push", "feed": "BTC"})
    time.sleep(0.3)
    assert len(sent) >= 2 and not VLEGS, (len(sent), VLEGS)
    print("UNIT_OK: вне каскада тир молчит при живых пробах (main=%d)" % len(sent))
finally:
    sfl_mod._facts_fresh = _orig_facts

print("ПРОШЛИ: каскадный тир (залповый слой)")
