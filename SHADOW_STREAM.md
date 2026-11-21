# Read-only swap-stream feasibility probe

This opt-in research command subscribes to Solana `logsSubscribe` for 1–3 exact
pool addresses. It compares received transaction signatures with bounded
`getSignaturesForAddress` backfill at `confirmed` commitment. It does not request
transactions, create candles, write to the journal, or affect the paper worker.
A pool mention is **not** proof that a swap occurred.

Install the pinned optional dependency: `pip install -c requirements.lock -e ".[dev,research]"`.
The command reads the existing `SOLANA_RPC_URL` from the process environment or
local `.env`; it accepts only the Helius mainnet host. Never paste the URL into
chat, logs, or shell history. Run from the project directory, for example:

```text
python -m memecoin_bot.shadow_swap_stream --pool PUBLIC_POOL_ADDRESS --seconds 120
```

For one verified PumpSwap SOL/token pool, add `--mint PUBLIC_TOKEN_MINT`.
This reads the pool account and mint decimals, then decodes only the buy/sell
event prefix defined by [Pump's official IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json).
It checks each sufficiently old event price against a single GeckoTerminal
`currency=token` 1m OHLCV request. Pool orientation matters: SOL may be either
the base or quote asset. The result is a **provisional event-price check**, not
validated candles; open/close ordering, trade completeness, USD volume, and
later provider revisions remain unproven. The optional check adds two Solana
RPC reads and one GeckoTerminal request.

The probe stops after 300 seconds, 2 MB of WebSocket data, or 2,000 events. It
The basic mode makes at most seven HTTP RPC calls per pool: two anchors and five reference
pages. The output contains counts and public pool addresses only. An incomplete
reference window or an empty reference set produces undefined coverage. A
WebSocket disconnect fails the trial; it is never treated as complete coverage.
The stream credit figure is an estimate from received message bytes, not a
Helius bill or a measure of all network overhead.

Validate trade classification and 1m prices across sustained windows before
using any derived candles for paper decisions. Keep the worker's existing
candle gates.
