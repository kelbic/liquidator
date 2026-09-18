"""Второй слой сторожа — НА ВЕНЕ (крон каждые 10 мин). Тянет heartbeat тени с Charlotte по ssh.

Смысл второго слоя: сторож на самой Charlotte не может доложить о смерти Charlotte
([[dead-watchdog-worse-than-none]]). Адресат тревоги — ИНБОКС АГЕНТА (hil=False):
чинит агент, человеку решать нечего ([[alerts-only-where-human-acts]]).
"""
from __future__ import annotations
import json, os, subprocess, sys, time

HOST = "root@185.173.146.134"
STATE = os.path.expanduser("~/.fleet-watch/shadow-b6.state")
LOGDOC = "/home/claude-agent/work/liquidator/docs/research/SHADOW-B6-LOG.md"
MAX_AGE = 420.0        # крон 10 мин + запас; heartbeat пишется каждые 10 с
MIN_ELAPSED = 120.0   # рост счётчиков судим только если с прошлой проверки прошло больше


def sh(cmd, timeout=40):
    return subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", HOST, cmd],
                          capture_output=True, text=True, timeout=timeout)


def alarm(text, key):
    sys.path.insert(0, os.path.expanduser("~/.fleet-watch"))
    try:
        import notify
        who = notify.notify(text, source="shadow-b6", hil=False, key=key, dedup_sec=3600)
    except Exception as e:
        who = f"notify-сломан:{type(e).__name__}"
    line = f"\n> ⚠️ {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} сторож тени: {text} (адресат: {who})\n"
    try:
        with open(LOGDOC, "a") as f:
            f.write(line)
    except Exception:
        pass
    print("ТРЕВОГА:", text, "->", who)


def main():
    # ВАЖНО: rc команды не годится как признак — `tail` по ещё не созданному watch.log даёт 1
    # и первый же прогон дал ЛОЖНУЮ тревогу (18.09 07:21). Признак = наличие маркера в выводе.
    r = sh("cat /root/shadow-b6/data/heartbeat.json 2>/dev/null; echo '---'; "
           "tail -3 /root/shadow-b6/data/watch.log 2>/dev/null || true")
    if "---" not in r.stdout:
        alarm(f"Charlotte недоступна или heartbeat отсутствует (rc={r.returncode}, {r.stderr.strip()[:80]})",
              "shadow-b6:unreachable")
        return
    raw = r.stdout.split("---")[0].strip()
    if not raw:
        alarm("heartbeat пуст — тень не пишет", "shadow-b6:empty")
        return
    hb = json.loads(raw)
    age = time.time() - float(hb.get("wall") or 0)
    cnt = hb.get("cnt") or {}
    cur = {k: int(cnt.get(k) or 0) for k in ("fb_msg", "blocks", "rounds", "decisions", "events")}
    prev = {}
    if os.path.exists(STATE):
        try:
            prev = json.load(open(STATE))
        except Exception:
            prev = {}
    json.dump({"t": time.time(), **cur}, open(STATE, "w"))
    if age > MAX_AGE:
        alarm(f"heartbeat стар {age:.0f} с (fb_msg={cur['fb_msg']}, blocks={cur['blocks']})", "shadow-b6:stale")
        return
    elapsed = time.time() - float(prev.get("t") or 0) if prev else 0.0
    if prev and elapsed >= MIN_ELAPSED and (cur["fb_msg"] <= prev.get("fb_msg", -1)
                                            or cur["blocks"] <= prev.get("blocks", -1)):
        alarm(f"цикл замер при свежем heartbeat: fb {prev.get('fb_msg')}->{cur['fb_msg']}, "
              f"blocks {prev.get('blocks')}->{cur['blocks']}", "shadow-b6:frozen")
        return
    print(f"OK age={age:.0f}с fb={cur['fb_msg']} blk={cur['blocks']} rounds={cur['rounds']} "
          f"dec={cur['decisions']} ev={cur['events']} up={hb.get('up_sec',0)/3600:.1f}ч")


if __name__ == "__main__":
    main()
