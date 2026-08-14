"""Р1: инвариант «одна цель — одна подготовка» при ПАРАЛЛЕЛЬНОМ входе.

ОТКУДА ГОНКА. Рынок может висеть под ДВУМЯ агрегаторами (multi-leg, 07-23: `agg=` в DIAG —
это атрибуция ноги-триггера). Обе ноги, транслируя в одном блоке, спавнят по своему
`_process_transmit`, и оба потока видят один и тот же (mid, borrower). Пока подготовка была
последовательной, окно перекрытия было узким; параллельная подготовка его РАСШИРЯЕТ — теперь
26 целей готовятся одновременно, и обе ноги стоят в этом окне целиком.

ЦЕНА ДУБЛЯ. Комментарий в коде говорит прямо: «prepare НЕ дублируем (2-я tx сожгла бы
nonce+газ)». Плюс 632мс сети на цель, которую и так готовят.

ЧТО ЗАПЕРТО. При двух одновременных транзитах по одной цели:
  1. prepare зовётся РОВНО ОДИН раз на (рынок, заёмщик);
  2. в dispatch цель уходит РОВНО ОДИН раз;
  3. вторая нога не «падает молча» — она получает свои ДРУГИЕ цели целиком;
  4. заявка снимается в finally: подготовка, кончившаяся ИСКЛЮЧЕНИЕМ, не запирает цель
     навсегда (иначе один сбой Kyber выключил бы заёмщика до перезапуска процесса).

ВТОРАЯ ЛИНИЯ (не заменяет эту). В боевом `dispatch_liquidations` есть `_RECENT_SENDS` под
`_SEND_LOCK` — дубль ОТПРАВКИ он отсечёт. Но платить 632мс дважды он не мешает, а в дрели
dispatch_fn инжектирован, поэтому здесь проверяется именно ЗАЯВКА на подготовку.
"""
import sys
import threading
import time
import types

import chain.hotpath as hp

MID = "0x" + "ab" * 32
AGG1 = "0x" + "11" * 20      # нога A
AGG2 = "0x" + "33" * 20      # нога B (тот же рынок — multi-leg)
ORACLE = "0x" + "22" * 20
LLTV = 860000000000000000

_FAILS = []


def _check(cond, msg):
    print(("  OK: " if cond else "  ПРОВАЛ: ") + msg)
    if not cond:
        _FAILS.append(msg)


def _log():
    import logging
    lg = logging.getLogger("test-one-prep")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


def _mk_env(*, agg, borrowers, rec, prep_sleep=0.30, boom=()):
    shared = types.SimpleNamespace(
        lock=threading.Lock(),
        groups={MID: list(borrowers)},
        debt_by={(MID, b): 5000.0 for b in borrowers},
        debt_assets_by={},
    )
    store = types.SimpleNamespace(realized_today=lambda: {"net": 0.0, "gas": 0.0},
                                  log_action=lambda **kw: None)
    from strategy.guard import KillSwitch
    guard = types.SimpleNamespace(blocked_reason=lambda st: None,
                                  is_transient=KillSwitch.is_transient)
    alerter = types.SimpleNamespace(send_async=lambda *a, **k: None)
    cfg = types.SimpleNamespace(mode="live", hot_min_repaid_usd=2000.0, fast_lane=False,
                                positions_from_cache=False, max_inflight=64)

    def reads_fn(rpc, mid, bs_, preconf_rpc=None, src_out=None):
        if src_out is not None:
            src_out["src"] = "test"
        return (10**6, 10**6, [(b, 10**6, 10**6) for b in bs_])

    def prepare_fn(rpc, preconf_rpc, cfg_, mid, b, du, debt_assets, price, state=None):
        with rec["lock"]:
            rec["prep"].append(b)
        time.sleep(prep_sleep)            # окно, в котором вторая нога обязана увидеть заявку
        if b in boom:
            raise RuntimeError("Kyber quote timed out")   # подготовка упала — заявка обязана сняться
        return {"ok": True, "calldata": "0x", "net_usd": 5.0}

    def dispatch_fn(rpc, cfg_, ready, log=None, max_inflight=None):
        with rec["lock"]:
            for _m, _b, _p in ready:
                rec["disp"].append(_b)
        return [{"market_id": m, "borrower": b, "sent": True, "status": 1, "net_usd": 5.0,
                 "hash": "0xt", "gas_usd": 0.1} for m, b, _ in ready]

    return dict(rpc=None, preconf_rpc=None, cfg=cfg, feeds={agg: [MID]},
                meta={MID: (ORACLE, LLTV)}, shared=shared, store=store, guard=guard,
                alerter=alerter, log=_log(), reads_fn=reads_fn, prepare_fn=prepare_fn,
                dispatch_fn=dispatch_fn, px_hint=12345, hint_oracle=ORACLE)


