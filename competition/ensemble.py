"""
Ensemble orchestrator — runs each cycle.
Fetches data, runs strategies, merges signals, resolves conflicts, executes.
"""

import logging

from competition import config, data, risk, state, sizing, executor
from competition.strategies.momentum import MomentumStrategy
from competition.strategies.mean_reversion import MeanReversionStrategy
from competition.strategies.sector_rotation import SectorRotationStrategy
from competition.strategies.base import TradeSignal, ExitSignal
from competition.universe import MOMENTUM_UNIVERSE, SECTOR_UNIVERSE

logger = logging.getLogger(__name__)


class Ensemble:
    def __init__(self, client=None, dry_run=True):
        self.client = client
        self.dry_run = dry_run
        self.momentum = MomentumStrategy()
        self.mean_reversion = MeanReversionStrategy()
        self.sector_rotation = SectorRotationStrategy()

    def run_cycle(self):
        """Main orchestration cycle — called every 3 minutes."""
        logger.info("=" * 60)
        logger.info("  ENSEMBLE CYCLE START")
        logger.info("=" * 60)

        # 1. Fetch account state
        if self.client and not self.dry_run:
            account = executor.get_account_info(self.client)
            equity = account["equity"]
            cash = account["cash"]
            logger.info("[ACCOUNT] Equity: $%s | Cash: $%s | Buying Power: $%s",
                        f"{equity:,.2f}", f"{cash:,.2f}", f"{account['buying_power']:,.2f}")
        else:
            equity = config.TOTAL_CAPITAL
            cash = equity * 0.1
            logger.info("[ACCOUNT] DRY RUN — Equity: $%s", f"{equity:,.2f}")

        # 2. Check portfolio risk
        risk_status = risk.check_portfolio_risk(equity)
        logger.info("[RISK] Halt=%s | Reduce=%s | StopNew=%s | Reason: %s",
                    risk_status["halt"], risk_status.get("reduce", False),
                    risk_status.get("stop_new", False), risk_status.get("reason", "OK"))

        if risk_status["halt"]:
            logger.critical("!!! RISK HALT — FLATTENING ALL POSITIONS !!!")
            self._flatten_all(equity)
            return

        # 3. Take snapshot
        exposure = state.get_total_exposure()
        cap_info = {
            s: state.get_strategy_capital(s)
            for s in ["momentum", "mean_reversion", "sector_rotation"]
        }
        logger.info("[EXPOSURE] Long: $%s | Short: $%s | Net: $%s | Gross: $%s",
                    f"{exposure['long']:,.0f}", f"{exposure['short']:,.0f}",
                    f"{exposure['net']:,.0f}", f"{exposure['gross']:,.0f}")
        logger.info("[CAPITAL] Momentum: $%s avail | MeanRev: $%s avail | SectorRot: $%s avail",
                    f"{cap_info['momentum']['available']:,.0f}",
                    f"{cap_info['mean_reversion']['available']:,.0f}",
                    f"{cap_info['sector_rotation']['available']:,.0f}")
        logger.info("[P&L] Momentum: $%s | MeanRev: $%s | SectorRot: $%s",
                    f"{cap_info['momentum']['realized_pnl']:+,.2f}",
                    f"{cap_info['mean_reversion']['realized_pnl']:+,.2f}",
                    f"{cap_info['sector_rotation']['realized_pnl']:+,.2f}")

        state.take_snapshot(
            total_equity=equity,
            cash=cash,
            long_exposure=exposure["long"],
            short_exposure=exposure["short"],
            momentum_pnl=cap_info["momentum"]["realized_pnl"],
            mean_reversion_pnl=cap_info["mean_reversion"]["realized_pnl"],
            sector_rotation_pnl=cap_info["sector_rotation"]["realized_pnl"],
        )

        # 4. Fetch market data
        market_data = self._fetch_market_data()
        if not market_data:
            logger.warning("Failed to fetch market data, skipping cycle")
            return

        # 5. Get open positions
        all_positions = state.get_open_positions()
        logger.info("[POSITIONS] %d open positions", len(all_positions))
        for p in all_positions:
            logger.info("  -> %s %s %s: %d shares @ $%.2f (stop=$%s, target=$%s)",
                        p["strategy"], p["side"].upper(), p["ticker"],
                        p["shares"], p["entry_price"],
                        f"{p['stop_price']:.2f}" if p["stop_price"] else "N/A",
                        f"{p['target_price']:.2f}" if p["target_price"] else "N/A")

        # 6. Check exits across all strategies
        all_exits = []

        mom_active = risk.is_momentum_active()
        mom_force = risk.is_momentum_force_close()
        mr_active = risk.is_mean_reversion_active()
        sr_rebalance = risk.is_sector_rebalance_time()
        logger.info("[WINDOWS] Momentum=%s (force_close=%s) | MeanRev=%s | SectorRebalance=%s",
                    mom_active, mom_force, mr_active, sr_rebalance)

        if mom_active or mom_force:
            market_data["force_close_momentum"] = mom_force
            mom_exits = self.momentum.check_exits(all_positions, market_data)
            all_exits.extend(mom_exits)

        if mr_active:
            state.increment_bars_held("mean_reversion")
            mr_exits = self.mean_reversion.check_exits(all_positions, market_data)
            all_exits.extend(mr_exits)

        market_data["sector_rebalance"] = sr_rebalance
        sr_exits = self.sector_rotation.check_exits(all_positions, market_data)
        all_exits.extend(sr_exits)

        # 7. Execute exits
        if all_exits:
            logger.info("[EXITS] %d exit signals:", len(all_exits))
            for ex in all_exits:
                logger.info("  -> EXIT %s @ $%.2f — %s", ex.ticker, ex.current_price, ex.reason)
        else:
            logger.info("[EXITS] No exits this cycle")
        self._execute_exits(all_exits, all_positions, market_data)

        # 8. Check if we can open new positions
        if risk_status["stop_new"] or risk_status["reduce"]:
            logger.info("[SKIP] Risk flag active (%s) — no new entries this cycle", risk_status["reason"])
            return

        # 9. Generate new signals
        all_signals = []

        if mom_active:
            mom_positions = [p for p in state.get_open_positions() if p["strategy"] == "momentum"]
            slots = config.MOM_MAX_POSITIONS - len(mom_positions)
            logger.info("[MOMENTUM] %d/%d slots used — scanning for entries...", len(mom_positions), config.MOM_MAX_POSITIONS)
            if slots > 0:
                mom_signals = self.momentum.generate_signals(market_data)
                all_signals.extend(mom_signals)
        else:
            logger.info("[MOMENTUM] Outside active window (10:00-15:45 ET)")

        if mr_active:
            mr_positions = [p for p in state.get_open_positions() if p["strategy"] == "mean_reversion"]
            slots = config.MR_MAX_POSITIONS - len(mr_positions)
            logger.info("[MEAN REV] %d/%d slots used — scanning for entries...", len(mr_positions), config.MR_MAX_POSITIONS)
            if slots > 0:
                mr_signals = self.mean_reversion.generate_signals(market_data)
                all_signals.extend(mr_signals)
        else:
            logger.info("[MEAN REV] Outside active window (9:45-15:45 ET)")

        if sr_rebalance:
            logger.info("[SECTOR ROT] Rebalance time — generating allocation signals...")
            sr_signals = self.sector_rotation.generate_signals(market_data)
            all_signals.extend(sr_signals)
        else:
            logger.info("[SECTOR ROT] Not rebalance time (rebalances at 10:00 ET)")

        # 10. Merge and resolve conflicts
        if all_signals:
            logger.info("[SIGNALS] %d raw signals:", len(all_signals))
            for sig in all_signals:
                logger.info("  -> %s %s %s %s @ $%.2f (strength=%.2f) — %s",
                            sig.strategy, sig.side.upper(), sig.direction, sig.ticker,
                            sig.price, sig.strength, sig.reason)

        resolved = self._resolve_conflicts(all_signals)

        # 11. Apply exposure limits
        exposure = state.get_total_exposure()
        regime = market_data.get("regime", "neutral")
        target_exposure = risk.check_exposure_limits(regime)
        filtered = risk.filter_signals_by_exposure(resolved, exposure, equity, target_exposure)

        if filtered:
            logger.info("[ENTRIES] Executing %d trades:", len(filtered))
            for sig in filtered:
                logger.info("  -> %s %s %s %s @ $%.2f", sig.strategy, sig.side.upper(), sig.direction, sig.ticker, sig.price)
        else:
            logger.info("[ENTRIES] No new trades this cycle")

        # 12. Execute entries
        self._execute_entries(filtered)

        # 13. End-of-day snapshot
        if risk.is_end_of_day():
            exposure = state.get_total_exposure()
            state.take_snapshot(
                total_equity=equity, cash=cash,
                long_exposure=exposure["long"],
                short_exposure=exposure["short"],
                momentum_pnl=cap_info["momentum"]["realized_pnl"],
                mean_reversion_pnl=cap_info["mean_reversion"]["realized_pnl"],
                sector_rotation_pnl=cap_info["sector_rotation"]["realized_pnl"],
                notes="EOD",
            )
            logger.info("[EOD] End-of-day snapshot saved")

        logger.info("=" * 60)
        logger.info("  ENSEMBLE CYCLE COMPLETE")
        logger.info("=" * 60)

    def _fetch_market_data(self) -> dict:
        """Fetch all required market data for this cycle."""
        market_data = {}

        try:
            # 1-min bars for momentum
            if risk.is_momentum_active() or risk.is_momentum_force_close():
                bars_1m = data.get_intraday_bars(MOMENTUM_UNIVERSE, timeframe_minutes=1, lookback_days=2)
                market_data["bars_1m"] = bars_1m

            # 15-min bars for mean reversion (need dynamic universe)
            if risk.is_mean_reversion_active():
                # Use momentum universe + sector ETFs as proxy for liquid stocks
                # In production, would fetch top 100 by dollar volume
                mr_symbols = MOMENTUM_UNIVERSE + SECTOR_UNIVERSE
                bars_15m = data.get_intraday_bars(mr_symbols, timeframe_minutes=15, lookback_days=5)
                market_data["bars_15m"] = bars_15m

            # Daily bars for sector rotation + mean reversion SMA check
            daily_symbols = list(set(MOMENTUM_UNIVERSE + SECTOR_UNIVERSE))
            daily_bars = data.get_daily_bars(daily_symbols, lookback_days=60)
            market_data["daily_bars"] = daily_bars

            # SPY emergency check
            spy_prices = data.get_latest_prices(["SPY"])
            if spy_prices and daily_bars is not None and not daily_bars.empty:
                spy_current = spy_prices.get("SPY", 0)
                try:
                    if isinstance(daily_bars.index, pd.MultiIndex):
                        spy_daily = daily_bars.xs("SPY", level="symbol")
                    else:
                        spy_daily = daily_bars
                    spy_open = float(spy_daily["open"].iloc[-1])
                    market_data["spy_emergency"] = risk.check_spy_emergency(spy_open, spy_current)
                except (KeyError, IndexError):
                    market_data["spy_emergency"] = False
            else:
                market_data["spy_emergency"] = False

        except Exception as e:
            logger.error("Error fetching market data: %s", e)
            return {}

        return market_data

    def _resolve_conflicts(self, signals: list[TradeSignal]) -> list[TradeSignal]:
        """
        Resolve conflicting signals:
        - Opposite signals on same ticker cancel out
        - Check we don't already have a position in the ticker for that strategy
        """
        # Group by ticker
        by_ticker: dict[str, list[TradeSignal]] = {}
        for sig in signals:
            by_ticker.setdefault(sig.ticker, []).append(sig)

        resolved = []
        open_positions = state.get_open_positions()
        held_tickers = {(p["strategy"], p["ticker"]) for p in open_positions}

        for ticker, sigs in by_ticker.items():
            # Check for opposite directions
            directions = {s.direction for s in sigs}
            if "long" in directions and "short" in directions:
                logger.info("Conflict on %s: long + short signals cancel out", ticker)
                continue

            for sig in sigs:
                # Skip if already holding this ticker in this strategy
                if (sig.strategy, sig.ticker) in held_tickers:
                    logger.debug("Skipping %s — already held in %s", ticker, sig.strategy)
                    continue
                resolved.append(sig)

        if len(resolved) < len(signals):
            logger.info("Conflict resolution: %d → %d signals", len(signals), len(resolved))

        return resolved

    def _execute_entries(self, signals: list[TradeSignal]):
        """Execute entry orders for approved signals."""
        for sig in signals:
            cap = state.get_strategy_capital(sig.strategy)
            shares = sizing.size_signal(sig, cap["available"])

            if shares <= 0:
                continue

            result = executor.execute_entry(
                client=self.client,
                strategy=sig.strategy,
                ticker=sig.ticker,
                side=sig.side,
                direction=sig.direction,
                shares=shares,
                price=sig.price,
                stop_price=sig.stop_price,
                target_price=sig.target_price,
                dry_run=self.dry_run,
                signal_details=sig.details,
            )
            logger.info("Entry %s %s %s: %s", sig.strategy, sig.ticker, sig.side, result["status"])

    def _execute_exits(self, exits: list[ExitSignal], positions: list[dict], market_data: dict):
        """Execute exit orders."""
        pos_map = {p["id"]: p for p in positions}

        for exit_sig in exits:
            pos = pos_map.get(exit_sig.position_id)
            if not pos:
                continue

            # If it's just a stop update, don't exit
            if exit_sig.new_stop is not None and exit_sig.reason == "TRAILING_ACTIVATED":
                state.update_position_stops(exit_sig.position_id, stop_price=exit_sig.new_stop)
                logger.info("Updated stop for %s #%d to $%.2f",
                            pos["ticker"], exit_sig.position_id, exit_sig.new_stop)
                continue

            # Get current price
            current_price = exit_sig.current_price
            if current_price <= 0:
                prices = data.get_latest_prices([pos["ticker"]])
                current_price = prices.get(pos["ticker"], pos["entry_price"])

            result = executor.execute_exit(
                client=self.client,
                position=pos,
                current_price=current_price,
                reason=exit_sig.reason,
                dry_run=self.dry_run,
            )
            logger.info("Exit %s %s: %s (%s)",
                        pos["strategy"], pos["ticker"], result["status"], exit_sig.reason)

    def _flatten_all(self, equity: float):
        """Emergency: close all positions."""
        positions = state.get_open_positions()
        if not positions:
            return

        prices = data.get_latest_prices([p["ticker"] for p in positions])

        for pos in positions:
            current_price = prices.get(pos["ticker"], pos["entry_price"])
            executor.execute_exit(
                client=self.client,
                position=pos,
                current_price=current_price,
                reason="RISK_FLATTEN",
                dry_run=self.dry_run,
            )

        logger.critical("Flattened %d positions", len(positions))


# Need pandas for the spy check in _fetch_market_data
import pandas as pd
