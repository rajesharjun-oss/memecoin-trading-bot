# Strategy Lab visual direction

The user asked for a less noisy trading interface based on the supplied Pinterest search.
The existing dark MemeOps terminal remains the design system.

## References reviewed

- [Trading Terminal | Web App](https://www.pinterest.com/pin/1102256077588694224/):
  chart-led canvas, compact navigation, and a narrow contextual information rail.
- [Trading bot UI](https://www.pinterest.com/pin/596093700677905722/):
  grouped summary cards and a separate history table. Its many equal-weight cards would
  add noise to the live decision surface, so only the separation of summary and history
  was retained.

## Reference lock and decisions

| Decision | Source | Purpose |
| --- | --- | --- |
| Keep the existing dark chart-first layout and compact signal rail | Trading Terminal reference | Let the candle chart and entry state dominate the decision view. |
| Keep one persistent live-lock label in the application header | User's paper-only requirement | Preserve the safety status without repeating implementation flags in the page hero. |
| Show token, timeframe, and replay actions first | User's noise-reduction request | Make the primary research flow readable at a glance. |
| Put TP, sale portions, and stop loss in an accessible disclosure | User's noise-reduction request | Keep replay controls available without giving every field equal first-screen weight. |
| Leave evaluation and risk evidence below the chart | Trading bot UI reference and existing app | Retain auditability while separating it from the first decision. |

The disclosure uses native `details` and `summary`, opens when a hidden field is invalid,
and does not change the server-side strategy or live-execution lock.

An isolated browser QA session with a snapshot of the local paper database confirmed the
Discovery first screen and Strategy Lab hierarchy. The isolated worker was intentionally
stopped; it did not change the active paper worker. The CSS audit also replaced an
undefined `--accent` reference with the existing `--primary` token, restoring the
intended teal marks in radar, narrative and audit evidence.

## Discovery screen: attention without clutter

The same Pinterest search also showed [NextGet's terminal](https://www.pinterest.com/pin/597571444346676019/)
and the [Trading Terminal | Web App](https://www.pinterest.com/pin/1102256077588694224/).
Both put a small set of current decisions ahead of the surrounding market data. The
existing MemeOps dark palette and compact typography remain the visual foundation.

| Decision | Source | Purpose |
| --- | --- | --- |
| Move Call Inbox directly below the worker | User's call workflow | Make the primary action reachable without scrolling past news and long tables. |
| Show audited-safe candidates by default and label the gate precisely | Trading terminal hierarchy and paper safety model | Focus review while avoiding the false claim that audit means entry approval. |
| Keep all scanned candidates behind an explicit toggle | User's request to reduce noise | Preserve rejected-token evidence without filling the normal view. |
| Show five Hot Radar rows with an expand control | NextGet's compact market panel | Keep current attention visible without letting it dominate the page. |
| Collapse narrative evidence and decision history by default | Trading bot UI's summary/history separation | Preserve provenance and explainability on demand. |

The source images are design references, not trading logic. No entry gate, paper fill, or
live-execution setting changes with these UI edits.

## Paper portfolio: positions before controls

The portfolio now places equity and current positions ahead of the new-position form.
This applies the chart-first terminal principle to position management: show the state
that may require action before setup and secondary model detail. The execution-cost model
is an accessible disclosure below the form. Saved marks are labeled as estimates,
with old or missing marks distinguished from actual quote failures in the worker's
last completed cycle. The portfolio can refresh quotes explicitly for a current value.

## Overview: a paper decision desk

The [Trading Terminal pin](https://www.pinterest.com/pin/1102256077588694224/)
shows a strong central work area with a compact navigation rail and contextual secondary
detail. [Allegra's transparent trading dashboard](https://dribbble.com/shots/26873515-Allegra-AI-Transparent-Crypto-Trading-Analytics-Dashboard)
emphasizes visible decision logic. The exact Pinterest search was inaccessible without
sign-in in the isolated browser, so the inspected saved pin and the existing app were
used as the concrete references.

The MemeOps overview keeps its restrained dark palette and teal action accent. A single
focus panel explains the next review action from real journal and worker data. Position
capacity, marked equity, complete costed exits, and worker state follow as compact
context. The primary navigation leads to Discovery, Paper Portfolio and Strategy Lab.
Wallet readiness and live-order history remain accessible in native disclosures.
On narrow screens the primary navigation occupies its own row, so all four workspaces
remain visible and scrollable. No visual status is treated as entry approval.

## Overview noise reduction (2026-11-03)

The user-provided [Pinterest trading-bot search](https://www.pinterest.com/search/pins/?q=memecoin%20trading%20bot%20ui%20design&rs=typed) and the inspected
[Trading Terminal pin](https://www.pinterest.com/pin/1102256077588694224/)
support one dominant decision surface and compact context. The app's existing dark
palette, restrained teal action color, and narrow navigation rail remain the reference
lock. This pass moves the real worker-derived focus panel above the metrics, removes
the introductory slogan and repeated paper-only badge, and removes three workflow cards
that duplicated the primary navigation. The detailed evidence string is in a native
disclosure beside the focus, so it remains available without taking first-screen space.
The changes are presentation only; trading rules and the live-execution lock are unchanged.
