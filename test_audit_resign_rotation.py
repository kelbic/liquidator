"""#127 R2 (СТАРВАЦИЯ ХВОСТА): аудит цены обходит протухшие ноги с ФИКСИРОВАННОГО начала списка
и обрывает по капу SPEC_FIRE_AUDIT_MAX_RESIGN. При устойчивом дребезге зеркала (мода<->статика<->
вторая мода в probe_tip_for) первые кап ног пере-протухают на КАЖДОМ проходе, кап всегда уходит
на них — хвост флота (105/110 ног в бою) не пересобирается НИКОГДА и застывает на старом tip
(а при экскурсии базы — с невключаемым max_fee) на всё время дребезга.

ЧТО ЗАКРЕПЛЯЕТСЯ (гоняем НАСТОЯЩЕЕ замыкание _audit из spec_fire_live — не пересказ боевой ветки;
шов над дефектом ловится тем, что на базе 85b4e15 падает поведенческий ассерт, а не импорт):
  1. КРАСНАЯ-НА-БАЗЕ (85b4e15): 8 ног протухли, кап 5, ТРИ прохода — обслуживаются одни и те же
     первые 5, хвост d5/d6/d7 не тронут НИКОГДА (union двух проходов != весь флот);
  2. ЗЕЛЁНАЯ-С-ФИКСОМ: круговой курсор ПО АДРЕСУ — за ceil(N/кап)=2 прохода обслужены ВСЕ 8,
     включая хвост; третий проход стартует со СЛЕДУЮЩЕЙ ноги (курсор переживает вызовы);
  3. кап НЕ тронут: ровно 5 ног за проход (поведенчески) и дефолт "5" (по исходнику);
  4. проводка: курсор живёт в теле _audit, порядок stale строится обходом addrs (legs_fn) — цел.

Шов на транспорте: invalidate_ladder/presign_ladder подменены фейками-регистраторами; реальная
подпись/сеть/треды огня НЕ поднимаются (FireEngine/ReceiptCollector — фейки, start() — no-op).
Без сети и без web3.
"""
import math
import sys
import threading
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import chain.execute as ex
import chain.fire_engine as fe
import chain.spec_fire_live as sfl

# Ручки аудита ПРИБИТЫ явно: замыкание читает МОДУЛЬНЫЕ глобалы при вызове, и env-переопределение
# на чужой машине иначе молча меняло бы смысл дрели. Сон между ногами в 0 — тест детерминирован.
sfl.SPEC_FIRE_AUDIT_MAX_RESIGN = 5
sfl.SPEC_FIRE_AUDIT_YIELD_SEC = 0.0
CAP = sfl.SPEC_FIRE_AUDIT_MAX_RESIGN

KEY = "0x" + "42" * 32
N = 8
ADDRS = ["0x%040x" % (0xD0 + i) for i in range(N)]   # 8 стабильных адресов-ног: ...d0 .. ...d7
BASEFEE = int(0.005e9)


# ---- фейковый транспорт: ни подписи, ни сети, ни тредов ----
class _FakeEth:
    def get_block(self, _b):
        return {"baseFeePerGas": BASEFEE}


class _FakeW3:
    eth = _FakeEth()


class _FakeRpc:
    chain_id = 8453
    rpc_url = "http://fake"

    def _web3(self):
        return _FakeW3()


CAPTURED = {}
SERVED = []   # адреса, чьи лестницы ФАКТИЧЕСКИ пересобраны в текущем проходе


class _FakeEngine:
    """Захватывает НАСТОЯЩЕЕ audit_fn; start() — no-op (треды огня НЕ поднимаются)."""

    def __init__(self, **kw):
        CAPTURED["audit"] = kw["audit_fn"]

    def start(self):
        pass


class _FakeCollector:
    def __init__(self, **kw):
        pass


# --- движок на фейки (без тредов, без сети) ---
fe.ENGINE_ON = True
fe.FireEngine = _FakeEngine
fe.ReceiptCollector = _FakeCollector

# --- execute: транспорт + модель «пере-протухания» (каждая нога протухла на КАЖДОМ проходе) ---
ex.volley_accounts = lambda: [(a, KEY) for a in ADDRS]
ex.probe_tip_for = lambda a: 1_000_000
ex.ladder_tip = lambda a: 1_000_000            # == probe_tip => учёт tip молчит, мерим ЧИСТУЮ ротацию
ex.note_tip_resign = lambda *a, **k: None
ex.ladder_depth = lambda a: 0
ex.ladder_resign_needed = lambda a, bf, tip: True   # дребезг зеркала: протухли ВСЕ, ВСЕГДА
ex.invalidate_ladder = lambda a: None


