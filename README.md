# Memecoin Trading Bot

Development follows the measurable, paper-first milestones in [BUILD_PLAN.md](BUILD_PLAN.md).
Live submission is hard-locked in this research build; environment switches and CLI flags cannot
override it. See [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) for the measured state.
The Overview, Strategy Lab, and Discovery screens' reduced-noise UI decisions are recorded in [UI_RESEARCH.md](UI_RESEARCH.md).

A security-first Solana token scanner and Jupiter swap executor. It audits SPL mints,
discovers candidate mints from selected on-chain programs, builds transactions from raw Jupiter
instructions, simulates them, and can manage a take-profit/stop-loss position.

The bot is **paper-only in this build**. No third-party bot platform fee is added,
but normal DEX fees, Jupiter routing fees (when applicable), RPC costs, priority fees, rent,
slippage, and price impact still apply.

> Memecoin trading can lose the entire position. Revoked mint/freeze authority and holder checks
> do not prove a token is safe. Programs can be malicious, liquidity can disappear, related wallets
> can evade concentration checks, quotes can become stale, and stop orders can fail to land.

## What is included

- Jupiter Swap V2 `/order` and `/build` are preferred; the official v1 quote and
  `/swap-instructions` endpoints are used as a bounded fallback when v2 routing is unavailable.
  No platform fee account is supplied.
- Strict instruction validation, no unexpected signers, and no validator-tip instruction.
- Token Program and opt-in Token-2022 audits with dangerous extension blocking.
- Integer-only supply, balance, SOL, and PnL calculations.
- Double transaction simulation, capped compute units and priority fee, RPC preflight, and
  confirmation polling.
- SQLite idempotency journal, daily new-exposure cap, and persistent position state.
- One-token audit, batch scanning, bounded program-traffic discovery, and a continuous watcher.
- Position monitoring with one-time partial take-profit and full stop-loss actions.
- Responsive local dashboard for wallet health, audits, trade previews, positions, activity, and
  read-only risk limits.
- JSON output suitable for logs and process supervision.
- Unit tests, a non-root container, and a hardened systemd example.

## Accounts and credentials

You need:

1. A dedicated Solana burner wallet. Prefer generating a new Solana CLI keypair and funding its
   public address; do not export or reuse your primary Phantom/Solflare wallet key.
2. An HTTPS Solana mainnet RPC endpoint from Helius, QuickNode, Chainstack, or another provider.
3. A Jupiter Developer Platform API key for authenticated swap requests.

Optional: a VPS for 24/7 monitoring. No Birdeye or Dexscreener account is required by the included
on-chain discovery adapter. Discovery requires you to provide the launchpad/program IDs you have
independently verified.

Never paste a seed phrase or private key into chat, source code, `.env`, command arguments, logs, or
a cloud dashboard. The application accepts only a standard 64-byte Solana CLI JSON keypair file.

## Installation

Python 3.11-3.14 is supported.

```powershell
cd memecoin-trading-bot
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip==26.2.1 setuptools==84.2.0
.\.venv\Scripts\python -m pip install -c requirements.lock -e ".[dev]"
Copy-Item .env.example .env
```

On Linux/macOS, activate with `source .venv/bin/activate` and use `cp .env.example .env`.

Create a burner keypair with the official Solana CLI:

```bash
solana-keygen new --outfile /secure/path/memecoin-bot-keypair.json
solana-keygen pubkey /secure/path/memecoin-bot-keypair.json
chmod 600 /secure/path/memecoin-bot-keypair.json
```

Fund only the public address with an amount you can fully lose. Keep enough SOL outside active
position sizing for network fees and token-account rent.

Copy `.env.example` to `.env`, then set:

```dotenv
SOLANA_RPC_URL=https://your-provider.example/?api-key=REDACTED
SOLANA_KEYPAIR_PATH=C:\secure\memecoin-bot-keypair.json
JUPITER_API_KEY=REDACTED
LIVE_TRADING_ENABLED=false
```

Do not commit `.env`, the keypair, the SQLite database, or logs. For deployment, inject secrets from
a managed secret store and mount the keypair read-only.

## First run

Validate configuration and RPC health without trading:

```bash
memecoin-bot check
```

Audit one mint or a file containing one mint per line:

```bash
memecoin-bot audit --mint MINT_ADDRESS
memecoin-bot scan --file mints.txt
```

Discover candidate mints from recent transactions touching verified program IDs:

```bash
memecoin-bot discover --program-id VERIFIED_PROGRAM_ID --signatures 20
```

This is candidate extraction, not authoritative launch detection. Every candidate must still pass
the audit and executable Jupiter route checks.

## Local dashboard

Launch the browser interface from the project folder:

```powershell
.\.venv\Scripts\memecoin-bot.exe dashboard
```

The command:

1. Starts the dashboard on `http://127.0.0.1:8787` only. It is not exposed to your network.
2. Prints a startup pairing code in PowerShell.
3. Opens the pairing screen in your browser. Paste the startup code for an eight-hour,
   HttpOnly, SameSite session. On your own computer, select **Remember this browser**
   for a session that survives routine dashboard restarts. Remembered sessions
   expire after 14 days or 7 days without use, whichever comes first.

Remembered session IDs are stored only as SHA-256 hashes in a separate local
`*.dashboard-auth.sqlite3` file beside the active trade journal. The pairing
code and CSRF tokens are never stored there. **Sign out** revokes the current
browser; **Sign out all browsers** revokes every remembered browser. If the auth
file is lost, the browser simply needs to pair again. A new paper campaign uses
a different auth file and requires one new pairing. The dashboard remains bound
to `127.0.0.1`; remembered sessions do not enable live trading. Restarting the
dashboard changes the code for new pairings but does not revoke remembered
browsers; use **Sign out all browsers** if you need to revoke them.

The dashboard uses the same server-side audit, sizing, price-impact, slippage, daily-exposure,
simulation, idempotency, and live-execution gates as the CLI. It does not receive or display your
private key. Sensitive `.env` values are never returned by the dashboard API.

Use `--no-browser` if you do not want it to open a tab automatically, or choose a different local
port with `--port 8788`. Stop it with `Ctrl+C` in the PowerShell window that launched it.

The normal workflow is:

1. Open **Overview** for the paper decision desk. The current action appears first;
   position capacity, marked paper equity, complete autonomous exits, and worker
   health follow. Expand **Decision evidence** for the data behind that action.
   Cached or missing marks are labelled as estimates. When an open-position
   quote is old, Paper Portfolio also shows equity with those old marks valued
   at zero as a stress scenario. Expand **System checks and
   wallet details** for RPC, wallet, Jupiter, database and backup diagnostics;
   historical live-order records are in a separate collapsed journal.
2. Run a bounded **Discovery** scan. The scanner round-robins candidates from the official
   Pump.fun New callouts API when `PUMP_API_KEY` is configured, verified Pump/PumpSwap program
   traffic, and DEX Screener's documented profiles, latest/top boosts, and community-takeover
   feeds. Candidates are audited and enriched with their highest-liquidity DEX pair when available.
   **Hot Radar** then ranks observable 5-minute flow, transaction acceleration, liquidity,
   market-cap range, momentum, and pair age. Paid boosts are disclosed as attention risk and add
   zero score.
   Discovery puts the **Audit passed** shortlist first. Its compact rows keep audit, score,
   decision, source, liquidity, volume, and **Review in Paper** visible together; on phones the
   rows become cards. Select **All scanned** to inspect blocked, rejected, and pending tokens.
   An audit pass is a shortlist gate, not a paper-entry signal. Open **Market watch** for Hot Radar
   and narrative evidence, or the decision history when you need those details.
3. Send a passed candidate to **Paper Portfolio**. Opening a paper position runs
   the real audit, Jupiter route, transaction build, and Solana simulation; it never broadcasts.
   Paper exits use a fresh Jupiter exit quote because simulated paper tokens do not exist in the
   wallet and therefore cannot honestly pass an on-chain balance simulation.
4. Use **Run Paper Decision** to have the explainable strategy engine score every candidate, enforce
   liquidity/volume/manipulation and portfolio gates, size the highest eligible entry, and attempt a
   real route simulation. Every rejection, observation, failed simulation, and paper entry is logged.
   When a portfolio circuit breaker blocks entries, the worker still records candidate signal
   decisions. An otherwise eligible candidate is labeled with the breaker reason; no paper entry
   is attempted. These are observations, not simulated fills or realized performance.
