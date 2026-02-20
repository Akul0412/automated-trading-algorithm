"""
Per-strategy position sizing with volatility adjustment.
"""

import logging

from competition import config
from competition.strategies.base import TradeSignal

logger = logging.getLogger(__name__)


def size_momentum(signal: TradeSignal, strategy_capital: float) -> float:
    """
    Momentum sizing: Risk 1% of strategy capital per trade.
    shares = (capital * 0.01) / (entry * 0.007)
    Cap at 20% of strategy capital per position.
    """
    if signal.price <= 0:
        return 0

    risk_dollars = strategy_capital * config.MOM_RISK_PER_TRADE
    shares = risk_dollars / (signal.price * config.MOM_STOP_LOSS_PCT)
    shares = int(shares)

    # Cap at 20% of strategy capital
    max_dollars = strategy_capital * config.MOM_MAX_POSITION_PCT
    max_shares = int(max_dollars / signal.price)
    shares = min(shares, max_shares)

    return max(shares, 0)


def size_mean_reversion(signal: TradeSignal) -> float:
    """
    Mean reversion sizing: Equal-weight $35K base, scaled by inverse volatility.
    BB width is used as volatility proxy. Scalar clamped [0.5, 1.5].
    """
    if signal.price <= 0:
        return 0

    bb_width = signal.details.get("bb_width", 0.04)

    # Inverse volatility scaling: narrower BB = larger position
    # Typical BB width ~0.03-0.06, so we normalize around 0.04
    if bb_width > 0:
        vol_scalar = 0.04 / bb_width
    else:
        vol_scalar = 1.0

    vol_scalar = max(config.MR_VOL_SCALE_MIN, min(config.MR_VOL_SCALE_MAX, vol_scalar))

    dollars = config.MR_BASE_POSITION * vol_scalar
    shares = int(dollars / signal.price)

    return max(shares, 0)


def size_sector_rotation(signal: TradeSignal) -> float:
    """
    Sector rotation sizing: shares are pre-calculated in the strategy
    based on weight allocation. Just validate and return.
    """
    return max(int(signal.shares), 0)


def size_signal(signal: TradeSignal, strategy_capital: float) -> int:
    """Route to the appropriate sizing function based on strategy."""
    if signal.strategy == "momentum":
        return size_momentum(signal, strategy_capital)
    elif signal.strategy == "mean_reversion":
        return size_mean_reversion(signal)
    elif signal.strategy == "sector_rotation":
        return size_sector_rotation(signal)
    else:
        logger.warning("Unknown strategy: %s", signal.strategy)
        return 0