def _fake_presign(rpc, cfg, addr, k, gas, base_fee=None, log=None):
    SERVED.append(addr)                        # «пересобрали» ногу — только регистрация, без подписи
    return CAP


ex.presign_ladder = _fake_presign

cfg = types.SimpleNamespace(sequencer_rpc="off", rpc_url="http://fake")

# --- инстанс БЕЗ тяжёлого __init__: ставим ровно то, что трогает _engine/_audit ---
s = object.__new__(sfl.SpecFireLive)
s._lock = threading.Lock()
s.cfg = cfg
s.rpc = _FakeRpc()
s._volley_accounts_fn = None
s.stats = {}
s._log = lambda *a, **k: None
s._receipts_fn = lambda combat_url: (lambda hs: {})   # шэдоу метода: коллектор всё равно фейковый
s._index_fn = lambda: None
sfl._collector_node_kind = lambda: "fake"             # голая модульная функция — патчим МОДУЛЬ

# --- поднимаем ДВИЖОК (фейковый) и достаём НАСТОЯЩЕЕ audit_fn из spec_fire_live ---
eng = s._engine()
assert eng is not None and "audit" in CAPTURED, "движок не отдал audit_fn — харнесс не собрал замыкание"
audit = CAPTURED["audit"]


def _run_pass():
    SERVED.clear()
    audit(ADDRS)               # НАСТОЯЩИЙ проход аудита: stale=все 8, сервит по капу
    return list(SERVED)


p1 = _run_pass()
p2 = _run_pass()
p3 = _run_pass()

print("PASS1 served: %s" % [a[-2:] for a in p1])
print("PASS2 served: %s" % [a[-2:] for a in p2])
print("PASS3 served: %s" % [a[-2:] for a in p3])

# ---- ПОВЕДЕНЧЕСКИЕ ассерты ПЕРВЫМИ: на базе падение здесь = демонстрация старвации хвоста ----
for _i, _p in enumerate((p1, p2, p3), 1):
    assert len(_p) == CAP, "проход %d обслужил %d ног (кап=%d) — кап тронут" % (_i, len(_p), CAP)

need = math.ceil(N / CAP)      # = 2 прохода
covered = set()
for _p in (p1, p2, p3)[:need]:
    covered |= set(_p)
missing = [a[-2:] for a in ADDRS if a not in covered]
assert covered == set(ADDRS), (
    "СТАРВАЦИЯ ХВОСТА (#127 R2): за ceil(N/кап)=%d прохода обслужены НЕ все ноги — хвост %s "
    "не пересобран НИКОГДА (первые %d ног пере-протухают и монополизируют кап каждый проход)"
    % (need, missing, CAP))
print("BEHAV_OK: за %d прохода (ceil(%d/%d)) обслужены ВСЕ %d ног — хвост не голодает" % (need, N, CAP, N))

# Курсор реально КРУТИТСЯ, а не сбрасывается в голову каждый вызов: 3-й проход канонически сдвинут.
assert p3 != p1, "третий проход == первый: курсор ротации не переживает вызовы (сброс к голове)"
assert p3 == [ADDRS[2], ADDRS[3], ADDRS[4], ADDRS[5], ADDRS[6]], \
    "ротация неканонична: ждём хвост+обёртка [d2..d6], вышло %s" % [a[-2:] for a in p3]
print("BEHAV_OK: курсор аудита переживает проходы — 3-й старт со следующей ноги (%s)"
      % [a[-2:] for a in p3])

# ---- ПРОВОДКА ПО ИСХОДНИКУ (после поведенческих): курсор в аудите, кап и порядок legs_fn целы ----
src = _pl.Path("chain/spec_fire_live.py").read_text()
_a0 = src.index("def _audit(addrs):")
_a1 = src.index("def _combat_url", _a0)          # следующий вложенный def в _engine — граница тела _audit
audit_src = src[_a0:_a1]
assert "_audit_resign_cursor" in audit_src, "курсор ротации не в теле _audit — прибор не на месте"
assert 'os.environ.get("SPEC_FIRE_AUDIT_MAX_RESIGN", "5")' in src, "дефолт капа изменён (не '5')"
assert "n_resign >= SPEC_FIRE_AUDIT_MAX_RESIGN" in audit_src, "гейт капа изменён"
assert "stale = [a for a in (addrs or [])" in audit_src, "построение stale (порядок addrs) изменено"
print("WIRING_OK: курсор в теле _audit, кап-гейт и дефолт '5' целы, stale строится обходом addrs")

print("=== #127 R2 ЗАКРЫТ: ротация старта обхода — хвост флота получает пересборку за ceil(N/кап) проходов ===")
