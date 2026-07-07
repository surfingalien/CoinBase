export interface Signal {
  id: string;
  timestamp: string;
  symbol: string;
  strategy: string;
  action: string;
  ai_decision: string | null;
  ai_confidence: number | null;
  ai_reasoning: string | null;
  status: string;
}

export interface Order {
  id: string;
  timestamp: string;
  symbol: string;
  side: string;
  size: number;
  quote_size_usd: number | null;
  avg_fill_price: number | null;
  status: string;
  is_live: boolean;
}

export interface Position {
  symbol: string;
  side: string;
  size: number;
  entry_price: number;
  current_price: number;
  peak_price: number | null;
  take_profit_price: number | null;
  stop_loss_price: number | null;
  unrealized_pnl: number;
}

export interface Portfolio {
  total_value: number;
  usd_balance: number;
  trading_budget_usd: number | null;
  open_positions: number;
  is_live: boolean;
  positions: Position[];
}

export interface Stats {
  total_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  win_rate: number;
  total_trades: number;
  closed_positions: number;
  total_signals: number;
  executed_signals: number;
}

export interface ClosedPosition {
  id: string;
  symbol: string;
  size: number;
  entry_price: number;
  exit_price: number | null;
  realized_pnl: number | null;
  exit_reason: string | null;
  opened_at: string | null;
  closed_at: string | null;
}

export interface Config {
  is_live: boolean;
  allowed_pairs: string[];
  risk_tiers: Record<string, number>;
  risk: {
    max_position_pct_of_portfolio: number;
    max_daily_loss_pct: number;
    max_open_positions: number;
    base_trade_size_usd: number;
    trading_budget_usd: number | null;
    tradeable_balance_usd: number;
    daily_pnl_pct: number;
    daily_loss_limit_hit: boolean;
  };
  exits: {
    take_profit_pct: number;
    stop_loss_pct: number;
    trailing_stop_pct: number;
    trailing_stop_activation_pct: number;
  };
  ai: {
    anthropic_configured: boolean;
    anthropic_model: string | null;
    market_analysis_poll_interval_seconds: number;
    market_analysis_min_confidence: number;
    signal_cooldown_minutes: number;
  };
  sentiment: {
    enabled: boolean;
    cache_minutes: number;
  };
  cross_sectional: {
    enabled: boolean;
    top_pct: number;
    rebalance_day: number;
    lookback_days: number;
    skip_days: number;
  };
}

export interface ValidationSegment {
  sharpe: number;
  max_drawdown: number;
  total_return: number;
  trades: number;
  bars: number;
}

export interface ValidationCheck {
  name: string;
  value: number;
  pass: boolean;
  threshold?: number;
}

export interface ValidationResult {
  symbol: string;
  strategy: string;
  bars: number;
  oos_fraction: number;
  costs: { fee_pct: number; slippage_pct: number; round_trip_pct: number };
  in_sample: ValidationSegment;
  out_of_sample: ValidationSegment;
  full_period: ValidationSegment;
  checks: ValidationCheck[];
  verdict: "PASS" | "FAIL";
  passed: number;
  total_checks: number;
  error?: string;
}

// Strategies backtest.py can validate (mirrors backend BUILDERS).
export const BACKTESTABLE_STRATEGIES = [
  "GainzAlgo_V2_Alpha",
  "Mean_Reversion_Master",
  "Ultimate_Oscillator",
  "Turtle_Trend",
] as const;

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `${url} responded ${res.status}`);
  }
  return res.json();
}

async function postJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `${url} responded ${res.status}`);
  }
  return res.json();
}

export const api = {
  portfolio: () => getJSON<Portfolio>("/api/portfolio"),
  signals: () => getJSON<Signal[]>("/api/signals"),
  orders: () => getJSON<Order[]>("/api/orders"),
  stats: () => getJSON<Stats>("/api/stats"),
  positionHistory: () => getJSON<ClosedPosition[]>("/api/positions/history"),
  config: () => getJSON<Config>("/api/config"),
  validate: (symbol: string, strategy: string) =>
    getJSON<ValidationResult>(`/api/validate?symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}`),
  resetPaperTrading: () => postJSON<{ status: string; usd_balance: number }>("/api/reset"),
  syncHoldings: () => postJSON<{
    synced: { symbol: string; size: number; entry_price: number; value_usd: number }[];
    skipped: { symbol: string; reason: string }[];
    note: string;
  }>("/api/sync-holdings"),
};
