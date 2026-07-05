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

To wipe all mock signals/orders/positions and reset the simulated balance
back to a clean start, use the **Clear mock trades & holdings** button on the
dashboard's Settings tab (or `POST /api/reset` directly). It's a no-op that
returns an error in live mode, so real trade history is never at risk.

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
      data.py       GET /api/portfolio, /api/signals, /api/orders, /api/stats,
                    /api/positions/history, /api/config; POST /api/reset
    main.py         FastAPI app wiring
frontend/        React + Vite + Tailwind dashboard (Dashboard / Portfolio /
                 Signals / Risk Manager / Settings tabs, all live-data)
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
   `MARKET_ANALYSIS_POLL_INTERVAL_SECONDS` (default 15 minutes).

**Token efficiency.** LLM spend is engineered down three ways:

- **Gate** — symbols are scored by the free rule-based confluence check
  first; only setups with meaningful directional agreement reach Claude.
  In a choppy market a whole poll cycle costs zero LLM tokens.
- **Batch** — all gated candidates share **one** Claude call per cycle, so
  the instructions and the sentiment/news block are paid for once, not once
  per symbol (previously up to 15 calls per cycle).
- **Compress** — each candidate is a single dense TA line
  (`BTC-USD: price=64500 ATR=350 6h=bullish RSI=62 MACD=bullish/increasing …`)
  instead of a multi-line indicator dump, headlines are capped at 5, and
  reasoning is limited to one sentence. Dashboard reasoning polish runs only
  for trades that actually execute.

Net effect: a typical cycle drops from ~15 LLM calls (~10-15k tokens) to at
most one compact call (~1-2k tokens), and often none.
5. When this analysis supplies its own ATR-based stop-loss/take-profit for a
   position, `position_monitor.py` uses those exact price levels instead of
   the global `TAKE_PROFIT_PCT`/`STOP_LOSS_PCT` percentages.

This runs unconditionally — there's nothing to deploy separately and nothing
to authenticate against. Leave `ANTHROPIC_API_KEY` blank to run purely on the
rule-based fallback.

## Market sentiment & news

`app/sentiment.py` pulls two free, keyless sources on a cache
(`SENTIMENT_CACHE_MINUTES`, default 30):

- the **Crypto Fear & Greed Index** (alternative.me) — the market-regime gauge
- recent **crypto news headlines** (CoinDesk / Cointelegraph RSS)

Both are injected into the Claude analysis prompt so signals are weighed
against what's actually moving the market, and the Fear & Greed regime damps
entry sizes for *every* strategy (webhook and native): extreme fear or greed
cuts new-position size to 60%, elevated regimes to 80%. Sentiment never
generates or vetoes a trade — it moderates the bet. Current snapshot is at
`GET /api/sentiment`.

## Live web research (optional, on by default)

When `ANTHROPIC_API_KEY` is set and `ENABLE_WEB_RESEARCH=true` (the default),
the batched Claude call in `market_analysis.py` is given Anthropic's
server-side `web_search` tool. Claude is instructed to use it sparingly — at
most one search per cycle, only for candidates where the cached Fear & Greed
snapshot and RSS headlines might be missing something very recent (last 24h
exchange listings, hacks, regulatory action, major partnership/ETF news) —
before it commits to a BUY/SELL/HOLD call. This is on top of, not a
replacement for, the cached sentiment/news feed: the cache covers the general
regime cheaply, and the live search fills gaps only when the model decides
it's needed.

This is a real cost tradeoff against the token-efficiency work above: each
search is billed separately from token usage. Set `ENABLE_WEB_RESEARCH=false`
to run on cached sentiment/news alone with zero search cost.

## Risk framework (enforced, not aspirational)

- **Long-only spot** — SELL signals close existing positions; shorting is rejected.
- **One position per symbol, `MAX_OPEN_POSITIONS` symbols max** — repeated
  bullish signals can't stack exposure.
- **Per-trade cap** — `MAX_POSITION_PCT_OF_PORTFOLIO` of equity per entry.
- **Optional trading budget** — `TRADING_BUDGET_USD` caps how much of the
  connected account's real balance the system will ever deploy (e.g. `250`
  to trade with only $250 even if the account holds more). Every sizing
  calculation and the daily-loss ceiling run against
  `min(actual_balance, TRADING_BUDGET_USD)` once it's set; leave it at `0`
  (the default) to trade with the full real balance.
