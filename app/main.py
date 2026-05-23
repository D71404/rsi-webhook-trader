"""
RSI Webhook Trader — FastAPI server that receives TradingView alerts
and routes them to the executor for paper-traded crypto positions.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from app.executor import execute_entry, open_positions_count, watch_and_exit

app = FastAPI(title="RSI Webhook Trader", version="0.2.0")

PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "changeme")


class WebhookPayload(BaseModel):
    passphrase: str
    ticker: str
    action: str  # "buy" for LONG entries
    price: float | None = None


@app.get("/health")
def health():
    return {
        "status": "ok",
        "open_positions": open_positions_count(),
    }


@app.post("/webhook")
def webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    if payload.passphrase != PASSPHRASE:
        raise HTTPException(status_code=403, detail="Invalid passphrase")

    if payload.action != "buy":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported action '{payload.action}'. Only 'buy' is supported.",
        )

    result = execute_entry(ticker=payload.ticker)

    if result["status"] == "rejected":
        raise HTTPException(status_code=429, detail=result["reason"])
    if result["status"] == "error":
        raise HTTPException(status_code=502, detail=result["reason"])

    # Spawn background watcher to auto-exit at TP or SL
    background_tasks.add_task(
        watch_and_exit,
        ticker=result["ticker"],
        qty=result["qty"],
        entry_price=result["entry_price"],
        take_profit_price=result["take_profit_price"],
        stop_loss_price=result["stop_loss_price"],
    )

    return result
