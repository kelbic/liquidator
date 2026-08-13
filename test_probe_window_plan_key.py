"""БЛОКЕР C (ревью р-6): атрибуция проб по ЯРЛЫКУ ФИДА не работает на БОЕВОМ тракте.

ЧТО БЫЛО. Правка р-5 привязала счётчики позиций к `meta[1][0]` — ярлыку фида. Но ярлык туда
кладёт `FireEngine._target`, а это ОДНО перезаписываемое поле: в него пишет ПОСЛЕДНИЙ тикнувший
`keepalive` любого окна каскада, тогда как доска в этот момент держит план ОДНОЙ цели. Значит
пробы, летящие под планом соседнего окна, приезжают с ЧУЖИМ ярлыком — и приёмка (б) читается
проваленной ровно на жирном окне (дрель L3 синтезатора: окно BTC несло no_agg=428 при
judged=428, то есть РОВНО как до правки).

ПОЧЕМУ ЭТОГО НЕ ЛОВИЛ СТЕНД р-5. `test_probe_window_parallel` звал `_note_probe_position` с
meta, СОБРАННОЙ РУКАМИ. Шов стоял ВЫШЕ дефекта: боевой производитель meta (`_engine_fat_keepalive`
-> `FireEngine._worker` -> `collector.track`) в стенде не исполнялся вовсе, и мутация «убить фид
в боевом производителе» оставляла стенд ЗЕЛЁНЫМ.

ЧТО ЗДЕСЬ. Настоящие `FireEngine` (пул тредов, токен-бакет, `_worker`), настоящий
`ReceiptCollector`, настоящий `_engine_fat_keepalive`, настоящие `_window_loop` двух окон
каскада, настоящий `_engine_result` -> `_note_probe_position`, настоящий `ChainIndex` на
записанном блоке 49890266. Подменены ТОЛЬКО данные и сокеты: сокет узла, I/O-шов резолва ног,
источник целей `_top_fat`, отправка пробы и партия квитанций.

ПРИЁМКА (б) ЧИТАЕТСЯ ПАРОЙ (и только парой): зрячее окно `probe_no_agg==0` ПРИ `probe_judged>=1`
И слепой сосед `probe_no_agg>=1` в ТОМ ЖЕ каскаде. Односторонний ноль зеленеет вакуумно — если
пробы перестанут учитываться совсем, ноль будет тем же нулём.
"""
import gzip
import json
import logging
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chain.chain_index as ci                                # noqa: E402
import chain.feeds as feeds                                   # noqa: E402
import chain.fire_engine as fe                                # noqa: E402
import chain.spec_fire_live as sfl                            # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
BN = 49890266
BOARD = "0x85bade2cdd375ab1b75f5c522b7989a12f939292"
AGG_PRIZE = "0x9491aedfe0da70eb110582b66b2edace28d82a5d"
AGG_LEG2 = "0x" + "c3" * 20
MID_SEEN = "0x" + "11" * 32          # рынок с резолвнутыми ногами: транзит судится ЗРЯЧЕ
MID_BLIND = "0x" + "22" * 32         # рынок без ног: суд законно ВСЛЕПУЮ
B_SEEN = "0x" + "aa" * 20
B_BLIND = "0x" + "bb" * 20

BLOCK = json.loads(gzip.open(FIX / "block_49890266.json.gz", "rt").read())
LOGS_FX = json.loads((FIX / "logs_49890266.json").read_text())["result"]
RECEIPTS = json.loads((FIX / "receipts_49890266.json").read_text())
H_FIRE = next(rc["transactionHash"] for rc in RECEIPTS if rc["to"].lower() == BOARD)[2:]


def _http(url, payload, timeout):
    m = payload["method"]
    if m == "eth_getLogs":
        return {"result": LOGS_FX}
    if m == "eth_getBlockByNumber":
        return BLOCK
    if m == "eth_getBlockReceipts":
        return {"result": RECEIPTS}
    if m == "eth_blockNumber":
        return {"result": hex(BN)}
    raise AssertionError(m)


idx = ci.ChainIndex("https://node.example/K", watch_to=[BOARD], http=_http,
                    now=lambda: 0.0, log_fn=lambda m: None)
idx.note_hash(H_FIRE, head=BN - 1)
idx.scan_block(BN)
FACTS = idx.block_facts(BN)
assert FACTS and len(FACTS["ours"]) == 33, (FACTS or {}).get("ours")

