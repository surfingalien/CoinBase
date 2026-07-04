# GainzAI — Autonomous Crypto Trading System

An end-to-end system that turns TradingView technical signals into risk-managed,
AI-reviewed trades on Coinbase, plus a live dashboard and a marketing landing page.

```
TradingView (Pine Script strategies)     FinSurfing (TradingView charts + Claude analysis)
        │  webhook (JSON + shared secret)         │  polled every N minutes
        ▼                                         ▼
              FastAPI backend
  ├─ ai_engine.py   → per-strategy confirmation logic + confidence score
  ├─ risk.py        → position sizing, per-trade cap, daily loss ceiling
  ├─ exchange.py     → MockExchange (paper) or CoinbaseExchange (live, opt-in)
  ├─ trading.py       → orchestrates signal → decision → order → position
  ├─ position_monitor.py → watches open positions, auto-sells at take-profit / stop-loss
  └─ finsurfing_monitor.py → polls FinSurfing's AI analysis as an extra signal source
        │
        ▼
SQLite (signals, orders, positions)
        │
        ▼
React dashboard (polls /api/*)
```

**Safety by default:** the system starts in paper-trading mode
(`MockExchange`) with a simulated balance. Live orders on Coinbase only fire
when you explicitly set `LIVE_TRADING_ENABLED=true` and provide real API
credentials. Nothing here promises profit — see "Risk" below.

## Repository layout

```
backend/         FastAPI app (paper + live trading engine)
  app/
    config.py      settings, allowed trading pairs, risk tiers
    models.py       SQLAlchemy models (Signal, Order, Position)
    exchange.py     MockExchange + CoinbaseExchange
    ai_engine.py    per-strategy signal confirmation + confidence scoring
    risk.py         position sizing and hard risk caps
    trading.py      the webhook → decision → order pipeline
    position_monitor.py  background loop: auto take-profit / stop-loss exits
    finsurfing_client.py   calls FinSurfing's AI analysis endpoint
    finsurfing_monitor.py  background loop: polls FinSurfing for every pair
    routers/
      webhook.py    POST /webhook/tradingview
      data.py       GET /api/portfolio, /api/signals, /api/orders, /api/stats
    main.py         FastAPI app wiring
frontend/        React + Vite + Tailwind dashboard
pinescript/      5 TradingView strategies (Pine Script v5)
website/         Standalone premium marketing landing page (index.html)
```

## Running the backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in secrets; leave LIVE_TRADING_ENABLED=false to stay in paper mode
uvicorn app.main:app --reload --port 8000
```

Send a test signal:

```bash
curl -X POST http://localhost:8000/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"webhook_secret":"change_me_to_a_long_random_string","symbol":"BTC-USD","action":"BUY","strategy":"GainzAlgo_V2_Alpha","price":64500,"rsi":42}'
```

Then check `GET /api/signals` and `GET /api/orders` to see it flow through the
AI engine and risk manager.

## Running the frontend

```bash
cd frontend
npm install
npm run dev
```

Opens on `http://localhost:5173`, proxying `/api` and `/webhook` to the
backend on port 8000.

## Connecting TradingView

1. Open each script in `pinescript/` in TradingView's Pine Editor and add it
   to the chart for the pair you want to trade (see `ALLOWED_PAIRS` in
   `backend/app/config.py` for the supported 15-pair universe).
2. Set the script's `Webhook Secret` input to match `WEBHOOK_SECRET` in your
   backend `.env`.
3. Create a TradingView alert on the strategy, and set the webhook URL to
   `https://<your-domain>/webhook/tradingview`.
4. Repeat per pair/timeframe. The backend accepts alerts from all five
   strategies concurrently — `ai_engine.py` routes each by its `strategy` field.

## Connecting FinSurfing (optional second signal source)