5. Refresh paper exit quotes to track simulated cash, equity, realized P&L, and unrealized P&L.
   Every paper entry commits its position, buy fill, and linked entry decision in one
   SQLite transaction. The same transaction rechecks the shared paper circuit breakers,
   open-position limit, and duplicate-token rule after quote generation, so concurrent
   requests cannot bypass those portfolio limits.
   A saved recent mark is labelled as a cached mark; only a newly fetched quote can
   trigger stop-loss or take-profit rules. Quote outages are logged once per episode,
   escalated after three cycles, and shown as degraded operational health. Positions
   without an executable exit quote remain open rather than receiving an invented fill.
    Exit quotes and stop/profit rules run in a separate paper monitor scheduled
    two minutes after each completed check, so a long candidate audit cannot
    delay the next exit check. New automated
    entries fail closed if that monitor is stopped, stale, or reporting an exit
    or quote failure. The console shows the exit monitor separately from discovery.
    Worker start and completion times are persisted separately, so operations
    health can show an ongoing scan before its result is available. The same
    exit-monitor freshness rule blocks entries and marks operational health
    degraded when checks are overdue. Transient
   SQLite state-write failures are retried and logged without sensitive data.
   Open positions appear below those totals. Stale marks turn the total P&L into a warning-colored
   estimate. **Forward paper evidence** counts only fully closed autonomous trades with recorded
    current-version market-policy inputs, candle signals, cost evidence, and
    costed exit marks throughout the position's life. A gap longer than two
    exit-monitor intervals plus two minutes excludes that closed trade from
    forward performance while retaining its fills in the audit ledger;
    call-driven, manual and incomplete trades are excluded and counted
   separately. Open positions that lack full entry evidence are identified in advance and will
    be excluded when closed. Expand its detail for profit factor, expectancy, holding period and loss reasons.
    Forward paper entries measure a newly created output token account's rent
    from the signed dry-run simulation; an existing on-chain account or a mint
    already bought in the paper ledger adds no new reserve. Missing or malformed
    simulation account evidence blocks an autonomous forward entry. Pre-trade
    sizing and historical replay still use the configured fixed reserve as a
    planning estimate, so their cost calibration remains limited.
    Replay applies modeled slippage to proceeds and subtracts network fees and
    rent as fixed outlays relative to the original swap size. It reserves rent
    only on the first simulated purchase of a mint, including across unseen
    walk-forward folds. The 150% execution-cost stress leaves refundable rent
    unchanged. A separate full-rent-recovery result uses the same cash-based
    trades and breaker path; it is a hypothetical sensitivity, not realized
    cash or independent strategy performance.
    Closed-trade cash P&L includes token-account rent reserved on entry. The forward
    performance API also reports a separate **hypothetical full rent recovery**
    sensitivity, but the paper worker does not close token accounts or record
    rent refunds. Do not interpret that sensitivity as realized profit.
   The read-only verifier reconstructs each linked paper entry's four timeframe
   signals from candles first observed by its decision time and compares them
   with the saved decision. New entries record the candle threshold and
   liquidity floor used for that comparison; older entries without those
   settings are labelled as using the current-configuration fallback.
   Expand **Execution Reality** below the entry form to review modeled costs.
    **Real wallet SOL** under system checks is the on-chain wallet balance.
    **Estimated paper equity** is simulated cash plus saved exit marks; it may
    rise or fall without a sale. Use **Net realized P&L** under Forward paper
    evidence to see the result of completed autonomous paper trades.
     If an open position has no valid exit mark, its value and total paper
     P&L are shown as unavailable. The backend retains cash plus known marks
     as a conservative risk bound and pauses new entries until the missing
     mark is resolved. A rising quote is not a net gain until estimated exit
     proceeds exceed the full modeled entry cash cost.
    New paper positions price buys and exits from Jupiter's fee-free `/build`
    router, matching the transaction the bot simulates. Older positions retain
    their original quote path and are excluded from the new run's comparable
    route-parity performance sample.
     Autonomous and call-inbox entries in a clean forward campaign require
     the fee-free built route; a fallback quote rejects the paper entry.
     Historical candle verification chooses only provider bars observed by the
     decision time, so a direct bar fetched later cannot replace an earlier
     complete 1m-derived bar in the saved entry signal.
 6. Start **Autonomous Paper Worker** to run independent exit monitoring and
    discovery/decisions on schedule. The exit monitor applies take-profit,
    stop-loss, trailing-stop, and maximum-hold rules using fresh exit quotes.
    The discovery worker applies portfolio breakers before considering at most
    one new paper entry per cycle.
7. Paste up to 5 contract addresses into **Call Inbox**. Each call must independently pass the same
   audit, market-data, scoring, sizing, position-limit, route, and simulation gates; a pasted call is
   a lead, never a buy command.
8. Run **Token Audit** using the exact contract address when investigating a token manually.
9. Create a buy or sell simulation in **Trade Console**.
10. Review expected output, price impact, router, and compute units.
11. Keep `LIVE_TRADING_ENABLED=false`. This research build rejects all live submissions even if that
    value or a CLI execution flag is changed.

Discovery is intentionally bounded to protect RPC quotas. The dashboard permits one scan every 30
seconds, reads no more than the configured transaction cap, and stores candidates in the local
SQLite journal. A passed audit does not establish ownership distribution, LP safety, profitability,
or legitimacy. DEX Screener enrichment is informational and can be unavailable or stale.
Each paper discovery cycle uses spare capacity for up to eight safe watchlist re-audits.
Candidates with a fresh, matching pool snapshot that already clear the market gate are checked
first; remaining slots go to the oldest audits. New discoveries and the separate research cohort
keep their audit allocation. An audit failure leaves the earlier result stale, and no entry uses
that result as a fresh pass. The worker status records new-discovery, cohort, watchlist, and
priority re-audit counts so the extra RPC use can be measured.

### Start a clean forward paper run

The paper dashboard stores trades and research in the SQLite file selected by
`TRADE_STATE_PATH`. To begin a new performance trial without mixing in earlier
positions, first stop the dashboard, then create a new journal:

```powershell
.\.venv\Scripts\memecoin-bot.exe new-paper-run --output .\forward-paper.sqlite3
```

The command copies timestamped discovery, audit, market, candle, and narrative
observations so the strategy has historical context. It starts with no paper
positions, fills, decisions, worker health, or old performance results. It
refuses to overwrite an existing file. Point `TRADE_STATE_PATH` in `.env` to
`./forward-paper.sqlite3`, keep `LIVE_TRADING_ENABLED=false`, and restart the
dashboard. **Overview** and **Paper Portfolio** identify the active run. Keep
the prior journal for comparison. A new run begins with zero qualified closed
autonomous trades; allow forward data collection before assessing performance.
If a forward campaign stopped while paper positions were open, first archive
it with `interrupt_forward_paper_run` after stopping its dashboard. This saves
the last completed exit-monitor cycle and an interruption event for each open
position without creating an exit fill. `new-paper-run` refuses an unclassified
forward campaign with open positions. The previous journal remains available
for audit; the new journal excludes its positions and performance.
If a campaign later reveals a long unmonitored interval while positions were
open, `archive_unmonitored_forward_paper_run` records the affected closed
positions before a new run. It preserves their simulated fills for review.

### Research history and replay

