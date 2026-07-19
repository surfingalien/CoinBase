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
import time
import uuid
from typing import Any, Dict, List, Optional, Protocol, Tuple

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

    async def get_account_value(self) -> float: ...

    async def get_recent_buy_fills(self, symbol: str) -> List[Dict[str, float]]: ...


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
    real market prices so take-profit/stop-loss and P&L behave realistically,
    and charges PAPER_FEE_PCT per fill so paper cash tracks what live
    trading would net."""

    is_live = False
    STARTING_BALANCE_USD = 25000.0

    def __init__(self) -> None:
        self.usd_balance = self.STARTING_BALANCE_USD
        # Holdings ledger. In-memory, so it dies on restart while the DB's
        # open positions survive — restore_state() rebuilds it from those
        # positions at startup so cash + holdings stay consistent with the DB.
        self.holdings: Dict[str, float] = {}
        self._last_price: Dict[str, float] = dict(_FALLBACK_PRICES)

    def reset(self) -> None:
        """Wipes paper-trading state back to a fresh starting balance —
        pairs with clearing the DB's signals/orders/positions so the
        dashboard and the simulator agree on a clean slate."""
        self.usd_balance = self.STARTING_BALANCE_USD
        self.holdings.clear()

    def restore_state(self, open_positions) -> None:
        """Rebuilds the in-memory ledger from the DB's open positions after a
        restart: re-adds each position's coins to holdings and re-deducts its
        entry cost from the fresh starting balance. Without this, a restart
        hands the paper account its $25k back while the DB still shows the
        positions — and closing them later credits cash that was never spent.
        """
        self.usd_balance = self.STARTING_BALANCE_USD
        self.holdings.clear()
        for position in open_positions:
            cost = position.entry_price * position.size + (position.entry_fees_usd or 0.0)
            self.holdings[position.symbol] = self.holdings.get(position.symbol, 0.0) + position.size
            self.usd_balance -= cost
        if self.usd_balance < 0:
            # Open cost basis exceeds the starting bankroll (positions opened
            # under an older, larger balance). Floor at zero: better to block
            # new buys than to trade on negative cash.
            logger.warning(
                f"[PAPER] Restored open positions cost more than the starting balance "
                f"(shortfall ${-self.usd_balance:,.2f}); flooring cash at $0."
            )
            self.usd_balance = 0.0
        if self.holdings:
            logger.info(
                f"[PAPER] Restored {len(self.holdings)} holding(s) from DB positions; "
                f"cash ${self.usd_balance:,.2f}"
            )

    async def get_price(self, symbol: str) -> float:
        live = await market_data.fetch_last_price(symbol)
        if live is not None:
            self._last_price[symbol] = live
            return live
        return self._last_price.get(symbol, 100.0)

    async def get_usd_balance(self) -> float:
        return self.usd_balance

    async def get_account_value(self) -> float:
        """Paper account NAV: cash + every held asset marked to market."""
        total = self.usd_balance
        for symbol, amount in self.holdings.items():
            if amount > 0:
                total += amount * await self.get_price(symbol)
        return total

    async def get_recent_buy_fills(self, symbol: str) -> List[Dict[str, float]]:
        """The paper simulator keeps no fill history — synced positions fall
        back to the sync-moment price as their basis."""
        return []

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
            # Fee comes out of the quote amount, like a real quote-sized
            # market buy: spend $X, receive $(X - fee) worth of coin. With
            # maker entries enabled, entries pay the maker tier instead —
            # so paper P&L tracks the live economics of that setting too.
            entry_fee_pct = settings.maker_fee_pct if settings.maker_entries_enabled else settings.paper_fee_pct
            fees_usd = quote_size * entry_fee_pct
            filled_size = (quote_size - fees_usd) / price
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
            gross = base_size * price
            fees_usd = gross * settings.paper_fee_pct
            self.holdings[symbol] = max(0.0, held - base_size)
            self.usd_balance += gross - fees_usd

        logger.info(
            f"[PAPER TRADE] {side} {symbol} {filled_size:.6f} units @ ${price:,.2f} "
            f"(fee ${fees_usd:.2f})"
        )
        return {
            "success": True,
            "order_id": str(uuid.uuid4()),
            "filled_size": filled_size,
            "avg_price": price,
            "fees_usd": fees_usd,
        }


def combine_fills(fills: List[Dict[str, float]]) -> Tuple[float, float, float]:
    """Merges partial fills (e.g. a partly-filled maker order plus its market
    fallback) into (total_size, size-weighted avg price, total fees)."""
    size = sum(f["size"] for f in fills)
    fees = sum(f["fees"] for f in fills)
    avg_price = sum(f["size"] * f["price"] for f in fills) / size if size > 0 else 0.0
    return size, avg_price, fees


def cost_basis_from_fills(held_size: float, buy_fills_newest_first: List[Dict[str, float]],
                          market_price: float) -> Tuple[float, str]:
    """Reconstructs the cost basis of currently-held coins from BUY fill
    history, so a synced position's P&L measures from what was actually paid
    rather than from the sync moment.

    Walks the newest BUY fills backwards until the held size is covered (the
    coins you still hold are, to a first approximation, the ones you bought
    most recently — older buys were consumed by intervening sells). Fees are
    folded into the basis pro-rata, matching how Coinbase reports cost basis.
    Held coins not covered by visible fills (transfers in, history beyond the
    API window) are valued at the current market price — the only honest
    baseline for coins with no visible purchase record.

    Returns (entry_price, basis_source): 'fills' when fills cover ≥99% of the
    held size, 'fills_partial' when partially covered, 'sync_price' when no
    usable fills exist.
    """
    if held_size <= 0 or not buy_fills_newest_first:
        return market_price, "sync_price"

    remaining = held_size
    covered = 0.0
    cost = 0.0
    for fill in buy_fills_newest_first:
        if remaining <= 0:
            break
        size, price = fill.get("size", 0.0), fill.get("price", 0.0)
        if size <= 0 or price <= 0:
            continue
        take = min(remaining, size)
        cost += take * price + (take / size) * fill.get("fees", 0.0)
        covered += take
        remaining -= take

    if covered <= 0:
        return market_price, "sync_price"
    cost += (held_size - covered) * market_price
    source = "fills" if covered >= held_size * 0.99 else "fills_partial"
    return cost / held_size, source


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

    async def get_account_value(self) -> float:
        """True account NAV in USD: cash (USD+USDC) plus every crypto balance
        marked to its current price — so the dashboard total matches the real
        Coinbase account, independent of which holdings are synced/tracked.
        Assets with no -USD market (or an unfetchable price) are skipped."""
        try:
            accounts = self._client.get_accounts(limit=250)
        except Exception as exc:
            raise await _enrich_coinbase_error_async(exc, self._api_key) from exc

        total = 0.0
        for account in accounts.get("accounts", []):
            currency = account.get("currency")
            try:
                amount = float(account["available_balance"]["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            if currency in ("USD", "USDC"):
                total += amount
            else:
                try:
                    total += amount * await self.get_price(f"{currency}-USD")
                except Exception:
                    continue  # no USD market for this asset; skip
        return total

    async def get_recent_buy_fills(self, symbol: str, max_fills: int = 1000) -> List[Dict[str, float]]:
        """Newest-first BUY fills for a product, normalized to
        [{'size': base_units, 'price': ..., 'fees': usd}] — the raw material
        for reconstructing a synced position's true cost basis. Coinbase
        returns fills most-recent-first; entries priced in quote units
        (size_in_quote) are converted to base units. Paginates via the fills
        cursor up to max_fills, since holdings accumulated through many small
        buys (DCA, dust-sized fills) need deeper history than one page for
        full basis coverage."""
        fills: List[Dict[str, float]] = []
        cursor: Optional[str] = None
        while len(fills) < max_fills:
            try:
                kwargs: Dict[str, Any] = {"product_id": symbol, "limit": 250}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self._client.get_fills(**kwargs)
            except Exception as exc:
                raise await _enrich_coinbase_error_async(exc, self._api_key) from exc

            page = response.get("fills", []) or []
            for fill in page:
                if str(fill.get("side", "")).upper() != "BUY":
                    continue
                try:
                    price = float(fill.get("price") or 0)
                    size = float(fill.get("size") or 0)
                    fees = float(fill.get("commission") or 0)
                except (TypeError, ValueError):
                    continue
                if str(fill.get("size_in_quote", "")).lower() == "true" or fill.get("size_in_quote") is True:
                    size = size / price if price > 0 else 0.0
                if size > 0 and price > 0:
                    fills.append({"size": size, "price": price, "fees": fees})

            cursor = response.get("cursor") or None
            if not cursor or not page:
                break
        return fills

    async def _fetch_actual_fill(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Polls the placed order until Coinbase reports its fill, and returns
        the ACTUAL filled size, average fill price, and fees charged. Market
        orders normally fill within a second; if the order still isn't filled
        after the polling window, returns None and the caller keeps its
        pre-order estimate. This is what keeps the DB's idea of a position in
        lockstep with the coins that actually landed in the account — the
        ticker-price estimate ignores both slippage and the taker fee, so it
        overstates every buy by the fee percentage."""
        import asyncio

        for attempt in range(5):
            if attempt:
                await asyncio.sleep(0.5)
            try:
                order = self._client.get_order(order_id=order_id).get("order") or {}
            except Exception:
                logger.warning(f"Could not fetch order {order_id} for fill details (attempt {attempt + 1})")
                continue
            filled_size = float(order.get("filled_size") or 0)
            if order.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED") or filled_size > 0:
                if filled_size <= 0:
                    return None
                return {
                    "filled_size": filled_size,
                    "avg_price": float(order.get("average_filled_price") or 0) or None,
                    "fees_usd": float(order.get("total_fees") or 0),
                }
        return None

    async def _maker_buy(self, symbol: str, quote_size: float, ticker_price: float) -> Dict[str, Any]:
        """Entry at the maker fee tier: post-only limit at the best bid,
        polled until MAKER_FILL_TIMEOUT_SECONDS, then cancelled — and whatever
        remains unfilled is bought at market so the entry the risk engine
        sized is the entry that actually opens. Raises on total failure; the
        caller falls back to a plain market order."""
        import asyncio
        from decimal import Decimal, ROUND_DOWN

        product = self._client.get_product(product_id=symbol)
        base_inc = Decimal(str(product.get("base_increment") or "0.00000001"))
        quote_inc = Decimal(str(product.get("quote_increment") or "0.01"))

        bid: Optional[float] = None
        try:
            book = self._client.get_best_bid_ask(product_ids=[symbol])
            pricebooks = book.get("pricebooks") or []
            bids = (pricebooks[0].get("bids") or []) if pricebooks else []
            bid = float(bids[0]["price"]) if bids else None
        except Exception:
            bid = None  # book unavailable — a hair under ticker keeps post-only valid
        limit_price = Decimal(str(bid if bid else ticker_price * 0.9995)).quantize(quote_inc, rounding=ROUND_DOWN)
        base_size = (Decimal(str(quote_size)) / limit_price).quantize(base_inc, rounding=ROUND_DOWN)
        if base_size <= 0:
            raise ValueError(f"{symbol}: quote size {quote_size} is below one base increment at {limit_price}")

        client_order_id = str(uuid.uuid4())
        result = self._client.limit_order_gtc_buy(
            client_order_id=client_order_id,
            product_id=symbol,
            base_size=format(base_size, "f"),
            limit_price=format(limit_price, "f"),
            post_only=True,
        )
        if not result.get("success", False):
            raise RuntimeError(f"post-only limit rejected: {result.get('error_response')}")
        order_id = result.get("success_response", {}).get("order_id", client_order_id)

        filled: Dict[str, Any] = {}
        deadline = time.monotonic() + settings.maker_fill_timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            order = self._client.get_order(order_id=order_id).get("order") or {}
            if order.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                filled = order
                break
        if filled.get("status") != "FILLED":
            try:
                self._client.cancel_orders(order_ids=[order_id])
            except Exception:
                logger.warning(f"Could not cancel maker order {order_id}; proceeding on its last known state")
            filled = self._client.get_order(order_id=order_id).get("order") or {}

        fills: List[Dict[str, float]] = []
        maker_size = float(filled.get("filled_size") or 0)
        if maker_size > 0:
            fills.append({
                "size": maker_size,
                "price": float(filled.get("average_filled_price") or 0) or float(limit_price),
                "fees": float(filled.get("total_fees") or 0),
            })

        # Top up at market so a stale bid can't quietly halve the position.
        filled_value = sum(f["size"] * f["price"] for f in fills)
        remaining_quote = quote_size - filled_value
        if remaining_quote >= 1.0:
            market = self._client.market_order_buy(
                client_order_id=str(uuid.uuid4()),
                product_id=symbol,
                quote_size=str(round(remaining_quote, 2)),
            )
            if market.get("success", False):
                market_id = market.get("success_response", {}).get("order_id")
                market_fill = await self._fetch_actual_fill(market_id) if market_id else None
                if market_fill:
                    fills.append({
                        "size": market_fill["filled_size"],
                        "price": market_fill["avg_price"] or ticker_price,
                        "fees": market_fill["fees_usd"],
                    })
                else:
                    fills.append({"size": remaining_quote / ticker_price, "price": ticker_price, "fees": 0.0})
            elif not fills:
                raise RuntimeError(f"maker unfilled and market fallback rejected: {market.get('error_response')}")

        size, avg_price, fees_usd = combine_fills(fills)
        if size <= 0:
            raise RuntimeError("maker entry produced no fill")
        logger.warning(
            f"[LIVE TRADE] BUY {symbol} {size:.6f} units @ ${avg_price:,.2f} "
            f"(maker entry{' + market top-up' if len(fills) > 1 else ''}, fees ${fees_usd:.2f})"
        )
        return {"success": True, "order_id": order_id, "filled_size": size,
                "avg_price": avg_price, "fees_usd": fees_usd}

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
                if settings.maker_entries_enabled:
                    try:
                        return await self._maker_buy(symbol, quote_size, price)
                    except Exception:
                        logger.exception(f"Maker entry for {symbol} failed; falling back to market order")
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
            order_id = order.get("order_id", client_order_id)

            # Replace the ticker-price estimate with the real fill. The
            # estimate ignores slippage and the taker fee, so recording it
            # would overstate the position vs. the coins actually received —
            # the drift the reconcile endpoint exists to catch.
            avg_price, fees_usd = price, 0.0
            fill = await self._fetch_actual_fill(order_id)
            if fill:
                filled_size = fill["filled_size"]
                avg_price = fill["avg_price"] or price
                fees_usd = fill["fees_usd"]
            else:
                logger.warning(
                    f"[LIVE TRADE] Could not confirm fill for {order_id}; "
                    f"recording pre-order estimate (size may overstate by the fee)."
                )

            logger.warning(
                f"[LIVE TRADE] {side} {symbol} {filled_size:.6f} units @ ${avg_price:,.2f} "
                f"(fees ${fees_usd:.2f})"
            )
            return {
                "success": True,
                "order_id": order_id,
                "filled_size": filled_size,
                "avg_price": avg_price,
                "fees_usd": fees_usd,
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


async def reconcile_paper_state() -> None:
    """Startup hook: rebuild the paper exchange's cash/holdings from the DB's
    open positions, so a restart doesn't reset paper cash to $25k while the
    positions live on (which also let later closes credit unspent cash).
    No-op in live mode — the real account is its own source of truth."""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Position

    exchange = get_exchange()
    if not isinstance(exchange, MockExchange):
        return

    async with async_session() as session:
        open_positions = (await session.execute(
            select(Position).where(Position.status == "open")
        )).scalars().all()
    if open_positions:
        exchange.restore_state(open_positions)
