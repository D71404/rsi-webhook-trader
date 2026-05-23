# Crypto RSI Scalping Strategy (Oversold Bounce)

## 1. Core Mechanics
* **Asset Class:** Crypto (Alpaca-supported pairs like BTC/USD, ETH/USD, SOL/USD)
* **Timeframe:** 5 Minutes (5m)
* **Indicator:** Relative Strength Index (RSI) using a standard 14-period window.
* **Trigger Condition:** RSI is equal to or less than 20% (Oversold condition).

## 2. Order Execution & Risk Management
When a webhook trigger is received from TradingView, the bot must immediately execute a LONG position and automate the exit via an internal price watcher:

* **Action:** Buy at Market Price.
* **Position Size:** Exactly $1,000 USD per trade.
* **Take Profit (TP):** Target is fixed at 0.5% above the entry price.
* **Stop Loss (SL):** Target is fixed at 1.0% below the entry price.

*Execution Note: Because Alpaca does not natively support Bracket/OCO orders for Crypto, the bot must immediately spin up an internal asynchronous background task to track the live price every 2 seconds and execute a Market Sell order the millisecond either the TP or SL target is crossed.*

## 3. Position Sizing & Portfolio Theory
* **The Numbers Game:** This strategy relies on high volume (10 to 15 concurrent positions).
* **Risk Model:** Because the Stop Loss risk (1.0%) is double the Take Profit reward (0.5%), the strategy requires a high win rate. Testing strictly with $1,000 fractional sizes ensures the $100k paper account is fully insulated from cascading liquidations.
