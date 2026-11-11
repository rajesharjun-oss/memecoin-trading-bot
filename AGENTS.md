# Agent rules for memecoin-trading-bot

## Non-negotiable
- Never change `LIVE_EXECUTION_LOCKED`, the live-trading gates in `service.py` and `dashboard.py`, or any code path that signs or submits a transaction. Do not add one.
- Never read, print, log, or commit secrets, keypairs, `.env`, SQLite databases, or backups.
- Do not relax strategy gates, candle-freshness rules, or fail-closed behaviour to create more trades.
- No new paid services or API keys. New integrations must be optional and off by default.

## Working style
- Make minimal, targeted edits. Do not refactor, rename, reformat, or reorganise code outside the task.
- Do not split or rewrite `journal.py`, `strategy_lab.py`, or `verification.py`.
- Match existing conventions: integer lamports, frozen dataclasses, strict typing.
- Add tests for every behaviour change.

## Before finishing any task, all of these must pass
ruff format --check . && ruff check . && mypy && pytest && pip-audit

## Reporting
List files changed, tests added, commands run with results, and anything you could not verify. Do not claim a check passed unless you ran it.
