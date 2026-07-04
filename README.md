# GainzAI — Autonomous Crypto Trading System

An end-to-end system that turns TradingView technical signals into risk-managed,
AI-reviewed trades on Coinbase, plus a live dashboard and a marketing landing page.

```
TradingView (Pine Script strategies)     Coinbase public candles (native technical + AI analysis)
        │  webhook (JSON + shared secret)         │  polled every N minutes
        ▼                                         ▼
              FastAPI backend
  ├─ ai_engine.py   → per-strategy confirmation logic + confidence score
  ├─ risk.py        → position sizing, per-trade cap, daily loss ceiling
  ├─ exchange.py     → MockExchange (paper) or CoinbaseExchange (live, opt-in)
  ├─ trading.py       → orchestrates signal → decision → order → position
  ├─ position_monitor.py → watches open positions, auto-sells at take-profit / stop-loss
  ├─ technical_indicators.py → RSI/MACD/EMA/BB/ATR/ADX/S&R/pattern math
  ├─ market_data.py → fetches OHLCV candles from Coinbase's public API
  ├─ market_analysis.py → turns indicators into a signal (Claude, or rule-based fallback)
  └─ market_analysis_monitor.py → polls market_analysis.py for every pair
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
    technical_indicators.py  RSI/MACD/EMA/BB/ATR/ADX/support-resistance/pattern math
    market_data.py         fetches OHLCV candles from Coinbase's public API
    market_analysis.py     turns indicators into a signal (Claude, or rule-based fallback)
    market_analysis_monitor.py  background loop: analyzes every pair on a schedule
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

## Native technical + AI analysis (optional second signal source)

Alongside the TradingView webhooks, GainzAI runs its own analysis loop —
`market_analysis_monitor.py` — that needs no external app and no API token:

1. `market_data.py` pulls OHLCV candles straight from Coinbase's public
   market data endpoint (no auth required) for every pair in `ALLOWED_PAIRS`.
2. `technical_indicators.py` computes RSI, MACD, EMAs, Bollinger Bands, ATR,
   StochRSI, VWAP, OBV, support/resistance, ADX, and candlestick/breakout
   patterns from those candles — the same style of technical analysis a
   TradingView chart would show.
3. `market_analysis.py` turns that into a BUY/SELL/HOLD call. If
   `ANTHROPIC_API_KEY` is set, it asks Claude to weigh the indicators and
   write the reasoning; if not, it falls back to a pure rule-based confluence
   score (counts bullish vs. bearish indicators, requires several to agree).
   Either way it works out of the box — the Claude key only upgrades the
   reasoning quality, it isn't required.
4. The resulting signals feed through the same `ai_engine.py` → `risk.py` →
   `exchange.py` pipeline as the Pine Script webhooks, tagged with
   `strategy: "Native_TA_AI"`, on a schedule set by
   `MARKET_ANALYSIS_POLL_INTERVAL_SECONDS` (default 15 minutes — each Claude
   call costs money, so don't set this too aggressively if a key is configured).
5. When this analysis supplies its own ATR-based stop-loss/take-profit for a
   position, `position_monitor.py` uses those exact price levels instead of
   the global `TAKE_PROFIT_PCT`/`STOP_LOSS_PCT` percentages.

This runs unconditionally — there's nothing to deploy separately and nothing
to authenticate against. Leave `ANTHROPIC_API_KEY` blank to run purely on the
rule-based fallback.

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
