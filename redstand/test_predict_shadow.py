"""Юнит-тесты PREDICT-слоя (порт Katana v5, chain/predict.py + chain/pricefeed.py, SHADOW).
Движок чистый (без I/O): часы инжектированы. Проверяет: (A) bootstrap+arm по ПЕР-ФИДОВОМУ
порогу (BTC 0.09% vs DOGE 0.45%), (B) disarm на ретрейсе (гистерезис), (C) confirmed с lead_s
на пуше под armed + ре-якорь, (D) push без arm (recall-мисс), (E) falsepos по HOLD-капу +
suppression до ретрейса, (F) driver: первый poll = baseline (не пуш), сдвиг updatedAt = пуш,
(G) stale-заморозка: слепой фид не armит и не зреет falsepos, freeze_gap сдвигает часы,
(H) make_poll_fn: декод latestRoundData из aggregate3-байтов. Без сети."""
import sys
sys.path.insert(0, ".")
import chain.predict as P


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _engine(clock=None):
    return P.PredictEngine(now=clock or _Clock())


def test_bootstrap_and_per_feed_arm():
    # (A) BTC armит на 0.09% (dev 0.1%), DOGE на том же движении НЕ armит (порог 0.45%)
    e = _engine()
    assert e.bootstrap("BTCUSDT", 100000.0)[0]["event"] == "bootstrap"
    assert e.bootstrap("DOGEUSDT", 0.10)[0]["event"] == "bootstrap"
    evs = e.on_mid("BTCUSDT", 100000.0 * 1.00095)
    assert [x["event"] for x in evs] == ["arm"] and evs[0]["dir"] == "up"
    assert e.on_mid("DOGEUSDT", 0.10 * 1.00095) == []          # 0.095% < 0.45%
    evs = e.on_mid("DOGEUSDT", 0.10 * 1.0046)
    assert [x["event"] for x in evs] == ["arm"]


def test_disarm_on_retrace():
    # (B) ретрейс ниже disarm (0.7*dev) -> disarm; между arm и disarm — тишина
    e = _engine()
    e.bootstrap("BTCUSDT", 100000.0)
    e.on_mid("BTCUSDT", 100095.0)
    assert e.on_mid("BTCUSDT", 100085.0) == []                 # внутри гистерезиса
    evs = e.on_mid("BTCUSDT", 100050.0)                        # 0.05% < 0.07%
    assert [x["event"] for x in evs] == ["disarm"]
    assert e.armed_symbols() == set()


def test_confirmed_with_lead_and_reanchor():
    # (C) пуш под armed -> confirmed c lead_s; anchor -> текущий mid; return ~0
    clk = _Clock()
    e = _engine(clk)
    e.bootstrap("BTCUSDT", 100000.0)
    e.on_mid("BTCUSDT", 100095.0)
    clk.t += 30.0
    evs = e.on_push("BTCUSDT")
    assert evs[0]["event"] == "confirmed" and abs(evs[0]["lead_s"] - 30.0) < 1e-6
    assert e.feeds["BTCUSDT"].anchor == 100095.0
    assert abs(e.ret("BTCUSDT")) < 1e-9


def test_push_without_arm():
    # (D) пуш без arm (движение под порогом) -> event=push, recall-мисс
    e = _engine()
    e.bootstrap("BTCUSDT", 100000.0)
    e.on_mid("BTCUSDT", 100040.0)
    evs = e.on_push("BTCUSDT")
    assert evs[0]["event"] == "push" and evs[0]["was_armed"] is False


def test_falsepos_cap_and_suppression():
    # (E) armed + отклонение держится дольше HOLD -> falsepos; ре-арм подавлен до ретрейса
    clk = _Clock()
    e = P.PredictEngine(now=clk, falsepos_window=600.0)
    e.bootstrap("BTCUSDT", 100000.0)
    e.on_mid("BTCUSDT", 100095.0)
    clk.t += 601.0
    evs = e.tick()
    assert [x["event"] for x in evs] == ["falsepos"]
    assert e.on_mid("BTCUSDT", 100096.0) == []                 # suppressed
    e.on_mid("BTCUSDT", 100040.0)                              # ретрейс снял подавление
    evs = e.on_mid("BTCUSDT", 100095.0)
    assert [x["event"] for x in evs] == ["arm"]


def test_driver_poll_baseline_then_push():
    # (F) первый poll после ленивого якоря = baseline (НЕ пуш); сдвиг updatedAt = пуш
    clk = _Clock()
    e = _engine(clk)
    reads = {"BTCUSDT": (111, 100000.0)}
    d = P.PredictDriver(e, mid_fn=lambda s: (100000.0, clk.t), poll_fn=lambda: dict(reads),
                        log=lambda m: None, now=clk, sleep=lambda s: None, wall=lambda: 0.0)
    d.step()                                                    # bootstrap с poll (anchor из price)
    assert e.feeds["BTCUSDT"].anchor == 100000.0
    d._last_poll = None
    d.step()                                                    # тот же updatedAt -> не пуш
    assert d.stats["confirmed"] == 0
    reads["BTCUSDT"] = (222, 100095.0)
    d._last_poll = None
    d.step()                                                    # сдвиг -> push (без arm)
    assert e.feeds["BTCUSDT"].anchor is not None


def test_driver_stale_freeze():
    # (G) протухший mid: фид заморожен (нет arm), falsepos не зреет; recovery сдвигает часы
    clk = _Clock()
    e = _engine(clk)
    e.bootstrap("BTCUSDT", 100000.0)
    mid_box = {"v": (100095.0, clk.t)}
    d = P.PredictDriver(e, mid_fn=lambda s: mid_box["v"] if s == "BTCUSDT" else None,
                        poll_fn=lambda: {}, log=lambda m: None,
                        stale_sec=10.0, now=clk, sleep=lambda s: None, wall=lambda: 0.0)
    d.step()                                                    # свежий mid -> arm
    assert e.armed_symbols() == {"BTCUSDT"}
    arm_mono0 = e.feeds["BTCUSDT"].arm_mono
    clk.t += 100.0                                              # mid протух (ts старый)
    d.step()
    assert d.stats["stale_episodes"] == 1
    clk.t += 700.0                                              # слепые 700с НЕ зреют falsepos
    d.step()
    assert d.stats["falsepos"] == 0
    mid_box["v"] = (100095.0, clk.t)                            # свежий mid -> recovery
    d.step()
    assert e.feeds["BTCUSDT"].arm_mono > arm_mono0              # freeze_gap сдвинул часы


def test_make_poll_fn_decodes_latest_round():
    # (H) декод aggregate3-ответа latestRoundData: answer (int256) + updatedAt
    import chain.multicall as mc
    data = (b"\x00" * 31 + b"\x01"                              # roundId
            + (6876912).to_bytes(32, "big")                     # answer (DOGE 0.06876912e8)
            + b"\x00" * 32                                      # startedAt
            + (1784800000).to_bytes(32, "big")                  # updatedAt
            + b"\x00" * 32)
    orig = mc.aggregate3
    mc.aggregate3 = lambda rpc, calls: [(True, data)]
    try:
        poll = P.make_poll_fn(None, {"DOGEUSDT": "0x" + "ab" * 20})
        out = poll()
        assert out == {"DOGEUSDT": (1784800000, 0.06876912)}
    finally:
        mc.aggregate3 = orig


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                fails += 1
                print("FAIL", name, "%s: %s" % (type(e).__name__, e))
    sys.exit(1 if fails else 0)
