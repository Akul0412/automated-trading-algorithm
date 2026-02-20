"""
Sector Rotation strategy — Relative strength ranking + macro regime.
Daily rebalance of SPDR sector ETFs.
"""

import logging

import pandas as pd
import numpy as np

from competition import config, indicators
from competition.strategies.base import BaseStrategy, TradeSignal, ExitSignal
from competition.universe import SECTOR_ETFS

logger = logging.getLogger(__name__)


class SectorRotationStrategy(BaseStrategy):
    name = "sector_rotation"

    def generate_signals(self, market_data: dict) -> list[TradeSignal]:
        """
        Rank sectors by composite score: 0.6 * rank(5d ROC) + 0.4 * rank(10d ROC).
        Regime from SPY vs 10/20-day SMA determines long/short allocation.
        """
        daily_bars = market_data.get("daily_bars")
        if daily_bars is None or daily_bars.empty:
            return []

        # Check for emergency SPY drop
        if market_data.get("spy_emergency", False):
            logger.warning("Sector Rotation: SPY emergency — no new positions")
            return []

        # Don't generate if not rebalance time
        if not market_data.get("sector_rebalance", False):
            return []

        # Extract close prices for all sector ETFs + SPY
        symbols_needed = SECTOR_ETFS + ["SPY"]
        closes = {}
        for sym in symbols_needed:
            try:
                if isinstance(daily_bars.index, pd.MultiIndex):
                    if sym not in daily_bars.index.get_level_values("symbol"):
                        continue
                    sym_data = daily_bars.xs(sym, level="symbol")
                else:
                    continue
                closes[sym] = sym_data["close"]
            except KeyError:
                continue

        if "SPY" not in closes or len(closes) < 6:
            logger.warning("Sector Rotation: insufficient data (%d symbols)", len(closes))
            return []

        spy_close = closes["SPY"]
        if len(spy_close) < config.SR_SMA_LONG + 1:
            return []

        # Determine regime
        spy_sma_short = float(indicators.sma(spy_close, config.SR_SMA_SHORT).iloc[-1])
        spy_sma_long = float(indicators.sma(spy_close, config.SR_SMA_LONG).iloc[-1])
        spy_current = float(spy_close.iloc[-1])

        if spy_current > spy_sma_short and spy_current > spy_sma_long:
            regime = "risk_on"
        elif spy_current < spy_sma_short and spy_current < spy_sma_long:
            regime = "risk_off"
        else:
            regime = "neutral"

        logger.info(
            "Sector Rotation regime: %s (SPY=%.2f, SMA%d=%.2f, SMA%d=%.2f)",
            regime, spy_current, config.SR_SMA_SHORT, spy_sma_short,
            config.SR_SMA_LONG, spy_sma_long,
        )

        # Rank sectors by composite ROC score
        scores = {}
        for sym in SECTOR_ETFS:
            if sym not in closes:
                continue
            close = closes[sym]
            if len(close) < config.SR_ROC_SLOW + 1:
                continue
            roc_fast = float(indicators.roc(close, config.SR_ROC_FAST).iloc[-1])
            roc_slow = float(indicators.roc(close, config.SR_ROC_SLOW).iloc[-1])
            if pd.isna(roc_fast) or pd.isna(roc_slow):
                continue
            scores[sym] = roc_fast  # Will be ranked

        if len(scores) < 4:
            logger.warning("Sector Rotation: only %d sectors with valid scores", len(scores))
            return []

        # Rank by each ROC period, then composite
        symbols = list(scores.keys())
        fast_rocs = {}
        slow_rocs = {}
        for sym in symbols:
            close = closes[sym]
            fast_rocs[sym] = float(indicators.roc(close, config.SR_ROC_FAST).iloc[-1])
            slow_rocs[sym] = float(indicators.roc(close, config.SR_ROC_SLOW).iloc[-1])

        # Rank (higher ROC = higher rank = better)
        sorted_by_fast = sorted(symbols, key=lambda s: fast_rocs[s])
        sorted_by_slow = sorted(symbols, key=lambda s: slow_rocs[s])
        fast_rank = {s: i for i, s in enumerate(sorted_by_fast)}
        slow_rank = {s: i for i, s in enumerate(sorted_by_slow)}

        composite = {
            s: config.SR_ROC_FAST_WEIGHT * fast_rank[s] + config.SR_ROC_SLOW_WEIGHT * slow_rank[s]
            for s in symbols
        }
        ranked = sorted(symbols, key=lambda s: composite[s], reverse=True)

        logger.info("Sector rankings: %s", [(s, f"{composite[s]:.2f}") for s in ranked])

        signals = []
        capital = config.SECTOR_ROTATION_CAPITAL

        if regime == "risk_on":
            # Long top 3 sectors (45%/35%/20% weight split)
            weights = [0.45, 0.35, 0.20]
            for i, weight in enumerate(weights):
                if i >= len(ranked):
                    break
                sym = ranked[i]
                price = float(closes[sym].iloc[-1])
                dollars = capital * weight
                shares = int(dollars / price)
                if shares > 0:
                    signals.append(TradeSignal(
                        strategy=self.name, ticker=sym, side="buy",
                        direction="long", shares=shares, price=price,
                        strength=1.0 - (i * 0.2),
                        reason=f"SR risk_on: rank #{i+1}, weight={weight:.0%}",
                        details={"regime": regime, "rank": i + 1, "composite": composite[sym]},
                    ))

        elif regime == "neutral":
            # Long top 2, short bottom 1
            for i, weight in enumerate([0.45, 0.35]):
                if i >= len(ranked):
                    break
                sym = ranked[i]
                price = float(closes[sym].iloc[-1])
                dollars = capital * weight
                shares = int(dollars / price)
                if shares > 0:
                    signals.append(TradeSignal(
                        strategy=self.name, ticker=sym, side="buy",
                        direction="long", shares=shares, price=price,
                        strength=0.8 - (i * 0.2),
                        reason=f"SR neutral long: rank #{i+1}",
                        details={"regime": regime, "rank": i + 1, "composite": composite[sym]},
                    ))

            # Short weakest
            worst = ranked[-1]
            price = float(closes[worst].iloc[-1])
            dollars = capital * 0.20
            shares = int(dollars / price)
            if shares > 0:
                signals.append(TradeSignal(
                    strategy=self.name, ticker=worst, side="sell",
                    direction="short", shares=shares, price=price,
                    strength=0.6,
                    reason=f"SR neutral short: rank #{len(ranked)}",
                    details={"regime": regime, "rank": len(ranked), "composite": composite[worst]},
                ))

        else:  # risk_off
            # Short weakest 2, long XLU (defensive)
            for i in range(min(2, len(ranked))):
                sym = ranked[-(i + 1)]
                price = float(closes[sym].iloc[-1])
                dollars = capital * 0.35
                shares = int(dollars / price)
                if shares > 0:
                    signals.append(TradeSignal(
                        strategy=self.name, ticker=sym, side="sell",
                        direction="short", shares=shares, price=price,
                        strength=0.7,
                        reason=f"SR risk_off short: rank #{len(ranked) - i}",
                        details={"regime": regime, "rank": len(ranked) - i},
                    ))

            # Long XLU (defensive)
            if "XLU" in closes:
                price = float(closes["XLU"].iloc[-1])
                dollars = capital * 0.30
                shares = int(dollars / price)
                if shares > 0:
                    signals.append(TradeSignal(
                        strategy=self.name, ticker="XLU", side="buy",
                        direction="long", shares=shares, price=price,
                        strength=0.6,
                        reason="SR risk_off defensive: long XLU",
                        details={"regime": regime},
                    ))

        logger.info("Sector Rotation: %d signals (regime=%s)", len(signals), regime)
        return signals

    def check_exits(self, positions: list[dict], market_data: dict) -> list[ExitSignal]:
        """
        Exits for sector rotation are handled by rebalancing — close all existing
        positions before opening new ones during rebalance.
        Emergency: flatten all if SPY drops >2% intraday.
        """
        exits = []

        # Emergency SPY drop
        if market_data.get("spy_emergency", False):
            for pos in positions:
                if pos["strategy"] == self.name:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=pos["ticker"],
                        reason="SPY_EMERGENCY_DROP",
                        current_price=pos["entry_price"],  # Will be updated by executor
                    ))
            if exits:
                logger.warning("Sector Rotation: EMERGENCY — flattening %d positions", len(exits))
            return exits

        # Regular rebalance: close all to make room for new allocation
        if market_data.get("sector_rebalance", False):
            for pos in positions:
                if pos["strategy"] == self.name:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=pos["ticker"],
                        reason="REBALANCE",
                        current_price=pos["entry_price"],
                    ))
            if exits:
                logger.info("Sector Rotation: closing %d positions for rebalance", len(exits))

        return exits
