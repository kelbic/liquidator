"""П.1 HEALTHY-RACE POLICY (22.08): приёмка по спеке ревьюера, красным-на-базе.

(а) реплей 4 отказов 05:11:04 (REFUSAL-LOSS, сверено цепью) -> все четыре проходят в бид;
(б) реплей 43 healthy-race замера #35 (30 трупов) -> трупы отсеяны, живых <=13;
(в) негативный контроль: спокойное поле без эскалации -> ни одного бида;
(г) дефолт ВЫКЛ; незнание (слепой индекс) закрывает гейт; бакет $20 с 80%-алертом.
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
        for k in hr.COUNTERS:
            hr.COUNTERS[k] = 0


NOW = 1_800_000_000.0

# ---- (г0) дефолт ВЫКЛ: без env-флага гейт закрыт даже на идеальной цели ----
_reset(on=False)
st, why = hr.classify("0xd4a903dc", "0xB62a99D6", 0.976, 4436.0, 0.0, 10**18,
                      episode=True, corpse=False, now=NOW)
check("дефолт ВЫКЛ: state=SKIP off", (st, why) == ("SKIP", "off"), (st, why))

# ---- (а) реплей 05:11:04 — четыре отданные цели проходят в бид ----
_reset(on=True)
REFUSED = [   # (mid, borrower, hf_est, prize$) — эпизод активен (эскалация 2237-2569 мгвей)
    ("0x73527ddd", "0x2ACdbbA7", 0.9738, 586.0),
    ("0x73527ddd", "0x62aeC1C6", 0.9740, 620.0),
    ("0x73527ddd", "0x47E2aD2C", 0.9755, 890.0),
    ("0xd4a903dc", "0xB62a99D6", 0.9760, 4436.0),
]
states = [hr.classify(m, b, h, p, 0.0, 10**18, episode=True, corpse=False, now=NOW)
          for m, b, h, p in REFUSED]
check("реплей 05:11: ВСЕ ЧЕТЫРЕ отданные цели -> HEALTHY_RACE",
      all(s == "HEALTHY_RACE" for s, _ in states), states)

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
         for m, b, h, p in REFUSED]
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
warns = [hr.note_bid("0xm", "0xb", 500, 5_000_000, 0.97, now=NOW) for _ in range(2)]
check("80%-алерт ровно один раз (2 бида x $9 = $18 >= 0.8x$20)",
      warns[0] is None and warns[1] and "80%" in warns[1], warns)
st, why = hr.classify("0xm", "0xb", 0.97, 500, 0.0, 1, episode=True, corpse=False, now=NOW)
check("третий бид не проходит: бакет исчерпан", st == "SKIP" and "бакет" in why, (st, why))
st2, _ = hr.classify("0xm", "0xb", 0.97, 500, 0.0, 1, episode=True, corpse=False,
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
                else "OK: healthy-race — 4 отданные цели стреляют, трупы #35 отсеяны, штиль молчит"))
sys.exit(1 if FAILS else 0)
