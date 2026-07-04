from typing import Any, Dict, Optional

from pydantic import BaseModel


class TradingViewAlert(BaseModel):
    webhook_secret: str
    strategy: str
    symbol: str
    action: str  # BUY, SELL, CLOSE, HOLD
    price: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    volume_ratio: Optional[float] = None

    model_config = {"extra": "allow"}

    def as_dict(self) -> Dict[str, Any]:
        return self.model_dump()
