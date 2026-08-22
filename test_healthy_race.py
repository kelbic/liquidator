"""П.1 HEALTHY-RACE POLICY (22.08): приёмка по спеке ревьюера, красным-на-базе.

(а) реплей 05:11:04 ЦЕПНЫМИ призами: мелкий класс ($14-109) режется полом $300 — противоречие
    со спекой «все четыре стреляют» НАЗВАНО и поднято ревьюеру; жирная $4436 бидит;
(б) реплей 43 healthy-race замера #35 (30 трупов) -> трупы отсеяны, живых <=13;
(в) негативный контроль: спокойное поле без эскалации -> ни одного бида;
(г) дефолт ВЫКЛ; незнание закрывает гейт; бакет $20/сутки ($9/бид = 2 бида) с 80%-алертом;
(д) ИСПОЛНЯЕМЫЕ стенды обеих боевых веток (hotpath/execute) + негативный контроль флага.
"""
import sys
import pathlib as _pl

sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

FAILS = []


def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok:
        FAILS.append(name)


import chain.healthy_race as hr  # noqa: E402


def _reset(on=True):
    hr.HR_ON = on
    with hr._LOCK:
        hr._BUCKET.update({"day": None, "spent": 0.0, "alert80": False})
        hr._RECENT.clear()
        for k in hr.COUNTERS:
            hr.COUNTERS[k] = 0


NOW = 1_800_000_000.0

# ---- (г0) дефолт ВЫКЛ: без env-флага гейт закрыт даже на идеальной цели ----
_reset(on=False)
st, why = hr.classify("0xd4a903dc", "0xB62a99D6", 0.976, 4436.0, 0.0, 10**18,
                      episode=True, corpse=False, now=NOW)
check("дефолт ВЫКЛ: state=SKIP off", (st, why) == ("SKIP", "off"), (st, why))

# ---- (а) реплей 05:11:04 ЦЕПНЫМИ призами (вскрытие 50293058), не прозой спеки ----
# НАЗВАННЫЙ ПРЕДЕЛ (линза 22.08): repaid четырёх REFUSAL-LOSS = $860/$204/$108/$245, премия
# ~12.7% => призы $109/$26/$14/$31 — ВСЕ НИЖЕ пола $300. Критерий спеки «все четыре стреляют»
# невыполним ПО ПОСТРОЕНИЮ самой спеки (пол $300 + бакет $20 = 2 бида/сутки) — противоречие
# поднято ревьюеру/kelbic. Фича спасает ЖИРНЫЙ класс: execute-цель $4436 (0xd4a903dc).
_reset(on=True)
REFUSED_CHAIN = [   # (mid, borrower, hf_est, prize$ ИЗ ЦЕПИ)
    ("0x73527ddd", "0x2ACdbbA7", 0.9738, 109.0),
    ("0x73527ddd", "0x62aeC1C6", 0.9740, 26.0),
    ("0x73527ddd", "0x47E2aD2C", 0.9755, 14.0),
    ("0xd7520ad1", "0x62aeC1C6", 0.9741, 31.0),
]
states = [hr.classify(m, b, h, p, 0.0, 10**18, episode=True, corpse=False, now=NOW)
          for m, b, h, p in REFUSED_CHAIN]
check("реплей 05:11 цепными призами: мелкий класс режется ПОЛОМ приза (экономика, названо)",
      all(s == "SKIP" and "приз" in w for s, w in states), states)
st, why = hr.classify("0xd4a903dc", "0xB62a99D6", 0.9760, 4436.0, 0.0, 10**18,
                      episode=True, corpse=False, now=NOW)
check("жирная цель $4436 (execute-путь, та самая) -> HEALTHY_RACE", st == "HEALTHY_RACE", why)

# ---- защёлка цели: повторный спавн не сжигает бакет об одного заёмщика ----
hr.note_bid("0xd4a903dc", "0xB62a99D6", 4436.0, 5_000_000, 0.976, now=NOW)
st, why = hr.classify("0xd4a903dc", "0xB62a99D6", 0.9760, 4436.0, 0.0, 10**18,
                      episode=True, corpse=False, now=NOW + 5)
