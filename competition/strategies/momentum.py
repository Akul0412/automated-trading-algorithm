"""
Momentum strategy — Opening Range Breakout.
Intraday only (1-min bars), 20 liquid large-caps.
"""

import logging

import pandas as pd

from competition import config, indicators
from competition.strategies.base import BaseStrategy, TradeSignal, ExitSignal
from competition.universe import MOMENTUM_UNIVERSE

logger = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self):
        # Track entries per ticker per day to limit re-entries
        self._daily_entries: dict[str, int] = {}  # ticker -> count
        self._last_date: str = ""

    def _reset_daily_counters(self, today: str):
        """Reset per-ticker entry counts at start of each new day."""
        if today != self._last_date:
            self._daily_entries.clear()
            self._last_date = today

    def generate_signals(self, market_data: dict) -> list[TradeSignal]:
        """
        Entry: Price breaks above/below 30-min opening range, confirmed by
        VWAP direction, 9-EMA > 21-EMA (long) or 9-EMA < 21-EMA (short),
        volume on breakout bar >= 1.5x average, and N consecutive closes
        beyond the range level (confirmation).
        Max entries per ticker per day is capped.
        """
        bars_1m = market_data.get("bars_1m")
        if bars_1m is None or bars_1m.empty:
            return []

        signals = []

        for symbol in MOMENTUM_UNIVERSE:
            try:
                ohlcv = self._get_symbol_data(bars_1m, symbol)
                if ohlcv is None or len(ohlcv) < config.MOM_EMA_SLOW + 5:
                    continue

                # Get today's data only
                today_data = self._get_today_data(ohlcv)
                if today_data is None or len(today_data) < 31:
                    # Need at least 30 min of data (30 bars of 1-min) + current
                    continue

                # Reset daily counters if new day
                today_str = str(today_data.index[-1].date()) if hasattr(today_data.index[-1], 'date') else ""
                self._reset_daily_counters(today_str)

                # Check per-ticker entry limit
                if self._daily_entries.get(symbol, 0) >= config.MOM_MAX_ENTRIES_PER_TICKER:
                    continue

                # Calculate opening range from first 30 bars (30 min)
                opening_bars = today_data.iloc[:30]
                range_high = float(opening_bars["high"].max())
                range_low = float(opening_bars["low"].min())

                # Current bar (most recent)
                current = today_data.iloc[-1]
                current_close = float(current["close"])
                current_volume = float(current["volume"])

                # Confirmation: check last N bars all closed beyond range level
                confirm_n = config.MOM_CONFIRM_BARS
                if len(today_data) < 30 + confirm_n:
                    continue
                recent_closes = today_data["close"].iloc[-confirm_n:].astype(float)

                # VWAP
                vwap_val = indicators.vwap_intraday(
                    today_data["high"], today_data["low"],
                    today_data["close"], today_data["volume"],
                )
                current_vwap = float(vwap_val.iloc[-1])

                # EMAs on close prices (use all available data for smoothness)
                ema_fast = indicators.ema(ohlcv["close"], config.MOM_EMA_FAST)
                ema_slow = indicators.ema(ohlcv["close"], config.MOM_EMA_SLOW)
                current_ema_fast = float(ema_fast.iloc[-1])
                current_ema_slow = float(ema_slow.iloc[-1])

                # Average volume (last 20 bars)
                avg_volume = float(ohlcv["volume"].tail(20).mean())
                volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

                # --- Long breakout (N consecutive closes above range high) ---
                long_confirmed = all(c > range_high for c in recent_closes)
                short_confirmed = all(c < range_low for c in recent_closes)

                if (long_confirmed
                        and current_close > current_vwap
                        and current_ema_fast > current_ema_slow
                        and volume_ratio >= config.MOM_VOLUME_MULTIPLIER):

                    stop = current_close * (1 - config.MOM_STOP_LOSS_PCT)
                    target = current_close * (1 + config.MOM_PROFIT_TARGET_PCT)
                    strength = min(volume_ratio / 3.0, 1.0)

                    signals.append(TradeSignal(
                        strategy=self.name,
                        ticker=symbol,
                        side="buy",
                        direction="long",
                        price=current_close,
                        stop_price=stop,
                        target_price=target,
                        strength=strength,
                        reason=f"ORB long: close={current_close:.2f} > range_high={range_high:.2f}, vol={volume_ratio:.1f}x, confirmed={confirm_n}bars",
                        details={
                            "range_high": range_high, "range_low": range_low,
                            "vwap": current_vwap, "ema_fast": current_ema_fast,
                            "ema_slow": current_ema_slow, "volume_ratio": volume_ratio,
                        },
                    ))
                    self._daily_entries[symbol] = self._daily_entries.get(symbol, 0) + 1

                # --- Short breakout (N consecutive closes below range low) ---
                elif (short_confirmed
                      and current_close < current_vwap
                      and current_ema_fast < current_ema_slow
                      and volume_ratio >= config.MOM_VOLUME_MULTIPLIER):

                    stop = current_close * (1 + config.MOM_STOP_LOSS_PCT)
                    target = current_close * (1 - config.MOM_PROFIT_TARGET_PCT)
                    strength = min(volume_ratio / 3.0, 1.0)

                    signals.append(TradeSignal(
                        strategy=self.name,
                        ticker=symbol,
                        side="sell",
                        direction="short",
                        price=current_close,
                        stop_price=stop,
                        target_price=target,
                        strength=strength,
                        reason=f"ORB short: close={current_close:.2f} < range_low={range_low:.2f}, vol={volume_ratio:.1f}x, confirmed={confirm_n}bars",
                        details={
                            "range_high": range_high, "range_low": range_low,
                            "vwap": current_vwap, "ema_fast": current_ema_fast,
                            "ema_slow": current_ema_slow, "volume_ratio": volume_ratio,
                        },
                    ))
                    self._daily_entries[symbol] = self._daily_entries.get(symbol, 0) + 1

            except Exception as e:
                logger.error("Momentum signal error for %s: %s", symbol, e)
                continue

        logger.info("Momentum: generated %d signals", len(signals))
        return signals

    def check_exits(self, positions: list[dict], market_data: dict) -> list[ExitSignal]:
        """
        Exit conditions:
        1. 1.5% profit target
        2. 0.7% stop loss
        3. Trailing stop at breakeven after +0.8%
        4. VWAP cross
        5. Forced close at 15:45 ET
        """
        bars_1m = market_data.get("bars_1m")
        if bars_1m is None or bars_1m.empty:
            return []

        exits = []
        force_close = market_data.get("force_close_momentum", False)

        for pos in positions:
            if pos["strategy"] != self.name:
                continue

            ticker = pos["ticker"]
            try:
                ohlcv = self._get_symbol_data(bars_1m, ticker)
                if ohlcv is None or ohlcv.empty:
                    continue

                current_price = float(ohlcv["close"].iloc[-1])
                entry_price = pos["entry_price"]
                side = pos["side"]

                # Force close at 15:45
                if force_close:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason="FORCE_CLOSE_EOD", current_price=current_price,
                    ))
                    continue

                # Calculate PnL
                if side == "long":
                    pnl_pct = (current_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - current_price) / entry_price

                # Profit target
                if pnl_pct >= config.MOM_PROFIT_TARGET_PCT:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"PROFIT_TARGET ({pnl_pct:+.2%})",
                        current_price=current_price,
                    ))
                    continue

                # Stop loss
                if side == "long" and pos["stop_price"] and current_price <= pos["stop_price"]:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"STOP_LOSS ({pnl_pct:+.2%})",
                        current_price=current_price,
                    ))
                    continue
                if side == "short" and pos["stop_price"] and current_price >= pos["stop_price"]:
                    exits.append(ExitSignal(
                        position_id=pos["id"], ticker=ticker,
                        reason=f"STOP_LOSS ({pnl_pct:+.2%})",
                        current_price=current_price,
                    ))
                    continue

                # Trailing stop: activate at +0.8%, then trail 1.2% below high watermark
                if pnl_pct >= config.MOM_TRAILING_ACTIVATE_PCT:
                    if side == "long":
                        # Trail below the current price by stop_loss_pct
                        new_stop = current_price * (1 - config.MOM_STOP_LOSS_PCT)
                        # Only move stop up, never down
                        if pos["stop_price"] is None or new_stop > pos["stop_price"]:
                            exits.append(ExitSignal(
                                position_id=pos["id"], ticker=ticker,
                                reason=f"TRAILING_UPDATE (stop→${new_stop:.2f})",
                                current_price=current_price,
                                new_stop=new_stop,
                            ))
                            continue
                    if side == "short":
                        # Trail above the current price by stop_loss_pct
                        new_stop = current_price * (1 + config.MOM_STOP_LOSS_PCT)
                        # Only move stop down, never up
                        if pos["stop_price"] is None or new_stop < pos["stop_price"]:
                            exits.append(ExitSignal(
                                position_id=pos["id"], ticker=ticker,
                                reason=f"TRAILING_UPDATE (stop→${new_stop:.2f})",
                                current_price=current_price,
                                new_stop=new_stop,
                            ))
                            continue

                # VWAP cross exit
                today_data = self._get_today_data(ohlcv)
                if today_data is not None and len(today_data) > 2:
                    vwap_val = indicators.vwap_intraday(
                        today_data["high"], today_data["low"],
                        today_data["close"], today_data["volume"],
                    )
                    current_vwap = float(vwap_val.iloc[-1])
                    if side == "long" and current_price < current_vwap and pnl_pct > 0:
                        exits.append(ExitSignal(
                            position_id=pos["id"], ticker=ticker,
                            reason=f"VWAP_CROSS (price={current_price:.2f} < vwap={current_vwap:.2f})",
                            current_price=current_price,
                        ))
                    elif side == "short" and current_price > current_vwap and pnl_pct > 0:
                        exits.append(ExitSignal(
                            position_id=pos["id"], ticker=ticker,
                            reason=f"VWAP_CROSS (price={current_price:.2f} > vwap={current_vwap:.2f})",
                            current_price=current_price,
                        ))

            except Exception as e:
                logger.error("Momentum exit check error for %s: %s", ticker, e)

        logger.info("Momentum: %d exit signals for %d positions", len(exits), len(positions))
        return exits

    def _get_symbol_data(self, bars: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
        """Extract single symbol OHLCV from MultiIndex DataFrame."""
        try:
            if isinstance(bars.index, pd.MultiIndex):
                if symbol not in bars.index.get_level_values("symbol"):
                    return None
                return bars.xs(symbol, level="symbol")
            return bars
        except KeyError:
            return None

    def _get_today_data(self, ohlcv: pd.DataFrame) -> pd.DataFrame | None:
        """Extract only today's bars from an OHLCV DataFrame."""
        if ohlcv.empty:
            return None
        try:
            dates = ohlcv.index.date if hasattr(ohlcv.index, 'date') else None
            if dates is None:
                return ohlcv
            today = dates[-1]
            mask = [d == today for d in dates]
            today_data = ohlcv[mask]
            return today_data if not today_data.empty else None
        except Exception:
            return ohlcv
