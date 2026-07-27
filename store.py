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
        # WAL: читатели (realized_today — пред-send kill-switch чек в горячем пути) НЕ блокируются
        # писателями и наоборот -> под каскадом (hot-path воркеры + блок-луп пишут разом) нет
        # `database is locked`, который раньше всплывал в блок-луп → wss-реконнект РОВНО в каскад.
        # busy_timeout: писатель ждёт лок до 5с вместо мгновенного отказа. journal_mode персистентен
        # в файле БД (повторная установка — дешёвый no-op). Диск локальный (Charlotte) — WAL применим.
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        # 27.07: NORMAL (fsync только на чекпоинте, не на каждом commit). Дефолтный FULL при
        # ~50 commit/с (upsert на КАЖДУЮ позицию block-assess) давал 90%+ iowait на 1-ядерной
        # Charlotte -> recv-цикл flashblocks голодал -> сервер рвал WS как slow-consumer
        # (12 реконнектов/30мин, дифф-проба с Вены: эндпоинт здоров). Телеметрия переживёт
        # потерю хвоста при падении ОС; целостность БД NORMAL в WAL не трогает.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def upsert_position(self, market_id: str, borrower: str, hf: float):
        self.upsert_positions([(market_id, borrower, hf)])

    def upsert_positions(self, rows):
        """Батч: ОДНА транзакция/commit на весь assess-проход вместо connect+fsync на позицию.
        95-98 позиций × блок каждые 2с в горячем рынке = ~50 fsync/с — источник iowait-шторма
        27.07 (см. комментарий в _conn). rows: [(market_id, borrower, hf), ...]."""
        if not rows:
            return
        now = int(time.time())
        with closing(self._conn()) as c:
            c.executemany(
                """INSERT INTO positions(market_id,borrower,first_seen,last_hf,last_seen)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(market_id,borrower)
                   DO UPDATE SET last_hf=excluded.last_hf, last_seen=excluded.last_seen""",
                [(m, b, now, hf, now) for (m, b, hf) in rows])
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
