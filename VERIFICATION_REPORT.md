# Trading system verification — 2026-11-03

This report describes the local research build and database on 2026-11-03.
The database changes while the paper collector runs. Reproduce current
measurements with `python -m memecoin_bot.verification`.

## Isolated forward paper run (2026-11-03)

The prior journal contains older paper positions with incomplete entry evidence,
including one open autonomous position whose exit quote became uneconomic after
recorded pool liquidity fell. Its risk pause correctly prevents another entry.
To collect a clean prospective sample while retaining that journal, a new paper
run was created at `forward-paper-2026-11-03-v2.sqlite3` at 14:42 UTC. Its run ID
is `81005a1c-f473-480d-ae31-cc75cff39602`. It copies 1,067 discoveries,
5,996 point-in-time audit observations, 31,668 market snapshots, 41,469
provenance-tagged pool candles, and the existing narrative research records.
The new paper ledger, decisions, worker health, and performance start empty.
The old `trade-state.sqlite3` was not modified or discarded.

The first creation attempt exposed migrated SQLite columns in a different order.
The copier now checks column names and types and inserts by explicit column
name. A test recreates that legacy ordering. The aborted first target file is
not selected for use. No live trade was sent; the live-execution lock remains
active. This new run has no forward performance result yet.

The local dashboard was restarted on the new journal. At 14:45 UTC, its API
reported the matching campaign ID, zero open positions, zero strategy decisions,
the autonomous decision worker running its first cycle, and the paper exit
monitor running with one completed check and no error. The live flag was false.
The read-only verifier reported zero complete closed autonomous trades and
`promotion_ready=false`; the strategy comparison remains unavailable. The full
test suite, Ruff lint and format checks, mypy, JavaScript syntax check, and
wheel/source build passed after the migration-order correction.

The first autonomous decision cycle then opened one **paper-only** position at
14:45:38 UTC, costing 12,094,280 modeled lamports including 2,039,280 lamports
of token-account rent. Its saved candle signal and point-in-time market gate
both replayed as `matched`. The first exit-monitor quote failed transiently and
did not create a fill. A subsequent read-only Jupiter exit quote succeeded;
refreshing the paper portfolio at 14:47 UTC recorded a current modeled exit
value of 10,774,353 lamports. Paper equity was 998,680,073 lamports and
realized P&L remained zero. This one open position is not a performance sample
or evidence of profitability. The worker left the position open for future
fresh-quote exit decisions.

Automatic exit refreshes also failed intermittently while a manual refresh
succeeded. The journal and dashboard now record a bounded reason code for
paper execution model rejections, such as `stale_route` or
`price_impact_limit`, without exposing raw provider details. Tests cover the
refresh-to-cached-status path. Following the dashboard restart at 14:50 UTC,
the paper exit monitor recovered, cleared its quote-health outage, and saved
a fresh 11,945,692-lamport mark. The position remained open with zero realized
P&L. Intermittent quote failures still merit observation; no threshold was
relaxed to make a failed route appear executable.

At 14:52 UTC the new run had completed two decision cycles and stored 200
decisions, with one open paper position and no realized or closed trade. The
read-only entry readiness check found 17 market-eligible mints among the latest
100 discoveries, but 15 lacked fresh required 1m/5m candles, one had misaligned
signals, and one was entry-eligible. In 12 observed public OHLCV collector
cycles spanning 491 seconds, 32 network requests succeeded (3.91/minute),
while refreshing all 17 ready mints every 120 seconds would require at least
8.5/minute before research and higher-timeframe requests. The collector had no
new 429s in that window, but an earlier six-request trial did receive a 429;
the four-request budget was retained. This is a measured data-capacity limit,
not evidence that a stricter or looser indicator would improve returns.

The configured 0.01 SOL paper notional faces a modeled 2,209-basis-point
round-trip hurdle when the 0.00203928 SOL token-account rent is treated as
unrecovered. The dashboard reports a separate hypothetical rent-recovery
sensitivity; no refund is booked as realized cash. Labels now distinguish the
real wallet balance from estimated paper equity and paper P&L.

### Match paper quotes to the built router

