# Replacement Procedures — GainzAI

Operating rules for whoever answers questions about, or changes, this trading
bot. Every procedure is a **trigger → action**: "When you see X, do Y." No
judgment calls. If a rule ever reads like advice, it's a bug in this document —
rewrite it into an action.

Scope note: these are the traps that have actually bitten this codebase (see
git history) or that a strong model would *not* catch on its own. Anything
obvious — read the code before editing, run the tests, don't hardcode secrets —
is deliberately omitted.

---

## Area 1 — Money & position sizing

**Procedures**

- When any code computes a trade size or the daily-loss ceiling, grep the
  balance it feeds in: if it is a raw `get_account_value()` or
  `get_usd_balance()` result, wrap it in `effective_usd_balance()` first.
  *(Prevents: sizing against the full account when `TRADING_BUDGET_USD` was set
  to cap it — trading $400 when the user said $250.)*
- When you display "account value / balance / equity" to the user, source it
  from `get_account_value()` (true NAV = cash + marked-to-market holdings), and
  when you size a trade, source it from `effective_usd_balance(...)`. Never swap
  the two.
  *(Prevents: showing the budget cap as if it were the account's real value, or
  sizing off NAV and blowing past the budget.)*
- When a sizing change lands, assert the order of clamps is unchanged:
  `raw = base_trade_size_usd × size_multiplier × confidence`, then
  `min(raw, balance × max_position_pct_of_portfolio)`, then reject below
  `MIN_TRADE_SIZE_USD`. If you reordered them, revert.
  *(Prevents: a high-confidence signal bypassing the per-trade cap.)*
- When a number that can be zero sits in a denominator (`total_value`,
  `price`, `size`), confirm the existing `if x > 0 else 0.0` guard survives your
  edit.
  *(Prevents: ZeroDivisionError zeroing out daily-P&L enforcement on an empty
  account.)*

**Worked example.** A PR "simplifies" `size_trade` by passing
`await exchange.get_account_value()` straight in as `usd_balance`. Procedure 1
fires: the value never passed through `effective_usd_balance()`. With
`TRADING_BUDGET_USD=250` and a $400 account, `max_allowed` becomes
`400 × pct` instead of `250 × pct` — the bot silently trades with money the
user fenced off. Rejected; re-route through `effective_usd_balance()`.

---

## Area 2 — Order execution (BUY vs SELL)

**Procedures**

- When you place a BUY, pass `quote_size` (USD to spend). When you place a
  SELL, pass `base_size` (units to sell). If you see a SELL carrying only
  `quote_size`, stop and route it through the base-size path.
  *(Prevents: closing a position by dollar amount, which under- or over-sells
  the actual holding.)*
- When a live SELL is built, confirm the size goes through
  `CoinbaseExchange._sell_size` — capped at the actually-held balance and
  floored to the product's `base_increment` — before hitting
  `market_order_sell`. If a raw float is passed straight to the SDK, fix it.
  *(Prevents: Coinbase rejecting a too-precise or rounded-up size, leaving a
  take-profit/stop-loss position **open and unsold**.)*
- When `_sell_size` can raise `ValueError` (dust below `base_min_size`), verify
  the caller returns `{"success": False, ...}` and does not crash the monitor
  loop.
  *(Prevents: one un-closable dust position killing the background exit loop for
  every other position.)*
- When an order result comes back, check `result.get("success")` before reading
  `success_response`. Treat a missing/false success as a failed order, not a
  fill.
  *(Prevents: recording a phantom fill for an order Coinbase refused.)*

**Worked example.** `position_monitor` triggers a stop-loss and calls
`place_market_order(symbol, "SELL", base_size=0.0734912...)`. Procedure 2
checks the live path: without `_sell_size`, that 7-decimal size is finer than
SOL's `base_increment`, Coinbase returns an error, the sell never happens, and
the "protected" position keeps falling. Route through `_sell_size` → floored to
increment, capped at held → the exit actually fills.

