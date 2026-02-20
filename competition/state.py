"""
SQLite persistence for competition bot.
Tables: strategy_positions, strategy_capital, daily_snapshots, trade_log.
"""

import datetime
import json
import logging
import sqlite3

from competition import config

logger = logging.getLogger(__name__)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create all competition tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'long',
            shares REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL,
            target_price REAL,
            entry_time TEXT NOT NULL,
            exit_price REAL,
            exit_time TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            pnl REAL,
            bars_held INTEGER DEFAULT 0,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS strategy_capital (
            strategy TEXT PRIMARY KEY,
            allocated REAL NOT NULL,
            used REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            total_equity REAL,
            cash REAL,
            long_exposure REAL,
            short_exposure REAL,
            net_exposure REAL,
            momentum_pnl REAL,
            mean_reversion_pnl REAL,
            sector_rotation_pnl REAL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            strategy TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            shares REAL NOT NULL,
            order_type TEXT,
            requested_price REAL,
            fill_price REAL,
            slippage REAL,
            status TEXT NOT NULL,
            signal_details TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_positions_strategy_status
            ON strategy_positions(strategy, status);
        CREATE INDEX IF NOT EXISTS idx_trade_log_strategy
            ON trade_log(strategy);
    """)
    conn.commit()
    conn.close()
    logger.info("Competition DB initialized at %s", config.DB_PATH)


def init_capital_pools():
    """Initialize or reset capital pools for each strategy."""
    conn = _get_conn()
    pools = [
        ("momentum", config.MOMENTUM_CAPITAL),
        ("mean_reversion", config.MEAN_REVERSION_CAPITAL),
        ("sector_rotation", config.SECTOR_ROTATION_CAPITAL),
    ]
    for strategy, allocated in pools:
        conn.execute(
            """INSERT INTO strategy_capital (strategy, allocated, used, realized_pnl)
               VALUES (?, ?, 0, 0)
               ON CONFLICT(strategy) DO UPDATE SET allocated=excluded.allocated""",
            (strategy, allocated),
        )
    conn.commit()
    conn.close()
    logger.info("Capital pools initialized")


# --- Position management ---

def open_position(
    strategy: str, ticker: str, side: str, shares: float,
    entry_price: float, stop_price: float | None = None,
    target_price: float | None = None, notes: str = "",
) -> int:
    """Record a new open position. Returns position ID."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO strategy_positions
           (strategy, ticker, side, shares, entry_price, stop_price, target_price,
            entry_time, status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (strategy, ticker, side, shares, entry_price, stop_price, target_price,
         datetime.datetime.now().isoformat(), notes),
    )
    pos_id = cur.lastrowid

    # Update capital used
    cost = shares * entry_price
    conn.execute(
        "UPDATE strategy_capital SET used = used + ? WHERE strategy = ?",
        (cost, strategy),
    )
    conn.commit()
    conn.close()
    return pos_id


def close_position(pos_id: int, exit_price: float, reason: str = ""):
    """Close an open position and record PnL."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT strategy, ticker, side, shares, entry_price FROM strategy_positions WHERE id=?",
        (pos_id,),
    ).fetchone()
    if not row:
        conn.close()
        return

    strategy, ticker, side, shares, entry_price = row
    if side == "long":
        pnl = (exit_price - entry_price) * shares
    else:
        pnl = (entry_price - exit_price) * shares

    cost = shares * entry_price
    conn.execute(
        """UPDATE strategy_positions
           SET exit_price=?, exit_time=?, status='closed', pnl=?, notes=?
           WHERE id=?""",
        (exit_price, datetime.datetime.now().isoformat(), pnl, reason, pos_id),
    )
    conn.execute(
        "UPDATE strategy_capital SET used = used - ?, realized_pnl = realized_pnl + ? WHERE strategy = ?",
        (cost, pnl, strategy),
    )
    conn.commit()
    conn.close()
    logger.info("Closed position #%d %s %s: PnL=$%.2f (%s)", pos_id, side, ticker, pnl, reason)


def update_position_stops(pos_id: int, stop_price: float | None = None, target_price: float | None = None):
    """Update stop/target for an open position."""
    conn = _get_conn()
    if stop_price is not None:
        conn.execute("UPDATE strategy_positions SET stop_price=? WHERE id=?", (stop_price, pos_id))
    if target_price is not None:
        conn.execute("UPDATE strategy_positions SET target_price=? WHERE id=?", (target_price, pos_id))
    conn.commit()
    conn.close()


def increment_bars_held(strategy: str):
    """Increment bars_held counter for all open positions in a strategy."""
    conn = _get_conn()
    conn.execute(
        "UPDATE strategy_positions SET bars_held = bars_held + 1 WHERE strategy=? AND status='open'",
        (strategy,),
    )
    conn.commit()
    conn.close()


def get_open_positions(strategy: str | None = None) -> list[dict]:
    """Get open positions, optionally filtered by strategy."""
    conn = _get_conn()
    if strategy:
        rows = conn.execute(
            """SELECT id, strategy, ticker, side, shares, entry_price, stop_price,
                      target_price, entry_time, bars_held, notes
               FROM strategy_positions WHERE strategy=? AND status='open'""",
            (strategy,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, strategy, ticker, side, shares, entry_price, stop_price,
                      target_price, entry_time, bars_held, notes
               FROM strategy_positions WHERE status='open'"""
        ).fetchall()
    conn.close()

    positions = []
    for row in rows:
        positions.append({
            "id": row[0],
            "strategy": row[1],
            "ticker": row[2],
            "side": row[3],
            "shares": row[4],
            "entry_price": row[5],
            "stop_price": row[6],
            "target_price": row[7],
            "entry_time": row[8],
            "bars_held": row[9],
            "notes": row[10],
        })
    return positions


