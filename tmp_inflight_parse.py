#!/usr/bin/env python3
# One-pass journal parser for _INFLIGHT interval measurement. Reads short-unix journalctl stream from stdin.
import sys, re, json
from collections import Counter, deque
from datetime import datetime, timezone

RE_TS = re.compile(r'^(\d+\.\d+)\s')
RE_DISPATCH = re.compile(r'dispatch: sent nonce=(\d+) n=(\d+).*?tx=([0-9a-fA-F]{8})')
RE_TX = re.compile(r'tx=(?:0x)?([0-9a-fA-F]{8})')
RE_FEED = re.compile(r'feed=(\S+)')
RE_PROBES = re.compile(r'probes=(\d+)')
RE_SUBMITTED = re.compile(r'submitted:(\w+)')

total = 0
unparsed = 0
dispatch_parse_fail = 0
volley_odd = 0  # dispatch lines containing 'volley' but not '(volley)'

dispatches = []            # dicts: ts, nonce, tx8, volley
dispatch_ts = []           # parallel list of ts for window checks
terminals = {}             # tx8 -> list of (ts, kind), chronological
xray_ts = []
hot_ready_ts = []
handoff_ts = []
evdensity_ts = []
send3_ts = []
sub_counts = Counter()

PROBE_CAP = 4000
probes_near = []
probes_over_cap = 0
probes_pending = deque()   # ts of probes awaiting a future dispatch
probes_dropped_no_dispatch = 0

wc_near = []
wc_pending = deque()       # (ts, feed, probes)
wc_dropped_no_dispatch = 0

first_ts = None
nonmono = 0
prev_ts = None

def add_probe(ts):
    global probes_over_cap
    if len(probes_near) < PROBE_CAP:
        probes_near.append(round(ts, 3))
    else:
        probes_over_cap += 1

def add_term(ts, tx8, kind):
    terminals.setdefault(tx8, []).append((ts, kind))

for raw in sys.stdin:
    total += 1
    m = RE_TS.match(raw)
    if not m:
        unparsed += 1
        continue
    ts = float(m.group(1))
    if first_ts is None:
        first_ts = ts
    if prev_ts is not None and ts < prev_ts - 1.0:
        nonmono += 1
    prev_ts = ts
    line = raw

    # prune pendings that can no longer be qualified by any future dispatch
    while probes_pending and probes_pending[0] < ts - 180.0:
        probes_pending.popleft()
        probes_dropped_no_dispatch += 1
    while wc_pending and wc_pending[0][0] < ts - 300.0:
        wc_pending.popleft()
        wc_dropped_no_dispatch += 1

    if 'dispatch: sent nonce=' in line:
        dm = RE_DISPATCH.search(line)
        if dm:
            volley = '(volley)' in line
            if (not volley) and 'volley' in line:
                volley_odd += 1
            dispatches.append({'ts': round(ts, 3), 'nonce': int(dm.group(1)),
                               'tx8': dm.group(3).lower(), 'volley': volley})
            dispatch_ts.append(ts)
            # qualify pending probes/window-closes (all remaining are within window)
            while probes_pending:
                add_probe(probes_pending.popleft())
            while wc_pending:
                w = wc_pending.popleft()
                wc_near.append({'ts': round(w[0], 3), 'feed': w[1], 'probes': w[2]})
        else:
            dispatch_parse_fail += 1

    if 'submit-xray:' in line:
        xray_ts.append(round(ts, 3))
        if 'submit-xray: WIN' in line:
            tm = RE_TX.search(line)
            if tm:
                add_term(ts, tm.group(1).lower(), 'win')

    # Пост-фикс 08.08: точный конец брекета логируется у декремента в проходе 1 —
    # это ГЛАВНЫЙ терминал; старые маркеры ниже остаются для журналов до фикса.
    if 'dispatch: inflight released' in line:
        tm = RE_TX.search(line)
        if tm:
            add_term(ts, tm.group(1).lower(), 'released')

    if 'liquidate revert' in line:
        tm = RE_TX.search(line)
        if tm:
            add_term(ts, tm.group(1).lower(), 'revert')
    if 'batch revert (top-level)' in line:
        tm = RE_TX.search(line)
        if tm:
            add_term(ts, tm.group(1).lower(), 'batch_revert')
    elif 'batch receipt tx=' in line:
        tm = RE_TX.search(line)
        if tm:
            add_term(ts, tm.group(1).lower(), 'batch')

    if 'HOT ready' in line:
        hot_ready_ts.append(round(ts, 3))
    if 'SPECFIRE ws-handoff' in line:
        handoff_ts.append(round(ts, 3))
    if 'SPECFIRE ev-density' in line:
        evdensity_ts.append(round(ts, 3))
    if 'SEND-3 uncertain' in line:
        send3_ts.append(round(ts, 3))

    if 'SPECFIRE probe #' in line:
        if dispatch_ts and dispatch_ts[-1] >= ts - 180.0:
            add_probe(ts)
        else:
            probes_pending.append(ts)

    if 'SPECFIRE window close' in line:
        fm = RE_FEED.search(line)
        pm = RE_PROBES.search(line)
        entry = (ts, fm.group(1) if fm else None, int(pm.group(1)) if pm else None)
        if dispatch_ts and dispatch_ts[-1] >= ts - 300.0:
            wc_near.append({'ts': round(ts, 3), 'feed': entry[1], 'probes': entry[2]})
        else:
            wc_pending.append(entry)

    m2 = RE_SUBMITTED.search(line)
    if m2:
        sub_counts['submitted:' + m2.group(1)] += 1