_sv_legs = feeds._resolve_legs
_mkt = lambda mid: types.SimpleNamespace(market_id=mid)                       # noqa: E731
try:
    feeds._resolve_legs = lambda rpc, mid, fb=None: (
        (AGG_PRIZE, [AGG_PRIZE, AGG_LEG2]) if mid == MID_SEEN else (None, []))
    feeds.resolve_feeds(None, [_mkt(MID_SEEN), _mkt(MID_BLIND)])
finally:
    feeds._resolve_legs = _sv_legs

_H, _F = sorted(FACTS["ours"].items(), key=lambda kv: kv[1]["index"])[0]
RCPT = ci.receipt_like(_F)
assert RCPT is not None, "квитанция из факта индекса обязана собираться — иначе дрель пуста"
_cfg = types.SimpleNamespace(liquidator_address=BOARD, gas_limit_est=500_000, tip_gwei=0.05,
                             sequencer_rpc="off", eth_price_usd=3000.0, rpc_url="off",
                             fast_lane_max_repaid_usd=500_000.0)
assert sfl._receipt_has_win(RCPT, BOARD) is not True, \
    "дрель обязана мерить ПОЗИЦИИ, а не победу: квитанция-заготовка не должна быть выигрышной"

_shared = types.SimpleNamespace(lock=threading.Lock(), groups={}, debt_by={}, by_id={})
TARGETS = {"BTC": (MID_SEEN, B_SEEN, 90_000.0), "ETH": (MID_BLIND, B_BLIND, 50_000.0)}


def cascade(kill_feed=False, kill_key=False):
    """ДВА ОКНА КАСКАДА + НАСТОЯЩИЙ движок огня. Доска сперва у слепого соседа, потом у зрячего.

    kill_feed / kill_key — МУТАЦИИ БОЕВОГО ПРОИЗВОДИТЕЛЯ meta (`_engine_fat_keepalive`):
    убрать из цели ярлык фида либо ключ плана. Первая проверяет, что атрибуция от ярлыка больше
    НЕ ЗАВИСИТ, вторая — что стенд краснеет, когда пропадает то, на чём атрибуция стоит.
    """
    log_rows, spy = [], []
    _sv = (sfl.SPEC_FIRE_V2_BOARD, fe.ENGINE_ON, ci._INDEX, sfl.SpecFireLive._top_fat,
           sfl.SpecFireLive._engine_fat_keepalive, sfl.SPEC_FIRE_POLL_SEC)
    ci._INDEX = idx
    try:
        sfl.SPEC_FIRE_V2_BOARD, fe.ENGINE_ON = True, True
        sfl.SPEC_FIRE_POLL_SEC = 0.02
        sfl.SpecFireLive._top_fat = lambda self, feed, d, max_dist=None, k=1: (
            [TARGETS[feed]], 0.0, None)
        if kill_feed or kill_key:
            _real_ka = _sv[4]

            def _mutated(self, feed):
                # МУТАЦИЯ ВНУТРИ БОЕВОГО ПРОИЗВОДИТЕЛЯ: тот же путь, но цель обеднена.
                plan = self._board_plan_live()
                eng = self._engine()
                if plan is None or eng is None:
                    return False
                eng.keepalive(target=((None if kill_feed else feed),
                                      (None if kill_key else plan[0]),
                                      (None if kill_key else plan[1]), 0.0))
                return True

            sfl.SpecFireLive._engine_fat_keepalive = _mutated
        live = sfl.SpecFireLive(rpc=None, cfg=_cfg, shared=_shared, ret_fn=lambda lbl: 0.0,
                                dev_by={"BTC": 0.001, "ETH": 0.001},
                                build_fn=lambda m, b, d: {"calldata": "0x00", "gas": 120_000},
                                send_fn=lambda c, g: None,
                                receipt_fn=lambda h: (0, 0.0), log_fn=log_rows.append)

        # СПИОН НА ЯРЛЫКЕ: что боевой тракт сказал бы редакции р-5. Считаем, у скольких проб
        # ярлык фида в meta НЕ СОВПАЛ с окном, чей план в этот момент стоял на доске.
        _real_note = live._note_probe_position

        def _note(h, r, meta=None):
            tgt = (meta or (None, None))[1] or ()
            spy.append((tgt[0] if len(tgt) > 0 else None,
                        (live._board or {}).get("key")))
            return _real_note(h, r, meta=meta)
        live._note_probe_position = _note

        sent = {"n": 0}

        def _send(addr):
            sent["n"] += 1
            return "%064x" % sent["n"]

        col = fe.ReceiptCollector(receipts_fn=lambda hs: {h: dict(RCPT) for h in hs},
                                  on_result=live._engine_result, on_timeout=live._engine_timeout,
                                  poll_sec=0.005, min_age_sec=0.0, timeout_sec=60.0, batch=64)
        eng = fe.FireEngine(legs_fn=lambda: ["0x" + "11" * 20, "0x" + "22" * 20],
                            send_fn=_send, collector=col, threads=2, rate=400.0, ttl=2.0)
        live._fire_engine = eng
        eng.start()

        stopA, stopB = threading.Event(), threading.Event()
        tA = threading.Thread(target=live._window_loop, args=("BTC", "down", stopA), daemon=True)
        tB = threading.Thread(target=live._window_loop, args=("ETH", "down", stopB), daemon=True)
        tA.start(); tB.start()
        time.sleep(0.25)                       # окна опубликовали свои планы (реальный prearm)

        def phase(mid, b, debt, sec=0.35):
            """Доска держит ОДИН план; тикают ОБА окна — как в каскаде."""
            live._board = {"key": (mid, b), "armed_ts": time.monotonic(), "debt": debt,
                           "until": time.monotonic() + 600.0,
                           "ready_at": time.monotonic() - 1.0, "verified": True,
                           "tx": "0x" + "ee" * 32, "dl": 0}
            t0, i = time.monotonic(), 0
            while time.monotonic() - t0 < sec:
                # ПОРЯДОК ТИКОВ ЧЕРЕДУЕТСЯ: окна каскада — независимые треды, и кто тикнул
                # последним, решает случай. Ровно поэтому ярлык фида и не принадлежит пробе.
                i += 1
                for f in (("BTC", "ETH") if i % 2 else ("ETH", "BTC")):
                    live._engine_fat_keepalive(f)
                time.sleep(0.005)

        phase(MID_BLIND, B_BLIND, 50_000.0)    # доску держит СЛЕПОЙ сосед
        phase(MID_SEEN, B_SEEN, 90_000.0)      # ...потом ЗРЯЧЕЕ жирное окно
        eng.stop()                             # ...и вместе с ним встаёт тред коллектора
        for _ in range(400):                   # поэтому ДОБИРАЕМ его настоящим tick() руками:
            if col.pending() == 0:             # пробы, отправленные перед стопом, обязаны быть
                break                          # разобраны, иначе дельта окна судит половину
            col.tick()
            time.sleep(0.005)
        drained = col.pending()
        stopB.set(); tB.join(timeout=5)
        stopA.set(); tA.join(timeout=5)
    finally:
        (sfl.SPEC_FIRE_V2_BOARD, fe.ENGINE_ON, ci._INDEX, sfl.SpecFireLive._top_fat,
         sfl.SpecFireLive._engine_fat_keepalive, sfl.SPEC_FIRE_POLL_SEC) = _sv
        fe._COLLECTORS[:] = [c for c in fe._COLLECTORS if c is not col]
    out = {}
    for m in [x for x in log_rows if "window close" in x]:
        out[m.split("feed=")[1].split(" ")[0]] = m.split("окно_позиции=")[1].split(" stats=")[0]
    return live, out, spy, sent["n"], drained


