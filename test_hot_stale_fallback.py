"""STALE-SKIP → STALE-USE (каскад 15:04 26.08): 18 из 19 жертв были в кандидатах горячего пути и
выброшены гейтом «живое чтение упало → не действовать по протухшему» (443 строки за 80с).
Фикс: при провале живого чтения действуем по кэшу (возраст <= HOT_STALE_FALLBACK_SEC),
выбрасываем только тех, кого в кэше нет. Страховка — реверт на цепи ($0.002).
(а) живое чтение упало -> протухшие ИЗ КЭША возвращаются; (б) без записи в кэше — пропуск;
(в) старше потолка — пропуск; (г) живое чтение живо -> прежний путь бит-в-бит;
(д) КРАСНЫЙ-НА-БАЗЕ: потолок 0 = прежнее поведение (все выброшены)."""
import sys, time, pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
FAILS = []
def check(name, ok, extra=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("" if ok else " — " + str(extra)[:200])))
    if not ok: FAILS.append(name)
import chain.hotpath as hp  # noqa: E402
MID = "0xmarket"; now = time.time()
with hp._POSITION_CACHE_LOCK:
    hp._POSITION_CACHE[MID] = (100, 200, {"0xfresh": (1, 2, now - 1), "0xstale": (3, 4, now - 120),
                                          "0xancient": (5, 6, now - 5000)})
_calls = []
def _dead(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
    _calls.append(list(borrowers)); raise RuntimeError("429 Too Many Requests")
_real = hp.read_positions; hp.read_positions = _dead
src = {}
tba, tbs, pos = hp.read_positions_cached(None, MID, ["0xfresh", "0xstale", "0xancient", "0xnocache"], src_out=src)
got = {b: (bs, col) for b, bs, col in pos}
check("(а) живое упало: свежий из кэша", got.get("0xfresh") == (1, 2), got)
check("(а) живое упало: ПРОТУХШИЙ (120с) — ИЗ КЭША, не выброшен (каскад 15:04)", got.get("0xstale") == (3, 4), got)
check("(в) старше потолка %ds — пропуск" % int(hp._HOT_STALE_FALLBACK_SEC), "0xancient" not in got, got)
check("(б) без записи в кэше — пропуск (действовать не по чему)", "0xnocache" not in got, got)
check("живое чтение звалось для протухших", _calls and set(_calls[0]) == {"0xstale", "0xancient", "0xnocache"}, _calls)
check("источник помечен cache+stale", src.get("src") == "cache+stale", src)
# (г) живое живо — прежний путь
def _alive(rpc, mid, borrowers, preconf_rpc=None, src_out=None):
    return 101, 201, [(b, 9, 9) for b in borrowers]
hp.read_positions = _alive
tba2, tbs2, pos2 = hp.read_positions_cached(None, MID, ["0xfresh", "0xstale", "0xnocache"])
g2 = {b: (bs, col) for b, bs, col in pos2}
check("(г) живое живо: протухшие и отсутствующие дочитаны живьём (бит-в-бит прежнее)", g2.get("0xstale") == (9, 9) and g2.get("0xnocache") == (9, 9) and g2.get("0xfresh") == (1, 2) and tba2 == 101, g2)
# (д) красный-на-базе: потолок 0 = прежнее поведение
hp.read_positions = _dead; _sv = hp._HOT_STALE_FALLBACK_SEC; hp._HOT_STALE_FALLBACK_SEC = 0.0
_, _, pos3 = hp.read_positions_cached(None, MID, ["0xstale"])
check("(д) КРАСНЫЙ-НА-БАЗЕ: потолок 0 = прежний stale-skip (жертва выброшена)", pos3 == [], pos3)
hp._HOT_STALE_FALLBACK_SEC = _sv; hp.read_positions = _real
_S = open("chain/hotpath.py").read()
check("причина записана в коде (18 из 19 жертв, 443 строки)", "18 из 19 жертв" in _S and "443 за 80с" in _S)
print("\n%s" % ("FAIL: %d" % len(FAILS) if FAILS else "OK: при провале живого чтения действуем по кэшу, страховка — реверт"))
sys.exit(1 if FAILS else 0)