# leftover pendings never qualified
probes_dropped_no_dispatch += len(probes_pending)
wc_dropped_no_dispatch += len(wc_pending)

# match dispatches to first terminal with same tx8, ts >= dispatch ts, within 130s
no_terminal = 0
for d in dispatches:
    end_ts = None
    end_kind = None
    for tts, kind in terminals.get(d['tx8'], []):
        if tts >= d['ts'] and tts - d['ts'] <= 130.0:
            end_ts = round(tts, 3)
            end_kind = kind
            break
    d['end_ts'] = end_ts
    d['end_kind'] = end_kind
    if end_ts is None:
        no_terminal += 1

retention_start = None
if first_ts is not None:
    retention_start = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M')

notes_parts = [
    'total journal lines read: %d' % total,
    'lines without epoch prefix (unparsed/continuation): %d' % unparsed,
    'dispatches: %d, without terminal marker within 130s: %d' % (len(dispatches), no_terminal),
    'dispatch lines matching "dispatch: sent nonce=" but regex parse failed: %d' % dispatch_parse_fail,
    'dispatch lines containing "volley" but not "(volley)": %d' % volley_odd,
    'probes_near capped at %d, dropped over cap: %d; probe lines with no dispatch within +/-180s (excluded): %d' % (PROBE_CAP, probes_over_cap, probes_dropped_no_dispatch),
    'window close lines with no dispatch within +/-300s (excluded): %d' % wc_dropped_no_dispatch,
    'non-monotonic timestamp jumps (>1s backwards): %d' % nonmono,
    'all ts rounded to 3 decimals (epoch seconds); retention_start is UTC',
]

out = {
    'retention_start': retention_start,
    'n_dispatch_total': len(dispatches),
    'dispatches': dispatches,
    'xray_ts': xray_ts,
    'hot_ready_ts': hot_ready_ts,
    'handoff_ts': handoff_ts,
    'evdensity_ts': evdensity_ts,
    'probes_near': probes_near,
    'window_close_near': wc_near,
    'submitted_status_counts': dict(sub_counts),
    'send3_uncertain_ts': send3_ts,
    'notes': '; '.join(notes_parts),
}
json.dump(out, sys.stdout)
