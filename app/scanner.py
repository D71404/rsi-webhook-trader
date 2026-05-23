"""
Autonomous RSI market scanner.

Every 5 minutes, fetches the active crypto universe from Alpaca, pulls
the latest 5-minute bars, calculates 14-period RSI via pandas-ta, and
triggers a Long Entry for any asset with RSI <= 20.

Tracks active symbols to prevent duplicate positions.
"""

import asyncio
import logging
import threading

import pandas as pd
import pandas_ta as ta
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from app.executor import _get_client, execute_entry, watch_and_exit

logger = logging.getLogger(__name__)

RSI_THRESHOLD = 20
RSI_PERIOD = 14
BAR_LIMIT = 20  # enough bars for a 14-period RSI calculation
SCAN_INTERVAL = 300  # seconds (5 minutes)
STABLECOIN_SYMBOLS = {"USDC/USD", "USDT/USD", "USDG/USD"}

# Tracks tickers with an active watcher thread to prevent duplicate entries
_active_watchers: set[str] = set()
_active_watchers_lock = threading.Lock()


def register_watcher(ticker: str) -> bool:
    """Register a ticker as actively watched. Returns False if already active."""
    with _active_watchers_lock:
        if ticker in _active_watchers:
            return False
        _active_watchers.add(ticker)
        return True


def unregister_watcher(ticker: str) -> None:
    """Remove a ticker from the active watcher set."""
    with _active_watchers_lock:
        _active_watchers.discard(ticker)


def _get_tradable_symbols() -> list[str]:
    """Fetch all tradable crypto USD pairs from Alpaca, excluding stablecoins."""
    client = _get_client()
    request = GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
    assets = client.get_all_assets(request)
    return [
        a.symbol
        for a in assets
        if a.tradable and a.symbol.endswith("/USD") and a.symbol not in STABLECOIN_SYMBOLS
    ]


def _compute_rsi(symbols: list[str]) -> dict[str, float]:
    """Fetch 5-minute bars and compute 14-period RSI for each symbol.

    Returns a dict of {symbol: latest_rsi} for symbols that have enough data.
    """
    client = CryptoHistoricalDataClient()
    request = CryptoBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        limit=BAR_LIMIT,
    )
    all_bars = client.get_crypto_bars(request)

    rsi_values: dict[str, float] = {}
    for symbol in symbols:
        bars = all_bars.get(symbol)
        if not bars or len(bars) < RSI_PERIOD:
            continue
        closes = pd.Series([float(b.close) for b in bars])
        rsi_series = ta.rsi(closes, length=RSI_PERIOD)
        if rsi_series is not None and not rsi_series.empty:
            latest = rsi_series.iloc[-1]
            if pd.notna(latest):
                rsi_values[symbol] = float(latest)

    return rsi_values


def _watched_exit_wrapper(
    ticker: str,
    qty: float,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
) -> None:
    """Wrapper around watch_and_exit that unregisters the ticker when done."""
    try:
        watch_and_exit(
            ticker=ticker,
            qty=qty,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
        )
    finally:
        unregister_watcher(ticker)
        logger.info("Watcher for %s finished and unregistered.", ticker)


async def run_market_scanner() -> None:
    """Infinite loop: scan every 5 minutes, trigger entries on RSI <= 20."""
    logger.info("Market scanner started — scanning every %ds.", SCAN_INTERVAL)

    while True:
        try:
            symbols = _get_tradable_symbols()
            logger.info("Scanning %d crypto symbols…", len(symbols))

            rsi_values = _compute_rsi(symbols)

            for symbol, rsi in rsi_values.items():
                if rsi > RSI_THRESHOLD:
                    continue

                logger.info("RSI signal: %s RSI=%.2f (<=%.0f)", symbol, rsi, RSI_THRESHOLD)

                if not register_watcher(symbol):
                    logger.info("Skipping %s — watcher already active.", symbol)
                    continue

                result = execute_entry(ticker=symbol)

                if result["status"] != "filled":
                    unregister_watcher(symbol)
                    logger.warning("Entry rejected for %s: %s", symbol, result.get("reason", result["status"]))
                    continue

                # Spawn the exit watcher in a background thread
                t = threading.Thread(
                    target=_watched_exit_wrapper,
                    args=(
                        result["ticker"],
                        result["qty"],
                        result["entry_price"],
                        result["take_profit_price"],
                        result["stop_loss_price"],
                    ),
                    daemon=True,
                )
                t.start()
                logger.info(
                    "Entry filled for %s — watcher spawned (TP=%.2f SL=%.2f).",
                    symbol, result["take_profit_price"], result["stop_loss_price"],
                )

        except Exception as exc:
            logger.error("Scanner cycle failed: %s", exc, exc_info=True)

        await asyncio.sleep(SCAN_INTERVAL)