def _new_rec():
    return {"lock": threading.Lock(), "prep": [], "disp": []}


def _race(borrowers_a, borrowers_b, block, prep_sleep=0.30, boom=()):
    """Два транзита по РАЗНЫМ ногам одного рынка, стартующие одновременно."""
    rec = _new_rec()
    ka = _mk_env(agg=AGG1, borrowers=borrowers_a, rec=rec, prep_sleep=prep_sleep, boom=boom)
    kb = _mk_env(agg=AGG2, borrowers=borrowers_b, rec=rec, prep_sleep=prep_sleep, boom=boom)
    gate = threading.Barrier(2)

    def _leg(agg, kw):
        gate.wait()
        try:
            hp._process_transmit(agg, block, 0, time.time(), **kw)
        except Exception as e:            # нога не имеет права утащить дрель за собой
            with rec["lock"]:
                rec.setdefault("err", []).append("%s: %s" % (type(e).__name__, e))

    ta = threading.Thread(target=_leg, args=(AGG1, ka))
    tb = threading.Thread(target=_leg, args=(AGG2, kb))
    ta.start(); tb.start(); ta.join(30); tb.join(30)
    return rec


_orig_flips = hp._flips
hp._flips = lambda price, lltv, tba, tbs, positions: [
    (b, types.SimpleNamespace(hf=0.98), du) for (b, bs, col, du) in positions]
try:
    # 1. ПОЛНОЕ пересечение: обе ноги видят одну и ту же цель
    print("1. Две ноги одного рынка, ОДНА общая цель")
    rec = _race(["0xa1"], ["0xa1"], 300)
    _check(rec["prep"].count("0xa1") == 1,
           "prepare для 0xa1 ровно один (факт %d: %s)" % (rec["prep"].count("0xa1"), rec["prep"]))
    _check(rec["disp"].count("0xa1") == 1,
           "dispatch для 0xa1 ровно один (факт %d)" % rec["disp"].count("0xa1"))

    # 2. ЧАСТИЧНОЕ пересечение: общая цель одна, свои — у каждой ноги свои и не теряются
    print("2. Частичное пересечение: общая цель + свои цели у каждой ноги")
    rec = _race(["0xa1", "0xa2", "0xa3"], ["0xa1", "0xb2", "0xb3"], 301)
    _check(rec["prep"].count("0xa1") == 1,
           "общая цель подготовлена один раз (факт %d)" % rec["prep"].count("0xa1"))
    _check(sorted(set(rec["prep"])) == ["0xa1", "0xa2", "0xa3", "0xb2", "0xb3"],
           "ни одна СВОЯ цель ноги не пропала (факт %s)" % sorted(set(rec["prep"])))
    _check(len(rec["prep"]) == 5,
           "лишних подготовок нет: 5 уникальных целей = 5 подготовок (факт %d)" % len(rec["prep"]))
    _check(sorted(rec["disp"]) == ["0xa1", "0xa2", "0xa3", "0xb2", "0xb3"],
           "каждая цель отправлена ровно один раз (факт %s)" % sorted(rec["disp"]))

    # 3. Заявка снимается при ИСКЛЮЧЕНИИ в подготовке — цель не заперта навсегда
    print("3. Упавшая подготовка снимает заявку (иначе один сбой Kyber запирает заёмщика)")
    rec = _race(["0xa9"], [], 302, boom=("0xa9",))
    _check(not rec.get("err"), "исключение подготовки не утащило транзит (%s)" % rec.get("err"))
    rec2 = _race(["0xa9"], [], 303)
    _check(rec2["prep"].count("0xa9") == 1,
           "после падения цель СНОВА готовится (факт %d)" % rec2["prep"].count("0xa9"))
finally:
    hp._flips = _orig_flips

if _FAILS:
    print("\nКРАСНАЯ: провалов %d" % len(_FAILS))
    for m in _FAILS:
        print("  - %s" % m)
    sys.exit(1)
print("\nПРОШЛИ: одна цель — одна подготовка при параллельном входе")
