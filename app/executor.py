"""
Trade executor using alpaca-py against Alpaca Paper Trading.

Executes LONG entries on crypto pairs when RSI oversold signals arrive
via webhook.  Spawns a background price watcher to exit at TP (+0.5%)
or SL (-1.0%).  All trades are logged to memory/TRADE-LOG.md and
pushed to GitHub (Git-as-Memory pattern).
"""

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from app.state_manager import state_manager

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_LOG = REPO_ROOT / "memory" / "TRADE-LOG.md"
MAX_POSITIONS = 15
TRADE_NOTIONAL_VALUE = 1000  # USD per trade


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def _get_client() -> TradingClient:
    """Return a TradingClient pointed at Alpaca Paper Trading."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=True)


def _get_data_client() -> CryptoHistoricalDataClient:
    """Return a CryptoHistoricalDataClient for live quotes."""
    return CryptoHistoricalDataClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_live_price(symbol: str) -> float:
    """Fetch the latest ask price for a crypto symbol from Alpaca."""
    client = _get_data_client()
    request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = client.get_crypto_latest_quote(request)
    return float(quotes[symbol].ask_price)


def open_positions_count() -> int:
    """Return the number of currently open positions on Alpaca."""
    try:
        client = _get_client()
        positions = client.get_all_positions()
        return len(positions)
    except Exception as exc:
        logger.warning("Could not fetch positions from Alpaca: %s", exc)
        return 0


def _normalize_symbol(ticker: str) -> str:
    """Ensure the ticker uses the 'XXX/USD' format required by Alpaca crypto APIs."""
    ticker = ticker.upper().strip()
    if "/" not in ticker and ticker.endswith("USD"):
        return ticker[:-3] + "/USD"
    return ticker


# ---------------------------------------------------------------------------
# Git-as-Memory
# ---------------------------------------------------------------------------

def _append_trade_log(ticker: str, action: str, price: float, size: float, notes: str) -> None:
    """Append one row to memory/TRADE-LOG.md, git-commit, and push."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    row = f"| {date_str} | {ticker} | {action} | {price} | {size} | {notes} |\n"

    with TRADE_LOG.open("a") as fh:
        fh.write(row)

    try:
        subprocess.run(
            ["git", "add", str(TRADE_LOG)],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"trade {action}: {ticker}"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        logger.info("Trade %s committed to git memory.", action)
        _git_push()
    except subprocess.CalledProcessError as exc:
        logger.warning("Git commit failed: %s", exc.stderr.decode().strip())


def _git_push() -> None:
    """Push to remote. Uses GITHUB_TOKEN in cloud, origin locally."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")

    if token and repo:
        remote_url = f"https://oauth2:{token}@github.com/{repo}.git"
        cmd = ["git", "push", remote_url, "main"]
    else:
        cmd = ["git", "push", "origin", "main"]

    try:
        subprocess.run(cmd, cwd=REPO_ROOT, check=True, capture_output=True)
        logger.info("Trade log pushed to remote.")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode().strip()
        if token:
            stderr = stderr.replace(token, "***")
        logger.warning("Git push failed: %s", stderr)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def execute_entry(ticker: str) -> dict:
    """
    Execute a LONG (buy) entry for *ticker* using $1,000 notional value.

    Fetches the live ask price from Alpaca, calculates qty / TP / SL,
    submits a market buy, logs the ENTRY, and returns all values the
    background watcher needs.
    """
    client = _get_client()

    # Hard gate: check live position count
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

    symbol = _normalize_symbol(ticker)

    # Fetch live entry price
    try:
        entry_price = _get_live_price(symbol)
    except Exception as exc:
        return {"status": "error", "reason": f"Could not fetch quote for {symbol}: {exc}"}

    # Position sizing and targets
    qty = round(TRADE_NOTIONAL_VALUE / entry_price, 8)
    take_profit_price = round(entry_price * 1.005, 2)
    stop_loss_price = round(entry_price * 0.990, 2)

    # Submit BUY order
    order_data = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
    )

    try:
        order = client.submit_order(order_data=order_data)
        order_id = str(order.id)
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}

    # Log ENTRY
    _append_trade_log(
        ticker=symbol,
        action="ENTRY",
        price=entry_price,
        size=qty,
        notes=f"order_id={order_id} TP={take_profit_price} SL={stop_loss_price}",
    )

    # Log to state manager
    state_manager.log_trade_entry(symbol, qty, entry_price, order_id)

    return {
        "status": "filled",
        "order_id": order_id,
        "ticker": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "take_profit_price": take_profit_price,
        "stop_loss_price": stop_loss_price,
        "open_positions": len(positions) + 1,
    }


# ---------------------------------------------------------------------------
# Background Watcher (runs in Starlette threadpool via BackgroundTasks)
# ---------------------------------------------------------------------------

def watch_and_exit(
    ticker: str,
    qty: float,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
) -> None:
    """
    Poll the live price every 2 seconds.
    Execute a market SELL the moment TP or SL is breached.
    """
    logger.info(
        "Watcher started for %s | entry=%.2f TP=%.2f SL=%.2f",
        ticker, entry_price, take_profit_price, stop_loss_price,
    )

    while True:
        time.sleep(2)

        try:
            live_price = _get_live_price(ticker)
        except Exception as exc:
            logger.warning("Price fetch failed for %s: %s", ticker, exc)
            continue

        if live_price >= take_profit_price:
            exit_reason = "TP"
        elif live_price <= stop_loss_price:
            exit_reason = "SL"
        else:
            continue

        # Exit triggered
        logger.info(
            "Exit triggered for %s | reason=%s live=%.2f",
            ticker, exit_reason, live_price,
        )

        client = _get_client()
        order_data = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        )

        try:
            order = client.submit_order(order_data=order_data)
            order_id = str(order.id)
            _append_trade_log(
                ticker=ticker,
                action="EXIT",
                price=live_price,
                size=qty,
                notes=f"order_id={order_id} reason={exit_reason} entry={entry_price}",
            )

            # Log to state manager
            state_manager.log_trade_exit(ticker, qty, live_price, entry_price, exit_reason, order_id)

            logger.info("Exited %s at %.2f (%s hit)", ticker, live_price, exit_reason)
        except Exception as exc:
            logger.error("Exit order failed for %s: %s", ticker, exc)

        break
