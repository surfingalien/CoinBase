import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import settings
from app.trading import process_signal

router = APIRouter(tags=["webhook"])


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if payload.get("webhook_secret") != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    for field in ("symbol", "action", "strategy"):
        if field not in payload:
            raise HTTPException(status_code=422, detail=f"Missing required field: {field}")

    signal_id = str(uuid.uuid4())
    background_tasks.add_task(process_signal, payload, signal_id)

    return {"status": "received", "signal_id": signal_id}
