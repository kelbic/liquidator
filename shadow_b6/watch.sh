#!/bin/bash
# Сторож тени НА CHARLOTTE (крон каждые 5 мин). Мёртвый сторож хуже отсутствующего:
# «всё хорошо» требует ПОЛОЖИТЕЛЬНОГО доказательства — живости ЦИКЛА, а не файла.
# Три условия: (1) процесс из pidfile жив И это действительно наша тень (cmdline из /proc,
# НЕ pgrep -f — [[proc-kill-self-match]]); (2) heartbeat моложе 120 с; (3) счётчики
# флешблоков и блоков СТРОГО выросли с прошлой проверки.
set -u
D=/root/shadow-b6
HB=$D/data/heartbeat.json
PIDF=$D/data/shadow.pid
STATE=$D/data/watch.state
LOGF=$D/data/watch.log
PY=/root/liquidator/venv/bin/python
now=$(date -u +%FT%TZ)

say() { echo "$now $*" >> "$LOGF"; }

alive=0
if [ -f "$PIDF" ]; then
  pid=$(cat "$PIDF" 2>/dev/null || echo 0)
  if [ -n "${pid:-}" ] && [ "$pid" -gt 0 ] 2>/dev/null && [ -r "/proc/$pid/cmdline" ]; then
    if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "shadow.py"; then alive=1; fi
  fi
fi

verdict=$($PY - "$HB" "$STATE" <<'PYEOF'
import json, os, sys, time
hb_path, st_path = sys.argv[1], sys.argv[2]
try:
    hb = json.load(open(hb_path))
except Exception as e:
    print("BAD heartbeat нечитаем: %s" % str(e)[:60]); raise SystemExit
age = time.time() - float(hb.get("wall") or 0)
cnt = hb.get("cnt") or {}
cur = {k: int(cnt.get(k) or 0) for k in ("fb_msg", "blocks", "rounds", "decisions")}
prev = {}
if os.path.exists(st_path):
    try: prev = json.load(open(st_path))
    except Exception: prev = {}
elapsed = (time.time() - float(prev.get("t") or 0)) if prev else 0.0
json.dump({"t": time.time(), **cur}, open(st_path, "w"))
if age > 120:
    print("BAD heartbeat стар %.0f с (счётчики fb=%d blk=%d)" % (age, cur["fb_msg"], cur["blocks"]))
elif prev and elapsed >= 120 and (cur["fb_msg"] <= prev.get("fb_msg", -1) or cur["blocks"] <= prev.get("blocks", -1)):
    print("BAD цикл замер: fb %d->%d, blocks %d->%d (heartbeat свежий — это и есть мёртвый сторож)"
          % (prev.get("fb_msg", -1), cur["fb_msg"], prev.get("blocks", -1), cur["blocks"]))
else:
    print("OK age=%.0fс fb=%d blk=%d rounds=%d dec=%d" % (age, cur["fb_msg"], cur["blocks"], cur["rounds"], cur["decisions"]))
PYEOF
)

if [ "$alive" = "1" ] && [ "${verdict:0:2}" = "OK" ]; then
  say "OK процесс $pid жив, $verdict"
  exit 0
fi

say "ТРЕВОГА: процесс_жив=$alive verdict='$verdict' — рестарт"
if [ "$alive" = "1" ]; then kill -TERM "$pid" 2>/dev/null; sleep 3; kill -KILL "$pid" 2>/dev/null; fi
cd $D && setsid nice -n 19 $PY shadow.py >> data/shadow.stdout 2>&1 < /dev/null &
sleep 5
newpid=$(cat "$PIDF" 2>/dev/null || echo 0)
say "рестарт выполнен, новый pid=$newpid"
