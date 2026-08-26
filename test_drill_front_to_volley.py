"""ДРЕЛЬ «ФРОНТОВАЯ ЦЕЛЬ → ЗАЛП» (п.1 пакета ревьюера 26.08).

Класс, ради которого дрель существует: ШОВ. Самодедлок 23-26.08 показал, что каждое звено
может быть «зелёным по себе», а цепочка — мёртвой: predict жив, окно живо, флот сыт, но
между ними лок — и залпа нет 2.5 суток. Юниты на звенья этого не ловят ПО ПОСТРОЕНИЮ.

Дрель гонит СИНТЕТИЧЕСКУЮ цель через ВСЮ цепочку на ЖИВЫХ объектах:
  хотсет (shared) → flip_dist-пуш детонатора → arm-событие predict → окно (_window_loop)
  → admission тиров → send_fn-ДВОЙНИК (сети нет, денег нет)
ИНВАРИАНТ: цель в хотсете + движение ⇒ ЗАЛП за N секунд.

Красный-на-базе строится ВНУТРИ самой дрели: каждое звено по очереди «отключается» (мок без
пуша), и дрель обязана краснеть — это доказывает, что она меряет ЦЕПОЧКУ, а не заглушки.
"""
import sys
import threading
import time
import pathlib as _pl
from types import SimpleNamespace

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.spec_fire_live as sfl  # noqa: E402
import chain.predict as pr  # noqa: E402

MID, TGT = "0xmid", "0xfrontTarget"
_sv_label = sfl._label_of
sfl._label_of = lambda s: s


def build(*, hotset=True, det_push=True, arm_event=True, window_ok=True):
    """Собирает боевой SpecFireLive со швами-двойниками. Флаги отключают ЗВЕНЬЯ (для красного)."""
    o = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    o.stats = {"windows_armed": 0, "probes": 0, "wins": 0}
    o._lock = threading.Lock()
    o._board_lock = threading.Lock()
    o.logs = []
    o._log = o.logs.append
    o._now = time.monotonic
    o._sleep = lambda s: time.sleep(min(s, 0.02))
    o.dev_by = {"XRP": 0.005}
    o.ret_fn = lambda f: -0.004 if window_ok else 0.0     # движение 0.4% (ниже универс. 0.5%)
    o._windows = {}
    o._armskip_ts = {}
    o._arm_ts = []
    o._cooldown_until = 0.0
    o._cooldown_feed = {}
    o._push_ts = {}
    o._win_birth = {}
    o._win_pushes = {}
    o._board = {}
    o._board_next_try = {}
    o._board_v6 = {}
    o._plan_key_by_feed = {}
    o._ws_handoff_ts = {}
    o._board_probe_cache = {}
    o._group_extra = {}
    o._owed = {}
    o._det_capped = set()
    o._prio = {}                                           # арбитр расписания (пакет B)
    o._pos_logged = {}
    o._audit_base_fee = None
    o._arm_fail_series = {}
    o.probe_board = {}                                     # доска-отметка для потребителей
    o.pending_fire = {}                                    # A2: атомарный handoff
    o.pending_healthy_fn = None                            # A6: pending-детект не участвует
    o._det_key_by_feed = {}
    o._alert = None
    o._fire_engine = None
    o.cfg = SimpleNamespace(eth_price_usd=4000.0, liquidator_address="0xliq", tip_gwei=0.05,
                            wallet_key="0xk", gas_limit_floor=3_000_000)
    o.rpc = SimpleNamespace(rpc_url="http://node", _web3=lambda: None)
    m = SimpleNamespace(collateral_token="XRP", loan_token="USDC", bonus=0.1268)
    groups = {MID: [TGT]} if hotset else {}
    o.shared = SimpleNamespace(lock=threading.Lock(), groups=groups,
                               debt_by={(MID, TGT): 90_000.0} if hotset else {},
                               by_id={MID: m},
                               hf_by={(MID, TGT): 1.0009} if hotset else {})
    # ШВЫ-ДВОЙНИКИ: сеть и деньги не участвуют
    fired = []
    o._build_fn = lambda mid, b, d: {"calldata": "0xab", "gas": 1_000_000}
    o._send_fn = lambda cd, gas: (fired.append(time.monotonic()) or "0x%d" % len(fired))
    o._receipt_fn = lambda h: None
    o._volley_accounts_fn = lambda: []
    o._volley_send_fn = lambda *a, **k: None
    o.fired = fired
    if not det_push:                                       # звено «детонатор публикует цель»
        o._min_flip_dist = lambda f, d: None
    if not arm_event:
        o._top_fat = lambda f, d, max_dist=None, k=1: ([], None, None)
    return o


