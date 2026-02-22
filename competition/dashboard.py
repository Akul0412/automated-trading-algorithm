"""
Dashboard — beautiful CLI to view everything the competition bot is doing.

Usage:
  python -m competition.dashboard              # Full summary
  python -m competition.dashboard trades       # All trades
  python -m competition.dashboard positions    # Open positions
  python -m competition.dashboard pnl          # P&L breakdown
  python -m competition.dashboard history      # Equity over time
  python -m competition.dashboard export       # Export all data to CSV
"""

import argparse
import csv
import datetime
import os
import sys

import pymysql

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from competition import config

console = Console()

LOGO = """
  ___       _   _                                   _     _    _       _
 / _ \\ _ __| |_| |__   ___   __ _  ___  _ __   __ _| |   / \\  | |_ __ | |__   __ _
| | | | '__| __| '_ \\ / _ \\ / _` |/ _ \\| '_ \\ / _` | |  / _ \\ | | '_ \\| '_ \\ / _` |
| |_| | |  | |_| | | | (_) | (_| | (_) | | | | (_| | | / ___ \\| | |_) | | | | (_| |
 \\___/|_|   \\__|_| |_|\\___/ \\__, |\\___/|_| |_|\\__,_|_|/_/   \\_\\_| .__/|_| |_|\\__,_|
                             |___/                                |_|"""


def _connect():
    if not config.MYSQL_URL:
        console.print("[red]MYSQL_URL not set — cannot connect to database[/red]")
        sys.exit(1)
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        cursorclass=pymysql.cursors.Cursor,
    )


def _pnl_color(value):
    if value > 0:
        return "green"
    elif value < 0:
        return "red"
    return "white"


def _pnl_str(value):
    color = _pnl_color(value)
    return f"[{color}]${value:>+,.2f}[/{color}]"


def _pct_str(value):
    color = _pnl_color(value)
    return f"[{color}]{value:>+.2f}%[/{color}]"


def _side_str(side):
    if side == "long":
        return "[green]LONG[/green]"
    elif side == "short":
        return "[red]SHORT[/red]"
    return side


def _status_str(status):
    colors = {"FILLED": "green", "DRY_RUN": "yellow", "FAILED": "red", "PENDING": "cyan"}
    color = colors.get(status, "white")
    return f"[{color}]{status}[/{color}]"


# ═══════════════════════════════════════════════════════
#  FULL SUMMARY
# ═══════════════════════════════════════════════════════

