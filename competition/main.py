"""
CLI entry point for the competition trading bot.

Usage:
  python -m competition.main --dry-run       # Simulate (no orders)
  python -m competition.main --live          # Live paper trading
  python -m competition.main --dry-run --once  # Single cycle then exit
"""

import argparse
import logging
import sys

from competition import config, state, risk
from competition.ensemble import Ensemble
from competition.executor import connect_alpaca
from competition.scheduler import run_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run_once(dry_run: bool = True):
    """Run a single cycle and exit."""
    state.init_db()
    state.init_capital_pools()

    client = None
    if not dry_run:
        client = connect_alpaca()

    ensemble = Ensemble(client=client, dry_run=dry_run)

    if not dry_run and not risk.is_market_open():
        logger.info("Market is closed. Use --dry-run to test outside market hours.")
        return

    ensemble.run_cycle()
    logger.info("Single cycle complete.")


def main():
    parser = argparse.ArgumentParser(description="Competition Trading Bot — Orthogonal Alpha")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Simulate with real data, no broker orders")
    mode.add_argument("--live", action="store_true",
                      help="Live paper trading via Alpaca")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle then exit (no loop)")

    args = parser.parse_args()
    dry_run = args.dry_run

    if args.once:
        run_once(dry_run=dry_run)
    else:
        run_loop(dry_run=dry_run)


if __name__ == "__main__":
    main()
