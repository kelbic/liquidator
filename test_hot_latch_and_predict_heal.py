"""Транзиентная блокировка не защёлкивает диспатч (#52), предиктор поднимается сам (#50).

#52: инкрементальный hot-dispatch защёлкивался на ЛЮБОЙ причине блокировки, включая
транзиентную «нет свободных слотов inflight». Слоты освобождаются на квитанции за секунды, а
латч выключал хвост каскада на ТОМ САМОМ транзите, который и есть FCFS-гонка.

#50: predict-стек стартовал ОДНОКРАТНО — пустой резолв проксей давал `return` навсегда,
исключение оставляло слой мёртвым до рестарта. При RPC-шторме на буте (задокументированный
класс — hot_loop получил self-heal ровно за это) верх сигнального тракта молчал бы сутками.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from strategy.guard import KillSwitch, GuardState        # noqa: E402

ks = KillSwitch(max_daily_loss_usd=100.0, max_daily_gas_usd=500.0, max_inflight=3)

# --- 1. нехватка слотов — ТРАНЗИЕНТНАЯ причина -------------------------------------------
r = ks.blocked_reason(GuardState(realized_net_today=0.0, gas_spent_today=0.0, inflight=3))
assert r and ks.is_transient(r), "нехватка слотов признана терминальной — латч выключит каскад"

# --- 2. КОНТРОЛЬ: дневные капы — ТЕРМИНАЛЬНЫЕ (латч там и есть защита) -------------------
r2 = ks.blocked_reason(GuardState(realized_net_today=-200.0, gas_spent_today=0.0, inflight=0))
assert r2 and not ks.is_transient(r2), "дневной лосс стал транзиентным — защита снята"
r3 = ks.blocked_reason(GuardState(realized_net_today=0.0, gas_spent_today=999.0, inflight=0))
assert r3 and not ks.is_transient(r3), "дневной газ стал транзиентным — защита снята"

# --- 3. свободные слоты — блокировки нет вовсе ------------------------------------------
assert ks.blocked_reason(GuardState(realized_net_today=0.0, gas_spent_today=0.0, inflight=1)) is None
assert ks.is_transient(None) is False and ks.is_transient("") is False

# --- 4. СТРУКТУРНО: горячий путь ставит латч ТОЛЬКО на терминальной причине --------------
hp = pathlib.Path("chain/hotpath.py").read_text()
_blk = hp[hp.index("def _dispatch_ready_now"):]
_blk = _blk[:_blk.index("def _run(it=item)")]
assert "if guard.is_transient(blocked):" in _blk, \
    "латч снова ставится на любую причину — хвост каскада выключается транзиентно (#52)"
_tr = _blk.index("if guard.is_transient(blocked):")
_latch = _blk.index('_incr_state["blocked"] = blocked')
assert _tr < _latch, "проверка транзиентности стоит ПОСЛЕ латча — бессмысленна"
assert "skipped_transient" in _blk, "пропуски по слотам не считаются — отказ невидим"

# --- 5. #50: предиктор под надзором, с бэкоффом, и частичный резолв — громкий ------------
mn = pathlib.Path("main.py").read_text()
assert "_run_predict_supervised" in mn and "threading.Thread(target=_run_predict_supervised" in mn, \
    "предиктор снова стартует однократно — RPC-шторм на буте оставит его мёртвым (#50)"
assert 'return "retry"' in mn and 'return "ok"' in mn, "надзор не различает исход старта"
assert "_delay = min(_delay * 2, _PREDICT_RETRY_MAX_SEC)" in mn, "нет бэкоффа — ретрай зашумит"
assert "резолв ЧАСТИЧЕН" in mn, \
    "частичный резолв снова молчит — слой стартует 'живым' с урезанным покрытием фидов"

print("OK: слот-блокировка не защёлкивает диспатч, предиктор поднимается сам")
