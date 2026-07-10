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
import json
import re
import uuid
from typing import Any, Dict, Optional, Protocol, Tuple

from loguru import logger

from app import market_data
from app.config import settings


def _normalize_cdp_credentials(api_key: str, api_secret: str) -> Tuple[str, str]:
    """Coinbase's CDP key-creation flow downloads a JSON file shaped like
    {"name": "...", "privateKey": "-----BEGIN EC PRIVATE KEY-----\\n...\\n-----END EC PRIVATE KEY-----\\n"}.
    Two copy-paste mistakes are common when moving that into env vars:
    pasting the *entire JSON file* into COINBASE_API_SECRET (or even
    COINBASE_API_KEY) instead of just the relevant field, and having a
    single-line env var editor flatten the PEM's real newlines into literal
    "\\n" text. Both are recovered here — JSON parsing correctly un-escapes
    "\\n" sequences for free, so this also fixes the plain flattened case."""
    for blob in (api_secret, api_key):
        candidate = blob.strip()
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        private_key = data.get("privateKey") or data.get("private_key")
        if private_key:
            api_key = data.get("name") or data.get("apiKey") or api_key
            api_secret = private_key
            break

    api_secret = api_secret.strip().strip('"').replace("\\n", "\n")
    return api_key.strip(), api_secret


def _diagnose_pem_issue(secret: str) -> Optional[str]:
    """Structural check that reports *why* a secret doesn't look like a
    valid PEM key, without ever echoing the key material itself — only the
    PEM boilerplate markers (which are constant, public text) and length/
    newline counts are safe to surface. Returns None if the shape looks
    fine (the underlying crypto library still does the real validation)."""
    if not secret:
        return "it's empty — COINBASE_API_SECRET has no value."
    if "\\n" in secret:
        return (
            f"it still contains a literal backslash-n after normalization "
            f"(length={len(secret)}) — it may be double-escaped (e.g. a "
            f"JSON string that was itself JSON-encoded again)."
        )
    if "-----BEGIN" not in secret:
        if secret.startswith("organizations/") or "/apiKeys/" in secret:
            return (
                f"it looks like the key *name/ID* (length={len(secret)}), not "
                f"the private key. Put this value in COINBASE_API_KEY and put "
                f"the \"privateKey\" field in COINBASE_API_SECRET."
            )
        if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", secret) and len(secret) <= 120:
            return (
                f"it looks like a base64 Ed25519 key (length={len(secret)}), not "
                f"a PEM key. This trading SDK only supports ECDSA keys — on "
                f"cloud.coinbase.com/access/api, create a new key and choose "
                f"signature algorithm ECDSA (not Ed25519), then use its "
                f"'-----BEGIN EC PRIVATE KEY-----' privateKey here."
            )
        return (
            f"it doesn't contain a '-----BEGIN' PEM header at all "
            f"(length={len(secret)}). Make sure you copied the "
            f"\"privateKey\" field's value, not \"name\" or something else."
        )
    if "-----END" not in secret:
        return f"it has a BEGIN marker but no END marker (length={len(secret)}) — it looks truncated."
    if secret.count("\n") < 2:
        return (
            f"it has BEGIN/END markers but no line breaks between them "
            f"(length={len(secret)}) — the key body must be on its own "
            f"line(s), separate from the markers."
        )
    return None


def _enrich_coinbase_error(exc: Exception, api_key: str = "") -> RuntimeError:
    """Coinbase's SDK raises a bare '401 Client Error: Unauthorized' whose
    HTTP body sometimes carries the actual reason. Pull that body out, and
    for a 401 append a concrete hint. An *empty-body* 401 specifically means
    the JWT signature was rejected at the edge (bad key-id or clock skew),
    not a permissions problem — and the single most common cause is
    COINBASE_API_KEY not being the full 'organizations/.../apiKeys/...'
    name, so call that out when the configured key doesn't match that shape."""
    message = str(exc)
    response = getattr(exc, "response", None)
    body = ""
    if response is not None:
        body = (getattr(response, "text", "") or "").strip()
        if body and body not in message:
            message = f"{message} — {body}"

    if "401" in message or "Unauthorized" in message:
        if api_key and "/apiKeys/" not in api_key:
            message += (
                " [COINBASE_API_KEY is '"
                + (api_key[:16] + "…" if len(api_key) > 16 else api_key)
                + "', which is NOT the full key name. It must be the entire "
                "'organizations/{org_id}/apiKeys/{key_id}' string from the "
                "downloaded key file — a truncated name makes Coinbase reject "
                "the request signature with this empty-body 401.]"
            )
        else:
            message += (
                " [Empty-body 401 = the request signature was rejected, not a "
                "permissions problem. Verify COINBASE_API_KEY is the full "
                "'organizations/.../apiKeys/...' name and COINBASE_API_SECRET "
                "is the matching privateKey from the SAME key file; if both are "
                "correct, the container clock may be skewed (Coinbase JWTs are "
                "only valid ~2 min). Re-download the key at "
                "cloud.coinbase.com/access/api if unsure.]"
            )
    return RuntimeError(message)


