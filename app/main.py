"""
RSI Webhook Trader — FastAPI server that receives TradingView alerts
and routes them to the ccxt executor for paper-traded short positions.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from pydantic import BaseModel

from app.executor import execute_short, open_positions_count

app = FastAPI(title="RSI Webhook Trader", version="0.1.0")

PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "changeme")


class WebhookPayload(BaseModel):
    passphrase: str
    ticker: str
    action: str  # only "short" is handled for now
    price: float


@app.get("/health")
def health():
    return {
        "status": "ok",
        "open_positions": open_positions_count(),
    }


@app.post("/webhook")
def webhook(payload: WebhookPayload):
    if payload.passphrase != PASSPHRASE:
        raise HTTPException(status_code=403, detail="Invalid passphrase")

    if payload.action != "short":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported action '{payload.action}'. Only 'short' is supported.",
        )

    result = execute_short(ticker=payload.ticker, price=payload.price)

    if result["status"] == "rejected":
        raise HTTPException(status_code=429, detail=result["reason"])
    if result["status"] == "error":
        raise HTTPException(status_code=502, detail=result["reason"])

    return result
