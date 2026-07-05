"""Exchange abstraction.

Two implementations share the same interface:

- MockExchange: a paper-trading simulator that marks to *real* market prices
  (Coinbase's public ticker, no auth) and tracks per-symbol holdings, so
  paper results behave like the live market instead of a frozen price sheet.
  Used by default so the whole pipeline can be exercised with zero risk.
- CoinbaseExchange: places real orders through Coinbase's Advanced Trade API.
  Only used when LIVE_TRADING_ENABLED=true and valid API credentials are set.

Orders are expressed either as quote_size (USD to spend — used for buys) or
base_size (units of the asset to sell — used for exits, so a close always
sells exactly what the position holds).
"""
import uuid
from typing import Any, Dict, Optional, Protocol

from loguru import logger

from app import market_data
from app.config import settings


class Exchange(Protocol):
    is_live: bool

    async def get_price(self, symbol: str) -> float: ...

    async def place_market_order(
        self, symbol: str, side: str,
        quote_size: Optional[float] = None, base_size: Optional[float] = None,
    ) -> Dict[str, Any]: ...

    async def get_usd_balance(self) -> float: ...


# Only used when Coinbase's public ticker is unreachable (e.g. offline dev).
_FALLBACK_PRICES = {
    "BTC-USD": 64500.0, "ETH-USD": 3250.0, "SOL-USD": 145.0,
    "AVAX-USD": 35.0, "LINK-USD": 15.0, "MATIC-USD": 0.85,
    "DOT-USD": 7.2, "ATOM-USD": 8.5, "LTC-USD": 85.0,
    "ADA-USD": 0.45, "UNI-USD": 11.0, "ARB-USD": 1.1,
    "OP-USD": 2.2, "NEAR-USD": 5.5, "INJ-USD": 25.0,
}


class MockExchange:
    """Paper-trading simulator. Never touches a real account, but marks to
    real market prices so take-profit/stop-loss and P&L behave realistically."""

    is_live = False
    STARTING_BALANCE_USD = 25000.0

    def __init__(self) -> None:
        self.usd_balance = self.STARTING_BALANCE_USD
        # Best-effort holdings ledger. In-memory only: after a restart the
        # database still knows the open positions, so exits are always
        # honoured even if this ledger has been reset.
        self.holdings: Dict[str, float] = {}
        self._last_price: Dict[str, float] = dict(_FALLBACK_PRICES)

    def reset(self) -> None:
        """Wipes paper-trading state back to a fresh starting balance —
        pairs with clearing the DB's signals/orders/positions so the
        dashboard and the simulator agree on a clean slate."""
        self.usd_balance = self.STARTING_BALANCE_USD
        self.holdings.clear()

    async def get_price(self, symbol: str) -> float:
        live = await market_data.fetch_last_price(symbol)
        if live is not None:
            self._last_price[symbol] = live
            return live
        return self._last_price.get(symbol, 100.0)

    async def get_usd_balance(self) -> float:
        return self.usd_balance

    async def place_market_order(
        self, symbol: str, side: str,
        quote_size: Optional[float] = None, base_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        price = await self.get_price(symbol)

        if side == "BUY":
            if not quote_size:
                return {"success": False, "error": "BUY requires quote_size"}
            if quote_size > self.usd_balance:
                return {"success": False, "error": "Insufficient paper USD balance"}
            filled_size = quote_size / price
            self.usd_balance -= quote_size
            self.holdings[symbol] = self.holdings.get(symbol, 0.0) + filled_size
        else:
            if base_size is None and quote_size:
                base_size = quote_size / price
            if not base_size:
                return {"success": False, "error": "SELL requires base_size"}
            held = self.holdings.get(symbol, 0.0)
            if held + 1e-12 < base_size:
                # A restart can reset the in-memory ledger while the DB still
                # holds the position — honour the exit rather than stranding it.
                logger.warning(f"[PAPER TRADE] Holdings ledger shows {held} {symbol} but selling {base_size}")
            filled_size = base_size
            self.holdings[symbol] = max(0.0, held - base_size)
            self.usd_balance += base_size * price

        logger.info(f"[PAPER TRADE] {side} {symbol} {filled_size:.6f} units @ ${price:,.2f}")
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

    async def place_market_order(
        self, symbol: str, side: str,
        quote_size: Optional[float] = None, base_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        try:
            price = await self.get_price(symbol)

            if side == "BUY":
                if not quote_size:
                    return {"success": False, "error": "BUY requires quote_size"}
                result = self._client.market_order_buy(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    quote_size=str(round(quote_size, 2)),
                )
                filled_size = quote_size / price
            else:
                if base_size is None and quote_size:
                    base_size = quote_size / price
                if not base_size:
                    return {"success": False, "error": "SELL requires base_size"}
                result = self._client.market_order_sell(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    base_size=str(round(base_size, 8)),
                )
                filled_size = base_size

            if not result.get("success", False):
                logger.error(f"[LIVE TRADE] Order failed: {result}")
                return {"success": False, "error": result.get("error_response")}

            order = result.get("success_response", {})
            logger.warning(f"[LIVE TRADE] {side} {symbol} {filled_size:.6f} units @ ~${price:,.2f}")
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
        logger.info("Running in PAPER TRADING mode (MockExchange, live market prices). Set LIVE_TRADING_ENABLED=true to go live.")
        _exchange_instance = MockExchange()

    return _exchange_instance
