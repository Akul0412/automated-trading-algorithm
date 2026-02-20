"""
Main loop — runs ensemble every 3 minutes during market hours.
"""

import logging
import sys
import time

from competition import config, risk, state
from competition.ensemble import Ensemble
from competition.executor import connect_alpaca

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run_loop(dry_run: bool = True):
    """Main scheduler loop."""
    logger.info("Competition bot scheduler starting (dry_run=%s)", dry_run)
    logger.info("Cycle interval: %ds", config.CYCLE_INTERVAL_SEC)

    # Initialize DB and capital pools
    state.init_db()
    state.init_capital_pools()

    # Connect to Alpaca
    client = None
    if not dry_run:
        try:
            client = connect_alpaca()
        except Exception as e:
            logger.error("Failed to connect to Alpaca: %s", e)
            return

    ensemble = Ensemble(client=client, dry_run=dry_run)

    while True:
        if not risk.is_market_open():
            logger.info("Market closed. Sleeping %ds...", config.CYCLE_INTERVAL_SEC)
            time.sleep(config.CYCLE_INTERVAL_SEC)
            continue

        try:
            ensemble.run_cycle()
        except Exception:
            logger.exception("Cycle failed")

        logger.info("Sleeping %ds until next cycle...", config.CYCLE_INTERVAL_SEC)
        time.sleep(config.CYCLE_INTERVAL_SEC)