---

## Area 3 — Live vs paper safety & secrets

**Procedures**

- When a code path can place, size, or cancel an order, trace whether it can
  run under `LIVE_TRADING_ENABLED=true`. If it can move real money, it must NOT
  silently fall back to `MockExchange` on error — it fails loudly. If you added
  a fallback, remove it.
  *(Prevents: a live user believing they're protected while the bot quietly ran
  in paper mode, or vice-versa.)*
- When you touch credential handling, confirm no key material is ever logged,
  echoed, returned in an API response, or written to the DB — only the constant
  PEM boilerplate markers and lengths (as in `_diagnose_pem_issue`) may surface.
  *(Prevents: leaking a private key into logs or a diagnostics endpoint.)*
- When you validate a Coinbase key, keep the ECDSA-only assumption: an Ed25519
  key (short base64, no `-----BEGIN`) must be rejected with the ECDSA hint, not
  accepted.
  *(Prevents: an unusable key silently passing validation and failing at order
  time.)*
- When you add or change an env var that gates real money or spends tokens
  (`LIVE_TRADING_ENABLED`, `ENABLE_WEB_RESEARCH`, `CROSS_SECTIONAL_ENABLED`),
  confirm its **default is the safe/off value** in `config.py` and
  `.env.example`.
  *(Prevents: a fresh deploy going live, or auto-rebalancing, without the user
  opting in.)*

**Worked example.** A refactor wraps `CoinbaseExchange.place_market_order` in
`try/except` that returns `MockExchange().place_market_order(...)` on failure
"so the loop never dies." Procedure 1 fires: a transient 401 now routes a real
exit into the paper simulator — the live position stays open while the
dashboard shows it closed. Rejected; surface the error, keep the position
truthful.

---

## Area 4 — Risk gates

**Procedures**

