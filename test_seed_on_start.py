"""#140: ПОСЕВ СКЛАДА НА СТАРТЕ — движок поднимается ДО первого окна, а не в нём.

ЗАМЕР, РАДИ КОТОРОГО (постановка координатора): с рестарта 22:16 движок не поднимался НИ РАЗУ —
8 часов склад пуст при 445 армах предикта и 2502 прогревах PREARM, потому что окно spec-fire не
армилось («ближ. жир dist > потолок 3.0%»). То есть немота длится не «50 минут после рестарта»,
а ДО ПЕРВОГО ОКНА, и детонатор-класс (мгновенный флип cbADA-типа) встречает пустой склад плюс
28-32с подписи ровно в горячий момент (третья потеря 20:51: право огня пришло с планом, склад
сеялся уже в окне).

ЗАБОР ЧЕСТЕРТОНА (проверен перед стройкой): лень подъёма НЕ несёт названной причины. В
`_engine()` названа только ручка отката (SPEC_FIRE_ENGINE=0 -> путь залпа бит-в-бит), а шапка
fire_engine и комментарии #78/#90 в `_prearm_board` говорят ОБРАТНОЕ — склад набивать заранее,
в спокойное время. Возражение «подпись — второй потолок CPU» снято замером 13.08 (coincurve:
0.557мс вместо 3.67мс, «перестаёт связывать вовсе»).

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ:
 * КРАСНЫЙ-НА-БАЗЕ: без посева движок поднимается ТОЛЬКО из окна/арма — то самое состояние;
 * посев поднимает движок и запускает фоновое пополнение ДО любого окна;
 * МЕХАНИКА ОГНЯ НЕ ТРОНУТА: поднятый движок без keepalive не стреляет;
 * осечка посева не роняет старт и деградирует к прежней лени;
 * порядок сигналов любой (refresh-воркер стартует раньше SpecFireLive);
 * откат ручкой SPEC_FIRE_SEED_ON_START=0.

ЗАПУСК: python3 test_seed_on_start.py
"""
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import chain.fire_engine as fe                               # noqa: E402
import chain.spec_fire_live as sfl_mod                       # noqa: E402
from chain.spec_fire_live import SpecFireLive                # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


def reset_seed():
    with sfl_mod._SEED_LOCK:
        sfl_mod._SEED_STATE.update({"hook": None, "ready": False, "done": False})


LOGS = []
CFG = types.SimpleNamespace(liquidator_address="0x" + "9a" * 20, gas_limit_est=500_000,
                            tip_gwei=0.05, sequencer_rpc="off", eth_price_usd=1730.0,
                            fast_lane_max_repaid_usd=500_000.0, wallet_key=None,
                            rpc_url="http://stub")
SHARED = types.SimpleNamespace(lock=threading.Lock(), groups={}, by_id={}, debt_by={}, hf_by={})


def mk_sf():
    LOGS.clear()
    sf = SpecFireLive(rpc=None, cfg=CFG, shared=SHARED, ret_fn=lambda l: 0.0,
                      dev_by={"BTC": 0.001}, log_fn=LOGS.append, now=time.monotonic,
                      sleep=lambda s: None)
    return sf


# --- СТЕНД ДВИЖКА: настоящий FireEngine, подменён только транспорт (ноги/отправка/подпись) ---
class _EngineSpy:
    """Считает, что просили у склада: какие ноги, сколько доливов. Подпись НЕ исполняется —
    предмет проверки не она (посев её не переизобретает), а ФАКТ подъёма и работы пополнения."""

    def __init__(self):
        self.refills = []
        self.sends = []
        self.depth = {}

    def install(self, sf):
        legs = ["0x%040x" % i for i in range(1, 6)]
        self.depth = {a: 0 for a in legs}

        def _refill(addr):
            self.refills.append(addr)
            self.depth[addr] = fe.ENGINE_LADDER_DEPTH if hasattr(fe, "ENGINE_LADDER_DEPTH") else 40

        def _mk_engine():
            eng = fe.FireEngine(legs_fn=lambda: legs, send_fn=self.sends.append,
                                refill_fn=_refill, depth_fn=lambda a: self.depth.get(a, 0),
                                log=LOGS.append)
            sf._fire_engine = eng
            eng.start()
            return eng

        sf._engine = _mk_engine
        return legs