def show_summary():
    conn = _connect()
    cur = conn.cursor()

    console.print(f"[bold cyan]{LOGO}[/bold cyan]")
    console.print()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Account Panel ──
    cur.execute(
        "SELECT total_equity, cash, long_exposure, short_exposure, net_exposure, timestamp "
        "FROM daily_snapshots ORDER BY id DESC LIMIT 1"
    )
    snap = cur.fetchone()

    account_table = Table(show_header=False, box=None, padding=(0, 2))
    account_table.add_column("Label", style="dim")
    account_table.add_column("Value", justify="right")

    if snap:
        equity, cash, long_exp, short_exp, net_exp, snap_time = snap
        pnl_total = equity - config.TOTAL_CAPITAL
        pnl_pct = pnl_total / config.TOTAL_CAPITAL * 100

        account_table.add_row("Equity", f"[bold white]${equity:>14,.2f}[/bold white]")
        account_table.add_row("P&L", _pnl_str(pnl_total) + f"  ({_pct_str(pnl_pct)})")
        account_table.add_row("Cash", f"${cash:>14,.2f}")
        account_table.add_row("Long Exposure", f"[green]${long_exp:>14,.2f}[/green]")
        account_table.add_row("Short Exposure", f"[red]${short_exp:>14,.2f}[/red]")
        account_table.add_row("Net Exposure", f"${net_exp:>14,.2f}")
        account_table.add_row("Last Update", f"[dim]{snap_time[:19]}[/dim]")
    else:
        account_table.add_row("Status", "[yellow]Waiting for first cycle...[/yellow]")

    console.print(Panel(account_table, title=f"[bold]{now}[/bold]  |  Account Overview",
                        border_style="cyan", box=box.HEAVY))

    # ── Capital Allocation ──
    cap_table = Table(box=box.SIMPLE_HEAVY, border_style="blue", show_lines=False)
    cap_table.add_column("Strategy", style="bold white", min_width=18)
    cap_table.add_column("Allocated", justify="right", style="dim")
    cap_table.add_column("In Use", justify="right", style="cyan")
    cap_table.add_column("Realized P&L", justify="right")
    cap_table.add_column("Available", justify="right", style="bold")

    cur.execute("SELECT strategy, allocated, used, realized_pnl FROM strategy_capital")
    rows = cur.fetchall()
    strat_icons = {"momentum": ">>> ", "mean_reversion": "<-> ", "sector_rotation": "~~~ "}

    total_pnl = 0
    for strategy, allocated, used, pnl in rows:
        available = allocated - used + pnl
        total_pnl += pnl
        icon = strat_icons.get(strategy, "")
        name = icon + strategy.replace("_", " ").title()
        cap_table.add_row(
            name,
            f"${allocated:>11,.0f}",
            f"${used:>11,.0f}",
            _pnl_str(pnl),
            f"${available:>11,.0f}",
        )

    cap_table.add_section()
    cap_table.add_row("[bold]TOTAL[/bold]", "", "", f"[bold]{_pnl_str(total_pnl)}[/bold]", "")

    console.print(Panel(cap_table, title="Capital Allocation", border_style="blue", box=box.HEAVY))

    # ── Open Positions ──
    pos_table = Table(box=box.SIMPLE_HEAVY, border_style="yellow")
    pos_table.add_column("Strategy", style="bold")
    pos_table.add_column("Ticker", style="bold cyan")
    pos_table.add_column("Side", justify="center")
    pos_table.add_column("Shares", justify="right")
    pos_table.add_column("Entry", justify="right")
    pos_table.add_column("Stop", justify="right", style="red")
    pos_table.add_column("Target", justify="right", style="green")
    pos_table.add_column("Entered", style="dim")

    cur.execute(
        "SELECT strategy, ticker, side, shares, entry_price, stop_price, target_price, entry_time "
        "FROM strategy_positions WHERE status='open' ORDER BY strategy, entry_time"
    )
    positions = cur.fetchall()

    if positions:
        for strat, ticker, side, shares, entry, stop, target, entry_time in positions:
            pos_table.add_row(
                strat.replace("_", " ").title(),
                ticker,
                _side_str(side),
                f"{shares:,.0f}",
                f"${entry:,.2f}",
                f"${stop:,.2f}" if stop else "-",
                f"${target:,.2f}" if target else "-",
                entry_time[:16],
            )
    else:
        pos_table.add_row("[dim]No open positions[/dim]", "", "", "", "", "", "", "")

    console.print(Panel(pos_table, title=f"Open Positions ({len(positions)})",
                        border_style="yellow", box=box.HEAVY))

    # ── Recent Trades ──
    trade_table = Table(box=box.SIMPLE_HEAVY, border_style="magenta")
    trade_table.add_column("Time", style="dim")
    trade_table.add_column("Strategy")
    trade_table.add_column("Ticker", style="bold cyan")
    trade_table.add_column("Side", justify="center")
    trade_table.add_column("Shares", justify="right")
    trade_table.add_column("Fill Price", justify="right")
    trade_table.add_column("Status", justify="center")

    cur.execute(
        "SELECT timestamp, strategy, ticker, side, shares, fill_price, status "
        "FROM trade_log ORDER BY id DESC LIMIT 10"
    )
    trades = cur.fetchall()

    if trades:
        for ts, strat, ticker, side, shares, price, status in trades:
            side_display = "[green]BUY[/green]" if side == "buy" else "[red]SELL[/red]"
            trade_table.add_row(
                ts[:19],
                strat.replace("_", " ").title(),
                ticker,
                side_display,
                f"{shares:,.0f}",
                f"${price:,.2f}" if price else "-",
                _status_str(status),
            )
    else:
        trade_table.add_row("[dim]No trades yet[/dim]", "", "", "", "", "", "")

    console.print(Panel(trade_table, title="Recent Trades (last 10)",
                        border_style="magenta", box=box.HEAVY))

    # ── Stats Bar ──
    cur.execute("SELECT COUNT(*) FROM trade_log")
    total_trades = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM trade_log WHERE status IN ('FILLED', 'DRY_RUN')")
    filled = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM trade_log WHERE status='FAILED'")
    failed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM strategy_positions WHERE status='closed'")
    closed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM strategy_positions WHERE status='closed' AND pnl > 0")
    winners = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(pnl), 0) FROM strategy_positions WHERE status='closed'")
    total_realized = cur.fetchone()[0]

    win_rate = f"{winners / closed * 100:.1f}%" if closed > 0 else "N/A"

    stats = Table(show_header=False, box=None, padding=(0, 3), expand=True)
    stats.add_column("", justify="center")
    stats.add_column("", justify="center")
    stats.add_column("", justify="center")
    stats.add_column("", justify="center")
    stats.add_column("", justify="center")

    stats.add_row(
        f"[bold]Orders[/bold]\n[cyan]{total_trades}[/cyan]",
        f"[bold]Filled[/bold]\n[green]{filled}[/green]",
        f"[bold]Failed[/bold]\n[red]{failed}[/red]",
        f"[bold]Win Rate[/bold]\n[yellow]{win_rate}[/yellow]",
        f"[bold]Realized P&L[/bold]\n{_pnl_str(total_realized)}",
    )

    console.print(Panel(stats, title="Statistics", border_style="white", box=box.HEAVY))

    cur.close()
    conn.close()


