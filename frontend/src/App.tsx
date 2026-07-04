import { useEffect, useState } from "react";
import {
  Bot, LayoutDashboard, Wallet, Activity, Settings, Shield, RefreshCw,
  TrendingUp, TrendingDown, Briefcase, Zap, Brain, Sparkles, CheckCircle2, XCircle, AlertTriangle,
} from "lucide-react";
import { cn, formatCurrency, formatRelativeTime } from "@/lib/utils";
import { api, type Order, type Portfolio, type Signal, type Stats } from "@/lib/api";

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

export default function App() {
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);

  const fetchData = async () => {
    setIsLoading(true);
    try {
      const [p, s, o, st] = await Promise.all([api.portfolio(), api.signals(), api.orders(), api.stats()]);
      setPortfolio(p);
      setSignals(s);
      setOrders(o);
      setStats(st);
      setError(null);
    } catch (err) {
      setError("Could not reach the trading backend. Is it running on :8000?");
    } finally {
      setIsLoading(false);
    }
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
          {[
            { label: "Dashboard", icon: LayoutDashboard, active: true },
            { label: "Portfolio", icon: Wallet },
            { label: "Signals", icon: Activity },
            { label: "Risk Manager", icon: Shield },
            { label: "Settings", icon: Settings },
          ].map((item) => (
            <a
              key={item.label}
              href="#"
              className={cn(
                "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all",
                item.active ? "bg-primary/10 text-primary" : "text-foreground-muted hover:text-foreground hover:bg-surface-raised"
              )}
            >
              <item.icon className="h-4 w-4 shrink-0" /> {item.label}
            </a>
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
          <h2 className="text-lg font-semibold">Dashboard</h2>
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
                <Card className="overflow-hidden">
                  <div className="flex items-center justify-between gap-4 p-5 border-b border-border">
                    <div className="flex items-center gap-2">
                      <Zap className="h-4 w-4 text-warning" />
                      <h3 className="text-sm font-semibold text-foreground-muted uppercase tracking-wider">Recent AI Signals</h3>
                    </div>
                    <Badge>TradingView → AI</Badge>
                  </div>
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
                </Card>

                <Card className="overflow-hidden">
                  <div className="flex items-center justify-between gap-4 p-5 border-b border-border">
                    <div className="flex items-center gap-2">
                      <Briefcase className="h-4 w-4 text-primary" />
                      <h3 className="text-sm font-semibold text-foreground-muted uppercase tracking-wider">Execution Log</h3>
                    </div>
                  </div>
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
                </Card>
              </div>

              <div className="space-y-6">
                <Card className="glass overflow-hidden">
                  <div className="flex items-center justify-between gap-4 p-5 border-b border-border">
                    <div className="flex items-center gap-2">
                      <Sparkles className="h-4 w-4 text-primary" />
                      <h3 className="text-sm font-semibold text-foreground-muted uppercase tracking-wider">AI Reasoning Engine</h3>
                    </div>
                    <Badge variant="primary">Rule Engine</Badge>
                  </div>
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
                </Card>
              </div>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