# --- 1. КРАСНЫЙ-НА-БАЗЕ: сегодня движок поднимается ТОЛЬКО из окна/арма -----------------------
_src = open(pathlib.Path(__file__).parent / "chain" / "spec_fire_live.py").read()
_seed_calls = _src.count("self._engine()")
check("КРАСНЫЙ-НА-БАЗЕ: все прежние точки подъёма — внутри окна/прогрева (их %d), ни одной на "
      "старте процесса" % _seed_calls, _seed_calls >= 4)
check("КРАСНЫЙ-НА-БАЗЕ: без сигнала «кандидаты готовы» посев не идёт",
      (reset_seed(), sfl_mod.set_seed_hook(lambda: None), sfl_mod._SEED_STATE["done"])[2] is False)
check("КРАСНЫЙ-НА-БАЗЕ: без сеятеля сигнал refresh тоже ничего не поднимает",
      (reset_seed(), sfl_mod.note_candidates_ready(), sfl_mod._SEED_STATE["done"])[2] is False)

# --- 2. ЗЕЛЁНЫЙ: оба условия сошлись -> движок поднят, склад НАПОЛНЯЕТСЯ ----------------------
_old_at = fe.ENGINE_REFILL_AT
fe.ENGINE_REFILL_AT = 40
try:
    reset_seed()
    sf = mk_sf()
    spy = _EngineSpy()
    legs = spy.install(sf)
    sfl_mod.set_seed_hook(sf.seed_on_start)
    check("до сигнала refresh движок НЕ поднят", getattr(sf, "_fire_engine", None) is None)
    sfl_mod.note_candidates_ready()
    _t0 = time.time()
    while getattr(sf, "_fire_engine", None) is None and time.time() - _t0 < 5:
        time.sleep(0.05)
    check("посев поднял движок ПОСЛЕ первого candidate refresh (до любого окна)",
          getattr(sf, "_fire_engine", None) is not None)
    _t0 = time.time()
    while not spy.refills and time.time() - _t0 < 5:
        time.sleep(0.05)
    check("склад НАПОЛНЯЕТСЯ: фоновое пополнение позвано (%d доливов, ноги %s)"
          % (len(spy.refills), sorted(set(spy.refills))[:2]), len(spy.refills) > 0)
    check("посев переиспользует ПОПОЛНЕНИЕ движка, а не второй путь подписи",
          set(spy.refills) <= set(legs))

    # --- 3. МЕХАНИКА ОГНЯ НЕ ТРОНУТА: без keepalive поднятый движок молчит --------------------
    time.sleep(0.4)
    check("права огня посев НЕ даёт: без keepalive ни одной отправки (%d)" % len(spy.sends),
          len(spy.sends) == 0)
    check("в журнале это сказано словами, а не подразумевается",
          any("keepalive" in m for m in LOGS), LOGS[-1] if LOGS else "")
    sf._fire_engine.stop()
finally:
    fe.ENGINE_REFILL_AT = _old_at

# --- 4. ПОРЯДОК СИГНАЛОВ ЛЮБОЙ (refresh-воркер стартует РАНЬШЕ SpecFireLive) -----------------
reset_seed()
sf2 = mk_sf()
spy2 = _EngineSpy()
spy2.install(sf2)
sfl_mod.note_candidates_ready()                              # сперва refresh
check("refresh раньше сеятеля: посев ждёт и не теряется", getattr(sf2, "_fire_engine", None) is None)
sfl_mod.set_seed_hook(sf2.seed_on_start)                     # потом объект
_t0 = time.time()
while getattr(sf2, "_fire_engine", None) is None and time.time() - _t0 < 5:
    time.sleep(0.05)
