"""
Trade executor using alpaca-py against Alpaca Paper Trading.

Enforces a hard cap of 15 open short positions (checked live from Alpaca).
After every successful trade, appends a row to memory/TRADE-LOG.md and
commits the file via git (Git-as-Memory pattern).
"""

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_LOG = REPO_ROOT / "memory" / "TRADE-LOG.md"
MAX_POSITIONS = 15
DEFAULT_QTY = 1  # shares / fractional units per order


def _get_client() -> TradingClient:
    """Return a TradingClient pointed at Alpaca Paper Trading."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=True)


def open_positions_count() -> int:
    """Return the number of currently open positions on Alpaca."""
    try:
        client = _get_client()
        positions = client.get_all_positions()
        return len(positions)
    except Exception as exc:
        logger.warning("Could not fetch positions from Alpaca: %s", exc)
        return 0


def _append_trade_log(ticker: str, action: str, price: float, size: float, notes: str) -> None:
    """Append one row to memory/TRADE-LOG.md and git-commit the file."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    row = f"| {date_str} | {ticker} | {action} | {price} | {size} | {notes} |\n"

    with TRADE_LOG.open("a") as fh:
        fh.write(row)

    try:
        subprocess.run(
            ["git", "add", str(TRADE_LOG)],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "webhook trade executed"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        logger.info("Trade committed to git memory.")
    except subprocess.CalledProcessError as exc:
        logger.warning("Git commit failed: %s", exc.stderr.decode().strip())


def execute_short(ticker: str, price: float) -> dict:
    """
    Open a short (sell) position for *ticker* at the given *price* via
    Alpaca Paper Trading.

    Returns a result dict.  Refuses to open if open positions >= MAX_POSITIONS.
    """
    client = _get_client()

    # Hard gate: check live position count from Alpaca
    try:
        positions = client.get_all_positions()
    except Exception as exc:
        return {"status": "error", "reason": f"Could not fetch positions: {exc}"}

    if len(positions) >= MAX_POSITIONS:
        logger.warning(
            "Position cap reached (%d/%d). Aborting trade for %s.",
            len(positions), MAX_POSITIONS, ticker,
        )
        return {
            "status": "rejected",
            "reason": f"Position cap reached ({MAX_POSITIONS}). Close a position first.",
            "open_positions": len(positions),
        }

    symbol = ticker.upper().replace("/", "")  # AAPL, BTCUSD, etc.
    qty = DEFAULT_QTY

    order_data = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
    )

    try:
        order = client.submit_order(order_data=order_data)
        order_id = str(order.id)
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}

    _append_trade_log(
        ticker=symbol,
        action="short",
        price=price,
        size=qty,
        notes=f"order_id={order_id}",
    )

    return {
        "status": "filled",
        "order_id": order_id,
        "ticker": symbol,
        "qty": qty,
        "open_positions": len(positions) + 1,
    }