The market collector keeps a three-hour prospective research sample of up to 20 mints chosen by a
stable hash from discoveries first seen in the preceding hour, within the 100 most recent records.
Selection ignores token score and audit result,
so blocked tokens remain eligible for the sample. Open paper positions are tracked separately.
The collector makes at most two DEX Screener snapshot requests of 30 addresses each, putting
open positions first, followed by audited-safe paper candidates that currently pass the market
gate, then the independent research cohort and remaining safe watchlist. This supplies
point-in-time pool liquidity to active candidates even when they appear below the first 30 safe
discoveries, while preserving room for the score-independent cohort. The collector reports
attempted and successful DEX requests and how many priority and cohort mints were included.
One failed batch does not discard the other. A candidate outside the bounded 60-address request
can still lack timely liquidity; replay and paper entry reject missing evidence.
Each mint's actual selection time and membership window are stored in SQLite; replay can enter
only after discovery and within a recorded sample window. The public OHLCV
baseline allows four calls at 15-second spacing; after 12 fresh network
responses within ten minutes with a sizable ready backlog, the collector can
cautiously try six calls at 10-second spacing.
New cohorts concentrate 1m collection on their first two score-independent mints.
An existing cohort keeps its legacy eight-mint focus until its three-hour window expires;
this avoids changing its data-collection policy mid-sample. All 20 selected mints remain in
the replay denominator, including pools without usable history. The current cohort's
`focus_size` is persisted in runtime state and shown in Strategy Lab and the verification
report. The two-mint policy is a capacity experiment, not evidence of improved returns.
When a complete current DEX market record reports both zero 5m volume and zero
5m transactions, and no authentic 1m trade bar was seen in the last five
minutes, the collector defers that pool's OHLCV request. Its recorded research
membership and past bars stay intact. A later DEX activity change restores
collection, while missing DEX activity fields do not trigger a skip.
Separately, the collector prospectively selects one audited mint that passes the market gate on
the latest saved values, by stable hash, and holds it for 60 minutes even if its market verdict
later changes. This allows eight fresh 5m confirmation bars to mature within one follow-up
window when the pool continues trading. Its selection time,
population count, expiry, and method are saved in `decision_followup_memberships`. One 1m call
is reserved for this mint each cycle when the pool is available, active by the
above rule, and outside provider cooldown.
Other slots rotate through active candidates and the independent research cohort. This selected
follow-up is **not** part of the unbiased replay population merely because it was selected;
its later price path is descriptive and cannot prove net trading performance. At the four-call
quota, a normal cycle leaves one research call while rotating other active candidates.
All market-ready paper candidates stay in the oldest-fetch rotation when their
pools show current activity; the scheduler does not freeze this lane at its
first four mints.
Strategy Lab shows a compact current entry funnel using the same market, audit, pool,
and candle gates as paper automation. The read-only verification report includes the
full `current_entry_readiness` breakdown by timeframe. Counts are operational data
readiness, not a backtest or proof of profit. The public OHLCV request budget
caps the trial at six calls per cycle. It grows only after 12 fresh provider
responses within ten minutes. Cached responses do not count; empty or stale
pages reset the evidence window.
An HTTP 429 restores four calls at 15-second spacing and delays another trial
for an hour.
The read-only verification report includes `decision_followup_price_paths`. It resolves the
pool from a market snapshot captured no later than selection time and requires that snapshot
to be at most 120 seconds old. It then checks continuous, authentic future 1m bars for separate
5m and 15m price-only outcomes. Pending, missing, stale, and gapped paths stay in the sample.
An observed path is not a simulated fill or a net strategy return. Earlier 30-minute selections
retain their recorded expiry; outcome counts are also grouped by selection method so the two
collection regimes are distinguishable.
The same read-only report also includes `market_eligible_followup_replay` for
the latest 20 prospectively selected market-eligible subjects. It fixes the
pool from a market observation available at selection time, then runs the
baseline candle, audit, market, cost, and paper-style exit replay only inside
the recorded selection window. Missing or gapped data remains missing or
censored. This is a conditional sample of already market-eligible tokens;
keep it separate from the score-independent launch cohort. Its modeled exits
do not establish parity with actual Jupiter fills or paper decision timing.
An active selection window is labeled `collecting` even when the partial replay
has no remaining simulated position; only an expired window can be labeled
`complete`. The report includes the number of collecting and expired windows.
One of every eight cycles collects direct 15m and 1h bars for
an active candidate. The research cycle keeps the fixed follow-up call and
uses its remaining public quota for focus and secondary cohort mints, with
up to two slots for the secondary cohort. Those research calls alternate
focus and remaining mints where available. This keeps the full
20-mint membership record without implying that the public quota can provide
timely bars for all 20. Nonfocused cohort mints remain in discovery, snapshot,
audit, and missing-history reports; their replay may still have insufficient data.
The previous 24-hour cohort memberships remain stored for audit but are excluded
from the new sample's performance calculations. A replayed mint must have a
recorded membership in the current sampling method; merely appearing in the
safe watchlist does not make it part of the research return population.
Normal requests put the fixed follow-up first, then rotate active and research mints.
Provider cooldown can prevent a follow-up request, and no-trade pool intervals may leave gaps.
The worker records attempted and successful OHLCV client-call counts, cache hits,
its lane, and a bounded provider-error count without saving response bodies.
The capacity warning uses only newly measured cycles and subtracts cache hits
from successful calls. Attempted client calls can still undercount HTTP retries.
An empty response or invalid pool data gets a five-minute retry delay keyed to
the pool and timeframe; a missing or mismatched pool gets 15 minutes. This
preserves the token in the research sample and reports its missing history
while allowing other sampled tokens to use the public quota.
When a nonempty response contains only candles whose newest close is more than
five minutes old for 1m data, or more than two bar periods old for longer
frames, that pool and timeframe get a 15-minute retry delay. The old bars and
cohort membership remain recorded; the collector reports the count of stale
responses separately from provider errors and empty responses.
The default interval between market collector cycle starts is 30 seconds. A
network-bound cycle longer than that gets a one-second minimum pause before the
next start; provider request pacing and persisted 429 cooldown still apply.
Complete groups of five closed 1m
trade candles supply missing 5m bars on read. Existing direct 5m provider bars take
precedence; a missing minute cannot produce a derived bar. Entry confirmation waits
when its most recent eight 5m bars mix direct and derived provenance. The read-only
verification report measures disagreement where both versions of a 5m bar exist.
If any open, high, low, or close price differs by more than 1% in the recent
confirmation window, the paper signal is held for data review across 5m, 15m,
and 1h bars. The separate close-price difference remains available for diagnosis.
This stricter replay policy uses provenance version v17 and excludes older saved results
from current-policy performance comparisons. Replay applies a direct/derived
conflict only after both candle versions were observed locally. For legacy rows
without first-observation provenance, it uses the later persisted fetch time
as a conservative known-by timestamp; a later backfill cannot retroactively
change an earlier entry decision.
Known 15m or 1h price conflicts, mixed direct/derived bars, revised bars, and unverified
values block a new paper entry. A higher frame still collecting bars remains
optional so a new token is not required to have an hour of trading history.
When a repeated provider response changes a previously closed candle, the journal
keeps the first observed OHLCV values and records when the revision was first
detected. Affected recent bars block new entries from that observation time;
earlier replay decisions are not retroactively changed. Revisions that occurred
before this additive migration cannot be recovered from old records.
The read-only verification report includes `candle_revisions`: detected changed
bars, affected pools, timeframes, and median detection delays. It counts only
changes observed on repeated fetches; bars fetched once and earlier revisions
are unknown, so the count is not a provider error rate.
The collector also retains the largest observed OHLC price change in basis
points and the largest volume change in USD for each bar relative to its first
stored values. These measurements begin when the additive columns are installed;
older detected revisions have no trustworthy magnitude until fetched again.
No measurement relaxes the entry gate.
For bounded historical outcome research, `memecoin-bot research-backfill --mint
<saved-mint> --pool <saved-pool> --start <unix-seconds> --end <unix-seconds>`
retrieves up to three pages of authentic 1m pool candles using the provider's
[`before_timestamp` cursor](https://coingecko-api-v3.readme.io/v3.0.1/reference/pool-ohlcv-contract-address).
The command accepts only a saved research mint and its recorded pool, a range
inside its selection plus configured holding horizon, and at most five provider
requests over a 24-hour range. For the default public provider, stop the dashboard
collector while running it so two processes do not compete for the same quota.
An optional CoinGecko Demo
route uses the same pool OHLCV data under a separate authenticated quota:
add `COINGECKO_DEMO_API_KEY` to `.env`, then add `--provider coingecko-demo` to
the command. [Create a free Demo key](https://support.coingecko.com/articles/9471335603-user-guide-how-to-sign-up-for-coingecko-demo-api-and-generate-an-api-key)
from CoinGecko's API pricing page, open the Developer Dashboard's API Keys tab,
and select **Add New Key**. Keep the key only in `.env`; do not paste it into
the dashboard, a screenshot, or a chat. The [Demo plan](https://www.coingecko.com/en/api/pricing)
currently caps usage at 10,000 calls/month, so this option is bounded research
backfill, not a continuous candle feed. Both providers use a fixed HTTPS host;
the command still enforces the saved mint/pool, research window, and five-call
maximum. Backfilled candles retain their actual late first-fetch time: they
cannot establish an earlier entry or
repair missing contemporaneous paper quotes. They can support clearly labeled
retrospective price-path studies after enough outcome and liquidity data exist.
Open positions remain in the quote-based exit monitor and use spare candle
collection slots when available. A DEX Screener outage can use an
already recorded pool address for candle collection. The Strategy Lab shows
sample size, candle coverage, and the last collection cycle's provider error count.
It also compares trusted 1m bars captured within two minutes of closing with
elapsed minute slots for the current research focus and remaining cohort. The
count uses each mint's currently recorded pool and actual cohort membership
window. A missing bar can mean no pool trades or an unavailable provider, so
this measures evidence availability rather than a trading success rate.
Within the fixed, score-independent cohort, the bounded OHLCV scheduler now
tries members with current five-minute DEX trades before inactive members.
Pools with explicit zero five-minute activity are deferred until activity
returns. Active secondary-cohort members keep a request slot when both focus
pools are inactive. The worker summary reports eligible and scheduled active
research members separately from all active members.
Because the public feed has only supported about four calls per minute, the
collector pins up to two audited, market-eligible candidates for one hour of
repeated 1m collection. It prefers currently active pools and rotates to
previously unpinned candidates after the hour, or sooner when a candidate
stops meeting the current market or audit criteria. A known revision in a
recent 1m, 5m, 15m, or 1h confirmation window also frees its focus slot for
another market-eligible pool. The collector records which active candidates
were excluded on this data-quality basis. This helps build the
continuous bars required by the live paper entry gate. The independent
research cohort remains unchanged; performance reports must still disclose
that only a small subset received intensive observation.
The occasional 15m/1h direct-pool request prioritizes these pinned candidates
so their higher-frame checks can use a current, consistent candle source.
When a pinned candidate has a complete recent 1m series but its required 5m
series has a gap, mixed provenance, or stale bars, the collector can replace
one secondary 1m request with a direct 5m pool OHLCV request. It keeps both
pinned 1m requests and any scheduled independent research pin. When all four
free slots are occupied, it may defer one decision-follow-up 1m request;
the worker records that missed follow-up. At most one repair is selected in
any five-minute window, and the same pool is not fetched again within five
minutes. Direct bars carry their actual first-observed time;
the normal candle integrity and entry gates still decide whether they are
usable. A known overlap disagreement above 1% prevents another repair
request while that bar remains in the eight-bar signal window. The worker
reports `direct_5m_repair_scheduled` for each cycle and
saves each repair's reason, displaced request, and observed result.
One active member of the fixed research cohort also receives a repeated 1m
request for up to an hour. Among members with current 5m trades, this lane
prefers a contemporaneously market-eligible audited token, then another safe
token, then any active member. Its selection tier and ended windows are saved
separately from cohort membership. This improves data coverage for conditional
replay, but it does not make the intensively observed subset representative of
all launches. Reports retain every sampled mint, including inactive and
unobserved members, and disclose missing exit history.
This is an activity-conditioned *observation schedule*, not a change to cohort
membership or a trading signal. Performance summaries must include all sampled
members and disclose missing bars; results from only observed active pools are
not a population-wide return estimate.

This is a bounded discovery sample, not a complete launch universe. Missing pools, source outages,
unobserved liquidity, candle gaps, and tokens that disappear before discovery remain research
limitations. The replay censors positions with incomplete exit history and reports unavailable
statistics when there are no closed trades. Run `python -m memecoin_bot.verification` for the
current read-only coverage and performance evidence.
The collector stores at most 120 recent cycle summaries in its runtime state.
The report's `ohlcv_collection_capacity` section shows observed successful
requests per minute once at least two minutes have elapsed, rate-limit and
zero-request cycles, and the theoretical minimum calls needed to refresh every
currently market-ready mint once per 120 seconds. That minimum excludes the
independent research sample, decision follow-ups, and higher timeframes; it is
not a provider quota or a guarantee of complete entry coverage.
When at least two minutes of observations show fewer successful calls than
that minimum, operations health marks the collector degraded and the Overview
action card names the capacity gap. The signal is advisory: entry eligibility
continues to depend on complete, current candles for each token.
For replay mints that reach decision evaluation, the report includes the
number of candidate entry decisions inside their recorded research windows and
a count by first blocking gate (for example, stale 1m data, a 5m gap, an
unconfirmed signal, or an unsafe or stale audit). These counts diagnose absent
trades; they are not counterfactual profit estimates.
Its primary replay population uses recorded research-cohort memberships only;
safe-watchlist candles appear in coverage diagnostics without entering the
research return population. Mints without 1m history are counted explicitly.
The journal records the first time each new candle was fetched, separately
from later refreshes. Older rows have unknown first-fetch time. The verifier
reports this coverage because retrospective bars do not prove the bot received
them promptly enough for an actual paper entry.
The collector's candle count is the number of newly stored candle rows in that
cycle. Re-fetching an existing historical page does not increase it; the
separate request counters show provider activity.
The replay rejects timestamps claiming a candle was fetched before it closed.
It decides only from bars observed before the decision second, so a bar first
fetched just after its close is normally usable at a later decision. A modeled
buy fills no earlier than the following bar open. An exit bar fetched or first
observed more than 120 seconds after its close censors the trade instead of
assigning a historical stop or take-profit fill. The research verifier,
Strategy Lab, and walk-forward evaluator sample observed closed candles at
the configured paper-worker interval, call the same typed stop, TP ladder,
trailing-stop, and max-hold policy as the forward worker, and model fills at a
later candle open. Missing exit marks, candle gaps, and thin exit liquidity
censor the trade. The earlier intrabar OHLC model remains callable as a
sensitivity test. A replayed next-open fill checks liquidity from the last
observed mark; it cannot use liquidity attached to the fill candle that was
recorded later. Intrabar sensitivity checks only prior observed bars for
liquidity known when that candle opened. Polling replay remains a proxy: historical candles cannot
reconstruct Jupiter route quotes, exact scheduling, or actual fills. Its
`paper_exit_rule_parity` flag is true, while the
`paper_exit_model_parity` promotion gate remains false.
The verifier separately reports prompt first fetches and bars that were both
first-observed and fetched promptly; only the latter have complete provenance.

### Pump.fun and DEX Screener integration

- DEX Screener profile, boost, community-takeover discovery, and pair enrichment are keyless and
  enabled by default. Disable these discovery feeds with `DEXSCREENER_DISCOVERY_ENABLED=false`;
  pasted calls still use pair enrichment.
- Pump/PumpSwap on-chain discovery is always available through the configured Solana RPC.
- The official Pump.fun developer API is optional and server-side only. Apply at
  `https://developers.pump.fun`, create a key after approval, and set `PUMP_API_KEY` in `.env`.
  The key must never be placed in JavaScript or sent to the browser. If it is absent or the API is
  unavailable, discovery continues through the verified on-chain programs.
- DEX Screener profiles and Pump.fun callouts are discovery leads, not trust signals. Neither
  source can bypass contract, liquidity, score, portfolio, route, price-impact, or simulation gates.
- DEX Screener boosts are paid visibility. The bot never treats a boost count as social proof,
  safety, or entry conviction. Hot Radar shows the flag beside independently measured activity.

### Hot Radar evidence model

Hot Radar is an attention monitor, not a buy signal. It refreshes from the local one-minute market
collector and preserves its evidence in SQLite so the UI can explain each ranking. A score can earn
points for usable liquidity, accelerating five-minute volume and transactions, buyer participation,
moderate momentum, a bounded market-cap range, and a pair age between ten minutes and one day.
Extreme price moves, implausible volume relative to liquidity, thin liquidity, very low transaction
counts, very new pairs, and paid boosts are displayed as risk flags. Extreme moves and anomalous
volume are penalized; paid promotion never adds points. Contract audit, candle confirmation, route
simulation, and portfolio circuit breakers remain separate mandatory gates for autonomous paper
entries.

### Narrative Intelligence

The trading signal uses deterministic source matching and scoring. An optional
OpenAI research layer can summarize what one recent, already contract-linked
public item claims. Set `OPENAI_API_KEY` in the local `.env` and explicitly set
`NARRATIVE_OPENAI_ENABLED=true` to allow paid calls; the default is off. The
layer sends only a bounded title, excerpt, publication time and known contract
addresses to OpenAI's fixed Responses endpoint. It uses structured output,
`store=false`, a 15-second request timeout, no redirects, one item per refresh,
and `NARRATIVE_OPENAI_DAILY_LIMIT` (default 10, including failed attempts).
The local journal records the model and prompt version, source item ID, token
usage and a short assessment or safe error code. The assessment journal never
stores provider error bodies, wallet secrets or API keys. The assessment is displayed as a
**summary of the source's claim**, not verification or trading advice. It does
not alter narrative scores, audit findings, paper entries, or exits. Model
input to automatic decisions needs separate historical and forward validation.
OpenAI API credentials and billing are separate from a ChatGPT subscription.

The Discovery screen also runs a bounded, paper-only narrative collector against fixed public read
endpoints: the GDELT DOC 2.0 ArticleList API, a Google News RSS search, and Bluesky's
fixed `api.bsky.app` AppView search. The `public.api.bsky.app` host returned HTTP 403
from this environment while the official AppView host returned valid unauthenticated
results. Any provider failure is displayed as degraded rather than contributing evidence. Google News is
parsed with `defusedxml` so entity expansion
and external-entity payloads are rejected. No user-provided URL is fetched by the backend. Requests
use strict timeouts, response-size limits, disabled redirects, and a small fixed query set. Provider
failures are isolated and displayed instead of stopping the collector.

Evidence is normalized and stored locally rather than retaining complete provider payloads. The
analyzer:

- distinguishes exact Solana contract-address mentions from weaker symbol/name matches;
- deduplicates repeated content and counts independent source identities;
- separates news aggregation from unverified public social posts;
- scores recency, source diversity, bounded engagement, and contract linkage using valid publication
  times inside the last 24 hours;
- flags promotion-dominant, social-only, weak-linkage, and security-claim narratives; ticker/name
  matches without an exact contract address remain low confidence and below the active threshold;
- exposes the evidence trail, source link, tier, and reasoning in the dashboard.

Exact contract addresses are also checked against already discovered tokens
outside the collector's 100 newest candidates. The lookup is bounded and
local; it never creates a token or fetches a pasted address. A corrected link
updates the saved item's match list without changing its original observation
or publication time. The signal first appears in the current refresh, so past
decisions are not rewritten. An old mention still fails the one-hour bonus
clock, and a lone social post remains uncorroborated.

Narrative data cannot bypass audit, liquidity, volume, candle, route-simulation, position-limit, or
portfolio circuit-breaker rules. A high-confidence, multi-source, contract-linked signal with news
corroboration can add at most five strategy points. Promotional or social-only signals add none.
The bonus requires both a signal calculation no older than 15 minutes and an
underlying contract-linked mention no older than one hour. A recalculation of
old articles does not reset the mention clock. Missing, future, or legacy
mention timestamps add no points. The current market-policy version is 2;
older paper entries are kept separate from its verified performance sample.
The Discovery table labels older linked signals as older evidence and shows
the underlying mention age, so a high narrative score is not mistaken for a
current paper-entry bonus.
A security claim only blocks a paper entry when it is contract-linked,
high-confidence, and corroborated by at least two independent sources. Ticker-only hype adds no
strategy points.
The local SQLite schema adds a nullable `latest_mention_at` to existing
`narrative_signals` tables. Existing rows remain readable and receive no
narrative bonus until a fresh collector refresh records the underlying time.
Each complete refresh also appends a timestamped, bounded signal set to
`narrative_signal_observations`, including an empty set when evidence clears.
The current view and its history commit together. This additive table starts
collecting only after upgrade; previous refreshes cannot be reconstructed.
Historical replay reads the last completed refresh available at each decision
and applies the same market scorer used by paper entry. A decision without a
prior refresh is rejected as missing evidence. Older refreshes cannot be
recovered, and historical policy settings and worker timing are not recorded
for every interval, so the market parity flag remains false.

### Wallet and contract forensics

Every token audit can resolve the controllers of the 20 largest SPL token accounts with bounded
`getMultipleAccounts` calls. It groups multiple token accounts controlled by the same address,
classifies program/PDA-controlled holdings without assuming they are safe, and searches a bounded mint
history for a parsed initialization transaction and creation signer. Exact Pump bonding-curve and
canonical SOL PumpSwap vault token accounts are derived locally from the PDA/ATA seeds in Pump's
official public documentation. Only those exact vault addresses and Solana's canonical incinerator are
excluded from effective holder concentration; unknown PDAs and generic program-controlled accounts
remain counted. The dashboard always shows both raw and effective concentration plus coverage limits.

## Dry-run trading and the live lock

A buy without `--execute` builds and simulates a signed transaction but does not broadcast it:

```bash
memecoin-bot buy --mint MINT_ADDRESS --amount-sol 0.005 --slippage-bps 100
```

This research build rejects `--execute` before quoting or submitting a swap, even when
`LIVE_TRADING_ENABLED=true`. Keep the environment value false. The CLI still accepts historical
execution flags for compatibility, but they cannot enable live trading.

Sell all wallet token accounts for a mint (dry-run unless live gates are supplied):

```bash
memecoin-bot sell --mint MINT_ADDRESS --all --slippage-bps 100
```

## Position monitor

Record the original SOL cost basis, and optionally the original token amount. The monitor sums all
wallet token accounts for the mint and persists whether its one-time take-profit was already used.

```bash
memecoin-bot monitor \
  --mint MINT_ADDRESS \
  --entry-sol 0.01 \
  --position-id my-position-001 \
  --take-profit-bps 20000 \
  --take-profit-sell-bps 5000 \
  --stop-loss-bps 6000 \
  --poll-seconds 15
```

`20000` PnL bps means 2x current liquidation value; `6000` means stop when liquidation value reaches
60% of cost basis. The dry-run monitor simulates an exit if a trigger is reached. Stops are not
guaranteed: liquidity, RPC, route, simulation, and block conditions can prevent execution.

## Continuous watcher

The watcher is bounded to one cycle and dry-run by default:

```bash
memecoin-bot watch \
  --program-id VERIFIED_PROGRAM_ID \
  --amount-sol 0.005 \
  --cycles 1
```

Use `--cycles 0` for continuous dry-run operation. Live submission remains locked.

Each cycle processes at most 10 previously unseen candidates by default; adjust the bounded
`--max-candidates-per-cycle` value to control RPC/Jupiter usage.

## Strategy Lab

The dashboard's **Strategy Lab** is a paper-only research workspace. A bounded
collector fetches pool-specific, trade-derived OHLCV from GeckoTerminal for
1m, 15m, and 1h intervals. Complete closed groups of real 1m bars also form
5m, 15m, and 1h candles, with exact OHLC aggregation and summed trade volume.
Every minute in the interval must be present. Direct provider bars take precedence;
when both forms exist, a price disagreement above 1% blocks that bar once the
conflict is observable. Aggregates inherit the latest first-observation time of
their component minutes, so backfilled history cannot create an earlier signal.
The collector stores fetched
closed candles in SQLite and prioritizes recent safe paper candidates. DEX Screener snapshots
remain available for discovery and pool liquidity, but are never converted into
trade candles or backtest volume. The collector starts at four requests per
cycle and may trial six at a 10-second public-request pace after a sustained
clean backlog. A provider rate limit returns the next cycle to four after
cooldown and holds the lower pace for an hour. A smaller ready set also
returns to four. More
requests do not guarantee fresh 1m candles or override
the stale-data entry gate.
The local 1m response cache expires after 20 seconds so a later scheduled
poll can observe a newly closed minute; higher-frame responses remain cached
for 45 seconds. Network calls still obey the provider pace and 429 backoff.

The entry signal uses four explicit factors:

- short trend versus the eight-candle baseline;
- three-candle momentum, with an overheat guard;
- actual pool trading volume compared with prior candles;
- current liquidity versus the configured minimum.

The compact follow-up strip reports the most recently selected market-ready
token's fixed observation window and the status of its 5m and 15m authentic
price paths. "Observed" describes a price path, not an executable fill or
realized profit. A pending or missing path is shown explicitly. The Paper
Portfolio remains the source for costed simulated fills and exits.

The token picker prioritizes recent first-observed one-minute bars and labels
older or unverified history. The chart note reports the age of the latest
trusted closed bar for the selected timeframe. A recent bar alone is not an
entry signal; the full contiguous multi-timeframe and safety gates still apply.

The tracked set contains open paper positions, a persisted score-independent
research cohort, and audited-safe candidates that fit the 30-address market
request limit. Safe-candidate tracking supplies candles for forward paper decisions;
it does not add those candidates to the independent research cohort or make them
eligible without fresh audits and all other entry gates.
Within the bounded OHLCV request slots, audited-safe candidates that already
clear the strategy's market-only gates receive priority over safe candidates
whose liquidity, volume, or other market checks fail. The remaining slots still
sample other safe candidates; the research cohort remains independent of this
paper-collection priority.
The autonomous scan spends up to three additional audits per cycle on previously
safe candidates, oldest audit first. A failed refresh keeps the prior timestamp;
the ten-minute audit freshness gate still blocks paper entry.
It also spends up to ten bounded audit slots on the fixed research cohort,
including mints that have left the latest discovery view. Missing point-in-time
observations are checked first, then the oldest observations. These checks do
not retroactively establish past safety. With a 20-mint sample and a five-minute
paper cycle, a ten-slot budget cannot guarantee every cohort audit remains under
ten minutes old; replay excludes entries without a fresh recorded audit. DEX
Screener enrichment is split into requests of at most 30 token addresses.

Market snapshots retain distinct second-level observations for later
point-in-time checks. A repeated observation in the same second keeps its
first value. Older minute-bucket rows remain as recorded and cannot recover
earlier values that were overwritten before this change. A saved snapshot is
market evidence, not a real OHLCV candle or proof of an executable fill.
When DEX Screener omits liquidity for a fixed research-cohort pool, a
research-only probe requests exact-pool reserves from GeckoTerminal's
[documented public pool endpoint](https://api.geckoterminal.com/docs/index.html).
It records the result only when the returned Solana pool and token both match,
labels its source, and never backdates the observation. The probe handles at
most ten pools per request and rotates across missing cohort members no faster
than once every five minutes. It yields whenever the candle lane is trying a
higher request budget. A pool-probe rate limit pauses the probe for at least
ten minutes and holds the candle lane at four calls per cycle for an hour.
It does not enrich
paper-entry candidates or relax live market, audit, or route gates. A zero
reserve is recorded as zero liquidity, so replay rejects that period.

Historical replay uses the adjustable TP ladder shown in the interface, a
one-buy/one-sell cost estimate plus a separate network and priority fee for each
additional partial exit, 1% portfolio allocation by default, trailing
and initial stops, a maximum holding period, liquidity exit breaker, and portfolio
drawdown, daily loss, and consecutive-loss breakers. These controls are scenario
inputs only and do not change the autonomous worker's server-enforced policy.
The decision is made after a candle closes; the earliest simulated entry is the
next candle's open. Replay also requires each entry and confirmation candle to
have been fetched before its decision timestamp, with OHLCV values preserved
from that first observation. Re-fetches update only the last-fetch time.
Backfilled bars and older rows without trusted first-observation values are
unavailable at that point in the replay;
complete derived 5m bars become available only after their last component was
fetched. If fewer than eight observable entry or confirmation bars exist, the
run reports insufficient history. This conservative timing may miss entries
that a faster feed could have taken; the next-open fill and cost assumptions
still do not model actual execution latency. Replays check the stop before profit targets when both occur
inside one candle because the intrabar path is unknown. Results show closed-trade
equity drawdown; intratrade drawdown and real fill probability are not measured.
Forward paper entry uses the same first-observation gate; it pauses until each
required recent candle has trusted values. Existing paper positions continue
to be monitored for exits while new entry evidence accumulates.
The first-observation and revision columns are additive SQLite migrations. Keep a
database backup before upgrading; rollback by restoring that backup rather than
dropping the columns, which would discard newly collected provenance.
Positions still open at the end of the series, or with a data gap or unavailable
liquidity during exit tracking, are disclosed as censored and excluded from realized
win rate, expectancy, profit factor, return and drawdown.
The 1m replay and walk-forward path now use the same 1m/5m closed-candle gate
and ready higher-timeframe downtrend veto as autonomous paper entry. Other
selected timeframes remain labeled single-frame research studies. Replay still
lacks historical point-in-time audits before the new `audit_observations` table
began recording. Each automated scan or pasted-call audit now appends its exact
completion time, outcome, concentration, token decimals, and observed pool.
Replay requires the latest recorded outcome at each decision to be safe and
no more than ten minutes old. Earlier audits are never inferred from the
current candidate row. This is an additive SQLite schema change; retain the
normal database backup before moving between app versions. Removing the new
table would discard newly collected research evidence, so rollback should
restore a backup rather than dropping it. Related-wallet ownership evidence,
Jupiter fill probability and actual intrabar execution remain unavailable.
Replay's candle-based stop and TP fill model differs from the forward worker's sampled Jupiter
quotes. These results cannot establish complete production-strategy parity
or justify live trading.
The replay and walk-forward evaluation now require a captured market snapshot
available at the decision time. They apply the paper market gate's hard pool,
freshness, liquidity, 24h volume, and extreme-move checks. Missing historical
snapshots fail closed and are reported as insufficient market history. Older
minute-bucket records may have lost earlier observations. The scorer uses the
last completed narrative refresh available by decision time and blocks when
that history is absent. It applies the current market policy only when the
mint's latest prior paper decision logged the same policy version and thresholds
within two automation intervals. Missing, stale, or changed policy evidence
blocks replay entry. Older settings and the paper worker decision cadence
cannot be fully reconstructed. Therefore `historical_market_gate_parity`
and `paper_entry_schedule_parity` remain false. Treat replay as an incomplete
strategy comparison until those gaps close.
New paper decisions save the market gate version, policy, inputs, score,
verdict, and hard-gate reasons alongside candle evidence. The read-only
verification report recomputes this market gate for linked paper entries and
labels older entries without the saved policy `unverifiable`; it does not
rewrite historical decisions or infer missing market observations. A matched
entry gate alone is not a full historical replay or a profitability result.
New autonomous decisions also carry a cycle ID, cycle start time, candidate
rank, candidate count, and initial entry-breaker reasons. The verifier checks
whether the latest bounded decision window contains complete, consistently
ranked worker cycles with at most one paper opening. A partial recent window
is labeled partial rather than treated as a complete cycle. Earlier decisions
cannot be assigned to cycles reliably from their timestamps alone.
The verifier also replays candle evidence for recent `observe` decisions as
well as paper openings. It checks that the saved 1m, 5m, 15m, and 1h signals,
latest closed bars, and final eligible/observe verdict agree with candles
available at that exact decision time. Decisions stopped before candle
analysis remain outside this candle-parity denominator.
The read-only verification report also follows recent saved decisions for 5m
and 15m *price-only* outcomes. Each horizon samples the latest 100 decisions
old enough for that horizon, enters at the first later minute's open, and
requires continuous authentic 1m bars with trusted first-observation times.
Paths are labeled timely when all bars were captured within 120 seconds of
their close, or backfilled when authentic bars arrived later. Missing pools,
absent bars, untrusted values, and incomplete horizons remain censored.
Maximum favorable/adverse movement and the
horizon close are observations, not simulated fills or net returns; the report
does not use them to change live paper decisions.

The lab displays **Collecting**, **Stale**, or **Data gap** when candles cannot
support a signal. The collector skips incomplete candles. Missing pool-specific
liquidity prevents entry. Existing snapshot-derived candles remain in the code
only as labeled legacy diagnostics and are rejected by replay. The keyless public
GeckoTerminal feed needs no new account. Keep the dashboard collector running to
accumulate both authentic candles and contemporaneous liquidity history.
The client paces owned requests at no more than four per minute, below the
[currently published 30-calls-per-minute public limit](https://support.coingecko.com/articles/6867086195-does-geckoterminal-have-an-api).
Observed 429 responses may reflect other consumers of the shared public quota.
The client retries transient server failures and stops a cycle on the first rate-limit
response. It honors a bounded
`Retry-After` cooldown and preserves the interrupted collection lane after the cooldown.
Within that lane, oldest-fetched pools are selected first; a changed safe watchlist
can change the exact mint order. A second
collector process can still share the same provider quota, so run only one collector.
Conflicting duplicate
candles are rejected and surfaced as provider errors; they never become signals.

Every paper entry path, including pasted calls and manual dashboard entries, checks:

- maximum simultaneous positions;
- daily realized paper loss;
- consecutive closed paper losses;
- low liquidity on an open paper position.
- portfolio drawdown;
- missing or stale open-position marks and pool-liquidity snapshots.
- an open position whose exit quote failed in the current worker cycle.
- an automated exit attempt that failed in the current worker cycle.

A triggered breaker blocks new entries but keeps the worker running so existing
positions can still reach stop-loss, trailing-stop, timed-exit, and TP logic.
After a partial take-profit, the worker obtains a new quote for the remaining
position before evaluating another TP or exit signal. The interim remaining-size
estimate stays stale, so entry risk checks cannot mistake it for a fresh
Jupiter mark. If the refresh fails, the worker stops cascading exits, records
the quote outage, and pauses new paper entries.
Other positions use the refreshed quote states from that same cycle, so a later
quote failure cannot leave an earlier mark eligible for an automated exit.
Full and partial paper exits retry a route once only if its built quote exceeds
the freshness limit. Each attempt is priced independently and the saved fill
records the number of route requests. If both quotes are stale, no paper tokens
or cash move; the worker records the failed exit and blocks new entries until
the exit lane recovers.

### Paper execution realism

Paper positions use net modeled fills rather than treating a Jupiter headline quote as a fill.
Every entry and exit records the quoted amounts, price impact, quote age, adverse-slippage haircut,
network fee, priority fee, token-account rent, visible liquidity, and trade-to-liquidity ratio.
Paper buys require a positive USD notional and a matching pool-liquidity snapshot
captured within 180 seconds; otherwise the size check cannot run and the buy
is rejected. If Jupiter's fee-free `/swap/v2/build` quote omits USD metadata for
a SOL input, the bot derives notional from the exact SOL input and Jupiter Price
V3's SOL/USD reference. It compares the price block ID with the current RPC slot
and rejects a reference more than 150 slots behind. The saved fill records this
source and both block IDs. Missing USD metadata does not prevent managing an existing paper
exit, but its trade-to-liquidity ratio is recorded as unavailable.
Entries debit all explicit costs; exits and portfolio marks use net proceeds after fees. Routes are
rejected when they are stale, exceed the configured price-impact cap, require more slippage than the
order permits, or consume too much visible pool liquidity. The model is deterministic so results can
be reproduced and stress-tested; it is conservative evidence, not a promise of live execution.
Every successful fresh Jupiter paper mark now also appends its time, remaining size, net
value, original-size-normalized performance value, peak, TP stage, and execution
estimate to `paper_position_marks`. The forward report counts total and costed
marks. This history starts when the updated collector runs; past overwritten
marks cannot be reconstructed. It supports future path and drawdown analysis,
but current closed-trade drawdown still does not measure intratrade lows.
The forward report separately counts positions whose costed marks have bounded
gaps from entry to the latest mark or close. For evidence-complete closed
trades with that coverage, it also shows the largest drawdown of the marked
position liquidation value, including cash realized by partial exits and the
final sale. Token inventory at each mark determines which realized fills belong
to it, even when a mark and sale share a timestamp. This is a lower bound: a
two-minute scheduled quote cadence can miss a larger move between marks. Positions
with older missing marks remain excluded from that sampled-mark statistic.
The new table is an additive SQLite migration. Make a database backup before
upgrading and restore that backup to roll back without losing pre-upgrade data.

### Walk-forward evaluation

Strategy Lab can run an anchored walk-forward evaluation after at least 36 authentic
candles are available.
For each fold it selects the signal threshold and stop loss from past candles only, freezes those
parameters, and evaluates the next unseen window. The report includes out-of-sample expectancy,
profit factor, drawdown, a 150%-execution-cost stress pass, market-regime results, signal-score
calibration, and ablations for trend, momentum, volume, and liquidity. The dashboard keeps promotion
locked until positive expectancy, profit factor of at least 1.25, positive stressed returns, and at
least 200 closed positions are all observed. These are necessary gates, not proof of future profit.
Each ablation uses the same unseen folds and frozen parameters as the full model.
Removing liquidity from the score never removes the minimum-liquidity entry gate.
The existing four-factor candle strategy is the baseline; proposed new indicators
have not been added. Performance improvement cannot be claimed until both variants
are evaluated on the same unseen data and paper-trading record.

Run `python -m memecoin_bot.verification` for a read-only coverage and replay report after the
dashboard has initialized the current database schema. It shows
authentic candle alignment, gaps, contemporaneous liquidity coverage, closed paper fills, and
available strategy statistics. `recent_decision_funnel` counts the latest 100 saved
audit-safe candidates, market-gate passes, 1m/5m candle states, confirmed signals,
and actions without treating rejected or still-open observations as trades.
Until sufficient point-in-time data is collected, performance
fields remain unavailable rather than showing invented returns.

## Risk configuration

All caps are enforced server-side in the Python process, not only by CLI validation:

- `MAX_TRADE_LAMPORTS`: maximum SOL for one new position.
- `MAX_DAILY_LAMPORTS`: total new SOL exposure reserved/submitted/confirmed per UTC day.
- `MAX_SLIPPAGE_BPS`: hard upper bound accepted from commands.
- `MAX_PRICE_IMPACT_BPS`: maximum Jupiter quote price impact.
- `MAX_TOP10_CONCENTRATION_BPS`: concentration limit; exact recognized vault and burn evidence can
  produce a separately displayed effective value, while raw concentration remains visible.
- `MAX_PRIORITY_FEE_LAMPORTS`: maximum transaction priority fee.
- `ALLOW_TOKEN_2022`: opt-in only. The mint extension list must be parsed; only
  immutable, mint-linked Metadata Pointer and Token Metadata extensions are reviewed
  for acceptance. Unknown extensions, mutable metadata authorities, transfer hooks,
  transfer fees, and other transfer-altering extensions remain blocked. This follows
  [Solana's extension definitions](https://solana.com/docs/tokens/extensions/metadata)
  and does not override the other contract and holder checks.
- `DISCOVERY_PROGRAM_IDS`: 1-10 comma-separated Solana program IDs. Defaults to the official Pump
  and PumpSwap programs.
- `PUMP_API_KEY`: optional official Pump.fun server API key used for the Solana New callouts feed.
- `DEXSCREENER_DISCOVERY_ENABLED`: include recent Solana token profiles as discovery leads.
- `NARRATIVE_INTELLIGENCE_ENABLED`: run the keyless public narrative collector in the dashboard.
- `NARRATIVE_GDELT_ENABLED`: read recent news matches from GDELT's fixed DOC 2.0 endpoint.
- `NARRATIVE_GOOGLE_NEWS_ENABLED`: read the fixed, last-24-hours Google News RSS query.
- `NARRATIVE_BLUESKY_ENABLED`: read recent public posts from Bluesky's fixed AppView host.
- `NARRATIVE_REFRESH_INTERVAL_SECONDS`: narrative refresh cadence; defaults to five minutes.
- `OPENAI_API_KEY`: optional secret used only by the server-side narrative research layer.
- `NARRATIVE_OPENAI_ENABLED`: opt into paid, paper-only OpenAI research; defaults to false.
- `NARRATIVE_OPENAI_DAILY_LIMIT`: maximum attempted model requests per UTC day; defaults to 10.
- `TOKEN_FORENSICS_ENABLED`: resolve holder controllers and exact known vault evidence during audits.
- `FORENSICS_ORIGIN_TRACE_ENABLED`: search bounded mint history for parsed initialization evidence.
- `FORENSICS_MAX_SINGLE_CONTROLLER_BPS`: hard limit for one observed non-vault controller; defaults
  to 15%.
- `FORENSICS_SIGNATURE_LIMIT`: maximum recent mint signatures considered for origin tracing.
- `FORENSICS_TRANSACTION_CANDIDATES`: maximum oldest candidate transactions decoded per audit.
- `DISCOVERY_SIGNATURES_PER_PROGRAM`: recent signatures inspected per configured program (1-25).
- `DISCOVERY_MAX_TRANSACTIONS`: hard transaction-read cap per program scan (1-100).
- `DISCOVERY_CANDIDATE_LIMIT`: maximum candidates audited and stored per scan (1-30).
- `PAPER_STARTING_LAMPORTS`: simulated starting cash; defaults to 1 SOL. This never changes the
  burner-wallet balance.
- `STRATEGY_MIN_LIQUIDITY_USD`: hard minimum liquidity before a candidate can be eligible.
- `STRATEGY_MIN_VOLUME_24H_USD`: hard minimum 24-hour volume.
- `STRATEGY_MIN_SCORE`: explainable score required for an automated paper entry.
- `PAPER_RISK_PER_TRADE_BPS`: maximum total modeled cash debit per autonomous or call-driven entry, as a percentage of available paper cash; `100` is 1%. The swap input is reduced by modeled entry network fees, priority fees, and token-account rent.
- `STRATEGY_MAX_OPEN_POSITIONS`: maximum simultaneous automated paper positions.
- `PAPER_AUTOMATION_AUTOSTART`: starts the paper worker with the dashboard; defaults to `false`.
- `PAPER_AUTOMATION_INTERVAL_SECONDS`: delay between completed cycles; defaults to 300 seconds.
- `PAPER_TP_LEVELS_BPS`: comma-separated profit thresholds for partial paper exits; `10000,30000,90000` means +100%, +300%, and +900%.
- `PAPER_TP_SELL_BPS`: matching portions of the original paper position to sell; `5000,2000,1500` sells 50%, 20%, then 15% and retains a 15% moonbag.
- `PAPER_STOP_LOSS_BPS`: paper exit loss threshold; `3500` is -35%.
- `PAPER_TRAILING_ACTIVATION_BPS`: gain required before the trailing stop is armed.
- `PAPER_TRAILING_STOP_BPS`: drawdown from the marked peak that closes an armed position.
- `PAPER_MAX_HOLD_SECONDS`: maximum paper-position age before a quoted exit.
- `PAPER_MAX_DRAWDOWN_BPS`: portfolio-equity drawdown that blocks new entries.
- `PAPER_DAILY_LOSS_BPS`: daily realized-loss limit relative to starting paper equity.
- `PAPER_MAX_CONSECUTIVE_LOSSES`: closed-loss streak that blocks new entries.
- `PAPER_LIQUIDITY_BREAKER_USD`: liquidity floor for open-position entry lockout.
  Each open position needs a fresh snapshot for its recorded pool. Low verified
  liquidity and missing or stale evidence have distinct lockout reasons, even
  after its token leaves the latest discovery list.
- `PAPER_EXECUTION_BASE_SLIPPAGE_BPS`: deterministic adverse haircut applied after the quoted price
  impact; defaults to 30 bps.
- `PAPER_EXECUTION_LIQUIDITY_SHOCK_BPS`: extra adverse haircut for stressed replay scenarios.
- `PAPER_EXECUTION_NETWORK_FEE_LAMPORTS`: modeled base transaction fee per fill.
- `PAPER_EXECUTION_PRIORITY_FEE_LAMPORTS`: modeled priority fee, bounded by
  `MAX_PRIORITY_FEE_LAMPORTS`.
- `PAPER_EXECUTION_ATA_RENT_LAMPORTS`: fixed planning reserve for replay and pre-trade sizing;
  forward entries measure the output token account in a signed dry-run simulation.
- `PAPER_EXECUTION_MAX_QUOTE_AGE_MS`: rejects a route that has aged beyond this limit before use.
- `PAPER_EXECUTION_MAX_TRADE_LIQUIDITY_BPS`: maximum notional share of visible pool liquidity;
  `100` is 1%.
- `OPERATIONS_BACKUP_ENABLED`: create a verified SQLite online backup when the dashboard starts and
  the configured interval is due.
- `OPERATIONS_BACKUP_INTERVAL_SECONDS`: minimum time between automatic state backups.
- `OPERATIONS_BACKUP_DIRECTORY`: local backup destination; keep it on encrypted durable storage in
  production. Backups contain strategy and portfolio state, never the wallet key.
- `STRATEGY_LAB_COLLECTION_INTERVAL_SECONDS`: bounded market and real OHLCV collection cadence.

The strategy is deliberately deterministic and inspectable. It is not an ML profitability claim:
it combines hard risk gates with weighted audit, liquidity, turnover, momentum, and pair-age evidence.
Route price-impact and transaction simulation remain mandatory after scoring. The strategy can only
open paper positions; it cannot turn on or bypass the separate live-execution gate.
The workers exist only inside the running dashboard process. A second dashboard using the same
SQLite journal fails startup before launching any workers; the `.workers.lock` file is an OS lock
and contains no credentials. The included hardened systemd service is for Linux deployments.
On Windows, register the current-user logon task from a normal PowerShell window:

```powershell
.\deploy\install-windows-paper-task.ps1
```

The task starts the local dashboard at sign-in and checks every five minutes while the user is
signed in, relaunching it if it has stopped. Task Scheduler also retries unexpected failures after
one minute and ignores duplicate starts. It runs without administrator rights and can continue
while the laptop is on battery power. Its command stores no API key,
wallet key, or pairing code. It uses `dashboard --free-only`, which refuses startup if either
`LIVE_TRADING_ENABLED` or `NARRATIVE_OPENAI_ENABLED` is true. The code-level live execution lock
remains in force independently. A browser that still has a valid remembered session can reopen
the dashboard without another pairing code. Otherwise, read the new code in the interactive
dashboard console after sign-in. Registration does not start or interrupt the current process.
The task runs only while this Windows user is signed in, and Windows must stay powered on and
awake for continuous collection. Ending its process while signed in is temporary; the five-minute
check starts it again. To deliberately pause it, disable the scheduled task and check for open
paper positions first. A sign-out, crash, or restart resets the continuity counter;
confirm the task and fresh worker heartbeats after the next sign-in. If Windows does not show the
task console and pairing is required, stop the task in Task Scheduler and launch the dashboard
manually to obtain a code. A manual start through Task Scheduler was verified on this machine while on battery;
automatic launch after the next sign-in has not yet been verified.

The current collection policy uses public, free OHLCV requests and pins a small number of
active pools because measured throughput is below the minimum needed to refresh every eligible
mint. The dashboard reports observed calls per minute, minimum demand, and missing or stale
bars; a candidate without complete recent history cannot enter paper trading. Paid market-data
feeds and paid OpenAI research remain optional and off in the free-only setup.

Top-account concentration includes pool, treasury, burn, and exchange accounts and cannot link
wallets controlled by the same party. It is a coarse rejection filter, not an identity analysis.

## Tests and quality checks

```bash
ruff format --check .
ruff check .
mypy
pytest
python -m build
pip-audit
```

Never run integration tests with a funded keypair. The test suite uses fakes and generated keys.

## Deployment

The included `deploy/memecoin-bot.service` runs a configured command as a dedicated unprivileged
Linux user with filesystem and kernel hardening. Copy the application to `/opt/memecoin-bot`, put
non-secret configuration in `/etc/memecoin-bot/bot.env`, inject provider credentials through your
secret manager, and keep the key at `/etc/memecoin-bot/keypair.json` with mode `0600` owned by the
service user.

The included container runs as a non-root user. Mount `.env` and the keypair as read-only files;
mount `/app/state` to durable encrypted storage. Do not bake either secret into an image.

## Architecture and limitations

`audit.py` performs on-chain mint checks; `discovery.py` only produces candidates; `jupiter.py`
requests routes and instructions; `transaction.py` validates, compiles, simulates, submits, and
confirms; `journal.py` handles idempotency/exposure/position state; `service.py` applies policy;
`strategy_lab.py` aggregates local candles and runs conservative replays; `evaluation.py` performs
walk-forward validation, regime analysis, calibration, and ablation; `execution.py` converts fresh
routes into deterministic net paper fills; `cli.py` is a thin command interface.

The system does not claim to identify insider clusters, prove locked liquidity, protect against all
MEV, verify LP locks/burns, or guarantee transaction landing. The current discovery adapter is
polling-based rather than a validator/Geyser stream. For higher scale, put discovery events on a
durable queue, use a dedicated Geyser feed, keep signing isolated, and use a high-availability RPC
strategy. Preserve the same policy and simulation boundary before any additional executor is
allowed to submit.
