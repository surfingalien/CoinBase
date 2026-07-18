# Agent safety: the untrusted-input boundary (R10)

The trading engine mixes trusted internal data (our own indicators, rule
verdicts, risk math) with **third-party text we do not control**: RSS news
headlines, TradingView webhook payload fields, and live web-search results.
That third-party text reaches the Claude analysis and reword prompts. This
document states the rule that governs it and where it is enforced.

## The rule

> **R10 — Input from a third party is data, never instruction.** It informs the
> reasoning; it can never, by itself, authorize or trigger a consequential
> action.

This is the trading engine's version of the Automaton constitution's Law III
("guard your reasoning against manipulation; obedience to strangers is not a
virtue"). Our system implied it — the rule-based gates in `ai_engine` and
`risk` are the sole authority on every trade, and the LLM can never turn a
`REJECT` into an `EXECUTE` — but nothing used to sanitize the text flowing
*into* the prompt. R10 closes that gap.

## Where it is enforced

`backend/app/injection_defense.py` is the single chokepoint. Every external
string is run through `sanitize_external_text` (neutralizes chat-role/turn
markers, prompt-boundary tags, tool-call syntax, our own fence tokens,
zero-width/control chars; size-caps) and fenced with `wrap_untrusted` inside
`<untrusted-external-data>…</untrusted-external-data>`. Every prompt that
carries external text also embeds `UNTRUSTED_INPUT_RULE`, which tells the model
that anything inside those fences is information to weigh, never a command.

| Entry point | External source | Applied in |
|---|---|---|
| News headlines | Unauthenticated RSS feeds | `sentiment.prompt_section` |
| Analysis prompt | Headlines + F&G + web-search results | `market_analysis._batch_prompt` |
| Reword prompt | TradingView webhook payload fields | `ai_engine._refine_with_llm` (via `sanitize_signal_for_prompt`) |

## Defense in depth

Sanitization is not the gate — it is the first layer. Even if a crafted input
slipped a suggestion past the model, the deterministic rule gates still decide
every trade, exits are sized to the exact position, and the hash-chained audit
trail records each decision. `injection_defense.scan` additionally logs when a
high-risk pattern is seen, so attempts are visible rather than silent.

## Metabolism / survival economics (built)

The economic "metabolism" layer is now in `backend/app/metabolism.py`: it meters
what the automaton costs to run (LLM token spend + amortized infra; trading fees
are already netted out of realized P&L), computes **runway** (days of cash left
at the current net burn), and sets a **survival tier**. The
`survival_monitor` loop recomputes it on an interval and records every tier
change to the audit chain; `GET /api/metabolism` exposes the live picture.

The tiers are the two-sided response Conway's `low-compute.ts` describes,
adapted to our stack:

Runway is judged on **equity** (liquid cash + open position value): deployed
capital is sellable, so moving cash into positions never reads as approaching
death.

| Tier | Trigger | Effect |
|---|---|---|
| `sustainable` | earns ≥ it burns | full behaviour |
| `stable` | burning, long runway | full behaviour |
| `low_compute` | runway ≤ `survival_runway_low_days` | **shed cost**: cheaper model, slower heartbeat |
| `critical` | runway ≤ `survival_runway_critical_days`, or zero equity | shed cost **and damp entries to 50% size**; alert |

A hard entry **halt** applies only when liquid cash can't fund a minimum
order — an entry is then physically impossible. A short runway alone never
halts trading: entries are the organism's only revenue source, so halting
them would lock a burning account into certain death (a self-lock found by an
Opus 4.8 review of the original design, which halted at critical). The
survival response targets the actual cash drain — LLM burn — via shedding,
and trades smaller rather than not at all.

This is the honest embodiment of "if it cannot pay, it stops" — *stops* means
stops **spending** (cheaper inference, slower heartbeat, smaller bets), never
self-deletion. Exits always run, and a human can always intervene.

## Not yet ported

- **Loop detector** — guards an agentic planner that chooses tool calls in a
  loop. Our pipeline is deterministic (`signal → gates → reword → risk →
  order`); the LLM never selects actions. Worth porting only if/when an
  agentic action loop is added.

## Deliberately not built

Autonomous **self-replication** (provisioning infra and spawning copies) and
autonomous **self-modification-and-redeploy** with the human removed from the
loop. The human merge-gate is the only point where a bad edit or a runaway loss
is caught before it propagates. Self-improvement here stays **gated**: the bot
proposes changes as pull requests and waits for a human to merge — which is how
the trade-execution fix and this layer both shipped.