- When you handle a SELL/short signal, confirm long-only holds: SELL closes an
  existing position; a short (SELL with no position) is rejected, never opened.
  *(Prevents: opening a spot short the account can't hold.)*
- When you process a BUY, confirm the "one position per symbol" and
  `MAX_OPEN_POSITIONS` checks run before sizing, so repeated bullish signals
  can't stack exposure.
  *(Prevents: three alerts on BTC becoming 3× the intended BTC bet.)*
- When you change daily-loss logic, confirm `compute_daily_pnl_pct` still sums
  **realized** P&L since UTC midnight over `total_value = cash + open_value`,
  and that `size_trade` refuses new entries once
  `daily_pnl_pct <= -max_daily_loss_pct`.
  *(Prevents: the circuit breaker measuring the wrong window and letting a
  bad day keep compounding.)*
- When sentiment/Fear-&-Greed enters sizing, confirm it only *scales* size
  (60%/80% multipliers) and never generates or vetoes a trade.
  *(Prevents: turning a risk moderator into a signal source.)*

**Worked example.** A change makes the daily-loss check use unrealized P&L "to
react faster." Procedure 3 fires: a temporary drawdown on an open position now
trips the breaker and blocks entries that were never actually losses. Revert to
realized-only.

---

## Area 5 — Position exits

**Procedures**

- When a signal supplies ATR-based stop-loss/take-profit levels, confirm
  `position_monitor` uses those exact price levels and does **not** fall back to
  global `TAKE_PROFIT_PCT`/`STOP_LOSS_PCT` for that position.
  *(Prevents: overriding a strategy's tuned stop with a generic percentage and
  exiting at the wrong price.)*
- When you display a synced/tracked holding's exit levels, show the **effective**
  levels actually in force (signal ATR levels if present, else the global
  percentages), not the raw globals.
  *(Prevents: the dashboard promising an 8% take-profit while the monitor is
  actually watching an ATR level.)*
- When you edit the monitor loop, confirm every open position is still polled
  every `POSITION_MONITOR_INTERVAL_SECONDS`, and one position raising an
  exception (e.g. dust `ValueError`) does not abort the pass for the rest.
  *(Prevents: a single bad symbol freezing all automatic exits.)*
- When a position closes, confirm `realized_pnl` and `closed_at` are written, so
  the daily-loss breaker and history/stats stay correct.
  *(Prevents: a closed loss that never counts against the daily ceiling.)*

**Worked example.** `Turtle_Trend` opens a position sized to a 2N (2·ATR) stop
and passes that stop level with the signal. A monitor edit reads
`settings.stop_loss_pct` unconditionally. Procedure 1 fires: the 2N stop is
ignored, a generic 4% stop replaces it, and the position is knocked out on
noise the strategy meant to sit through. Restore the ATR-level precedence.

---

## Area 6 — LLM / Claude integration

**Procedures**

- When you parse Claude's output, run it through the prose-tolerant JSON
  extractor (find the JSON object inside surrounding text), never
  `json.loads(raw)` on the whole reply.
  *(Prevents: a valid analysis being dropped because Claude wrapped the JSON in
  a sentence — the #17 failure.)*
- When a Claude call uses the `web_search` tool and the tool call fails,
  confirm the code retries the analysis **without** `web_search` rather than
  failing the whole cycle.
  *(Prevents: one tool hiccup zeroing out a poll cycle's signals — the #24
  failure.)*
- When you add anything to the per-cycle Claude prompt, confirm the batching
  contract holds: **one** call per cycle for all gated candidates, gated by the
  free rule-based confluence check first, one dense TA line per symbol,
  headlines capped, reasoning one sentence.
  *(Prevents: silently regressing to ~15 calls/cycle and multiplying token
  cost.)*
- When `ANTHROPIC_API_KEY` is absent, confirm the rule-based confluence
  fallback still produces BUY/SELL/HOLD and the pipeline runs unchanged.
  *(Prevents: making Claude a hard dependency for a system that must run
  keyless.)*

**Worked example.** A tidy-up replaces the extractor with
`data = json.loads(response.content)`. Procedure 1 fires against a real reply:
`"Based on the indicators, here's my call:\n{...}"` — `json.loads` throws, the
signal is discarded, and a valid BUY never reaches the risk engine. Restore the
substring-extraction path.

---

## Area 7 — Market data & indicators

**Procedures**

- When a lookback exceeds ~300 daily bars (Coinbase's public cap), confirm the
  clamp to available history is applied **identically to every symbol** before
  ranking.
  *(Prevents: a cross-sectional momentum ranking comparing symbols over
  different windows — apples to oranges.)*
- When an indicator can return `NaN`/`None` on short history (early RSI, MACD,
  ATR warm-up), confirm downstream comparisons treat it as "no signal," not as
  a number.
  *(Prevents: `NaN`-driven false BUYs on a freshly listed pair.)*
- When live price fetch fails, confirm the code uses the last-known/`_FALLBACK_PRICES`
  path (paper) or raises the enriched error (live) — it must not proceed with
  price `0` or `100.0` as if real.
  *(Prevents: sizing or P&L computed against a placeholder price.)*
- When you add a pair, add it to both `ALLOWED_PAIRS` and (for offline dev)
  `_FALLBACK_PRICES`, and confirm it has a real `-USD` Coinbase market.
  *(Prevents: a configured pair that silently never trades.)*

**Worked example.** A momentum change requests a 400-day lookback and clamps
per-symbol only when the fetch returns short. Procedure 1 fires: BTC (350 bars)
is ranked over 350 days while a newer pair (200 bars) is ranked over 200 — the
newer pair's shorter, hotter window wins the ranking artificially. Clamp all
symbols to the common minimum first.

---

## Area 8 — Backtest / validation gate

**Procedures**

- When you change `backtest.py`, confirm all six gates remain and none is
  loosened silently: OOS Sharpe > 0.5, max DD better than −35%, OOS Sharpe
  < 2.5, OOS ≤ IS×1.3+0.5, ≥ 30 trades, IS Sharpe > 0.
  *(Prevents: quietly passing an overfit or under-sampled strategy.)*
- When results look strong, confirm fees+slippage are still charged per side
  (`BACKTEST_FEE_PCT` + `BACKTEST_SLIPPAGE_PCT`, ≈1.1% round trip). A
  frictionless run is only valid if the user explicitly set both to 0.
  *(Prevents: reporting a gross Sharpe as if it were net and greenlighting a
  high-churn loser.)*
- When you present a `PASS`, state it as "not obviously broken," never as a
  profit guarantee, and include the per-segment numbers.
  *(Prevents: a pass being read as a promise.)*
- When someone asks to run validation on a strategy not in the backtestable set
  (`GainzAlgo_V2_Alpha`, `Mean_Reversion_Master`, `Ultimate_Oscillator`,
  `Turtle_Trend`), say so rather than fabricating a result.
  *(Prevents: inventing a backtest for an unimplemented path.)*

**Worked example.** A tweak defaults fees to 0 "for cleaner numbers." A churny
scalp strategy now shows OOS Sharpe 1.8 and passes. Procedure 2 fires: at the
real ≈1.1% round trip its turnover eats the edge and it fails gate 1. Restore
the modelled costs before reporting.

---

## Area 9 — Reporting & API correctness

**Procedures**

- When an API/dashboard number can be derived two ways, confirm it reuses the
  one source of truth already in the code (e.g. `compute_daily_pnl_pct` for
  today's P&L) rather than recomputing it locally.
  *(Prevents: the dashboard and the risk engine disagreeing on the same
  number.)*
- When you report a dollar figure, label which pool it is — real NAV, budget
  cap, or spendable cash — using the matching function from Area 1.
  *(Prevents: the "$250 vs $400" class of report bug.)*
- When an endpoint mutates state (`POST /api/reset`), confirm it is a no-op that
  returns an error in live mode.
  *(Prevents: a "clear mock trades" button wiping real trade history.)*
- When you answer a "why didn't it trade?" question, cite the concrete gate
  that blocked it (daily-loss, position cap, budget, min size, confidence,
  long-only) from `/api/diagnostics` — never guess a reason.
  *(Prevents: a plausible-but-wrong explanation sending the user to fix the
  wrong knob.)*

**Worked example.** A user asks why a BUY didn't fire. The answer guesses "low
confidence." Procedure 4 fires: `/api/diagnostics` shows the symbol already had
an open position, so the one-per-symbol cap blocked it. The confidence guess
would have sent them tuning thresholds that were never the problem.

---

## Final gate — run before sending EVERY answer

Before sending any reply, PR, or change, run this checklist:

1. **Money direction:** does any dollar figure use the right pool —
   `effective_usd_balance` for sizing, `get_account_value` for NAV? (Areas 1, 9)
2. **Order shape:** BUY uses `quote_size`; SELL uses `base_size` through
   `_sell_size` on the live path? (Area 2)
3. **Real-money safety:** no silent paper↔live fallback; money/token env
   defaults are off/safe; no key material surfaced? (Area 3)
4. **Risk gates intact:** long-only, position caps, and realized-P&L daily
   breaker all still enforced before an order? (Areas 4, 5)
5. **Exit truth:** ATR levels take precedence; displayed exit levels are the
   effective ones; one bad position can't freeze the loop? (Area 5)
6. **Claude I/O:** JSON pulled from prose; tool-failure retries without the
   tool; still one batched keyless-capable call? (Area 6)
7. **Data integrity:** lookbacks clamped identically; no NaN/placeholder price
   treated as real? (Area 7)
8. **Validation honesty:** six gates present, costs charged, PASS stated as "not
   broken," not "profitable"? (Area 8)
9. **Claims grounded:** every "why"/number cited from `/api/diagnostics`,
   `/api/validate`, or the source of truth — nothing guessed? (Area 9)

**If any item fails: fix it and re-run the whole checklist from item 1. Never
send anyway.**
