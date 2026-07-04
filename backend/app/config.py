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

    # FinSurfing integration: an external app (github.com/surfingalien/finsurfing)
    # that renders live TradingView charts and runs a Claude-based technical
    # analysis engine (RSI/MACD/EMA/BB/ATR/ADX/S&R/patterns -> structured
    # BUY/SELL/HOLD signal). When configured, GainzAI polls it as an
    # additional signal source alongside the TradingView Pine Script webhooks.
    finsurfing_base_url: str = ""
    finsurfing_api_token: str = ""
    finsurfing_poll_interval_seconds: int = 900
    finsurfing_min_confidence: float = 0.60
    finsurfing_interval: str = "60"  # TradingView-style interval: 1,5,15,30,60,240,D,W


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
    "FinSurfing_AI",
}