def get_strategy_capital(strategy: str) -> dict:
    """Get capital pool info for a strategy."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT allocated, used, realized_pnl FROM strategy_capital WHERE strategy=?",
        (strategy,),
    ).fetchone()
    conn.close()
    if not row:
        return {"allocated": 0, "used": 0, "realized_pnl": 0, "available": 0}
    allocated, used, realized_pnl = row
    return {
        "allocated": allocated,
        "used": used,
        "realized_pnl": realized_pnl,
        "available": allocated - used + realized_pnl,
    }


def get_total_exposure() -> dict:
    """Calculate total long and short exposure from open positions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT side, shares, entry_price FROM strategy_positions WHERE status='open'"
    ).fetchall()
    conn.close()

    long_exp = 0.0
    short_exp = 0.0
    for side, shares, price in rows:
        val = shares * price
        if side == "long":
            long_exp += val
        else:
            short_exp += val
    return {
        "long": long_exp,
        "short": short_exp,
        "net": long_exp - short_exp,
        "gross": long_exp + short_exp,
    }


# --- Trade logging ---

def log_trade(
    strategy: str, ticker: str, side: str, shares: float,
    order_type: str, requested_price: float | None,
    fill_price: float | None, status: str,
    signal_details: dict | None = None,
):
    """Log a trade execution."""
    conn = _get_conn()
    slippage = None
    if requested_price and fill_price:
        slippage = abs(fill_price - requested_price) / requested_price

    conn.execute(
        """INSERT INTO trade_log
           (timestamp, strategy, ticker, side, shares, order_type,
            requested_price, fill_price, slippage, status, signal_details)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.datetime.now().isoformat(), strategy, ticker, side, shares,
         order_type, requested_price, fill_price, slippage, status,
         json.dumps(signal_details) if signal_details else None),
    )
    conn.commit()
    conn.close()


# --- Snapshots ---

def take_snapshot(
    total_equity: float, cash: float,
    long_exposure: float, short_exposure: float,
    momentum_pnl: float = 0, mean_reversion_pnl: float = 0,
    sector_rotation_pnl: float = 0, notes: str = "",
):
    """Record a periodic equity/exposure snapshot."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO daily_snapshots
           (timestamp, total_equity, cash, long_exposure, short_exposure,
            net_exposure, momentum_pnl, mean_reversion_pnl, sector_rotation_pnl, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.datetime.now().isoformat(), total_equity, cash,
         long_exposure, short_exposure, long_exposure - short_exposure,
         momentum_pnl, mean_reversion_pnl, sector_rotation_pnl, notes),
    )
    conn.commit()
    conn.close()


def get_daily_start_equity() -> float | None:
    """Get the first snapshot equity of today, for daily loss tracking."""
    conn = _get_conn()
    today = datetime.date.today().isoformat()
    row = conn.execute(
        "SELECT total_equity FROM daily_snapshots WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
        (today,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_peak_equity() -> float | None:
    """Get highest recorded equity across all snapshots."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT MAX(total_equity) FROM daily_snapshots"
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None
