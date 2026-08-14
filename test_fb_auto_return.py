"""Юниты пакетов 05.08: (б) авто-возврат flashblocks (FbAutoReturn: эпизод/бэкофф x2 до капа/
сброс успехом/откат) и (в) двухскоростной каденс кэша позиций (cache_boost: штиль QUIET, окно MIN,
откат QUIET=0). Без сети."""
import sys
import threading
import time
import types

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from main import FbAutoReturn
import chain.hotpath as hp

# ---- 1) FbAutoReturn: не в фолбэке -> проб нет; эпизод стартует таймер с базы ----
fb = FbAutoReturn(base=600.0, cap=3600.0, enabled=True)
assert not fb.due(10**9)                          # нет фолбэка — нет проб
fb.on_fallback(1000.0)
assert fb.next_at == 1600.0
assert not fb.due(1599.0) and fb.due(1600.0)
print("OK 1: эпизод фолбэка стартует таймер с базы 600с")

# ---- 2) бэкофф x2 до капа (урок 07-21: per-IP лимит flashblocks не терпит частых коннектов) ----
fb.on_probe(1600.0)
assert fb.probing and fb.delay == 1200.0 and fb.next_at == 2800.0
fb.on_probe(2800.0)
assert fb.delay == 2400.0
fb.on_probe(5200.0)
assert fb.delay == 3600.0
fb.on_probe(8800.0)
assert fb.delay == 3600.0                         # кап держит
print("OK 2: бэкофф проб 600->1200->2400->кап 3600")

# ---- 3) успех (первый блок на flashblocks) сбрасывает эпизод целиком ----
fb.on_success()
assert fb.delay == 600.0 and fb.next_at is None and not fb.probing and not fb.due(10**9)
fb.on_fallback(50_000.0)                          # новый эпизод — снова с базы
assert fb.next_at == 50_600.0 and fb.delay == 600.0
print("OK 3: успех сбрасывает; новый эпизод с базы")

# ---- 4) откат FLASHBLOCK_AUTO_RETURN=0: due() всегда False ----
fb0 = FbAutoReturn(base=600.0, cap=3600.0, enabled=False)
fb0.on_fallback(0.0)
assert not fb0.due(10**9)
print("OK 4: откат — авто-возврат выключен, фолбэк односторонний как раньше")

# ---- 5) двухскоростной кэш позиций: штиль держит QUIET-гейт, буст (prearm-окно) открывает MIN ----
sh = types.SimpleNamespace(lock=threading.Lock(), groups={})
assert hp._CACHE_QUIET_INTERVAL > hp._CACHE_REFRESH_MIN_INTERVAL      # дефолт 120 > 30
hp._CACHE_BOOST_UNTIL = 0.0
hp._CACHE_REFRESH_STARTED = time.monotonic() - 40.0                   # цикл был 40с назад
hp._CACHE_REFRESH_THREAD = None
hp._spawn_cache_refresh(cache_rpc=None, shared=sh, bn=1)
assert hp._CACHE_REFRESH_THREAD is None                               # штиль: 40 < 120 — skip
hp.cache_boost(90.0)                                                  # окно живо
hp._spawn_cache_refresh(cache_rpc=None, shared=sh, bn=2)
assert hp._CACHE_REFRESH_THREAD is not None                           # бой: 40 >= 30 — цикл пошёл
hp._CACHE_REFRESH_THREAD.join(2.0)
print("OK 5: штиль=QUIET, prearm-буст=боевой MIN")

# ---- 6) откат QUIET=0: одна скорость (MIN) и без буста ----
hp._CACHE_BOOST_UNTIL = 0.0
_q = hp._CACHE_QUIET_INTERVAL
hp._CACHE_QUIET_INTERVAL = 0.0
hp._CACHE_REFRESH_STARTED = time.monotonic() - 40.0
hp._CACHE_REFRESH_THREAD = None
hp._spawn_cache_refresh(cache_rpc=None, shared=sh, bn=3)
assert hp._CACHE_REFRESH_THREAD is not None
hp._CACHE_REFRESH_THREAD.join(2.0)
hp._CACHE_QUIET_INTERVAL = _q
hp._CACHE_BOOST_UNTIL = 0.0
print("OK 6: откат QUIET=0 — прежний одноcкоростной каденс")

# ---- 7) гистерезис (ревью 05.08): успех только после stable_sec потока; хлопающий источник
#         не объявляет возврат и не сбрасывает бэкофф; счётчики fallback/restore ----
fbh = FbAutoReturn(base=600.0, cap=3600.0, enabled=True, stable_sec=30.0)
fbh.on_fallback(0.0)
assert fbh.n_fallbacks == 1
fbh.on_probe(600.0)
assert fbh.on_block(700.0) is False        # первый блок — старт отсчёта, не успех
assert fbh.on_block(720.0) is False        # 20с < 30с
assert fbh.on_block(731.0) is True         # стабилен 31с — возврат состоялся
assert fbh.n_restores == 1 and not fbh.probing and fbh.next_at is None and fbh.delay == 600.0
fbh.on_fallback(1000.0)
fbh.on_probe(1600.0)
assert fbh.on_block(1700.0) is False and fbh.on_block(1720.0) is False
fbh.probing = False                        # петля: обрыв до stable -> тихий probe-fail
assert fbh.n_restores == 1 and fbh.n_fallbacks == 2   # restore НЕ засчитан
assert fbh.due(1600.0 + fbh.delay)         # следующая проба строго по бэкоффу
print("OK 7: гистерезис 30с + счётчики (хлоп не считается возвратом)")

print("\nВСЕ БЛОКИ FB-AUTO-RETURN/BG-CU: OK")