# ═══════════════════════════════════════════════════════
#  TRADES VIEW
# ═══════════════════════════════════════════════════════

def show_trades():
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, timestamp, strategy, ticker, side, shares, order_type, "
        "requested_price, fill_price, slippage, status, signal_details "
        "FROM trade_log ORDER BY id"
    )
    trades = cur.fetchall()

    table = Table(title="All Trades", box=box.HEAVY, border_style="magenta",
                  show_lines=False, row_styles=["", "dim"])
    table.add_column("#", style="dim", justify="right")
    table.add_column("Time", style="dim")
    table.add_column("Strategy")
    table.add_column("Ticker", style="bold cyan")
    table.add_column("Side", justify="center")
    table.add_column("Shares", justify="right")
    table.add_column("Type")
    table.add_column("Requested", justify="right")
    table.add_column("Filled", justify="right")
    table.add_column("Slippage", justify="right")
    table.add_column("Status", justify="center")

    if not trades:
        table.add_row("[dim]No trades recorded yet[/dim]", *[""] * 10)
    else:
        for tid, ts, strat, ticker, side, shares, otype, req_p, fill_p, slip, status, details in trades:
            side_display = "[green]BUY[/green]" if side == "buy" else "[red]SELL[/red]"
            slip_str = f"{slip * 100:.4f}%" if slip else "-"
            table.add_row(
                str(tid), ts[:19], strat.replace("_", " ").title(), ticker,
                side_display, f"{shares:,.0f}", otype or "-",
                f"${req_p:,.2f}" if req_p else "-",
                f"${fill_p:,.2f}" if fill_p else "-",
                slip_str, _status_str(status),
            )

    console.print()
    console.print(table)
    console.print(f"\n  [dim]Total: {len(trades)} trades[/dim]")

    cur.close()
    conn.close()


# ═══════════════════════════════════════════════════════
#  POSITIONS VIEW
# ═══════════════════════════════════════════════════════