[FinSurfing](https://github.com/surfingalien/finsurfing) is a separate app
that embeds TradingView's own chart widget and runs a Claude-based technical
analysis engine (RSI/MACD/EMA/Bollinger/ATR/ADX/support-resistance/pattern
detection) to produce a structured BUY/SELL/HOLD signal with its own
confidence, stop-loss, and take-profit. GainzAI can poll it as an additional,
independent signal source alongside (not instead of) the TradingView
webhooks — useful since it reasons over live charts on a schedule rather than
waiting for a Pine Script condition to fire.

1. Deploy FinSurfing (it runs as its own Node/Express app) and note its public URL.
2. Generate an API token: FinSurfing's `/api/trading-analysis/analyze` route
   requires a JWT signed with FinSurfing's `JWT_SECRET`. The simplest path is
   to log in to FinSurfing normally and copy your access token; for a
   long-running bot you'll want a dedicated service-account token instead so
   it doesn't expire with your session.
3. In `backend/.env`, set `FINSURFING_BASE_URL` (FinSurfing's URL) and
   `FINSURFING_API_TOKEN` (the JWT from step 2). Leave both blank to disable
   this integration entirely — everything else works the same without it.
4. Restart the backend. `finsurfing_monitor.py` will poll every pair in
   `ALLOWED_PAIRS` every `FINSURFING_POLL_INTERVAL_SECONDS` (default 15
   minutes — each call triggers an LLM analysis on FinSurfing's side, so
   don't set this too aggressively), feeding BUY/SELL calls through the same
   `ai_engine.py` → `risk.py` → `exchange.py` pipeline as everything else,
   tagged with `strategy: "FinSurfing_AI"`.
5. When FinSurfing supplies its own stop-loss/take-profit for a position,
   `position_monitor.py` uses those exact price levels instead of the global
   `TAKE_PROFIT_PCT`/`STOP_LOSS_PCT` percentages — FinSurfing's levels are
   ATR-based per symbol, which is more precise than one fixed percentage
   applied to every asset.

## Going live on Coinbase

1. Create an Advanced Trade API key at
   https://www.coinbase.com/settings/api with trade permissions.
2. Set `COINBASE_API_KEY`, `COINBASE_API_SECRET`, and
   `LIVE_TRADING_ENABLED=true` in `backend/.env`.
3. Restart the backend. `get_exchange()` in `app/exchange.py` will now route
   orders through `CoinbaseExchange` instead of the paper simulator, and every
   order/portfolio call touches your real Coinbase account.

**Do this only after you've reviewed the signal history in paper mode** and
are comfortable with the strategies' behavior.

## Automatic profit booking & stop-loss

Placing an entry order is only half the job — `app/position_monitor.py` runs
as a background task from the moment the backend starts, polling every open
position every `POSITION_MONITOR_INTERVAL_SECONDS` (default 30s) and selling
automatically the instant unrealized P&L crosses either threshold:

- `TAKE_PROFIT_PCT` (default 8%) — books the win
- `STOP_LOSS_PCT` (default 4%) — cuts the loss

This runs against whichever exchange is active (paper `MockExchange` or live
`CoinbaseExchange`), so you can watch full entry-to-exit cycles play out in
the dashboard before ever setting `LIVE_TRADING_ENABLED=true`.

## Risk

This is a trading tool, not a promise of profit. Cryptocurrency markets are
volatile, and automated strategies can lose money — including in ways
backtests don't anticipate. The built-in risk controls
(`MAX_POSITION_PCT_OF_PORTFOLIO`, `MAX_DAILY_LOSS_PCT` in `.env`) bound how
much a single trade or a single day can lose, but they do not eliminate
market risk. You retain full custody of funds through your own Coinbase
account at all times; this system never pools or custodies user funds.

## Marketing site

`website/index.html` is a self-contained, dependency-free landing page
(open it directly in a browser) built around the actual pipeline and risk
controls described above — no unrealistic return claims, since the audience
for a trading product deserves the same rigor as its code.