async def _enrich_coinbase_error_async(exc: Exception, api_key: str = "") -> RuntimeError:
    """Same as _enrich_coinbase_error, but for a 401 it also measures the
    real container-vs-Coinbase clock skew and states it outright — turning
    the 'maybe clock skew' guess into a definitive yes/no."""
    enriched = _enrich_coinbase_error(exc, api_key)
    message = str(enriched)
    if "401" in message or "Unauthorized" in message:
        skew = await market_data.fetch_clock_skew_seconds()
        if skew is not None:
            if abs(skew) > 30:
                message += (
                    f" [CONFIRMED: container clock is {skew:+.0f}s off Coinbase's "
                    f"server — this alone causes the 401. The host clock needs "
                    f"to be corrected/synced (NTP).]"
                )
            else:
                message += (
                    f" [Clock skew checked and fine ({skew:+.1f}s), so this is a "
                    f"credential mismatch — re-download the key and set BOTH "
                    f"COINBASE_API_KEY (name) and COINBASE_API_SECRET (privateKey) "
                    f"from that one file.]"
                )
    return RuntimeError(message)


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

        api_key, api_secret = _normalize_cdp_credentials(api_key, api_secret)
        issue = _diagnose_pem_issue(api_secret)
        if issue:
            raise ValueError(f"COINBASE_API_SECRET looks malformed: {issue}")
        self._api_key = api_key
        self._client = RESTClient(api_key=api_key, api_secret=api_secret)

    async def get_price(self, symbol: str) -> float:
        try:
            product = self._client.get_product(product_id=symbol)
        except Exception as exc:
            raise await _enrich_coinbase_error_async(exc, self._api_key) from exc
        return float(product["price"])

    async def get_usd_balance(self) -> float:
        """Spendable cash balance. Counts both USD and USDC — Coinbase treats
        them 1:1 for USD-quoted products and most retail funds actually sit as
        USDC — and requests up to 250 accounts so the cash balance isn't
        missed to pagination when the account holds many assets."""
        try:
            accounts = self._client.get_accounts(limit=250)
        except Exception as exc:
            raise await _enrich_coinbase_error_async(exc, self._api_key) from exc

        total = 0.0
        for account in accounts.get("accounts", []):
            if account.get("currency") in ("USD", "USDC"):
                try:
                    total += float(account["available_balance"]["value"])
                except (KeyError, TypeError, ValueError):
                    continue
        return total

    def _sell_size(self, symbol: str, requested: float) -> Tuple[str, float]:
        """Returns a Coinbase-valid base_size string for a SELL: capped at the
        actually-held balance and floored to the product's base_increment.
        Raises ValueError if the sellable amount is below the product minimum
        (a dust position that can't be closed)."""
        from decimal import Decimal, ROUND_DOWN

        base_currency = symbol.split("-")[0]
        available = 0.0
        for account in self._client.get_accounts(limit=250).get("accounts", []):
            if account.get("currency") == base_currency:
                try:
                    available = float(account["available_balance"]["value"])
                except (KeyError, TypeError, ValueError):
                    available = 0.0
                break

        sellable = min(float(requested), available) if available > 0 else float(requested)

        product = self._client.get_product(product_id=symbol)
        increment = Decimal(str(product.get("base_increment") or "0.00000001"))
        min_size = Decimal(str(product.get("base_min_size") or "0"))
        floored = Decimal(str(sellable)).quantize(increment, rounding=ROUND_DOWN)

        if floored <= 0 or floored < min_size:
            raise ValueError(
                f"{symbol}: sellable size {floored} (held {available}) is below the "
                f"exchange minimum {min_size} — position too small to close."
            )
        return format(floored, "f"), float(floored)

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
                # Floor to the product's increment and cap at the actually-held
                # balance — a too-precise or rounded-up size is rejected by
                # Coinbase, which would leave the position open and unsold.
                try:
                    sized_str, filled_size = self._sell_size(symbol, base_size)
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                result = self._client.market_order_sell(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    base_size=sized_str,
                )

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
            return {"success": False, "error": str(_enrich_coinbase_error(exc, self._api_key))}


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
