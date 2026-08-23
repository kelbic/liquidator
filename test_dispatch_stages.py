"""СТАДИИ dispatch-тракта (пост-мортем $128k, блок 50302440). Приёмка:
(а) прибор верно называет виновника на СИНТЕТИКЕ с известной раскладкой (реплей 50302440:
    detect→prep 259мс, prep→sent 166мс — виновник detect→prep);
(б) другая раскладка -> другой виновник (прибор не приколочен);
(в) негативный контроль: меток нет/одна -> строка честно молчит «—»;
(г) ИСПОЛНЯЕМЫЕ обе боевые ветки (урок П.3: стенд исполняет, не грепает) — dispatch_batch
    ставит sent/ack по хэшу, а вердиктовая ветка hotpath дописывает detect/prep и печатает
    строку СТАДИИ.
"""
import sys
import time
import pathlib as _pl
from types import SimpleNamespace

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.stage_probe as sp  # noqa: E402


def _reset():
    with sp._LOCK:
        sp._MARKS.clear(); sp._ORDER.clear(); sp._FEED.clear(); sp._BLOCK.clear()


# ---- (а) реплей 50302440 синтетикой: виновник detect→prep ----
_reset()
T = 1_000_000.0
H = "0x8edd9824a6a9177adbfb136e05b5e37cb5fc643221c815e042324c090ef73b44"
sp.stamp_dispatch(H, {"detect": T, "prep": T + 0.259, "sent": T + 0.425, "ack": T + 0.470})
line, (worst, ms) = sp.format_dispatch(sp.profile(H))
check("реплей 50302440: виновник detect→prep ~259мс",
      worst == "detect→prep" and 255 <= ms <= 265, (worst, ms))
check("строка несёт все переходы и сумму",
      "detect→prep" in line and "prep→sent" in line and "sent→ack" in line
      and "всего 470мс" in line, line)

# ---- (б) другая раскладка -> другой виновник ----
_reset()
sp.stamp_dispatch(H, {"detect": T, "prep": T + 0.020, "sent": T + 0.600, "ack": T + 0.620})
_, (worst2, ms2) = sp.format_dispatch(sp.profile(H))
check("медленная отправка -> виновник prep→sent", worst2 == "prep→sent" and 575 <= ms2 <= 585,
      (worst2, ms2))

# ---- (в) негативный контроль: молчание вместо выдумки ----
_reset()
check("меток нет -> «—»", sp.format_dispatch(sp.profile(H)) == ("—", (None, None)))
sp.stamp_dispatch(H, {"sent": T})
check("одна метка -> «—» (прибор молчит)",
      sp.format_dispatch(sp.profile(H)) == ("—", (None, None)))
_reset()
sp.stamp_dispatch(H, {"detect": T, "prep": None, "sent": T + 0.1})
_l3, (_w3, _m3) = sp.format_dispatch(sp.profile(H))
check("None-метка не пишется, цепь считается по имеющимся",
      _w3 == "detect→sent" and "prep" not in _l3, (_l3, _w3))
check("лестничные стадии не смешиваются с dispatch",
      sp.format_profile(sp.profile(H))[0] == "—")

# ---- (г1) ИСПОЛНЕНИЕ боевой ветки execute.dispatch_batch: sent/ack по хэшу ----
_reset()
import chain.execute as ex  # noqa: E402

TXH = "0xfeed0000000000000000000000000000000000000000000000000000000000ff"
_sv_inf = ex._inflight_add
_sv_local = dict(ex._LOCAL_NONCE)
try:
    ex._inflight_add = lambda n: None
    BOT = "0x" + "b0" * 20
    ex._LOCAL_NONCE[BOT] = 7                       # локальный nonce: dispatch не пойдёт в сеть
    cfg = SimpleNamespace(liquidator_address="0xLiq", wallet_key="0xk", tip_gwei=0.05,
                          gas_limit_est=1_000_000, max_inflight=4, eth_price_usd=4000.0,
                          sequencer_rpc="off", mode="execute")
    _w3 = SimpleNamespace(eth=SimpleNamespace(
        account=SimpleNamespace(from_key=lambda k: SimpleNamespace(address=BOT))))
    item = ("0xmid", "0xborrower", {"ok": True, "calldata": "0xabcd", "gas": 1_000_000,
                                    "net_usd": 100.0, "profit_usd": 110.0, "cost_usd": 10.0,
                                    "debt_usd": 1000.0})
    res = ex.dispatch_liquidations(SimpleNamespace(_web3=lambda: _w3), cfg, [item], log=None,
                                   max_inflight=4, wait_receipts=False,
                                   send_fn=lambda *a, **k: {"hash": TXH, "status": None})
    prof = sp.profile(TXH)
    check("ИСПОЛНЕНО execute: dispatch поставил d_sent и d_ack на реальном хэше",
          "d_sent" in prof and "d_ack" in prof and prof["d_ack"] >= prof["d_sent"], prof)
except Exception as e:                                       # noqa: BLE001
    check("ИСПОЛНЕНО execute: dispatch поставил метки", False, "%s: %s" % (type(e).__name__, e))
finally:
    ex._inflight_add = _sv_inf
    ex._LOCAL_NONCE.clear(); ex._LOCAL_NONCE.update(_sv_local)

# ---- (г2) гарды проводки боевых точек ----
_H = open("chain/hotpath.py").read()
_E = open("chain/execute.py").read()
check("execute: метки sent/ack стоят вокруг send в dispatch-петле",
      '_sd(res["hash"], {"sent": _t_snd, "ack": time.time()})' in _E)
check("hotpath: detect/prep дописываются на вердикте и печатается строка СТАДИИ",
      '_sd2(r["hash"], {"detect": t, "prep": _t_prep_wall})' in _H
      and 'log.info("HOT СТАДИИ tx=%s' in _H)
check("hotpath: метка prep есть в ОБОИХ путях (инкрементальный — боевой по умолчанию)",
      '_incr_state["t_prep_wall"] = time.time()' in _H
      and '_t_prep_wall = _incr_state.get("t_prep_wall")' in _H
      and "_t_prep_wall = time.time()" in _H)
check("прибор сбоку: обе точки под своим try, отказ глотается",
      _E.count("from chain.stage_probe import stamp_dispatch as _sd") == 1
      and "except Exception:                             # noqa: BLE001" in _E)

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: dispatch-тракт профилируется — виновник назван, без меток молчит"))
sys.exit(1 if FAILS else 0)