logging.getLogger("liquidator").setLevel(logging.CRITICAL)
live, win, spy, n_sent, drained = cascade()

# ---------------------------------------------------------------- 0. ДРЕЛЬ ВООБЩЕ РАБОТАЕТ
assert n_sent > 20 and drained == 0, \
    ("движок обязан был отстрелять пробы и коллектор — их разобрать: отправлено=%d, "
     "неразобранных=%d" % (n_sent, drained))
assert set(win) == {"BTC", "ETH"}, win
_judged = live.stats.get("probe_judged", 0)
assert _judged > 10, ("судов позиций обязано быть много — иначе числа ниже ни о чём: %s"
                      % live.stats)

# ------------------------------------- 1. РЕПРО: ЯРЛЫК ФИДА В БОЕВОМ ТРАКТЕ НЕ ПРИНАДЛЕЖИТ ПРОБЕ
# Владелец плана известен: это окно, опубликовавшее ключ. Считаем, у скольких проб ярлык фида
# в meta указывает НЕ на владельца плана. Это и есть механика, которой была убита правка р-5.
PKEY = lambda m, b: (str(m).lower(), str(b).lower())            # noqa: E731 — свой, не из кода
_owner = {PKEY(*TARGETS[f][:2]): f for f in TARGETS}
_mis = [1 for lbl, bkey in spy if bkey and _owner.get(PKEY(*bkey)) != lbl]
assert len(_mis) > 0, \
    ("дрель обязана ВОСПРОИЗВОДИТЬ дефект: если ярлык всегда совпадает с владельцем плана, "
     "она не проверяет ничего (проб %d)" % len(spy))