check("повторный бид той же цели заперт защёлкой", st == "SKIP" and "защёлка" in why,
      (st, why))
st, _ = hr.classify("0xd4a903dc", "0xB62a99D6", 0.9760, 4436.0, 0.0, 10**18,
                    episode=True, corpse=False, now=NOW + hr.HR_REBID_SEC + 1)
check("защёлка отпускает после HR_REBID_SEC", st == "HEALTHY_RACE", st)

# ---- (б) реплей #35: 30 трупов из 43 отсеяны, живых <=13 ----
_reset(on=True)
res = []
for i in range(43):
    corpse = i < 30                                     # замер #35: 30/43 — трупы
    res.append(hr.classify("0xm%02d" % i, "0xb%02d" % i, 0.99, 500.0, 0.0, 10**18,
                           episode=True, corpse=corpse, now=NOW)[0])
check("#35: трупы -> CORPSE все 30", res[:30] == ["CORPSE"] * 30, res[:30])
check("#35: живых в бид <=13 и счётчик трупов честный",
      sum(1 for s in res if s == "HEALTHY_RACE") <= 13
      and hr.COUNTERS["hr_corpse_filtered"] == 30,
      (res.count("HEALTHY_RACE"), hr.COUNTERS))

# ---- (в) негативный контроль: штиль (нет эскалации/pending) -> ни одного бида ----
_reset(on=True)
quiet = [hr.classify(m, b, h, p, 0.0, 10**18, episode=False, corpse=False, now=NOW)
         for m, b, h, p in REFUSED_CHAIN + [("0xd4a903dc", "0xB62a99D6", 0.976, 4436.0)]]
check("штиль: ноль бидов, причина «эпизод не активен»",
      all(s == "SKIP" and "эпизод" in w for s, w in quiet), quiet)

# ---- (г) остальные гейты по одному ----
_reset(on=True)
check("труп по shares=0", hr.classify("0xm", "0xb", 0.97, 500, 0.0, 0, episode=True,
                                      corpse=False, now=NOW)[0] == "CORPSE")
check("слепой индекс закрывает гейт (незнание)",
      hr.classify("0xm", "0xb", 0.97, 500, 0.0, 1, episode=True, corpse=None,
                  now=NOW) == ("SKIP", "индекс слеп — незнание закрывает гейт"))
check("hf выше порога -> SKIP", hr.classify("0xm", "0xb", 0.9985, 500, 0.0, 1,
      episode=True, corpse=False, now=NOW)[0] == "SKIP")
check("протухшие данные -> SKIP", hr.classify("0xm", "0xb", 0.97, 500, 61.0, 1,
      episode=True, corpse=False, now=NOW)[0] == "SKIP")
check("приз ниже $300 -> SKIP", hr.classify("0xm", "0xb", 0.97, 299, 0.0, 1,
      episode=True, corpse=False, now=NOW)[0] == "SKIP")

# ---- бакет: $20/сутки, 80%-алерт, сброс по UTC-полуночи ----
_reset(on=True)
import os
hr.HR_JOURNAL = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "tmp", "hr_test.jsonl")
warns = [hr.note_bid("0xm", "0xb%d" % i, 500, 5_000_000, 0.97, now=NOW) for i in range(2)]
check("80%-алерт ровно один раз (2 бида x $9 = $18 >= 0.8x$20)",
      warns[0] is None and warns[1] and "80%" in warns[1], warns)
st, why = hr.classify("0xm", "0xb3", 0.97, 500, 0.0, 1, episode=True, corpse=False, now=NOW)
check("третий бид не проходит: бакет исчерпан", st == "SKIP" and "бакет" in why, (st, why))
st2, _ = hr.classify("0xm", "0xb3", 0.97, 500, 0.0, 1, episode=True, corpse=False,
                     now=NOW + 86400)
check("UTC-полночь сбрасывает бакет", st2 == "HEALTHY_RACE", st2)
check("счётчики: bids=2 budget_skips=1",
      hr.COUNTERS["hr_bids"] == 2 and hr.COUNTERS["hr_budget_skips"] == 1, hr.COUNTERS)

