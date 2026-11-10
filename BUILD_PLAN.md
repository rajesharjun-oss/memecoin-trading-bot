# MemeOps continuous build plan

MemeOps is being built as a safety-first Solana memecoin intelligence and paper-trading system.
"Best" is treated as a measurable engineering target, not a promise of profit. Work continues through
the milestones below; live trading remains disabled until every live-readiness gate is satisfied and
the owner gives explicit authorization.

## Non-negotiable constraints

- Paper mode is the default and autonomous live execution is unavailable by default.
- News, social posts, paid boosts, and callouts are evidence, never independent entry authority.
- Contract, liquidity, route, portfolio, and circuit-breaker gates cannot be bypassed by a score.
- Secrets remain local and are never returned by the dashboard, written to logs, or embedded in URLs.
- Every external provider must fail closed or degrade visibly without stopping risk management.
- Every decision must retain its inputs, score contributions, rejection reasons, and resulting action.

## Milestones

| Phase | Deliverable | State | Exit evidence |
| --- | --- | --- | --- |
| 1 | Secure local foundation | Complete | Burner-wallet isolation, localhost pairing, HttpOnly session, CSRF, validation, live gate |
| 2 | Paper trading and risk engine | Complete | Position sizing, partial TP ladder, trailing exits, stop loss, daily/consecutive-loss limits |
| 3 | Discovery and call ingestion | Complete | DEX Screener, official Pump feeds where available, pasted-call queue, paid-boost penalty |
| 4 | Strategy Lab | Implemented; collecting authentic history | Pool-specific 1m/5m/15m/1h OHLCV, stale/gap guards, explainable confirmation, replay tests |
| 5 | Narrative Intelligence | Complete | Bounded news/social evidence, contract linkage, source diversity, promotion/security flags |
| 6 | Wallet and contract forensics | Complete | Exact vault derivation, controller clusters, bounded origin tracing, limitations and hard-risk gates |
| 7 | Execution realism | Implemented; ATA-rent calibration pending | Net fills, quote freshness, slippage/priority/ATA costs, liquidity sizing and forced-shock tests |
| 8 | Model evaluation and tuning | Engine implemented; evidence pending | Frozen walk-forward folds, regime/score diagnostics, ablation and 150% cost stress on authentic data |
| 9 | Resilient operations | Complete | Durable worker state, provider backoff, health telemetry, alert events, verified backups and hardened deployment |
| 10 | Live-readiness review | Locked | Independent security review and every gate below, followed by explicit owner authorization |

Visual design, accessibility, responsive behavior, error states, and operator clarity are reviewed in
every phase rather than postponed to the end.

## Paper validation gates

All of these are required before a live-readiness review. Passing them does not enable live trading.

1. At least 30 calendar days of continuous paper observation spanning quiet, trending, and stressed
   Solana market conditions.
2. At least 1,000 fully recorded candidate decisions and 200 closed paper positions after simulated
   network fees, priority fees, slippage, price impact, and token-account costs.
3. Positive out-of-sample expectancy and a profit factor of at least 1.25; results must remain positive
   when execution costs are increased by 50%.
4. Maximum paper drawdown no greater than 12%, with no single position risking more than the configured
   limit and no breach of daily loss, open-position, liquidity, or concentration controls.
5. Walk-forward and historical replay results must not depend on one token, one narrative, or one short
   market regime. Parameter choices must be frozen before each out-of-sample window.
6. A seven-day unattended soak test with no lost positions, duplicate orders, uncaught worker crashes,
   secret leakage, or silent provider failure.
7. Every forced failure test must be safe: stale quotes, 429/5xx responses, RPC timeouts, malformed
   payloads, unavailable candle data, SQLite contention, and process restart.
8. Lint, formatting, type checks, tests, package build, dependency integrity, vulnerability audit, and
   secret scan must pass on the release candidate.

## Live-readiness gates

- An independent code/security review has no unresolved critical or high-severity findings.
- A dedicated low-balance live wallet and strict on-chain spending boundary are configured.
- Live orders require an expiring server-side preview, a second confirmation, and an immutable audit
  record. Autonomous live entry remains a separate, deny-by-default capability.
- Emergency stop, maximum daily spend, maximum transaction value, provider kill switches, and manual
  position recovery are tested with micro amounts.
- The owner explicitly authorizes the exact live scope after reviewing the paper report and residual
  risks. Until then, `LIVE_TRADING_ENABLED=false` remains mandatory.

## Current engineering focus

