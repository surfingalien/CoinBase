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
  managed?: boolean;
  // "trade" = bot's own fill; "fills" = basis reconstructed from Coinbase buy
  // history; "fills_partial" = partly reconstructed; "sync_price" = P&L
  // measures from the sync moment, not original purchase.
  basis_source?: "trade" | "fills" | "fills_partial" | "sync_price";
}

export interface Portfolio {
  total_value: number;
  holdings_value: number;
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

export interface CompareSnapshot {
  symbol: string;
  price: number;
  rsi: number | null;
  macd_trend: string | null;
  adx: number | null;
  atr: number | null;
  above_ema50: boolean | null;
  above_ema200: boolean | null;
  higher_timeframe_trend: string | null;
  rule_verdict: { signal: string; confidence: number; reasoning: string };
  returns: Record<string, number>;
}

export interface CompareResult {
  a: CompareSnapshot;
  b: CompareSnapshot;
  ai_view: string | null;
  note: string;
}

export interface AuditVerify {
  valid: boolean;
  events: number;
  first_break: { seq: number; reason: string } | null;
}

// Strategies backtest.py can validate (mirrors backend BUILDERS).
export const BACKTESTABLE_STRATEGIES = [
  "GainzAlgo_V2_Alpha",
  "Mean_Reversion_Master",
  "Ultimate_Oscillator",
  "Turtle_Trend",
] as const;

export interface Metabolism {
  enabled: boolean;
  tier: "sustainable" | "stable" | "low_compute" | "critical";
  window_days: number;
  liquid_cash_usd: number;
  open_position_value_usd: number;
  equity_usd: number;
  costs: {
    llm_usd: number;
    infra_usd: number;
    operating_total_usd: number;
  };
  revenue: { trading_net_pnl_usd: number };
  rates_per_day: {
    operating_cost_usd: number;
    trading_net_pnl_usd: number;
    net_cashflow_usd: number;
  };
  runway_days: number | null;   // null = self-sustaining (infinite)
  self_sustaining: boolean;
  shedding_compute: boolean;
  entries_halted: boolean;   // only when liquid cash can't fund a minimum order
  entry_size_multiplier: number;   // 0.5 at critical tier, 1.0 otherwise
  active_model: string;
}

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
  compare: (a: string, b: string) =>
    getJSON<CompareResult>(`/api/analyze/compare?symbol_a=${encodeURIComponent(a)}&symbol_b=${encodeURIComponent(b)}`),
  auditVerify: () => getJSON<AuditVerify>("/api/audit/verify"),
  metabolism: () => getJSON<Metabolism>("/api/metabolism"),
  resetPaperTrading: () => postJSON<{ status: string; usd_balance: number }>("/api/reset"),
  syncHoldings: () => postJSON<{
    synced: { symbol: string; size: number; entry_price: number; value_usd: number }[];
    skipped: { symbol: string; reason: string }[];
    note: string;
  }>("/api/sync-holdings"),
};
