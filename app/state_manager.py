"""
State manager for tracking trading metrics and dashboard data.

Maintains in-memory state for the dashboard API endpoint including:
- Total equity from Alpaca account
- Daily P&L calculation
- Active positions tracking
- Activity log with 200-item limit
- Trade history from logged activities
"""

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Activity log file for persistence
ACTIVITY_LOG_FILE = Path(__file__).resolve().parent.parent / "memory" / "activity_log.json"
ACTIVITY_LOG_FILE.parent.mkdir(exist_ok=True)


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

    def _extract_trade_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Extract closed trade history from activity log.

        Returns a list of dictionaries with standardized keys:
        - symbol (str)
        - side (str): "LONG" or "SHORT"
        - entry_price (float)
        - exit_price (float)
        - qty (float)
        - realized_pnl (float)
        - closed_at (str): ISO timestamp
        """
        trade_history = []

        for activity in self._activity_log:
            if activity.get("type") == "trade_exit":
                details = activity.get("details", {})

                # Extract trade information
                symbol = details.get("ticker", "")
                qty = details.get("qty", 0.0)
                exit_price = details.get("price", 0.0)
                entry_price = details.get("entry_price", 0.0)
                pnl = details.get("pnl", 0.0)
                closed_at = activity.get("timestamp", "")

                # Only include valid trades with complete data
                if symbol and qty and exit_price and entry_price:
                    trade_history.append({
                        "symbol": symbol,
                        "side": "LONG",  # All trades in this system are LONG positions
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "qty": round(qty, 8),
                        "realized_pnl": round(pnl, 2),
                        "closed_at": closed_at
                    })

        # Return most recent trades (already sorted by timestamp in activity log)
        return trade_history[-limit:] if len(trade_history) > limit else trade_history

    def get_dashboard_data(self) -> Dict[str, Any]:
        """Get all dashboard data as a dictionary."""
        with self._lock:
            # Extract trade history from activity log
            trade_history = self._extract_trade_history(limit=50)

            return {
                **self._state.copy(),
                "activity_log": list(self._activity_log)[-50:],  # Return last 50 items for API
                "trade_history": trade_history,  # Include closed trades
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