def show_positions():
    conn = _connect()
    cur = conn.cursor()

    # Open positions
    open_table = Table(title="Open Positions", box=box.HEAVY, border_style="yellow")
    open_table.add_column("#", style="dim", justify="right")
    open_table.add_column("Strategy")
    open_table.add_column("Ticker", style="bold cyan")
    open_table.add_column("Side", justify="center")
    open_table.add_column("Shares", justify="right")
    open_table.add_column("Entry", justify="right")
    open_table.add_column("Stop", justify="right", style="red")
    open_table.add_column("Target", justify="right", style="green")
    open_table.add_column("Bars Held", justify="right", style="dim")

    cur.execute(
        "SELECT id, strategy, ticker, side, shares, entry_price, stop_price, "
        "target_price, entry_time, bars_held "
        "FROM strategy_positions WHERE status='open' ORDER BY entry_time"
    )
    open_pos = cur.fetchall()

    if open_pos:
        for pid, strat, ticker, side, shares, entry, stop, target, etime, bars in open_pos:
            open_table.add_row(
                str(pid), strat.replace("_", " ").title(), ticker, _side_str(side),
                f"{shares:,.0f}", f"${entry:,.2f}",
                f"${stop:,.2f}" if stop else "-",
                f"${target:,.2f}" if target else "-",
                str(bars or 0),
            )
    else:
        open_table.add_row("[dim]None[/dim]", *[""] * 8)

    console.print()
    console.print(open_table)

    # Closed positions
    closed_table = Table(title="Closed Positions (last 20)", box=box.HEAVY,
                         border_style="blue", row_styles=["", "dim"])
    closed_table.add_column("#", style="dim", justify="right")
    closed_table.add_column("Strategy")
    closed_table.add_column("Ticker", style="bold cyan")
    closed_table.add_column("Side", justify="center")
    closed_table.add_column("Shares", justify="right")
    closed_table.add_column("Entry", justify="right")
    closed_table.add_column("Exit", justify="right")
    closed_table.add_column("P&L", justify="right")
    closed_table.add_column("Reason")

    cur.execute(
        "SELECT id, strategy, ticker, side, shares, entry_price, exit_price, "
        "entry_time, exit_time, pnl, notes "
        "FROM strategy_positions WHERE status='closed' ORDER BY id DESC LIMIT 20"
    )
    closed_pos = cur.fetchall()

    if closed_pos:
        for pid, strat, ticker, side, shares, entry, exit_p, etime, xtime, pnl, notes in closed_pos:
            closed_table.add_row(
                str(pid), strat.replace("_", " ").title(), ticker, _side_str(side),
                f"{shares:,.0f}", f"${entry:,.2f}",
                f"${exit_p:,.2f}" if exit_p else "-",
                _pnl_str(pnl) if pnl else "-",
                (notes or "")[:30],
            )
    else:
        closed_table.add_row("[dim]None[/dim]", *[""] * 8)

    console.print()
    console.print(closed_table)

    cur.close()
    conn.close()


# ═══════════════════════════════════════════════════════
#  P&L BREAKDOWN
# ═══════════════════════════════════════════════════════

