"""Юниты пульса сторожей и надзора (урок 28.07: usde-watch молча падал на 429). Без сети/TG:
run_check принимает send-коллбек, транспорт (alerts) в heartbeat_check грузится только в main().
"""
import os
import sys
import tempfile
import time

import pathlib as _pl                 # РЕПО ЭТОГО ФАЙЛА, а не чужой worktree:
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from heartbeat import beat, age_sec
from analysis.heartbeat_check import REGISTRY, REALERT_SEC, run_check

# 0) страж каналов: импорт надзора НЕ должен тянуть транспорт (alerts грузится только в main())
assert "alerts" not in sys.modules, "heartbeat_check обязан импортировать alerts лениво (в main)"
print("UNIT_OK: импорт надзора не тянет транспорт (alerts не загружен)")

d = tempfile.mkdtemp(prefix="hb_test_")


def _pulse(name, ts):
    with open(os.path.join(d, name), "w") as f:
        f.write("%d\n" % int(ts))


# 1) beat/age_sec: запись атомарна, возраст ~0, содержимое — целые epoch-секунды
assert beat("bot-block", hb_dir=d)
a = age_sec("bot-block", hb_dir=d)
assert a is not None and a < 5.0, a
int(open(os.path.join(d, "bot-block")).read().strip())
assert age_sec("nonexistent", hb_dir=d) is None
print("UNIT_OK: beat/age_sec (атомарная запись, возраст ~0, нет файла -> None)")

# 2) все пульсы свежие -> тишина
NOW = time.time()
for n in REGISTRY:
    _pulse(n, NOW)
assert run_check(NOW, lambda t: (_ for _ in ()).throw(AssertionError(t)), hb_dir=d) == []
print("UNIT_OK: свежие пульсы -> ни одной тревоги")

# 3) протухание: bot-block (порог 300с) и usde-watch (7500с) молчат 8000с -> ровно 2 тревоги
_pulse("bot-block", NOW - 8000)
_pulse("usde-watch", NOW - 8000)
got = run_check(NOW, lambda t: None, hb_dir=d)
assert len(got) == 2 and all("\U0001F480" in m for m in got), got
assert any("usde-watch" in m for m in got) and any("bot-block" in m for m in got), got
print("UNIT_OK: 2 протухших пульса -> 2 тревоги (daily-summary в пороге — молчит)")

# 4) антиспам: повтор через 10 мин — тишина; через REALERT_SEC — напоминание
assert run_check(NOW + 600, lambda t: None, hb_dir=d) == []
got = run_check(NOW + REALERT_SEC + 1, lambda t: None, hb_dir=d)
assert len(got) == 2, got
print("UNIT_OK: антиспам (тишина 10 мин, напоминание раз в %dч)" % (REALERT_SEC // 3600))

# 5) отбой: пульс вернулся -> ✅ ровно по ожившим, состояние очищено (повторный прогон молчит)
_pulse("bot-block", NOW + REALERT_SEC + 100)
_pulse("usde-watch", NOW + REALERT_SEC + 100)
got = run_check(NOW + REALERT_SEC + 200, lambda t: None, hb_dir=d)
assert len(got) == 2 and all("✅" in m for m in got), got
assert run_check(NOW + REALERT_SEC + 300, lambda t: None, hb_dir=d) == []
print("UNIT_OK: отбой при ожившем пульсе, состояние очищено")

# 6) бутстрап: пустая директория — часы пошли от первого прогона, тревога только после порога
d2 = tempfile.mkdtemp(prefix="hb_boot_")
assert run_check(NOW, lambda t: None, hb_dir=d2) == [], "первый прогон без пульсов не тревожит"
got = run_check(NOW + 8000, lambda t: None, hb_dir=d2)
assert len(got) == 2, got
print("UNIT_OK: бутстрап пустой директории (часы от первого прогона, не ложная тревога)")

# 7) beat никогда не кидает (директория = файл -> False, не исключение)
bad = os.path.join(d, "bot-block", "sub")
assert beat("x", hb_dir=bad) is False
print("UNIT_OK: beat не кидает на битой директории")

print("=== ВСЕ HEARTBEAT-ЮНИТЫ ПРОШЛИ ===")

# 8) хост-фильтр реестра (30.07: usde-watch переехал на Вену): ONLY сужает, SKIP вычитает
import tempfile as _tf
from analysis.heartbeat_check import _host_registry
d8 = _tf.mkdtemp()
os.environ["HB_WATCH_ONLY"] = "usde-watch"
assert set(_host_registry()) == {"usde-watch"}
run_check(NOW, lambda t: None, hb_dir=d8)                       # бутстрап часов пустой директории
got8 = run_check(NOW + 10 * 86400, lambda t: None, hb_dir=d8)   # все давно молчат, но следим за одним
assert len(got8) == 1 and "usde-watch" in got8[0], got8
del os.environ["HB_WATCH_ONLY"]
os.environ["HB_WATCH_SKIP"] = "usde-watch"
assert "usde-watch" not in _host_registry() and "bot-block" in _host_registry()
del os.environ["HB_WATCH_SKIP"]
assert set(_host_registry()) == set(REGISTRY), "без env фильтр обязан быть прозрачным"
print("UNIT_OK: HB_WATCH_ONLY/SKIP — пер-хостовый реестр, без env поведение прежнее")
