"""
Layered risk management for the competition bot.
Portfolio-level drawdown, daily loss limits, exposure caps.
"""

import datetime
import logging
import zoneinfo

from competition import config, state
from competition.strategies.base import TradeSignal

logger = logging.getLogger(__name__)

ET = zoneinfo.ZoneInfo("America/New_York")


def check_portfolio_risk(equity: float) -> dict:
    """
    Check portfolio-level risk conditions.
    Returns dict with risk status and any actions needed.
    """
    result = {
        "halt": False,
        "reduce": False,
        "stop_new": False,
        "reason": "",
    }

    peak = state.get_peak_equity()
    if peak is None or peak <= 0:
        return result

    drawdown = (peak - equity) / peak

    # 12% drawdown → flatten everything
    if drawdown >= config.DRAWDOWN_FLATTEN_PCT:
        result["halt"] = True
        result["reason"] = f"FLATTEN: drawdown {drawdown:.1%} >= {config.DRAWDOWN_FLATTEN_PCT:.1%}"
        logger.critical(result["reason"])
        return result

    # 8% drawdown → reduce by 50%
    if drawdown >= config.DRAWDOWN_REDUCE_PCT:
        result["reduce"] = True
        result["reason"] = f"REDUCE: drawdown {drawdown:.1%} >= {config.DRAWDOWN_REDUCE_PCT:.1%}"
        logger.warning(result["reason"])
        return result

    # Daily loss limit: 3% from day start
    day_start_equity = state.get_daily_start_equity()
    if day_start_equity and day_start_equity > 0:
        daily_loss = (day_start_equity - equity) / day_start_equity
        if daily_loss >= config.DAILY_LOSS_LIMIT_PCT:
            result["stop_new"] = True
            result["reason"] = f"DAILY_LIMIT: loss {daily_loss:.1%} >= {config.DAILY_LOSS_LIMIT_PCT:.1%}"
            logger.warning(result["reason"])
            return result

    logger.info("Risk check OK: drawdown=%.2f%%, peak=$%.0f, current=$%.0f",
                drawdown * 100, peak, equity)
    return result


def check_exposure_limits(regime: str) -> float:
    """Get target exposure cap based on current regime."""
    if regime == "risk_on":
        return config.EXPOSURE_RISK_ON
    elif regime == "risk_off":
        return config.EXPOSURE_RISK_OFF
    else:
        return config.EXPOSURE_NEUTRAL


def check_spy_emergency(spy_open: float, spy_current: float) -> bool:
    """Check if SPY has dropped more than 2% from today's open."""
    if spy_open <= 0:
        return False
    drop = (spy_current - spy_open) / spy_open
    if drop <= config.SR_EMERGENCY_DROP:
        logger.critical("SPY EMERGENCY: dropped %.2f%% from open", drop * 100)
        return True
    return False


def filter_signals_by_exposure(
    signals: list[TradeSignal],
    current_exposure: dict,
    equity: float,
    target_exposure_pct: float,
) -> list[TradeSignal]:
    """
    Filter signals to stay within exposure limits.
    Removes signals that would push exposure beyond the target.
    """
    max_exposure = equity * target_exposure_pct
    current_gross = current_exposure.get("gross", 0)
    remaining = max_exposure - current_gross

    if remaining <= 0:
        logger.info("Exposure limit reached (%.0f/%.0f), filtering all signals",
                     current_gross, max_exposure)
        return []

    filtered = []
    running_total = 0

    # Sort by strength (best signals first)
    for sig in sorted(signals, key=lambda s: s.strength, reverse=True):
        sig_value = sig.shares * sig.price if sig.shares > 0 else sig.price * 100
        if running_total + sig_value <= remaining:
            filtered.append(sig)
            running_total += sig_value

    if len(filtered) < len(signals):
        logger.info("Exposure filter: %d/%d signals kept (remaining=$%.0f)",
                     len(filtered), len(signals), remaining)

    return filtered


def is_market_open() -> bool:
    """Check if current time is within NYSE regular trading hours."""
    now = datetime.datetime.now(ET)
    if now.weekday() > 4:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def get_current_et_time() -> datetime.time:
    """Get current time in Eastern timezone."""
    return datetime.datetime.now(ET).time()


def is_momentum_active() -> bool:
    """Momentum strategy active window: 10:00-15:45 ET."""
    t = get_current_et_time()
    return datetime.time(10, 0) <= t <= datetime.time(15, 45)


def is_momentum_force_close() -> bool:
    """Force close momentum at 15:45 ET."""
    t = get_current_et_time()
    return t >= datetime.time(15, 45)


def is_mean_reversion_active() -> bool:
    """Mean reversion active window: 9:45-15:45 ET."""
    t = get_current_et_time()
    return datetime.time(9, 45) <= t <= datetime.time(15, 45)


def is_sector_rebalance_time() -> bool:
    """Sector rotation rebalances once daily at 10:00 ET."""
    t = get_current_et_time()
    return datetime.time(10, 0) <= t <= datetime.time(10, 5)


def is_end_of_day() -> bool:
    """Check if we're near end of day for final snapshot."""
    t = get_current_et_time()
    return t >= datetime.time(15, 55)