def show_pnl():
    conn = _connect()
    cur = conn.cursor()

    strategies = ["momentum", "mean_reversion", "sector_rotation"]
    icons = {"momentum": ">>>", "mean_reversion": "<->", "sector_rotation": "~~~"}
    colors = {"momentum": "green", "mean_reversion": "yellow", "sector_rotation": "magenta"}

    panels = []

    for strat in strategies:
        color = colors[strat]
        icon = icons[strat]

        cur.execute(
            "SELECT allocated, used, realized_pnl FROM strategy_capital WHERE strategy=%s",
            (strat,),
        )
        cap = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM strategy_positions WHERE strategy=%s AND status='closed'",
            (strat,),
        )
        closed = cur.fetchone()
        total_trades, total_pnl = closed

        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(pnl), 0) "
            "FROM strategy_positions WHERE strategy=%s AND status='closed' AND pnl > 0",
            (strat,),
        )
        winners = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(pnl), 0) "
            "FROM strategy_positions WHERE strategy=%s AND status='closed' AND pnl <= 0",
            (strat,),
        )
        losers = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) FROM strategy_positions WHERE strategy=%s AND status='open'",
            (strat,),
        )
        open_count = cur.fetchone()[0]

        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("Label", style="dim")
        t.add_column("Value", justify="right")

        if cap:
            alloc, used, realized = cap
            ret = total_pnl / alloc * 100 if alloc > 0 else 0
            t.add_row("Capital", f"${alloc:>12,.0f}")
            t.add_row("In Use", f"[cyan]${used:>12,.0f}[/cyan]")
            t.add_row("Available", f"${alloc - used + realized:>12,.0f}")
            t.add_row("", "")
            t.add_row("Realized P&L", _pnl_str(realized))
            t.add_row("Return", _pct_str(ret))
            t.add_row("", "")

        t.add_row("Open", f"[cyan]{open_count}[/cyan]")
        t.add_row("Closed", f"{total_trades}")

        if total_trades > 0:
            win_rate = winners[0] / total_trades * 100
            t.add_row("Win Rate", f"[{'green' if win_rate >= 50 else 'red'}]{win_rate:.1f}%[/{'green' if win_rate >= 50 else 'red'}]")
            t.add_row("Avg Win", f"[green]${winners[1]:>+,.2f}[/green]")
            t.add_row("Avg Loss", f"[red]${losers[1]:>+,.2f}[/red]")

        name = f"{icon} {strat.replace('_', ' ').title()}"
        panels.append(Panel(t, title=f"[bold {color}]{name}[/bold {color}]",
                            border_style=color, box=box.HEAVY, expand=True))

    # Print side by side using columns
    from rich.columns import Columns
    console.print()
    console.print(Columns(panels, equal=True, expand=True))

    # Ensemble total
    cur.execute("SELECT COALESCE(SUM(pnl), 0) FROM strategy_positions WHERE status='closed'")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM strategy_positions WHERE status='closed'")
    total_closed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM strategy_positions WHERE status='open'")
    total_open = cur.fetchone()[0]

    ensemble_t = Table(show_header=False, box=None, padding=(0, 4), expand=True)
    ensemble_t.add_column("", justify="center")
    ensemble_t.add_column("", justify="center")
    ensemble_t.add_column("", justify="center")
    ensemble_t.add_column("", justify="center")

    ensemble_t.add_row(
        f"[bold]Total P&L[/bold]\n{_pnl_str(total)}",
        f"[bold]Return[/bold]\n{_pct_str(total / 1_000_000 * 100)}",
        f"[bold]Open[/bold]\n[cyan]{total_open}[/cyan]",
        f"[bold]Closed[/bold]\n{total_closed}",
    )

    console.print(Panel(ensemble_t, title="[bold white]ENSEMBLE TOTAL[/bold white]",
                        border_style="white", box=box.DOUBLE))

    cur.close()
    conn.close()


# ═══════════════════════════════════════════════════════
#  EQUITY HISTORY
# ═══════════════════════════════════════════════════════

