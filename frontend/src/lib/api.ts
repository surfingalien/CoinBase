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
  unrealized_pnl: number;
}

export interface Portfolio {
  total_value: number;
  usd_balance: number;
  open_positions: number;
  is_live: boolean;
  positions: Position[];
}

export interface Stats {
  total_pnl: number;
  win_rate: number;
  total_trades: number;
  total_signals: number;
  executed_signals: number;
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} responded ${res.status}`);
  return res.json();
}

export const api = {
  portfolio: () => getJSON<Portfolio>("/api/portfolio"),
  signals: () => getJSON<Signal[]>("/api/signals"),
  orders: () => getJSON<Order[]>("/api/orders"),
  stats: () => getJSON<Stats>("/api/stats"),
};
