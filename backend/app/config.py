from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    live_trading_enabled: bool = False

    openai_api_key: str = ""

    webhook_secret: str = "change_me_to_a_long_random_string"
    database_url: str = "sqlite+aiosqlite:///./trading.db"

    max_position_pct_of_portfolio: float = 0.10
    max_daily_loss_pct: float = 0.05
    base_trade_size_usd: float = 1000.0

    # Automatic exit management: the position monitor closes a position the
    # moment its unrealized P&L crosses either threshold.
    take_profit_pct: float = 0.08
    stop_loss_pct: float = 0.04
    position_monitor_interval_seconds: int = 30

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
}
