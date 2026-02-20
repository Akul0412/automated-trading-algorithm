"""
Mean Reversion strategy — Bollinger Band + RSI extremes.
1-2 day holds on top 100 S&P 500 stocks by liquidity.
"""

import logging

import pandas as pd
import numpy as np

from competition import config, indicators
from competition.strategies.base import BaseStrategy, TradeSignal, ExitSignal

logger = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def generate_signals(self, market_data: dict) -> list[TradeSignal]:
        """
        Entry: Price at/below lower BB AND RSI < 30 AND volume not spiking
        AND price within 5% of daily 50-day SMA. Mirror for shorts.
        """
        bars_15m = market_data.get("bars_15m")
        daily_bars = market_data.get("daily_bars")

        if bars_15m is None or bars_15m.empty:
            return []

        # Get available symbols from the 15-min data
        if isinstance(bars_15m.index, pd.MultiIndex):
            symbols = bars_15m.index.get_level_values("symbol").unique().tolist()
        else:
            return []

        signals = []

        for symbol in symbols:
            try:
                ohlcv = self._get_symbol_data(bars_15m, symbol)
                if ohlcv is None or len(ohlcv) < config.MR_BB_PERIOD + 5:
                    continue

                close = ohlcv["close"]
                volume = ohlcv["volume"]
                current_close = float(close.iloc[-1])
                current_volume = float(volume.iloc[-1])

                # Bollinger Bands on 15-min close
                bb_upper, bb_mid, bb_lower = indicators.bollinger_bands(
                    close, config.MR_BB_PERIOD, config.MR_BB_STD,
                )
                current_bb_upper = float(bb_upper.iloc[-1])
                current_bb_mid = float(bb_mid.iloc[-1])
                current_bb_lower = float(bb_lower.iloc[-1])

                # RSI
                rsi_val = indicators.rsi(close, config.MR_RSI_PERIOD)
                current_rsi = float(rsi_val.iloc[-1])

                # Average volume check
                avg_volume = float(volume.tail(20).mean())
                volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

                # Daily 50-SMA proximity check
                daily_sma_ok = True
                if daily_bars is not None and not daily_bars.empty:
                    daily_ohlcv = self._get_symbol_data(daily_bars, symbol)
                    if daily_ohlcv is not None and len(daily_ohlcv) >= 50:
                        daily_sma50 = float(indicators.sma(daily_ohlcv["close"], 50).iloc[-1])
                        proximity = abs(current_close - daily_sma50) / daily_sma50
                        daily_sma_ok = proximity <= config.MR_SMA_PROXIMITY

                # BB width for volatility-based sizing
                bb_width = (current_bb_upper - current_bb_lower) / current_bb_mid if current_bb_mid > 0 else 0

                # --- Long signal (oversold) ---
                if (current_close <= current_bb_lower
                        and current_rsi < config.MR_RSI_OVERSOLD
                        and volume_ratio < config.MR_VOLUME_SPIKE
                        and daily_sma_ok):

                    stop = current_close * (1 - config.MR_STOP_LOSS_PCT)
                    target = current_bb_mid
                    strength = (config.MR_RSI_OVERSOLD - current_rsi) / config.MR_RSI_OVERSOLD

                    signals.append(TradeSignal(
                        strategy=self.name,
                        ticker=symbol,
                        side="buy",
                        direction="long",
                        price=current_close,
                        stop_price=stop,
                        target_price=target,
                        strength=strength,
                        reason=f"MR long: RSI={current_rsi:.1f}, at lower BB",
                        details={
                            "rsi": current_rsi, "bb_lower": current_bb_lower,
                            "bb_mid": current_bb_mid, "bb_width": bb_width,
                            "volume_ratio": volume_ratio,
                        },
                    ))

                # --- Short signal (overbought) ---
                elif (current_close >= current_bb_upper
                      and current_rsi > config.MR_RSI_OVERBOUGHT
                      and volume_ratio < config.MR_VOLUME_SPIKE
                      and daily_sma_ok):

                    stop = current_close * (1 + config.MR_STOP_LOSS_PCT)
                    target = current_bb_mid
                    strength = (current_rsi - config.MR_RSI_OVERBOUGHT) / (100 - config.MR_RSI_OVERBOUGHT)

                    signals.append(TradeSignal(
                        strategy=self.name,
                        ticker=symbol,
                        side="sell",
                        direction="short",
                        price=current_close,
                        stop_price=stop,
                        target_price=target,
                        strength=strength,
                        reason=f"MR short: RSI={current_rsi:.1f}, at upper BB",
                        details={
                            "rsi": current_rsi, "bb_upper": current_bb_upper,
                            "bb_mid": current_bb_mid, "bb_width": bb_width,
                            "volume_ratio": volume_ratio,
                        },
                    ))

            except Exception as e:
                logger.error("Mean reversion signal error for %s: %s", symbol, e)
                continue

        logger.info("Mean Reversion: generated %d signals", len(signals))
        return signals

    def check_exits(self, positions: list[dict], market_data: dict) -> list[ExitSignal]:
        """
        Exit conditions:
        1. Price returns to BB midline
        2. RSI normalizes past 50
        3. 1.5% stop beyond the band
        4. 2-day time stop (~192 15-min bars)
        """
        bars_15m = market_data.get("bars_15m")
        if bars_15m is None or bars_15m.empty:
            return []

        exits = []

        for pos in positions:
            if pos["strategy"] != self.name:
                continue

            ticker = pos["ticker"]
            try:
                ohlcv = self._get_symbol_data(bars_15m, ticker)
                if ohlcv is None or len(ohlcv) < config.MR_BB_PERIOD:
                    continue

                close = ohlcv["close"]
                current_price = float(close.iloc[-1])
                entry_price = pos["entry_price"]
                side = pos["side"]

                # BB midline
                _, bb_mid, _ = indicators.bollinger_bands(
                    close, config.MR_BB_PERIOD, config.MR_BB_STD,
                )
                current_bb_mid = float(bb_mid.iloc[-1])

                # RSI
                rsi_val = indicators.rsi(close, config.MR_RSI_PERIOD)
                current_rsi = float(rsi_val.iloc[-1])

                # Stop loss check
                if pos["stop_price"]:
                    if side == "long" and current_price <= pos["stop_price"]:
                        exits.append(ExitSignal(
                            position_id=pos["id"], ticker=ticker,
                            reason="STOP_LOSS", current_price=current_price,
                        ))
                        continue
                    if side == "short" and current_price >= pos["stop_price"]:
                        exits.append(ExitSignal(
                            position_id=pos["id"], ticker=ticker,
                            reason="STOP_LOSS", current_price=current_price,
                        ))
                        continue

                # Price returned to BB midline
                if side == "long" and current_price >= current_bb_mid:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"BB_MIDLINE (price={current_price:.2f} >= mid={current_bb_mid:.2f})",
                        current_price=current_price,
                    ))
                    continue
                if side == "short" and current_price <= current_bb_mid:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"BB_MIDLINE (price={current_price:.2f} <= mid={current_bb_mid:.2f})",
                        current_price=current_price,
                    ))
                    continue

                # RSI normalization
                if side == "long" and current_rsi > config.MR_RSI_NEUTRAL:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"RSI_NORMALIZED ({current_rsi:.1f} > 50)",
                        current_price=current_price,
                    ))
                    continue
                if side == "short" and current_rsi < config.MR_RSI_NEUTRAL:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"RSI_NORMALIZED ({current_rsi:.1f} < 50)",
                        current_price=current_price,
                    ))
                    continue

                # Time stop
                if pos["bars_held"] >= config.MR_MAX_HOLD_BARS:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"TIME_STOP ({pos['bars_held']} bars)",
                        current_price=current_price,
                    ))
                    continue

            except Exception as e:
                logger.error("Mean reversion exit check error for %s: %s", ticker, e)

        logger.info("Mean Reversion: %d exit signals", len(exits))
        return exits

    def _get_symbol_data(self, bars: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
        try:
            if isinstance(bars.index, pd.MultiIndex):
                if symbol not in bars.index.get_level_values("symbol"):
                    return None
                return bars.xs(symbol, level="symbol")
            return bars
        except KeyError:
            return None