def show_history():
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        "SELECT timestamp, total_equity, cash, long_exposure, short_exposure, "
        "net_exposure, momentum_pnl, mean_reversion_pnl, sector_rotation_pnl "
        "FROM daily_snapshots ORDER BY id"
    )
    snapshots = cur.fetchall()

    table = Table(title="Equity History", box=box.HEAVY, border_style="cyan",
                  row_styles=["", "dim"])
    table.add_column("Time", style="dim")
    table.add_column("Equity", justify="right", style="bold white")
    table.add_column("Cash", justify="right")
    table.add_column("Long", justify="right", style="green")
    table.add_column("Short", justify="right", style="red")
    table.add_column("Net", justify="right")
    table.add_column("Mom P&L", justify="right")
    table.add_column("MR P&L", justify="right")
    table.add_column("SR P&L", justify="right")

    if not snapshots:
        table.add_row("[dim]No snapshots yet[/dim]", *[""] * 8)
    else:
        for ts, equity, cash, long_e, short_e, net_e, mp, mrp, srp in snapshots:
            table.add_row(
                ts[:19],
                f"${equity:>13,.2f}",
                f"${cash:>12,.2f}",
                f"${long_e:>12,.2f}",
                f"${short_e:>12,.2f}",
                f"${net_e:>12,.2f}",
                _pnl_str(mp or 0),
                _pnl_str(mrp or 0),
                _pnl_str(srp or 0),
            )

    console.print()
    console.print(table)

    if snapshots:
        first_eq = snapshots[0][1] or 1_000_000
        last_eq = snapshots[-1][1] or first_eq
        change = last_eq - first_eq
        pct = change / first_eq * 100

        summary = Table(show_header=False, box=None, padding=(0, 3), expand=True)
        summary.add_column("", justify="center")
        summary.add_column("", justify="center")
        summary.add_column("", justify="center")
        summary.add_column("", justify="center")

        summary.add_row(
            f"[bold]Starting[/bold]\n${first_eq:,.2f}",
            f"[bold]Current[/bold]\n${last_eq:,.2f}",
            f"[bold]Change[/bold]\n{_pnl_str(change)}",
            f"[bold]Return[/bold]\n{_pct_str(pct)}",
        )
        console.print(Panel(summary, border_style="cyan", box=box.HEAVY))

    cur.close()
    conn.close()


# ═══════════════════════════════════════════════════════
#  EXPORT TO CSV
# ═══════════════════════════════════════════════════════

def export_csv():
    conn = _connect()
    cur = conn.cursor()

    console.print()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    exports = []

    # Trades
    cur.execute("SELECT * FROM trade_log ORDER BY id")
    trades = cur.fetchall()
    if trades:
        cols = [d[0] for d in cur.description]
        fname = f"trades_{timestamp}.csv"
        with open(fname, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(trades)
        exports.append(("Trades", fname, len(trades)))

    # Positions
    cur.execute("SELECT * FROM strategy_positions ORDER BY id")
    positions = cur.fetchall()
    if positions:
        cols = [d[0] for d in cur.description]
        fname = f"positions_{timestamp}.csv"
        with open(fname, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(positions)
        exports.append(("Positions", fname, len(positions)))

    # Snapshots
    cur.execute("SELECT * FROM daily_snapshots ORDER BY id")
    snapshots = cur.fetchall()
    if snapshots:
        cols = [d[0] for d in cur.description]
        fname = f"snapshots_{timestamp}.csv"
        with open(fname, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(snapshots)
        exports.append(("Snapshots", fname, len(snapshots)))

    table = Table(title="Exported Files", box=box.HEAVY, border_style="green")
    table.add_column("Data", style="bold")
    table.add_column("File", style="cyan")
    table.add_column("Rows", justify="right")

    for data_type, fname, count in exports:
        table.add_row(data_type, fname, str(count))

    if not exports:
        table.add_row("[dim]No data to export[/dim]", "", "")

    console.print(table)
    if exports:
        console.print("\n  [green]Open these in Excel or Google Sheets.[/green]")

    cur.close()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Competition Bot Dashboard")
    parser.add_argument(
        "view", nargs="?", default="summary",
        choices=["summary", "trades", "positions", "pnl", "history", "export"],
        help="What to show (default: summary)",
    )
    args = parser.parse_args()

    views = {
        "summary": show_summary,
        "trades": show_trades,
        "positions": show_positions,
        "pnl": show_pnl,
        "history": show_history,
        "export": export_csv,
    }
    views[args.view]()


if __name__ == "__main__":
    main()
