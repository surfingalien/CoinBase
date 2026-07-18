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

## Not yet ported

Two Automaton defenses were evaluated and deferred because they guard
subsystems this repo does not have:

- **Loop detector** — guards an agentic planner that chooses tool calls in a
  loop. Our pipeline is deterministic (`signal → gates → reword → risk →
  order`); the LLM never selects actions. Worth porting only if/when an
  agentic action loop is added.
- **Cost-shedding survival tiers** — need the economic "metabolism" layer
  (cost tracking → runway → tiers) that does not exist here yet. The
  burn-reduction response drops in naturally once that layer is built.
