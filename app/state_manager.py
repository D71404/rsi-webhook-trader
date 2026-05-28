"""
State manager for tracking trading metrics and dashboard data.

Maintains in-memory state for the dashboard API endpoint including:
- Total equity from Alpaca account
- Daily P&L calculation
- Active positions tracking
- Activity log with 200-item limit
- Trade history from Alpaca API fills
"""

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Activity log file for persistence
ACTIVITY_LOG_FILE = Path(__file__).resolve().parent.parent / "memory" / "activity_log.json"
ACTIVITY_LOG_FILE.parent.mkdir(exist_ok=True)


def _fetch_alpaca_fills(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Fetch FILL activities directly from Alpaca REST API.

    Returns raw fill activities from the broker with fields:
    - symbol, side, qty, price, transaction_time, order_id, etc.
    """
    try:
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")

        if not api_key or not secret_key:
            logger.warning("Alpaca API credentials not configured")
            return []

        # Use paper trading endpoint
        url = "https://paper-api.alpaca.markets/v2/account/activities/FILL"

        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }

        params = {
            "page_size": min(limit, 100),  # Max 100 per page
            "direction": "desc",  # Most recent first
        }

        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()

        fills = response.json()
        logger.info(f"Fetched {len(fills)} fill activities from Alpaca API")
        return fills

    except Exception as e:
        logger.error(f"Failed to fetch fills from Alpaca API: {e}")
        return []


def _reconstruct_trade_history(fills: List[Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
    """
    Reconstruct closed trades from raw fill activities.

    Matches sell orders against prior buy orders (FIFO) to calculate:
    - entry_price, exit_price, realized_pnl

    Returns standardized trade_history format:
    - symbol, side, entry_price, exit_price, qty, realized_pnl, closed_at
    """
    # Group fills by symbol
    symbol_fills = {}

    for fill in fills:
        symbol = fill.get("symbol", "")
        side = fill.get("side", "").lower()
        qty = float(fill.get("qty", 0))
        price = float(fill.get("price", 0))
        timestamp = fill.get("transaction_time", "")

        if not symbol or not side or not qty or not price:
            continue

        if symbol not in symbol_fills:
            symbol_fills[symbol] = {"buys": [], "sells": []}

        if side == "buy":
            symbol_fills[symbol]["buys"].append({
                "qty": qty,
                "price": price,
                "time": timestamp
            })
        elif side == "sell":
            symbol_fills[symbol]["sells"].append({
                "qty": qty,
                "price": price,
                "time": timestamp
            })

    # Match sells with buys to create closed trades (FIFO)
    trade_history = []

    for symbol, fills_data in symbol_fills.items():
        # Sort by timestamp (oldest first for FIFO matching)
        buys = sorted(fills_data["buys"], key=lambda x: x["time"])
        sells = sorted(fills_data["sells"], key=lambda x: x["time"])

        # Match each sell with corresponding buy
        for sell in sells:
            if buys:
                buy = buys.pop(0)  # FIFO: take oldest buy

                realized_pnl = (sell["price"] - buy["price"]) * sell["qty"]

                trade_history.append({
                    "symbol": symbol,
                    "side": "LONG",  # All trades in this system are LONG
                    "entry_price": round(buy["price"], 2),
                    "exit_price": round(sell["price"], 2),
                    "qty": round(sell["qty"], 8),
                    "realized_pnl": round(realized_pnl, 2),
                    "closed_at": sell["time"]
                })

    # Sort by closed_at descending (most recent first)
    trade_history.sort(key=lambda x: x["closed_at"], reverse=True)

    return trade_history[:limit]


class TradingStateManager:
    """Manages trading state and metrics for the dashboard API."""

    def __init__(self, max_activity_log: int = 200):
        """Initialize state manager with thread-safe collections."""
        self._lock = threading.RLock()
        self.max_activity_log = max_activity_log

        # Initialize state
        self._state = {
            "total_equity": 0.0,
            "daily_pnl": 0.0,
            "daily_pnl_percent": 0.0,
            "active_positions": [],
            "open_positions_count": 0,
            "last_scan_time": None,
            "scanner_status": "idle",
            "today_trades": 0,
            "today_wins": 0,
            "today_losses": 0,
        }

        # Activity log as deque for automatic size limiting
        self._activity_log: deque = deque(maxlen=max_activity_log)

        # Load existing activity log if available
        self._load_activity_log()

    def _load_activity_log(self) -> None:
        """Load activity log from file if it exists."""
        try:
            if ACTIVITY_LOG_FILE.exists():
                with open(ACTIVITY_LOG_FILE, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        # Only load the most recent items up to max
                        for item in data[-self.max_activity_log:]:
                            self._activity_log.append(item)
                        logger.info(f"Loaded {len(self._activity_log)} activity items from file")
        except Exception as e:
            logger.warning(f"Could not load activity log: {e}")

    def _save_activity_log(self) -> None:
        """Persist activity log to file."""
        try:
            with open(ACTIVITY_LOG_FILE, 'w') as f:
                json.dump(list(self._activity_log), f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Could not save activity log: {e}")

    def update_account_metrics(self, total_equity: float, daily_pnl: float) -> None:
        """Update account-level metrics."""
        with self._lock:
            self._state["total_equity"] = round(total_equity, 2)
            self._state["daily_pnl"] = round(daily_pnl, 2)

            # Calculate daily P&L percentage
            if total_equity > 0 and daily_pnl != 0:
                # Estimate starting equity (total_equity - daily_pnl)
                start_equity = total_equity - daily_pnl
                if start_equity > 0:
                    self._state["daily_pnl_percent"] = round((daily_pnl / start_equity) * 100, 2)
                else:
                    self._state["daily_pnl_percent"] = 0.0
            else:
                self._state["daily_pnl_percent"] = 0.0

    def update_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Update active positions list."""
        with self._lock:
            self._state["active_positions"] = positions
            self._state["open_positions_count"] = len(positions)

    def update_scanner_status(self, status: str, last_scan_time: Optional[datetime] = None) -> None:
        """Update scanner status and last scan time."""
        with self._lock:
            self._state["scanner_status"] = status
            if last_scan_time:
                self._state["last_scan_time"] = last_scan_time.isoformat()

    def add_activity(self, activity_type: str, message: str, details: Optional[Dict] = None) -> None:
        """Add an activity to the log."""
        with self._lock:
            activity = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": activity_type,
                "message": message,
                "details": details or {}
            }

            self._activity_log.append(activity)
            self._save_activity_log()

            # Update trade counters for today
            if activity_type == "trade_entry":
                self._state["today_trades"] += 1
            elif activity_type == "trade_exit":
                if details and "pnl" in details:
                    if details["pnl"] > 0:
                        self._state["today_wins"] += 1
                    else:
                        self._state["today_losses"] += 1

    def log_trade_entry(self, ticker: str, qty: float, price: float, order_id: str) -> None:
        """Log a trade entry."""
        self.add_activity(
            "trade_entry",
            f"Entered {ticker} - {qty} @ ${price:.2f}",
            {
                "ticker": ticker,
                "qty": qty,
                "price": price,
                "order_id": order_id,
                "action": "BUY"
            }
        )

    def log_trade_exit(self, ticker: str, qty: float, price: float, entry_price: float,
                       reason: str, order_id: str) -> None:
        """Log a trade exit."""
        pnl = (price - entry_price) * qty
        pnl_percent = ((price - entry_price) / entry_price) * 100 if entry_price > 0 else 0

        self.add_activity(
            "trade_exit",
            f"Exited {ticker} - {qty} @ ${price:.2f} ({reason}) - P&L: ${pnl:.2f}",
            {
                "ticker": ticker,
                "qty": qty,
                "price": price,
                "entry_price": entry_price,
                "reason": reason,
                "order_id": order_id,
                "action": "SELL",
                "pnl": round(pnl, 2),
                "pnl_percent": round(pnl_percent, 2)
            }
        )

    def log_scan_result(self, symbols_scanned: int, signals_found: int) -> None:
        """Log a market scan result."""
        self.add_activity(
            "market_scan",
            f"Scanned {symbols_scanned} symbols, found {signals_found} RSI signals",
            {
                "symbols_scanned": symbols_scanned,
                "signals_found": signals_found
            }
        )
        self.update_scanner_status("completed", datetime.now(timezone.utc))

    def get_dashboard_data(self) -> Dict[str, Any]:
        """Get all dashboard data as a dictionary."""
        with self._lock:
            # Fetch fills from Alpaca API and reconstruct trade history
            fills = _fetch_alpaca_fills(limit=100)
            trade_history = _reconstruct_trade_history(fills, limit=50)

            return {
                **self._state.copy(),
                "activity_log": list(self._activity_log)[-50:],  # Return last 50 items for API
                "trade_history": trade_history,  # Include closed trades from API
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

    def reset_daily_counters(self) -> None:
        """Reset daily trade counters (call at market open or midnight)."""
        with self._lock:
            self._state["today_trades"] = 0
            self._state["today_wins"] = 0
            self._state["today_losses"] = 0
            self._state["daily_pnl"] = 0.0
            self._state["daily_pnl_percent"] = 0.0
            logger.info("Daily counters reset")


# Global state manager instance
state_manager = TradingStateManager()