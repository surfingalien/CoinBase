from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    live_trading_enabled: bool = False

    webhook_secret: str = "change_me_to_a_long_random_string"
    database_url: str = "sqlite+aiosqlite:///./trading.db"

    max_position_pct_of_portfolio: float = 0.10
    max_daily_loss_pct: float = 0.05
    base_trade_size_usd: float = 1000.0

    # Optional hard ceiling on how much of the account's real USD balance the
    # system will ever deploy — e.g. set to 250 to trade with only $250 even
    # if the connected Coinbase account holds more. All position sizing,
    # the daily-loss ceiling, and portfolio-pct limits are computed against
    # min(actual_balance, trading_budget_usd) once this is set. Leave unset
    # (0) to use the full real account balance, unchanged from before.
    trading_budget_usd: float = 0.0

    # Portfolio-level exposure limits: the system is long-only spot, holds at
    # most one position per symbol, and caps how many symbols it holds at once.
    max_open_positions: int = 10

    # Automatic exit management: the position monitor closes a position the
    # moment its unrealized P&L crosses either threshold. The trailing stop
    # arms once a position is up trailing_stop_activation_pct, then sells if
    # price falls trailing_stop_pct below its peak — letting winners run
    # while still locking in most of the gain. Set trailing_stop_pct=0 to
    # disable trailing and use the fixed take-profit only.
    take_profit_pct: float = 0.08
    stop_loss_pct: float = 0.04
    trailing_stop_pct: float = 0.03
    trailing_stop_activation_pct: float = 0.04
    position_monitor_interval_seconds: int = 30

    # Market sentiment: Crypto Fear & Greed Index (alternative.me, free, no
    # key) plus recent crypto news headlines (public RSS). Injected into the
    # analysis prompt and used to damp position sizes in extreme regimes.
    sentiment_enabled: bool = True
    sentiment_cache_minutes: int = 30

    # Minimum minutes between two native-analysis signals for the same symbol,
    # so a persistently bullish chart doesn't spam the pipeline every cycle.
    signal_cooldown_minutes: int = 60

    # Native technical + AI analysis: computes RSI/MACD/EMA/BB/ATR/ADX/S&R/
    # patterns directly from Coinbase's own public candle data (no external
    # app, no token) and polls every pair on a fixed interval as an
    # additional signal source alongside the TradingView Pine Script webhooks.
    # Runs with pure rule-based confluence scoring if ANTHROPIC_API_KEY is
    # unset; uses Claude for the reasoning/confidence when it is set.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    market_analysis_poll_interval_seconds: int = 900
    market_analysis_min_confidence: float = 0.60
    market_analysis_granularity_seconds: int = 3600  # Coinbase candle bucket: 60,300,900,3600,21600,86400

    # When on, the batched Claude call is given Anthropic's server-side
    # web_search tool (billed per-search, separate from token costs) so the
    # AI can check for very recent news the cached RSS headlines might miss
    # before deciding. Has no effect if anthropic_api_key is unset.
    enable_web_research: bool = True

    # Cross-sectional momentum: ranks the whole ALLOWED_PAIRS universe by a
    # 12-1 style momentum score (return from `momentum_lookback_days` ago to
    # `momentum_skip_days` ago, skipping the most recent month to avoid
    # short-term reversal) and goes long the top `momentum_top_pct`. The
    # ranking API (/api/momentum/rankings) is always available; the automatic
    # monthly rebalancer that actually places trades stays OFF unless you set
    # cross_sectional_enabled=true, since it opens real positions.
    cross_sectional_enabled: bool = False
    momentum_lookback_days: int = 330
    momentum_skip_days: int = 30
    momentum_top_pct: float = 0.20
    # Day of month (UTC) the rebalancer fires on when enabled.
    momentum_rebalance_day: int = 1
    # How often the rebalancer wakes to check the date (default 6h). It only
    # acts once on momentum_rebalance_day each month, regardless of interval.
    cross_sectional_check_interval_seconds: int = 21600

    # Regime filter: classifies each symbol's market regime (trend / range /
    # storm / neutral) from its own daily candles and only lets strategies
    # open positions in regimes they're built for. Exits are never filtered.
    regime_filter_enabled: bool = True
    regime_cache_minutes: int = 60

    # Validation gate: a (strategy, symbol) pair must hold a PASS verdict from
    # the out-of-sample backtest harness before new entries are allowed, and
    # is revalidated on this TTL. Strategies the harness can't model are
    # exempt; validation infrastructure errors fail open.
    validation_gate_enabled: bool = True
    validation_gate_ttl_hours: int = 24

    # Validation harness (/api/validate): the fraction of history held out as
    # out-of-sample when backtesting a strategy before you trust it live.
    backtest_oos_fraction: float = 0.30
    # Round-trip trading friction applied to every simulated entry and exit, so
    # backtest Sharpe/return reflect what you'd actually net. Charged per side:
    # a fee (Coinbase Advanced Trade taker fee, ~0.4-0.6% at low volume) plus
    # slippage (market-order fill drift). Total cost per round trip is roughly
    # 2 x (fee + slippage). Set both to 0 for a frictionless (optimistic) run.
    backtest_fee_pct: float = 0.005
    backtest_slippage_pct: float = 0.0005

    # Taker fee simulated by the paper exchange on every fill, so paper cash
    # and P&L track what live trading would actually net instead of running
    # fee-free optimistic. Matches Coinbase Advanced Trade's retail taker
    # tier; set to 0 for the old frictionless behaviour. Also used as the
    # assumed per-side fee in the risk engine's expectancy check (an entry is
    # rejected when round-trip fees eat too much of its take-profit distance).
    paper_fee_pct: float = 0.006


settings = Settings()

# The crypto universe the system is allowed to trade on Coinbase.
ALLOWED_PAIRS = [
    "BTC-USD", "ETH-USD",
    "SOL-USD", "AVAX-USD", "LINK-USD", "MATIC-USD", "DOT-USD", "ATOM-USD", "LTC-USD",
    "ADA-USD", "UNI-USD", "ARB-USD", "OP-USD", "NEAR-USD", "INJ-USD",
]

# Risk weight per pair: higher weight = more volatile = smaller position size.
RISK_TIERS = {
    "BTC-USD": 1.0, "ETH-USD": 1.0,
    "SOL-USD": 1.2, "AVAX-USD": 1.2, "LINK-USD": 1.2, "MATIC-USD": 1.2,
    "DOT-USD": 1.2, "ATOM-USD": 1.2, "LTC-USD": 1.2,
    "ADA-USD": 1.5, "UNI-USD": 1.5, "ARB-USD": 1.5,
    "OP-USD": 1.5, "NEAR-USD": 1.5, "INJ-USD": 1.5,
}

KNOWN_STRATEGIES = {
    "GainzAlgo_V2_Alpha",
    "Mean_Reversion_Master",
    "Breakout_Hunter",
    "VWAP_Bounce_Bot",
    "Scalp_Momentum",
    "Native_TA_AI",
    "Ultimate_Oscillator",
    "Turtle_Trend",
    "Cross_Sectional_Momentum",
}
