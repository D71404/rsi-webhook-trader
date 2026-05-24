"""
RSI Webhook Trader — FastAPI server with an autonomous 5-minute
crypto market scanner and a manual webhook endpoint for testing.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.executor import execute_entry, open_positions_count, watch_and_exit
from app.scanner import run_market_scanner
from app.state_manager import state_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


async def update_account_metrics():
    """Periodically update account metrics from Alpaca."""
    from app.executor import _get_client

    while True:
        try:
            client = _get_client()
            account = client.get_account()
            positions = client.get_all_positions()

            # Update account metrics
            total_equity = float(account.equity)
            daily_pnl = float(account.equity) - float(account.last_equity)
            state_manager.update_account_metrics(total_equity, daily_pnl)

            # Update positions
            positions_data = []
            for pos in positions:
                positions_data.append({
                    "symbol": pos.symbol,
                    "qty": float(pos.qty),
                    "side": pos.side.value if hasattr(pos.side, 'value') else str(pos.side),
                    "avg_entry_price": float(pos.avg_entry_price) if pos.avg_entry_price else 0,
                    "market_value": float(pos.market_value) if pos.market_value else 0,
                    "unrealized_pl": float(pos.unrealized_pl) if pos.unrealized_pl else 0,
                    "unrealized_plpc": float(pos.unrealized_plpc) if pos.unrealized_plpc else 0,
                })
            state_manager.update_positions(positions_data)

        except Exception as exc:
            logger.error("Failed to update account metrics: %s", exc)

        await asyncio.sleep(10)  # Update every 10 seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spawn the background market scanner and metrics updater on startup."""
    scanner_task = asyncio.create_task(run_market_scanner())
    metrics_task = asyncio.create_task(update_account_metrics())
    yield
    scanner_task.cancel()
    metrics_task.cancel()


app = FastAPI(title="RSI Webhook Trader", version="0.3.0", lifespan=lifespan)

# Configure CORS to allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

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


@app.get("/dashboard")
async def dashboard():
    """Return live JSON data for the dashboard."""
    return state_manager.get_dashboard_data()


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


if __name__ == "__main__":
    import uvicorn

    # Get port from environment variable with fallback to 8080
    port = int(os.getenv("PORT", "8080"))

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )
