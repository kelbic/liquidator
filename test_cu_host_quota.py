"""Пер-хостовое ведро квоты (27.08: ключ A #3133 на платном тарифе — своё ведро, свой CUPS).
(а) hosts[h].quota_cu перекрывает общее; (б) без поля — общее quota_cu; (в) fleet_deficit
складывает достижимое ПО ХОСТАМ; (г) КРАСНЫЙ-НА-БАЗЕ: с общим 30M остаток A считался бы ложно."""
import sys, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
FAILS=[]
def check(n, ok, extra=""):
    print("%-4s %s%s"%("OK" if ok else "FAIL", n, "" if ok else " — "+str(extra)[:160]))
    if not ok: FAILS.append(n)
from analysis import cu_quota_watch as w
A="base-mainnet.g.alchemy.com#3133"; B="base-mainnet.g.alchemy.com#5735"
anc={"quota_cu":30e6,"hosts":{A:{"mtd_cu":15e6,"quota_cu":300e6},B:{"mtd_cu":26e6}}}
check("(а) A: своё ведро 300M", w._host_quota(anc,A)==300e6, w._host_quota(anc,A))
check("(б) B: общее 30M", w._host_quota(anc,B)==30e6, w._host_quota(anc,B))
check("(б) неизвестный хост: общее", w._host_quota(anc,"x#0000")==30e6)
check("(г) КРАСНЫЙ-НА-БАЗЕ: по общему ведру остаток A = 15M, по своему = 285M", (30e6-15e6)==15e6 and (w._host_quota(anc,A)-15e6)==285e6)
_S=open("analysis/cu_quota_watch.py").read()
_direct=[l for l in _S.splitlines() if 'get("quota_cu"' in l and "_host_quota" not in l and "def _host_quota" not in l and not l.strip().startswith("#")]
check("общий quota_cu читается напрямую только внутри хелпера", len(_direct)==1 and "_host_quota(anchor, host)" in _S and "_host_quota(anchor, h)" in _S, _direct)
print("\n%s"%("FAIL: %d"%len(FAILS) if FAILS else "OK: ведро квоты — по хосту"))
sys.exit(1 if FAILS else 0)
