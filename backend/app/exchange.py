"""Exchange abstraction.

Two implementations share the same interface:

- MockExchange: an in-memory paper-trading simulator. Used by default so the
  whole pipeline (webhook -> AI -> order -> position) can be exercised safely
  with zero risk and no API keys.
- CoinbaseExchange: places real orders through Coinbase's Advanced Trade API.
  Only used when LIVE_TRADING_ENABLED=true and valid API credentials are set.

get_exchange() returns whichever one is configured, so the rest of the app
never needs to know which one it's talking to.
"""
import uuid
from typing import Any, Dict, Protocol

from loguru import logger

from app.config import settings


class Exchange(Protocol):
    is_live: bool

    async def get_price(self, symbol: str) -> float: ...

    async def place_market_order(self, symbol: str, side: str, quote_size: float) -> Dict[str, Any]: ...

    async def get_usd_balance(self) -> float: ...


class MockExchange:
    """Paper-trading simulator. Never touches a real account."""

    is_live = False

    def __init__(self) -> None:
        self.prices = {
            "BTC-USD": 64500.0, "ETH-USD": 3250.0, "SOL-USD": 145.0,
            "AVAX-USD": 35.0, "LINK-USD": 15.0, "MATIC-USD": 0.85,
            "DOT-USD": 7.2, "ATOM-USD": 8.5, "LTC-USD": 85.0,
            "ADA-USD": 0.45, "UNI-USD": 11.0, "ARB-USD": 1.1,
            "OP-USD": 2.2, "NEAR-USD": 5.5, "INJ-USD": 25.0,
        }
        self.usd_balance = 25000.0

    async def get_price(self, symbol: str) -> float:
        return self.prices.get(symbol, 100.0)

    async def get_usd_balance(self) -> float:
        return self.usd_balance

    async def place_market_order(self, symbol: str, side: str, quote_size: float) -> Dict[str, Any]:
        price = await self.get_price(symbol)
        filled_size = quote_size / price
        logger.info(f"[PAPER TRADE] {side} {symbol} for ${quote_size:.2f} @ ${price:,.2f}")

        if side == "BUY":
            self.usd_balance -= quote_size
        else:
            self.usd_balance += quote_size

        return {
            "success": True,
            "order_id": str(uuid.uuid4()),
            "filled_size": filled_size,
            "avg_price": price,
        }


class CoinbaseExchange:
    """Places real orders via Coinbase's Advanced Trade API.

    Requires the `coinbase-advanced-py` package and a CDP/Advanced Trade API
    key + secret with trade permissions. Only ever instantiated when
    LIVE_TRADING_ENABLED=true — this is the one code path that moves real
    money, so it fails loudly rather than silently falling back to paper mode.
    """

    is_live = True

    def __init__(self, api_key: str, api_secret: str) -> None:
        from coinbase.rest import RESTClient

        self._client = RESTClient(api_key=api_key, api_secret=api_secret)

    async def get_price(self, symbol: str) -> float:
        product = self._client.get_product(product_id=symbol)
        return float(product["price"])

    async def get_usd_balance(self) -> float:
        accounts = self._client.get_accounts()
        for account in accounts.get("accounts", []):
            if account.get("currency") == "USD":
                return float(account["available_balance"]["value"])
        return 0.0

    async def place_market_order(self, symbol: str, side: str, quote_size: float) -> Dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        try:
            if side == "BUY":
                result = self._client.market_order_buy(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    quote_size=str(round(quote_size, 2)),
                )
            else:
                base_size = quote_size / await self.get_price(symbol)
                result = self._client.market_order_sell(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    base_size=str(round(base_size, 8)),
                )

            success = result.get("success", False)
            if not success:
                logger.error(f"[LIVE TRADE] Order failed: {result}")
                return {"success": False, "error": result.get("error_response")}

            order = result.get("success_response", {})
            price = await self.get_price(symbol)
            filled_size = quote_size / price
            logger.warning(f"[LIVE TRADE] {side} {symbol} for ${quote_size:.2f} @ ~${price:,.2f}")
            return {
                "success": True,
                "order_id": order.get("order_id", client_order_id),
                "filled_size": filled_size,
                "avg_price": price,
            }
        except Exception as exc:
            logger.exception("Coinbase order placement failed")
            return {"success": False, "error": str(exc)}


_exchange_instance: Exchange | None = None


def get_exchange() -> Exchange:
    global _exchange_instance
    if _exchange_instance is not None:
        return _exchange_instance

    if settings.live_trading_enabled:
        if not settings.coinbase_api_key or not settings.coinbase_api_secret:
            raise RuntimeError(
                "LIVE_TRADING_ENABLED is true but COINBASE_API_KEY / "
                "COINBASE_API_SECRET are not set."
            )
        logger.warning("LIVE TRADING ENABLED — orders will be placed on real Coinbase account.")
        _exchange_instance = CoinbaseExchange(settings.coinbase_api_key, settings.coinbase_api_secret)
    else:
        logger.info("Running in PAPER TRADING mode (MockExchange). Set LIVE_TRADING_ENABLED=true to go live.")
        _exchange_instance = MockExchange()

    return _exchange_instance