def run_drill(o, timeout=6.0):
    """Гонит цепочку: пуш детонатора -> arm-событие -> окно -> ждём ЗАЛП (send_fn-двойник)."""
    o._min_flip_dist("XRP", "down") if hasattr(o, "_min_flip_dist") else None
    o.handle_event({"event": "arm", "feed": "XRP", "dir": "down", "ret_pct": -0.4})
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if o.fired:
            return time.monotonic() - t0
        time.sleep(0.05)
    return None


# ---- 1. ПОЛНАЯ ЦЕПОЧКА: цель в хотсете + движение ⇒ ЗАЛП ----
o = build()
dt = run_drill(o)
check("ИНВАРИАНТ: фронтовая цель в хотсете + движение -> ЗАЛП (send_fn-двойник дёрнут)",
      dt is not None, "залпа нет за 6с; логи: %s" % o.logs[-3:])
if dt is not None:
    check("залп пришёл быстро (<3с от арма)", dt < 3.0, "%.2fс" % dt)
check("окно армлено (счётчик)", o.stats.get("windows_armed", 0) >= 1, o.stats)
check("детонатор опубликовал цель окна", (o._det_key_by_feed or {}).get("XRP") is not None,
      o._det_key_by_feed)

# ---- 2-4. КРАСНЫЙ-НА-БАЗЕ ПО ЗВЕНЬЯМ: снятое звено обязано убить залп ----
o2 = build(hotset=False)
check("КРАСНЫЙ: нет цели в хотсете -> залпа нет (дрель меряет цепочку, не заглушку)",
      run_drill(o2, timeout=2.0) is None)
o3 = build(det_push=False)
d3 = run_drill(o3, timeout=2.0)
check("КРАСНЫЙ: детонатор не публикует цель -> порог универсальный, движения 0.4% мало",
      d3 is None, "залп всё равно был за %.2fс" % (d3 or 0))
o4 = build(arm_event=False)
check("КРАСНЫЙ: _top_fat пуст (арм не проходит) -> залпа нет",
      run_drill(o4, timeout=2.0) is None)
o5 = build(window_ok=False)
check("КРАСНЫЙ: движения нет (ret=0) -> залпа нет",
      run_drill(o5, timeout=2.0) is None)

# ---- 5. Гард дрейфа: дрель ходит по БОЕВЫМ методам, не по копиям ----
_SRC = open("test_drill_front_to_volley.py").read()
# литералы собираются из кусков: иначе проверка находит САМУ СЕБЯ в тексте файла
_needle_def = "def _window" + "_loop"
_needle_loop = "while not stop" + ".is_set()"
check("дрель зовёт БОЕВОЙ handle_event; своей копии петли окна в ней НЕТ",
      "o.handle_event({" in _SRC and _needle_def not in _SRC and _needle_loop not in _SRC)
check("сеть и деньги не участвуют (только швы-двойники)",
      "_send_fn = lambda" in _SRC and "_receipt_fn = lambda" in _SRC)

for _o in (o, o2, o3, o4, o5):                             # гасим окна дрели
    for _st in list(_o._windows.values()):
        _st.set()
sfl._label_of = _sv_label
print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: цепочка фронт→залп жива целиком; снятие ЛЮБОГО звена её убивает"))
sys.exit(1 if FAILS else 0)