- **Daily loss circuit-breaker** — realized P&L is tracked per position; once
  today's realized losses cross `MAX_DAILY_LOSS_PCT` of portfolio value, all
  new entries are refused until the next UTC day.
- **Three exit paths** — fixed take-profit, protective stop-loss, and a
  **trailing stop** that arms once a position is up
  `TRAILING_STOP_ACTIVATION_PCT` and sells if price falls `TRAILING_STOP_PCT`
  from its peak, letting winners run while locking in most of the gain.
  Signal-supplied ATR-based levels take precedence over the global percentages.
- **Higher-timeframe confirmation** — native analysis checks the 6h trend;
  counter-trend entries need much stronger evidence and get lower confidence.
- **Realistic paper trading** — paper mode marks to Coinbase's real public
  ticker and keeps a holdings ledger, so paper results mean something before
  you risk a dollar.

## Deploying (Railway)

The bot runs two long-lived background loops (`position_monitor.py`,
`market_analysis_monitor.py`) inside the FastAPI process — it needs a host
that keeps one process running continuously, not a serverless/functions
platform that spins down between requests.

1. On [Railway](https://railway.app), create a new project from this GitHub repo.
2. Railway detects `railway.json` at the repo root, which points it at
   `backend/Dockerfile` — no other build config needed.
3. In the project's **Variables** tab, set every value from
   `backend/.env.example` (at minimum `WEBHOOK_SECRET`; add
   `COINBASE_API_KEY`/`COINBASE_API_SECRET` and flip
   `LIVE_TRADING_ENABLED=true` only once you're ready to go live).
4. Deploy. `backend/Dockerfile` is a two-stage build: it builds `frontend/`
   with Node, then copies the result into the Python image's `./static`
   directory, where FastAPI serves it at `/` (API routes at `/api`,
   `/webhook`, `/health` still take priority). One Railway service, one
   URL, no CORS or separate frontend deploy needed. **After the first
   deploy, go to the service's Settings → Networking → Generate Domain**
   — Railway never creates a public URL automatically. Use
   `https://<that-url>/webhook/tradingview` as the webhook URL in your
   TradingView alerts, and `https://<that-url>/` for the dashboard.
5. **Persistence matters for a trading bot.** The default SQLite file lives
   on the container's ephemeral disk, which can reset on redeploy. Either
   attach a Railway volume mounted where `trading.db` lives, or switch
   `DATABASE_URL` to a Railway Postgres add-on before running with real money.

## Going live on Coinbase

1. Create an API key at https://cloud.coinbase.com/access/api. When asked
   for the **signature algorithm, choose ECDSA, not Ed25519** — the bundled
   `coinbase-advanced-py` SDK only understands ECDSA keys (an Ed25519 key is
   a short base64 string with no `-----BEGIN` header and will be rejected).
   Grant **View + Trade** permissions only — explicitly leave
   **Transfer/Withdraw** unchecked, so a leaked key can never move funds out
   of the account, only trade within it. Add an IP allow-list restricted to
   your host's address if Coinbase offers one.
2. Set `COINBASE_API_KEY` (the key name) and `COINBASE_API_SECRET` (the
   private key Coinbase shows you once) as environment variables on your
   host — never commit them to the repo. Coinbase's CDP key creation flow
   gives you a downloadable JSON file shaped like
   `{"name": "...", "privateKey": "-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n"}`;
   the app accepts either the individual `name`/`privateKey` values or the
   *entire JSON file's contents* pasted into `COINBASE_API_KEY` /
   `COINBASE_API_SECRET` (in either field), and normalizes flattened `\n`
   sequences and wrapping quotes back into real PEM newlines automatically —
   so whichever way you copy it from the downloaded file should work. If you
   still see `Unable to load PEM file`, re-download the key from
   https://cloud.coinbase.com/access/api and try again.
3. Leave `LIVE_TRADING_ENABLED=false` first and watch `GET /api/signals` and
   `GET /api/orders` in paper mode until you trust the strategies' behavior.
4. When ready, set `LIVE_TRADING_ENABLED=true` and restart. `get_exchange()`
   in `app/exchange.py` will now route orders through `CoinbaseExchange`
   instead of the paper simulator, and every order/portfolio call touches
   your real Coinbase account.

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
