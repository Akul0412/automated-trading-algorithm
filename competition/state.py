"""
MySQL persistence for competition bot (Railway managed MySQL).
Tables: strategy_positions, strategy_capital, daily_snapshots, trade_log.
"""

import datetime
import json
import logging

import pymysql

from competition import config

logger = logging.getLogger(__name__)


def _get_conn() -> pymysql.Connection:
    conn = pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        autocommit=False,
        cursorclass=pymysql.cursors.Cursor,
    )
    return conn


def init_db():
    """Create all competition tables if they don't exist."""
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_positions (
            id INT PRIMARY KEY AUTO_INCREMENT,
            strategy VARCHAR(255) NOT NULL,
            ticker VARCHAR(255) NOT NULL,
            side VARCHAR(255) NOT NULL DEFAULT 'long',
            shares DOUBLE NOT NULL,
            entry_price DOUBLE NOT NULL,
            stop_price DOUBLE,
            target_price DOUBLE,
            entry_time VARCHAR(255) NOT NULL,
            exit_price DOUBLE,
            exit_time VARCHAR(255),
            status VARCHAR(255) NOT NULL DEFAULT 'open',
            pnl DOUBLE,
            bars_held INT DEFAULT 0,
            notes TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_capital (
            strategy VARCHAR(255) PRIMARY KEY,
            allocated DOUBLE NOT NULL,
            used DOUBLE NOT NULL DEFAULT 0,
            realized_pnl DOUBLE NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id INT PRIMARY KEY AUTO_INCREMENT,
            timestamp VARCHAR(255) NOT NULL,
            total_equity DOUBLE,
            cash DOUBLE,
            long_exposure DOUBLE,
            short_exposure DOUBLE,
            net_exposure DOUBLE,
            momentum_pnl DOUBLE,
            mean_reversion_pnl DOUBLE,
            sector_rotation_pnl DOUBLE,
            notes TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id INT PRIMARY KEY AUTO_INCREMENT,
            timestamp VARCHAR(255) NOT NULL,
            strategy VARCHAR(255) NOT NULL,
            ticker VARCHAR(255) NOT NULL,
            side VARCHAR(255) NOT NULL,
            shares DOUBLE NOT NULL,
            order_type VARCHAR(255),
            requested_price DOUBLE,
            fill_price DOUBLE,
            slippage DOUBLE,
            status VARCHAR(255) NOT NULL,
            signal_details TEXT
        )
    """)

    # Indexes — CREATE INDEX IF NOT EXISTS is not standard MySQL,
    # so we use a helper approach with SHOW INDEX.
    _create_index_if_not_exists(cur, "strategy_positions", "idx_positions_strategy_status", "strategy, status")
    _create_index_if_not_exists(cur, "trade_log", "idx_trade_log_strategy", "strategy")

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Competition DB initialized (MySQL: %s)", config.MYSQL_HOST)


def _create_index_if_not_exists(cur, table: str, index_name: str, columns: str):
    """Create an index only if it doesn't already exist."""
    cur.execute("SHOW INDEX FROM %s WHERE Key_name = %%s" % table, (index_name,))
    if not cur.fetchone():
        cur.execute("CREATE INDEX %s ON %s (%s)" % (index_name, table, columns))


def init_capital_pools():
    """Initialize or reset capital pools for each strategy."""
    conn = _get_conn()
    cur = conn.cursor()
    pools = [
        ("momentum", config.MOMENTUM_CAPITAL),
        ("mean_reversion", config.MEAN_REVERSION_CAPITAL),
        ("sector_rotation", config.SECTOR_ROTATION_CAPITAL),
    ]
    for strategy, allocated in pools:
        cur.execute(
            """INSERT INTO strategy_capital (strategy, allocated, used, realized_pnl)
               VALUES (%s, %s, 0, 0)
               ON DUPLICATE KEY UPDATE allocated=VALUES(allocated)""",
            (strategy, allocated),
        )
    conn.commit()
    cur.close()
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
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO strategy_positions
           (strategy, ticker, side, shares, entry_price, stop_price, target_price,
            entry_time, status, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)""",
        (strategy, ticker, side, shares, entry_price, stop_price, target_price,
         datetime.datetime.now().isoformat(), notes),
    )
    pos_id = cur.lastrowid

    # Update capital used
    cost = shares * entry_price
    cur.execute(
        "UPDATE strategy_capital SET used = used + %s WHERE strategy = %s",
        (cost, strategy),
    )
    conn.commit()
    cur.close()
    conn.close()
    return pos_id


def close_position(pos_id: int, exit_price: float, reason: str = ""):
    """Close an open position and record PnL."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT strategy, ticker, side, shares, entry_price FROM strategy_positions WHERE id=%s",
        (pos_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return

    strategy, ticker, side, shares, entry_price = row
    if side == "long":
        pnl = (exit_price - entry_price) * shares
    else:
        pnl = (entry_price - exit_price) * shares

    cost = shares * entry_price
    cur.execute(
        """UPDATE strategy_positions
           SET exit_price=%s, exit_time=%s, status='closed', pnl=%s, notes=%s
           WHERE id=%s""",
        (exit_price, datetime.datetime.now().isoformat(), pnl, reason, pos_id),
    )
    cur.execute(
        "UPDATE strategy_capital SET used = used - %s, realized_pnl = realized_pnl + %s WHERE strategy = %s",
        (cost, pnl, strategy),
    )
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Closed position #%d %s %s: PnL=$%.2f (%s)", pos_id, side, ticker, pnl, reason)


def update_position_stops(pos_id: int, stop_price: float | None = None, target_price: float | None = None):
    """Update stop/target for an open position."""
    conn = _get_conn()
    cur = conn.cursor()
    if stop_price is not None:
        cur.execute("UPDATE strategy_positions SET stop_price=%s WHERE id=%s", (stop_price, pos_id))
    if target_price is not None:
        cur.execute("UPDATE strategy_positions SET target_price=%s WHERE id=%s", (target_price, pos_id))
    conn.commit()
    cur.close()
    conn.close()


def increment_bars_held(strategy: str):
    """Increment bars_held counter for all open positions in a strategy."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE strategy_positions SET bars_held = bars_held + 1 WHERE strategy=%s AND status='open'",
        (strategy,),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_open_positions(strategy: str | None = None) -> list[dict]:
    """Get open positions, optionally filtered by strategy."""
    conn = _get_conn()
    cur = conn.cursor()
    if strategy:
        cur.execute(
            """SELECT id, strategy, ticker, side, shares, entry_price, stop_price,
                      target_price, entry_time, bars_held, notes
               FROM strategy_positions WHERE strategy=%s AND status='open'""",
            (strategy,),
        )
    else:
        cur.execute(
            """SELECT id, strategy, ticker, side, shares, entry_price, stop_price,
                      target_price, entry_time, bars_held, notes
               FROM strategy_positions WHERE status='open'"""
        )
    rows = cur.fetchall()
    cur.close()
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
    cur = conn.cursor()
    cur.execute(
        "SELECT allocated, used, realized_pnl FROM strategy_capital WHERE strategy=%s",
        (strategy,),
    )
    row = cur.fetchone()
    cur.close()
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
    cur = conn.cursor()
    cur.execute(
        "SELECT side, shares, entry_price FROM strategy_positions WHERE status='open'"
    )
    rows = cur.fetchall()
    cur.close()
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
    cur = conn.cursor()
    slippage = None
    if requested_price and fill_price:
        slippage = abs(fill_price - requested_price) / requested_price

    cur.execute(
        """INSERT INTO trade_log
           (timestamp, strategy, ticker, side, shares, order_type,
            requested_price, fill_price, slippage, status, signal_details)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (datetime.datetime.now().isoformat(), strategy, ticker, side, shares,
         order_type, requested_price, fill_price, slippage, status,
         json.dumps(signal_details) if signal_details else None),
    )
    conn.commit()
    cur.close()
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
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO daily_snapshots
           (timestamp, total_equity, cash, long_exposure, short_exposure,
            net_exposure, momentum_pnl, mean_reversion_pnl, sector_rotation_pnl, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (datetime.datetime.now().isoformat(), total_equity, cash,
         long_exposure, short_exposure, long_exposure - short_exposure,
         momentum_pnl, mean_reversion_pnl, sector_rotation_pnl, notes),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_daily_start_equity() -> float | None:
    """Get the first snapshot equity of today, for daily loss tracking."""
    conn = _get_conn()
    cur = conn.cursor()
    today = datetime.date.today().isoformat()
    cur.execute(
        "SELECT total_equity FROM daily_snapshots WHERE timestamp >= %s ORDER BY timestamp ASC LIMIT 1",
        (today,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def get_peak_equity() -> float | None:
    """Get highest recorded equity across all snapshots."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(total_equity) FROM daily_snapshots"
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row and row[0] else None
