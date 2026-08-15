"""Дрель В1: липкое окно — жить серию пушей; TTL/кап/нечитаемость закрывают.

Причина №4 вскрытия: окно закрывалось по первому транзиту, деньги шли на следующем пуше
(+30с нёс $27k). Теперь push засчитывает пуш серии и НЕ закрывает окно, пока живы гарды.
Тесты — на handle_event с фейковым состоянием; петля не запускается, сеть не трогается.
"""
import os
import sys
import threading
import types

os.environ.setdefault("SPEC_FIRE_V1", "1")

import chain.spec_fire_live as sfl                            # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("OK" if cond else "FAIL", name, (" — " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def mk(now=1000.0):
    """SpecFireLive без конструктора: только поля, которых касается ветка confirmed/push."""
    s = sfl.SpecFireLive.__new__(sfl.SpecFireLive)
    s._lock = threading.Lock()
    s._windows = {}
    s._win_birth = {}
    s._win_pushes = {}
    s._owed = {}
    s.stats = {}
    s._push_ts = {}
    s._logs = []
    s._log = lambda m: s._logs.append(m)
    s._now = lambda: s.t
    s.t = now
    s.note_push = lambda feed, updated_at=None: None      # решётка не под тестом
    return s


def ev_push(feed="XRP"):
    return {"event": "push", "feed": feed}


# --- 1. Пуш при живом окне и молодой серии -> окно ЖИВЁТ, пуш засчитан ---
s = mk()
stop = threading.Event()
s._windows["XRP"] = stop
s._win_birth["XRP"] = 1000.0
s.t = 1030.0                                              # возраст 30с < TTL 150
s.handle_event(ev_push())
check("серия: окно живёт", "XRP" in s._windows and not stop.is_set())
check("серия: пуш засчитан", s._win_pushes.get("XRP") == 1 and s.stats.get("v1_series_pushes") == 1)

# --- 2. Второй и третий пуш — живёт; четвёртый (кап MAX_PUSHES=4) — закрывает ---
s.t = 1060.0; s.handle_event(ev_push())
s.t = 1090.0; s.handle_event(ev_push())
check("серия: три пуша засчитаны", s._win_pushes.get("XRP") == 3 and "XRP" in s._windows)
s.t = 1120.0; s.handle_event(ev_push())                   # _pushes станет 4 == MAX -> закрытие
check("кап пушей: окно закрыто", "XRP" not in s._windows and stop.is_set())

# --- 3. Жёсткий TTL серии: пуш после TTL закрывает даже первый ---
s = mk(); stop = threading.Event()
s._windows["XRP"] = stop
s._win_birth["XRP"] = 1000.0
s.t = 1000.0 + sfl.SPEC_FIRE_V1_SERIES_TTL_SEC + 1
s.handle_event(ev_push())
check("TTL серии: окно закрыто", "XRP" not in s._windows and stop.is_set())

# --- 4. Окна нет -> ветка серии не мешает (нет ключа) ---
s = mk()
s.handle_event(ev_push())
check("без окна: тишина без исключений", s._win_pushes.get("XRP") is None)

# --- 5. Рождение неизвестно (birth=0/нет) -> закрыт# --- 5. Рождение неизвестно (birth нет) -> закрывается как раньше (незнание закрывает) ---
s = mk(); stop = threading.Event()
s._windows["XRP"] = stop                                  # окно есть, birth НЕ записан
s.handle_event(ev_push())
check("нет birth: окно закрыто (незнание)", "XRP" not in s._windows and stop.is_set())

# --- 6. ОТКАТ SPEC_FIRE_V1=0: пуш закрывает окно бит-в-бит ---
_saved = sfl.SPEC_FIRE_V1
sfl.SPEC_FIRE_V1 = False
try:
    s = mk(); stop = threading.Event()
    s._windows["XRP"] = stop
    s._win_birth["XRP"] = 1000.0
    s.t = 1010.0
    s.handle_event(ev_push())
    check("откат: пуш закрыл окно", "XRP" not in s._windows and stop.is_set())
finally:
    sfl.SPEC_FIRE_V1 = _saved

# --- 7. disarm/falsepos/stale ветки НЕ тронуты серией (закрывают как раньше без owed) ---
s = mk(); stop = threading.Event()
s._windows["XRP"] = stop
s._win_birth["XRP"] = 1000.0
s.t = 1010.0
s.handle_event({"event": "stale", "feed": "XRP"})
check("stale: закрывает как раньше", "XRP" not in s._windows and stop.is_set())

# --- 8. ПРОВОДКА: арм пишет birth; петля несёт гард нечитаемости; WIN-закрытие не тронуто ---
src = open(sfl.__file__).read()
i_arm = src.find('SPECFIRE LIVE window feed=')
check("проводка: birth на арме", i_arm > 0 and "_win_birth[feed] = self._now()" in src[i_arm - 400:i_arm])
check("проводка: гард нечитаемости в петле", "нечитаемых тиков подряд" in src and "_unreadable = 0" in src)
i_win = src.find("SPECFIRE WIN!!!")
check("проводка: WIN-закрытие не тронуто (замороженный слой)",
      i_win > 0 and "V1" not in src[i_win - 600:i_win])
check("боевой дефолт: SPEC_FIRE_V1 включён", sfl.SPEC_FIRE_V1 is True)

if FAILS:
    print("=== ДРЕЛЬ В1 КРАСНАЯ: %d провалов ===" % len(FAILS))
    sys.exit(1)
print("=== ДРЕЛЬ В1 ЗЕЛЁНАЯ: окно живёт серию, TTL/кап/незнание закрывают, откат бит-в-бит ===")
