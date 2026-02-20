"""Fixed stock and ETF universes for competition strategies."""

# Momentum: 20 most liquid large-caps
MOMENTUM_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "JPM", "UNH", "V",
    "XOM", "JNJ", "MA", "PG", "HD",
    "AVGO", "COST", "MRK", "ABBV", "BRK.B",
]

# Sector Rotation: 11 SPDR sector ETFs + SPY
SECTOR_ETFS = [
    "XLB", "XLC", "XLE", "XLF", "XLI",
    "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
]
SECTOR_UNIVERSE = SECTOR_ETFS + ["SPY"]

# Mean Reversion: dynamic top 100 by dollar volume from S&P 500
# Resolved at runtime in the strategy — this is just documentation
MEAN_REVERSION_POOL_SIZE = 100