# ...и ЧТО ИМЕННО напечатала бы редакция р-5 на этом же прогоне: пробы с ярлыком «BTC»,
# улетевшие под СЛЕПЫМ планом соседа, — это и есть чужой probe_no_agg в дельте жирного окна.
_p5 = len([1 for lbl, bkey in spy
           if lbl == "BTC" and bkey and PKEY(*bkey) == PKEY(MID_BLIND, B_BLIND)])
assert _p5 > 0, ("если чужих слепых судов под ярлыком BTC не случилось, дрель не воспроизводит "
                 "блокер C: %d проб" % len(spy))
print("UNIT_OK: РЕПРО блокера C — у %d из %d проб ярлык фида в meta НЕ принадлежит владельцу "
      "плана (ярлык ставит последний тикнувший keepalive, план на доске один); редакция р-5 "
      "на этом же прогоне засчитала бы окну BTC %d ЧУЖИХ слепых судов"
      % (len(_mis), len(spy), _p5))

# ---------------------------------------------------------------- 2. ПРИЁМКА (б) — ПАРОЙ
_btc, _eth = win["BTC"], win["ETH"]
assert "'probe_no_agg': 0" in _btc and "'probe_judged': 0" not in _btc, \
    ("ЗРЯЧЕЕ окно обязано показать no_agg=0 ПРИ judged>=1 — ноль в одиночку зеленеет "
     "вакуумно: %s" % _btc)
assert "'probe_no_agg': 0" not in _eth and "'probe_judged': 0" not in _eth, \
    ("СЛЕПОЙ сосед обязан показать СВОЙ законный no_agg>=1 при своих судах — иначе пара не "
     "прочитана и ноль зрячего окна ничем не подтверждён: %s" % _eth)
assert "probe_foreign_plan" in _btc and "probe_foreign_plan" in _eth, \
    ("пробы под ЧУЖИМ планом обязаны быть НАЗВАНЫ отдельным бакетом, иначе дельта окна "
     "молчит о том, куда делись пробы каскада: %s | %s" % (_btc, _eth))
assert "'probe_foreign_plan': 0" not in _btc, \
    ("в этом каскаде доску держал сосед — окно BTC обязано это ВИДЕТЬ числом: %s" % _btc)
print("UNIT_OK: приёмка (б) ПАРОЙ — зрячее окно BTC %s; слепой сосед ETH %s" % (_btc, _eth))

# ------------------------------------------- 3. МУТАЦИЯ M1: убить ЯРЛЫК ФИДА в боевом meta
# На коде р-5 это оставляло стенд ЗЕЛЁНЫМ (пробы уезжали в None-бакет, и ноль в no_agg читался
# как «всё сужено зряче»). Теперь атрибуция от ярлыка не зависит ВОВСЕ — числа обязаны совпасть.
_l2, win2, _spy2, _n2, _dr2 = cascade(kill_feed=True)
assert "'probe_no_agg': 0" in win2["BTC"] and "'probe_judged': 0" not in win2["BTC"], \
    ("атрибуция обязана быть НЕЗАВИСИМА от ярлыка фида — в этом и состоит блокер C: %s"
     % win2["BTC"])
assert "'probe_no_agg': 0" not in win2["ETH"], win2["ETH"]
print("UNIT_OK: мутация M1 (ярлык фида убит в БОЕВОМ производителе meta) числа приёмки НЕ "
      "меняет — BTC %s, ETH %s: атрибуция стоит на ключе плана, а не на ярлыке"
      % (win2["BTC"], win2["ETH"]))

# ------------------------------------------ 4. МУТАЦИЯ M1': убить КЛЮЧ ПЛАНА в боевом meta
# Критерий годности стенда: он обязан КРАСНЕТЬ, когда пропадает то, на чём атрибуция стоит.
_l3, win3, _spy3, _n3, _dr3 = cascade(kill_key=True)
assert "'probe_judged': 0" in win3["BTC"] and "'probe_no_agg': 0" in win3["BTC"], \
    ("без ключа плана суды не имеют права приписываться окну: %s" % win3["BTC"])
assert "'probe_no_key_proc': 0" not in win3["BTC"], \
    ("пробы без ключа обязаны быть НАЗВАНЫ (незнание закрывает гард), а не раствориться: %s"
     % win3["BTC"])
print("UNIT_OK: мутация M1' (ключ плана убит в БОЕВОМ производителе meta) КРАСНИТ приёмку: "
      "BTC %s — judged=0 при названном probe_no_key_proc" % win3["BTC"])

print("ПРОШЛИ: приёмка (б) считается по ключу ДОСКИ и читается ПАРОЙ (зрячее окно + слепой "
      "сосед); боевой производитель meta исполняется настоящий")
