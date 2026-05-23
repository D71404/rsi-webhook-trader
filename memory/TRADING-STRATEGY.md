# Trading Strategy

## Core Rules

1. **Max 15 open short positions.** Do not open a new position if 15 or more shorts are already open on Alpaca. Abort the trade and log a warning.

2. **Paper trading on Alpaca only.** The `TradingClient` must always be instantiated with `paper=True`. Live trading is not permitted.
