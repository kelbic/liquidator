#!/bin/bash
# Суточная строка тени B+6 -> docs/research/SHADOW-B6-LOG.md (крон Вены, 06:30Z).
# В Telegram не слать (владелец: «пока ждём»).
set -u
DOC=/home/claude-agent/work/liquidator/docs/research/SHADOW-B6-LOG.md
DAY=${1:-$(date -u -d yesterday +%F)}
OUT=$(timeout 900 ssh -o ConnectTimeout=20 -o BatchMode=yes root@185.173.146.134 \
      "cd /root/shadow-b6 && nice -n 19 /root/liquidator/venv/bin/python daily.py $DAY" 2>&1)
if [ -z "$OUT" ]; then
  echo "$(date -u +%FT%TZ) суточный отчёт за $DAY НЕ ПОЛУЧЕН" >> /home/claude-agent/.fleet-watch/shadow-b6.log
  nice -n 19 python3 -c "
import sys; sys.path.insert(0,'/home/claude-agent/.fleet-watch'); import notify
notify.notify('суточная строка за $DAY не получена с Charlotte', source='shadow-b6', hil=False, key='shadow-b6:daily-$DAY', dedup_sec=7200)"
  exit 1
fi
LINE=$(printf '%s\n' "$OUT" | head -1)
DET=$(printf '%s\n' "$OUT" | tail -n +2)
printf '%s\n' "$LINE" >> "$DOC"
{ echo; echo "<details><summary>детали $DAY</summary>"; echo; echo '```'; printf '%s\n' "$DET"; echo '```'; echo "</details>"; } >> "$DOC"
cd /home/claude-agent/work/liquidator && git add docs/research/SHADOW-B6-LOG.md && \
  git -c user.name="claude-agent" -c user.email="kelbic.dm@gmail.com" commit -q -m "тень B+6: суточная строка за $DAY

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TiyJrRor1ue1vPcethh9t4" || true
echo "$(date -u +%FT%TZ) суточная строка за $DAY добавлена" >> /home/claude-agent/.fleet-watch/shadow-b6.log
