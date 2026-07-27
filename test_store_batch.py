"""Юниты батч-апсерта (27.07, iowait-фикс): 1 commit на assess-проход вместо ~98.
Без сети; SQLite во временном файле."""
import os
import sys
import tempfile
sys.path.insert(0, "/home/claude-agent/work/liquidator/.claude/worktrees/latency-race")
from store import Store

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "t.db")
s = Store(db)

# 1) батч вставляет и апдейтит через ON CONFLICT
s.upsert_positions([("0xm1", "0xb1", 1.05), ("0xm1", "0xb2", 1.10), ("0xm2", "0xb1", 0.99)])
s.upsert_positions([("0xm1", "0xb1", 1.01)])   # апдейт существующей
import sqlite3
c = sqlite3.connect(db)
rows = {(m, b): hf for m, b, hf in c.execute("SELECT market_id,borrower,last_hf FROM positions")}
assert len(rows) == 3, rows
assert abs(rows[("0xm1", "0xb1")] - 1.01) < 1e-9, "апдейт last_hf не применился"
print("UNIT_OK: батч вставляет и апдейтит (3 строки, конфликт -> update)")

# 2) пустой батч — no-op (не открывает соединение зря)
s.upsert_positions([])
print("UNIT_OK: пустой батч — no-op")

# 3) обратная совместимость: одиночный upsert_position жив
s.upsert_position("0xm3", "0xb9", 1.5)
n = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
assert n == 4, n
print("UNIT_OK: одиночный upsert_position работает через батч")

# 4) PRAGMA synchronous=NORMAL реально применяется на соединении Store
conn = s._conn()
assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1, "ожидали NORMAL(1)"
conn.close()
print("UNIT_OK: synchronous=NORMAL на соединениях Store (fsync только на чекпоинте)")

# 5) порядок в process_candidates: деньги -> потом телеметрия (стрёмная регрессия для гонки)
import re
src = open("/home/claude-agent/work/liquidator/.claude/worktrees/latency-race/main.py").read()
i_exec = src.index("_execute_actionable(rpc, cfg, actionable")
i_ups = src.index("store.upsert_positions(_pos_rows)")
i_sim = src.index("for _kw in _sim_rows")
assert i_exec < i_ups < i_sim, "телеметрия обязана идти ПОСЛЕ _execute_actionable"
assert "store.upsert_position(mid" not in src, "в цикле не должно остаться одиночных апсертов"
print("UNIT_OK: порядок main.py — деньги раньше телеметрии, в цикле апсертов нет")

print("=== ВСЕ STORE-BATCH ЮНИТЫ ПРОШЛИ ===")
