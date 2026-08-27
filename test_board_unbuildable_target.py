"""НЕСОБИРАЕМАЯ ЦЕЛЬ НЕ ДЕРЖИТ ДОСКУ (27.08: yoETH/WETH без роута Kyber — доска 5ч переспрашивала
ту же вершину каждые 5с, 682 отказа за 2ч, 0 выстрелов при живой службе).
(а) осечка сборки помечает цель и следующая вершина — ДРУГАЯ цель; (б) TTL истёк — цель
возвращается; (в) КРАСНЫЙ-НА-БАЗЕ: без пометки вершина та же."""
import sys, threading, time, pathlib as _pl
from types import SimpleNamespace
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
FAILS=[]
def check(n, ok, extra=""):
    print("%-4s %s%s"%("OK" if ok else "FAIL", n, "" if ok else " — "+str(extra)[:180]))
    if not ok: FAILS.append(n)
import chain.spec_fire_live as sfl
_sv=sfl._label_of; sfl._label_of=lambda s:s
MID="0xmid"; BAD="0xyoETH_target"; GOOD="0xgood_target"
T=[1000.0]
def mk():
    o=sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    o.stats={}; o._lock=threading.Lock(); o._logs=[]; o._log=o._logs.append; o._now=lambda:T[0]
    o._armskip_ts={}; o._unbuildable={}
    m=SimpleNamespace(collateral_token="ETH", loan_token="USDC", bonus=0.043)
    o.shared=SimpleNamespace(lock=threading.Lock(), groups={MID:[BAD,GOOD]}, debt_by={(MID,BAD):59_000.0,(MID,GOOD):28_000.0}, by_id={MID:m}, hf_by={(MID,BAD):1.001,(MID,GOOD):1.002})
    return o
o=mk()
top1=o._top_fat("ETH","down",k=1)[0]
check("вершина до осечки — жирная несобираемая цель", top1 and top1[0][1]==BAD, top1)
# имитация осечки сборки: то, что делает _prearm_board в ветке «проба не собралась»
o._unbuildable[(MID,BAD)]=o._now()+sfl.SPEC_FIRE_UNBUILDABLE_TTL_SEC
top2=o._top_fat("ETH","down",k=1)[0]
check("(а) после пометки вершина — СЛЕДУЮЩАЯ собираемая цель", top2 and top2[0][1]==GOOD, top2)
T[0]+=sfl.SPEC_FIRE_UNBUILDABLE_TTL_SEC+1
top3=o._top_fat("ETH","down",k=1)[0]
check("(б) TTL истёк — цель возвращается в вершину", top3 and top3[0][1]==BAD, top3)
# (в) красный-на-базе: без пометки вершина не меняется сколько ни переспрашивай
o2=mk(); tops=[o2._top_fat("ETH","down",k=1)[0][0][1] for _ in range(3)]
check("(в) КРАСНЫЙ-НА-БАЗЕ: без пометки три переспроса = та же цель (замкнутый круг)", tops==[BAD,BAD,BAD], tops)
# детонатор тоже не должен судить по несобираемой цели (доска планирует ПОД ЦЕЛЬ ДЕТОНАТОРА)
o4=mk(); o4._det_key_by_feed={}; o4.dev_by={"ETH":0.0015}
o4._unbuildable[(MID,BAD)]=o4._now()+sfl.SPEC_FIRE_UNBUILDABLE_TTL_SEC
o4._min_flip_dist("ETH","down")
_dk=(o4._det_key_by_feed or {}).get("ETH")
check("детонатор: несобираемая цель пропущена, ключ — собираемая", _dk is not None and _dk[0]==(MID,GOOD), _dk)
_S=open("chain/spec_fire_live.py").read()
check("гард: пропуск стоит и в _min_flip_dist, и в ветке «под цель детонатора»", _S.count("несобираемая цель не судит детонатор")==1 and "несобираемую не навязываем" in _S)
check("пометка стоит в ветке «проба не собралась» и печатает причину", "self._unbuildable[(mid, b)] = self._now() + SPEC_FIRE_UNBUILDABLE_TTL_SEC" in _S and "несобираема (нет роута/котировки)" in _S)
check("причина записана (682 отказа, 5ч без выстрела)", "682 раза" in _S and "5ч без единого выстрела" in _S)
sfl._label_of=_sv
print("\n%s"%("FAIL: %d"%len(FAILS) if FAILS else "OK: несобираемая цель не держит доску"))
sys.exit(1 if FAILS else 0)
