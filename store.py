"""SQLite store: positions seen, simulations, PnL audit (append-only). Own DB only."""
from __future__ import annotations
import sqlite3
import time
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    borrower TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_hf REAL,
    last_seen INTEGER,
    UNIQUE(market_id, borrower)
);
CREATE TABLE IF NOT EXISTS simulations (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    borrower TEXT NOT NULL,
    repaid_usd REAL, seized_usd REAL, bonus_usd REAL, gas_usd REAL, net_usd REAL,
    would_submit INTEGER NOT NULL,   -- cleared min_profit + guards
    reverted INTEGER,                -- sim revert flag
    note TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    borrower TEXT NOT NULL,
    mode TEXT NOT NULL,              -- monitor (paper) | execute
    tx_hash TEXT,                    -- null in monitor mode
    net_usd REAL, gas_usd REAL,
    status TEXT                      -- paper | submitted | confirmed | failed
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert_position(self, market_id: str, borrower: str, hf: float):
        now = int(time.time())
        with closing(self._conn()) as c:
            c.execute(
                """INSERT INTO positions(market_id,borrower,first_seen,last_hf,last_seen)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(market_id,borrower)
                   DO UPDATE SET last_hf=excluded.last_hf, last_seen=excluded.last_seen""",
                (market_id, borrower, now, hf, now))
            c.commit()

    def _insert(self, table: str, kw: dict):
        kw.setdefault("ts", int(time.time()))
        cols = ",".join(kw); ph = ",".join("?" for _ in kw)
        with closing(self._conn()) as c:
            c.execute(f"INSERT INTO {table}({cols}) VALUES({ph})", tuple(kw.values()))
            c.commit()

    def log_simulation(self, **kw): self._insert("simulations", kw)
    def log_action(self, **kw): self._insert("actions", kw)

    def realized_today(self) -> dict:
        """Sum net+gas of executed actions since UTC midnight — feeds the kill switch."""
        since = int(time.time()) - (int(time.time()) % 86400)
        with closing(self._conn()) as c:
            row = c.execute(
                """SELECT COALESCE(SUM(net_usd),0) net, COALESCE(SUM(gas_usd),0) gas
                   FROM actions WHERE ts>=? AND mode='execute' AND status LIKE 'submitted:%'""", (since,)).fetchone()
            return {"net": row["net"], "gas": row["gas"]}
