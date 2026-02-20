"""
Order execution wrapper for the competition bot.
Extends parent execution.py with short selling and limit orders.
"""

import logging
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from competition import config, state

logger = logging.getLogger(__name__)


def connect_alpaca() -> TradingClient:
    """Create an Alpaca TradingClient for the competition."""
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

    client = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=config.ALPACA_PAPER,
    )

    account = client.get_account()
    logger.info("Connected to Alpaca (%s)", "PAPER" if config.ALPACA_PAPER else "LIVE")
    logger.info("Equity: $%s | Cash: $%s", account.equity, account.cash)

    if account.status != "ACTIVE":
        raise ValueError(f"Account not active: {account.status}")

    return client


def get_account_info(client: TradingClient) -> dict:
    """Get account equity and cash."""
    account = client.get_account()
    return {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
    }


def get_positions(client: TradingClient) -> dict[str, dict]:
    """Get all current broker positions. Returns {symbol: {qty, side, market_value, avg_entry}}."""
    positions = {}
    for pos in client.get_all_positions():
        qty = float(pos.qty)
        positions[pos.symbol] = {
            "qty": abs(qty),
            "side": "long" if qty > 0 else "short",
            "market_value": float(pos.market_value),
            "avg_entry": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "unrealized_pnl": float(pos.unrealized_pl),
        }
    return positions


def execute_entry(
    client: TradingClient | None,
    strategy: str,
    ticker: str,
    side: str,  # "buy" or "sell"
    direction: str,  # "long" or "short"
    shares: int,
    price: float,
    stop_price: float | None = None,
    target_price: float | None = None,
    dry_run: bool = False,
    signal_details: dict | None = None,
) -> dict:
    """
    Execute an entry order. Uses limit orders with fallback to market.
    Returns execution result dict.
    """
    if shares <= 0:
        return {"status": "SKIPPED", "reason": "zero shares"}

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    if dry_run:
        logger.info("[DRY RUN] %s %d x %s @ $%.2f (%s %s)",
                     side.upper(), shares, ticker, price, direction, strategy)
        pos_id = state.open_position(
            strategy, ticker, direction, shares, price, stop_price, target_price,
        )
        state.log_trade(
            strategy, ticker, side, shares, "LIMIT", price, price, "DRY_RUN",
            signal_details,
        )
        return {"status": "DRY_RUN", "pos_id": pos_id, "fill_price": price}

    # Try limit order first
    fill_price = _try_limit_order(client, ticker, order_side, shares, price)

    if fill_price is None:
        # Fallback to market order
        logger.info("Limit order timeout, falling back to market for %s", ticker)
        fill_price = _market_order(client, ticker, order_side, shares)

    if fill_price is None:
        state.log_trade(
            strategy, ticker, side, shares, "LIMIT+MKT", price, None, "FAILED",
            signal_details,
        )
        return {"status": "FAILED", "ticker": ticker}

    pos_id = state.open_position(
        strategy, ticker, direction, shares, fill_price, stop_price, target_price,
    )
    state.log_trade(
        strategy, ticker, side, shares, "LIMIT", price, fill_price, "FILLED",
        signal_details,
    )

    logger.info("FILLED %s %d x %s @ $%.2f (requested $%.2f, slippage=%.4f%%)",
                side.upper(), shares, ticker, fill_price, price,
                abs(fill_price - price) / price * 100)

    return {"status": "FILLED", "pos_id": pos_id, "fill_price": fill_price}


def execute_exit(
    client: TradingClient | None,
    position: dict,
    current_price: float,
    reason: str,
    dry_run: bool = False,
) -> dict:
    """Execute an exit order for an open position."""
    ticker = position["ticker"]
    shares = position["shares"]
    side = position["side"]

    # To close a long, sell. To close a short, buy.
    order_side = OrderSide.SELL if side == "long" else OrderSide.BUY
    action = "sell" if side == "long" else "buy"

    if dry_run:
        logger.info("[DRY RUN] EXIT %s %d x %s @ $%.2f (%s)",
                     action.upper(), shares, ticker, current_price, reason)
        state.close_position(position["id"], current_price, reason)
        state.log_trade(
            position["strategy"], ticker, action, shares,
            "MARKET", current_price, current_price, "DRY_RUN",
            {"reason": reason},
        )
        return {"status": "DRY_RUN", "fill_price": current_price}

    # Use market order for exits (speed matters)
    fill_price = _market_order(client, ticker, order_side, int(shares))

    if fill_price is None:
        state.log_trade(
            position["strategy"], ticker, action, shares,
            "MARKET", current_price, None, "FAILED",
            {"reason": reason},
        )
        return {"status": "FAILED"}

    state.close_position(position["id"], fill_price, reason)
    state.log_trade(
        position["strategy"], ticker, action, shares,
        "MARKET", current_price, fill_price, "FILLED",
        {"reason": reason},
    )

    logger.info("EXIT %s %d x %s @ $%.2f (%s)", action.upper(), shares, ticker, fill_price, reason)
    return {"status": "FILLED", "fill_price": fill_price}


def _try_limit_order(
    client: TradingClient, ticker: str, side: OrderSide, shares: int, ref_price: float,
) -> float | None:
    """
    Place a limit order: ask+$0.01 for buys, bid-$0.01 for sells.
    Wait up to LIMIT_ORDER_TIMEOUT_SEC for fill.
    Returns fill price or None.
    """
    # Set limit price with $0.01 improvement
    if side == OrderSide.BUY:
        limit_price = round(ref_price + 0.01, 2)
    else:
        limit_price = round(ref_price - 0.01, 2)

    req = LimitOrderRequest(
        symbol=ticker,
        qty=shares,
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    try:
        order = client.submit_order(req)
        order_id = order.id

        # Poll for fill
        deadline = time.time() + config.LIMIT_ORDER_TIMEOUT_SEC
        while time.time() < deadline:
            time.sleep(2)
            updated = client.get_order_by_id(order_id)
            if updated.status.value == "filled":
                return float(updated.filled_avg_price)
            if updated.status.value in ("canceled", "expired", "rejected"):
                return None

        # Timeout — cancel the order
        try:
            client.cancel_order_by_id(order_id)
        except Exception:
            pass
        return None

    except Exception as e:
        logger.error("Limit order failed for %s: %s", ticker, e)
        return None


def _market_order(
    client: TradingClient, ticker: str, side: OrderSide, shares: int,
) -> float | None:
    """Place a market order. Returns fill price or None."""
    req = MarketOrderRequest(
        symbol=ticker,
        qty=shares,
        side=side,
        time_in_force=TimeInForce.DAY,
    )

    try:
        order = client.submit_order(req)
        # Wait briefly for fill
        for _ in range(10):
            time.sleep(1)
            updated = client.get_order_by_id(order.id)
            if updated.status.value == "filled":
                return float(updated.filled_avg_price)
            if updated.status.value in ("canceled", "expired", "rejected"):
                return None
        # Return partial fill price if available
        if updated.filled_avg_price:
            return float(updated.filled_avg_price)
        return None
    except Exception as e:
        logger.error("Market order failed for %s: %s", ticker, e)
        return None