# ---- журнал калибровки P пишется ----
check("журнал бидов: 2 строки с prize/tip/hf",
      sum(1 for _ in open(hr.HR_JOURNAL)) >= 2, hr.HR_JOURNAL)

# ---- liquidated_recently: юнит на фикстуре индекса ----
import chain.chain_index as ci  # noqa: E402
import threading  # noqa: E402

idx = ci.ChainIndex.__new__(ci.ChainIndex)
idx._lock = threading.Lock()
MID = "0ab52c30" + "11" * 28                            # ведущий ноль — ловушка lstrip("0x")
idx._blocks = {1000: {"liquidate": [{"mid": MID, "borrower": "0xdead00", "tx": "0x1"}]}}
idx.sealed_head = lambda: 1100
check("труп найден в lookback (и ведущий ноль mid цел)",
      idx.liquidated_recently("0x" + MID, "0xDEAD00", lookback=150) is True)
check("вне lookback — не труп", idx.liquidated_recently("0x" + MID, "0xdead00",
                                                        lookback=50) is False)
check("чужой borrower — не труп", idx.liquidated_recently("0x" + MID, "0xbeef",
                                                          lookback=150) is False)

# ---- ИСПОЛНЯЕМЫЕ стенды боевых веток (урок «шов подменён выше дефекта»): ветка НЕ греп,
# ---- а реально исполняется — латентный NameError в скоупе всплыл бы здесь, не в эпизоде ----
from types import SimpleNamespace  # noqa: E402
import chain.hotpath as hp  # noqa: E402
import chain.execute as ex  # noqa: E402
import chain.multicall as mc  # noqa: E402

TBA, TBS = 10**12, 10**12
BS, COLLAT, PRICE = 35_000 * 10**6, 40_000 * 10**6, 10**36   # hf = 40k*0.86/35k = 0.983
LLTV = int(0.86e18)
BORR = "0x" + "ab" * 20


class _Rpc:
    def _web3(self):
        return SimpleNamespace(eth=SimpleNamespace(
            block_number=1,
            account=SimpleNamespace(from_key=lambda k: SimpleNamespace(address="0xB0T"))))

    def to_bytes32(self, s):
        return b"\x00" * 32


_CFG = SimpleNamespace(liquidator_address="0xLiq", wallet_key="0xk", tip_gwei=0.05,
                       gas_limit_floor=3_000_000, gas_limit_est=1_500_000,
                       eth_price_usd=4000.0, min_profit_usd=5.0)
_sv = {n: getattr(ex, n) for n in ("simulate_tx", "swap_plan", "direct_swap", "kyber_swap",
                                   "account_for", "encode_liquidate")}
_sv_mc = {n: getattr(mc, n) for n in ("aggregate3", "decode_id_to_market_params",
                                      "decode_market", "decode_position", "decode_price")}
