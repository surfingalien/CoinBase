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
  resetPaperTrading: () => postJSON<{ status: string; usd_balance: number }>("/api/reset"),
};