The immediate focus is Phase 1 market-data integrity and Phase 2 paper validation. The new
GeckoTerminal collector stores trade-derived candles; old snapshot-derived candles are excluded from
entries and backtests. Historical liquidity must be recorded for the same pool before replay can make
an entry. Replay positions still open at a series boundary or interrupted by a data gap or missing
liquidity are censored rather than booked as wins or losses. The existing four-factor strategy is the
baseline. Proposed indicators remain deferred until this baseline has enough unseen history and closed
paper trades to evaluate. A persisted, score-independent three-hour cohort drawn from
recent discoveries keeps weak and blocked tokens in the research universe; replay entries are confined
to their recorded sample membership windows. This bounded sample still cannot prove an unbiased
launch-wide result. Phase 10 remains locked; `LIVE_TRADING_ENABLED=false` stays in place.

## Candle-feed capacity decision (3 November 2026)

The operator chose to keep this phase free-only. Continue the bounded public
collector and prospective paper run. Do not add a paid stream adapter, subscribe
to a feed, or relax missing-history gates to increase trade count. Revisit feed
capacity only after recording its effect on observed coverage and paper results.

Operations measured about 3.5 successful public pool-OHLCV requests per minute
over an hour. Refreshing the 29 currently market-ready mints every two minutes
needs at least 14.5 calls per minute, before research, follow-up, and higher
timeframes. The existing collector pins a small paper lane and records the
coverage shortfall; it must not report the wider candidate pool as fully
observed or loosen the candle gate to create trades.

Recent public-feed cycles showed the six-call trial could be followed by an
HTTP 429 and zero-request backoff cycles. A quota hit now holds the collector
at the four-call baseline for 24 hours, including when it reloads an older
one-hour cooldown from persisted state. This protects focused paper and
research coverage while keeping the strategy gates unchanged. It does not
solve the wider pool capacity deficit.

The paper audit scanner also reserves freshness for the two currently pinned
candle-observation subjects within its existing eight watchlist re-audit slots.
It requires a current same-pool market snapshot and an unexpired focus pin;
the normal audit and entry gates still decide whether a trade is allowed.

### Execution-cost calibration (3 November 2026)

The forward entry and exit paths use Jupiter's fee-free v2 build route. A
forward buy now measures the output token account's post-simulation lamports
and reserves that amount only on the first simulated paper purchase of a mint.
Existing on-chain ATAs cost no new reserve. The strategy's pre-trade sizing
and historical replay still use a fixed 2,039,280-lamport planning estimate.
The current RPC returns 1,488,440 lamports for a 165-byte original-token
account and 1,513,840 for a 170-byte Token-2022 account. Actual Token-2022
account size can be larger when extensions apply, and an existing ATA needs
no new reserve. The forward entry now fails closed when it cannot measure the
account reserve or when the measured full debit exceeds its risk budget.
The dashboard's rent-inclusive cash P&L and full-rent-reclaim sensitivity
remain distinct accounting scenarios because account closure is not modeled.
Replay now separates price-dependent slippage from fixed fees and reserved
rent, charges the reserve once per mint across a replay or unseen folds, and
stresses only irrecoverable execution friction by 50%. It reports a
hypothetical full-rent-recovery sensitivity on the same cash-based trade path.
Before the paper validation gate can be judged, distinguish locked refundable
capital from irrecoverable execution fees in headline returns, calibrate the
fixed replay reserve to actual account state, and measure repeated purchases
and eventual account closure prospectively.
Do not tune entry rules around the fixed replay planning number.

[CoinGecko's paid OnchainOHLCV WebSocket](https://docs.coingecko.com/websocket/wssonchainohlcv)
is a possible higher-capacity feed. [Its delivery page](https://www.coingecko.com/en/api/data-delivery)
lists WebSocket access on Basic and above and charges 0.1 credit per response;
[the pricing page](https://www.coingecko.com/en/api/pricing) lists 100,000
monthly credits for Basic. Even a hypothetical *one* response per minute for
30 pools over 30 days would be 1,296,000 responses, or 129,600 credits, before
any REST use. Active pools can emit more often, so the Basic plan is not yet
proven sufficient. Subscription limits and real response volume must be checked
before choosing a paid tier or building a cost forecast. No paid feed is
connected or authorized. The OpenAI key only supports optional research notes
and cannot increase candle throughput.

If a paid authenticated stream is selected in a later phase, implement it behind an off-by-default
configuration switch and preserve the public REST collector as a bounded
fallback. Before allowing its candles into paper signals, verify exact
mint/pool/token-side identity, accept only fully closed intervals, persist the
first local receipt time and provider origin, reject conflicting duplicate
values, and treat reconnect gaps as missing history. Keep subscription changes
and response credits in durable health records; enforce an operator-set daily
credit cap before connecting. Replay may use a candle only after its recorded
first observation, and the same stale/gap, safety, and risk gates must apply to
both sources. Tests must cover null fields, identity mismatch, late updates,
out-of-order and conflicting messages, reconnects, quota exhaustion, and
fail-closed entry behavior. Paper performance must then be measured prospectively
before changing any strategy threshold.
