import { useEffect, useState } from "react";
import {
  Bot, LayoutDashboard, Wallet, Activity, Settings as SettingsIcon, Shield, RefreshCw,
  TrendingUp, TrendingDown, Briefcase, Zap, Brain, Sparkles, CheckCircle2, XCircle, AlertTriangle, Trash2,
} from "lucide-react";
import { cn, formatCurrency, formatRelativeTime } from "@/lib/utils";
import { api, type ClosedPosition, type Config, type Order, type Portfolio, type Signal, type Stats } from "@/lib/api";

const Card = ({ children, className }: { children: React.ReactNode; className?: string }) => (
  <div className={cn("rounded-2xl border border-border bg-surface transition-all duration-300", className)}>{children}</div>
);

const Skeleton = ({ className }: { className?: string }) => (
  <div className={cn("shimmer-bg animate-shimmer rounded-md", className)} />
);

const Badge = ({ children, variant = "default" }: { children: React.ReactNode; variant?: string }) => {
  const variants: Record<string, string> = {
    success: "bg-success/15 text-success",
    danger: "bg-danger/15 text-danger",
    warning: "bg-warning/15 text-warning",
    primary: "bg-primary/15 text-primary",
    default: "bg-surface-raised text-foreground-muted",
  };
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold", variants[variant] || variants.default)}>
      {children}
    </span>
  );
};

const SectionCard = ({ title, icon: Icon, badge, children }: { title: string; icon: any; badge?: React.ReactNode; children: React.ReactNode }) => (
  <Card className="overflow-hidden">
    <div className="flex items-center justify-between gap-4 p-5 border-b border-border">
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold text-foreground-muted uppercase tracking-wider">{title}</h3>
      </div>
      {badge}
    </div>
    {children}
  </Card>
);

const NAV_ITEMS = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "portfolio", label: "Portfolio", icon: Wallet },
  { id: "signals", label: "Signals", icon: Activity },
  { id: "risk", label: "Risk Manager", icon: Shield },
  { id: "settings", label: "Settings", icon: SettingsIcon },
] as const;

type TabId = (typeof NAV_ITEMS)[number]["id"];

const TAB_TITLES: Record<TabId, string> = {
  dashboard: "Dashboard",
  portfolio: "Portfolio",
  signals: "Signals",
  risk: "Risk Manager",
  settings: "Settings",
};

