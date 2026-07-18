import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import cross_sectional_monitor, market_analysis_monitor, position_monitor, strategy_evaluator, survival_monitor
from app.database import init_db
from app.exchange import reconcile_paper_state
from app.routers import data, webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    # Must run before the monitors: rebuilds paper cash/holdings from the
    # DB's open positions so a restart can't desync the simulator's ledger.
    await reconcile_paper_state()
    position_monitor.start()
    market_analysis_monitor.start()
    cross_sectional_monitor.start()
    strategy_evaluator.start()
    survival_monitor.start()
    yield
    await position_monitor.stop()
    await market_analysis_monitor.stop()
    await cross_sectional_monitor.stop()
    await strategy_evaluator.stop()
    await survival_monitor.stop()


app = FastAPI(title="GainzAI Crypto Trading System", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered before the static mount below so /webhook, /api, and /health
# always win — Starlette matches routes in registration order.
app.include_router(webhook.router)
app.include_router(data.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# The Docker build copies the built React dashboard into ./static (see
# backend/Dockerfile). In local dev without a build, this directory won't
# exist, so the API still runs fine on its own — the mount is skipped rather
# than crashing the app.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="dashboard")
    logger.info("Serving React dashboard from ./static")
else:
    logger.info("No ./static directory found — dashboard not served (API-only mode)")
