"""
Configuration constants for the competition trading bot.
All values can be overridden via environment variables prefixed with COMP_.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _env_float(key, default):
    return float(os.environ.get(key, default))


def _env_int(key, default):
    return int(os.environ.get(key, default))


def _env_bool(key, default):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes")


# --- Alpaca connection (competition account, falls back to parent keys) ---
ALPACA_API_KEY = os.environ.get("COMP_ALPACA_API_KEY", os.environ.get("ALPACA_API_KEY", ""))
ALPACA_SECRET_KEY = os.environ.get("COMP_ALPACA_SECRET_KEY", os.environ.get("ALPACA_SECRET_KEY", ""))
ALPACA_PAPER = _env_bool("ALPACA_PAPER", True)

# --- Execution mode ---
DRY_RUN = _env_bool("COMP_DRY_RUN", True)

# --- Total capital ---
TOTAL_CAPITAL = _env_float("COMP_TOTAL_CAPITAL", 1_000_000.0)

# --- Strategy capital allocation ---
MOMENTUM_PCT = _env_float("COMP_MOMENTUM_PCT", 0.40)
MEAN_REVERSION_PCT = _env_float("COMP_MEAN_REVERSION_PCT", 0.35)
SECTOR_ROTATION_PCT = _env_float("COMP_SECTOR_ROTATION_PCT", 0.25)

# Derived capital pools
MOMENTUM_CAPITAL = TOTAL_CAPITAL * MOMENTUM_PCT
MEAN_REVERSION_CAPITAL = TOTAL_CAPITAL * MEAN_REVERSION_PCT
SECTOR_ROTATION_CAPITAL = TOTAL_CAPITAL * SECTOR_ROTATION_PCT

# --- Momentum strategy ---
MOM_RISK_PER_TRADE = _env_float("COMP_MOM_RISK_PER_TRADE", 0.01)
MOM_STOP_LOSS_PCT = _env_float("COMP_MOM_STOP_LOSS_PCT", 0.007)
MOM_PROFIT_TARGET_PCT = _env_float("COMP_MOM_PROFIT_TARGET_PCT", 0.015)
MOM_TRAILING_ACTIVATE_PCT = _env_float("COMP_MOM_TRAILING_ACTIVATE_PCT", 0.008)
MOM_MAX_POSITION_PCT = _env_float("COMP_MOM_MAX_POSITION_PCT", 0.20)
MOM_MAX_POSITIONS = _env_int("COMP_MOM_MAX_POSITIONS", 5)
MOM_VOLUME_MULTIPLIER = _env_float("COMP_MOM_VOLUME_MULTIPLIER", 1.5)
MOM_EMA_FAST = _env_int("COMP_MOM_EMA_FAST", 9)
MOM_EMA_SLOW = _env_int("COMP_MOM_EMA_SLOW", 21)

# --- Mean Reversion strategy ---
MR_BB_PERIOD = _env_int("COMP_MR_BB_PERIOD", 20)
MR_BB_STD = _env_float("COMP_MR_BB_STD", 2.0)
MR_RSI_PERIOD = _env_int("COMP_MR_RSI_PERIOD", 14)
MR_RSI_OVERSOLD = _env_float("COMP_MR_RSI_OVERSOLD", 30.0)
MR_RSI_OVERBOUGHT = _env_float("COMP_MR_RSI_OVERBOUGHT", 70.0)
MR_RSI_NEUTRAL = _env_float("COMP_MR_RSI_NEUTRAL", 50.0)
MR_VOLUME_SPIKE = _env_float("COMP_MR_VOLUME_SPIKE", 1.5)
MR_SMA_PROXIMITY = _env_float("COMP_MR_SMA_PROXIMITY", 0.05)
MR_BASE_POSITION = _env_float("COMP_MR_BASE_POSITION", 35_000.0)
MR_VOL_SCALE_MIN = _env_float("COMP_MR_VOL_SCALE_MIN", 0.5)
MR_VOL_SCALE_MAX = _env_float("COMP_MR_VOL_SCALE_MAX", 1.5)
MR_STOP_LOSS_PCT = _env_float("COMP_MR_STOP_LOSS_PCT", 0.015)
MR_MAX_HOLD_BARS = _env_int("COMP_MR_MAX_HOLD_BARS", 192)  # ~2 days of 15-min bars
MR_MAX_POSITIONS = _env_int("COMP_MR_MAX_POSITIONS", 10)

# --- Sector Rotation strategy ---
SR_ROC_FAST = _env_int("COMP_SR_ROC_FAST", 5)
SR_ROC_SLOW = _env_int("COMP_SR_ROC_SLOW", 10)
SR_ROC_FAST_WEIGHT = _env_float("COMP_SR_ROC_FAST_WEIGHT", 0.6)
SR_ROC_SLOW_WEIGHT = _env_float("COMP_SR_ROC_SLOW_WEIGHT", 0.4)
SR_SMA_SHORT = _env_int("COMP_SR_SMA_SHORT", 10)
SR_SMA_LONG = _env_int("COMP_SR_SMA_LONG", 20)
SR_EMERGENCY_DROP = _env_float("COMP_SR_EMERGENCY_DROP", -0.02)
SR_MAX_POSITIONS = _env_int("COMP_SR_MAX_POSITIONS", 5)

# --- Risk management ---
DRAWDOWN_REDUCE_PCT = _env_float("COMP_DRAWDOWN_REDUCE_PCT", 0.08)
DRAWDOWN_FLATTEN_PCT = _env_float("COMP_DRAWDOWN_FLATTEN_PCT", 0.12)
DAILY_LOSS_LIMIT_PCT = _env_float("COMP_DAILY_LOSS_LIMIT_PCT", 0.03)
EXPOSURE_RISK_ON = _env_float("COMP_EXPOSURE_RISK_ON", 0.90)
EXPOSURE_NEUTRAL = _env_float("COMP_EXPOSURE_NEUTRAL", 0.82)
EXPOSURE_RISK_OFF = _env_float("COMP_EXPOSURE_RISK_OFF", 0.70)

# --- Execution ---
LIMIT_ORDER_TIMEOUT_SEC = _env_int("COMP_LIMIT_ORDER_TIMEOUT_SEC", 30)
CYCLE_INTERVAL_SEC = _env_int("COMP_CYCLE_INTERVAL_SEC", 60)  # 1 minute

# --- Database ---
DB_PATH = os.environ.get("COMP_DB_PATH", "competition_bot.db")