export default function App() {
  const [activeTab, setActiveTab] = useState<TabId>("dashboard");
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [closedPositions, setClosedPositions] = useState<ClosedPosition[]>([]);
  const [config, setConfig] = useState<Config | null>(null);

  const fetchData = async () => {
    setIsLoading(true);
    const [p, s, o, st, ch, cfg] = await Promise.allSettled([
      api.portfolio(), api.signals(), api.orders(), api.stats(), api.positionHistory(), api.config(),
    ]);
    if (p.status === "fulfilled") setPortfolio(p.value);
    if (s.status === "fulfilled") setSignals(s.value);
    if (o.status === "fulfilled") setOrders(o.value);
    if (st.status === "fulfilled") setStats(st.value);
    if (ch.status === "fulfilled") setClosedPositions(ch.value);
    if (cfg.status === "fulfilled") setConfig(cfg.value);

    const failures = [
      ["portfolio", p], ["signals", s], ["orders", o],
      ["stats", st], ["position history", ch], ["config", cfg],
    ].filter(([, r]) => (r as PromiseSettledResult<unknown>).status === "rejected");

    if (failures.length === 6) {
      setError("Could not reach the trading backend. Is it running on :8000?");
    } else if (failures.length > 0) {
      const detail = failures
        .map(([name, r]) => `${name}: ${(r as PromiseRejectedResult).reason?.message ?? "failed"}`)
        .join("; ");
      setError(`Some data failed to load — ${detail}`);
    } else {
      setError(null);
    }
    setIsLoading(false);
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="relative flex h-screen overflow-hidden bg-background">
      <div className="pointer-events-none fixed inset-0 grid-pattern opacity-30" />
      <div className="pointer-events-none fixed -top-40 -left-40 h-96 w-96 rounded-full bg-primary/5 blur-3xl" />

      <aside className="hidden lg:flex z-20 w-64 shrink-0 flex-col border-r border-border bg-surface/50 backdrop-blur-xl">
        <div className="flex h-16 items-center gap-3 border-b border-border px-6">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-primary/60 shadow-lg shadow-primary/30">
            <Bot className="h-5 w-5 text-white" />
          </div>
          <div>
            <p className="text-sm font-bold leading-none">GainzAI</p>
            <p className="text-[10px] text-foreground-muted mt-0.5">Trading System</p>
          </div>
        </div>
        <nav className="flex-1 space-y-1 p-4">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setActiveTab(item.id)}
              className={cn(
                "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all text-left",
                activeTab === item.id ? "bg-primary/10 text-primary" : "text-foreground-muted hover:text-foreground hover:bg-surface-raised"
              )}
            >
              <item.icon className="h-4 w-4 shrink-0" /> {item.label}
            </button>
          ))}
        </nav>
        {portfolio && (
          <div className="p-4">
            <Badge variant={portfolio.is_live ? "danger" : "warning"}>
              {portfolio.is_live ? "LIVE TRADING" : "PAPER TRADING"}
            </Badge>
          </div>
        )}
      </aside>

      <div className="relative flex flex-1 flex-col overflow-hidden">
        <header className="relative z-10 flex h-16 items-center justify-between gap-4 border-b border-border bg-surface/50 px-6 backdrop-blur-xl">
          <h2 className="text-lg font-semibold">{TAB_TITLES[activeTab]}</h2>
          <div className="flex items-center gap-2">
            {error ? (
              <Badge variant="danger"><AlertTriangle className="h-3 w-3" />Offline</Badge>
            ) : (
              <Badge variant="success"><span className="h-1.5 w-1.5 rounded-full bg-current animate-pulse" />Live</Badge>
            )}
            <button
              onClick={fetchData}
              className="inline-flex items-center justify-center gap-2 rounded-xl text-sm font-medium transition-all h-9 w-9 bg-surface-raised text-foreground-muted hover:text-foreground hover:bg-surface-overlay border border-border active:scale-95"
            >
              <RefreshCw className={cn("h-4 w-4", isLoading && "animate-spin")} />
            </button>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[1600px] p-6 lg:p-8 space-y-6">
            {error && (
              <div className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
                <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}
            {activeTab === "dashboard" && (
              <DashboardTab isLoading={isLoading} portfolio={portfolio} stats={stats} signals={signals} orders={orders} />
            )}
            {activeTab === "portfolio" && (
              <PortfolioTab isLoading={isLoading} portfolio={portfolio} closedPositions={closedPositions} />
            )}
            {activeTab === "signals" && (
              <SignalsTab isLoading={isLoading} signals={signals} />
            )}
            {activeTab === "risk" && (
              <RiskTab isLoading={isLoading} config={config} />
            )}
            {activeTab === "settings" && (
              <SettingsTab isLoading={isLoading} config={config} onReset={fetchData} />
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

function DashboardTab({ isLoading, portfolio, stats, signals, orders }: {
  isLoading: boolean; portfolio: Portfolio | null; stats: Stats | null; signals: Signal[]; orders: Order[];
}) {
  return (
    <>
      {isLoading && !portfolio ? (
        <Card className="glass p-8 space-y-4">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-12 w-64" />
          <Skeleton className="h-4 w-48" />
        </Card>
      ) : (
        <Card className="glass relative overflow-hidden p-8 animate-slide-up">
          <div className={cn("absolute -top-20 -right-20 h-64 w-64 rounded-full blur-3xl opacity-10", (stats?.total_pnl || 0) >= 0 ? "bg-success" : "bg-danger")} />
          <div className="relative flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
                  <Wallet className="h-4 w-4 text-primary" />
                </div>
                <span className="text-sm font-medium uppercase tracking-wider text-foreground-muted">Total Portfolio Value</span>
              </div>
              <p className="text-4xl font-bold tracking-tight lg:text-5xl">{formatCurrency(portfolio?.total_value)}</p>
              <div className="flex flex-wrap items-center gap-4">
                <div className={cn("flex items-center gap-1.5", (stats?.total_pnl || 0) >= 0 ? "text-success" : "text-danger")}>
                  {(stats?.total_pnl || 0) >= 0 ? <TrendingUp className="h-4 w-4" /> : <TrendingDown className="h-4 w-4" />}
                  <span className="text-sm font-semibold">{formatCurrency(stats?.total_pnl)}</span>
                  <span className="text-xs text-foreground-muted">total P&L</span>
                </div>
                <div className="h-4 w-px bg-border" />
                <Badge variant={(stats?.win_rate || 0) >= 50 ? "success" : "warning"}>{stats?.win_rate ?? 0}% win rate</Badge>
              </div>
            </div>
            <div className="flex items-center gap-6">
              <div className="text-right">
                <p className="text-xs uppercase tracking-wider text-foreground-muted">USD Available</p>
                <p className="text-2xl font-bold text-success">{formatCurrency(portfolio?.usd_balance)}</p>
              </div>
              <div className="text-right">
                <p className="text-xs uppercase tracking-wider text-foreground-muted">Open Positions</p>
                <p className="text-2xl font-bold text-primary">{portfolio?.open_positions ?? 0}</p>
              </div>
            </div>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2 space-y-6 flex flex-col">
          <SectionCard title="Recent AI Signals" icon={Zap} badge={<Badge>TradingView → AI</Badge>}>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-border">
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Time</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Symbol</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-center">Action</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">AI Decision</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {isLoading && !signals.length
                    ? Array.from({ length: 3 }).map((_, i) => (
                        <tr key={i}><td colSpan={5} className="p-4"><Skeleton className="h-6 w-full" /></td></tr>
                      ))
                    : signals.map((s) => (
                        <tr key={s.id} className="hover:bg-surface-raised/50 transition-colors">
                          <td className="px-5 py-3 text-xs text-foreground-muted">{formatRelativeTime(s.timestamp)}</td>
                          <td className="px-5 py-3 text-sm font-semibold">{s.symbol}</td>
                          <td className="px-5 py-3 text-center"><Badge variant={s.action === "BUY" ? "success" : "danger"}>{s.action}</Badge></td>
                          <td className="px-5 py-3 text-sm">
                            <div className="flex items-center gap-2">
                              <Brain className="h-3.5 w-3.5 text-primary" />
                              <span className="text-xs font-medium">{s.ai_decision ?? "—"}</span>
                              {s.ai_confidence != null && (
                                <span className="text-[10px] text-foreground-subtle">({(s.ai_confidence * 100).toFixed(0)}%)</span>
                              )}
                            </div>
                          </td>
                          <td className="px-5 py-3 text-center">
                            <Badge variant={s.status === "executed" ? "success" : s.status === "rejected" ? "danger" : "warning"}>{s.status}</Badge>
                          </td>
                        </tr>
                      ))}
                  {!isLoading && !signals.length && (
                    <tr><td colSpan={5} className="p-8 text-center text-sm text-foreground-muted">No signals yet — waiting for TradingView alerts.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </SectionCard>

          <SectionCard title="Execution Log" icon={Briefcase}>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-border">
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Time</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Symbol</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-center">Side</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Size</th>
                    <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Fill Price</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {isLoading && !orders.length
                    ? Array.from({ length: 3 }).map((_, i) => (
                        <tr key={i}><td colSpan={5} className="p-4"><Skeleton className="h-6 w-full" /></td></tr>
                      ))
                    : orders.map((o) => (
                        <tr key={o.id} className="hover:bg-surface-raised/50 transition-colors">
                          <td className="px-5 py-3 text-xs text-foreground-muted">{formatRelativeTime(o.timestamp)}</td>
                          <td className="px-5 py-3 text-sm font-semibold">{o.symbol}</td>
                          <td className="px-5 py-3 text-center"><Badge variant={o.side === "BUY" ? "success" : "danger"}>{o.side}</Badge></td>
                          <td className="px-5 py-3 text-sm text-right font-mono">{o.size.toFixed(4)}</td>
                          <td className="px-5 py-3 text-sm text-right font-mono">{formatCurrency(o.avg_fill_price)}</td>
                        </tr>
                      ))}
                  {!isLoading && !orders.length && (
                    <tr><td colSpan={5} className="p-8 text-center text-sm text-foreground-muted">No orders executed yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </SectionCard>
        </div>

        <div className="space-y-6">
          <SectionCard title="AI Reasoning Engine" icon={Sparkles} badge={<Badge variant="primary">Rule Engine</Badge>}>
            <div className="p-5 space-y-3">
              {isLoading && !signals.length
                ? Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-20 animate-pulse rounded-xl bg-surface-raised" />)
                : signals.filter((s) => s.ai_reasoning).slice(0, 5).map((s) => (
                    <div key={s.id} className="rounded-xl border border-border bg-surface-raised/50 p-3 hover:border-border-strong transition-colors animate-fade-in">
                      <div className="flex items-start justify-between gap-2 mb-2">
                        <div className="flex items-center gap-2">
                          <Badge variant={s.action === "BUY" ? "success" : "danger"}>{s.action}</Badge>
                          <span className="text-xs font-semibold">{s.symbol}</span>
                        </div>
                        <span className="text-[10px] text-foreground-subtle">{formatRelativeTime(s.timestamp)}</span>
                      </div>
                      <p className="text-xs text-foreground-muted leading-relaxed mb-2">{s.ai_reasoning}</p>
                      <div className="flex items-center justify-between border-t border-border/50 pt-2 mt-2">
                        <div className="flex items-center gap-1.5">
                          {s.ai_decision === "EXECUTE" ? <CheckCircle2 className="h-3 w-3 text-success" /> : <XCircle className="h-3 w-3 text-danger" />}
                          <span className="text-[10px] font-medium text-foreground-muted">{s.ai_decision}</span>
                        </div>
                        {s.ai_confidence != null && (
                          <span className="text-[10px] font-mono text-foreground-muted">{(s.ai_confidence * 100).toFixed(0)}% Conf.</span>
                        )}
                      </div>
                    </div>
                  ))}
            </div>
          </SectionCard>
        </div>
      </div>
    </>
  );
}

function PortfolioTab({ isLoading, portfolio, closedPositions }: {
  isLoading: boolean; portfolio: Portfolio | null; closedPositions: ClosedPosition[];
}) {
  return (
    <div className="space-y-6">
      <SectionCard title="Open Positions" icon={Wallet} badge={<Badge>{portfolio?.open_positions ?? 0} open</Badge>}>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-border">
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Symbol</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Size</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Entry</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Current</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Take Profit</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Stop Loss</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Unrealized P&L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {isLoading && !portfolio?.positions.length
                ? Array.from({ length: 3 }).map((_, i) => (
                    <tr key={i}><td colSpan={7} className="p-4"><Skeleton className="h-6 w-full" /></td></tr>
                  ))
                : (portfolio?.positions ?? []).map((p) => (
                    <tr key={p.symbol} className="hover:bg-surface-raised/50 transition-colors">
                      <td className="px-5 py-3 text-sm font-semibold">{p.symbol}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono">{p.size.toFixed(4)}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono">{formatCurrency(p.entry_price)}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono">{formatCurrency(p.current_price)}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono text-success">{p.take_profit_price != null ? formatCurrency(p.take_profit_price) : "—"}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono text-danger">{p.stop_loss_price != null ? formatCurrency(p.stop_loss_price) : "—"}</td>
                      <td className={cn("px-5 py-3 text-sm text-right font-mono font-semibold", p.unrealized_pnl >= 0 ? "text-success" : "text-danger")}>
                        {formatCurrency(p.unrealized_pnl)}
                      </td>
                    </tr>
                  ))}
              {!isLoading && !portfolio?.positions.length && (
                <tr><td colSpan={7} className="p-8 text-center text-sm text-foreground-muted">No open positions.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard title="Closed Position History" icon={Briefcase}>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-border">
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Closed</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Symbol</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Entry</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Exit</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Reason</th>
                <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-right">Realized P&L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {isLoading && !closedPositions.length
                ? Array.from({ length: 3 }).map((_, i) => (
                    <tr key={i}><td colSpan={6} className="p-4"><Skeleton className="h-6 w-full" /></td></tr>
                  ))
                : closedPositions.map((p) => (
                    <tr key={p.id} className="hover:bg-surface-raised/50 transition-colors">
                      <td className="px-5 py-3 text-xs text-foreground-muted">{p.closed_at ? formatRelativeTime(p.closed_at) : "—"}</td>
                      <td className="px-5 py-3 text-sm font-semibold">{p.symbol}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono">{formatCurrency(p.entry_price)}</td>
                      <td className="px-5 py-3 text-sm text-right font-mono">{p.exit_price != null ? formatCurrency(p.exit_price) : "—"}</td>
                      <td className="px-5 py-3 text-xs text-foreground-muted">{p.exit_reason ?? "—"}</td>
                      <td className={cn("px-5 py-3 text-sm text-right font-mono font-semibold", (p.realized_pnl ?? 0) >= 0 ? "text-success" : "text-danger")}>
                        {p.realized_pnl != null ? formatCurrency(p.realized_pnl) : "—"}
                      </td>
                    </tr>
                  ))}
              {!isLoading && !closedPositions.length && (
                <tr><td colSpan={6} className="p-8 text-center text-sm text-foreground-muted">No closed trades yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </SectionCard>
    </div>
  );
}

function SignalsTab({ isLoading, signals }: { isLoading: boolean; signals: Signal[] }) {
  return (
    <SectionCard title="All Signals" icon={Activity} badge={<Badge>{signals.length} total</Badge>}>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-border">
              <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Time</th>
              <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Symbol</th>
              <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">Strategy</th>
              <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-center">Action</th>
              <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-left">AI Reasoning</th>
              <th className="px-5 py-3 text-xs font-semibold uppercase tracking-wider text-foreground-muted text-center">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {isLoading && !signals.length
              ? Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i}><td colSpan={6} className="p-4"><Skeleton className="h-6 w-full" /></td></tr>
                ))
              : signals.map((s) => (
                  <tr key={s.id} className="hover:bg-surface-raised/50 transition-colors">
                    <td className="px-5 py-3 text-xs text-foreground-muted">{formatRelativeTime(s.timestamp)}</td>
                    <td className="px-5 py-3 text-sm font-semibold">{s.symbol}</td>
                    <td className="px-5 py-3 text-xs text-foreground-muted">{s.strategy}</td>
                    <td className="px-5 py-3 text-center"><Badge variant={s.action === "BUY" ? "success" : "danger"}>{s.action}</Badge></td>
                    <td className="px-5 py-3 text-xs text-foreground-muted max-w-md">{s.ai_reasoning ?? "—"}</td>
                    <td className="px-5 py-3 text-center">
                      <Badge variant={s.status === "executed" ? "success" : s.status === "rejected" ? "danger" : "warning"}>{s.status}</Badge>
                    </td>
                  </tr>
                ))}
            {!isLoading && !signals.length && (
              <tr><td colSpan={6} className="p-8 text-center text-sm text-foreground-muted">No signals yet — waiting for TradingView alerts.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </SectionCard>
  );
}

const StatRow = ({ label, value }: { label: string; value: React.ReactNode }) => (
  <div className="flex items-center justify-between border-b border-border/50 py-3 last:border-0">
    <span className="text-sm text-foreground-muted">{label}</span>
    <span className="text-sm font-semibold font-mono">{value}</span>
  </div>
);

function RiskTab({ isLoading, config }: { isLoading: boolean; config: Config | null }) {
  if (isLoading && !config) {
    return <Card className="glass p-8 space-y-4"><Skeleton className="h-4 w-32" /><Skeleton className="h-32 w-full" /></Card>;
  }
  if (!config) return null;
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <SectionCard title="Risk Limits" icon={Shield} badge={
        config.risk.daily_loss_limit_hit ? <Badge variant="danger">Daily loss limit hit</Badge> : <Badge variant="success">Within limits</Badge>
      }>
        <div className="p-5">
          <StatRow label="Max position size (% of portfolio)" value={`${(config.risk.max_position_pct_of_portfolio * 100).toFixed(1)}%`} />
          <StatRow label="Max daily loss" value={`${(config.risk.max_daily_loss_pct * 100).toFixed(1)}%`} />
          <StatRow label="Today's realized P&L" value={
            <span className={config.risk.daily_pnl_pct >= 0 ? "text-success" : "text-danger"}>{(config.risk.daily_pnl_pct * 100).toFixed(2)}%</span>
          } />
          <StatRow label="Max open positions" value={config.risk.max_open_positions} />
          <StatRow label="Base trade size" value={formatCurrency(config.risk.base_trade_size_usd)} />
        </div>
      </SectionCard>

      <SectionCard title="Exit Management" icon={TrendingDown}>
        <div className="p-5">
          <StatRow label="Take profit" value={`${(config.exits.take_profit_pct * 100).toFixed(1)}%`} />
          <StatRow label="Stop loss" value={`${(config.exits.stop_loss_pct * 100).toFixed(1)}%`} />
          <StatRow label="Trailing stop" value={`${(config.exits.trailing_stop_pct * 100).toFixed(1)}%`} />
          <StatRow label="Trailing stop activation" value={`${(config.exits.trailing_stop_activation_pct * 100).toFixed(1)}%`} />
        </div>
      </SectionCard>

      <SectionCard title="Risk Tier Weights" icon={Briefcase} badge={<Badge>{Object.keys(config.risk_tiers).length} pairs</Badge>}>
        <div className="p-5 grid grid-cols-2 sm:grid-cols-3 gap-3">
          {Object.entries(config.risk_tiers).map(([symbol, weight]) => (
            <div key={symbol} className="rounded-xl border border-border bg-surface-raised/50 p-3 text-center">
              <p className="text-xs font-semibold">{symbol}</p>
              <p className="text-xs text-foreground-muted mt-1">weight {weight.toFixed(1)}x</p>
            </div>
          ))}
        </div>
      </SectionCard>
    </div>
  );
}

function SettingsTab({ isLoading, config, onReset }: { isLoading: boolean; config: Config | null; onReset: () => void }) {
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const handleReset = async () => {
    setResetting(true);
    setResetError(null);
    try {
      await api.resetPaperTrading();
      setConfirming(false);
      onReset();
    } catch (err) {
      setResetError(err instanceof Error ? err.message : "Reset failed.");
    } finally {
      setResetting(false);
    }
  };

  if (isLoading && !config) {
    return <Card className="glass p-8 space-y-4"><Skeleton className="h-4 w-32" /><Skeleton className="h-32 w-full" /></Card>;
  }
  if (!config) return null;
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <SectionCard title="Trading Mode" icon={Bot} badge={
        <Badge variant={config.is_live ? "danger" : "warning"}>{config.is_live ? "LIVE TRADING" : "PAPER TRADING"}</Badge>
      }>
        <div className="p-5">
          <StatRow label="Allowed trading pairs" value={config.allowed_pairs.length} />
        </div>
      </SectionCard>

      {!config.is_live && (
        <SectionCard title="Paper Trading Data" icon={Trash2}>
          <div className="p-5 space-y-3">
            <p className="text-xs text-foreground-muted leading-relaxed">
              Clears all mock signals, orders, and positions, and resets the
              simulated USD balance and holdings back to a fresh start. This
              cannot be undone.
            </p>
            {resetError && <p className="text-xs text-danger">{resetError}</p>}
            {!confirming ? (
              <button
                type="button"
                onClick={() => setConfirming(true)}
                className="inline-flex items-center gap-2 rounded-xl border border-border bg-surface-raised px-4 py-2 text-sm font-medium text-danger hover:bg-danger/10 transition-all"
              >
                <Trash2 className="h-4 w-4" /> Clear mock trades &amp; holdings
              </button>
            ) : (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleReset}
                  disabled={resetting}
                  className="inline-flex items-center gap-2 rounded-xl bg-danger px-4 py-2 text-sm font-medium text-white hover:bg-danger/90 transition-all disabled:opacity-50"
                >
                  {resetting ? "Clearing…" : "Confirm — clear everything"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  disabled={resetting}
                  className="rounded-xl border border-border px-4 py-2 text-sm font-medium text-foreground-muted hover:bg-surface-raised transition-all"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        </SectionCard>
      )}

      <SectionCard title="AI Engine" icon={Brain} badge={
        <Badge variant={config.ai.anthropic_configured ? "success" : "default"}>{config.ai.anthropic_configured ? "Claude enabled" : "Rule-based only"}</Badge>
      }>
        <div className="p-5">
          <StatRow label="Model" value={config.ai.anthropic_model ?? "—"} />
          <StatRow label="Poll interval" value={`${config.ai.market_analysis_poll_interval_seconds}s`} />
          <StatRow label="Min confidence" value={`${(config.ai.market_analysis_min_confidence * 100).toFixed(0)}%`} />
          <StatRow label="Signal cooldown" value={`${config.ai.signal_cooldown_minutes} min`} />
        </div>
      </SectionCard>

      <SectionCard title="Market Sentiment" icon={Sparkles} badge={
        <Badge variant={config.sentiment.enabled ? "success" : "default"}>{config.sentiment.enabled ? "Enabled" : "Disabled"}</Badge>
      }>
        <div className="p-5">
          <StatRow label="Cache duration" value={`${config.sentiment.cache_minutes} min`} />
        </div>
      </SectionCard>

      <SectionCard title="Allowed Pairs" icon={Wallet}>
        <div className="p-5 flex flex-wrap gap-2">
          {config.allowed_pairs.map((pair) => <Badge key={pair}>{pair}</Badge>)}
        </div>
      </SectionCard>
    </div>
  );
}
