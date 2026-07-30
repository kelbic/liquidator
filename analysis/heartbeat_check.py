"""Надзор за сторожами (урок 28.07: usde-watch молча падал на 429 — приз «охранялся» мёртвым
сервисом). Пульс старше порога -> TG-тревога; ожил -> TG-отбой. Запуск: heartbeat-check.timer
каждые 10 мин; вручную: cd /root/liquidator && venv/bin/python -m analysis.heartbeat_check

Пороги НЕ слепые «2x каденс»: у bot-block каденс пульса 30с, но WS-реконнекты дают легитимные
паузы в минуты — порог 300с (5 минут слепоты = реальная тревога, а не блип). Отсутствующий
пульс бутстрапится часами в state-файле (файлы пульса пишут только владельцы): часы пошли,
монитор обязан отбиться за порог, иначе тревога.

Кто сторожит надзор: он сам бьёт пульс heartbeat-check, а его возраст печатает daily_summary —
независимый суточный канал, который человек читает каждый день.
"""
from __future__ import annotations
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from heartbeat import HB_DIR, age_sec, beat

REGISTRY = {
    # имя: (каденс_сек, порог_тревоги_сек)
    "bot-block":     (30,    300),      # блок-луп: реконнекты легитимно рвут пульс до минут
    "usde-watch":    (3600,  7500),     # hourly + джиттер -> 2x (30.07 живёт на Вене: HB_WATCH_*)
    "daily-summary": (86400, 176400),   # суточная сводка: 2x + час
}


def _host_registry() -> dict:
    """30.07: вотчеры разъезжаются по хостам (usde-watch -> Вена, разгрузка одноядерной
    Charlotte) — реестр один на все хосты, каждый хост фильтрует своё окружением:
    HB_WATCH_ONLY="a,b" — следить только за этими; HB_WATCH_SKIP="c" — за всеми, кроме."""
    only = {s.strip() for s in os.environ.get("HB_WATCH_ONLY", "").split(",") if s.strip()}
    skip = {s.strip() for s in os.environ.get("HB_WATCH_SKIP", "").split(",") if s.strip()}
    reg = {k: v for k, v in REGISTRY.items() if not only or k in only}
    return {k: v for k, v in reg.items() if k not in skip}
SELF_THRESHOLD_SEC = 1800               # порог самого надзора (таймер 10 мин) — для daily_summary
REALERT_SEC = 6 * 3600                  # пока молчит — напоминание раз в 6ч, не каждые 10 мин
_STATE = ".supervisor.json"


def _load_state(d: str) -> dict:
    try:
        with open(os.path.join(d, _STATE)) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("boot", {})
    st.setdefault("alerted", {})
    return st


def run_check(now: float, send, hb_dir: str = "") -> list[str]:
    """Одна проверка всех пульсов; send(text) зовётся на каждую тревогу/отбой.
    Возвращает отправленные тексты (для лога/тестов)."""
    d = hb_dir or HB_DIR
    os.makedirs(d, exist_ok=True)
    st = _load_state(d)
    sent = []
    for name, (cadence, threshold) in _host_registry().items():
        age = age_sec(name, now=now, hb_dir=d)
        if age is None:
            boot = st["boot"].setdefault(name, now)
            age = max(0.0, now - boot)
        if age > threshold:
            if now - st["alerted"].get(name, 0.0) >= REALERT_SEC:
                st["alerted"][name] = now
                msg = ("\U0001F480 сторож %s молчит %.0f мин (порог %.0f мин) — "
                       "systemctl status / journalctl -u" % (name, age / 60, threshold / 60))
                send(msg)
                sent.append(msg)
        elif name in st["alerted"]:
            del st["alerted"][name]
            msg = "✅ сторож %s ожил (пульс %.0fс назад)" % (name, age)
            send(msg)
            sent.append(msg)
    try:
        with open(os.path.join(d, _STATE), "w") as f:
            json.dump(st, f)
    except Exception:
        pass
    return sent


def main() -> None:
    from config import Config
    from alerts import Alerter
    cfg = Config.from_env()
    al = Alerter(cfg.tg_bot_token, cfg.tg_admin_id, antiflood_sec=0, tag=cfg.bot_tag)
    sent = run_check(time.time(), lambda t: al.send(t, key="hbcheck"))
    beat("heartbeat-check")
    print("heartbeat-check: %d событий" % len(sent))
    for m in sent:
        print("  " + m)


if __name__ == "__main__":
    main()
