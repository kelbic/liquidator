"""Пульс сторожей (урок 28.07: usde-watch молча падал на 429 — «охраняемый» приз сторожили
мёртвым сервисом; мёртвый сторож хуже отсутствующего — он создаёт ложную уверенность).

Каждый монитор в конце УСПЕШНОГО цикла зовёт beat(name): атомарная запись epoch-секунд в
state/heartbeats/<name>. Надзор (analysis/heartbeat_check.py на systemd-таймере) сравнивает
возраст пульса с порогом реестра и тревожит TG. beat() никогда не кидает и не блокирует:
пульс не имеет права уронить или затормозить монитор.
"""
from __future__ import annotations
import os
import time

HB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "heartbeats")


def beat(name: str, hb_dir: str = "") -> bool:
    d = hb_dir or HB_DIR
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".%s.tmp" % name)
        with open(tmp, "w") as f:
            f.write("%d\n" % int(time.time()))
        os.replace(tmp, os.path.join(d, name))
        return True
    except Exception:
        return False


def age_sec(name: str, now: float | None = None, hb_dir: str = "") -> float | None:
    """Секунд с последнего пульса; None = пульса ещё не было (или файл нечитаем)."""
    d = hb_dir or HB_DIR
    try:
        with open(os.path.join(d, name)) as f:
            ts = float(f.read().strip())
        return max(0.0, (time.time() if now is None else now) - ts)
    except Exception:
        return None