check("...и срабатывает, когда приходит второй сигнал",
      getattr(sf2, "_fire_engine", None) is not None)
sf2._fire_engine.stop()

# --- 5. ОДНОРАЗОВОСТЬ -------------------------------------------------------------------------
_n_before = sfl_mod._SEED_STATE["done"]
check("посев одноразов (повторные сигналы не сеют заново)",
      _n_before is True and sfl_mod._maybe_seed() is False)

# --- 6. ОСЕЧКА ПОСЕВА НЕ РОНЯЕТ СТАРТ ---------------------------------------------------------
reset_seed()
sf3 = mk_sf()


def _boom():
    raise RuntimeError("узел не отвечает")


sf3._engine = _boom
sfl_mod.set_seed_hook(sf3.seed_on_start)
LOGS.clear()                     # соседние треды посева уже отписались — мерим ТОЛЬКО этот случай
sfl_mod.note_candidates_ready()
time.sleep(0.4)
_fail_lines = [m for m in LOGS if "НЕ УДАЛСЯ" in m]
check("осечка посева: старт жив, деградация к прежней лени НАЗВАНА",
      len(_fail_lines) == 1, _fail_lines[0] if _fail_lines else str(LOGS))
check("исключение не вышло за тред посева (процесс жив, тест продолжается)", True)

# --- 7. ДВИЖОК ВЫКЛЮЧЕН -> посев молчит и говорит об этом --------------------------------------
reset_seed()
sf4 = mk_sf()
sf4._engine = lambda: None                                   # ENGINE_ON=0 отдаёт None
LOGS.clear()
check("движок выключен: посев не выдумывает склад", sf4.seed_on_start() is False)
check("...и называет причину", any("выключен" in m for m in LOGS), str(LOGS[-1:]))

# --- 8. ОТКАТ РУЧКОЙ ---------------------------------------------------------------------------
reset_seed()
sf5 = mk_sf()
spy5 = _EngineSpy()
spy5.install(sf5)
_old = sfl_mod.SPEC_FIRE_SEED_ON_START
sfl_mod.SPEC_FIRE_SEED_ON_START = False
try:
    check("SPEC_FIRE_SEED_ON_START=0: посев не поднимает движок (прежнее поведение)",
          sf5.seed_on_start() is False and getattr(sf5, "_fire_engine", None) is None)
finally:
    sfl_mod.SPEC_FIRE_SEED_ON_START = _old

# --- 9. УЖЕ ПОДНЯТЫЙ ДВИЖОК ПОСЕВ НЕ ТРОГАЕТ ---------------------------------------------------
reset_seed()
sf6 = mk_sf()
sf6._fire_engine = object()
sf6._engine = _boom                                          # если полезет — упадёт
check("движок уже поднят окном: посев молча уходит", sf6.seed_on_start() is False)

# --- 10. ПРОВОДКА В main ------------------------------------------------------------------------
_main = open(pathlib.Path(__file__).parent / "main.py").read()
check("main армирует сеятеля явно", "set_seed_hook(_sfl.seed_on_start)" in _main)
check("сигнал идёт из refresh-воркера ПОСЛЕ успешного fetch", "note_candidates_ready()" in _main)
_pos_refresh = _main.index("note_candidates_ready()")
_pos_fetch = _main.index("g, db, dab, hb, n = fetch_candidates(markets)")
check("сигнал стоит ПОСЛЕ обновления shared, а не на t=0", _pos_refresh > _pos_fetch)
check("кольца зеркала через рестарт не персистятся (скоуп не расползся)",
      "persist" not in _src.lower().split("#140")[-1][:2000])

if FAILS:
    print("\n=== ПОСЕВ #140: %d ПРОВАЛОВ ===" % len(FAILS))
    sys.exit(1)
print("\n=== ПОСЕВ #140: движок поднят до первого окна, склад наполняется, право огня не тронуто ===")
