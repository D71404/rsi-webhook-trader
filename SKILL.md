---
name: rsi-webhook-trader
description: TradingView RSI webhook receiver with ccxt paper-trading execution on Binance Testnet
---

# RSI Webhook Trader

A FastAPI server that listens for TradingView RSI alerts and opens short positions on Binance Futures Testnet via ccxt. Positions are tracked in a local `positions.json` file with a hard cap of 15 open shorts.

## Architecture

```
TradingView Alert ──POST /webhook──▶ FastAPI ──▶ executor.py ──▶ Binance Testnet
                                                     │
                                                     ▼
                                              positions.json
```

- **app/main.py** — FastAPI app with `/webhook` (POST) and `/health` (GET) endpoints.
- **app/executor.py** — ccxt trade executor. Enforces the 15-position cap, writes state to `positions.json`.
- **positions.json** — local ledger of all open short positions (created on first trade).

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `WEBHOOK_PASSPHRASE` | Must match the `passphrase` field in every webhook payload | `changeme` |
| `BINANCE_TESTNET_API_KEY` | Binance Futures Testnet API key | _(none — paper entries logged without exchange call)_ |
| `BINANCE_TESTNET_SECRET` | Binance Futures Testnet API secret | _(none)_ |

## Webhook Payload

```json
{
  "passphrase": "your-secret",
  "ticker": "BTCUSDT",
  "action": "short",
  "price": 68421.50
}
```

## Natural Language Invocations

| Say this to Claude | What happens |
|---|---|
| "start the rsi webhook server" | `cd ~/.claude/skills/rsi-webhook-trader && uv run uvicorn app.main:app --reload` |
| "how many positions are open?" | `cd ~/.claude/skills/rsi-webhook-trader && uv run python -c "from app.executor import open_positions_count; print(open_positions_count())"` |
| "show open positions" | `cat ~/.claude/skills/rsi-webhook-trader/positions.json` |
| "reset all positions" | `echo '[]' > ~/.claude/skills/rsi-webhook-trader/positions.json` |
| "start the webhook on port 9000" | `cd ~/.claude/skills/rsi-webhook-trader && uv run uvicorn app.main:app --reload --port 9000` |