try:
    ex.simulate_tx = lambda *a, **k: {"ok": False,
                                      "error": "execution reverted: position is healthy"}
    ex.swap_plan = lambda rpc, coll, seized: ("0xCollTok", int(seized), None)
    ex.direct_swap = lambda *a, **k: {"router": "0xRouter", "calldata": "0x1234"}
    ex.kyber_swap = lambda *a, **k: {"router": "0xRouter", "calldata": "0x1234",
                                     "amount_out": 10**30}
    ex.account_for = lambda w3, key: SimpleNamespace(address="0xB0T")
    ex.encode_liquidate = lambda *a, **k: b"\xca\xfe"
    hr.HR_ON = True
    _reset(on=True)
    _hr_cbi = hr.corpse_by_index
    hr.corpse_by_index = lambda *a, **k: False
    _hr_ep = hr.episode_active
    hr.episode_active = lambda now=None: True

    # -- hotpath.prepare_hot: healthy-реверт -> HR-бид (episode=True по построению) --
    hp._FULL_PARAMS_CACHE["0xhrmid"] = ("0xLoan", "0xColl", "0xOra", "0xIrm", LLTV)
    _p = hp.prepare_hot(_Rpc(), _Rpc(), _CFG, "0xhrmid", BORR, 35_000.0, BS,
                        PRICE, state=(TBA, TBS, BS, COLLAT))
    check("ИСПОЛНЕНО hotpath: healthy-реверт даёт ok=True healthy_race с calldata и tip",
          _p.get("ok") is True and _p.get("healthy_race") is True
          and _p.get("calldata") == b"\xca\xfe" and _p.get("tip", 0) >= int(0.05e9)
          and _p.get("min_profit", 0) >= 1, str(_p)[:180])
    # негативный контроль исполнения: флаг снят -> тот же вход возвращает skip
    _reset(on=False)
    _p0 = hp.prepare_hot(_Rpc(), _Rpc(), _CFG, "0xhrmid", BORR, 35_000.0, BS,
                         PRICE, state=(TBA, TBS, BS, COLLAT))
    check("ИСПОЛНЕНО hotpath: REACTIVE_HEALTHY_RACE=0 -> прежний skip бит-в-бит",
          _p0.get("ok") is False and "sim revert" in _p0.get("reason", ""), str(_p0)[:120])

    # -- execute.prepare_liquidation: тот же вход через multicall-стабы --
    _reset(on=True)
    mc.aggregate3 = lambda rpc, calls: [(True, b"")] * len(calls)
    mc.decode_id_to_market_params = lambda b: ("0xLoan", "0xColl", "0xOra", "0xIrm", LLTV)
    mc.decode_market = lambda b: (0, 0, TBA, TBS, 0, 0)
    mc.decode_position = lambda b: (BS, COLLAT)
    mc.decode_price = lambda b: PRICE
    _q = ex.prepare_liquidation(_Rpc(), _CFG, "0xhrmid", BORR, 35_000.0, BS)
    check("ИСПОЛНЕНО execute: healthy-реверт даёт ok=True healthy_race с hr_tip_wei",
          _q.get("ok") is True and _q.get("healthy_race") is True
          and _q.get("hr_tip_wei", 0) >= int(0.05e9), str(_q)[:180])
    _reset(on=False)
    _q0 = ex.prepare_liquidation(_Rpc(), _CFG, "0xhrmid", BORR, 35_000.0, BS)
    check("ИСПОЛНЕНО execute: флаг снят -> прежний skip (…| HR SKIP: off)",
          _q0.get("ok") is False and "sim revert" in _q0.get("reason", ""), str(_q0)[:140])
finally:
    for n, v in _sv.items():
        setattr(ex, n, v)
    for n, v in _sv_mc.items():
        setattr(mc, n, v)
    hr.corpse_by_index = _hr_cbi
    hr.episode_active = _hr_ep
    hp._FULL_PARAMS_CACHE.pop("0xhrmid", None)

# ---- гарды дрейфа боевых потребителей ----
_H = open("chain/hotpath.py").read()
_E = open("chain/execute.py").read()
check("hotpath: HR-ветка на бакете healthy-race, возврат ok с tip",
      '_diag_bucket == "healthy-race"' in _H and '"healthy_race": True' in _H
      and "hr_tip_wei(int(cfg.tip_gwei * 1e9))" in _H)
check("execute: HR-ветка на срезе sim revert + tip-override в try_liquidate",
      '"hr_tip_wei": _hr_tip' in _E and 'prep.get("hr_tip_wei")' in _E)
check("on-chain страховка: min_profit 25% премии в ОБОИХ путях",
      _H.count("* 0.25))") >= 1 and _E.count("* 0.25))") >= 1)
check("наблюдаемость: строка HR bid в обоих путях",
      "HR bid target=" in _H and "HR bid target=" in _E)
check("откат одним env: дефолт ВЫКЛ",
      'os.environ.get("REACTIVE_HEALTHY_RACE", "0")' in open("chain/healthy_race.py").read())

print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS
                else "OK: healthy-race — жирный класс бидит (обе ветки ИСПОЛНЕНЫ), мелкий "
                     "режется полом $300 (названо), трупы #35 отсеяны, штиль молчит"))
sys.exit(1 if FAILS else 0)