The [Jupiter Swap V2 documentation](https://developers.jup.ag/docs/swap/index)
states that `/order` quotes include a Jupiter platform fee while the `/build`
router uses Metis and has no Jupiter swap fee. This bot assembled its dry-run
transaction with `/build` but previously valued paper buys and exits from
`/order`. A read-only same-token comparison returned 8,391,838,632 output
units from `/order` with a 10-bps fee and 8,400,238,870 from `/build` with
`platformFeeBps=0`, a near-exact 10-bps difference. These are point-in-time
quotes, not realized execution results.

New paper entries now use the validated output of the transaction actually
built, and new paper exit quotes use the fee-free `/build` route with the same
public taker address. The quote source and returned fee rate are saved with
entries, marks, and exits. The buy quote-age clock starts when the built route
is requested, after the audit, so audit latency cannot make a fresh route look
old. The existing open position retains `/order` for its future marks and
exit, and a future closed result from it is excluded from the new run's
router-parity performance sample. There was no live submission.
Autonomous and call-inbox entries in this forward campaign now fail closed if
Jupiter falls back to a quote source other than the fee-free built router;
the test confirms rejection leaves the paper portfolio without a new position.

Historical candle replay still uses a fixed cost assumption rather than
reconstructing historical Jupiter routes and execution. Its results remain
conditional on that assumption and currently contain zero comparable closed
trades. Route parity in forward paper improves future measurement but does
not establish live execution parity or profitability.

After the code restart, the dashboard resumed with the same campaign ID,
the existing open paper position, and both paper workers running without a
reported error. A separate read-only call through the new Python router-quote
method returned a `v2-build` quote with `fee_bps=0`, a swap instruction, and
the same output amount from the cached build response. No signing or broadcast
occurred. This confirms the API contract for that sample, not future quote
availability or live fill quality.

At 15:04 UTC, after the final slippage-setting correction and restart, the
dashboard reported four completed paper decision cycles, 12 completed exit
checks, one legacy open paper position, zero realized P&L, and no worker error.
The new run still has no comparable closed trade. The full test suite,
Ruff lint and format checks, mypy, JavaScript syntax, and wheel/source build
passed; the final slippage alignment also passed its focused paper tests.

### Point-in-time candle replay correction

Later verification briefly labelled the one open entry's 15m signal a
`mismatch`: the bot had recorded three complete 15m bars derived from 1m
trades, but four direct 15m provider bars arrived after the entry. The loader
preferred those later direct bars before applying the historical cutoff, so
the verifier saw zero bars known at the entry time. The loader now filters
provider observations by first-seen time before choosing direct or derived
bars. A regression test covers a derived 15m bar available at a decision and
a conflicting direct bar arriving later. Re-running the read-only verifier on
the unchanged paper journal recovered all four frame matches and the saved
entry verdict. This fixes entry-verification timing; it does not establish
historical fill parity or a profitable strategy.

The latest read-only capacity check found 20 market-gate-eligible mints in the
current discovery window. Seventeen lacked fresh required 1m/5m candle data,
three had ready frames but no aligned entry signal, and none qualified for a
new entry. Over 1,584 observed seconds the public OHLCV lane completed 98
network requests, or 3.71 per minute; keeping all 20 eligible mints fresh
every two minutes would need at least 10 per minute before research and deep
frames. Eighteen of 20 current research-cohort mints still have no observed
1m bars inside their membership window. This is an explicit data-coverage
limit on strategy comparison, not a reason to relax entry guards.

At 15:29:50 UTC the first paper position closed on its saved `stop_loss`
rule after 2,652 seconds. Its modeled entry cash debit was 12,094,280
lamports, including 2,039,280 lamports of reserved token-account rent; the
sell credited 7,398,394 lamports. The realized paper loss was 4,695,886
lamports (38.83% of the cash debit). Full hypothetical rent recovery would
still leave a 2,656,606-lamport loss; no recovery was booked. The entry used
the earlier fee-bearing route. Recorded pool liquidity went from about
$24,070 at entry to $20,498 at exit, and the exit quote carried 252 bps of
price impact and a 10-bps platform fee. The new forward report correctly excludes
this closed position with `entry_route_parity_missing`. One excluded loss is
useful execution-cost evidence, not a comparable strategy-performance sample.

### Cautious public OHLCV throughput trial

The [provider's current support page](https://support.coingecko.com/articles/6867086195-does-geckoterminal-have-an-api)
publishes a 30-call-per-minute free limit, but this connection previously
received HTTP 429 at a much slower pace. The initial gate required three
fully uncached four-call cycles, but live observation showed useful
fresh responses mixed with cache hits. The final gate now requires 12 fresh
provider responses within ten minutes, with at least eight market-ready
candidates and no provider, empty-page, or stale-page error. Cache hits never
count toward the 12. The collector may then try six calls at ten-second
spacing. A 429 immediately restores four calls at fifteen-second spacing and
suppresses another expanded trial for an hour. An initial runtime promotion
to six calls was traced to cached
responses being counted as provider acceptance; that check was corrected and
the persisted trial budget was returned to four without changing market or
paper records. Tests cover the trial pace, six-call cap, bounded cache-aware
promotion, and fallback. Sustained accepted throughput and improved fresh
candle coverage still require live paper observation.

The live trial then supplied the missing evidence. After 12 accepted fresh
responses, the next six-call cycle returned six successful client responses,
but three were local cache hits. The following cycle received two fresh
responses and then HTTP 429 on its third attempt. The persisted budget fell
back to four, the higher-pace retry was delayed by one hour, and the provider
cooldown prevented further OHLCV calls until its expiry. Operational health
correctly reported `market_collector` degradation for both rate limiting and
capacity below demand. The observed last-hour rate was 3.34 fresh responses
per minute against at least 8.5 per minute needed to refresh the 17 currently
market-ready mints every two minutes, before research and higher frames. The
six-call pace is therefore **not validated as sustainable on this connection**.
Freshness gates continue to reject stale entries; no live trade was sent.
After the short provider cooldown, the worker resumed a four-call cycle with
four successful client responses and no new 429. The rate-limit degradation
cleared; the separate capacity-below-demand warning remained. No process
restart or data reset was needed for that recovery.

At the safe four-call pace, some subsequent cycles still reused cached 1m
responses inside the client's 45-second cache, reducing fresh provider reads
without supplying a new closed minute bar. The 1m cache lifetime was reduced
to 20 seconds; 5m, 15m, and 1h retain 45 seconds. The 15-second network pace,
six-call trial cooldown, and stale-entry gates are unchanged. A focused test
verifies that a 1m response is fetched again after its shorter cache expires.

After restart, nine completed four-call cycles returned 36 fresh network
responses, zero local cache hits, and zero 429s over 531 seconds through
15:52:26 UTC (4.07 accepted responses per minute in this short window). This
contrasts with repeated one-to-three cache hits per cycle immediately before
the change. The rolling-hour rate still includes older cycles. At the latest
read-only check, 15 of 17 market-eligible tokens lacked fresh required candles,
two had misaligned signals, and none qualified for entry. The shorter cache
improves useful polling of the selected pools; it does not solve the public
provider's whole-watchlist capacity gap or prove a strategy
performance improvement.

The official [CoinGecko Demo plan](https://www.coingecko.com/en/api/pricing)
offers pool OHLCV with a higher short-term request limit but only 10,000 calls
per month. Its [pool OHLCV endpoint](https://www.coingecko.com/learn/how-to-pull-crypto-prices-from-dexs)
is now an optional `research-backfill --provider coingecko-demo` route. It
uses a fixed host and header-based key, with the command's existing saved-pool,
research-window, and five-request bounds. Mock HTTP tests verify path, key
header, pool identity, and missing-key rejection. It cannot sustain the current
continuous 1m watchlist demand, and
late backfills cannot establish that an entry signal was observable at the
historical decision time. No Demo key is configured, so this alternative has
not been exercised against the live journal. The autonomous collector still
uses the observed public GeckoTerminal pace.
The complete test suite, Ruff lint and format checks, mypy, and wheel/sdist
build passed after this optional route was added. The running dashboard still
has `LIVE_TRADING_ENABLED=false`; the paper worker continued recording decisions
and four-call collector cycles without a restart.

## Strategy comparison identity (2026-11-03)

The read-only report previously labelled its authentic-candle replay
`current_momentum_replay` while also describing the original candlestick
strategy as unavailable. Inspection of the strategy code and upgrade brief
shows that this replay is the **existing four-factor, multi-timeframe candle
baseline**. It is now labelled `baseline_candlestick_replay`. A separate
`strategy_comparison` summary explicitly records that the upgraded indicator
strategy and early-launch strategy have not been implemented. The user deferred
additional indicators until real OHLCV and baseline backtesting are verified.
The baseline replay still has no comparable closed trades and lacks full
historical market-gate parity, so no original-versus-upgraded performance claim
can be made.

At 14:22 UTC, the current decision funnel had 13 market-gate-eligible mints:
10 were blocked by missing, stale, or incomplete required 1m/5m candles;
three had ready required frames but their trend, momentum, volume, and
liquidity factors did not align. Zero were entry-eligible. The collector and
paper worker were running, and the live-execution lock remained active.
The read-only baseline replay covered 60 sampled mints and still produced zero
comparable closed trades. Its statuses were: 18 insufficient history, 11
insufficient market history, 10 no eligible trades, eight missing 1m history,
six insufficient candle availability, six insufficient confirmation history,
and one censored unclosed position. Historical market-gate parity is still
false, so these counts cannot support a strategy ranking.
Of six prospectively selected market-eligible follow-up tokens, five have a
timely observed five-minute price path. Only two have a timely observed
15-minute path; three 15-minute paths were filled later, and one lacked an
entry bar. These are price-only observations, not costed trades or evidence
that a rejected entry would have been profitable.

## Complete real 1m aggregation (2026-11-03)

The loader can now construct 5m, 15m, and 1h OHLCV only from every closed 1m
pool-trade bar in the interval. It uses the first open, maximum high, minimum
low, last close, and sum of trade volume. A missing minute blocks aggregation;
the derived bar becomes available only after its latest component was first
observed. Direct provider bars take precedence, and observable OHLC conflicts
above 1% block signal use.

At 13:08 UTC, the read-only verifier found 2,669 derived-only 5m bars, 345
derived-only 15m bars, and 65 derived-only 1h bars. Across matching direct and
complete 1m bars, 17 of 577 15m overlaps and 0 of 87 1h overlaps differed by
more than 1% in at least one OHLC field. The 15m and 1h conflict checks are
reported separately; discrepancies are not silently treated as agreement.
Current entry readiness improved for higher frames, but the 1m feed was stale
for all 14 market-eligible candidates in that observation. Comparable closed
replay trades remain zero.
The replay provenance version is now v16; saved runs using earlier aggregation
or market-policy replay rules remain stored but are marked unverified against
the current policy.
The shared paper/replay entry gate also blocks known 15m and 1h candle-integrity
failures: direct/derived OHLC conflicts, mixed provenance, or values without
trusted observation evidence. A higher frame with too little history remains
optional; its unknown trend is not reported as confirmed. Tests cover both
integrity rejection and the collecting case.
After the rule loaded in the restarted dashboard, the read-only verifier found
15 market-gate-eligible candidates: 13 had insufficient 1m/5m candle data,
two lacked aligned 1m/5m signals, and none qualified for entry. Four 15m and
four 1h frames in that snapshot had mixed provenance; none was allowed to
override a failed required-frame gate. This observation verifies the current
decision funnel, not a trade outcome.

## Point-in-time market policy replay (2026-11-03)

Paper decisions already record the market-policy version and numeric thresholds.
The replay now reads the latest decision for the same mint at or before each
simulated decision. It accepts that policy only when it matches the current
replay configuration and was recorded within two paper-automation intervals.
Missing, stale, invalid, or mismatched policy evidence blocks a simulated
entry with a distinct reason. It does not fill old policy gaps by assuming
today's settings applied in the past. An index on mint and decision time keeps
the point-in-time lookup bounded as the journal grows.

Tests cover prior versus future policy, changed thresholds, stale policy, and
the backtest's explicit rejection reasons. A consistent pre-change SQLite backup
passed `quick_check`; after dashboard restart, the new index existed and the
live database also passed `quick_check`. The dashboard shell and assets returned
HTTP 200; unpaired private health returned HTTP 401. All four worker states were
running without recorded errors, and the independent live lock remained true
with the environment live flag false. The full Python suite, Ruff checks,
source mypy, and wheel/source build passed. Read-only verification still shows
**zero comparable closed replay trades across 60 sampled mints** and zero
qualified closed autonomous paper trades. Policy evidence alone does not prove
historical worker schedule parity or a trading edge.

## Inactive-pool request deferral (2026-11-03)

The current two-mint score-independent research focus had no current 5m DEX
transactions or volume, and its last authentic 1m bars predated the observation
by more than five minutes. Repeated OHLCV requests for those pools cannot add
a current trade candle while they remain inactive. The collector now defers a
pool only when the current DEX response has a pool, price, liquidity, explicit
zero 5m volume, zero 5m transactions, and no authentic 1m bar in the last five
minutes. Missing DEX fields do not trigger deferral. The mint remains in the
prospective cohort and replay denominator, and a later active DEX observation
restores collection. This is request scheduling, not a revised safety verdict
or a claim that inactive tokens could not have lost value. A test covers
deferral, the recent-trade exception, reactivation, and retained membership.

The first completed collector cycle after this change (2026-11-03 13:44:49
UTC) deferred 15 explicitly inactive pools, retained all 20 research members,
and completed four of four OHLCV requests without a rate limit. Its three
research members with current 5m DEX transactions received no request that
cycle. The scheduler had reserved research capacity only while a focus mint
was eligible; both focus mints were inactive. This was corrected so an eligible
secondary-cohort mint retains a request slot even with no active focus mint.
The worker now records active research size, eligible size, and scheduled size
separately. The correction is covered by an inactive-focus scheduling test. A
normal collector cycle at 2026-11-03 13:50:14 UTC then showed five sampled
tokens with current 5m DEX transactions, four with an eligible candle slot,
and one actually scheduled. All four OHLCV requests succeeded with no rate
limit. This verifies request selection, not complete candle coverage or trading
performance.

## Continuous paper-entry candle collection (2026-11-03)

The read-only verifier found 15 current market-gate-eligible candidates, and
all 15 failed the candle gate: fourteen had stale 1m history and one had a 1m
gap. The previous oldest-fetch rotation spread four public OHLCV calls per
minute across the larger ready set, so it could not reliably observe eight
timely consecutive 1m bars for any one candidate. The collector now reserves
one 1m request per normal cycle for each of up to two audited,
market-eligible candidates. Focus selection persists for an hour while each
mint remains eligible, then rotates to previously unpinned candidates; the
separate score-independent research cohort keeps its own requests. A test
verifies continuity across cycles and rotation after expiry. This is a data
collection correction, not a strategy change or proof of improved entries.
After reload, the completed 2026-11-03 14:01:26 UTC cycle scheduled both
pinned candidates plus one active research member. Four of four OHLCV requests
succeeded without a rate limit, the worker recorded no error, and the focus
selection timestamp remained unchanged. A later completed cycle again
scheduled both focus mints and one research mint. At 14:06 UTC, the production
market and candle gates classified both focus mints as market eligible with
ready 1m and 5m signals, then returned `observe` because trend, momentum,
volume, and liquidity did not align. Across the current 14 market-eligible
candidates, 11 were still candle-data-limited, three reached signal evaluation,
and zero met the full entry rule. This verifies improved forward candle
availability without claiming a profitable trade.
Both focus mints still had a `mixed_provenance` 1h signal: recent higher-frame
history combined direct pool candles and complete minute-derived candles.
That status correctly blocks an entry if lower-frame factors later align.
The occasional direct 15m/1h collection now prioritizes pinned candidates,
while keeping the two 1m focus calls; a scheduling test verifies this. Runtime
evidence at 14:11 UTC shows one pinned token received a new direct 1h candle;
its production 1h signal changed from `mixed_provenance` to `ready`. The other
pinned token still has mixed 1h history and remains blocked by that integrity
gate when lower-frame factors align. No full entry occurred.

## Active member research observation (2026-11-03)

The fixed 20-mint, score-independent research cohort had two inactive focus
mints. Rotating one public 1m request among its other active members could not
produce a continuous signal window for replay. The collector now pins one
currently trading cohort member for up to an hour, prioritizing a member that
clears the current market-only gate, then another audited safe member, then
another active member. The original cohort membership and all unavailable
members stay in the replay denominator. The current pin and ended selection
windows record the tier and times, so this activity-conditioned observation
can be separated from the original random sample. Tests cover the selected
tier, replacement when activity ceases, retained cohort membership, and
request scheduling. A completed worker cycle at 14:19 UTC reported one active
research pin scheduled, two strategy focus mints scheduled, and four of four
OHLCV requests successful without a rate limit. The selected research member
was in the audited-safe tier. By 14:19:46 UTC its 1m pool record had three
newly fetched historical bars since pinning, with the latest opened at 14:17
UTC. The first-observed timestamps remain attached, so bars fetched after a
decision cannot enter that earlier replay decision. This verifies a real fetch
path; sustained timely coverage and closed replay trades remain unverified.
At 14:25 UTC, the same pin had eight newly first-fetched 1m bars since
selection; seven were first fetched within 120 seconds of close. Its latest
stored bar opened at 14:24 and was fetched at 14:25:17. This is a short
observed coverage window, still too small for a fair trading backtest.

## Paper equity with unavailable exits (2026-11-03)

At 14:27 UTC, a read-only Jupiter v2 quote for the stranded old paper token
returned 1,344 lamports gross for the remaining bag. The paper model charges
55,000 lamports in network and priority fees for an exit. The route exists but
cannot produce positive net proceeds at that size. The old 9,642,034-lamport
mark must not be presented as realizable value. The execution model now
classifies this as `below_exit_fees`; the worker records that reason and the
gross quote, and Paper Portfolio labels it separately from a provider outage.
The stale-mark stress scenario and entry pause remain active. This is a
diagnostic correction, not a simulated sale or write-off.

The position was opened by the autonomous paper path at 08:16 UTC, with a
recorded score of 81 and candle confirmation. The nearest market snapshot
before entry showed about $332,800 in pool liquidity. Ninety recorded quote
marks from 08:19 through 13:19 stayed above its 35% stop threshold; the last
mark was 9,642,034 lamports. The first later snapshot below the configured
$15,000 open-position liquidity breaker was at 13:20:58, at about $2,814.
The latest observed liquidity was about $2,385. This is evidence of a sharp
liquidity and price collapse between usable exit observations. It does not
identify a rug mechanism or prove a missed executable stop. The entry predates
the currently required recorded market-policy version and remains open, so it
is excluded from complete closed forward-performance statistics. The episode
is retained as a qualitative stress case, not silently counted as a win or a
closed loss. The persistent entry pause also reflects the low-liquidity and
stale-mark breakers, not just the below-fee quote.

At 14:02 UTC, the paper exit monitor closed one old simulated position at its
stop loss, leaving four open. One open position had no usable exit quote; the
entry circuit paused new paper entries. The displayed portfolio equity still
included that position's old mark, which can overstate current realizable
value. Paper Portfolio now shows the total old-mark amount and a separate
scenario with stale marks valued at zero. This is a stress estimate, not a
claim that the token is worthless or that a sale occurred. A test covers live
marks, failed refreshes, and the zero-stale calculation.

## Measured OHLCV capacity (2026-11-03)

The market collector previously waited a full 30 seconds after each network
scan, even when the scan itself ran for about 30 seconds. It now schedules its
next cycle from the previous cycle's start, with a one-second minimum rest if
the work exceeded that interval. Other workers keep their prior schedule.
The fixed-host OHLCV client's request pacing and persisted 429
backoff remain in force. This removes avoidable idle time; a new measured
capacity window after restart is required before claiming any throughput gain.
The earlier figures below counted successful client calls, including possible
45-second local cache hits, so they are upper bounds on accepted network
responses. The capacity summary now excludes legacy cycles without cache-hit
accounting and subtracts cache hits from new successful calls. It waits for at
least two minutes of new observations before reporting a rate.
At 13:04 UTC, the first cache-aware window covered five cycles and 121 seconds:
seven client calls, five successful noncached responses, one cache hit, and
four cycles under a provider rate-limit or cooldown state (three without a
request). The observed 2.48 noncached successes/minute was below the 7.5/minute
minimum for 15 market-ready mints. The window began during a persisted
cooldown, so it is too short to attribute a sustained throughput change to the
new schedule. The capacity bottleneck remains open.
Over the first 23 cache-aware cycles spanning 670 seconds, the collector then
recorded 29 successful noncached responses (2.60/minute), six cache hits, and
17 cycles in a rate-limit or cooldown state. The sequence repeatedly allowed
four fresh calls, then one more before a 429 interrupted the next cycle. This
is an observation of the current shared public feed, not its published quota.
A 15-second pace was tested after restart. The first six completed cycles from
13:17:06 through 13:22:55 UTC made 24 attempts and received 24 fresh responses,
with no cache hits, 429s, or cooldown cycles. Across 350 seconds this is 4.12
fresh responses/minute, above the prior 2.60/minute observed window but still
below the 7.5/minute minimum for 15 market-ready mints. The short trial supports
keeping the slower pace for now; it does not establish a full-day provider rate
or sufficient coverage for the whole candidate set. Stale data continues to
block paper entries.
The collector had also accumulated the three successful cycles needed to raise
its persisted next-cycle request budget from four to six. At a 15-second serial
pace, six calls would take about 90 seconds without increasing requests per
minute and would delay the next DEX snapshot. The scheduler now caps the cycle
budget at four for this pace, including when an older persisted state says six
or eight. A focused test covers the persisted-state clamp; the existing faster
pace test still covers adaptive expansion and quota fallback.
After restart, the real collector completed a 4/4 cycle without a 429 and
persisted a next-cycle budget of four, correcting the pre-restart value of six.
All four workers reported running without a recorded error. The dashboard shell,
CSS, and JavaScript returned HTTP 200; an unpaired private health route returned
HTTP 401. SQLite `quick_check` was `ok`, `LIVE_TRADING_ENABLED` was false, and
the independent code-level live-execution lock was true. The full Python suite,
Ruff lint and format check, source mypy, and package build passed.
`pip-audit` against `requirements.lock` reported no known vulnerabilities.

The [DEX Screener API reference](https://docs.dexscreener.com/api/reference)
documents pair snapshots but no OHLCV endpoint. CoinGecko's
[OnchainOHLCV WebSocket](https://docs.coingecko.com/websocket/wssonchainohlcv)
is an authentic streaming alternative, but its
[pricing page](https://www.coingecko.com/en/api/pricing) places WebSocket access
on paid plans. No paid subscription was started and no credential was added.

The restarted paper collector records a bounded one-hour series of OHLCV
client-call attempts, successes, and 429 cooldown cycles. The read-only verifier
compares the measured successful request rate with the minimum one-call-per-mint
rate needed to refresh currently market-ready tokens every 120 seconds. At
12:39:18 UTC, the first recorded cycle completed 4/4 requests. Fifteen mints
were market-ready, requiring at least 7.5 calls per minute for that refresh
target alone, before research, decision follow-ups, and 15m/1h requests. A
single cycle cannot establish a measured hourly throughput; the report leaves
that value null until at least two minutes of observations exist.

At 12:43:44 UTC, four completed cycles covered 169 seconds from the first
cycle start: 11 successful requests out of 12 attempts, including one 429
interruption and one subsequent zero-request cooldown cycle. The observed rate
was **3.91 successful calls/minute** over this short window, compared with a
minimum **8 calls/minute** to refresh the 16 market-ready mints every 120
seconds. Fourteen of those 16 still lacked usable current 1m/5m confirmation.
The demand estimate excludes the research cohort, follow-ups, and 15m/1h
collection. This is a measured capacity shortfall in the current environment,
not a claim about the provider's published or future quota. The bot must keep
blocking stale entries. A sustained higher-capacity candle feed, or a smaller
prospectively declared monitored universe, is needed for broad timely coverage;
neither choice may be silently treated as a fair historical performance sample.

### Additional free-source feasibility

The [CoinGecko Demo pool OHLCV endpoint](https://docs.coingecko.com/demo/reference/pool-ohlcv-contract-address)
has the required pool candle fields, but the documented request requires a
Demo API key; a bounded keyless request from this machine returned HTTP 401.
Its [free Demo plan](https://www.coingecko.com/en/api/pricing) currently allows
10,000 successful calls per month. Even the current market-ready refresh floor
of 8 calls/minute would require about 345,600 calls in a 30-day month, before
research and higher timeframes. The free Demo tier is therefore useful for
limited experiments, but cannot resolve this continuous-coverage shortfall.
No Demo credential or paid subscription was added.
The [official Pump.fun developer API](https://docs.pump.fun/) currently
documents read-only profiles, follows, callouts, leaderboards, and public
wallet positions. It does not document a pool OHLCV route, and access requires
an approved organisation key. The existing Pump callout integration is useful
for discovery evidence but cannot replace the candle feed.

Operations health now retains the capacity shortfall as a degraded collector
reason even after an individual cycle succeeds. The Overview action card shows
the observed and required call rates and points to Strategy Lab. Tests cover
both insufficient and sufficient observed rates; this warning does not weaken
the token-specific stale-candle entry gate.
After restarting the dashboard, the live health calculation returned
`ohlcv_capacity_below_demand` with 4.06 successful calls/minute versus 8.0
needed across eight recorded cycles. The database quick check was `ok`, the
dashboard shell and updated JavaScript asset returned HTTP 200, private status
and health routes returned HTTP 401 without pairing, and the code execution
lock and environment flag remained true and false respectively.
At 12:49:15 UTC, the window had grown to ten cycles over 515 seconds:
30 successful calls in 32 attempts, or 3.50/minute against the same 8.0/minute
minimum. Four cycles carried a rate-limit/cooldown state, including two with no
requests. Fourteen of 16 currently market-ready candidates lacked usable
current candle confirmation, and the strict comparable replay and qualified
autonomous paper samples still had zero closed trades. The longer observation
supports a continuing capacity gap; it does not establish a trading edge.

At 12:53:45 UTC, fifteen cycles spanning 802 seconds had succeeded on 49 of
52 attempted OHLCV requests. The measured rate remained 3.67/minute against a
7.5/minute minimum for 15 market-ready mints. Five cycles recorded a provider
rate-limit or cooldown state, including two with no requests. The read-only
replay now includes 60 prospectively selected mints, of which 49 have some 1m
history, but it still has zero comparable closed trades and no full historical
market-gate parity. The sole closed paper position lacks unambiguous entry
signal evidence and remains excluded from qualified forward performance.

The new fixed research cohort has a two-mint focus and retains all 20 sampled
mints in the replay denominator. The preceding cohort had 424 timely 1m bars
out of 1,449 membership bars. Current replay still has zero complete comparable
closed trades; there is no supported strategy-performance conclusion. Five
legacy paper positions are open without complete entry-signal evidence and are
excluded from qualified forward performance. Live execution remains locked.

## Public OHLCV cooldown continuity (2026-11-03)

The running market collector again entered its persisted 429 cooldown. Inspection
found that the scheduler advanced its eight-cycle lane even when a 429 stopped
collection partway through that lane. A failure in the independent research
lane could therefore move the next attempt to the active-paper lane after
cooldown, delaying unbiased 1m observations. The scheduler now advances only
after a lane completes without a quota failure. Tests cover both the normal
cooldown and a 429 on research lane seven followed by a retry of that same
lane. The exact mint order may still change if the audited-safe watchlist
changes during cooldown. This improves continuity; it cannot remove the
provider's shared quota or create missing historical candles.

## Recent decision funnel (2026-11-03)

The read-only verifier now summarizes the latest 100 saved decisions from
structured audit and candle evidence. The 00:56:44 UTC worker cycle recorded
29 audited-safe candidates, four that passed the market-only gate and reached
candle evaluation, one with both required 1m/5m frames ready, and zero with
both required frames confirmed. The other 96 decisions were rejected; four
were observations. Of the four candle-evaluated candidates, 1m states were
two ready and two stale, while 5m states were two gaps, one stale, and one
ready. The one candidate with ready frames had weak momentum and volume on
both timeframes, so it was left as an observation under the current
rules. These are decision counts, not trades or an estimate of missed profit.
The helper has a focused test covering blocked, gapped, and eligible signals;
it makes no claim about later returns of rejected candidates.

## Bluesky AppView availability (2026-11-03)

The local connection consistently received HTTP 403 from
`public.api.bsky.app/xrpc/app.bsky.feed.searchPosts`. The same two bounded,
unauthenticated queries returned HTTP 200 and valid post arrays from Bluesky's
official `api.bsky.app` AppView host. The fixed-host client now uses that host;
a direct client check returned ten posts with valid URIs. No credentials or
user-supplied URL were added. The restarted narrative worker completed cycle
76 at 01:00:25 UTC without a worker error: Bluesky and Google News each
returned 30 items, while GDELT remained degraded by timeout. Only one
Bluesky item passed normalization into the saved evidence journal; raw post
count is not token-linked callout count. One successful cycle does not prove
sustained provider availability. GDELT still timed out in a separate
15-second spot check, so news/social coverage remains incomplete.
After the narrative-client change, the full suite had 153 passing tests;
Ruff lint/format, mypy, JavaScript syntax, and wheel/sdist build passed.
The dashboard returned HTTP 200, SQLite quick check was `ok`, and the live
environment flag and independent execution lock remained false/locked.

## OHLCV allocation toward paper-eligible markets (2026-11-03)

After the guarded Token-2022 opt-in, the latest 100 decision records included
18 audited-safe candidates. Only three cleared the market-only strategy gate;
all three remained observations because their 1m/5m candle confirmation was
missing, stale, or incomplete. The four active OHLCV slots previously selected
the four most recently audited safe candidates, two of which already failed
market checks. The collector now prioritizes audited-safe candidates that pass
the exact existing market-only strategy evaluation, then fills remaining slots
with other safe candidates. It still makes at most four public OHLCV requests
per cycle and leaves the independent research cohort unchanged. A regression
test verifies that an older market-ready mint enters the four-slot schedule
ahead of newer safe mints with insufficient liquidity. A read-only check of the
current journal showed the active set changing from two market-ready mints to
three. This is a collection allocation change, not an entry-rule change or a
performance result.
After restart, the market collector completed cycle 280 without an error and
persisted `active_market_ready_size=3`, 651 refreshed candle rows, and no 429
in that cycle. The older third candidate's latest 1m close was then about
84 seconds old, versus about 705 seconds before the change. Refreshed rows
include updates to existing candles; this is a freshness spot check, not 651
new observations or sustained provider reliability. The dashboard returned
HTTP 200, the database quick check was `ok`, and both live-trading gates
remained locked. Full suite: 152 passed; Ruff lint/format, mypy, JavaScript
syntax and wheel/sdist build passed.

## Token-2022 paper research coverage (2026-11-03)

Before this local configuration change, 19 of the 20 fixed research-cohort
mints had blocked latest audits. Across 440 discovered candidates, 402 audit
summaries included the default Token-2022 block, and 216 named that as their
only blocker. This is a coverage bottleneck, not evidence those mints are safe
or profitable.

The local `.env` now sets `ALLOW_TOKEN_2022=true` for guarded paper research.
The repository default and `.env.example` remain false. The existing audit
still rejects missing, malformed, unknown, or dangerous extensions and mutable
or mismatched metadata, then applies the same mint, holder and forensic gates.
Its five audit tests passed. A current Token-2022 mint passed the full RPC
audit under the opt-in. This is one spot check, not a sell or fill guarantee.
Another current Token-2022 mint with dangerous extensions remained blocked
under the same opt-in; the CLI returned a failed audit with
`dangerous_extensions` among its blocking findings.
`LIVE_TRADING_ENABLED=false` and the independent hard lock remain active.
The restarted worker completed cycle 61 at 00:48:59 UTC without an error. It
scanned 33 candidates, found 17 safe within that scan, and recorded 100
strategy evaluations. The fixed cohort's latest safe count rose from 1/20 to
5/20; across all stored discoveries the latest safe count rose from 10 to 23.
This confirms the gate now admits some previously excluded candidates, not
that they satisfy entry signals or produce positive returns. The 3/3 paper
position cap prevented new entries. The verifier still has zero valid closed
replay trades and zero complete closed autonomous forward trades. Only 31 of
449 research-membership 1m candles were first fetched within 120 seconds of
their close. No strategy performance claim is supported yet.

## Decision desk and paper evidence update (2026-11-03, 01:31 WAT)

The Overview now leads with paper positions, marked equity, complete costed exits and
worker status. Its focus panel routes to the relevant paper, discovery or research view.
System checks and the historical live-order journal remain available behind native
disclosures. Desktop and 390px mobile browser checks confirmed the loaded values,
responsive navigation and persistent paper/live lock. The Pinterest terminal pin and
Allegra transparency reference informed the hierarchy; see `UI_RESEARCH.md`.

The journal currently has three open paper positions, all opened before full signal
evidence was recorded. The forward-performance summary now counts open positions
without full entry evidence explicitly. Those three cannot enter the complete-trade
sample after they close. One older closed position is already excluded. There are
**zero complete closed autonomous exits** and no supported win-rate or profitability
estimate. The 3/3 position cap still prevents new paper entries; exit monitoring
continues. No positions were changed to improve the metric.

The cross-resolution check compared 672 overlapping direct 5m and complete-derived
1m bars from 41 pool series. Fifty-four differ by more than 1% in at least one OHLC
field. Recent close-price disagreement above 1% blocks 5m entry confirmation; mixed
recent direct/derived provenance also blocks it. Full test suite: **146 passed**;
Ruff lint/format, mypy, JavaScript syntax and wheel/sdist build passed. The local
dashboard was restarted and the paper worker resumed. `LIVE_TRADING_ENABLED=false`
and the independent live-execution lock both remain in force.

## Collector reliability update (2026-11-02)

The public OHLCV collector was configured for 24 calls per minute and runtime
collection had encountered HTTP 429 responses. GeckoTerminal's
[current support page](https://support.coingecko.com/articles/6867086195-does-geckoterminal-have-an-api)
states a 30-call-per-minute free limit; the observed effective quota on this
connection can still be lower or shared. The collector now paces calls
eight seconds apart per process, stops on the first 429, persists a bounded
cooldown, and resumes at the interrupted mint. Mock-provider regression tests
verify that 429 causes one request, no request occurs during cooldown, and the
same mint is retried afterward. This is a code-level correction; sustained
provider acceptance and resulting candle coverage still require forward
observation. Concurrent collector processes can exceed the shared public quota.
In the first restarted paper-worker observation, stored candles increased from
13,074 to 13,078 before another 429 triggered the cooldown; the provider did
not yet sustain an entire collection cycle. At eight seconds per request,
tracking 30 pools across four timeframes would revisit a pool only after many
minutes, so this free feed alone cannot support timely 1-minute signals for
the whole watchlist. Stale-data entry guards remain essential.
After the cooldown, the next observed cycle also reached a 429 after completing
one pool; the stored row count reached 13,080. The dashboard's "candles refreshed"
measure counts fetched rows including updates to existing candles, so it must
not be interpreted as that many new historical observations. The public feed's
effective quota or other consumers still constrain collection.

## Collection scheduling update (2026-11-03)

The prior three-pool rotation spent most public calls on the broad research
cohort before safe paper candidates, so the required 1m/5m confirmation was
usually stale. The collector now uses at most four requests per cycle: six of
every eight cycles refresh 1m/5m for two of the four most recent safe
candidates, one refreshes their higher frames, and one samples all four frames
for the independent research cohort. The default between-cycle pause is 30
seconds; 429 cooldown still takes precedence. Tests verify the lane sequence,
four-call cap, rotation across four safe candidates, cooldown, and resumption.

The restarted worker completed one scheduled cycle without a 429 and added
new stored candles. The next cycle refreshed another safe candidate but again
received a 429. Stored candle rows reached 13,346; paper fills remained five.
This is an improvement in allocation, not proof of continuous fresh coverage:
the public feed still cannot guarantee timely candles for every candidate.
Candidates with stale 1m/5m data remain ineligible for automatic paper entry.

## Replay parity correction (2026-11-03)

Inspection found that the earlier "current momentum" replay used only one
timeframe, while forward paper entry requires both 1m and 5m confirmation and
may reject a ready higher-timeframe downtrend. Both paths now call the same
candle-gate function. Replay selects only confirmation candles closed by the
decision time. Tests show a falling 5m signal and future-only 5m candles cannot
produce entries. Saved runs without an entry-model marker are excluded from
performance display, even when they used authentic OHLCV.

The prior one-trade loss and six censored positions came from the narrower
single-frame study and are **not evidence for the current paper strategy**.
The two censored positions found with the corrected candle gate also predate
the point-in-time audit gate below. They are not current strategy results.
Replay's intrabar exit model still differs from sampled forward Jupiter quotes.
Full production-strategy parity and out-of-sample performance remain unverified.

## Point-in-time audit evidence (2026-11-03)

Both automated scans and pasted-call audits now append a timestamped observation
when the audit finishes. Each record includes the mint, safe/blocked/error status,
summary, concentration, decimals and selected pool. Replay can use only an audit
recorded no later than its decision time, no more than ten minutes old, and safe
for that pool. A future, stale, blocked or pool-mismatched audit cannot authorize
an entry. Tests cover each case. The existing latest-status candidate table is
still useful for the current dashboard, but is no longer treated as historical
safety evidence.

Old audit observations cannot be reconstructed. They were not backfilled from
current candidate status. At 00:11:59 Africa/Lagos after the worker restart,
the database held 23 new audit observations and 14,036 real pool candles. The
current verifier found zero closed and zero censored replay positions once
both candle and audit chronology were enforced; most series report
`insufficient_audit_history`. These observations establish that the new logging
path is operating, not that the strategy is profitable. An integrity-checked
SQLite backup was made before the additive schema change:
`trade-state-pre-audit-20261102T230952.sqlite3`.

## Research-cohort audit continuity (2026-11-03)

At the pre-change database check, the 20-mint fixed research cohort had real
pool candles, but only **3 of 20** mints had any timestamped audit observation.
Sixteen selected mints had already left the latest-100 discovery view. The scan
only refreshed new feed leads and a few recently visible safe candidates, so
the replay would continue to lack contemporaneous safety evidence for much of
the cohort. Historical audit states cannot be reconstructed from current status.

The autonomous scan now reserves up to ten bounded audit checks per cycle for
the fixed cohort, whether a mint is safe, blocked or no longer in the latest
view. It prioritizes missing observations, then the oldest. New feed leads keep
their full configured audit budget, and up to three older safe candidates remain
eligible for refresh. DEX Screener enrichment splits requests at its 30-token
address limit. A failed safe refresh leaves the prior observation stale; a
failed blocked refresh records an error rather than a safe verdict. Two new
regression tests cover out-of-view collection, failed refreshes, audit budget
and API batching. This improves evidence collection but does not guarantee
every mint has a safe audit within the replay's ten-minute gate, especially
with provider outages and a five-minute pause between paper cycles. Replay
continues to exclude entries without sufficiently fresh, earlier evidence.

The restarted local worker completed a live paper scan at 23:30:31 UTC with
33 audited candidates, 100 recorded strategy evaluations and no worker error.
The current research cohort rose from 3/20 to **13/20** mints with recorded
audits in that first cycle; the remaining mints need later cycles and may still
face provider failures. The entry breaker remained `position_limit`, and the
paper fill count stayed at five. The dashboard returned HTTP 200, the SQLite
integrity check returned `ok`, and the environment live flag was false. The
service and dashboard hard-lock tests also pass with an attempted true live
flag. No strategy return comparison is supported yet.
The next completed paper cycle at 23:35:48 UTC brought the cohort to **20/20**
with audits fresh within ten minutes, with no reported worker error. This
verifies two collection cycles, not durable coverage or safety approval: 19
cohort outcomes were blocked and one was safe. The retrospective replay still has zero
valid closed trades because older candles predate the new audit observations.

## Research-only performance population (2026-11-03)

The read-only verifier previously replayed every stored 1m pool series,
including tokens tracked because they passed the current safe-watchlist filter.
That mixed a selected sample with a safety-filtered one and could bias future
performance claims. Performance replay now includes only mints with a recorded
research-cohort membership and restricts entries to their actual membership
windows. Watchlist candles remain in the coverage diagnostic, but they do not
enter the research return population. The verifier also explicitly counts
selected mints without any 1m candles rather than silently dropping them.
Each replay row names its pool so multiple pools are visible as separate
series. A regression test includes a safe watchlist token with candles and
confirms it is excluded from the research performance population.

At 00:32 Africa/Lagos, the selected population was 20 mints: 18 had 1m
history and two had none. Of the 18 series, 14 lacked adequate audit history,
three lacked enough candle history, and one had no eligible trade. There were
**zero valid closed replay trades**. These counts describe missing evidence,
not poor or profitable strategy performance. Multiple pools per mint and
provider-disappeared tokens still require explicit treatment before any
aggregated return is trusted.

## Candle availability provenance (2026-11-03)

The candle journal previously stored only `fetched_at`, which was overwritten
whenever a pool candle was refreshed. It could not establish when this bot first
obtained a historical candle. A valid backtest can use historical trade-derived
bars, but that does not prove the running, quota-limited collector could have
acted on the same bars at their close. The journal now stores
`first_fetched_at` for newly inserted candles while retaining `fetched_at` as
the latest refresh time. Existing rows remain **unknown**; later refreshes do
not invent their first-fetch time. The read-only verifier reports research
membership 1m candles with known versus unknown first-fetch provenance and
how many were first obtained within 120 seconds after their close. This is a
data-availability diagnostic, not a fill simulation or profitability metric.
The migration test verifies new insertions, repeated updates and legacy rows.
Replay now rejects a safe audit that has no recorded pool address when a
specific pool is being tested; an unknown pool can no longer satisfy the
pool-specific safety gate. A regression assertion covers this denied case.
The report now opens SQLite in read-only mode, so a performance check cannot
run migrations or alter paper state. A regression test confirms writes fail.

An integrity-checked SQLite online backup was created before the additive
column migration: `trade-state-pre-first-fetched-20261102T233339Z.sqlite3`.
After migration, all 14,139 pre-existing candle rows correctly remained
first-fetch unknown, SQLite integrity returned `ok`, and the restarted local
dashboard returned HTTP 200. At 00:34 Africa/Lagos, 246 research-membership
1m candles existed; all 246 predated this provenance field, so none can prove
timely availability. Forward collection must build a new known sample before
the replay can be called an execution-parity study.
The running collector subsequently inserted six new candle rows with recorded
first-fetch times and completed a cycle without a worker error. None of those
six was a research-membership 1m candle yet, so the research availability
denominator remains 246 unknown rows at the 00:36 check.
By 00:40 Africa/Lagos, the research-membership 1m set contained 250 rows:
four had known first-fetch times, two of those arrived within 120 seconds of
close, and the original 246 remained unknown. The sample remains far too small
to infer execution-parity performance.

## Forward decisions during an entry breaker (2026-11-03)

The three open paper positions currently fill the configured position limit.
Previously that made the autonomous worker skip all candidate signal evaluation,
leaving no evidence about the market while entries were blocked. The worker now
evaluates and journals candidate decisions during a breaker. A confirmed eligible
signal is recorded as blocked by the breaker, and the paper entry method is never
called. A breaker detected by the outer worker is passed into the decision cycle
so a later risk recalculation cannot silently authorize an entry.

At 00:26 Africa/Lagos, the restarted worker's completed cycle reported
`entry_paused=true`, `pause_reason=position_limit`, 100 candidates evaluated,
zero exits, and zero entries. The latest 100 decisions were 97 rejected and
three observed; none was an eligible signal blocked by the limit in that cycle.
The dashboard displayed **Entry locked** and explained that exits and signal
logging continue. This proves the logging path is operating while capped; it
does not establish missed profits or strategy expectancy.

## Implementation and verification

| Area | Finding | Evidence |
| --- | --- | --- |
| OHLCV | Real pool-specific, trade-derived GeckoTerminal candles for 1m, 5m, 15m and 1h are stored. DEX Screener snapshots are not converted into candles. Open candles are excluded. | 13,983 stored candles at the 00:05 snapshot; parser, timeframe, cache and stale/gap tests passed. |
| Timeframe accuracy | Stored opens align to their respective 60, 300, 900 or 3,600-second grid. Gaps are detectable. | At the 00:05 snapshot, 151/151 stored pool/timeframe groups aligned; 57 had missing intervals and cannot be treated as continuous history. |
| Indicators | Trend, momentum, pool trade volume and liquidity use verified closed candles and a pool-matched snapshot captured no later than the candle close. | Unit tests cover closed-candle inputs, gaps, stale inputs, signal decisions and next-open replay. This establishes code behavior, not predictive validity. |
| Token safety | Mint/freeze checks, Token-2022 extension checks, holder concentration, recognized pool vault exclusions and liquidity/volume filters are implemented. | Audit, holder, discovery, strategy and route tests passed. Related-wallet ownership and LP withdrawal risk remain unproven by these checks. |
| Entry and exits | Paper entry requires a current audit, a current pool snapshot, 1m/5m confirmation, higher-frame veto where ready, a route and simulation. Paper exits implement TP ladder, stop, trailing stop and max hold. | Tests exercise allowed and denied entries, stale data, intrabar stop-first replay, costs, partial TP, stop and trailing behavior. |
| Portfolio protection | Position count, daily realized loss, consecutive losses, portfolio drawdown, open-position liquidity and stale open-position marks now gate all paper entry paths. Exit management continues while entries are paused. | Shared-breaker tests cover scheduled/manual decision paths and direct entry helper. Manual dashboard entry uses the same breaker. |
| Live safety | Live swap submission is hard-locked in code, including if the environment switch or `--execute` is set. | Two live-lock regression tests and dashboard route check passed. `LIVE_TRADING_ENABLED=false` in the current environment. |
| Paper logs | Decisions persist verdict, reasons, signal evidence and action. Manual paper entries explicitly record that strategy signals were not evaluated. Fills persist cost model and exit reason; position marks retain quote time. | Journal and automation tests passed; the UI shows decisions, portfolio and performance states. Legacy decisions lack the new evidence fields. |
| Forward performance | Closed autonomous, call-driven, manual and incomplete positions are separated. Strategy metrics require recorded multi-frame signal evidence, a costed buy and every costed sell, reconciled fills and an exit reason. | Two new tests cover accepted trades, lane separation, legacy exclusions and a missing exit cost. The running journal currently has zero complete autonomous exits, three open positions and one incomplete closed position. The dashboard displays unavailable metrics rather than treating older paper P&L as strategy evidence. |
| Research sample | A persisted 24-hour cohort samples up to 20 of the 100 most recently discovered mints by stable hash, without score or audit filtering. Actual selection windows are stored; replay entries are limited to those windows. | At report time the sample held 20/100 mints, including blocked candidates. Cohort rotation, late addition, provider outage, replay cutoff and window tests passed. |

## Historical performance

| Strategy | Closed replay trades | Unclosed replay positions | Win rate | Profit factor | Net return | Maximum drawdown | Expectancy | Average hold | Loss reasons |
| --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- |
| Existing four-factor candlestick baseline with point-in-time checks | 0 comparable | 0 | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |
| Upgraded momentum strategy | not implemented | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |
| Early-launch strategy | not implemented | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |

The existing baseline is implemented but has no fairly comparable closed
replay trades because the older historical point-in-time audit and candidate
selection series was not retained. The upgraded momentum indicators remain
deferred, and the early-launch strategy has not been implemented. The replay
has no closed trades after enforcing candle, discovery, sample-selection,
liquidity and audit chronology. Most older candles were fetched before
contemporaneous liquidity or audit observations were recorded. There is
therefore no valid same-period comparison, optimization sample, or
out-of-sample performance claim.

Two saved runs predate the verified OHLCV path and are labeled `legacy_unverified`.
Two more used authentic OHLCV but predate the shared 1m/5m gate; they are labeled
`legacy_entry_model_unverified`. None of these four runs supports promotion.
At 14:22 UTC, the local paper journal held six positions (four open, two
closed). Neither closed position qualifies for autonomous strategy statistics
because complete current-format entry-to-exit evidence is missing. In-sample
versus out-of-sample testing will start only when enough point-in-time data
and complete closed trades exist.

The configured deterministic replay estimate is **2,209 basis points round trip**
for its default 0.01 SOL paper allocation. This includes modeled slip, network and
priority fees, and a full new token-account rent debit. Rent may later be reclaimed
or an account reused, so this is a conservative scenario, not a measured live fill.
Replay now adds **55 basis points per sale after the first** for the current
position size, matching the separate network and priority fees charged by the paper
fill model. This prevents a multi-stage TP ladder from receiving free extra exits.
The walk-forward summary now calculates drawdown across the complete sequence of
out-of-sample closed trades, rather than taking the worst isolated fold; replay retains
the full trade log needed for that calculation. These corrections change future replay
results, but the current authentic sample still has no closed replay trades.
It shows why micro-sized trades can look profitable before costs and fail after costs.
Real route availability, failed fills and intra-candle path uncertainty remain
additional limitations.

## Defects fixed during verification

- Market snapshots now store actual capture time separately from the minute bucket;
  replay will not use liquidity recorded after a candle's close.
- Replay refuses entries before the token was discovered by this bot.
- A persisted, audit-independent research cohort prevents the newest-discovery feed from
  dropping selected tokens before they accumulate history. Each token's actual membership
  windows are retained so replay cannot enter before selection or during an untracked gap. The collector can continue from
  a previously verified pool address during DEX Screener outages.
- Replay excludes positions that remain open at series end, encounter a data gap, or
  lose liquidity evidence from realized trade statistics. These are disclosed as
  censored positions; walk-forward promotion also requires none in the test folds.
  The local dashboard was exercised with RICO's real candle history and displayed
  `Partial history · 1 unclosed position`, zero closed trades, and unavailable returns.
- Paper fill history and the closed-position loss streak use insertion order, so
  rapid exits cannot reverse order when timestamps collide or move backward.
- Missing or stale current audits, market snapshots, candles and open-position marks
  prevent new paper entries.
- The optional Token-2022 audit now fails closed if the mint extension list is missing,
  malformed or contains an unreviewed extension. Metadata Pointer and Token Metadata
  require immutable authorities and must link to the audited mint. The default
  `ALLOW_TOKEN_2022=false` remains in place; one current RPC mint with immutable
  mint-linked metadata parsed without extension findings. This does not establish
  that its other audit gates pass.
- Pasted calls and manual paper entries now obey shared portfolio circuit breakers.
- The shared paper-entry liquidity breaker now looks up every open mint directly,
  including mints outside the latest-100 discovery view, and checks a fresh
  snapshot for that mint's recorded pool. It distinguishes a verified pool
  below the liquidity floor from missing, stale, mismatched, or nonfinite
  evidence. The autonomous worker uses the same breaker result as manual and
  pasted-call entries; both states block new paper entries while exits continue.
  Tests cover an older open mint, low and healthy liquidity, `NaN`, stale data,
  and enforcement in the worker.
  After restart, a full autonomous cycle completed at 23:40:39 UTC without
  worker error: it scanned 33 candidates, evaluated 100 decisions, and reported
  the expected `position_limit` entry lock. Paper fills remained five.
- Paper exits record their reason; decision logs include signal evidence.
- Exit quote failures now enter a deduplicated paper-health journal. The first and
  third failed cycles produce warnings, recovery is recorded, and operations health
  becomes degraded while exits cannot be evaluated. A recent saved mark is labelled
  `recent mark` in the portfolio, never treated as a fresh quote for stop or profit
  decisions. Unquotable positions stay open rather than becoming fictitious losses
  or wins. Tests cover outage, escalation, recovery, and quote-state behavior.
- Paper entries now atomically commit the position, buy fill, and linked decision.
  The transaction rechecks the shared circuit breakers, position count, and
  same-token duplicate rule after quote generation. If
  decision logging fails, the position and fill roll back. Autonomous and call-inbox
  entries require complete point-in-time candle evidence before a simulated buy.
  Manual entries were previously assigned an invalid database verdict, so a normal
  manual open could fail after quoting. They now use an `observe` verdict with a
  `manual_paper_opened` action and an explicit unevaluated-signal reason. Tests cover
  the service path, duplicate and capacity rejection, and rollback on log failure.
- Old backtest results no longer appear as verified performance; zero-trade statistics
  display as unavailable.
- The collector also watches audited-safe candidates outside the independent
  research cohort. A database check found four safe candidates in the latest 100;
  only one was in the 20-mint cohort and had 1m candles before this change.
- A runtime check at 23:51 UTC found one active paper candidate's 1m candle signal
  stale while the collector had four active safe candidates. The higher-frame cycle
  now spends two of its four public requests on 15m/1h for one active mint and the
  other two on 1m/5m for another active mint. The single-active-mint case retains
  all four frames. A focused scheduler test confirms both mints receive candles
  within the unchanged four-request budget; this is a freshness improvement, not
  evidence of better trading returns.
- The autonomous scan now reserves up to three extra audit slots per cycle for
  previously safe candidates, oldest audit first. A failed refresh preserves its
  old timestamp, which remains ineligible under the ten-minute freshness gate.
- Live execution is locked in the service and dashboard regardless of environment.
- The Strategy Lab now puts token, timeframe, chart and signal ahead of replay-only
  TP/stop settings. The settings disclosure was checked in the running browser;
  invalid hidden input reopened it for correction. See `UI_RESEARCH.md`.

## Test results and remaining work

`.venv` `pytest -q`: **145 passed**. `ruff check .`: passed. `ruff format --check .`:
passed. `mypy src tests`: passed. `node --check src/memecoin_bot/static/app.js`: passed.
The Python source distribution and wheel also built successfully. A lockfile
dependency audit with `pip-audit -r requirements.lock --no-deps --disable-pip`
found no known advisories on 2026-11-02.
The installed FastAPI/Starlette test client emits a third-party deprecation warning.
No live transaction was submitted.

After the audit-history migration, the local dashboard was restarted and returned
HTTP 200. The active database passed `PRAGMA integrity_check`; the paper worker
appended audit observations. The live-execution lock remained active with the
environment live flag false. This verifies process recovery at one restart;
it does not establish continuous multi-day availability.

On 2026-11-02 at approximately 22:38 UTC, the running collector reported 26 tracked
mints: the 20-mint research cohort, four audited-safe watchlist candidates, and open
positions with overlap. Two safe candidates outside the cohort that previously had
no 1m history now had 200 and 77 real 1m pool candles, respectively. The autonomous
scan refreshed three previously safe audits in one cycle. The portfolio was already
at its three-position limit, so this is collection and audit evidence, not evidence
of a new paper entry or improved strategy returns.

At 23:57 UTC, after the scheduler restart, the dashboard returned HTTP 200 and the
market worker recorded another successful cycle across 26 tracked mints with no
provider errors or rate limit. The paper worker completed a cycle, logged 100
decisions, and correctly held new entries at the three-position limit. The live
execution lock was true and `LIVE_TRADING_ENABLED` remained false. A read-only
verification at this point found 269 one-minute research-membership candles, 23
with known first-fetch time, and eight first fetched within 120 seconds of close.
Replay still showed 14 mints with insufficient audit history, three with
insufficient 1m history, two without 1m history, and one with no eligible trades.
There were zero closed eligible replay trades. Forward paper had three open positions
and one older closed position excluded for missing or ambiguous entry-decision
evidence. Net expectancy and drawdown are therefore unmeasured.

After the atomic-entry change, the dashboard restarted and returned HTTP 200.
The paper worker completed a full scheduled cycle at 00:04:15 UTC on 2026-11-03
with no error, evaluated 100 decisions, and retained the expected three-position
entry lock. This verifies recovery and the normal decision path after restart;
the new atomic entry path is covered by tests but has not been exercised by a new
autonomous trade in the user journal. No live transaction was sent.

After the quote-health change, the next scheduled paper cycle completed at
00:09:15 UTC on 2026-11-03 without error. It recorded zero quote failures, scanned
33 candidates, evaluated 100 decisions, and retained the three-position entry
limit. The dashboard returned HTTP 200 and live execution remained locked. This
verifies the normal no-outage path; outage, escalation, and recovery were verified
with controlled tests, not a manufactured failure in the live journal.

## Narrative evidence integrity (2026-11-03)

The running narrative collector completed another cycle at 00:12:52 UTC with
30 Google News items. GDELT timed out and the unauthenticated Bluesky search
returned HTTP 403; both remain visibly degraded and contributed no evidence.
Bluesky's own API documentation broadly describes public reads, but its
[searchPosts issue](https://github.com/bluesky-social/bsky-docs/issues/332)
records intermittent 403 responses. No access-control workaround was added.
The current feed is therefore a limited news source, not comprehensive social
callout coverage.

Inspection of the saved signals found two ticker/name-only news matches labelled
`medium` despite having zero exact mint-address mentions. Such ambiguous matches
now remain `low` confidence and have a maximum attention score of 39, below the
active threshold. Invalid, missing, stale, or far-future publication times are
excluded from signal building or new persistence; normalization no longer
substitutes the current time for a missing timestamp. Expired saved signals are
cleared even if every provider is unavailable. Promotional or social-only narratives cannot add
the two- or five-point entry bonus, even when they include a contract address.
After restarting the worker, all four saved narrative signals were low confidence
with no exact contract mentions and scores of 39, 39, 20, and 20. The dashboard
returned HTTP 200 and `LIVE_TRADING_ENABLED` remained false. These safeguards
reduce misleading attention scores; they do not establish the predictive value
of news or social features.

The full 142-test suite passes, including new source-time, ticker-linkage,
promotion, and event-order regressions. The event list now uses insertion order
as a tie-breaker for equal timestamps, fixing a flaky quote-health recovery test.
Ruff lint and formatting, strict mypy, JavaScript syntax check, wheel/sdist
build, and the pinned-dependency audit pass with no known advisories. The installed
FastAPI/Starlette test-client deprecation warning remains.
A read-only verification still finds zero eligible closed replay trades and zero
complete closed forward-paper trades; three forward positions remain open. Net
expectancy and drawdown remain unmeasured. The research cohort has 271 one-minute
candles during membership, 25 with known first-fetch time, and 10 fetched within
120 seconds after close. This is insufficient for a fair strategy comparison.
After the final restart, the paper worker completed another cycle at 00:13:52 UTC:
33 scanned, three safe, 100 decisions evaluated, no quote failures or worker error,
and no entry because the three-position limit remains active. The narrative worker
completed at 00:14:35 UTC with the same provider statuses and four low-confidence
signals. This verifies one additional normal cycle, not long-run reliability.

## Five-minute OHLCV provenance and quota (2026-11-03)

The prior four-call market schedule fetched both 1m and 5m bars for two active
tokens in most cycles. The current schedule spends those calls on 1m bars for up
to four active tokens, with spare capacity for the independent research cohort.
It still fetches direct 15m and 1h bars for an active token periodically. A missing
5m bar can be formed only from five contiguous, closed 1m bars for the same pool;
any missing or malformed minute leaves it unavailable. Direct provider 5m bars
remain preferred. The last eight 5m bars must have consistent direct or derived
provenance before they can confirm a paper entry. Backtest runs now carry a
candle-provenance version; older saved runs cannot appear as current results.

This change exposed a provider consistency limitation. A read-only comparison of
671 overlapping direct 5m bars and complete 1m groups across 37 pool series found
54 bars with at least one OHLC price differing by over 1%. The largest open-price
difference was **35.26%**; the largest close-price difference was **3.82%**.
Overlapping volume totals agreed. The cause could be provider revisions or
resolution-specific price construction; it has not been verified, so direct and
derived prices are not claimed equivalent. The read-only verifier now reports
overlap counts and maximum divergence by pool to keep this visible. Recent
overlapping bars for three active safe tokens matched exactly in a spot check;
that narrow observation does not erase the historical discrepancy.
Where both bars exist, the loader now attaches their close-price disagreement to
the direct 5m candle. A difference over 1% in the latest eight bars prevents
entry confirmation with a `conflicting_prices` status. This protects the paper
decision path from a measurable price-data conflict but is not evidence of
improved trading returns.

The first complete market-worker cycle after the schedule restart ended at
00:19:12 UTC without a rate limit or worker error. It refreshed 712 fetched
1m rows across the four active safe mints; that count includes updates to
already stored bars. One normal cycle does not establish durable freshness or
better strategy performance. The replay and costed forward-paper verifier still
show zero eligible closed trades, and live execution remains locked.
After the final code restart, the dashboard returned HTTP 200 and the 00:22:20 UTC
market cycle completed with 199 refreshed rows before another HTTP 429 initiated
the bounded cooldown. The collector remained running; the public feed's effective
quota continues to limit forward freshness. The narrative collector completed
without a worker error. The paper worker completed at 00:23:23 UTC with 33 scanned,
three safe, 100 decisions evaluated, zero quote failures, and no new position
because the three-position limit remained active. This verifies the normal
post-restart decision path, not a new eligible entry or improved returns.

The immediate next step is to keep collecting fresh pool OHLCV and timestamped
liquidity observations over a broad, unfiltered discovery universe. Measure missing
intervals and source outages. The cohort is bounded to recent discoveries and is not a
complete unbiased launch universe; failed or undiscovered launches remain missing. After
enough trades exist, freeze an in-sample period,
compare variants on the same later out-of-sample tokens and dates, then inspect cost,
entry timing and loss attribution. Only then should indicator, stop or TP changes be
selected. No condition has yet been shown to improve net expectancy or drawdown.

**Readiness:** The system is suitable for cautious, local forward paper observation
with entry logging and fail-closed data gates. It is not ready for a profitability
claim or live trading. Continuous collection requires the local dashboard/worker to
remain running; availability over days and reliable execution of every paper exit
have not yet been established.

## Paper dashboard quote-state correction (2026-11-03)

The read-only portfolio endpoint returns saved marks for open positions; it obtains
live exit quotes only when asked to refresh. The overview had treated every saved
mark as an exit-quote failure. This was misleading while the paper worker's last
completed cycle reported zero quote failures and an active position-cap pause.
The overview now distinguishes saved or old marks from actual worker quote failures,
labels estimated equity, and prioritizes monitoring existing positions. The paper
page makes the same valuation distinction and offers an explicit quote refresh.
Browser checks showed three open positions, `Monitor existing positions` on the
overview, and `3 old or missing saved marks` on the portfolio. Individual rows now
say `old mark`, `saved mark`, or `quote failed` as appropriate. No strategy gate,
automated exit, or live-execution path changed. The 146-test suite, Ruff,
strict mypy, JavaScript syntax check, and package build pass. The paper worker
completed another cycle at 00:36:46 UTC with no error or exit-quote failure;
entries remained paused at the three-position limit. Three open legacy positions
still lack complete entry evidence; there are zero eligible closed autonomous
trades and no measured net expectancy.

## Autonomous paper exit safety (2026-11-03)

The exit loop could evaluate additional take-profit stages from the full-position
mark obtained before a partial sale. A failed re-quote for another position could
also leave its earlier mark eligible later in the same loop. The worker now
refreshes the portfolio after each partial sale, evaluates subsequent exits only
from that refreshed position map, and records post-sale quote outages. A failed
exit quote or automated exit attempt pauses new autonomous paper entries while
the worker remains active to retry exits. The overview distinguishes an exit
failure from a quote failure. Regression tests cover a lower post-sale mark, a
failed post-sale quote, another position losing its quote after the refresh,
initially unquotable open positions, and failed automated closes.

The full 151-test suite, Ruff lint and formatting, strict mypy, JavaScript syntax
check, package build, pinned-dependency audit, and changed-file secret pattern
scan pass. These checks validate the failure paths with
controlled paper-service responses; no live funds were used. Historical replay
still contains zero eligible closed trades, so the change has no measured effect
on returns or drawdown yet. After the final restart, the paper worker completed
cycle 60 at 00:42:32 UTC with no worker error, exit failure, or quote failure;
new entries remained paused by the three-position limit. This confirms a normal
cycle on the updated process, not an observed failure recovery or profitable trade.

## Point-in-time candle availability correction (2026-11-03)

The journal records when each authentic pool candle was first fetched, but replay
previously made that candle available at its historical close even if the provider
delivered it later. This could overstate historical entry quality. Replay now
uses only entry and confirmation candles fetched before each decision second.
Five-minute bars assembled from minute bars inherit the latest component fetch
time. Repeated fetches previously overwrote OHLCV values, creating another
possible revision bias. Newly inserted closed bars now retain their first
observed OHLCV values; re-fetches update only the last-fetch timestamp. An
additive `first_observed_values_at` column leaves all older rows null because
their original values cannot be reconstructed. Replay excludes those rows from
entry evidence without deleting them. Complete derived 5m bars require all
five component bars to have trusted first-observation values.
An SQLite backup (`state-backups/pre-immutable-20261103T011754Z.sqlite3`)
passed `PRAGMA quick_check` before the additive migration.
The forward paper signal applies the same gate to its recent candle window;
historical legacy bars cannot justify a new entry while they age out. Existing
positions remain in the automated exit monitor.
The walk-forward result reports insufficient candle availability when every
out-of-sample fold lacks an observable entry or confirmation history. Saved runs
from older candle-provenance versions are excluded from current performance.

Regression tests cover delayed one-minute and five-minute candles, unknown fetch
times, immutable first-observation values, legacy rows of unknown provenance,
and a derived five-minute bar whose last minute arrived late. The full
158-test suite, Ruff lint and formatting, strict mypy, and package build pass. At the first
read-only check after the provenance migration (about 01:19 UTC), the research
replay has **zero closed trades**:
11 pool series lack historical audits, three lack enough candles, five lack
trusted point-in-time candle values, and two lack 1m history. Of 506 1m
candles within sampled membership windows, 260 have known first-fetch times,
but none have certified first-observation values because they predate this
migration. New closed bars will acquire that evidence as collection continues.
Across the entire candle table, 458 new bars already had trusted first-observation
values at the next read-only check; the research cohort had not yet received
new membership-window bars.
The actual forward autonomous sample still has zero complete closed trades.
There is no defensible win rate, expectancy, drawdown comparison, or indicator
ranking yet. The restarted dashboard responds on localhost, its paper and data
workers are running, the SQLite integrity check is `ok`, and both the runtime
flag and code lock keep live execution disabled.
The final-process paper cycle 70 completed at 01:21:30 UTC with 100 decisions
evaluated, zero exit or quote failures, and no new position because the three
existing positions occupy the cap. Among the latest 100 decisions, eight
passed the market gate, none had both required candle frames ready, and two
5m signals were explicitly rejected as `unverified_values`. The collector
continued recording new trusted bars; 479 existed across all pools at the
next read-only check. This confirms the fail-closed forward gate is active,
not a measured improvement in trading performance.

## Quota-aware OHLCV collection and measured coverage (2026-11-03)

The public OHLCV quota could not keep all 20 score-independent research mints
current at one-minute resolution. The collector now follows a fixed eight-mint
subset chosen from that cohort before looking at momentum or audit scores. The
full 20-mint membership and selection history remain recorded for bias checks.
Each cycle has at most four OHLCV calls: normally two active candidates and two
research mints, interleaved so a limit on the third request does not always
starve research. Occasional cycles refresh active 15m/1h bars or give four
slots to the research subset. Closed 5m bars continue to be built from complete
one-minute buckets. This is a data-collection schedule, not an entry approval.

An empty provider response previously had no saved fetch time and kept winning
the oldest-first queue. Empty results and non-quota validation failures now
receive a persisted, pool-and-timeframe-specific retry delay (five minutes;
15 minutes for pool-not-found or identity mismatch). The pool stays in the
sample and is reported as missing history. Rate limits retain their separate
global backoff. The persisted worker summary exposes the collection lane,
bounded attempt/success/empty counts, retry-delay count, and aggregate error
categories without exposing provider bodies or credentials. A successful
client call can be served from its short-lived cache, so the success count is
not an exact HTTP request count.

The restarted local worker completed cycle 468 at 03:41:46 UTC: four OHLCV
operations attempted, one returned candles, and four pool/timeframe retry
delays were saved. Cycle 469 completed at 03:42:34 UTC: three attempted, two
returned successfully (one empty), and the provider rate limited the third.
The latest paper worker cycle completed without a worker error. The dashboard
returned HTTP 200; SQLite `PRAGMA quick_check` returned `ok`.

At the read-only check, research membership contained 816 one-minute candle
rows; 310 had trusted first-observed values and only 135 had first been fetched
within 120 seconds of close. Of the eight focused mints, one still had no
valid one-minute history, three were in retry delay, and several last-fetch
ages were in the tens of minutes. This is **not yet continuous point-in-time
one-minute coverage**. The momentum replay still has zero eligible closed
trades across 21 pool series. Original candlestick replay lacks historical
point-in-time audit/candidate series; early-launch replay is unimplemented.
Forward autonomous paper and call-inbox samples also have zero complete closed
trades. No returns, expectancy, drawdown advantage, or indicator ranking can
be claimed from these observations. The three open legacy positions continue
to be monitored, and new autonomous entries remain subject to the position
cap and fail-closed candle gate. `LIVE_TRADING_ENABLED=false` and the code-level
live-execution lock remain in force.

The full test suite, Ruff lint and format checks, strict mypy, and wheel build
pass after this change. Focused regressions exercise mixed active/research
quota scheduling, empty and invalid OHLCV retry delay, and sanitized worker
summaries. The next measurement is whether enough first-observed bars and
complete paper trades accrue for an honest out-of-sample comparison; provider
limits may require a different data source before that is possible.

The subsequent cycle 471 completed at 03:44:04 UTC with three successful
client calls out of four attempts and one categorized invalid-OHLCV response;
five pool/timeframe retry delays were active. Two focus mints had been fetched
within 30 seconds at the next check, while other focus mints were still stale
or unavailable. The paper worker reported zero exit and quote failures and a
`position_limit` entry pause. JavaScript syntax checking and `pip-audit` also
passed; the local project package itself is not listed on PyPI and was skipped
by the dependency audit.

## Conflicting provider candle correction (2026-11-03)

A focused research pool returned duplicate one-minute timestamps with different
OHLCV values. The parser previously rejected the entire response, including
unrelated valid history. It now discards the disputed interval and keeps only
unambiguous closed intervals. A response consisting solely of conflicting
duplicates still fails. The downstream entry and replay rules require a
contiguous recent window and a fresh latest bar, so a missing disputed minute
cannot silently become a valid recent signal. A regression test covers mixed
valid/conflicting rows; the existing fail-closed gap test remains in place.

The same provider response then yielded 13 unambiguous closed bars, but its
newest bar had closed about 6.5 hours earlier. It therefore remains stale for
current entry despite the parser fix. The full test suite, Ruff lint and format,
strict mypy, and wheel build pass; the dashboard was restarted with the fix.
This increases honest historical coverage without claiming a tradable signal
or strategy performance.

The first completed collector cycle after restart (475 at 03:47:26 UTC) used
the research-only lane: all four client calls returned successfully, with no
provider error recorded in that cycle. Focus freshness was still mixed; two
mints had fetched within a minute, one had no valid minute history, and others
remained minutes to hours old. The worker had no last error, and the local
dashboard returned HTTP 200 with SQLite integrity `ok` and live execution
still disabled and code-locked.

## Strategy Lab candle-readiness display (2026-11-03)

The tracked-token selector previously sorted by total stored candle count,
which could put hours-old or legacy rows ahead of current first-observed bars.
The journal now returns separate total and first-observed counts and the latest
first-observed open time. The authenticated Strategy Lab summary derives the
age of the latest trusted one-minute close; the candle endpoint reports the
same age for the selected timeframe. The UI sorts recent trusted bars first
and labels older or unverified rows, while the chart note shows the selected
bar's age. This does not change trading signals or risk gates.

On the running journal at the read-only check, 31 tracked pool series had at
least one stored one-minute bar: three had a first-observed bar closed within
120 seconds, 12 had older trusted bars, and 16 had only unverified legacy bars.
These labels describe data freshness, **not signal readiness**. In the latest
100 saved decisions, five passed the market-only gate, one had both required
candle frames ready, and none confirmed an entry. The paper worker completed
cycle 96 with no quote or exit failure; entries remained paused at the
three-position cap. Focused API and journal tests, the full test suite, Ruff,
strict mypy, JavaScript syntax check, and wheel build pass. After restart, the
dashboard responded on localhost, both workers were running, SQLite integrity
was `ok`, and live execution remained disabled and code-locked.

## Replay chronology and exit observation correction (2026-11-03)

The replay fixtures had assigned first-fetch timestamps one second *before*
candle close, an impossible state for this closed-bar provider. They now use
one second after close. Both historical replay and forward signal validation
reject provenance timestamps before the bar closes. With realistic fixture
timing, the replay waits for a later decision bar and fills only at a
subsequent bar open; tests for entry timing and research membership windows
were updated to reflect that delay. Stored authentic rows were not rewritten.

The old replay could also use an OHLC exit bar backfilled long after the
modeled exit to claim a stop or take-profit fill. It now censors the trade as
`exit_candle_unobserved` if either first-fetch or first-observed-value time is
missing, before close, or more than 120 seconds after close. Tests cover both
late timestamps. This is a conservative data-availability gate; the replay's
intrabar stop/target prices still assume resting orders and do not match the
forward paper worker's quote-polling execution. Walk-forward output now says
`lookahead_protection=false`, names the exit hypothesis, and exposes a failed
`paper_exit_model_parity` promotion gate. The candle provenance version was
advanced so older saved runs cannot be presented as current evidence.

The research cohort is still collected, but its 24-hour fixed membership can
become stale in this market. At the diagnostic check, six new candidates had
been discovered in five minutes while all eight focused cohort mints lacked a
recent trade candle; seven had old bars and one had no valid minute bar.
Changing cohort cadence would affect entry windows and exit censoring, so it
requires a separate prospective design rather than selecting only survivors.
The read-only verifier now distinguishes 143 prompt first fetches from 94 bars
that were both first-observed and fetched within 120 seconds of close, among
857 one-minute membership rows at that check. The current replay still has
zero eligible closed trades and forward autonomous paper has zero complete
closed trades. No performance advantage is established.

The full test suite, Ruff lint and formatting, strict mypy, JavaScript syntax
check, and wheel build pass with the revised chronology and exit gate. Live
submission remains code-locked and the environment flag remains false.

The replay log also previously labelled the current decision bar as the
signal bar and used that unobserved bar to classify the market regime. It now
logs the last candle actually visible to the signal, records the decision
second separately, and classifies the regime from the same visible window.
A regression changes the unobserved bar radically and verifies that signal
score and regime stay unchanged. The provenance version was advanced again
so saved results from the earlier diagnostic calculation are excluded.

After the final restart, market cycle 494 completed at 04:04:39 UTC without
a worker error. Paper cycle 100 had completed with zero quote or exit failures
and an entry pause from the existing three-position limit. The dashboard
returned HTTP 200, SQLite integrity was `ok`, and live execution was disabled
by both the environment flag and code lock.

## Shared forward exit policy (2026-11-03)

The forward paper worker's mark-based stop, next TP stage, trailing stop, and
max-hold rules now delegate to a typed pure exit-policy function. The worker
continues to require a live quote and uses the same `PaperTradingService`
partial and full fills. A malformed TP stage or open time cannot prevent a
stop-loss exit from being selected; regression tests cover that priority and
the unchanged partial-profit behavior. The historical OHLC replay has not yet
been switched to quote-polling proxy marks or this shared policy, so its
`paper_exit_model_parity` gate remains false and no historical profitability
claim is made. The full test suite, Ruff lint and format checks, strict mypy,
JavaScript syntax check, and wheel build pass after the extraction.
The restarted process completed paper cycle 102 at 04:09:17 UTC with no worker,
exit, or quote failure; new entries remained paused by the three-position
limit. Market cycle 498 also completed without error. SQLite integrity was
`ok`, the local dashboard returned HTTP 200, and live execution remained
disabled and code-locked. This verifies a normal running cycle, not a triggered
TP or stop on the new process.

## Polling-aligned historical exit replay (2026-11-03)

The Strategy Lab, read-only verifier, and walk-forward evaluator now use a
closed-candle polling proxy at the configured paper-worker interval. A mark is
eligible only when its first fetch and first observation preceded that poll.
The mark calls the same typed exit rule as the forward worker; a selected exit
fills at a later candle open. The earlier intrabar OHLC path remains a
callable sensitivity test. Six targeted tests cover a transient intrabar
spike, a candle fetched after the poll, a stopped position filled at a later
gap price, late exit observations, a stale mark after missed polls, and partial
profit followed by a trailing exit. The candle provenance version advanced
to v9 so earlier saved runs
are not shown as current evidence.

This is **rule parity**, not full execution parity. Historical closes and
later opens do not reproduce the worker's exact scheduling, Jupiter exit
quotes, route changes, or fill impact. The `paper_exit_model_parity` promotion
gate stays false and `lookahead_protection` remains false for performance
claims. Thin liquidity, candle gaps, and missing or late exit observations
censor trades rather than assign a profitable fill.

The read-only verifier at 04:13 UTC found 21 recorded research-cohort replay
rows with **zero eligible closed replay trades**. Ten lack usable historical
audit coverage, one lacks confirmation history, three lack enough overall
history, three lack prompt candle availability, two have no eligible signal,
and two have no one-minute history. Of 882 one-minute membership bars, 101
were both first observed and fetched within 120 seconds of close. The
original candlestick strategy has no comparable point-in-time audit and
candidate history; the early-launch strategy is unimplemented. Forward paper
has three open positions and one closed position excluded for ambiguous entry
evidence, leaving zero complete closed autonomous trades. Win rate, profit
factor, net expectancy, and drawdown remain unestablished.

On the resumed verification at 07:17 WAT, the same 21 research-cohort replay
rows still produced zero eligible closed trades. One-minute membership bars
had grown to 887, with 102 both first observed and fetched within 120 seconds
of close. Forward paper still had three open positions and one excluded closed
position. The final v9 polling model also rejects a stale candle after missed
polls; a regression covers that case. The full test suite, Ruff lint and
format, strict mypy, JavaScript syntax, and wheel build passed after this fix.
The dependency audit was attempted against `requirements.lock`, but returned
no result after two minutes and was stopped; its vulnerability status is
unverified in this run. No dependency versions changed during this work.

After the final restart, paper cycle 106 completed at 07:24 WAT with no worker,
quote, or exit failure. New entries remained paused by the three-position
limit. Market and narrative collection resumed, the local dashboard returned
HTTP 200, SQLite integrity was `ok`, and live trading remained false in the
environment and locked in code. A fresh read-only verifier still found zero
eligible closed replay trades across the 21 research rows and zero complete
closed autonomous paper trades. Of 976 one-minute membership bars, only 105
were both first observed and fetched promptly. The worker is operational;
its strategy performance is still unproven.

## Stale OHLCV response cooldown (2026-11-03)

The fixed research focus had seven of eight mints with no recent one-minute
close at a read-only check; several were hours old. The provider still returned
nonempty historical pages for inactive pools, so the scheduler kept choosing
them as its oldest fetches. The collector now records those historical bars
and places the pool/timeframe on a 15-minute retry cooldown when its newest
close is stale. Cohort membership and missing-history accounting are retained.
The worker summary reports stale responses separately from empty responses,
rate limits, and provider errors. A regression verifies that a stale pool is
deferred while another selected pool continues receiving calls. The full test
suite, Ruff lint/format, strict mypy, JavaScript syntax, and wheel build pass.
This improves allocation of the public call budget, but does not yet prove
fresh data coverage or trading performance. The first collector cycle after
restart encountered a provider rate limit after three attempts; no stale
response occurred in that cycle. The cooldown needs a successful real-data
observation before its effect can be measured.

The following collector cycle (524 at 07:29 WAT) completed four client calls
with no provider error, counted two nonempty stale responses, and had three
pool/timeframe entries in the bounded retry cooldown. This verifies the new
classification and scheduling state on real responses. Paper cycle 107 also
completed without a worker, quote, or exit error. A later prospective sample
is still needed to measure whether prompt candle coverage improves.

## Prospective fresh-discovery cohort (2026-11-03)

The 24-hour cohort selected eight hours earlier had seven of eight focus mints
without recent one-minute closes. A read-only check found 30 newly discovered
mints within the prior hour, all independently audited, so keeping that old
focus was wasting the limited public candle budget. A new cohort version now
selects up to 20 mints from discoveries first seen within the prior hour,
using a stable hash and ignoring score and audit verdict. Its membership is
fixed for three hours. The old 24-hour memberships remain in SQLite for audit;
the verifier filters replay, selection windows, and candle availability to
the new selection method. The Strategy Lab also rejects a pasted token with
no recorded membership instead of silently replaying it as part of this
research population. This is a prospective sampling change, so old and new
cohort results must not be pooled into one strategy performance statistic.

After restart at 07:36 WAT, the collector recorded 20 new memberships from
30 eligible discoveries. The read-only report counted the previous 20 mints
as excluded legacy memberships. The first v2 check had zero post-selection
one-minute membership bars; later cycles encountered stale responses and a
provider rate limit, and still yielded zero eligible closed v2 replay trades.
Paper cycle 109 completed without worker, quote, or exit errors; new entries
remained paused by the existing three-position cap. Full tests, Ruff lint and
format, strict mypy, JavaScript syntax, and wheel build passed. SQLite
integrity was `ok`, the local dashboard returned HTTP 200, and live trading
remained disabled by both environment flag and code lock.

The verifier's "both first observed and fetched promptly" count now requires
**both** timestamps to fall within 120 seconds of candle close. Previously it
only required a prompt fetch plus the presence of an observation timestamp;
that could overstate decision-time availability. A regression includes a bar
fetched promptly but observed late and excludes it from the strict count.
No past results were promoted on the basis of the old count.
At 07:41 WAT, the strict v2 count had risen to four promptly fetched and
observed one-minute membership bars, with zero eligible closed replay trades.
This shows the new sample has begun collecting usable prospective evidence;
it is far below the history needed for a fair strategy comparison.

## DEX snapshot coverage for paper and research lanes (2026-11-03)

At 07:41 WAT, two recent candidates had both required candle frames ready but
no confirmed entry. One had an 80-point confirmed 1m signal and a 55-point 5m
signal with zero recorded liquidity. Its last same-pool liquidity observation
before the 5m close was 713 seconds old; the next one arrived 41 seconds after
the close. The latter cannot be used as decision-time evidence. The token was
audited safe and passed the current market gate, but was 30th on the safe
watchlist. Three open positions plus 20 cohort mints occupied the first 23
slots of the 30-address DEX request. This caused candle collection without a
contemporaneous liquidity snapshot. Entry rejection was correct; snapshot
collection was incomplete.

The collector now puts open positions and currently market-eligible audited
paper candidates ahead of the general safe list. It uses at most two batches
of 30 addresses so the independent research cohort can continue receiving
snapshots. A failed batch leaves a successful one intact. The worker records
attempted and successful DEX requests and tracked priority/cohort counts. DEX
Screener's [API reference](https://docs.dexscreener.com/api/reference) documents
30 addresses per tokens request and a 300 requests/minute endpoint limit. Two
requests per approximately 30-second cycle are well below that published limit;
other processes or shared IP usage can still affect availability.

The regression creates a market-eligible candidate below the first 30 safe
discoveries alongside 20 score-independent cohort mints. It confirms both
30-address batches, the candidate's recorded liquidity snapshot, full cohort
coverage, and retention of the first batch when the second fails. The first
real collector cycle after restart completed at 07:48 WAT without worker or
provider errors: 60 addresses tracked, 2/2 DEX requests successful, 10
priority-safe candidates and all 20 cohort mints included. SQLite integrity
was `ok`, the dashboard returned HTTP 200, the environment live flag was false,
and the code live lock remained true. The full test suite, Ruff lint/format,
strict mypy, JavaScript syntax, and wheel build passed after the collection
change. An error-message compatibility test failed after the first run; the
message was restored, and the full suite passed on rerun.

At 07:48 WAT, the v2 cohort had 45 one-minute membership bars, of which 26
were first observed and fetched within 120 seconds of close. Eleven of 20
selected mints had any 1m history. The v2 replay still had zero eligible closed
trades. Forward paper had three open positions without complete entry-signal
evidence and one excluded closed position, leaving zero complete closed
autonomous trades. The existing three-position limit pauses new entries. No
win rate, profit factor, expectancy, or drawdown can be inferred from this
sample; a later prospective period is required.

At 07:50 WAT after the final restart, the collector was running without a
worker error. Its latest completed cycle again tracked 60 addresses with 2/2
DEX requests successful, 12 priority-safe candidates and all 20 cohort mints.
The public OHLCV provider was in its existing rate-limit cooldown, so this DEX
coverage does not imply complete or timely candle coverage. The dashboard
returned HTTP 200, SQLite integrity remained `ok`, and both live safeguards
remained off/locked. The full test suite passed after the stronger 15-candidate
contention regression; Ruff lint/format, strict mypy, JavaScript syntax, and
wheel build passed as well.

## Replay liquidity known at fill time (2026-11-03)

An audit of the replay exit paths found a look-ahead defect. The primary
paper-polling replay priced an exit at a later candle's open but checked that
same candle's attached liquidity. That liquidity can be captured after the
modeled fill. A failing regression confirmed the effect: changing only the
later fill candle's liquidity from $50,000 to zero changed a completed
max-hold exit into a censored trade. Polling replay now checks the last timely
observed mark's liquidity at order time. The secondary intrabar sensitivity
model also uses the latest prior bar observed before each bar opens, with a
freshness bound, instead of the current bar's later liquidity. Regressions
cover both models and unknown prior liquidity. This fixes prospective replay
logic; previously saved backtests should be treated as legacy results and
recomputed before any strategy comparison. Replay compatibility was bumped to
v11 so saved v10 results cannot be promoted as current. The read-only verifier
already classifies all four saved runs as legacy for older source or entry-model
metadata. Current v2 history remains too short for a performance conclusion.

After the final restart at 07:54 WAT, all three workers reported running with
no last error, the dashboard returned HTTP 200, SQLite integrity was `ok`, the
environment live flag was false, and the code live lock was true. The verifier
used replay version v11, counted 63 v2 one-minute membership bars including 41
first observed and fetched within 120 seconds of close, and still found zero
eligible closed replay trades and zero complete closed autonomous paper trades.
The full test suite, Ruff lint/format, strict mypy, JavaScript syntax, and wheel
build all passed after the replay change.

## Count newly stored candles accurately (2026-11-03)

The collector's `authentic_candles_refreshed` counter previously returned the
number of rows in provider responses, including historical candles already in
SQLite. A cycle could report hundreds of refreshed candles while adding only
a few new rows. This overstated progress toward prospective replay coverage.
The journal now returns the net number of newly inserted candle rows while
preserving existing first-fetch provenance and later `fetched_at` refreshes.
The database count and inserts are measured in one SQLite write transaction.
The Strategy Lab labels the result “new candles,” and tests cover a new row,
duplicate provider values, an older legacy row, and an empty response.

After restarting the collector at 06:58 UTC, the table contained 25,395 pool
candles. Its first completed cycle reported four new candles, and the table
contained 25,399 rows afterward. The worker reported no cycle error. The full
test suite, Ruff lint/format, strict mypy, JavaScript syntax, and wheel build
passed. This corrects the measurement; it does not increase the amount of
historical data or establish trading performance.

## Honest walk-forward indicator comparisons (2026-11-03)

The Strategy Lab previously ran its indicator ablations across the entire
series using fixed default parameters. Those rows mixed training and test
periods and could be mistaken for out-of-sample indicator evidence. Ablations
now replay each unseen test fold using that fold's training-selected threshold
and stop, the same execution costs, membership windows, audit observations,
and exit model as the full model. Their outputs state
`frozen_out_of_sample_folds`, include the fold and censored-trade counts, and
mark variants with no closed trades as `no_closed_trades`. The full-model
ablation is the same out-of-sample result as the main evaluation.

A failing regression also showed that disabling liquidity scoring allowed a
signal with zero liquidity to confirm. The minimum-liquidity entry gate now
remains mandatory in every ablation. An empty synthetic walk-forward sample
had previously reported `complete` despite zero closed training and test
trades; it now reports `insufficient_training_trades`, with an explicit reason.
The Strategy Lab clears old gate and regime displays when a new evaluation has
insufficient evidence. Replay compatibility is v12, so earlier saved results
cannot be promoted under the revised methodology. These are methodology and
reporting corrections; current real data still cannot establish which factor
improves net expectancy.

The full test suite, Ruff lint/format, strict mypy, JavaScript syntax, and
wheel build passed after this change. After the dashboard restart at 07:04
UTC, the local page returned HTTP 200, SQLite integrity was `ok`, all three
workers reported running without a last error, the environment live flag was
false, and the code live lock was true. The verifier used replay version v12.
The current 20-mint cohort had 111 recorded 1m membership bars, 62 both first
observed and fetched within 120 seconds of close, and still zero eligible
closed replay trades. There were zero complete closed autonomous paper trades.
No indicator improvement or net expectancy can yet be estimated from this
prospective sample.

## Sample the remaining research cohort (2026-11-03)

The v2 cohort contained 20 mints, but the fixed eight-mint focus had six
currently blocked tokens. At a read-only check, eight cohort mints had no 1m
history; six of those were currently audited safe with known pools. The
scheduled `research_1m` cycle was supposed to reach the 12 remaining cohort
mints, but its shared four-call limit was filled by four focus mints first.
The remaining cohort could receive requests only when focus mints were
unavailable. A regression reproduced this: with all 20 pools available and
the research cycle selected, all four calls went to the focus.

The research cycle now reserves two calls for the focus and two for the
remaining cohort, then alternates them so an early provider limit cannot
systematically exclude the latter. If a group has fewer available pools, the
other can fill unused slots. The collector summary records attempted and
successful remaining-cohort calls. The cohort selection and membership remain
score independent; the change only allocates the limited data requests more
faithfully across its preselected members. A real cycle and resulting
point-in-time candle coverage must still be observed after restart before
claiming that this improves replay availability or performance.

After the dashboard restart, the scheduled `research_1m` cycle completed at
2026-11-03 07:11:21 UTC. The persisted collector summary recorded two
remaining-cohort requests attempted and two successful, with no worker error.
The read-only verifier then counted 194 cohort-membership 1m bars (previously
111), including 79 with trusted first-observed provenance and fetch within
120 seconds of close (previously 62). Fourteen of the 20 selected mints now
have some 1m history (previously 12). These are coverage counts, not
evidence that all candles needed for replay were captured at decision time.
The momentum replay still had zero eligible closed trades, and forward paper
performance still had zero complete closed autonomous trades. The three open
legacy paper positions lack entry-signal evidence. Strategy profitability and
relative indicator value remain unmeasurable from this sample.

## Explain the absent replay entries (2026-11-03)

The backtest now records one first blocking gate per evaluated decision point
inside a recorded research entry window. This is diagnostic accounting in the
existing point-in-time replay loop; it does not change entry, exit, cost, or
risk rules. Regression tests verify that failed 5m confirmation, a blocked
audit, and a pool mismatch are reported while still preventing trades. The
read-only verifier includes the counts for each replay mint.

At the first post-change read-only evaluation, three of the 20 cohort mints
had enough history to reach a complete no-trade replay. Across their 105
in-window candidate decision points, the first rejection was: 1m signal
unconfirmed 55 times, 1m stale 29, 1m gap 10, 5m signal unconfirmed 5,
1m still collecting 4, and 5m gap 2. The other 17 mints remained in
missing or insufficient-history states, so these counts are not an estimate
for the full sampled population. None of the 105 points reached the audit or
execution gate. The unconfirmed count alone cannot show whether relaxing an
indicator would improve net expectancy: the sample has no complete closed
trades for that comparison. The data gaps and staleness also show that
stored historical bars do not imply usable decision-time history.

## Unblock forward paper sampling without removing legacy positions (2026-11-03)

At 07:22:55 UTC, the autonomous paper cycle completed normally but reported
`position_limit`: three older open paper positions filled the configured
three-slot cap. They had fresh exit quotes and no quote or exit failures, but
their original entries lack the signal evidence required for performance
measurement. The only active entry breaker was the slot cap. No existing
position was deleted or force-closed.

The local `.env` paper capacity was raised to five slots. The autonomous
position size remains 1% of available paper cash, and the portfolio drawdown,
daily loss, consecutive loss, liquidity, audit, and quote-freshness breakers
remain in force. Live execution is still both environment-disabled and code
locked. After a safe dashboard restart, the 07:26:00 UTC paper cycle completed
with no entry pause, no quote or exit failure, and one autonomous paper entry.
The journal then held four open positions; the new position had recorded
execution evidence, while the three older positions still lacked complete
entry-signal evidence. This is a forward data-collection milestone, not a
closed-trade result or a profitability claim. The local five-slot override is
reversible and does not change the documented conservative default of three.
The new position links to a persisted `paper_opened` decision with score 81,
eight recorded reasons, point-in-time market and audit fields, and 1m, 5m,
15m, and 1h candle signals. Its stored entry execution includes adverse
slippage, price impact, network and priority fees, token-account rent, quote
age, liquidity, and explicit cost. This verifies the required entry record for
this one new trade; exit reason and realized net result remain pending.

## Persist in-progress worker cycles (2026-11-03)

The paper scan that opened the first complete-evidence position ran from
07:24:07 to 07:26:00 UTC. During that interval the persisted worker record
still showed the prior completed cycle because `run_once` wrote state only
after the work ended. A regression test reproduced the missing start record.
The worker now persists its new `last_started_at` before invoking the cycle,
then persists completion or failure as before. This changes observability,
not scan cadence or trade decisions. The test verifies a durable start record
exists while the cycle callback is executing. After a safe dashboard restart,
the paper worker persisted a start at 07:29:53 UTC while its last completion
still showed 07:26:00 UTC, demonstrating the intended in-progress record for
that cycle. A later restart revealed that this write was not yet reliable.

## Reproduce paper entry signals at decision time (2026-11-03)

The first complete-evidence autonomous paper entry was independently replayed
from locally saved candles. A naive retrospective load disagreed on its 15m
and 1h signals because those older bars were fetched only after the entry.
Filtering every frame to candles whose first fetch and first observed values
were no later than the recorded decision time reproduced all four saved
signals and the eligible 1m/5m verdict. This is a point-in-time signal parity
check, not a price-return backtest.

The read-only verifier now checks up to the latest 100 linked paper entries
this way and reports matched, mismatched, or unverifiable records separately.
The current journal contains three linked entries: the new one matches all
four frames and both saved latest-candle timestamps; two older entries are
unverifiable because their signal evidence is incomplete. The matching entry
uses the current configuration as a fallback because its threshold and
liquidity policy were not yet stored at entry. Future entries record those
two policy values in the decision evidence, and the verifier uses the saved
values. A regression test includes late backfilled 15m/1h bars and a tampered
saved score; it confirms both the time filter and mismatch detection.

## Retry transient worker-state write failures (2026-11-03)

After a later dashboard restart, the paper cycle refreshed all four open
positions at 07:35:44 UTC, but its persisted `last_started_at` still showed
07:29:53 UTC. The cycle was active; the state write had been silently skipped
by the worker's broad persistence-error handler. A regression now injects two
SQLite lock failures before a successful write and verifies the start is
persisted before the callback runs. The worker retries transient SQLite
operational errors twice and logs the final error type without logging the
payload or exception text. Exit management remains fail-safe if persistence
continues to fail. After another safe dashboard restart, all three workers
persisted new starts. The paper start was saved at 07:38:35 UTC while its
previous completion still showed 07:37:44 UTC. This verifies the live path
once after the retry change; future SQLite failures remain possible and are
now logged by type.

## Rotate OHLCV requests across every market-ready paper candidate (2026-11-03)

The latest saved decision window had 100 candidates: 92 rejects and eight
observations, with five reaching the candle gate. A separate read-only check
of current candidates found nine audited-safe mints passing the market-only
entry gate and carrying known pools. The OHLCV collector nevertheless
reported only four `active_market_ready_size` because `active_mints` was cut
to the first four before its oldest-fetch scheduling. That fixed slice could
leave other eligible candidates without a current 1m candle indefinitely.

A regression with six market-ready mints outside the fixed research cohort
failed before the change (`active_market_ready_size` was four), then passed
after three cycles when every ready mint received an OHLCV call. The
collector now keeps all market-ready mints in the bounded request rotation;
it still makes at most four public OHLCV calls per cycle and preserves the
score-independent research cohort allocation. This removes deterministic
starvation but does not guarantee fresh 1m history for all nine under the
public provider quota.
At 07:42:56 UTC, the restarted collector completed a cycle with
`active_market_ready_size=8`, four attempted and four successful OHLCV
requests, and no reported worker error. The eligible count had changed from
nine to eight as the live market inputs updated. This verifies the running
collector uses the expanded candidate rotation, without implying that all
eight already have enough timely candles for entry or replay.

The 07:45 UTC read-only verifier found 395 trusted first-observation 1m
membership bars, of which 139 were fetched within 120 seconds of close.
Nineteen of 20 cohort mints have some 1m history, but replay still has zero
eligible closed trades: three complete no-trade series, seven with insufficient
history, six with insufficient confirmation history, three with insufficient
candle availability, and one missing 1m history. The forward paper journal
has four open positions and one closed position; the closed position lacks
complete entry evidence and is excluded. The measured autonomous forward
sample remains zero complete exits, so return, win rate, profit factor, and
indicator value remain unmeasurable. The dashboard responds with HTTP 200,
all three workers report running without a last error, SQLite quick-check
passes, and live execution remains environment-disabled and code locked.

A subsequent read-only cadence check found 11 currently market-ready mints,
but only five had a latest 1m bar opened within the strategy's 180-second
staleness window. Their latest bar ages ranged from 120 to 360 seconds.
The 07:45:23–07:45:44 UTC collection cycle used four of four successful
public OHLCV calls, then scheduled its next cycle 30 seconds later. With
two active-candidate calls in the normal mixed active/research lane, current
quota and provider latency cannot keep every changing candidate current.
The safe entry gate rejects stale bars; the rotation fix prevents permanent
exclusion but does not solve this cadence limit. A faster source or a measured,
explicit focus policy is needed before interpreting missed entries as
indicator failures. No collection quota or freshness threshold was raised
without evidence that the public provider can sustain it.
CoinGecko's [GeckoTerminal Public API guidance](https://support.coingecko.com/articles/6867086195-does-geckoterminal-have-an-api)
states a 30-call-per-minute ceiling, but that is not a guaranteed per-client
throughput. This journal recorded an OHLCV backoff at 07:26:47 UTC with a
07:27:47 UTC expiry even at the existing four-call cycle limit. We therefore
did not increase the request budget based on the published ceiling alone.

## Test a bounded adaptive OHLCV request budget (2026-11-03)

The next paper scan found 11 market-ready candidates, but only five had a
latest 1m bar opened within the 180-second signal freshness window. Among
the latest 100 saved decisions, six passed market gates, none had both
required frames ready, and no autonomous entry followed. This is a data
collection constraint, not evidence that an indicator rejects good trades.

The collector now starts with the existing four-request budget. Only when
at least eight market-ready mints persist and three consecutive cycles fill
their budget without provider errors does it increase the next budget by
two, capped at eight. The pacer was later changed to 15 seconds per request;
the collector reserves research-cohort calls, restores four after a
429/cooldown, and returns to four if the ready backlog shrinks. A test
exercises the four-to-six ramp,
the eight-request ceiling and research share, 429 rollback, and no-backlog
reset. The full test suite, Ruff lint and format, strict mypy, wheel build,
and JavaScript syntax check all passed. This changes data collection only;
no trading threshold, position sizing, exit rule, or live-trading gate changed.
The initial restarted-collector measurement is recorded below.

The restarted collector completed three full four-request cycles and raised
the saved next budget to six at 07:55:41 UTC with nine ready mints. The
07:56:11–07:56:48 UTC six-request cycle then received a provider 429 on its
sixth request: five succeeded, and the saved next budget returned to four.
Immediately after this trial, only one of nine market-ready mints had a
latest 1m bar within the 180-second freshness window. This single cycle
does not prove that six requests always fail, but it does contradict a claim
of improved coverage from raising the budget. The policy now holds the
four-request level for one hour after a larger-budget 429 before another
expansion attempt. A regression checks that hold, the existing provider
cooldown, and the four-request fallback. The local collector state was set
to retry larger cycles no earlier than 08:56:48 UTC, one hour after the
observed 429. After a safe restart, the 07:58:57–07:59:47 UTC cycle made
four successful requests and saved the same hold deadline and a four-request
next budget. The dashboard returned HTTP 200, SQLite quick-check passed,
the paper and narrative workers were running, and both live-trading gates
remained closed.

The 08:00 UTC read-only verifier had 526 trusted 1m membership bars, 165
fetched within 120 seconds of close. One sampled replay path reached an
entry but its exit candle was unobserved, so it is reported as one censored
unclosed trade. There are still zero eligible closed replay trades and zero
complete closed autonomous forward paper trades. The latest decision window
had seven market-gate passes and one with both required frames ready. These
are coverage milestones, not performance evidence; net expectancy and
drawdown remain unmeasurable.

## Diagnose the first censored replay entry (2026-11-03)

The first replay entry in the prospective cohort was signaled at 07:19 UTC
and entered at the next 1m open, 07:21 UTC. The paper polling proxy's first
exit check was at 07:26 UTC. Although stored 1m bars exist for 07:20–07:26,
their first fetches were 1,780–2,140 seconds after the respective closes.
They therefore could not have provided a point-in-time mark at that exit
check. The `exit_candle_unobserved` censor is justified; later backfilled
bars must not be used to assign a favorable exit. This mint had 40 recorded
DEX price snapshots after entry, spanning $0.5457 to $0.5507; none by
itself establishes a Jupiter executable exit or an exit-rule trigger. The
replay still has zero realized closed trades, so no strategy return or
indicator conclusion is supported. Better contemporaneous exit evidence,
not a relaxed replay gate, is the next data requirement.

## Retain forward paper quote-mark evidence (2026-11-03)

Inspection found that successful fresh Jupiter paper quotes updated only the
latest mark on `paper_positions`; the previous marks were overwritten. This
prevented an independent review of the observed position path and any later
intratrade drawdown estimate. An additive `paper_position_marks` table now
appends each successful mark's time, remaining tokens, net SOL value,
original-size-normalized value, peak, TP stage, and modeled execution fields.
The mark and latest-position update share one SQLite transaction: a regression
injects an insert failure and verifies that neither value changes. A second
regression checks marks before and after a partial TP, including size and TP
stage. The forward report now distinguishes total marks from marks with
complete cost evidence. This is a prospective evidence improvement, not a
retroactive repair or a new profitability metric. Existing closed-trade
drawdown still excludes intratrade lows.

A consistent pre-migration SQLite backup named
`pre-paper-mark-history-20261103T080516Z.sqlite3` passed quick-check. The
full test suite, Ruff lint and format, strict mypy, wheel build, and
JavaScript syntax check passed. One full-suite run exposed a test-only
second-boundary race in research-cohort setup; its fresh timestamp is now
fixed to the test's `now`, and the full suite passed afterward. After a safe
dashboard restart, the 08:08:23–08:09:48 UTC paper cycle completed without
error and appended four costed Jupiter marks, one for each open position.
The forward verifier reports four total and four costed marks across four
open positions. SQLite quick-check passes, the dashboard responds with HTTP
200, and live execution remains disabled and code locked. The historical
mark path before this release remains unavailable; no complete autonomous
position has closed yet.

## Bound sampled-mark drawdown claims by recorded coverage (2026-11-03)

The first live mark records were complete, but all four open positions were
created before mark history began. The forward report now checks, for each
position, that costed Jupiter marks start within a bounded interval after
entry, stay within that interval of one another, and extend to the current
time or close. The bound is twice the configured paper polling interval plus
120 seconds for cycle/runtime jitter. It reports the number of open and
closed positions meeting that rule. Only evidence-complete closed trades
with such mark coverage receive `max_observed_mark_drawdown_pct`; the metric
uses the marked value of remaining tokens plus realized cash from partial
exits and the final sale. It is a lower bound on the observed position path
because moves between quotes remain unseen. The existing
closed-trade equity drawdown remains separate. Tests include a valid path,
missing entry/exit/intermediate marks, an uncosted mark, and two costed
closed trades with a known 20% sampled drawdown. Full checks and a live
read-only verification found 13 costed marks across five open positions, one
of which has bounded mark coverage from its entry. The older positions opened
before this mark history existed and cannot acquire retrospective coverage.
There are still zero complete closed autonomous forward trades; sampled
drawdown and net expectancy therefore remain unavailable. The dashboard
responded with HTTP 200, SQLite quick-check returned `ok`, and both live
execution gates remained closed. The full test suite, Ruff lint and format,
strict mypy, wheel build, and JavaScript syntax check passed.

## Preserve the paper worker's newest start record (2026-11-03)

The paper worker's `start()` launched its thread and then persisted a startup
status separately. That write could overtake the cycle's newer start write,
leaving the durable health record behind the actual quote marks. Startup now
relies on `run_once()` to persist the first in-progress state before network
work. A regression forces the former stale-write ordering while a cycle is
blocked and checks that the newest start remains recorded. After a safe
dashboard reload, the persisted paper `last_started_at` advanced to the new
cycle at 08:19:44 UTC before its completion; the market and narrative workers
also started, and the dashboard returned HTTP 200. This is a status accuracy
fix, not a change to entry, exit, or live-trading behavior. The first reloaded
paper cycle completed at 08:21:17 UTC with no worker error, and SQLite
quick-check remained `ok`.

The Paper Portfolio's collapsed performance details now show costed mark
count, bounded open-path coverage, and the sampled drawdown lower bound when
an eligible covered trade closes. It labels the metric unavailable while
there are no such exits, avoiding a misleading zero. JavaScript syntax
validation passed; the running dashboard returned the new HTML elements and
matching versioned JavaScript asset over HTTP.

## Correct partial-exit drawdown accounting (2026-11-03)

Review of the forward metric found that its first implementation used marks
scaled from the remaining tokens back to the original position size. That is
a price-path proxy, and after a partial take-profit it omits realized cash.
The metric now values each observed position as realized sale proceeds plus
the net Jupiter mark of the remaining tokens, then includes the final sale.
The mark's recorded remaining token amount identifies which sales preceded
it, even when Windows gives a mark and sale the same timestamp. Tests cover
a partial exit with 10% actual sampled drawdown versus the prior 20% price
proxy, and a deterministic tied-timestamp case. The full Python suite,
Ruff lint and format, strict mypy, and wheel/sdist build passed. Live
execution remains locked.

The same review found that a partial sale saved a size-derived estimate as
if it were a newly quoted mark. The journal now clears `last_quoted_at` for
an open remainder until Jupiter supplies a fresh quote. A regression verifies
that the position is stale and the open-position mark breaker blocks new
entries in that interval. The full test suite, Ruff lint and format, strict
mypy, and JavaScript syntax check passed after this safety fix. The dashboard
was reloaded after all three workers were idle. Its new worker instances
persisted their cycle starts at 08:33:30 UTC, the dashboard returned HTTP
200 with the updated performance labels, and SQLite quick-check returned
`ok`. The forward report has 23 costed marks across five open positions,
one with bounded path coverage. It still has zero complete closed autonomous
trades and no sampled drawdown result. `LIVE_TRADING_ENABLED=false` and the
independent execution lock are unchanged.

## Separate research capture from strategy parity (2026-11-03)

The read-only verifier now splits timely 1m capture for the *current* fixed
research cohort, keeping older cohorts unclassified rather than assigning
them today's lane. At 08:40 UTC, 189 of 239 focus bars and 46 of 507
secondary bars were first fetched with trusted values within 120 seconds of
close. One focus replay position remained open at the end of its series; one
secondary replay entry had an unobserved exit candle. The focused lane has
better timely capture in this sample, but neither lane has a closed eligible
replay trade.

Inspection of the secondary mint's saved live decisions found repeated
`reject` verdicts for an extreme reported 24-hour price move (about 1.4
million percent). The candle replay can signal an entry there because it
does not reconstruct the historical DEX market gate or the paper worker's
scan cadence. Both missing parity checks are now explicit false fields in
replay output and walk-forward promotion gates. We did not redirect scarce
OHLCV quota toward this candle-only signal: it is not evidence of a trade
the live paper strategy would have taken. A targeted lane-diagnostic test,
the existing replay/evaluation tests, Ruff lint and format, and strict mypy
passed. Historical strategy performance remains unmeasured.
The same parity flags and a false promotion status are now returned even when
walk-forward evaluation exits early for insufficient history; this prevents
an incomplete result from appearing to omit its known limitations.
The full test suite, Ruff lint and format, strict mypy, JavaScript syntax,
and wheel/sdist build passed. After all workers reached an idle interval, the
dashboard was reloaded. Its paper, market, and narrative workers recorded new
starts, HTTP returned 200, SQLite quick-check returned `ok`, and the report
counted 33 costed marks across five open paper positions. There are still no
complete closed autonomous exits; the environment live flag is false and the
independent code lock remains true.

## Preserve and verify paper market-gate decisions (2026-11-03)

The autonomous paper strategy now records a versioned market-policy snapshot,
the exact market and contract inputs used by its scorer, the narrative fields
that can change its score or hard gate, its pre-candle verdict, score, and gate
reasons. The read-only verifier recomputes that market decision using the
recorded policy and evaluation time. It detects changed scores or narrative
risk inputs and labels older entries without these fields `unverifiable`.
This verifies consistency of saved decisions; it does not independently prove
the external DEX values or provide historical worker-schedule parity.

After the dashboard was restarted between worker cycles at 08:52 UTC, the
paper worker completed a new cycle at 08:53:33 UTC. All 100 most recent saved
decisions contained the new evidence and recomputed to the recorded market
verdict and score: 100 matched, zero mismatched. They included 89 rejections
and 11 observations, with no new paper entry. All four earlier linked paper
entries remain market-gate-unverifiable because their policy was never saved;
two match candle replay and two lack enough historical candle evidence. The
forward report still has five open positions and no complete closed autonomous
trades, so realized strategy performance remains unmeasured.

The full Python test suite, Ruff lint and format checks, strict mypy, and
wheel/sdist build passed. The pinned dependency audit reported no known
vulnerabilities. Dashboard HTTP returned 200, SQLite remained readable, the
environment live flag is false, and the independent execution lock is true.

The five open positions were also checked against the configured exit
thresholds. Their sampled net value ratios were 0.805 to 1.031, above the
0.65 stop threshold and below the 2.0 first take-profit target. None had
reached the 24-hour maximum hold; the oldest was about 17 hours old. Every
position had a quote no more than 158 seconds old at inspection. The absence
of completed exits is therefore consistent with the current rules and observed
marks, rather than evidence that the exit monitor has stopped. This is a
point-in-time check, not a prediction of future exits.

## Preserve actual paper worker cycle order (2026-11-03)

Future autonomous decisions now store one shared cycle ID, cycle start time,
rank after scoring, candidate count, and initial portfolio breaker reasons.
This allows a decision-aligned replay to distinguish one worker scan from
another and preserve the rule that only one position may open in a cycle. The
read-only verifier labels bounded windows as complete, partial, or inconsistent
and rejects duplicate ranks or multiple paper openings. Legacy rows without a
cycle ID remain unassigned rather than being grouped by guessed timestamps.

The dashboard was restarted between completed collector cycles and returned
HTTP 200. Its first new paper cycle ran from 09:00:53 to 09:02:06 UTC. The
latest 100 saved decisions form one complete cycle with ranks 1 through 100,
zero openings, and 100 of 100 market-gate matches recomputed from saved inputs.
The cycle recorded 89 rejections and 11 observations. The full Python suite,
Ruff lint and format, strict mypy, and wheel/sdist build passed. The live flag
is false and the independent execution lock is true. These records establish
cycle provenance for future research; they do not yet supply a closed,
decision-aligned historical performance sample.

## Verify rejected candle decisions at their decision time (2026-11-03)

The candle verifier previously assumed every checked decision was a paper
entry. That correctly tested linked openings but mislabeled legitimate
`observe` decisions as mismatches when their candle gate rejected entry. It
now checks the saved final verdict against the recomputed multi-frame gate,
while still comparing every saved signal and latest closed-bar reference with
authentic candles known at the decision time.

In the latest complete 100-decision worker cycle, 11 candidates passed the
market score. Five stopped because their on-chain audit was older than ten
minutes; six reached candle confirmation. All six saved candle signals and
their `observe` verdicts replayed exactly. The remaining 94 decisions were
not counted as candle checks. This verifies this cycle's gate behavior, not
whether rejected candidates would later have been profitable. Autonomous
forward performance still has no complete closed trades.

## Censor post-decision price opportunity rather than infer profit (2026-11-03)

A read-only follow-up study now anchors each outcome to the first 1m candle
opening strictly after the saved decision time. The 5m and 15m cohorts are
selected separately from the latest 100 decisions old enough for each
horizon, including a further three-minute allowance for the next bar and
capture. It requires continuous authentic GeckoTerminal 1m bars with first
value observation and fetch no later than 120 seconds after each close. It
reports future high, low, and horizon close relative to that later open only
when every required bar passes. It never assigns a fill, trading cost, or
profit to a rejected token. Missing and late data stay in the denominator.

At 09:09 UTC, neither cohort had an observed path. The 5m cohort had 100
distinct mints: 90 lacked the next bar, nine had late or untrusted capture,
and one had a future-bar gap. The 15m cohort also had 100 distinct mints: 91
lacked the next bar, three had a future-bar gap, and six had late or untrusted
capture. Thus the current database cannot show which rejected decisions would
have had favorable short-term price movement, let alone executable net profit.
The result points to observation coverage as the limiting evidence, not a
measured advantage or defect in any entry filter.

### Separate ex-post backfills from executable observations

The initial 120-second capture cutoff above was too strict for this
*price-only* follow-up. It is appropriate when modeling a live decision or
prompt exit, but authentic future bars fetched later still reveal subsequent
price movement. The study now requires trusted first-value provenance and a
continuous future series, then labels a path `observed_timely` only when every
bar arrived within 120 seconds of close; otherwise it is
`observed_backfilled`. Both remain non-executable price observations.

At 09:14 UTC, the latest age-qualified 5m cohort had six backfilled paths,
88 missing next bars, five future gaps, and one missing pool. The 15m cohort
had five backfilled paths, 86 missing next bars, and nine gaps. No timely path
was available. The six observed 5m decisions had been held for three stale
audits, two missing/stale candle histories, and one unaligned candle signal.
Their maximum favorable movement ranged from 0.35% to 13.43%, but a later
intrabar high is not an executable sale and the sample is tiny. These data do
not establish that any safety gate rejected a net-profitable trade. Capturing
continuous 1m data for more decision cohorts remains necessary.

## Prospective market-eligible follow-up lane (2026-11-03)

The latest decision follow-up remained too sparse to judge entry filters: most
saved decisions lacked a next authentic one-minute bar. The collector now fixes
one audited candidate that passes the market gate on the latest saved values
for 30 minutes using a deterministic
hash of the current eligible population. Selection is recorded before future
prices in the additive `decision_followup_memberships` table with its time,
expiry, population count, and method. A changed verdict does not replace the
subject mid-window. The collector reserves its first one-minute OHLCV request
for that mint when its pool is available and the provider is not cooling down;
other calls still cover rotating active candidates and the score-independent
research cohort. This deliberately selected follow-up is kept separate from
the unbiased historical replay sample.

The first local selection at 09:21:39 UTC recorded one mint from ten market-ready
ones. A subsequent completed collector cycle stored authentic one-minute bars
for the selected pool. At 09:24:00 UTC, the persisted worker summary showed
follow-up due/attempted/succeeded = true/1/1 within four attempted and four
successful OHLCV calls. This confirms a real provider response and database
write, not continuous coverage or a complete trade path. Zero-trade intervals,
stale pools, and the public rate limit can still leave gaps; follow-up outcomes
remain price-only observations. No strategy return, missed-profit claim, or
production promotion follows from this initial cycle. The schema change is
additive; the existing scheduled SQLite backups remain the rollback source,
and restoring an older backup would discard later follow-up selections.

At the next read-only check, three contiguous one-minute bars began at or after
the selection minute, ending at 09:23 UTC. That is below either five- or
fifteen-minute outcome horizon; continued request success does not fill minutes
without trades.

The full Python suite, Ruff lint/format, strict mypy, wheel/sdist build, and
pinned dependency audit passed after the change. SQLite quick check was `ok`,
the dashboard returned HTTP 200, and the environment flag plus independent
execution lock remained false/true. The existing one closed paper position
lacks a complete linked entry decision and remains excluded; the autonomous
forward sample still has zero complete closed trades.

### Read-only follow-up outcome verification

The verifier now resolves each selected pool from a market snapshot captured
no later than the recorded selection time and at most 120 seconds earlier. A
later pool change cannot be used for that selection. It requires a complete
authentic future 1m path before reporting 5m or 15m price movement; pending,
missing, stale and gapped paths remain in the denominator. A regression test
changes the pool after selection and confirms the original pool is used.

At 09:29 UTC the first selection's pool snapshot was captured at the selection
second. Its 5m future path was `observed_timely` with 88 seconds maximum
capture delay, +14.76% maximum favorable movement, -8.97% maximum adverse
movement, and +12.42% horizon close movement from the next 1m open. The 15m
path was still pending. These are price observations for one selected token,
without a modeled fill, trading costs, safety replay, or a completed trade.
They cannot establish net profitability or justify relaxing an entry gate.
The full test suite, Ruff, strict mypy, and build passed for this read-only
reporting change.

### Follow-up duration for actual 5m confirmation

At 09:30 UTC, the first follow-up had nine consecutive authentic 1m bars and
no worker errors. Its fixed 30-minute selection would end before the 40 fresh
minutes needed to form eight consecutive 5m bars, so it could not reliably
exercise the full entry gate within its own observation window. New selections
will last 60 minutes and use a distinct v2 selection method. The existing v1
selection still expires at its originally recorded 30-minute boundary; the
selector now uses that persisted expiry rather than retroactively extending it.
The read-only outcome report separates counts by selection method. This is a
prospective coverage adjustment, not an optimization based on a trade return.
It does not change paper entry or live execution rules.

The local dashboard restart at 09:31:43 UTC overlapped a collector start and
left that cycle without a completion marker. The next dashboard process
recovered: collector cycle 719 completed at 09:32:24 UTC without a worker error,
and its follow-up call succeeded. This shows the interrupted collection did
not stop the worker; it is not evidence that an interrupted paper trade would
be safe. The paper worker had completed its prior cycle before the restart.
The full Python suite, Ruff lint/format, strict mypy, and wheel/sdist build
passed after the 60-minute window change. The live flag remained false and
the independent execution lock remained true.

### Strategy Lab follow-up evidence panel

The authenticated Strategy Lab summary now returns the most recent prospectively
selected follow-up with its fixed window and 5m/15m outcome statuses. The page
shows these in a compact strip linked to Paper Portfolio. It labels a complete
price path as observed rather than a profit or fill, and explicitly shows
pending, missing, gapped, stale-pool, or untrusted evidence. This reduces the
chance that an attractive intrabar high is mistaken for a costed result.

An API regression test verifies the summary is inaccessible before pairing and
returns the recorded selection with both horizons pending. The full Python
suite, Ruff lint/format, strict mypy, JavaScript syntax check, and wheel/sdist
build passed. No strategy threshold or execution rule changed. The autonomous
forward sample still needs complete closed trades before any performance claim.

After the restart, the first selection's 15m path became
`observed_backfilled`: the maximum bar capture delay was 160 seconds, the
maximum favorable move was 94.32%, the maximum adverse move was -8.97%, and
the 15m close was +68.41% from the next 1m open. This is one price-only
observation, not a costed entry, exit, or evidence of profitability. The
dashboard returned HTTP 200 with the new panel, SQLite quick check was `ok`,
the paper worker completed its post-restart cycle without error, and the
environment live flag / code lock remained false / true.

### Per-position paper exit isolation

Code review found that the initial `evaluate_exit_signal` call sat outside the
per-position exception handler. A malformed open position could therefore stop
the entire autonomous paper cycle before other positions were checked, leaving
valid stops or take-profit exits unevaluated. The worker now records a typed
`paper_exit_evaluation_failed` event for that position, continues evaluating
other positions, and blocks new entries with `exit_evaluation_failed`. An
execution failure remains a distinct `exit_execution_failed` breaker. The
operations health endpoint now reports degraded status when the paper worker's
latest cycle contains any exit failure as well as an unavailable exit quote.

A regression test presents one invalid marked position and one valid stop-loss
position: the valid exit still runs, the invalid one is logged, and entry stays
blocked. A second test checks the degraded operations state. The full Python
suite, Ruff lint/format, and strict mypy passed. This change affects only
paper exit resilience; live execution remains locked.

The running dashboard was restarted between completed worker cycles. Its first
post-restart paper cycle completed at 09:48:45 UTC with zero exit or quote
failures and entry paused by the existing five-position limit. The market
collector then completed at 09:49:18 UTC with four successful authentic OHLCV
requests, including the fixed follow-up request, after a temporary public
provider cooldown. HTTP returned 200, SQLite quick check passed, and the live
flag / independent code lock remained false / true. These healthy cycles do
not exercise the new malformed-position branch in production; that branch is
covered by the isolated regression test.

At the recorded v1 expiry (09:51:39 UTC), the collector selected a new v2
market-eligible token at 09:51:43 UTC, fixed its expiry at 10:51:43 UTC, and
kept the earlier v1 record unchanged. The 09:52:09 UTC collector cycle made
four successful authentic OHLCV requests, including the new fixed follow-up.
Both v2 price horizons correctly remain `pending_horizon`. This verifies the
60-minute rotation and collection path against the running database; it does
not establish future continuity, strategy returns, or a costed paper trade.

### Paper entry liquidity evidence

The paper execution model previously skipped its trade-to-liquidity size check
when a Jupiter route lacked a USD value or the discovery candidate lacked a
liquidity value. A paper buy now requires a positive route USD value and a
positive, pool-matched market snapshot captured within the past 180 seconds
(allowing at most 30 seconds of clock skew). Without either value the buy is
rejected, so the position cannot enter the performance sample on an unchecked
size assumption. Paper sells and open-position marks remain available when
USD metadata is missing; their execution records show the unverified
`trade_liquidity_bps` as null rather than claiming a checked ratio.

Unit tests cover missing/zero route USD value and missing liquidity. An
integration test ages a saved market snapshot, verifies that no paper position
or fill is written, then restores a fresh snapshot and completes the normal
paper open/mark/close flow. The two newest open paper positions had route USD
value and liquidity evidence in their saved entry records; three older legacy
positions did not. This change does not retroactively validate those entries.

The full Python suite, Ruff lint/format, strict mypy, and wheel/sdist build
passed. After a between-cycle dashboard restart, HTTP returned 200, SQLite
quick check passed, and the first paper worker cycle completed at 09:58:25
UTC with zero quote/exit failures and entry still paused by the five-position
limit. The live environment flag stayed false and the independent execution
lock stayed true. Existing positions were not modified by the entry rule.

At 09:59 UTC the new v2 follow-up's 5m horizon matured, but its required first
one-minute pool bar was absent. The outcome is correctly
`entry_bar_missing`, not an inferred flat return or a simulated trade. The
15m horizon was still pending. This single selected token had no observed
authentic 1m bar after selection at the time of inspection; the latest saved
pool bar preceded selection. Continued collection can document later bars,
but cannot repair that missing first bar without falsely reconstructing it.

### Four-timeframe coverage and replay readiness (10:01 UTC)

A read-only verifier pass counted 18,086 saved authentic 1m bars across 119
pool series, 3,935 5m bars across 41 (3,673 direct and 262 complete
1m-derived), 6,884 15m bars across 65, and 4,727 1h bars across 54. All
available opens in these series aligned to their declared
timeframe. Gaps remain common: 59 of the 1m series, 22 of the 5m series, 16
of the 15m series, and six of the 1h series had at least one missing interval.
Alignment alone does not prove uninterrupted usable history.

Where direct provider 5m bars overlapped complete 1m-derived bars, 55 of 672
compared closes differed by over 1%; the largest difference was 35.26%.
The existing disagreement gate must therefore remain active, and direct and
derived frames cannot be silently substituted. Within the recorded research
membership windows, 1,062 authentic 1m bars had known first-observation
provenance, but only 322 were fetched within 120 seconds of close. The current
replay still had zero closed eligible trades. These counts explain why a large
historical candle store does not yet provide a valid strategy-performance
sample or a basis for loosening entry rules.

### Market-data outage visibility (10:05 UTC)

The operations health endpoint previously reported a running market collector
as healthy even when its last cycle recorded an OHLCV provider rate limit or
failed DEX batches. A live read at 10:02 UTC showed exactly this case: the
collector had tracked 60 tokens and completed both DEX requests, but its
`rate_limited` flag was true and it had attempted no fresh OHLCV requests.
The health classifier now reports the collector as degraded for that condition,
for incomplete DEX request batches, or when all attempted OHLCV requests fail
with provider errors. It exposes specific reason codes and the overview tells
the operator when recent candle collection is rate limited or discovery is
incomplete. A partial OHLCV miss without provider errors is not treated as a
global outage because individual pools can have no new bars.

Focused tests cover each failure state and a complete healthy collection. The
full Python suite, Ruff lint/format, strict mypy, and wheel/sdist build passed.
This is monitoring honesty, not a new trading signal: missing or stale bar
guards still decide entry eligibility. No strategy-performance claim follows
from these checks.

The dashboard was restarted between worker cycles to load this change. The
served page returned HTTP 200 and the served JavaScript contained the new
rate-limit message. The first post-restart paper cycle completed at 10:06:59
UTC with zero quote and exit failures; entry remained paused by the existing
five-position limit. The market collector completed at 10:07:12 UTC with both
DEX batches and all four attempted OHLCV requests successful. SQLite quick
check passed, and the live environment flag / independent execution lock
remained false / true. A healthy provider cycle does not exercise the new
degraded branch in production; the focused tests cover that branch.

### Point-in-time market gate inputs (10:14 UTC)

The current multi-token replay spans 40 pool series but has no closed eligible
trade. Its statuses are 16 insufficient history, nine insufficient confirmation
history, five missing 1m history, four no eligible trades, three censored open
trades, and three insufficient candle availability. Across 660 entry-window
checks, the most frequent first rejection was stale 1m data (355), followed by
unconfirmed 1m signals (132), still-collecting 1m data (96), and 1m gaps (49).
Every series explicitly reports `historical_market_gate_parity=false`; these
are diagnostic candle replays, not a valid same-policy historical comparison.

Inspection found that saved market snapshots lacked two inputs the baseline
market gate uses: 24h price change and pair creation time. New snapshots now
retain both values from the observed DEX pair. The schema change adds nullable
columns, so historical values remain unknown instead of being inferred from
current candidate state. Tests cover discovery ingestion, DEX refresh, and
migration of an old snapshot table. A verified SQLite backup was taken before
the dashboard restart and migration.

After migration, the database had 17,635 market snapshots; 38 newly captured
rows contained both added inputs and 17,597 rows still had neither. The row
count exceeded the 17,593 rows in the pre-migration backup, SQLite quick check
passed, the dashboard returned HTTP 200, and the live flag / code lock stayed
false / true. These new fields improve future evidence capture but do not by
themselves establish market-gate parity: historical audit, narrative, and
paper-worker timing must also be reconstructed without look-ahead.

The second fixed prospective follow-up's 5m and 15m horizons both matured as
`entry_bar_missing`; the selected pool had no authentic first post-selection
minute. This remains in the observed sample denominator. It cannot be assigned
a synthetic flat return or used to tune entry thresholds.

The restarted runtime then exercised both health states: a collector cycle
under the public OHLCV cooldown was reported as degraded with reason
`ohlcv_rate_limited`; the next completed cycle at 10:14:33 UTC succeeded on
both DEX batches and all four OHLCV attempts. The post-migration paper cycle
completed at 10:14:44 UTC with zero quote and exit failures and entry still
paused by the five-position limit. The verifier ran against the migrated
schema, still found zero closed autonomous trades, and kept promotion readiness
false. The full Python suite, Ruff lint/format, strict mypy, and wheel/sdist
build passed after the change.

### Saved decision reproducibility (10:19 UTC)

All 15,634 stored market-gate decisions were recomputed once from their saved
policy and input values. Of those, 1,900 matched exactly and none mismatched;
13,734 were unverifiable because the policy-version record did not yet exist
when they were made. All four historical autonomous paper entries are in that
older group. The normal verifier now performs a bounded, explicit audit of the
latest 5,000 saved decisions and reports match, mismatch, and unverifiable
counts separately. In that latest window, 1,900 matched and 3,100 were older
unverifiable records. Its `historical_market_data_parity` flag stays false:
recomputing a saved decision does not prove that the original market or audit
input was fresh or independently reconstruct the paper worker's schedule.

Among the 1,900 versioned decisions, 1,720 market gates rejected their tokens,
172 market gates were eligible but the final candle decision was observe, seven
were observe at both gates, and one fully eligible decision was stopped by the
five-position cap. The open positions were 2.0 to 18.5 hours old when checked,
inside the configured 24-hour maximum hold. There is no justification yet to
change the cap or optimize the candle threshold. At 10:19 UTC, 241 of 17,824
saved market snapshots had both newly retained market-gate inputs; older rows
remain unknown. The full tests, Ruff lint/format, strict mypy, and package
build passed. The dashboard still returned HTTP 200 and live execution stayed
locked.

### Re-audit scheduling for entry readiness (10:26 UTC)

Of 173 versioned decisions whose market gate was eligible, 73 stopped because
the on-chain audit was older than ten minutes and 84 stopped because authentic
1m/5m candles were missing, stale, or incomplete. This identified an audit
scheduling constraint, not evidence that either gate should be relaxed. Before
the scheduling change, eight current candidates cleared the market-only gate,
but only three had audits within the required ten minutes; their median audit
age was 975 seconds.

The paper scanner still reserves the full new-discovery and unbiased research
cohort allocations. It now uses up to eight spare watchlist slots (previously
three), prioritizing safe candidates that clear the current market gate and
have a fresh snapshot for the same pool. Other safe candidates remain ordered
by oldest audit. The callback only changes the order of actual RPC re-audits;
it cannot turn a cached audit into a fresh pass. A failed re-audit leaves the
old result stale, and the independent market, candle, and portfolio gates still
apply. Worker summaries record each audit lane's count.

Focused tests covered the eight-slot cap, priority order, and refusal to
prioritize a stale pool snapshot. The full Python suite, Ruff lint/format,
strict mypy, and wheel/sdist build passed. The first live paper worker cycle
with the new scheduler completed in 80 seconds: 20 new discoveries, ten
research-cohort re-audits, and eight watchlist re-audits, seven of them
priority candidates. All eight current market-eligible candidates then had
audits within ten minutes; their median audit age was 42 seconds. The cycle
had zero quote or exit failures, kept entries paused by the five-position cap,
and left live execution locked. This one-cycle freshness improvement is an
operational measurement, not a measured trading edge. Authentic candle
availability and closed, costed forward trades remain the performance gates.

### Current entry data and OHLCV quota verification (10:32 UTC)

The current top-100 discovery set had eight market-gate-eligible candidates and
eight fresh on-chain audits. The new `current_entry_readiness` report uses the
same confirmation function as the paper decision loop. At the measured instant,
zero of the eight had a full entry signal: five were limited by missing, stale,
or incomplete 1m/5m bars, and three had usable required bars but failed signal
alignment. The 1m frame was ready for four and stale for four; the 5m frame
was ready for five, collecting for two, and gapped for one. These counts can
change each collector cycle and are shown compactly in Strategy Lab. They do
not establish which rejected tokens would have been profitable.

The adaptive public OHLCV budget previously treated an HTTP-successful empty
or stale page as a successful capacity cycle. It now resets the growth streak
unless every attempted request returns current, nonempty bars. Focused tests
cover both empty and stale responses, alongside the existing quota-backoff
case. This change does not loosen the entry gate or increase the current
four-call request pace. The independent research cohort remains intact.

The read-only report still shows zero complete, costed autonomous paper exits
and `promotion_ready=false`; real-money submission is locked. The freshness
funnel is an operational measure, not historical or forward profitability.
After the dashboard restart, a short provider cooldown completed and the next
observed market cycle made four OHLCV requests with four responses, no empty or
stale pages, and no rate limit. The quota remained at four calls, as intended
while its higher-budget cooldown is active. The local dashboard served HTTP
200, its updated JavaScript served HTTP 200, and the unauthenticated Strategy
Lab API returned HTTP 401. SQLite `quick_check` returned `ok`.
The full Python test suite, Ruff lint/format, JavaScript syntax check, source
mypy, package build, and installed-dependency audit passed. Full-project mypy
also checks tests and currently reports 42 typing errors across five test
files; the new readiness test introduces none of them. That test typing debt
remains separate from the passing source typecheck and runtime tests.

### Prospective research candle allocation (10:41 UTC)

The collector's one-minute research calls were spread across eight fixed
focus mints in each 20-mint cohort. In the current cohort, 68 of 132 focus
bars and only three of 124 secondary bars were first captured within two
minutes of closing. Across 40 prospectively sampled mints, the replay still
has no complete closed trade; 17 have insufficient history, eight lack enough
confirmation history, six lack timely candle availability, three have
unobserved exits, one has no 1m history, and five have no eligible trade.
These are data-coverage counts, not strategy returns.

The next new three-hour cohort will concentrate the normal-cycle research
call on its first two hash-selected mints. The current cohort retains its
legacy eight-mint allocation until expiry, avoiding a mid-cohort change.
All 20 selections remain in the replay denominator, including failed and
inactive pools; no safety, candle, or paper entry gate was relaxed. The report
and Strategy Lab expose the active focus size. Research lane bar counts now
filter to each mint's current recorded selection window, so a mint selected
again in a later cohort cannot carry earlier bars into the current lane.
Replay statuses for those mints still cover all recorded windows and are
marked as such. Tests verify legacy cohort continuity, next-cohort rotation,
the two-mint collector allocation, and window filtering. Whether the smaller
focus improves usable coverage or paper performance remains unmeasured until
the next cohort has run.
The full Python suite passed after a dashboard connection-scope regression was
found and fixed. Ruff lint/format, source mypy, JavaScript syntax, and the
sdist/wheel build passed. Full-project mypy still reports 41 test-file typing
errors; source typing passes. The restarted local dashboard served HTTP 200,
the updated asset served HTTP 200, unauthenticated Strategy Lab returned HTTP
401, and SQLite `quick_check` returned `ok`. Both the market and paper workers
were running. The current cohort expires at 12:36:45 UTC on 3 November; its
reported focus remains eight until then. Live execution remains locked.
The first completed paper cycle after restart scanned 38 tokens, had 25 safe
audits, evaluated 100 candidates, and logged no quote or exit failures. Its
five-position cap still paused new entries. No complete, costed autonomous
exit had appeared by that check.

### Five-minute OHLC consistency gate (10:46 UTC)

The stored direct 5m bars and five-complete-1m aggregates overlap on 672
intervals. The report found 55 intervals with more than 1% disagreement in
at least one OHLC field; only 29 exceeded that threshold at the close. The
existing entry gate checked the close alone, so 26 high/open/low conflicts
could pass that particular data check. It now records both the close-only
and maximum OHLC differences and blocks a recent 5m confirmation when any
OHLC field differs by more than 1%. The overlap diagnostic and entry gate
therefore use the same definition of conflicting price evidence. Unit tests
cover a high-price disagreement with an identical close. The saved replay
compatibility version advances to v13 because this can change entry decisions.
The conflict timestamp is the latest first-observation time across the direct
and derived bars. If legacy rows lack that provenance, the latest persisted
fetch time is a conservative upper bound on when both values were known.
Replay does not apply the conflict before that timestamp, preventing a later
backfill from retroactively rejecting an earlier decision. All 55 current
conflicts rely on the legacy fetch-time fallback, so they are unsuitable for
establishing historical entry performance on their own.
These are data-consistency findings; they do not show whether a rejected
trade would have made or lost money. Live execution remains locked.

The restarted dashboard served the home page and auth status with HTTP 200;
the status and Strategy Lab APIs returned HTTP 401 without a paired session.
SQLite `quick_check` returned `ok`. There were five open paper positions and
one closed position, but the latter is excluded from measured performance for
missing or ambiguous entry-decision linkage. Autonomous complete, costed trades
remain zero, so profit factor and expectancy are unavailable. The latest read-only
report checked ten recent candle decisions: nine matched replay and one was
unverifiable because historical 1h candle provenance was incomplete. The
verifier now distinguishes this case from a genuine signal mismatch. Current
entry readiness was zero eligible among the latest 100 candidates; nine of ten
candle-evaluated candidates lacked complete timely candle evidence and one was
signal-misaligned. The environment live flag is false and the source hard lock
is true.

The fresh v13 read-only momentum replay covered all 40 recorded v2 research
mints under the current paper cost and exit assumptions. It produced zero
completed trades and three censored positions whose exit candles were not
observed. Statuses were 17 `insufficient_history`, eight
`insufficient_confirmation_history`, six `insufficient_candle_availability`,
five `no_eligible_trades`, three `censored_unclosed`, and one
`missing_1m_history`. Of 1,276 trusted 1m bars inside sample memberships,
380 were fetched within two minutes of their close. The original candlestick
strategy lacks comparable point-in-time data; the early-launch variant is not
implemented. No win rate, profit factor, net expectancy, or strategy winner can
be inferred from this sample.

The current replay cost assumption is 2,209 basis points for a 0.01 SOL
position. Its components are 60 bps modeled round-trip slippage, 110 bps
network and priority fees relative to that small position, and about 2,039 bps
of ATA rent treated as an unrecovered cash outlay. This is a conservative
capital accounting choice, and treating recoverable rent as a permanent fee
would overstate economic trading costs if an empty token account is later
closed. Do not tune the strategy against this zero-completed-trade sample.

### Bounded historical outcome retrieval (11:06 UTC)

One censored older-cohort token stopped receiving 1m candles before the end of
its saved membership window. The public pool-OHLCV client now accepts a bounded
[`before_timestamp` cursor](https://coingecko-api-v3.readme.io/v3.0.1/reference/pool-ohlcv-contract-address),
and the `research-backfill` command restricts requests to a saved mint/pool,
its selected research and exit horizon, at most 24 hours per call, and at most
five provider requests. The dashboard was briefly stopped so the maintenance
request did not compete with its public-rate collector. One live request
returned 101 new authentic 1m pool bars. All 101 were stamped with a first
fetch time after the requested historical interval; their past timestamps
cannot qualify earlier entry decisions. The dashboard was restarted, returned
HTTP 200 for its public shell and auth status, and returned HTTP 401 for private
APIs without pairing. SQLite `quick_check` returned `ok`; paper autostart and
market collection remained enabled; live trading remained false and hard locked.

After backfill, recent candle-decision parity stayed nine matched and one
unverifiable, and paper-entry parity stayed two matched and two unverifiable.
The strict paper-polling replay still has zero completed trades and three
censored positions. Later OHLCV can support retrospective price-path research,
but it cannot substitute for a quote that the paper worker did not observe at
the time. It would be invalid to relabel these censored positions as costed
forward-paper wins or losses.

An additional read-only retrospective replay used the newly retrieved bars
with a resting-order intrabar OHLC exit hypothesis. It still produced **zero
completed trades and one censored position**: an exit candle was unobserved.
That run is a data-coverage diagnostic, not comparable trading performance.

### Cash P&L versus recoverable token-account rent (11:13 UTC)

The forward paper report now keeps its primary closed-trade P&L on a cash basis:
entry includes reserved associated token-account rent, and no refund is booked
unless one is actually modeled. A separate field sums the recorded entry rent
and shows what net P&L would be **if every eligible token account were later
closed and all that rent returned**. This is a hypothetical sensitivity, not a
realized return, and it never changes trade decisions, circuit breakers, or the
primary cash metrics. The dashboard's execution-cost label now identifies rent
as a cash outlay until reclaimed. A regression test verifies both figures for a
costed closed trade; the live journal still has zero complete autonomous closed
trades, so neither figure establishes a strategy result.
The full Python suite passed (225 tests), as did Ruff lint/format, source mypy,
JavaScript syntax, and the sdist/wheel build. After restart, the local dashboard
served its shell and auth status with HTTP 200, rejected unpaired status and
paper-portfolio requests with HTTP 401, and SQLite `quick_check` returned `ok`.
Paper automation autostart is enabled; `LIVE_TRADING_ENABLED` is false and the
source live-execution lock remains in place. Five paper positions are open;
three lack complete historical entry evidence and will remain excluded from
the performance sample when they close. The two newer positions will count
only if their close and cost records reconcile.

At the final read-only check, the current research focus had 169 saved 1m
candles inside its selected windows, 92 fetched within two minutes of close.
The twelve secondary mints also had 169 selected-window 1m candles, but only
eight fetched that promptly. Across all 40 recorded research mints, strict
replay still had zero completed trades and three censored positions. This is
insufficient for in-sample tuning or out-of-sample comparison. The running
collector can continue improving coverage; late backfills remain excluded from
historical entry evidence.

### Current-cohort capture visibility (11:20 UTC)

Strategy Lab now displays the number of trusted 1m bars first fetched within
two minutes of close alongside all elapsed, closed minute slots in the current
cohort. It separates focus from secondary mints and restricts bars to each
mint's current recorded pool and actual selection window. This avoids
mistaking a raw candle total or a late backfill for timely replay evidence.
At the implementation check, focus had 92 timely bars from 169 observed across
816 elapsed slots; secondary had 10 from 175 across 1,224 slots. Missing slots
can mean no pool trades as well as provider gaps. These are data-coverage
figures, not strategy returns or simulated fills. A test covers the selected
pool, elapsed slots, and late bars; the paired summary route exposes the data.
The full 226-test Python suite, Ruff lint/format, source mypy, JavaScript
syntax, and package build passed. After dashboard restart, the shell and new
script returned HTTP 200, the capture element was present, and unpaired
Strategy Lab/status requests returned HTTP 401. SQLite `quick_check` was `ok`;
paper autostart and market collection were enabled. Five paper positions
remain open and complete autonomous exits remain zero. The live environment
flag is false and the source execution lock is true.

### Forward entry evidence audit (11:24 UTC)

The earlier forward-performance filter accepted a four-timeframe candle record
without the saved market-policy version, thresholds, market inputs, and market
score needed to reproduce its entry. A read-only check of the five open paper
positions found all five lack that complete market evidence. The filter now
requires the current market-policy version and complete market and candle
inputs before an autonomous position can enter the measured sample. New
strategy paper entries are rejected before quoting if the evidence is missing
or its embedded mint differs from the requested mint. Older positions remain
visible and managed by the exit worker, but will be excluded from verified
strategy performance when they close. The verifier now reports **5/5 open
positions without complete entry evidence**, rather than 3/5. This corrects
the denominator; it does not create a loss or a win.

One paper test used random mint ordering to assume which non-priority token
would fall outside an eight-slot audit budget. That assertion occasionally
failed despite correct production sorting. It now checks the documented
age-then-mint order deterministically. Focused tests cover current strategy
evidence, legacy exclusion, and mismatched embedded mint rejection.
All 228 Python tests passed, as did Ruff lint/format, source mypy,
JavaScript syntax, and sdist/wheel build. The restarted local dashboard
returned HTTP 200 for its shell and auth status, HTTP 401 for unpaired
private status and Strategy Lab requests, and SQLite `quick_check` returned
`ok`. Paper autostart remains enabled, the live environment flag is false,
and the source live-execution lock remains true.

### Narrative freshness and underlying mention time (11:28 UTC)

The strategy previously had no age check for a stored narrative signal. A
first fix checked its `updated_at`, but inspection showed that this timestamp
is refreshed when the same old articles are recalculated. The signal now also
stores the newest underlying mention's publication time. The version-2 market
policy awards a narrative bonus only when the calculation is at most 15
minutes old **and** the newest mention is at most one hour old. Missing,
malformed, future, and legacy timestamps add zero points. A corroborated
contract-linked security claim remains a manual-review gate even if its
signal is old. This is a fail-closed safety rule, not evidence that the
narrative indicator improves net trading performance.

A nullable SQLite column is added in place for existing `narrative_signals`
tables. Legacy rows have no underlying mention time and therefore cannot
receive a score bonus until refreshed. Unit tests cover fresh, stale, future,
and missing times, old articles recalculated now, persistence, and migration.
The live journal migrated with SQLite `quick_check` returning `ok`. The
restarted collector repopulated nine signals with actual mention timestamps.
All nine latest mentions were more than one hour old at the runtime check
(newest about 211 minutes old), so none qualifies for a narrative entry bonus.
The Discovery table now labels such rows as older evidence and shows mention
age. This does not prove that the other market or candle gates would accept
those tokens.
The full 234-test Python suite, Ruff lint/format, source mypy, JavaScript
syntax, and package build passed. After the dashboard resumed, the shell and
new script returned HTTP 200, and the unpaired narrative API returned HTTP
401. The paper worker remains on, the live environment flag is false, and the
source live-execution lock is true. No complete autonomous paper exit exists
yet, so narrative usefulness and strategy profitability remain unmeasured.

### Independent paper exit monitoring (12:00 UTC, 3 November 2026)

Runtime inspection found that the discovery worker was progressing through a
long batch of candidate audits, but position marks were about 25 minutes old.
Its single cycle refreshed exits only before the audit batch, despite a
five-minute schedule. This could delay a stop, take-profit, or maximum-hold
paper exit until the audit finished. The scanner was active rather than hung;
the flaw was shared scheduling.

Exit quotes and rules now run in a separate paper worker, scheduled 120 seconds
after each completed check. Discovery and entry decisions run independently.
Automated entries fail closed if the exit worker is stopped, stale, or reports
quote or exit failures. The entry worker rereads portfolio risk and worker
health after its audit, immediately before considering an entry. Operational
status and the console expose the exit worker separately. Forward mark-path
coverage uses the actual exit-monitor interval, so the performance denominator
does not silently allow gaps based on the older discovery cadence.

Tests deliberately held discovery in a blocked scan while the exit worker
closed an expired paper position; the scan was still blocked when the close
completed. Separate tests cover missing, stale, and failed exit-monitor states,
and a dashboard test verifies start/stop controls both workers. The full
237-test Python suite, Ruff lint and format, source mypy, JavaScript syntax,
and sdist/wheel build passed. After restart, the local dashboard returned
HTTP 200, private endpoints returned HTTP 401 without pairing, SQLite
`quick_check` passed, and the exit worker had completed a cycle with zero
quote and exit failures. All five open paper marks were about 11-12 seconds
old at the check. The live environment flag remains false and the source
execution lock remains true.

There are still zero complete autonomous exits and five open positions lack
full entry evidence. The revised schedule has not been observed over a long
forward period; the short runtime check and concurrency test establish that
the exit lane operates independently, not that exits will always land or that
the strategy has positive net expectancy. No profitability claim is supported.

### Research candle scheduling and observed limits (12:08 UTC, 3 November 2026)

The next evidence bottleneck is timely 1m OHLCV, not a missing indicator. The
read-only replay had 40 prospectively selected mints and **zero complete
comparable trades**: 17 insufficient-history, seven insufficient-confirmation,
six insufficient-candle-availability, six no-eligible-trade, three censored
unclosed, and one missing-1m-history results. At inspection, only 403 of 1,420
membership 1m candles had been fetched within 120 seconds of closing. In the
current 20-mint cohort, 93 of 177 observed focus bars and 14 of 227 secondary
bars met that timing. Those fractions are among *observed bars*, not all
elapsed minute slots; they cannot establish complete price paths.

The current DEX batch showed five-minute trades for only two of the 20 fixed
research members. Many inactive pools returned old candles. Previously the
oldest-fetch scheduler repeatedly spent scarce public OHLCV calls on these
pools before a trading secondary-cohort member. Collection now gives current
five-minute-active members a slot within the fixed focus/secondary schedule
and alternates which active lane gets the first research call. The cohort
selection itself remains score-independent and unchanged; inactive and missing
members stay in the denominator. The Strategy Lab labels activity as an
observation priority, not an entry approval. This conditional observation
schedule must be disclosed when interpreting any later performance results.

Focused tests verify both active lanes receive scheduled calls, successive
cycles alternate the first research call, and membership remains intact.
After restart, a completed collection cycle
reported two members with five-minute DEX trades, one active member scheduled,
one secondary-cohort request attempted and succeeded, four of four OHLCV
requests succeeded, and 18 authentic candles refreshed. This single cycle
shows the revised path executed; it does not prove sustained coverage or a
profitable strategy. The public provider still rate limits intermittently.

### Exit-monitor health visibility (12:12 UTC, 3 November 2026)

The separate exit worker completed scheduled checks at 12:07:42 and 12:09:44
UTC while discovery and collection continued. Its position marks remained
recent and it reported no quote or exit failures at inspection. A running
thread alone had previously been enough for the operations view to appear
healthy even if its last successful exit check was overdue. The entry gate
already failed closed on that condition; the health view now uses the same
monitor-status rule and reports a degraded `exit_monitor_stale` or
`exit_monitor_unavailable` state. The Overview, readiness list, and automation
badge display that condition directly. Tests cover missing, stale, and fresh
monitor states. The full Python suite, Ruff lint/format, source mypy, and
JavaScript syntax passed after the change. The restarted dashboard returned
HTTP 200 for its shell and script, HTTP 401 for unpaired private endpoints,
and SQLite `quick_check` passed. Operational health was healthy with the exit
monitor running; the environment live flag remained false and the source
execution lock remained true.

This closes a visibility gap, not the performance-evidence gap. Zero complete
comparable replay trades and zero verified autonomous forward exits still
prevent a net-expectancy or strategy-superiority claim.

### Point-in-time market snapshot retention (12:20 UTC, 3 November 2026)

The market snapshot journal previously rounded observations to a minute and
replaced the earlier row when a later poll arrived in that minute. The replay
lookup respected `captured_at`, so it did not expose later data to earlier
decisions, but it could no longer retrieve an overwritten earlier market
observation. This is a history-integrity defect for prospective comparisons.

New snapshots retain their actual capture second. The first observation in a
second is kept if another write has the same timestamp. Existing minute-bucket
records were not rewritten; values previously overwritten cannot be recovered.
Tests cover two observations in one minute, a same-second duplicate, selection
of the observation available at a decision timestamp, pool-matched liquidity,
and rejection of liquidity observed after a candle closed. The full Python
test suite, Ruff lint/format, source mypy, and package sdist/wheel build passed.

After restarting the local dashboard with the change, its shell and JavaScript
asset returned HTTP 200 and three unpaired private APIs returned HTTP 401.
SQLite `quick_check` returned `ok`; `LIVE_TRADING_ENABLED` was false and the
source execution lock was true. The paper, exit, market, and narrative workers
were marked running. In a two-minute database inspection, 12 mint/minute
groups contained at least two distinct non-rounded second-level observations,
confirming that the new retention path executed during real collection.

This improves future market-gate evidence. It does not make old history
complete, prove parity of every replay gate, create a comparable closed trade,
or support a claim of profitability. The original candle strategy and
early-launch strategy still lack adequate point-in-time inputs for a fair
historical comparison.

### Conservative historical market hard gates (12:25 UTC, 3 November 2026)

Inspection found that momentum replay used authentic closed candles and saved
audits, but could still simulate an entry without applying the paper strategy's
recorded DEX market hard gates. The replay and anchored walk-forward paths now
select only a market snapshot captured by each decision time and reject a
missing or older-than-180-second snapshot, a mismatched pool, liquidity below
the configured minimum, 24h volume below the configured minimum, and a 24h
move beyond the shared 250% guard. Invalid or unavailable liquidity and volume
also block. Older rows without a trusted capture time are excluded. These
checks use the paper policy's configured thresholds, but do not reconstruct
the historical narrative bonus, complete market score, or worker schedule;
`historical_market_gate_parity` remains false. An empty history is reported as
`insufficient_market_history`, including in walk-forward results.

Tests cover valid prior evidence, missing/future/stale observations, pool
mismatch, liquidity and volume failures, extreme moves, same-minute ordering,
and walk-forward failure when market history is absent. The full Python suite,
Ruff lint/format, source mypy, and package build passed. The restarted local
dashboard served its shell and asset with HTTP 200; unpaired private status,
Strategy Lab, and operations routes returned HTTP 401. SQLite `quick_check`
was `ok`; all four worker states were running, the live environment flag was
false, and the source execution lock was true.

Read-only verification examined 40 prospectively selected mints. There are
**zero closed comparable replay trades**. The statuses are 17 insufficient
history, seven insufficient market history, six insufficient candle
availability, four no eligible trade, three censored unclosed, two insufficient
confirmation history, and one missing 1m history. Five paper positions remain
open; the one earlier closed position does not satisfy the prospective
comparison. The extra gate changes eligibility classification but provides no
profitability or strategy-superiority evidence. The original candlestick and
early-launch strategies still cannot be compared fairly from the recorded
point-in-time dataset.

### Timestamped narrative refresh evidence (12:28 UTC, 3 November 2026)

The current `narrative_signals` table replaced its contents on each refresh.
Before this change, a later replay could not know which signal existed at an
earlier decision or whether a completed refresh had cleared it. The new
append-only `narrative_signal_observations` table records each complete signal
set with a microsecond capture timestamp, including an empty set. The current
view and its observation commit in one SQLite savepoint. A point-in-time
lookup returns the latest refresh available by a decision time and explicitly
distinguishes no prior refresh from a refresh with no signal for that mint.
Older refreshes are not reconstructed; the replay does not yet consume this
history to reproduce the complete market score, so market-gate parity remains
false.

Before applying the additive schema, a consistent 47,886,336-byte backup was
created and passed SQLite `quick_check`. Tests verified prior versus cleared
signals and rollback of both current and historical state after a failed
refresh. The full Python suite, Ruff lint/format, source mypy, and package
build passed. After dashboard restart, SQLite `quick_check` was `ok`, the new
table existed, and the narrative worker recorded one actual refresh with nine
signals matching the nine in the current view. The dashboard shell and asset
returned HTTP 200; three unpaired private APIs returned HTTP 401. Paper,
exit, market, and narrative workers were marked running; the environment live
flag was false and the source execution lock was true.

This creates prospective evidence for future replay. The current 40-mint
research set still has zero complete comparable closed trades, so no net
expectancy or strategy superiority can be inferred.

### Point-in-time narrative market scoring (12:31 UTC, 3 November 2026)

The replay and walk-forward paths now call the same `evaluate_candidate`
market scorer used for paper entry after the saved candle, audit, and DEX
hard gates pass. A decision uses the last *completed* narrative refresh at or
before its timestamp, including an explicitly empty refresh. If no refresh
was recorded by then, the simulated entry is rejected with
`narrative_history_missing`; later news or a later cleared signal cannot be
read backward. Rejected and below-threshold market scores block replay
entries. An accepted simulated trade records its replayed market score.

Tests exercise a corroborated security claim, later clearing, missing prior
history, reject/observe/eligible score outcomes, and a logged accepted score.
The full Python suite, Ruff lint/format, source mypy, and package build passed.
The restarted dashboard served its shell and asset with HTTP 200 and returned
HTTP 401 for three unpaired private routes. SQLite `quick_check` passed; the
paper, exit, market, and narrative workers were marked running. The live
environment flag remained false and the source execution lock true.

In the read-only 40-mint research report, the score gate was configured for
39 mints with 1m history and evaluated 14 candidate moments; 12 entry
rejections were due to absent pre-decision narrative history. There are still
**zero complete comparable closed replay trades**. Historical policy settings
and worker decision cadence are not fully recoverable, and old minute-bucket
market observations may be incomplete. Therefore the report keeps
`historical_market_gate_parity` and `paper_entry_schedule_parity` false and
does not claim positive expectancy or strategy superiority.

### Bounded DEX snapshot decision paths and paper balance clarity (16:04 UTC, 3 November 2026)

The verification report now includes a separate, price-only follow-up for the
latest 100 age-eligible saved decisions at each 5-minute and 15-minute horizon.
It pairs the last DEX Screener snapshot from *before* each decision with a
snapshot captured at or within two minutes after the horizon. Entry snapshots
older than two minutes, ambiguous captures from the decision's own second,
changed pools, and late or absent target snapshots are censored. Missing
outcomes remain in the denominator. This does not supply candles, intrahorizon
extremes, executable quotes, trade costs, or realised P&L, and cannot replace
the authentic OHLCV study for strategy validation.

At 16:04 UTC, 46/100 five-minute and 39/100 fifteen-minute decisions had a
bounded snapshot price comparison. Authentic timely OHLCV price paths covered
only 3/100 at each horizon. The snapshot study improves observation of
rejected calls, but the missing 54 and 61 decisions prevent a broad
false-negative claim. It does not establish a profitable alternative entry
rule. Tests cover pre-decision timing, same-second ambiguity, stale or missing
snapshots, pool changes, target delay, and retention of missing outcomes. The
full Python suite, Ruff lint, mypy, JavaScript syntax check, and package build
passed; Ruff formatting passed after formatting the new tests.

The forward paper journal had zero open and one closed position at the same
read-only inspection. The closed position's simulated stop-loss returned
0.007398394 SOL from a 0.01209428 SOL entry debit, leaving 0.995304114 SOL
cash against 1 SOL starting capital. The increase in displayed *cash* on exit
was a transfer from the open position back to cash, not a gain. The paper page
now labels this state as no open positions and realised paper P&L. The trade
lacks complete comparable entry evidence, so it remains excluded from the
strategy-performance sample. The live environment flag was false and the
source execution lock remained true.

### Decision-outcome observation bias and failed paper entries (16:09 UTC, 3 November 2026)

The current two-mint research focus had 46 trusted timely closed 1m bars in
166 eligible minute slots; the other 18 cohort members had 14 in 1,494. This
is too sparse for a fair strategy ranking, and a missing bar can mean either
no trade or unavailable provider data. The prospectively selected
market-eligible follow-up has two subjects with timely 5m and 15m paths, but
these are price-only paths and still have no executable net-return estimate.

The DEX snapshot follow-up now reports an observed/total denominator for each
saved final decision reason. In a read-only latest-100 five-minute cohort,
only 26/79 decisions ending in “no token-linked narrative evidence” had a
bounded price observation, whereas 16/16 ending in missing/stale candle
evidence did. Those groups have very different observation coverage; comparing
their observed price moves alone would introduce selection bias. A snapshot
price change is not a trade return.

One otherwise eligible decision at 15:58 UTC ended in
`simulation_failed:PaperExecutionError`. Its historical record contains only
the exception class, so the exact failure cause cannot be recovered. Future
paper entry failures now record a fixed, non-sensitive execution reason code
such as `entry_liquidity_unavailable`, `stale_route`, or
`liquidity_share_limit`; unknown errors still record only their class. This
does not relax any entry or risk gate. The recent-decisions table shows a
readable execution rejection and the final decision reason; its previous
"Primary Reason" column showed the first audit note even when a later candle
or execution gate determined the outcome. The full Python test suite, Ruff
lint/format, mypy, JavaScript syntax check, and package build passed. The
dashboard was restarted with the new code; its shell and asset returned HTTP
200, an unpaired portfolio request returned HTTP 401, and all four worker
states were running without a recorded error at 16:11 UTC. There were zero
open paper positions. The live environment flag was false and the source
execution lock true.

### Paper entry cash budget and replay exposure (16:18 UTC, 3 November 2026)

Read-only forensics of the sole closed paper trade found a 0.010 SOL swap but
0.01209428 SOL total buy debit after modeled network fees, priority fees, and
new token-account rent. The `PAPER_RISK_PER_TRADE_BPS=100` setting had sized
only the swap, so the modeled debit exceeded the advertised 1% of available
paper cash. The prior mark at 15:27 UTC valued the position at 0.008837224
SOL; the 15:29 UTC stop mark and simulated sale were 0.007398394 SOL. With a
two-minute exit poll, the loss moved from roughly 26.9% to 38.8% of the
original buy debit between observations. A stop is a trigger checked at poll
time, not a guaranteed fill price.

Autonomous and call-driven paper sizing now subtracts modeled buy fees and
token-account rent from the configured total cash budget before requesting a
swap. Under the current 1 SOL starting balance and 1% budget, the modeled
swap input is 0.00790572 SOL and the full buy debit is at most 0.010 SOL.
Budgets that cannot fund the 0.001 SOL minimum swap fail closed. The same
calculated swap input drives the replay's initial exposure estimate: 79 bps
of starting cash rather than the 100 bps total-debit budget. The estimated
single-exit round-trip cost rose from 2,209 to 2,778 bps of swap input,
primarily because fixed rent is large relative to a micro trade. Tests cover
the exact modeled debit, insufficient budget, replay API rejection, and cost
arithmetic. Current read-only verification still reports zero comparable
replay trades; the remaining replay model uses a fixed initial exposure and
aggregate cost estimate rather than dynamic per-trade cash debits and live
Jupiter fills. It must not be treated as paper/live execution parity or proof
of profitability.

The full Python suite, Ruff lint/format, source mypy, JavaScript syntax
check, and package sdist/wheel build passed after this change. The dashboard
was restarted with the new sizing rule. At 16:19 UTC its shell and asset
returned HTTP 200, an unpaired portfolio request returned HTTP 401, and all
four workers reported running without a recorded error. The environment live
flag was false, the source execution lock true, and there were zero open
paper positions. This runtime check verifies the guard is loaded; a future
complete autonomous entry and exit are still needed to measure its actual
effect on forward performance.

### Paper valuation when an exit mark is unavailable (3 November 2026)

The existing portfolio conversion treated a missing open-position exit mark
as zero. Immediately after a paper entry, or after a first quote failure,
this produced a false full-loss P&L and understated displayed equity. The
portfolio now returns unknown position value and P&L until it has a mark,
plus `unvalued_position_count` and `all_positions_have_marks`. Its numeric
cash-plus-known-marks equity remains a conservative bound for portfolio risk
checks, while the dashboard shows total equity/P&L as unavailable. The entry
breaker already pauses new positions without a fresh mark. Dashboard labels
now identify full entry cash cost, net proceeds plus marks, and the fact that
a rising quote can remain below break-even after modeled costs.

Regression tests cover a newly opened unmarked position, an unavailable
first exit quote, recovery after a valid mark, and the existing stale-mark
and partial-exit paths. The full Python test suite, Ruff lint/format, source
mypy, JavaScript syntax check, and package build passed. After restarting,
the local dashboard shell and updated asset returned HTTP 200; an unpaired
portfolio request returned HTTP 401. A paired read-only check returned all
four workers running without recorded errors. The journal still had zero
open positions and one closed position, with 0.995304114 SOL simulated cash
and -0.004695886 SOL realized paper P&L. The environment live flag was false
and the source execution lock true. No new autonomous trade occurred during
this verification, so the change has not yet produced new performance data.

### Prospective pool liquidity evidence for replay (3 November 2026)

The replay comparison remained empty even after authentic 1m candles arrived:
five sampled mints had saved candles but no matching saved market snapshots.
DEX Screener returned token prices for those pools with no liquidity field,
and the collector correctly declined to manufacture liquidity. A missing
liquidity input cannot support a historical entry or a net-return estimate.

The collector now makes an optional, paced research request to GeckoTerminal's
fixed Solana pool endpoint for at most ten exact pools per request. It checks
the returned pool and token identities, validates the numeric values, and
records the observed pool reserve and price with the actual receipt time and
`geckoterminal_pool` source label. A zero reserve remains zero; it does not
pass the strategy's minimum liquidity rule. Existing snapshots retain
`legacy_unknown` provenance, and DEX Screener snapshots are labelled
`dexscreener` going forward. The extra data is for prospective replay
evidence only. It does not promote a candidate, bypass any paper entry gate,
or fill past periods where liquidity was not observed. Provider rate limits
or failures pause this optional lane without changing OHLCV collection or
paper risk checks.

The read-only verification report now exposes snapshot counts by source and
the probe's last bounded status. A pre-migration journal still opens read-only
for verification, with its older snapshots explicitly attributed to unknown
legacy provenance. Parser, identity, zero-reserve, rate-limit, request-bound,
round-robin, and old-schema regression tests passed. At 16:36 UTC, before the
new collector was restarted, the active paper journal had 39,071 legacy
snapshots, 44,703 saved pool candles, and zero comparable closed forward
trades. The one closed paper position was excluded from comparable performance
because its entry route evidence predates the route-parity fix. Its simulated
cash was 0.995304114 SOL after a 0.004695886 SOL stop-loss. No position was
open; the live flag was false and the source execution lock true. These
counts and the new source-labelled observations will be rechecked after the
collector restart. They do not establish strategy profitability.

After restart at 16:40 UTC, the dashboard shell and JavaScript asset returned
HTTP 200; an unpaired portfolio request returned HTTP 401. A paired read-only
check showed all four workers running without an error, no open paper position,
and the same 0.995304114 SOL paper cash. The collector saved 90 new
DEX Screener snapshots and ten exact-pool GeckoTerminal snapshots; its first
pool probe reported success with ten requested pools. The sampled replay
population had 20 mints, seven with saved 1m candles and 13 without. Every
sampled replay still reported insufficient historical evidence and zero
comparable trades; consequently, no strategy return or win rate is reported.
The dashboard reported a separate real-wallet balance of 0.037145420 SOL.
The paper ledger had no live orders, the environment live flag was false, and
the source execution lock was true. The full Python suite, Ruff lint/format,
source mypy, JavaScript syntax check, and package build passed on the final
source. This check verifies the new data path prospectively, not historical
backfill or profitable performance.

### Prospective market-eligible follow-up replay (3 November 2026)

The random launch cohort and the existing market-eligible follow-up serve
different questions. The former samples discovered mints without choosing on
current momentum; the latter fixes one token from the eligible market pool for
an hour to collect a denser series. The read-only verification report now
replays the latest 20 eligible follow-up selections separately. Each replay
fixes its pool from a snapshot recorded no later than selection, restricts
entries to its stored selection window, and uses the same baseline candle,
audit, liquidity, volume, cost, and paper-style exit rules as the random-cohort
replay. A pool observed only after selection cannot enter the sample.

At the first read-only check, two eligible follow-ups were recorded. The
Slingoor selection had 59 evaluated entry decisions, no entry and no closed
trade; 39 were rejected for unconfirmed 1m candles, 11 for unconfirmed 5m,
six for mixed 15m provenance and three for stale 1m data. The other selection
had 14 entry decisions, one prospective entry and **no closed trade**: its
exit path was censored by a gap in the authentic 1m series. Its remaining
entry rejections were six unconfirmed 1m, five unconfirmed 5m, one stale 1m
and one stale audit. Across both selections there were zero realized replay
trades and one censored trade, so win rate, profit factor, return, expectancy,
holding period and drawdown are unavailable. This conditional sample cannot
replace the broader launch cohort or prove profitability. Tests verify that
the report rejects a pool first observed after selection. The full Python
suite, Ruff lint/format, source mypy and package build passed after this
addition. The report is read-only and did not submit an order or modify a
paper fill.

### Shared public provider quota (3 November 2026)

Read-only worker records showed a pool-reserve probe rate limit at 16:42 UTC,
followed by a six-call OHLCV trial that succeeded twice and hit another rate
limit at 16:44 UTC. The candle worker then had two cycles with zero completed
calls during the provider cooldown. This is evidence that the optional pool
probe and the higher candle trial compete for the same public service
capacity; the exact provider quota and any other users sharing this route are
unknown.

The collector now runs the optional pool probe at most every five minutes
after success, defers it while the six-call candle trial is active, and waits
at least ten minutes after a pool-probe 429. That 429 also keeps the candle
lane at its four-call budget for an hour. This prioritizes timely authentic
candles and keeps the probe's research data separate from entry approval.
Tests cover success pacing, shared-quota backoff, and six-call deferral.
The full Python suite, Ruff lint/format, source mypy, and package build
passed. After restarting the dashboard at 16:47 UTC, its shell and asset
returned HTTP 200, an unpaired portfolio request returned HTTP 401, and a
paired read-only health check showed all four workers running without errors.
There were zero open paper positions; the environment live flag was false and
the source execution lock true. Sustained provider behavior under the new
cadence still needs prospective observation.
The first post-restart collector cycle completed a ten-pool research probe at
16:48 UTC and set its next eligible probe five minutes later. The candle
budget remained four calls per cycle. One successful cycle does not establish
sustained quota stability.

### Exact-address narrative linkage (3 November 2026)

The narrative collector was running, but one stored Bluesky item named an
exact contract address for a locally discovered token and still had an empty
matched-token list. That candidate had fallen outside the 100 most recent
discovery candidates used for matching, so its mention was absent from the
token's narrative signal. The collector now performs a bounded lookup of
explicit contract addresses against the local discovery journal when it
rebuilds signals. It never fetches or trades an unknown mint from a social
post, and linking a stored item does not backdate or refresh its timestamp.

The previously missed item is now linked to
`9X5UwHT7gWSPtAH5yeEzrLHqv3Nedmhy3HyZN3Yspump`. The resulting signal
remains `quiet`, has `social_only_evidence`, and retains its original mention
time of 06:38 UTC. It cannot supply a fresh narrative entry bonus at the
16:52 UTC check. Google News and Bluesky collection were working at that
check; GDELT timed out and was marked degraded. A focused regression test
covers an exact-address item for a candidate outside the newest 100, an
unknown address, and timestamp preservation. The full Python test suite,
Ruff lint and format checks, source mypy, and package build passed. After
the dashboard restart, the narrative worker completed without an error and
the linked signal was present. This improves attribution but supplies no
new evidence of positive trading expectancy.

### Follow-up window status and candle capacity (3 November 2026)

The third market-eligible follow-up was still inside its one-hour selection
window, but the read-only replay labeled its partial path `complete`. The
replay now labels that case `collecting` and reports how many selection windows
have expired. A regression test checks both the active and expired status.
At the 16:56 UTC read-only check, three selections were recorded: two expired
and one collecting. There were zero closed replay trades and one censored
entry across the sample. The collecting token had eight entry evaluations,
seven rejected for authentic 1m gaps and one for stale 1m data. Its first ten
post-selection 1m bars had been fetched within 120 seconds of closing, so the
initial gaps included pre-selection history; the observation window had only
been running for about twelve minutes. No performance statistic is inferred
from this incomplete sample.

The last-hour operational check recorded 211 successful public OHLCV network
requests over 3,519 seconds (3.6 per minute), with six rate-limited cycles.
There were 25 currently market-ready mints; refreshing all of them every two
minutes would require at least 12.5 calls per minute before research or deep
timeframes. The existing pinned focus and fail-closed candle gates are still
necessary. Public GeckoTerminal OHLCV is keyless, while the configured Helius
RPC and Jupiter gateway already use separate API keys. At this observation,
no OpenAI API call or model-derived entry decision had occurred.

### Optional OpenAI narrative research (3 November 2026)

An optional, disabled-by-default Responses API path now assesses at most one
recent public item per narrative refresh, and only if the item already has an
exact contract-address match to a known token. It sends a bounded title,
excerpt, publication time and contract list to a fixed OpenAI host, requests
structured output with `store=false`, and uses no model tools. A database
reservation counts attempts against a configurable UTC daily cap before any
HTTP request. Successes record a short source-claim summary, model and prompt
version, and token usage; failures record only a safe error code. Paired
dashboard users can see assessments on their source items. The model result
has no path into narrative trading scores, audits, entry approval or exits.

Mocked provider tests verify request limits, response validation, error-body
secrecy, daily cap, and that model assessments leave signals, decisions and
paper fills unchanged. Dashboard tests verify the key is never returned and
an unpaired narrative request is denied. The local `.env` has not been given
an OpenAI key and the feature remains off; no paid API call or model-quality
assessment can be verified yet. The candle-capacity and zero-comparable-trade
limitations above remain. Any promotion of model output into strategy logic
requires a separate out-of-sample and forward paper comparison.

After restarting the local dashboard on 3 November, the shell returned HTTP
200, an unpaired narrative request returned HTTP 401, and a paired request
reported `enabled=false`, `key_configured=false`, `trading_signal=false`.
The narrative worker completed a cycle and its compact status reported
`openai_research.status=disabled`; no source items had model assessments.
Both narrative and general status reported live trading disabled. The full
Python test suite, Ruff lint and format checks, source mypy, and package build
passed after the worker-status fix. This verifies the disabled local path,
not a real provider call or model-driven trading performance.

### Decision reason attribution and current candle limit (17:20 UTC, 3 November 2026)

The latest read-only verifier counted 1,197 discovery candidates, 45,513 stored
pool candles, 2,900 saved strategy decisions, one closed paper position, and
zero saved backtests. Of the latest 100 current discovery candidates, 24 passed
the market-only gate; 22 of those lacked complete fresh 1m/5m entry history,
two had nonconfirming signals, and none was entry-eligible. The last-hour
public OHLCV throughput was 207 successful calls in 3,534 seconds (3.51 per
minute), while refreshing all 24 market-ready mints every two minutes would
require at least 12 calls per minute before research. The three prospective
market-eligible follow-ups still had zero closed comparable replay trades.
The first two windows had ended; one window was still collecting. The first
had 59 entry evaluations and no confirmed entry, the second had a censored
unclosed path after a data gap. No strategy return is inferred.

The decision table and price-path summaries had been labeling a rejected
token's final score explanation as its main reason. In one current case the
display said "no token-linked narrative evidence" although the audit failed.
The read-only API now chooses the first recorded blocking market gate for a
rejection and keeps the complete reason list; observed candle decisions still
show their last decision reason. The live dashboard check found 75 recent
rejections with market gates, and the example now reports "on-chain audit did
not pass" while retaining the narrative explanation in its full list.
The separately regenerated five-minute decision-path report had 76 rejected
rows with saved market gates; all 76 attributed their primary reason to the
first recorded blocking gate.
Unpaired access returned HTTP 401, paired access HTTP 200, all four workers
were running without worker errors, and live execution remained disabled.
Operations still marked the market collector degraded because OHLCV capacity
was below demand. Full Python tests, Ruff lint and format, source mypy,
JavaScript syntax, and package build passed. This is
an explainability repair, not a change to trading thresholds or evidence of
positive performance.

### First saved baseline and walk-forward attempts (3 November 2026)

The active forward-paper campaign records a start time of 14:42:54 UTC on
3 November. The earliest possible end of its required 30-calendar-day
observation window is 14:42:54 UTC on 3 December, contingent on continuous
operation. That date does not predict completion of the trade-count, market
regime, performance, soak, or security gates. The Overview now separates the
30-closed-trade preliminary reading threshold from the 200-closed-paper-trade
validation target and labels the 30-day clock as elapsed time, not proof of
continuous observation.

One authenticated, CSRF-protected, paper-only baseline replay was saved for a
member of the current score-independent research cohort using the configured
1m four-factor strategy and TP ladder. It examined 80 entry points inside the
recorded membership window, rejected 60 for 1m history still collecting,
four for gaps, and 16 for staleness. It produced zero trades, so its `0.0%`
idle-cash return is not a strategy performance estimate. A separate saved
walk-forward attempt returned `insufficient_market_history`, with no
out-of-sample performance claim. Both runs were read-only with respect to
wallets and did not submit on-chain orders. More contemporaneous data and
closed paper positions are needed before strategy comparison is meaningful.

After the dashboard reload, a paired browser check showed `0 / 200` complete
autonomous exits, the separate 30-trade preliminary threshold, `0 / 30`
elapsed paper days, and an earliest possible paper-window end of 3 December.
Desktop and 390-pixel mobile renderings kept this message legible. An unpaired
Strategy Lab request returned HTTP 401; a paired request returned both saved
runs. All four workers were running, the market collector still disclosed
capacity below demand, and live trading remained disabled. The full Python
suite, Ruff lint and format, source mypy, JavaScript syntax, and package build
passed after the Overview change.

### Prospective paper-worker continuity (3 November 2026)

The previous worker status retained only each worker's latest cycle and could
not substantiate the required 30-day paper observation or unattended soak. An
additive SQLite `worker_cycle_history` table now records each newly completed
paper-decision, exit-monitor, market-collector, and narrative cycle. The
latest status and completion record are committed together. Existing worker
status is not backfilled. A fresh run ID changes on every worker start and
lets the continuity report identify brief restarts even when the heartbeat
gap stays within tolerance. Errors, pauses, stale heartbeats, and gaps beyond
twice the configured interval plus 60 seconds reset the current streak.

The authenticated operations API now reports recorded cycle counts, errors,
heartbeat gaps, restarts, and the shared paper/exit/market streak. Overview
shows recorded worker days against the 30-day target and gives an earliest
possible end date only when the streak is current. This is process evidence;
market-data quality, realistic fills, sufficient closed trades, and positive
net expectancy remain separate gates. The table migration only adds data.
Rollback should preserve this table to retain observation history.

Before restart detection was added, a live dashboard read showed one
prospectively recorded paper-decision cycle, two exit-monitor cycles, and two
market-collector cycles, with a 63-second shared streak. The same health
response still marked the market collector degraded because public OHLCV
throughput was below the refresh demand. The new run-ID release will start a
new observation streak when the dashboard restarts; earlier time cannot be
credited to the 30-day requirement.

Two direct public OHLCV probes on current active and research pools returned
real recent closed candles without empty intervals or gaps. Enabling
synthetic empty intervals would therefore not address the observed coverage
shortfall for those pools. [Helius Free pricing](https://www.helius.dev/pricing)
includes standard WebSockets; its [parsed streams](https://www.helius.dev/blog/parsed-events-and-streams)
are paid. A free standard log subscription would require separately decoding
and validating raw swap events and is not yet an equivalent candle source.
No new indicator or trade threshold was introduced, and live execution remains
locked.

After the run-ID release, authenticated health showed the expected reset:
each required worker had one recorded restart and a new shared streak rather
than inherited pre-restart time. The paired Overview rendered the unverified
message while the new paper cycle was pending, and the API later reported a
current 42-second shared streak. `live_enabled` and Strategy Lab's live flag
both returned `false`. The full Python suite, Ruff lint/format, mypy, JS
syntax check, and package build passed after the run-ID change.

At that point, 28 of the latest 100 discovery candidates cleared the market
gate, 25 lacked complete fresh entry candles, three had nonconfirming signals,
and none was entry-eligible. The two pinned strategy-focus pools did have
recent, gap-free authentic 1m candles and complete 5m bars derived from those
minutes. Their 1m and 5m signals were both unconfirmed: HELL showed a down
trend and USOE lacked momentum and relative volume. This proves the focused
lane can reach signal evaluation, while the public feed cannot yet refresh the
full eligible set. There is still no closed-trade performance basis for
loosening the entry thresholds.

### Candle capacity and research sample check (3 November 2026)

The collector was still running and the latest 12 saved cycles each attempted
four pool OHLCV calls; the most recent had no provider errors. Its six-call
trial remained in a recorded cooldown after earlier rate limits. The current
market gate had 28 eligible tokens. Refreshing each every two minutes would
require at least 14 calls/minute, plus research and deeper frames. The public
GeckoTerminal service [publishes a 30-call/minute limit](https://support.coingecko.com/articles/6867086195-does-geckoterminal-have-an-api),
but that headline is not the observed capacity of this connection. The
collector's recent successful network pace was about 3.4 calls/minute.

CoinGecko's [free Demo plan](https://www.coingecko.com/en/api/pricing)
publishes 100 calls/minute and 10,000 calls/month. At 14 calls/minute, the
present eligible universe would require about 20,160 calls/day and 604,800
over a 30-day month, before other endpoints. Thus a Demo key is suitable only
for bounded research backfill here, as the README already states; its short
term rate headline does not solve sustained live candle coverage.

The current score-independent research cohort had 20 selected mints. Only
two had any saved authentic 1m bars on their recorded pools; one had 31 such
bars and five complete 5m aggregates, while another had 210 1m bars but nine
complete 5m aggregates. The other 18 had no saved 1m bars yet. This cohort
cannot support a fair original-versus-upgraded strategy performance comparison
or a defensible out-of-sample profitability estimate. The independent sample
selection and existing paper entry gates were left unchanged.

The continuity read has also been bounded to the latest 31 days of recorded
cycles. Its returned cycle, gap, error, and restart counts are explicitly
windowed; the extra day permits proof of a 30-day current streak without
loading an unbounded campaign history on every dashboard health request. A
synthetic 45-day campaign test verified that older rows are excluded while a
30-day streak remains provable. The focused worker and operations tests, Ruff
lint/format, and mypy passed after this change.

### Exit-monitor restart gate (3 November 2026)

Review of the restarted worker exposed a safety defect: its restored
`last_completed_at` and result could still fall within the freshness window
before the new exit-monitor process finished a cycle. The paper entry path
could therefore accept a prior process's check during the new process's first
discovery scan. The worker now stamps each completed cycle with its current
run ID. Exit-monitor health requires the completion's run ID to match the
running worker, and the dashboard's paper decision cycle reads the current
in-memory exit worker immediately before deciding on entries. A stale
persisted status cannot authorize an entry even if its database update fails.
Focused tests cover a recent prior-run result being rejected, a current-run
result being accepted, and the production paper decision path rejecting a
prior-run result. Live execution remains locked.

The full Python suite, Ruff lint and format, mypy, JavaScript syntax, and
package build passed. On the restarted live dashboard, the new exit-monitor
cycle completed with a run ID matching the active worker, while the overall
paper continuity remained unverified until all required workers completed in
the new process. Strategy Lab continued to report live trading disabled.
The first current-run paper scan then completed at 21:00:02 UTC: 38 scanned,
29 marked safe, 100 decisions evaluated, no entry, no quote or exit failure,
and no worker error. The shared worker continuity became current. The next
entry-readiness snapshot still had zero eligible entries: eight of 28
market-eligible candidates had an audit older than ten minutes, while the
remaining 20 lacked complete fresh 1m/5m history. There were zero comparable
closed autonomous paper trades. These are coverage and safety observations,
not evidence of strategy profit or loss.

### Forward-paper campaign boundary (3 November 2026)

The performance summary previously validated signal, cost, and Jupiter route
evidence but did not apply the recorded campaign start to closed positions.
It now requires an aware `opened_at` at or after `paper_campaign.started_at`
for the `forward_paper_only` campaign. Missing or invalid start times exclude
the trade from campaign metrics. The response reports the start time and the
closed-position count within the campaign, while retaining the all-time
closed-position count for reconciliation. Tests cover a complete trade opened
before the campaign, an in-campaign trade, and a missing campaign start.

The full Python suite, Ruff lint and format, mypy, JavaScript syntax check,
and package build passed after this change. After restarting the local
dashboard, the paired portfolio endpoint reported the campaign start as
2026-11-03T14:42:54.613864+00:00, one closed position during the campaign,
zero comparable closed autonomous trades, and one exclusion for missing
entry-route parity. Strategy Lab and operations health both reported live
trading disabled. The OpenAI research-note key was absent and its optional
paid-call toggle remained off. This remains a measurement repair, not
evidence that either strategy is profitable.

### OpenAI note configuration and two-cohort replay audit (3 November 2026)

The user added an OpenAI project key to the local `.env`. The loaded settings
recognized a plausible key without displaying its value. After restarting the
dashboard, the authenticated narrative endpoint reported `key_configured=true`,
`enabled=false`, a ten-attempt daily ceiling, and `trading_signal=false`.
Operations health reported live trading disabled and all four paper/data
workers running without a current worker error. No OpenAI request was sent.

The current paper ledger contains two recorded, score-independent research
cohorts with 40 distinct selected mints. Read-only replay verification found
authentic 1m history for ten mints and none for 30. There were 446 saved 1m
bars inside membership windows, with known first-fetch and first-observed
timestamps for all 446; only 123 were fetched within two minutes of their
close. Five replay candidates lacked eight usable confirmation candles, five
had no eligible trades after point-in-time entry gates, and 30 lacked 1m
history. The baseline produced zero comparable closed trades and zero censored
trades. These figures cannot establish net expectancy, drawdown, or a valid
original-versus-upgraded strategy ranking. The upgraded momentum and early
launch strategies remain unimplemented, pending adequate point-in-time data.

Observed public OHLCV throughput was 3.49 successful network calls per minute
over the previous hour, while 30 currently market-ready mints would require
at least 15 calls per minute for a two-minute refresh, before research or
higher timeframes. This is the current data-collection constraint; an OpenAI
research key does not increase candle capacity. The paper entry gate continues
to reject stale or incomplete candles.

The read-only replay population report also now counts a selected mint as
having 1m history only when at least one saved bar lies inside its recorded
selection window. Previously, an unrelated bar from before selection or after
expiry could inflate the reported coverage and create an unnecessary replay
attempt. A focused regression test includes a mint whose only candle predates
selection; it is correctly classified as missing research-window history.
The full Python suite, Ruff lint and format, mypy, JavaScript syntax check,
and package build passed after this reporting change.

### Alternative candle-feed feasibility (3 November 2026)

The public collector remained running with no worker error. In the latest
hour it completed about 3.52 successful network OHLCV calls per minute, versus
a minimum 14.5 calls per minute to refresh 29 market-ready mints every two
minutes. CoinGecko's paid pool-OHLCV WebSocket is an official possible
alternative, but its Basic plan cannot be assumed to cover this workload:
the published 0.1 credit per response and 100,000 monthly Basic credits imply
129,600 credits for 30 pools even at only one response per minute over a
30-day month. Active pools may emit faster. This estimate excludes REST use
and needs subscription-limit and real-message-rate validation. Sources:
[WebSocket channel](https://docs.coingecko.com/websocket/wssonchainohlcv),
[delivery credits](https://www.coingecko.com/en/api/data-delivery),
[plan credits](https://www.coingecko.com/en/api/pricing). No paid feed was
connected and live trading remains locked.

The operator selected free-only collection for this phase. The public feed
capacity shortfall is therefore a stated limit on forward paper evidence;
no subscription or paid feed integration is planned now. The configured
OpenAI research-note key is present, but `NARRATIVE_OPENAI_ENABLED=false`
and `NARRATIVE_OPENAI_DAILY_LIMIT=10`; the note feature has made no paid
calls in this verification. The key cannot improve OHLCV coverage.

### Mixed-fold replay reporting (3 November 2026)

Walk-forward evaluation previously could return `complete` when at least one
unseen fold lacked point-in-time market inputs but other folds yielded trades.
It now returns `partial_out_of_sample_coverage`, includes the status count for
every unseen fold, and keeps promotion locked. The dashboard only shows
performance metrics for a fully complete evaluation. A regression test builds
mixed sufficient and insufficient unseen folds and verifies the partial status.
Focused evaluation tests, Ruff lint and format, mypy, and JavaScript syntax
checks passed. The full Python suite and package build also passed. After a
dashboard restart, authenticated local endpoints returned HTTP 200. The note
settings reported `key_configured=true`, `enabled=false`, `daily_limit=10`,
and `trading_signal=false`. Operations reported `live_trading_enabled=false`,
SQLite quick-check `ok`, and all four workers running without a latest error.
The overall health state remains `degraded` solely because the measured public
OHLCV throughput is below candidate-pool demand. The restart reset the current
worker continuity streak; a long prospective paper run is still required.

The post-restart read-only verification snapshot had 30 market-ready mints and
an observed 3.5 successful public OHLCV calls per minute over 57 collector
cycles, versus a 15/minute minimum for two-minute refreshes. Among the latest
100 candidates, zero were currently entry-eligible: 13 had incomplete or stale
1m/5m evidence, one had a nonaligned signal, and 16 had missing or old audit
evidence. The campaign has one closed position excluded for missing entry-route
parity and **zero** comparable closed autonomous positions. Win rate, profit
factor, expectancy, and net return remain unreportable. The read-only report
confirmed both the code-level live lock and `LIVE_TRADING_ENABLED=false`.

### Free OHLCV quota recovery (3 November 2026)

The recent-cycle record contained 58 collector cycles: 51 completed four of
four OHLCV requests, one completed six of six, two hit a provider rate limit,
and four made zero requests during backoff. A six-call cycle was followed by
a rate limit on the next cycle. This sequence does not prove the exact provider
quota, but repeated hourly trials consume collection time while the paper
candidate pool already needs more than the observed free throughput.

The collector now keeps the four-call baseline for 24 hours after a quota hit.
It also extends an older persisted one-hour cooldown when the recent-cycle
history contains an actual rate-limited request. A regression test verifies
this recovery after restart and keeps the independent research lane active.
This is a data-collection reliability change, not an entry-rule change or a
claim that four calls per minute cover all candidates.

The full Python suite, Ruff lint/format, mypy, and package build passed after
the change. After the dashboard restart, the first completed market cycle
made four of four successful requests with no rate limit. The persisted
retry deadline was extended from the old one-hour value to about 24 hours
after the latest actual quota hit, while the next request budget remained
four. The authenticated operations endpoint reported all four workers
running without a latest error, SQLite quick-check `ok`, and live trading
disabled. OpenAI research stayed disabled with the key configured and a
10-attempt daily ceiling. The new paper worker run had not yet completed a
decision cycle, so post-restart continuity was still `unverified` at this
observation; process health alone does not establish a 30-day paper run.

### Audit freshness for continuously observed paper candidates (3 November 2026)

Both pinned paper candidates had recent market snapshots (18 seconds old)
and continuous 1m collection. Their latest audit observations were about 991
and 586 seconds old; the entry gate accepts only audits from the last ten
minutes. The paper scanner's prior cycle re-audited eight other market-priority
watchlist candidates out of 30 market-ready mints. Its age-only priority
could let a pinned candidate's audit expire before its 5m candle history
matured.

The scanner now puts at most two currently pinned paper candidates first in
its unchanged eight-slot watchlist re-audit budget, but only when their latest
pool snapshot is fresh and the existing market-priority callback passes. The
pin itself grants no safety or entry verdict. The test covers both a stale
market snapshot (no special priority) and an active pin competing with eight
older priority candidates. The full Python suite, Ruff lint/format, and mypy
passed after this scheduling change. The runtime check below confirms the
active focus was refreshed under the free RPC workload.

The restarted local paper worker completed its first cycle at 21:38:33 UTC
without an error. It scanned 38 candidates, including eight priority
watchlist re-audits. Both active focus mints then had audit observations
under 30 seconds old, compared with roughly 991 and 586 seconds before the
cycle. Worker continuity returned to `current`; the remaining operations
degradation was public candle capacity. A subsequent read-only verification
found nine current candidates with missing or older-than-ten-minute audits,
17 limited by candle history, zero entry-eligible, and zero comparable closed
autonomous paper trades. Measured public throughput was 3.54 successful
calls/minute versus a 14/minute minimum for the then-current market-ready
pool. The refreshed focus audits improve evaluation coverage, but no paper
performance conclusion follows yet.

### Focused candle-source diagnostic (3 November 2026)

The two current paper-focus mints had 59 and 58 authentic 1m bars within the
last hour, yielding 11 and 10 complete verified 5m bars. Neither had a direct
provider 5m bar. The first mint's 1m and 5m signals were both `ready` but
unconfirmed (scores 25 and 55). The second mint's 5m signal was `ready` and
confirmed (score 80), while its 1m signal was stale at the read-only snapshot.
The latest saved paper decision for that mint had a ready 1m signal but
rejected a `conflicting_prices` 15m integrity result. One of 28 comparable
direct-versus-minute-built 15m bars differed by 1.0935% at the close; the
direct bar was first fetched 71 seconds after the minute-built value was first
observed. The reason for that provider difference is not established, so the
existing >1% integrity gate remains in force.

Direct 5m requests would consume scarce public quota without addressing either
observed entry blocker. No extra 5m calls or indicator threshold changes were
made. The current evidence supports continued prospective collection and
point-in-time replay, not a profitable-trade claim.

An authenticated local operations check after the diagnostic showed all four
workers running without latest errors, paper continuity `current`, and live
trading disabled. The only reported degradation was public OHLCV capacity
below wider candidate demand. This is a current-process observation, not an
unattended soak result.

### Matched route and token-account cost check (3 November 2026)

A new read-only verification snapshot still had no comparable closed
autonomous paper trades, so win rate, profit factor, net return, expectancy,
and maximum drawdown cannot be inferred. The active entry cohort had 28
market-gate-eligible candidates: 11 limited by missing or stale candle
evidence, two with nonaligned signals, and 15 with missing or stale audits.
None was entry-eligible. The score-independent replay population contained
40 recorded mints; 11 had 1m history and 29 did not. Of 532 saved research
1m bars, 147 were fetched within two minutes of closing. Five sampled
decision follow-ups yielded three timely observations, one late-backfilled
observation, and one missing entry bar on the 5m/15m paths. These are
observability counts, not trade returns.

The actual CLI `buy` dry run for
`HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump` at 0.005 SOL and 100 bps
slippage passed the token audit, built a Jupiter `v2-build` Metis route with
zero platform fee, and simulated the transaction (99,762 compute units).
The result reported `submitted=false` and no signature. This confirms a
working candidate route and dry-run path at that instant; it created no
paper position and sent no transaction.

A separate **read-only** matched `v2-build` buy-then-reverse quote for the
same mint and size returned 40,604,156 token atomic units from 5,000,000
lamports and 4,994,871 lamports on an immediate reverse quote: a 5,129
lamport, approximately 10 bps quoted round-trip difference. Both route
responses listed zero platform-fee bps. This is a simultaneous quoted
scenario, not an attainable fill or profit estimate; it excludes modeled
adverse slippage, network and priority fees, ATA reserve, latency, and price
movement. Earlier quote-only `/swap/v2/order` buy/reverse checks showed
approximately 96 and 247 bps loss for two mints with 10 bps platform fees
on each leg. Those routes differ from the paper campaign's fee-free build
route and must not be used as its execution-cost estimate.

The previous paper execution model reserved a fixed 2,039,280 lamports on
every buy and subtracted that amount from rent-inclusive cash P&L. The live
RPC's `getMinimumBalanceForRentExemption` returned 1,488,440 lamports for
165 bytes and 1,513,840 for 170 bytes at this observation. The focused mint
is Token-2022 and reports `metadataPointer` and `tokenMetadata` mint
extensions. Its exact required ATA size and rent were **not** verified from
the actual simulated output account. Existing ATAs would need no new
reserve; a previously simulated paper ATA is also not tracked across repeat
buys. Solana documents that the token-account balance is refundable when an
empty account is closed, and that Token-2022 account extensions can increase
its size: [create token account](https://solana.com/docs/tokens/basics/create-token-account),
[close token account](https://solana.com/docs/tokens/basics/close-account).
The dashboard's rent-inclusive P&L and full-rent-reclaim sensitivity were
therefore illustrative accounting scenarios. The fixed replay planning cost
and refund treatment still need calibration before profitability can be judged.

The forward paper buy path now asks the signed dry-run simulation to return
the output ATA's post-simulation lamports. It verifies the account program,
checks whether the ATA already existed on-chain, and records zero new reserve
for a mint previously purchased in the paper ledger. An absent or malformed
simulation account prevents a forward entry; the measured full cash debit
must remain within the paper risk budget. A second dry run on the focused
Token-2022 mint returned a 1,513,840-lamport output account reserve, a
`v2-build` route, 99,761 simulated compute units, `submitted=false`, and no
signature. This verifies the exact reserve for that simulation at that time.
It did not open a paper position or send a transaction. The full Python test
suite passed, including simulated new/existing/malformed ATA cases and
repeat-mint reserve checks. Ruff formatting/lint, mypy, and package build
passed. The local dashboard was restarted with this source change. Its
authenticated health endpoint reported all four workers running without
latest errors, SQLite quick-check `ok`, and live trading disabled. Paper
continuity was temporarily `unverified` immediately after restart, then
returned to `current` after new completed worker cycles. The only reported
operational degradation was public candle capacity below wider candidate
demand. This verifies current-process health, not unattended durability.

### Replay execution-cost attribution (3 November 2026)

The replay engine previously multiplied sale proceeds by one combined
execution-cost percentage. That percentage included a fixed token-account
rent reserve and fixed network/priority fees. For a large winner it therefore
charged more than the modeled fixed cash amount, and for a loser it charged
less. The 150% cost-stress path also inflated refundable rent as though it
were an execution fee. These were model defects; prior replay return figures
using that formula should not be compared with new ones without relabeling.

The replay now applies modeled adverse slippage to gross proceeds and deducts
the fixed fee and rent components relative to the initial swap size. Rent is
reserved on the first simulated purchase of a mint, not every repeat trade;
unseen walk-forward folds retain that simulated account state. A separately
labeled full-rent-recovery sensitivity uses the **same cash-based entries,
exits, and circuit-breaker path** and adds back the first reserve only. The
150% stress increases nonrefundable execution friction and leaves the rent
reserve unchanged. The primary result remains rent-inclusive cash P&L.

At the present settings, the planning model estimates 7,905,720 lamports of
swap input, 2,778 bps first-trade cash cost, of which 2,718 bps are fixed
outlays and 2,579 bps are the configured refundable ATA reserve. These are
planning assumptions, not measured fill costs; the dry-run output account for
one focused Token-2022 mint measured 1,513,840 lamports instead of the
2,039,280-lamport fixed planning reserve. The replay still lacks executable
historical spreads, an actual account-close transaction, and enough
point-in-time candle coverage to establish performance. The current
read-only verifier reports zero comparable closed autonomous paper trades;
no win rate, profit factor, expectancy, or net return is claimed.

Focused replay/evaluation tests cover fixed-cost attribution on a winning
trade, one-time rent reservation, a previously reserved ATA, the hypothetical
refund, and 150% stress that leaves rent unchanged. The full Python suite,
Ruff lint and format checks, mypy, and the package build passed. A read-only
verifier run returned `LIVE_TRADING_ENABLED=false`, code-level live lock
`true`, and the cost split above. After a controlled dashboard restart, the
authenticated Strategy Lab endpoint returned the split cost fields for a
current research-cohort mint with zero eligible replay trades. The operations
endpoint then showed all four workers running without latest errors, paper
continuity `current`, and live trading disabled. Its only degradation was
free public OHLCV capacity below wider candidate demand. No order or paper
position was created by these verification calls.

The read-only verification still reported the code-level live lock and
`LIVE_TRADING_ENABLED=false`. No live or paper order was placed during this
check. The public OHLCV lane remains free-only per the operator's decision.

## 2026-11-03: free-only capacity and single-owner paper worker

The operator chose free-only market data. Official API documentation shows
CoinGecko's OHLCV WebSocket requires a Pro key, Birdeye's free Standard tier
has limited data and 30,000 monthly compute units, and DEX Screener's public
API exposes pair snapshots without an OHLCV endpoint. The existing bounded
GeckoTerminal public collector therefore remains the live candle source. No
paid feed or OpenAI research was enabled.

A fresh read-only report over the preceding hour recorded 216 successful
network OHLCV calls across 54 collector cycles, or 3.62 calls/minute. Thirty
market-ready mints would need at least 15 calls/minute for a 120-second
refresh, before research and higher timeframes. Among the latest 100
discovery candidates, 22 reached candle evaluation and all 22 lacked
complete current evidence; nine others needed a fresh on-chain audit. There
were zero entry-eligible candidates, zero comparable closed autonomous paper
trades, and no defensible strategy-performance estimate. These counts are
time-specific observations, not provider guarantees.

Dashboard startup now takes an OS-level exclusive lock beside the paper
journal before starting workers. A second dashboard using the same journal
fails closed, including when it runs in another process. Focused unit and
dashboard tests passed, including a separate-process contention and release
check. The full Python test suite passed, Ruff lint and format passed,
`mypy src` passed for 37 source files, and the package build succeeded.
Repository-wide `mypy` still reports 113 existing test annotation/type errors
across ten test files; this change does not claim that check passes.

After the first controlled local dashboard restart, an independent process
was denied the same journal and all four workers completed a cycle under
their new run IDs without latest errors. The final startup-cleanup change
was then loaded by one more restart; its lock was confirmed from a separate
process, and all four workers again completed under their final run IDs
without latest errors. Both restarts reset the measured continuous
observation streak. The full Python suite passed again after the final code
change. An additional
startup-failure regression test confirmed partially started paper workers
are stopped before the lock is released. `LIVE_TRADING_ENABLED` and
`NARRATIVE_OPENAI_ENABLED` both remained false.

## 2026-11-03: free direct 5m repair for pinned paper candidates

The free collector's two pinned candidates both had current, complete 1m
signals but blocked 5m signals. One had only seven complete 5m bars; the
other had a missing 5m interval inside an otherwise active series. A
read-only direct 5m GeckoTerminal request for the gap case returned 199
authentic pool bars, including the missing interval. On a SQLite online
backup of the live journal, inserting those bars changed that candidate's
5m integrity status from `gap` to `ready`. The live journal was not modified
by the diagnostic probe. This establishes that the free direct-pool endpoint
can repair this specific data gap; it does not establish that the token's
entry signal is confirmed or profitable.

The normal collector now schedules one direct 5m request for a pinned
candidate only when its 1m data are ready and its 5m status is `gap`,
`mixed_provenance`, or `stale`. It reuses the same public request budget,
preserves both pinned 1m requests and any scheduled research pin, and avoids
repeating the same direct 5m fetch within five minutes. The first live cycle
after deployment showed all four free request slots occupied by the two
focus mints, one research sample, and a decision follow-up. The scheduler
therefore now permits one follow-up 1m request to be deferred for a direct
5m repair, with a global limit of one such repair per five minutes. The
follow-up subject remains selected and its missed request remains visible
in the worker metrics. Each conditional
selection is saved with its mint, pool, reason, attempt/result, returned bar
count, displaced 1m request, and post-fetch status. Existing candle integrity, point-in-time
observation, and paper entry gates remain in force. The offline scheduler
test verifies four-call budgeting, research preservation, bounded follow-up
deferral, two focus repairs, and cooldown. Full tests, Ruff, source mypy, and package build were run for
this change; deployment and runtime results are recorded below.

The first package-build attempt failed because Hatch's default source
selection tried to read the dashboard's active `.workers.lock` file. The
source-distribution configuration now uses an explicit allowlist, and the
ignore rules cover local environment variants and worker locks. A rebuilt
source archive and wheel passed member-name inspection: neither contains a
private `.env`, SQLite journal, worker lock, keypair, backup, or cache file.
The safe `.env.example`, package source, and dashboard static assets remain
present. This is a packaging boundary check, not a scan of all historical
archives or the operator's filesystem. `pip-audit` against the current local
project reported no known vulnerabilities at the time of this check.

After the final scheduler restart, a live paper collector cycle used four
successful free OHLCV requests. It fetched 12 direct 5m bars for one pinned
candidate with a recorded `gap`, deferred the selected follow-up's 1m call,
and kept the two pinned 1m calls plus one research 1m call. The post-fetch
5m result was `conflicting_prices`: direct and minute-derived OHLC disagreed
by more than the existing 1% integrity limit on an overlapping bar. The
entry gate stayed closed. This is a useful safety result, not a successful
repair or a strategy-performance result. The second pinned candidate had
previously changed from `gap` to `ready` in the isolated journal backup;
its live 5m series later became ready without a direct repair, while its
momentum signal remained unconfirmed. A fresh read-only report showed 20
candle-limited candidates, one signal mismatch, zero entry-eligible tokens,
and zero comparable closed autonomous trades. The measured free feed was
3.44 successful calls/minute against at least 15.5/minute for the wider
31-mint market-ready set. The collector now avoids spending another direct
5m request while a known >1% overlap conflict remains inside the recent
eight-bar signal window; a regression test covers this masked-conflict case.

The conflict-aware scheduler was loaded by a final local dashboard restart.
The new run completed fresh paper decision, exit-monitor, market-collector,
and narrative-collector cycles with no latest worker error. An independent
process was denied the paper journal lock, the unauthenticated operations
endpoint returned HTTP 401, `LIVE_TRADING_ENABLED=false`, the code-level
live lock remained true, and OpenAI research remained disabled. The restart
reset the continuity streak. The full Python suite, Ruff lint/format,
`mypy src`, package build, archive member inspection, and project dependency
audit passed after this code change; repository-wide mypy still has the
previously reported test-file typing backlog. There are still zero
comparable closed autonomous paper trades, so performance is unmeasured.

## 2026-11-04: closed-candle revision integrity

The free-only data path remains selected. A saved pinned pool showed more than
10% disagreement on one recent 5m OHLC overlap between the provider's direct
bar and the complete 1m-derived bar. Its entry remained blocked; neither source
was silently chosen as ground truth. Separately, review of the SQLite candle
upsert found that a changed provider response for the same closed interval
would retain the first OHLCV values without recording that a revision occurred.
That made a revised 1m bar, or a higher frame derived from it, appear trustworthy.

The journal now records `revision_detected_at` on the first changed closed-candle
response while preserving the initially observed values and their timestamp.
Equivalent numeric formatting does not mark a revision. Both direct and derived
timeframes carry the earliest known revision, and recent revised bars produce
`revised_values`, which blocks new paper entries. Point-in-time replay can still
evaluate a decision made before the revision was observed. The provenance policy
is version v17; earlier saved backtests are excluded from current-policy
comparisons. Revisions before the new column existed cannot be reconstructed.

A SQLite online backup of the active journal was migrated separately and retained
all 49,997 saved candle rows. Focused tests covered changed OHLC, changed volume,
numeric-format equivalence, first-value preservation, point-in-time behavior,
derived 5m propagation, direct/derived overlap, and the higher-timeframe entry
veto. The full Python suite passed; Ruff lint and format, `mypy src`, and
source/wheel builds passed. The built archives contained no private environment,
journal, worker lock, keypair, backup, or cache member names. The project
dependency audit passed in the preceding check with no dependency changes since.

After a verified SQLite online backup, the local dashboard was restarted to
load the new guard. This reset the continuous-run streak. All four workers
completed a fresh cycle with no latest error. Newly observed provider changes
marked 54 saved 1m bars as revised during the first minutes of the run; three
currently evaluated candidates were blocked by `revised_values` in the entry
report. The same report showed zero entry-eligible candidates, and no comparable
closed autonomous paper trades exist yet. Unauthenticated operations access
returned HTTP 401; the dashboard remained loopback-only;
`LIVE_TRADING_ENABLED=false`, the code-level live lock remained true, and
paid OpenAI research stayed disabled. The paper process is still running.

## 2026-11-04: Windows paper startup under the free-only policy

The local worker previously depended on a manually opened PowerShell process.
An interactive, limited-privilege Windows Task Scheduler entry is now registered
for the current user's next sign-in. It starts the same loopback dashboard from
the project directory, ignores a second task instance, and retries an unexpected
failure after one minute. The task was not started during registration, so the
current paper process and its SQLite worker lock were not disturbed.

The task command contains no API key, wallet key, or pairing code. The new
`dashboard --free-only` startup guard checks the environment on *every* launch
and refuses to start if live mode or paid OpenAI narrative research is enabled;
the separate code-level live lock remains. Tests cover safe startup and both
rejection cases. The installer is idempotent and refuses an unrelated task
with the same name. Task Scheduler's CIM view shortened the account name and
looked ambiguous, so verification used the exported task XML: both its
principal SID and logon trigger match the current Windows account. Verified
metadata: interactive logon, limited privilege, one action, one trigger,
`IgnoreNew`, 999 allowed restarts, one-minute restart interval, and free-only
arguments. The installer was rerun successfully without changing that task.
After adding the CLI guard, Ruff lint and format, `mypy src`, and a fresh source
and wheel build passed; the source archive includes the Windows installer and
neither archive includes local secrets or state files. The full Python regression
suite passed for the final command-line change. `pip-audit` reported no known
dependency vulnerabilities; it cannot audit this unpublished local project as
a PyPI package, so source review and tests remain necessary.

The next actual sign-in has not occurred during this check; automatic launch,
console visibility for the pairing code, and restart-on-failure have not yet
been observed end to end. The task cannot run after sign-out or while this
computer is powered off, and those breaks reset continuous-run evidence.

## 2026-11-04: forward paper route evidence audit

The journal has one closed paper position, opened at 2026-11-03 14:45 UTC and
closed at 15:29 UTC. Its saved entry execution has cost fields but no
`quote_source`; the forward performance report excludes it as
`entry_route_parity_missing`. No router source was inferred or backfilled, so
the count of comparable autonomous closed trades remains zero.

The current paper entry path requires a fee-free `v2-build` router quote and a
measured token-account reserve in `forward_paper_only` mode, then saves
`quote_source` and `quote_fee_bps` with both the position and buy fill. Existing
tests cover the rejection paths; a new isolated positive test opened an
autonomous forward paper position with a simulated built-router report and
confirmed both records retained the route source and zero fee. That verifies
the current persistence path. It does not retroactively validate the earlier
closed trade or prove that a future live data cycle will produce one.
The full Python regression suite, Ruff lint, and Ruff format check passed again
after this test was added.

## 2026-11-04: free-feed candle revision diagnosis

The public GeckoTerminal collector was still running and completing about
3.5 successful network OHLCV requests per minute. Refreshing each of the 30
market-ready mints every two minutes would need at least 15 requests per minute,
before research and higher-timeframe calls. The collector already prioritizes
two active mints; both had recently revised candles and were correctly denied
a new paper entry. The latest 100 saved decisions had no frame-ready entry.

A bounded read-only repeat fetch for one active pool returned 96 previously
stored closed 1m bars. Fifteen differed from their first recorded values;
13 had a changed close, and the largest observed OHLC difference was about
0.53%. The revision gate is therefore responding to price changes, not merely
numeric formatting or revised volume. The journal later showed 94 detected
1m revisions across seven pools, with a median 2,081 seconds from first local
observation to first detected revision and 3,303.5 seconds after bar close.
Those delays measure detection on subsequent requests, not when the provider
actually changed the data. Bars fetched only once and earlier revisions are
unknown, so these figures are not an error rate.

The read-only verification report now exposes that bounded revision summary,
with a test for a changed close and an unchanged neighboring candle. This is
diagnosis only: it does not relax entry safety or change the autonomous
strategy. Helius documents standard WebSocket access on its free plan, but
Solana `logsSubscribe` supplies signatures and logs rather than OHLCV; a
second on-chain feed would need swap decoding and many transaction fetches.
No paid candle subscription or OpenAI request was enabled. The forward paper
performance report still has zero comparable closed autonomous trades and
does not support a profitability claim.

## 2026-11-04: revision-aware focus rotation

The collector's two intensive 1m focus slots could be held by mints whose
recent confirmation bars had known provider revisions. A market-eligible mint
with a recent revised 1m, 5m, 15m, or 1h bar is now skipped for focus selection;
the stored focus state lists those exclusions. This affects only which pools
receive scarce public candle requests. The paper entry gate still checks all
frames, and the fixed research cohort remains unchanged. A narrow indexed
presence check avoids rebuilding four full histories for candidates with no
known recent revision. On the current 30-candidate journal, the scan found
seven candidates with a raw revision in the potential lookback and took about
1.3 seconds, including deeper checks for those seven.

An isolated collector test proved that a previously pinned revised mint was
replaced by another market-eligible mint while a fixed 20-mint research cohort
was preserved. The full regression suite, Ruff lint and format, source mypy,
and source/wheel build passed. Before reloading the local dashboard, a new
SQLite backup passed SHA-256 verification and `PRAGMA integrity_check`.

After the controlled restart, the focus state recorded seven revision-based
exclusions and selected two other mints. The first completed new collector
cycle scheduled both focus mints, received four successful OHLCV responses,
and recorded no provider or worker error. All four workers resumed; the
dashboard remains loopback-only. `LIVE_TRADING_ENABLED=false`, the code-level
live execution lock is true, and paid OpenAI research remains disabled. The
restart resets the uninterrupted forward-run streak. The newly focused
tokens still need sufficient clean observations and actual costed closed
paper trades before performance can be assessed.

## 2026-11-04: post-rotation entry and replay evidence

The live read-only check found 30 market-eligible candidates but no
entry-eligible candidate. The focused mints changed as fresh market and
revision evidence arrived; one candidate's 1h gap crossed from 2026-11-02
23:00 UTC to 2026-11-03 13:00 UTC, so it was an actual missing trading period,
not a timestamp-order bug. Another candidate's recent eight 1h bars mixed six
direct provider bars with two complete minute-derived bars. The immediate
1m and 5m signals also did not confirm together, so omitting the higher-frame
veto would not have produced a valid entry in that observation.

Across the stored direct/minute-derived overlaps, 33 of 832 15m bars and two
of 141 1h bars differed in OHLC by more than 1%; the largest differences were
14.2853% and 1.9777%, respectively. This supports retaining the existing
mixed-source and conflicting-price gates while data provenance is unresolved.
The two focused pools each had three exact 1h overlaps, but those narrow local
matches do not prove the provider's bars always agree.

The fixed, score-independent replay population had 60 selected mints, only 15
with any observed 1m history inside their recorded membership windows. The
market-eligible follow-up replay had seven selection records, two censored
trades, and zero closed trades. Recent decision price-path diagnostics were
also sparse: among 100 decisions at the five-minute horizon, 91 lacked an
entry bar and only three had a timely observed path; at the 15-minute horizon,
none had a timely observed path. These price paths are not executable trade
returns. There is still no comparable closed autonomous paper trade, so no
win rate, profit factor, expectancy, or strategy improvement can be inferred.
The next evidence milestone is reliable forward OHLCV coverage and costed
closed paper trades, with live execution locked.

## 2026-11-04: measured revision size on the free feed

An additive journal migration now keeps the largest observed OHLC price
discrepancy (basis points) and volume discrepancy (USD) for each saved pool
bar relative to its first stored values. The existing first values, first
observation timestamp, and first revision detection time remain intact.
The read-only revision report exposes the number of bars with a measured
discrepancy and separates price from volume changes. Earlier detected
revisions have no size measurement until the bar is fetched again. No
provider request or trading decision was added by this telemetry.

Tests covered a changed high, a volume-only change, numeric formatting, and
retaining the maximum discrepancy after a provider reversion. The full Python
suite, Ruff lint and format, source mypy, and source/wheel build passed. A
raw SQLite backup taken while the dashboard was stopped passed
`PRAGMA integrity_check` before the additive migration. The dashboard then
restarted in free-only mode and completed a collector cycle with four
successful OHLCV requests and no worker error.

At the first post-migration check, only ten revised bars had a measurable
post-migration discrepancy. Nine had a price change; the median of their
largest observed OHLC changes was 6.42 basis points, and the largest was
284.56 basis points. Nine also had a volume change. This tiny, selected
sample does not establish a provider error distribution or justify changing
the revision veto. `LIVE_TRADING_ENABLED=false`, the code-level live lock
remains true, and paid OpenAI research remains disabled. The restart again
resets the uninterrupted forward-run streak.

### First follow-up on revision measurements

After the next completed collector cycle, 153 bars had a detected revision,
but only 11 had a price or volume discrepancy measured by the new fields.
Ten of those 11 had a price discrepancy, with a median maximum OHLC change
of 5.89 basis points and a largest change of 284.56 basis points. Nine had a
volume discrepancy; the largest observed absolute volume change was
$1,658.88. At that checkpoint, only one revision had first been detected
after the metric rollout; the other measurements came from revisiting bars
whose revision flags predated it. Treat this as diagnostic evidence, not a
prospective error distribution or a basis for a revision tolerance. The
collector completed four OHLCV calls in the cycle without an error, and the
portfolio still had no comparable closed autonomous paper trades.

### Free-only second-source capacity check

Official Birdeye documentation currently lists a Standard free plan with
30,000 compute units per month and 1 request per second, and its OHLCV V3
endpoint is advertised for Standard and above. CoinGecko lists 10,000 calls
per month on its free Demo API. Two focused pools refreshed once per minute
would require 86,400 calls in a 30-day month, before research or retries.
Even an unrealistically cheap one-compute-unit Birdeye request would exceed
its monthly free allowance. These services may support occasional
independent checks if a free key is later supplied, but neither documented
free allowance supports replacing the continuous public candle collector.
No external account was created and no paid feed was enabled.

Sources: [Birdeye pricing](https://docs.birdeye.so/docs/pricing),
[Birdeye OHLCV plan availability](https://birdeye.so/data-api/blog/detail/token-investigation-dashboard-solana-birdeye-data),
and [CoinGecko API pricing](https://www.coingecko.com/en/api/pricing).

### Free-only source decision and next forward checkpoint

At 2026-11-04 00:55 WAT, the running collector had completed 216 successful
network OHLCV requests across 55 cycles in the preceding 3,487 seconds,
or 3.72 successful requests per minute. Four attempts did not succeed and
four responses were local cache hits; no cycle hit a provider rate limit.
Refreshing all 30 currently market-eligible mints every two minutes would
require at least 15 requests per minute before research and higher frames.
The live readiness check found no eligible entry: 19 of the 30 were stopped
by stale or missing on-chain audit evidence; of the 11 candle-evaluated
subjects, ten lacked complete fresh candle evidence and one had unaligned
signals. The 10-minute audit freshness limit and candle data gates remain in
force. This is a capacity and evidence limitation, not evidence that a
strategy is unprofitable.

The free Helius plan includes standard Solana WebSockets, but its standard
`logsSubscribe` stream supplies transaction logs, not pool OHLCV. Building
trusted candles from it would require a separate swap decoder, pool-specific
accounting, continuity checks, and a historical backfill. Helius lists the
enhanced `transactionSubscribe` method above the Free tier. Therefore the
existing free RPC is useful for audits, but it is not a ready independent
candle feed. This is an inference from the documented stream capabilities;
no new WebSocket collector has been implemented or validated.

The revision journal has now detected 158 changed bars across 17 pools, of
which only 16 have measured post-migration magnitude. Fifteen have measured
price differences (median of each bar's maximum OHLC difference 5.35 basis
points; largest 284.56) and 11 have volume differences (largest $1,658.88).
Most measured bars were already flagged before magnitude logging began.
This selected sample cannot justify relaxing the correction veto. There are
still zero comparable closed autonomous paper trades, so all performance
metrics remain unavailable. The paper dashboard and collector are running,
with the code-level live execution lock and `LIVE_TRADING_ENABLED=false`.

Sources: [Helius free plan and feature table](https://www.helius.dev/pricing),
[Helius standard and enhanced WebSocket distinction](https://www.helius.dev/blog/laserstream-websockets).

### Closed-candle request timing fix

The free OHLCV client previously captured its `now` timestamp before waiting
for its public request slot and receiving the HTTP response. A 1m bar closing
during that wait was incorrectly discarded until a later poll, even though it
was closed when the payload arrived. Parsing now uses the response-time clock
for live requests while preserving an explicit caller-supplied cutoff for
historical replay. Open bars are still excluded, and entry freshness and
revision gates are unchanged. A mock transport regression test covers both
the live close-during-request case and the historical cutoff case.

The targeted OHLCV tests (26), the full Python suite, Ruff lint and format,
source mypy, and source/wheel build passed. The dashboard was stopped before
the data-preserving restart. Its SQLite backup at
`state-backups/paper-before-ohlcv-close-fix-20261104T000416Z.sqlite3` passed
`PRAGMA integrity_check` (`ok`). After restart in `--free-only` mode, the
local protected operations endpoint returned HTTP 401 without pairing, and
the paper, exit, market, and narrative workers all reported new running IDs
without a recorded worker error. The market collector's first full cycle after
the restart completed at 00:06:47 UTC: three of four OHLCV requests succeeded,
one returned `provider_unavailable`, and none hit a rate limit. The worker
remained running. This provider failure is a data-availability warning, not
an approved signal or a reason to bypass freshness gates.
The next cycle, completed at 00:08:36 UTC, also had three of four successful
OHLCV requests and one `provider_unavailable`, affecting a different mint.
The collector placed the affected pool/frame requests on retry cooldown and
continued. This is a current intermittent upstream availability problem;
the effect of the timestamp fix on timely coverage cannot be measured from
these two cycles alone.
`NARRATIVE_OPENAI_ENABLED=false`,
`LIVE_TRADING_ENABLED=false`, and the code-level live execution lock remains
true. This timing correction has not yet been shown to improve eligible-entry
or closed-trade counts; a new uninterrupted forward observation window is
needed. The restart resets the previous forward-run streak.

### Public trade-feed feasibility check (free-only; 2026-11-04)

The user chose to keep data sources free-only. No paid candle feed, trial, or
subscription was enabled. A read-only check found 30 market-eligible mints but
no entry-eligible candidate. Of 20 candle-evaluated mints, all had stale 1m
evidence; another ten needed fresh audit evidence. The direct public OHLCV
collector was delivering about 3.49 successful requests per minute over the
observed hour, while even a two-minute refresh for 30 pools requires at least
15 per minute before higher timeframes or retries. Recently fetched direct
1m candles for focused pools were still over 288 seconds old despite active
DEX trades. These are observed throughput and freshness limits, not a measured
strategy outcome. Stale and missing evidence continues to block entries.

GeckoTerminal's documented free pool-trades endpoint returns up to 300 recent
trades over the past 24 hours, with a fluctuating public request limit around
ten calls per minute and a cache interval. In a bounded, read-only comparison,
a focused pool's newest trade was 21–64 seconds old. Across 75–78 overlapping
completed 1m bars, reconstructed trade USD volume matched the saved direct
OHLCV to floating-point rounding. However, the maximum reconstructed OHLC
price difference was 163.13 basis points; even some single-trade bars differed
by roughly 53 basis points. Those sample bars were not marked as later OHLCV
revisions. Therefore the volume agreement does **not** establish price
equivalence, candle ordering, or safe use for entry decisions. The public
trade page also has no cursor, so 300 prints may not cover a full requested
window for a busy pool. A third exploratory public request received HTTP 429.
The read-only comparison script now refuses to run while the collector is
active, protecting their shared free quota.

The free Solana RPC was checked as a possible independent source. One
`getTransaction` result contained the pool's token and WSOL vault balance
changes, so trade decoding is technically possible. But bounded signature
counts were 237 in an hour on one pool and at least 1,000 in 192 seconds on
another. Fetching every transaction at that rate would consume the free
Helius monthly credit allowance rapidly. A standard WebSocket
`blockSubscribe` attempt returned JSON-RPC `-32601 Method not found` on this
endpoint. This does not rule out a more selective free decoder, but it does
not provide a validated continuous OHLCV replacement now.

A strict, research-only parser and tests were added in
`src/memecoin_bot/trade_feed.py` and `tests/test_trade_feed.py`. It rejects
malformed pages, mixed token sides, nonpositive prices, missing anomaly flags,
and duplicate or misordered trades. It exposes a one-sided lower-boundary
check so callers can reject a page truncated before a requested window; this
does not prove that every recent swap was delivered. It
aggregates only real prints into completed minute buckets. It
is deliberately **not imported by the collector, strategy, or paper worker**.
The running bot's strategy and entry gates are unchanged. Before any use in
paper signals, a controlled comparison must explain the price discrepancy,
verify same-second trade ordering and coverage across pool types, and show
that the public quota can support the chosen focus set without starving the
existing collector.

Verification: nine focused parser tests and the full Python test suite passed.
Ruff lint and format, source plus new-test mypy, and source/wheel build passed.
Whole-suite mypy remains red with 122 pre-existing test typing errors; the new
file adds none. At the final read-only process check, the market, paper entry,
paper exit, and narrative workers all reported `running` with no last error.
Live execution remains hard-locked,
`LIVE_TRADING_ENABLED=false`, and OpenAI narrative calls remain disabled.
There are still no comparable closed autonomous paper trades, so no net
expectancy or profitability conclusion is possible.

A second read-only readiness check near 00:25 UTC showed that the two currently
pinned pools had received 1m rows only about 50 seconds after their closes;
their most recent fetches were 17 and 46 seconds old. Thus the earlier
288-second lag is not a constant provider property. Both pinned 1m entry
windows still had gaps, and their 5m signals were stale. Across the 30
market-eligible mints, 18 lacked a current pre-candle prerequisite; all 12
candle-evaluated mints were data-limited and none was entry-eligible. One
historical paper position is marked closed in the database, but it does not
form a comparable closed *autonomous forward* cohort. The collector therefore
remains in observation mode without weakening any data or risk gates.

Sources: [GeckoTerminal public API documentation](https://api.geckoterminal.com/docs/index.html),
[GeckoTerminal API schema](https://api.geckoterminal.com/docs/v2/swagger.json),
[Solana `getTransaction`](https://solana.com/docs/rpc/http/gettransaction),
[Solana `blockSubscribe`](https://solana.com/docs/rpc/websocket/blocksubscribe),
and [Helius pricing](https://www.helius.dev/pricing).

### Free-only forward checkpoint (00:30 UTC, 4 November 2026)

The read-only strategy report now has 60 selected research mints, 24 with any
1m bar inside their recorded membership window and 36 without one. There are
still zero baseline comparable closed replay trades and zero comparable closed
autonomous forward paper trades. In the latest 100 saved decisions, 19 passed
the market gate, one had the required 1m/5m frames ready, and none had both
required frames confirm an entry. This is a data and signal observation, not
evidence that the baseline loses or wins. The upgraded momentum and
early-launch strategies remain unimplemented pending usable baseline evidence.

The two currently pinned candidates had recently fetched 1m bars but lacked
eight contiguous trade minutes; their last verified 5m bars had latest closes
roughly 14 and 24 minutes old, respectively, and neither had a direct 5m bar
stored. The existing direct-5m repair waits for a ready 1m signal before
spending scarce public requests, so fetching 5m alone would not make either
candidate entry-eligible. This supports keeping the free quota on fresh 1m
observation and independent research rather than adding speculative indicator
rules. The paper entry, exit, market, and narrative workers remained running
with no recorded last error; live execution and OpenAI calls remained disabled.

The research trade parser's earlier `covers_window` method was renamed to
`has_lower_boundary`. An older trade can rule out one kind of 300-print
truncation, but a cached public page cannot prove every recent swap is present.
The parser remains disconnected from the strategy and paper worker.

### Automated paper entry commit-time revalidation (00:45 UTC, 4 November 2026)

Code review found that an automated candidate's market and candle signal was
confirmed before the route preview, but the final SQLite entry guard checked
portfolio risk only. An audit could become blocked, a provider candle could be
revised, or a decision could age out while the quote was built. The paper
service now requires the original decision and market evaluation to be at most
120 seconds old both before the preview and at commit. Inside the same
transaction that records the paper fill, it reloads the candidate from the
latest discovery population, reruns the existing market scorer and all four
timeframe candle checks, and rejects the fill unless the signal is still
eligible. The refreshed score, timestamps, latest candle opens, and frame
signals are saved with a successful entry's execution evidence. A failed
recheck produces the stable action `decision_changed_before_entry`; the pasted
call view says no position was opened. Manual paper entries retain their
explicit manual classification and bypass only the automated strategy signal,
while their existing execution and portfolio checks still apply.

Regression tests used an initially eligible, real-candle fixture, then changed
the saved on-chain audit status or revised a closed 1m bar during the fake route
preview. Both changes prevented a position and fill; a valid built-router
preview saved the fresh revalidation evidence. An old initial decision was
rejected before preview, and the pasted-call path recorded the changed-signal
reason. The full Python suite, Ruff lint/format, source mypy, and source/wheel
build passed. Whole-suite test mypy retains its existing 122 errors in ten
test files; this change added none. On the current database a read-only single
candidate recheck took about 238 ms, so the added transaction hold is bounded
in the observed case, though it has not yet been exercised by a real forward
entry.

Before restarting, a SQLite online backup at
`state-backups/paper-before-entry-revalidation-20261104T004448Z.sqlite3`
passed its integrity check and SHA-256 verification. The restarted local
dashboard's market, paper entry, paper exit, and narrative workers all
reported running with no last error. The protected operations endpoint
returned HTTP 401 without pairing. `LIVE_TRADING_ENABLED=false`, the code
live-execution lock is true, and OpenAI research remained disabled. Restarting
begins a new uninterrupted forward-observation window; it does not reset the
saved research or paper history. There are still zero comparable closed
autonomous forward paper trades and no profitability finding.

The first completed post-restart paper cycle scanned 38 mints, found 29 safe,
evaluated 100 saved candidates, and opened no position; it recorded no entry,
exit, or quote failure. The market collector's first completed cycle made four
successful OHLCV requests with no provider error or rate limit. These checks
confirm the workers resumed, but do not exercise the new entry guard against
a real forward route because no eligible entry occurred.

### Forward-entry evidence accounting (00:56 UTC, 4 November 2026)

The forward performance report previously counted a fully costed campaign
trade with a built-router entry even if its saved execution omitted the final
entry revalidation. It now excludes such closed trades under
`entry_revalidation_missing_or_invalid` and flags open positions lacking that
evidence. The final paper-entry record now includes the refreshed market
inputs, policy version, score, verdict, reasons, audit observation time,
market observation time, and four candle signals. The report checks that the
saved token identity, safe audit status, 1m/5m confirmations, closed-candle
times, and input freshness match the committed position. It rejects missing,
stale, reversed, or blocked evidence. This is an audit-trail validation; it
does not independently prove that an external data provider was complete or
that paper fills would have landed live.

Focused tests include a service-produced paper entry whose stored final
evidence passes the report gate, plus counterexamples for absent, stale,
unconfirmed, and blocked records. The full Python test suite passed after
these changes. Ruff lint and format, source mypy, and source/wheel build also
passed. An online SQLite backup at
`state-backups/paper-before-forward-evidence-20261104T005550Z.sqlite3`
passed `PRAGMA integrity_check` and SHA-256 verification before the local
dashboard restart. After restart the dashboard returned HTTP 200, its
protected operations endpoint returned HTTP 401 without pairing, and all
four workers reported running with no last error. Live trading and OpenAI
research calls remain disabled. The forward campaign still has zero
comparable closed autonomous trades; profitability and strategy superiority
remain unmeasured.

### Free-only candle collection focus (01:09 UTC, 4 November 2026)

The last-hour collection meter observed 202 successful network candle
requests in 3,552 seconds (3.41/minute), with 29 market-ready mints. Merely
refreshing each ready mint every two minutes would require 14.5/minute,
before research or higher-frame calls. A read-only evaluation of those 29
mints found no current `ready` 1m or 5m entry signal. The two pinned pools had
only two and three DEX transactions in the latest five-minute window; their
saved minute series contained gaps. A bounded Solana RPC signature probe for
one pool also found only five recent transactions in five minutes. These
observations explain why spending scarce 1m requests on quiet pins had not
yielded an eight-bar trade-backed window.

The active collection scheduler now retires a pinned pool after three
consecutive fresh DEX observations with fewer than five transactions per five
minutes. With fewer than five trades, five distinct trade-backed minute bars
are mathematically impossible. This changes observation priority only; the
independent research cohort and all entry, exit, audit, and risk gates are
unchanged. A regression test verifies that two quiet pins are retained for
the first two observations, then replaced by more active candidates without
changing the research cohort. The running worker did rotate from the two
observed quiet pins to different candidates after this change.

Review also found that the scheduler excluded candidates whenever any recent
1m, 5m, 15m, or 1h bar had once been revised, even if the corresponding
signal would currently be `stale` or `collecting`. This could starve highly
active pools of the fresh observations needed to recover. The scheduler now
excludes a revision only when the existing timeframe analyzer itself returns
`revised_values`; the analyzer's trading decision remains unchanged. A test
checks both a complete revised 1m window that must still be excluded and an
isolated revised 1h bar that is still collecting and must not block
collection.

The complete Python suite, Ruff lint/format, source mypy, and package build
passed. Online SQLite backups before both controlled dashboard restarts
(`paper-before-quiet-pin-rotation-20261104T010505Z.sqlite3` and
`paper-before-revision-schedule-20261104T010820Z.sqlite3`) passed integrity
and SHA-256 checks. The changes do not establish a usable free-feed entry
window or any positive expectancy yet. The next checkpoint must measure
whether the newly selected active pins produce eight timely, consistent 1m
bars and a verified 5m window. No paid feed was added.

At the 08:00 UTC read-only follow-up, the revised collector completed four
OHLCV requests without an error. Among 26 market-eligible candidates, three
had a `ready` 1m signal, three had a `ready` 5m signal, two had both ready,
and one briefly had both required frames confirmed. No paper position was
opened. The discovery-and-decision worker's next cycle began at 08:00:06 and
finished at 08:01:58; it then waits 300 seconds after completion. A 1m
signal can change from ready to stale within that interval. The next
engineering step is to evaluate newly refreshed candidates promptly after
each collector cycle, using the same paper decision, exit-monitor, risk, and
commit-time revalidation paths, while respecting a user pause and avoiding
excess decision-log volume. The current observation is evidence of a cadence
problem, not evidence of a missed profitable trade.

### Decision cadence after fresh candle collection (08:10 UTC, 4 November 2026)

The paper discovery cycle took about 112 seconds on the observed 08:00 run
and waits another 300 seconds after completion. This can miss a short window
in which authentic 1m and 5m candles are both ready. The market collector now
returns the bounded set of mints whose OHLCV call yielded a recent closed
bar. After collection, it runs the unchanged market and multi-timeframe
confirmation only for those refreshed mints. It invokes the normal paper
decision cycle only if at least one is eligible and the paper automation
worker is active. A paused paper worker causes no wallet loading or fast
entry evaluation. Any fast decision uses the exit monitor's existing
fail-closed status, portfolio risk breakers, quote simulation, and the
commit-time market/candle revalidation. The ordinary discovery-and-decision
cycle remains in place for full candidate coverage and its regular logs.

Focused tests covered empty candle evidence, a confirmed real-candle
opportunity, a paused worker, and an unavailable exit monitor. The full
Python suite, Ruff lint/format, source mypy, and source/wheel build passed.
The online pre-restart SQLite backup
`state-backups/paper-before-fresh-decision-20261104T080929Z.sqlite3` passed
integrity and SHA-256 checks. The restarted local dashboard returned HTTP
200; its protected operations endpoint returned HTTP 401 without pairing.
All four worker states were running with no recorded error immediately after
restart. Live execution is still code-locked and configured off, and OpenAI
research calls remain off. A real fast-triggered forward paper position has
not yet occurred, so this is a tested scheduling improvement rather than
performance evidence.

### Free-only collection checkpoint (08:16 UTC, 4 November 2026)

The user chose to keep the bot on free sources. No paid OHLCV adapter or
subscription was added. The active dashboard remains in `--free-only` mode;
`LIVE_TRADING_ENABLED` and OpenAI narrative calls are off. The code-level
live execution lock remains in place.

The public OHLCV feed supplied about 3.6 successful requests per minute in
the preceding observed hour. At that rate, refreshing every one of the 27
market-ready mints within 120 seconds would require about 13.5 requests per
minute. The collector therefore focuses its scarce calls on two active
market-eligible mints while preserving a separate research observation.
This is a material sampling limit for forward performance and market-wide
opportunity claims.

In a read-only 08:13 candidate check, 28 of the latest 100 discoveries passed
the market and audit gate, but none passed the complete entry gate. A separate
08:14 candle-status check found two candidates with both required frames
ready; both failed trend, momentum, volume, and liquidity alignment. Their
1m signal scores were 45 and 25. At 08:16:56, another same-pass check found
29 market-eligible candidates and again zero confirmed entries; 15 had both
required frames stale, seven had no candle evaluation because their audit was
older than the permitted ten minutes, and the rest had collecting, gapped,
revised, mixed, or partially stale evidence. The last 100 saved decisions
contained 72 rejects, 28 observes, and zero eligible entries. The forward
campaign still had zero open and zero comparable closed autonomous paper
positions. The paper entry, exit, and market workers continued to report
running states without errors at 08:16.

These observations support keeping the existing entry checks rather than
loosening them to force paper trades. They do not establish that the strategy
is profitable or unprofitable. Continue collecting authentic candles and
costed forward decisions; report the free-feed selection bias in any later
strategy comparison.

### Audit-fresh candle focus (08:29 UTC, 4 November 2026)

The free-feed candle scheduler could select a new market-eligible focus mint
whose last passing on-chain audit was already older than the paper entry
limit of ten minutes. One observed focused mint had a 723-second-old audit,
making its collected candles unusable for an immediate entry. New focus
selection now requires an audit no older than seven minutes, leaving three
minutes for candle collection and decision. Existing pins are retained while
their scheduled re-audit is pending; no audit result is extended or inferred.
The fixed, score-independent research cohort, trading signal, risk rules,
and live lock remain unchanged. A focused regression test shows that a
high-activity stale-audit mint loses a new focus slot to two freshly audited
candidates. The collector reports the number of stale-audit candidates
excluded from new focus selection.

The complete Python suite, Ruff lint and format checks, source mypy, and
source/wheel build passed. Before the controlled restart, an online SQLite
backup named `paper-before-fresh-audit-focus-20261104T082707Z.sqlite3`
passed integrity and SHA-256 checks. The restarted dashboard returned HTTP
200, and its protected operations endpoint returned HTTP 401 without a
session. All four workers reported running with no recorded error. In the
first completed post-restart collector result, two focus mints were scheduled,
14 other market-eligible candidates were excluded from *new* focus selection
because their audit was too old, and all four OHLCV requests succeeded. Both
current focused audits were subsequently younger than five minutes. The
forward campaign still had zero open comparable positions. This is an
observation-scheduling fix, not strategy-performance evidence. The restart
begins a new uninterrupted runtime observation window.

The first full paper decision cycle after restart completed at 08:30:47 UTC
without a recorded error or entry pause. It evaluated 100 candidates and
opened no paper position. This confirms that the restarted decision worker
resumed; it does not establish an eligible trade or a return estimate.

### Prospective collector signal evidence (08:44 UTC, 4 November 2026)

The full paper decision worker logs all 100 current candidates about every
seven minutes. A free-feed focus can receive a new 1m candle between those
cycles, leaving short-lived rejected and eligible signal states unrecorded.
The active collector now records one prospective signal observation for each
successfully refreshed candidate it evaluates while the paper worker is
active. These records have their own `collector_signal_observations` table,
separate from paper decisions and orders. They preserve the market score,
verdict, reasons, and candle evidence without implying a trade occurred.

The read-only verification report now includes a bounded signal funnel and
5- and 15-minute outcome checks from authentic OHLCV, plus separately labeled
DEX snapshot price proxies. Every missing or late outcome remains visible;
the focused collection sample is explicitly labeled as selected, not as a
market-wide random sample. An outcome is never promoted to a fill or net
return. The table and index are added on normal journal initialization;
the verified pre-change backup
`state-backups/paper-before-collector-signals-20261104T084212Z.sqlite3`
is the rollback point if this additive schema change must be removed.

Focused tests verified that observations round-trip through SQLite, remain
separate from paper decisions, and appear in the report. The full Python
suite, Ruff lint/format, source mypy, and source/wheel build passed. The
restarted dashboard returned HTTP 200; its protected operations endpoint
returned HTTP 401 without pairing. At 08:43:52 UTC the first restarted
collector cycle completed without a recorded error and wrote four real
observations: three `observe`, one `reject`. One had a ready 1m frame; none
was a confirmed entry. The read-only report returned the matching four-row
funnel. Its 5- and 15-minute price-path cohorts were empty because the
required future windows had not elapsed. The autonomous forward paper cohort
still had zero comparable closed trades and was not promotion-ready. Live
execution and OpenAI calls remained disabled. This controlled restart begins
a new uninterrupted runtime observation window.

### Early rejection-outcome audit (08:53 UTC, 4 November 2026)

The read-only verification code now groups prospective collector observations
by verdict and primary rejection reason for five- and fifteen-minute price
paths. It keeps every missing, late, and backfilled outcome in the reported
denominator. Descriptive medians use only complete, promptly observed future
OHLCV. The report requires at least 30 timely outcomes from 30 distinct
tokens per reason before calling that group descriptive; repeated observations
of one mint cannot satisfy the sample gate. Even after that gate, price paths
are not executable fills or net trading returns. This analysis is not fed
back into entry decisions.

At the first read-only runtime check, 34 collector signal observations had
been saved. Six were old enough for the five-minute outcome horizon. Three
had a complete, timely OHLCV path and three lacked the next minute's entry
bar. Six had DEX snapshot price proxies, which are reported separately.
No fifteen-minute outcomes were yet age-eligible. The most common rejection
reason had four age-eligible observations but only two timely paths, both
from one token. The result is an insufficient sample, not evidence for
loosening candle or audit rules. The autonomous forward paper cohort still
had zero comparable closed trades.

Targeted tests cover missing-outcome denominators, distinct-mint sampling,
and propagation of mint identity into price-path rows. The full Python
suite, Ruff lint/format, source mypy, and source/wheel build passed. This
change is confined to read-only verification and opportunity reporting, so
the running paper worker was not restarted. A fresh verification CLI process
uses the new analysis now; the already running web process will load this
report code at its next controlled restart. Live execution remains locked.

### Windows paper-task recovery on battery (4 November 2026)

Before handing the dashboard to Task Scheduler, an online SQLite backup was
written to `state-backups/paper-before-task-handoff-20261104T085946Z.sqlite3`.
The backup passed `integrity_check`. The first scheduled start remained queued:
the laptop was on battery and the task had Windows' default battery-start
prohibition. The manual dashboard had already stopped, so collection was
temporarily interrupted. The installer now allows this free-only task to start
and continue on battery power and updates an existing task with those settings.

After the update, a manual `Start-ScheduledTask` reached `Running` and the
dashboard returned HTTP 200. The paper entry, paper exit, and market collector
workers each completed fresh cycles without recorded errors. The existing task
still invokes `dashboard --free-only`; the runtime flags were
`LIVE_TRADING_ENABLED=false` and `NARRATIVE_OPENAI_ENABLED=false`, with the
code-level live lock true. Re-running the installer was idempotent. The focused
live-lock tests passed (3 tests). Automatic launch after a future Windows
sign-in and restart after a process crash remain unverified.

At the 09:08 UTC read-only performance check, the prospective focused-collector
funnel had 71 observations from 13 distinct mints: 52 observe, 17 reject, and
2 eligible. The autonomous forward paper cohort still had zero complete closed
trades, so no net profitability conclusion is supported. The collector's public
OHLCV throughput and selection bias limits remain in force. This handoff resets
the uninterrupted runtime observation window; it does not reset paper data.

### Paper-entry USD notional repair (4 November 2026)

The first two prospectively eligible collector observations, both for one mint,
led to autonomous paper actions `simulation_failed:PaperExecutionError:entry_liquidity_unavailable`.
Their stored market snapshots had positive liquidity and matched the candidate
pool, so the failure was not an absent pool. A read-only live Jupiter V2 `/build`
probe returned three route legs but no top-level `swapUsdValue`, `usdValue`, or
`inUsdValue`; its route-leg `usdValue` fields were also absent. The previous
paper execution model therefore could not compute the trade's USD share of
visible pool liquidity. This affected the only two eligible observations in
the early sample. The missed entries remain missed; they were not inserted
retroactively into the forward paper ledger.

For a SOL-input paper buy with no route USD metadata, the model now values the
exact input lamports using Jupiter Price V3's SOL/USD reference and rejects the
entry unless its block ID is within 150 slots of a current processed RPC slot.
The source, price, price block ID, and checked slot are recorded with the paper
fill. The original route USD value takes precedence when present. Missing or
stale price evidence still fails closed, and the pool-liquidity, slippage,
price-impact, quote-age, audit, and candle gates remain in force. Jupiter's
[V2 build response reference](https://developers.jup.ag/docs/api-reference/swap/build)
documents the route shape; its [Price V3 reference](https://developers.jup.ag/docs/price)
documents the SOL/USD field and block ID. The live API response, rather than
the documented optional route-leg USD field, established the observed failure.

Unit and integration tests cover valid and invalid SOL references, missing
route USD, stale price blocks, and persistence of the source in a forward paper
entry. The full Python suite, Ruff lint/format, source mypy, and package build
passed. A separate read-only probe using a public BONK route confirmed that
the current V2 build lacked route USD, while a SOL Price V3 response produced a
positive modeled notional with a 27-slot gap. That probe used an arbitrary
liquidity input only to exercise the model; it was not an actual paper fill.

An online pre-change backup at
`state-backups/paper-before-sol-usd-fix-20261104T092812Z.sqlite3` passed
`quick_check`. The free-only scheduled dashboard was restarted once, returned
HTTP 200, and remained in `Running` state. Live execution remained code-locked
with `LIVE_TRADING_ENABLED=false`; paid OpenAI research remained disabled.
Fresh exit-monitor and market-collector completions were observed after the
restart. The paper entry worker completed its first post-restart cycle at
09:31:06 UTC without a recorded error; it logged 30 observed and 70 rejected
decisions, with no eligible signal. A read-only route-plus-price probe measured
a 1,678 ms quote age against the 5,000 ms gate and a 17-slot SOL price gap.
A missing or stale SOL reference now has a plain-language skip reason in the
dashboard; the updated JavaScript and asset-versioned page were served with
HTTP 200, and `node --check` passed.
A future genuine eligible signal is still needed to verify a complete
autonomous paper fill and costed exit in the running process.

### Discovery transport timeout recovery (4 November 2026)

The 09:36 UTC paper-entry cycle recorded a `ConnectTimeout` during its scan.
The scheduled dashboard process stayed alive and the next entry cycle completed
at 09:45:57 UTC without an error. This was a transient worker degradation, not
a process failure. The exact remote request is not retained in the worker
status, so the timeout cannot be attributed to one provider with certainty.

Code inspection found that DEX Screener and optional pump.fun clients could
raise raw `httpx.ConnectTimeout` from their GET calls. The scan already handles
their typed provider errors and records degradation, but a raw transport error
escaped and aborted that cycle before decisions were logged. Both clients now
translate request transport failures into their typed provider errors without
including connection strings or credentials in the error message. A scan-level
test injects a DEX handshake timeout and confirms that all four DEX discovery
feeds are marked degraded while the scan returns normally. Client tests cover
both DEX token lookup and profile lookup, plus the optional pump.fun feed.

At the read-only 09:38 UTC report, the most recent 100 focused collector
observations included four eligible observations, all from one mint. Each
five-minute authentic OHLCV follow-up was missing at least one future bar;
these are unavailable outcomes, not wins or losses. The free OHLCV collector
delivered 3.28 successful network requests per minute versus a theoretical
15.0/minute just to refresh each of 30 market-ready mints every two minutes.
The independent research and follow-up demand is additional. The autonomous
forward paper sample still had zero complete comparable closed trades.

The full Python suite, Ruff lint/format, source mypy, and package build passed.
An online backup at
`state-backups/paper-before-provider-timeout-fix-20261104T094630Z.sqlite3`
passed `quick_check`. The scheduled free-only dashboard was restarted once;
Task Scheduler reported `Running`, the dashboard returned HTTP 200, and the
code-level live lock and disabled paid OpenAI flag were verified. A fresh exit
and narrative cycle completed; entry and market cycles were in progress at
the first post-restart check. A later entry completion is needed to confirm
the deployed process runs through the full decision loop.

The post-restart paper entry cycle completed at 09:49:53 UTC without a
recorded error. It logged 30 observed and 70 rejected decisions, with no
eligible signal or simulated entry. The market collector also completed fresh
cycles without a recorded error; the dashboard returned HTTP 200. The
unauthenticated operations endpoint returned HTTP 401 as expected. Local
configuration still has `LIVE_TRADING_ENABLED=false` and
`NARRATIVE_OPENAI_ENABLED=false`; the live service additionally enforces the
code-level `LIVE_EXECUTION_LOCKED` gate, covered by the existing live-lock
tests. This verifies recovery of the deployed worker, not a successful
end-to-end entry and exit.

The four eligible collector observations from 09:18 to 09:22 UTC did trigger
immediate paper decision cycles. The matching strategy records show eligible
scores of 77 to 91 and action
`simulation_failed:PaperExecutionError:entry_liquidity_unavailable` each time.
Those failures occurred before the SOL/USD reference fix was deployed. The
collector-to-entry trigger itself did not miss those signals. The liquidity
valuation repair remains to be exercised by a new genuine eligible signal;
it would be misleading to count the earlier opportunities as executed trades.

### First post-repair autonomous paper entry (4 November 2026)

At 09:57:13 UTC, a new collector-confirmed signal opened an autonomous paper
position for mint `Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump`.
The saved decision scored 78 and was eligible. The simulated entry debited
9,427,601 paper lamports and credited 18,348,530 token atomic units. Its
recorded route source was Jupiter `v2-build`; because that route omitted USD
notional, the entry used the free Jupiter Price V3 SOL/USD reference. The
recorded price block was 465,259,825 and the RPC slot check was 465,259,850,
a 25-slot gap within the 150-slot freshness limit. Quote age was 1,504 ms
against the 5,000 ms gate. The fill stored modeled slippage, network cost,
priority cost, token-account reserve, pool liquidity, and the final entry
revalidation. This is a simulated fill only; no transaction was broadcast.

Independent read-only verification replayed the saved entry decision against
point-in-time candles and found exact signal matches for 1m, 5m, 15m, and 1h.
The saved market-gate inputs and policy reproduced the eligible verdict, and
the entry revalidation evidence passed the forward-performance gate. At
09:59:51 UTC the independent paper exit worker recorded a costed mark for the
open position and completed without an error. The position was still open at
that checkpoint. It cannot yet contribute to closed-trade return, win rate,
profit factor, expectancy, or drawdown claims. The dashboard remained HTTP
200, live trading was locked in code and disabled in configuration, and paid
OpenAI research remained disabled although its key was configured.

### Jupiter exit-route timeout recovery (4 November 2026)

The newly opened paper position exposed a real exit-monitor reliability issue.
At 10:04:01 UTC, an exit cycle recorded a raw `ConnectTimeout` and kept its
previous quote mark. The worker thread remained live and recovered on a later
cycle, marking the position again at 10:08:09 UTC. Inspection showed that
`JupiterClient.router_quote()` allowed `httpx.RequestError` to escape before
`PaperTradingService.portfolio()` could convert it into a stale-quote state.
That aborted a whole exit cycle instead of reporting the quote outage and
pausing new entries through the existing health gate.

All Jupiter GET and POST transport errors now become a bounded, credential-free
`JupiterError`. A regression test injects a real `httpx.ConnectTimeout` into
the fee-free router call and verifies the typed error. Existing paper tests
verify that the portfolio treats `JupiterError` as an unavailable exit quote,
and existing automation tests verify that an unavailable exit monitor blocks
new entries. The focused Jupiter, paper, and automation tests, full Python
suite, Ruff lint/format, source mypy, and package build passed.

An online backup named
`state-backups/paper-before-jupiter-timeout-fix-20261104T100923Z.sqlite3`
passed `PRAGMA quick_check` and contained the open paper position. The
free-only scheduled dashboard was stopped and started once to load the fix;
Task Scheduler returned `Running` and the dashboard returned HTTP 200. Its
first exit cycle completed at 10:10:21 UTC without an error and saved a fresh
costed mark for the open position. The quote-health state had zero unavailable
positions. The market and entry workers had started but had not completed
their first post-restart cycles at the initial checkpoint. The live execution
code lock remained true, `LIVE_TRADING_ENABLED=false`, and
`NARRATIVE_OPENAI_ENABLED=false`. The position remained open; complete
closed-trade performance was still unavailable.

The first post-restart entry cycle then completed at 10:13:00 UTC with no
recorded error. It logged 31 observed and 69 rejected decisions, and no new
eligible signal. Market and exit collection continued with fresh successful
cycles. The open position had costed exit marks with gaps inside the
360-second coverage window, and the forward-performance report still excluded
it from closed-trade metrics until an actual simulated exit is recorded.

At the 10:14 UTC forward checkpoint, that position was still open and had a
fresh costed exit mark. Its 9,427,601-lamport paper entry debit was 94 basis
points of the configured one-SOL starting bankroll, below the 100-basis-point
per-trade limit. The risk query returned no active entry breaker. The
independent exit, market, and paper-entry workers had all completed after the
restart without a current error. The autonomous sample remained at zero
complete closed trades; mark-to-market values are not substituted for
realized results.

The Paper Portfolio P&L note now shows the token-account rent reserved by
currently open positions and explains on hover that the paper model does not
assume rent recovery without closing an empty account. This addresses the
otherwise misleading initial P&L on small positions, where the measured
1,513,840-lamport reserve is a large share of the paper entry debit. The
strategy, exit trigger, and accounting values were not changed. The versioned
JavaScript asset and page were served with HTTP 200, the updated text was
present, `node --check` passed, and Ruff lint/format passed. The static asset
was served from disk without restarting the running paper workers.
The full Python test suite, source mypy, and source/wheel build also passed
after the UI edit; the only test warning was the existing Starlette test-client
deprecation.

### First forward paper trade lifecycle audit (4 November 2026)

A read-only audit of the first post-repair autonomous position found eight
saved entry reasons, point-in-time market-gate inputs, audited token status,
and signal values for all four candle timeframes. Its entry fill records the
fee-free built-router source, 1,504 ms quote age, measured token-account
reserve, simulated slippage and fees, SOL/USD price-block provenance, and the
final entry revalidation. The most recent inspected exit mark used the same
`v2-build` quote source, was 1,977 ms old, and recorded modeled network,
priority, and slippage costs. The forward mark report counted the open path as
covered within its 360-second maximum mark-gap window. These checks establish
record completeness for this open trade, not exit execution quality.

At 10:21 UTC, the position remained open at TP stage zero with no realized
proceeds. The paper, exit, market, and narrative workers were running without
a current error, and the dashboard returned HTTP 200. The prospective shared
paper/exit/market continuity streak was `current` from 10:13:00 UTC on
4 November. The earliest possible 30-day uninterrupted observation endpoint
is therefore 10:13:00 UTC on 4 December, assuming every subsequent cycle
passes; trade-count and performance gates would remain separate. No complete
comparable autonomous closed trade or net expectancy is available yet.

### Live quote-outage and free-candle coverage checkpoint (4 November 2026)

The open paper position accumulated 11 costed exit marks from 09:57 to
10:23 UTC. Its longest observed mark-to-mark gap was 264 seconds; two gaps
exceeded 180 seconds. The first crossed the earlier 10:04 transport-error
cycle. The second crossed a 10:16 exit cycle that completed normally but
recorded `paper_quote_unavailable` with `PaperExecutionError:stale_route`.
The next completed cycle recorded `paper_quote_recovered` at 10:19. This
demonstrates the typed quote-health path under a real slow route: the worker
continued, disclosed the missing fresh quote, and resumed marking. It does
not prove a stop could have filled during the unavailable interval. The
360-second bounded mark-gap criterion still covered this open path at the
checkpoint; the two long gaps remain a measured execution-risk limitation.

The latest production entry-readiness evaluation considered 100 saved
discovery candidates. Thirty-one passed the market gate; 18 then lacked a
current on-chain audit, 12 lacked usable fresh 1m/5m candles, and one had
ready but misaligned signals because a higher timeframe trended down.
None was entry-eligible. The running collector completed four authentic
OHLCV requests in its latest measured cycle, but its free capacity is far
below a two-minute refresh of every market-ready pool. The bot kept stale
or missing data as an entry veto. This is coverage evidence, not a strategy
return or proof that rejected coins would have lost money.

### Bounded stale-route retry for paper exits (4 November 2026)

A second independent exit quote, at 10:25 UTC, exceeded the 5,000 ms
paper quote-age gate. The worker completed its cycle, recorded
`paper_quote_unavailable` with reason `stale_route`, and recovered at 10:27.
Together with the 10:16 outage, this showed repeated roughly four-minute
gaps in costed marks while the position was open. The fail-closed behavior
was correct, but the next mark waited for another two-minute cycle.

The paper exit-mark path now retries **once** only when the first built-route
preview is stale. It requests a new route and measures its age independently.
A successful second quote records `route_quote_attempts=2` in the saved mark.
Two stale results still yield an unavailable quote and preserve the existing
entry breaker; unrelated route errors are not retried. This changes only
read-only paper exit quoting, not live execution or the deterministic exit
thresholds. A focused regression test covers both successful retry and the
two-stale limit. The complete Python suite, Ruff lint/format, source mypy,
JavaScript syntax, and package build passed.

An online SQLite backup named
`state-backups/paper-before-stale-route-retry-20261104T103247Z.sqlite3`
passed `PRAGMA quick_check` and contained the open position. The free-only
scheduled dashboard was restarted once. Task Scheduler returned `Running`,
the page returned HTTP 200, and the protected operations endpoint returned
HTTP 401 without a session. The first post-restart exit cycle completed at
10:33:35 UTC without an error and saved a fresh `v2-build` mark with a
749 ms quote age and `route_quote_attempts=1`. A genuine stale-first,
fresh-second route has not yet occurred in the running worker, so live
effectiveness of the retry remains unverified. The paper-entry and market
workers had started their first post-restart cycles at that checkpoint.
Live execution remained code-locked and disabled; OpenAI research remained
disabled. This restart begins a new prospective continuity streak.

The running worker subsequently exercised the new path. Its 10:35 UTC exit
cycle saved a costed `v2-build` mark after **two** route requests; the accepted
second quote was 1,338 ms old. The position remained open, quote-health
state reported no unavailable position, and the entry, exit, and market
workers all completed post-restart cycles without a recorded error. This
observed successful retry closes the deployment verification gap above.
The two post-restart marks were 129 seconds apart, inside the 360-second
coverage criterion.
It does not show that stops or take-profits would land during a future
outage, and the forward sample still has zero complete comparable exits.

### Free-only forward checkpoint after exit retry (4 November 2026)

At 10:42 UTC, the scheduled free-only dashboard was still running. The
post-restart open position had five costed exit marks; one needed the bounded
second route request, and the largest mark-to-mark gap was 129 seconds. Its
latest mark was at 10:42:01 UTC. The position was still open at TP stage zero,
so no complete autonomous exit, realized trade return, expectancy, profit
factor, or win rate can be reported. The recent market and exit worker cycles
completed normally. The configured OpenAI research key was present but
`NARRATIVE_OPENAI_ENABLED=false`; live trading remained disabled.

The latest read-only verification considered 100 current discovery candidates:
32 passed the market gate, 16 lacked a current audit, 15 more lacked usable
1m/5m candles, and one failed candle alignment. None qualified for another
entry. The 15 most recent saved candle decisions replayed with matching
verdicts; both checked linked paper entries matched their saved candle and
market-gate evidence. These are decision-integrity checks, not return data.
Of 2,134 research-cohort 1m bars with known first fetches, 475 were fetched
within 120 seconds of closing. The free public request capacity remains the
main limitation on broad timely coverage. The current strategy requires fresh
1m and 5m data; stale 15m/1h bars are disclosed and never used as confirmed
higher-timeframe signals, while verified ready higher-timeframe downtrends
veto entry. There is still no comparable historical or forward sample from
which to claim that an upgraded momentum strategy performs better.

### Bounded stale-route retry for paper exit fills (4 November 2026)

The prior bounded retry covered mark quotes but not the separate fresh route
used to execute a triggered stop or partial take-profit in paper mode. A slow
first built-route response could therefore fail an otherwise actionable paper
exit and leave it until the next monitor cycle. Full and partial paper exits
now retry once **only** when the first quote fails the route-age gate. The
second quote has its own timestamp and cost estimate. The saved exit execution
records `route_quote_attempts`; two stale responses leave the position and
fill ledger unchanged. Other pricing or Jupiter errors are not retried.

Focused tests exercised stale-first/fresh-second partial and full exits, and
two stale full-exit responses with no recorded fill. The entire Python suite,
Ruff lint/format, source mypy, and source/wheel build passed. An online SQLite
backup at `state-backups/paper-before-exit-fill-retry-20261104T104801Z.sqlite3`
passed `PRAGMA quick_check` and held the one open position. After the
scheduled free-only dashboard was stopped and started, Task Scheduler reported
`Running`, HTTP returned 200 for the page and 401 for the unpaired operations
endpoint, and a fresh costed `v2-build` exit mark was saved at 10:48:38 UTC.
The code-level live lock remained true, `LIVE_TRADING_ENABLED=false`, and
`NARRATIVE_OPENAI_ENABLED=false`. No triggered paper exit has yet exercised
the new retry in the running worker; deployment verifies startup and mark
continuity, while the exit-fill behavior is covered by the focused tests.
The position remains open, so forward returns and strategy superiority are
still unproven.

## 2026-11-06: interrupted campaign archive and clean paper restart

The old scheduled dashboard was stopped, localhost port 8787 was closed, and
the previous campaign still had five open paper positions. Their latest exit
marks and the last completed exit-monitor cycle were on 4 November at about
16:59 UTC. The market collector's later completion did not provide exit
monitoring. The backup
`state-backups/paper-before-interruption-20261106T094039Z.sqlite3` passed
`PRAGMA quick_check` and held five open and two closed paper positions.

At 09:40:47 UTC on 6 November, the old campaign was marked `interrupted`.
It records the last completed exit-monitor time, the interruption time, and
the five affected position IDs with their last quote timestamps. One campaign
event and five position events were saved. The positions remain `open` in
the archived database because no exits occurred; no sell fill, proceeds, or
post-gap P&L was fabricated. The old journal passed `PRAGMA quick_check`.

A separate run in `forward-paper-2026-11-06-v3.sqlite3` copied timestamped
research observations and started with zero positions, fills, and decisions.
Its campaign metadata links to the interrupted source. The old and new
journals both passed `PRAGMA quick_check`, and `.env` now selects the new run.
The free-only Windows logon task was revalidated and started. It has a
one-minute unexpected-failure restart interval, no execution time limit,
ignores duplicate starts, and permits battery operation. Task Scheduler
reported `Running`; localhost returned HTTP 200 and the unpaired operations
route returned HTTP 401. The paper decision, exit, market, and narrative
workers each completed an initial cycle without a recorded error. Live
execution remained code-locked and configured off; OpenAI narrative calls
remained disabled. Automatic startup after the next Windows sign-in is still
unverified, and the machine must stay awake and connected for uninterrupted
collection.

The full Python suite, Ruff lint/format, source mypy, and package build
passed. A new test verifies that an interrupted campaign preserves its open
positions and fills, records exactly one interruption per position, and
starts a new run without inherited paper positions. No trading-performance
claim follows from this restart; the new forward sample begins at zero.
The interruption helper also refuses to archive a campaign while its worker
holds the journal's OS lock; this is covered by the focused campaign test.

## 2026-11-06: clean forward run health check (10:02 UTC)

This was a read-only check of `forward-paper-2026-11-06-v3.sqlite3` while the
scheduled free-only dashboard remained running. Localhost returned HTTP 200,
Task Scheduler reported `Running`, and SQLite `PRAGMA quick_check` returned
`ok`. The market collector, narrative collector, paper decision worker, and
independent paper exit monitor had each completed cycles without a recorded
worker error. The exit monitor had completed 10 cycles; all three open paper
positions had fresh marks at about 10:01 UTC. The decision worker was in a
new cycle when checked, after three completed cycles.

The campaign had 900 saved strategy decisions, three autonomous paper buys,
13 saved position marks, and **zero closed positions or sell fills**. There is
therefore no realized return, win rate, profit factor, expectancy, or strategy
superiority result for this campaign yet. The latest 100 saved decisions formed
one complete ranked cycle with no paper opening. Three of three linked entry
market gates replayed with matching verdicts. Two of three entry candle signals
matched; one was `unverifiable` because its older 1-hour candle provenance was
incomplete at decision time. That entry's recorded 1-hour signal was stale and
unconfirmed, not an entry confirmation. Of the latest 15 decisions with candle
evidence, 13 matched and two were unverifiable; none mismatched. These
unverifiable cases must remain disclosed or excluded from strict evidence.

The read-only verifier reported the code-level live lock enabled and the
environment live flag false. The focused forward-performance and verification
test modules passed (28 tests). No code defect was demonstrated by this check;
no code or trading state was changed. Continuous worker operation still depends
on Windows staying signed in, awake, and connected. Automatic launch after the
next Windows sign-in remains unverified.

## 2026-11-06: controlled dashboard restart for pairing-code rotation

The running free-only task was stopped and restarted after an online backup at
`state-backups/paper-before-pairing-rotation-20261106T100747Z.sqlite3` passed
`PRAGMA quick_check` with three open paper positions. The scheduled task returned
to `Running` and localhost returned HTTP 200. The independent exit monitor
completed at 10:07:22 UTC before the restart and again at 10:08:30 UTC after it,
refreshing marks for all three positions. This 68-second interval was shorter
than the configured two-minute exit check cadence; no scheduled exit check was
missed. There were no strategy decisions recorded after 10:07 UTC before the
restart, so the stopped decision cycle left no partial decision records.
Post-restart SQLite `PRAGMA quick_check` returned `ok`. The campaign was kept
active with the same three open positions; no exit, proceeds, or P&L was
invented. The new ephemeral pairing code was printed only in the restarted
dashboard console and was not saved in the journal or this report.

## 2026-11-06: optional remembered browser sessions

The eight-hour memory-only browser session remains the default. An explicit
**Remember this browser** choice now stores only the SHA-256 digest of a random
session ID in a separate per-campaign SQLite auth file. Remembered sessions have
a 14-day absolute and seven-day idle expiry. Each restart generates a fresh
in-memory CSRF token for a restored session. Sign out revokes one browser, and
Sign out all browsers revokes every remembered browser. Store errors deny
remembered pairing or session use; a short-lived session still works if the
optional auth store is unavailable. No wallet key, pairing code, raw session
ID, or CSRF token is written to the auth file.

Tests covered opt-in persistence across a new dashboard instance, rejection of
short sessions after restart, strict request validation, absolute and idle
expiry, stale CSRF rejection after restart, logout, logout-all, and corrupt-store
fail-closed behavior. The full Python suite, Ruff lint/format, source mypy,
JavaScript syntax check, package build, and a pinned-dependency vulnerability
audit passed. The repository-wide mypy command still reports 133 errors in
test annotations; source-only mypy passed with no errors. The first slower
dependency-audit attempt was cancelled; the no-resolution pinned-lock audit
completed with no known vulnerabilities.

The deployment backup
`state-backups/paper-before-remember-browser-20261106T102007Z.sqlite3`
passed `PRAGMA quick_check` with three open positions. The free-only dashboard
task restarted successfully; localhost returned the new pairing UI and HTTP
200, while an unpaired auth-status request stayed unauthenticated. The exit
monitor completed at 10:18:48 UTC before the restart and 10:20:41 UTC after it,
refreshing all three open-position marks within its two-minute check cadence.
The active journal again passed `PRAGMA quick_check`. Live execution remained
code-locked and configured off; paid OpenAI research remained off. Because the
operator's old session was memory-only, one final pairing with the new checkbox
is needed to activate the remembered session in that browser.

## 2026-11-10: monitored paper-run recovery and restart watchdog

The free-only dashboard was offline when checked on 10 November. The scheduled
task was registered but `Ready`; localhost refused the connection. The last
completed exit-monitor cycle was at 18:51:23 UTC on 7 November. All five
positions in the active 6 November campaign had already closed before that
cycle, so there were **zero open positions exposed to missed exits** at the
restart. An online SQLite backup was saved at
`state-backups/paper-before-resume-20261110T175701Z.sqlite3` and passed
`PRAGMA quick_check`. The existing campaign and its remembered browser session
were retained. No fills or prices were invented for the offline period.

The scheduled free-only task was started. The first new exit-monitor cycle
completed at 17:57:36 UTC on 10 November. A single `paper_monitoring_gap`
event records that interval and `open_positions_at_resume=0`. The market
collector and paper decision worker then completed new cycles. At the 18:04 UTC
checkpoint, the continuity helper reported `current`, with its **new** common
streak beginning at 17:58:51 UTC on 10 November. This resets the observation
clock; it does not retroactively count the offline days. The journal passed
`PRAGMA quick_check`, localhost returned HTTP 200, and the task remained
`Running` with the time trigger's next run scheduled. The live execution code
lock was true, `LIVE_TRADING_ENABLED=false`, and paid OpenAI narrative calls
remained disabled.

The task installer now preserves the sign-in launch and adds a repeating
five-minute time trigger with no end duration. It keeps `IgnoreNew`, so a
scheduled tick while the dashboard is running does not create a duplicate.
Task Scheduler event 322 confirmed such a duplicate start was ignored during
this session. The installer ran twice successfully; the second run made no
change. The PowerShell parser found no errors, and the focused continuity and
campaign tests passed (7 tests). An unexpected-stop relaunch and a future
sign-in launch have **not** yet been observed end to end. Continuous collection
still requires Windows to stay powered, awake, signed in, and connected.

Read-only performance at the checkpoint: 5 autonomous closed trades, 0 open,
0% win rate, profit factor 0, and net realized paper P&L of **-0.0091359 SOL**
after modeled execution costs. Net trade return was -19.6851% and mean trade
expectancy -19.6782%; average holding time was 91,707 seconds (about 25.5
hours). Four losses exited at maximum hold time and one at the stop loss.
The ledger reserves 0.0075438 SOL in token-account rent; hypothetical full
recovery would leave -0.0015921 SOL, but account closure and rent recovery are
not simulated as realized proceeds. Only one of five closed positions had
bounded costed mark gaps over its full life. This five-trade sample is below
the report's 30-trade minimum and cannot establish profitability or support
strategy optimization. The campaign stays in forward paper mode.

## 2026-11-10: historical monitoring gap found; clean v4 run started

A position-level mark audit found that four of the five v3 positions were still
open across a **90,784-second (25.2-hour)** gap, from approximately 16:45 UTC
on 6 November to 17:58 UTC on 7 November. Exit-monitor history confirms its
last completion before the gap at 16:45:25 UTC. The later simulated max-hold
exits cannot show whether a stop or take-profit would have fired during that
outage. The five-trade figures in the preceding checkpoint describe the
ledger, **not a valid five-trade strategy sample**.

The forward-performance verifier now requires bounded costed exit marks across
each closed position's entire life. It retains fills in the audit ledger but
excludes positions with missing or overlong mark intervals under
`unbounded_quote_mark_gap`. On v3, four positions are excluded; the remaining
fully monitored trade lost 3,246,707 lamports (0.003246707 SOL). One trade
cannot support a performance conclusion. The Paper Portfolio now shows closed
mark-path coverage and a monitoring-gap warning when this exclusion occurs.

After confirming no v3 positions were open, the scheduled task was stopped and
temporarily disabled. A SQLite backup at
`state-backups/paper-before-v4-20261110T183105Z.sqlite3` passed
`PRAGMA quick_check` with zero open positions. The v3 campaign was marked
`interrupted` with the four affected position IDs and their exact gap bounds;
existing fills and positions were preserved. The new
`forward-paper-2026-11-10-v4.sqlite3` campaign began at 18:31:23 UTC. It copied
timestamped research observations but started with **zero positions, fills,
and decisions**. The one valid remembered browser session was moved to the
new campaign's auth store after the dashboard stopped; only its stored hash
and expiry data moved. The old auth store is no longer present. No pairing
code or wallet secret was copied into the journal.
The remembered session record and its SQLite integrity were verified, but
Chrome session reuse could not be observed: browser automation timed out while
attaching to the existing dashboard tab. The local HTTP check does not prove
that the browser cookie is still accepted.

The free-only task was re-enabled and started. Localhost returned HTTP 200;
the active v4 journal passed `PRAGMA quick_check`. At the 18:34 UTC checkpoint,
the market, narrative, exit-monitor, and paper decision workers had each
completed a cycle with no recorded error. Prospective paper continuity was
`current`, beginning at the paper decision worker's first completion at
18:34:12 UTC. The first cycle recorded 100 decisions: 74 rejected, 26 observed,
and **zero buys**. No new trading-performance estimate exists yet. Live
execution remained code-locked and configured off; paid OpenAI research stayed
disabled.

Focused forward-performance and campaign tests passed (17 tests). The full
Python test suite, Ruff lint and format checks, source mypy, JavaScript syntax
check, and package build passed. The remaining operational gate is sustained
uninterrupted observation, including closed-position mark coverage and an
observed recovery after an unexpected task stop or future sign-in. Do not use
the interrupted v3 fills to tune the strategy.

## 2026-11-10: first free-only v4 readiness checkpoint

By 18:48 UTC, the clean v4 run had completed three paper decision cycles and
saved 300 decisions, with no positions opened and no worker error. The common
continuity streak was current for about 14 minutes. The active journal passed
`PRAGMA quick_check`, the scheduled task remained `Running`, and localhost
returned HTTP 200.

The latest 100 discovery candidates contained 28 that passed the market-only
gate. Of 21 candidates whose candles were evaluated, **all 21 lacked usable
authentic 1m/5m evidence**; seven more were blocked before candle evaluation
by missing or stale on-chain audits. None was fully entry eligible. In the
preceding saved decision cycle, 19 candle-bearing decisions replayed exactly
against recorded point-in-time evidence, and no required 1m/5m pair was ready.
These are fail-closed decisions, not evidence that the strategy is profitable
or unprofitable.

The free public OHLCV lane delivered about 3.6 successful network calls per
minute over the observed window, with three rate-limited cycles. Refreshing
all 28 market-ready mints every two minutes would require at least 14 calls
per minute before research or higher-timeframe requests. The collector already
focuses a smaller candidate subset; this checkpoint does not justify relaxing
the candle gate or selecting only winners for replay. [CoinGecko lists a nominal
30-call/minute public GeckoTerminal limit](https://support.coingecko.com/articles/6867086195-does-geckoterminal-have-an-api),
but that figure does not override the lower measured throughput on this
connection. Its [on-chain OHLCV WebSocket documentation](https://docs.coingecko.com/websocket/wssonchainohlcv)
requires a Pro API key, and [DEX Screener's published API reference](https://docs.dexscreener.com/api/reference)
exposes pair snapshots without an OHLCV endpoint. The free-only constraint
remains in force.

[CoinGecko's free Demo REST API does include pool OHLCV](https://www.coingecko.com/learn/how-to-pull-crypto-prices-from-dexs),
and the client already supports it for bounded backfill. Its [10,000 monthly
call credits](https://www.coingecko.com/en/api/pricing) average only about
0.23 calls per minute over a 30-day month, despite a higher burst limit.
It cannot sustain this bot's continuous 1m multi-pool lane. No new account,
key, paid plan, or unverified feed was added at this checkpoint.

## 2026-11-10: unattended startup protection

At the 20:10 UTC checkpoint, v4 had two open paper positions, both with fresh
costed exit marks, and no closed trades. The exit worker and dashboard were
healthy. This is observation, not a trading-performance result.

Startup now checks the last completed exit-monitor cycle and the last quote
timestamp for every open position before starting paper workers. If either
exceeds the same `2 * exit interval + 60 seconds` coverage limit used by the
entry monitor gate, the campaign is marked `interrupted` under the exclusive
worker lock. Open positions and fills stay in the journal for audit; no catch-up
exit is invented. The dashboard stays available, while paper entry, exit,
refresh, call submission, and automation-start paths reject new mutations in
that campaign. A clean forward run is required to resume paper trading.

Tests cover a fresh restart, stale position quotes, an offline monitor with an
open position, no open positions, idempotent interruption, preserved fills,
and blocked dashboard/service mutations. The full Python suite passed, as did
Ruff lint/format, mypy, and a wheel build. A consistent pre-restart SQLite backup at
`state-backups/forward-paper-2026-11-10-v4-pre-quarantine-20261110T201801Z.sqlite3`
passed `PRAGMA quick_check`.

The scheduled localhost task was stopped and restarted at about 20:18 UTC.
It returned HTTP 200, v4 remained `active`, both positions remained open, and
fresh exit marks and a completed exit-monitor cycle were recorded at
20:18:45 UTC. Live execution remained locked and configured off. There are
still no closed v4 trades, so net expectancy and drawdown cannot yet be
estimated. An in-process monitor that hangs after startup remains a separate
runtime-watchdog issue; missing marks are excluded from forward performance.

## 2026-11-10: runtime watchdog and atomic paper-write gate

The remaining in-process monitoring gap now has a 30-second watchdog. During
an active forward paper run, it checks both completed exit cycles and the last
quote for every open position. A gap beyond `2 * exit interval + 60 seconds`
interrupts the campaign and stops paper entry/exit workers. The market and
narrative collectors can continue gathering research. Paper position opens,
marks, partial exits, and closes now check campaign status inside their SQLite
write transactions. A worker that finishes a network request after the
interruption cannot insert a late paper fill or mark.

A deployment check caught a missing allowlist entry for the watchdog's durable
worker state. That defect was fixed and covered by a regression test requiring
both a persisted heartbeat and a recorded watchdog cycle. Focused journal and
dashboard tests, the full Python suite, Ruff lint/format, mypy, and wheel build
passed. A consistent backup at
`state-backups/forward-paper-2026-11-10-v4-pre-watchdog-heartbeat-20261110T202925Z.sqlite3`
passed SQLite integrity checks before redeployment.

After the controlled restart, localhost returned HTTP 200 and the scheduled
task was `Running`. At 20:30 UTC, v4 remained active with the same two open
positions, fresh marks at 20:30:02 UTC, an exit-monitor completion at
20:30:03 UTC, and a persisted watchdog completion at 20:30:00 UTC with no
error. No closed v4 trades or performance conclusion are available yet. Live
trading stayed disabled.

The Operations health payload now flags a stopped or stale watchdog while
paper automation runs. A focused health test covers both failure states; the
dashboard was restarted after this change. At 20:35 UTC, the task was running,
localhost returned HTTP 200, SQLite integrity passed, the watchdog completed
at 20:35:16 UTC without error, and both open positions received new marks by
20:35:19 UTC. The campaign remained active. No real-money execution was
enabled.

## 2026-11-10: shared paper-entry monitoring gate

A route audit found that pasted calls and manual paper buys could attempt an
entry while the exit monitor was unavailable. Autonomous cycles had their own
monitor breaker, but the shared `paper_entry_breakers` did not. Forward paper
entries now require a recent completion from both the current exit-monitor
run and the current watchdog run. The same check executes inside the atomic
paper-entry transaction, after quote building, so an outage during preview
also blocks the fill. Interrupted campaigns remain blocked. This affects all
entry sources without changing candle or market thresholds.

Focused tests cover missing monitor, missing watchdog, and healthy workers;
the existing paper service and call tests passed. The full Python suite, Ruff
lint/format, mypy, and wheel build passed. A consistent backup at
`state-backups/forward-paper-2026-11-10-v4-pre-entry-monitor-gate-20261110T204152Z.sqlite3`
passed `PRAGMA quick_check` before the controlled restart. At 20:42 UTC,
localhost returned HTTP 200, the scheduled task was running, v4 remained
active, both open positions had fresh marks and bounded mark paths, and the
new shared entry breaker returned no reasons. The two positions had 80 costed
marks in total. No v4 position had closed, so strategy performance remains
unmeasured. Live trading stayed code-locked and configured off.

## 2026-11-10: read-only walk-forward coverage checkpoint

The clean v4 campaign still had two open positions and zero closed positions.
Its first 82 paper marks were all costed, and both open position paths had
bounded mark gaps. The scheduled dashboard returned HTTP 200 and its decision,
exit, watchdog, and market workers had completed recent cycles without errors.

The two research-cohort mints with the most verified 1m candles had 343 and
338 such candles. A read-only anchored walk-forward run used the configured
TP ladder, stop, cost model, saved research membership, pool-matched candles,
point-in-time audit and market observations, and the paper polling interval.
Each produced 26 unseen folds and **zero closed trades**. Both returned
`partial_out_of_sample_coverage`, with no promotion claim. A separate coverage
probe counted 14 and 13 folds lacking timely audit history, plus 3 and 6 folds
lacking timely candle availability; 9 and 7 folds were complete. These are
input-coverage findings, not zero-return strategy results.

All 20 fixed research-cohort mints now have at least one timestamped audit.
At this checkpoint 14 had an audit from the previous ten minutes, including
9 currently safe mints. The existing worker reserves up to ten cohort audits
per cycle and recorded ten in its latest completed scan. Older audit states
cannot be reconstructed, so historical folds remain excluded. The free public
OHLCV lane delivered 202 successful network responses over roughly 59 minutes
(3.41/minute), while refreshing all 32 market-ready mints every two minutes
would need at least 16/minute before research and higher timeframes. The
latest 100 saved decisions contained no entry-ready candle set. The existing
stale-data gate remains in force; no indicator ranking, profitability, or
original-versus-upgraded strategy claim is supported.

## 2026-11-10: bounded mint-account batching and free-tier capacity check

The prior status check alone did not improve research coverage. Twelve recent
paper scans started roughly 400 seconds apart because each 98–111 second scan
was followed by a 300 second pause. Ten fixed-cohort re-audits per scan across
20 members can therefore leave some audit observations older than the
600-second replay freshness limit. A live checkpoint saw all 20 audits fresh,
but seven were already 528–541 seconds old and would become stale before their
next turn. This is intermittent coverage, not a reason to relax the safety gate.

The first bounded RPC optimization groups up to eight mint accounts into each
`getMultipleAccounts` call. Each token still receives the same mint-authority,
freeze-authority, largest-account and controller analysis. A malformed or failed
batch falls back to the original per-mint read. The 38-token real paper scan
completed at 21:00:25 UTC with **38/38 mint accounts supplied by five batch
calls**, 28 safe audits, no worker error, and two open monitored paper positions.
This removes 33 individual mint-account requests in that scan. The scan still
took about 110 seconds, so this is a request-budget improvement, not a measured
latency or strategy-performance improvement.

Before batching, the database recorded 397 audit observations in one hour.
At that sustained pace, the original two mandatory mint/holder RPC calls alone
would use about 572,000 calls over 30 days; enriched controller and origin
analysis, discovery, retries and other workers add further calls. Helius lists
one million monthly credits on its [Free plan](https://www.helius.dev/pricing),
but the account's actual credit usage and per-method billing were not inspected.
The batch optimization does not prove that continuous operation fits the free
allowance. Raising scan frequency or cohort size without measuring that usage
would be premature. The next engineering step is to measure actual per-method
RPC use, then schedule cohort audits within a verified free-tier budget.

Validation: the focused audit/paper tests covered valid, malformed and failed
batch responses and authority rejection. The complete pytest suite passed after
updating the worker-summary contract; Ruff lint and formatting, mypy, and wheel
build passed. The scheduled dashboard restarted cleanly from an online SQLite
backup, returned HTTP 200, and retained the active paper campaign. Live
execution remains locked in code and `LIVE_TRADING_ENABLED=false`.

## 2026-11-10: measured RPC history budget reduction

Completed paper-worker cycles now persist outbound Solana RPC attempts by
method, including retries, in `worker_cycle_history.rpc_requests_json`.
An additive migration on a 343-cycle copy preserved every historical cycle;
the running journal migrated on dashboard restart. These are request counts,
not the provider's billed credits or account quota balance.

Two measured scans before origin-history prioritization used 350 and 356 RPC
requests. The initial cache scans used 314 and 332 requests; they still
retraced many mints because one unavailable archival transaction prevented a
negative cache entry. A read-only audit of one such mint confirmed a history
window older than the bounded 100 signatures, one unreadable transaction, and
no verified creation signer. Its current top-20 holder/controller analysis was
available, but origin attribution remained partial.

The narrow fix caches an inconclusive creation-history search for one hour and
defers that archival search for tokens below the configured market liquidity
or 24-hour volume thresholds. A token is retraced when it becomes market
eligible, and a verified creation signer, if present, is reused while its
**current** holder balance is recalculated on every audit. Mint/freeze checks,
largest token accounts, controller grouping, and concentration limits still
run each scan. The creation-signer concentration finding uses the same
controller amount and threshold as the broader controller-concentration gate,
so deferring attribution does not bypass that blocking concentration gate.
The audit explicitly records deferred origin coverage as a limitation.

The first 38-token scan after deploying this rule completed at 21:31:41 UTC:
**218 RPC attempts**, versus **332** in the preceding 38-token scan, a
**34% reduction**. `getSignaturesForAddress` plus `getTransaction` fell from
216 to 102 attempts, a **53% reduction**. The new scan had 28 safe audits,
10 blocked audits and no worker error. This is one comparable cycle, so
sustained credit savings remain unverified; the Helius dashboard's actual
credit usage has not been inspected. The paper campaign was active with four
monitored open positions and no completed trades. No net expectancy or
live-readiness claim follows from this infrastructure improvement.

The complete pytest suite, Ruff lint/format checks, mypy, and wheel build
passed. A fresh online SQLite backup preceded deployment. The scheduled
dashboard returned HTTP 200 after restart; the exit monitor and watchdog had
recent successful cycles. Live execution remains code-locked and the local
environment keeps `LIVE_TRADING_ENABLED=false`.
