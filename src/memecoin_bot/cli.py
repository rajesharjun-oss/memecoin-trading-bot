from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from .alerts import run_persisted_alert_cycle, watch_alerts
from .audit import audit_token
from .config import ConfigurationError, Settings
from .constants import LAMPORTS_PER_SOL, LIVE_RISK_PHRASE
from .discovery import DiscoveryError, discover_recent_mints, unique_mints_from_file
from .journal import JournalError, TradeJournal
from .jupiter import JupiterClient, JupiterError
from .ohlcv import GeckoTerminalOhlcvClient, OhlcvError
from .paper_campaign import PaperCampaignError, create_forward_paper_run
from .paper_health import paper_worker_health
from .paper_identity import ensure_paper_identity
from .position import PositionEvent, monitor_position
from .research_history import backfill_research_candles
from .rpc import RpcClient, RpcError
from .service import RiskRejectedError, TradingService, audit_as_dict
from .strategy_lab import RESEARCH_SELECTION_METHOD
from .telemetry import configure_paper_telemetry, telemetry_report
from .transaction import SubmissionUncertainError, TransactionError
from .wallet import WalletError, load_keypair


def _json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=True), file=stream, flush=True)


def _sol_to_lamports(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("SOL amount must be numeric") from exc
    lamports = amount * LAMPORTS_PER_SOL
    if amount <= 0 or lamports != lamports.to_integral_value():
        raise argparse.ArgumentTypeError("SOL amount must be positive with at most 9 decimals")
    return int(lamports)


def _settings(path: str | None) -> Settings:
    return Settings.from_env(Path(path).resolve() if path else None)


def _load_mints(values: list[str], file_path: str | None) -> list[str]:
    result = list(dict.fromkeys(values))
    if file_path:
        with Path(file_path).resolve().open(encoding="utf-8") as handle:
            result.extend(unique_mints_from_file(handle))
    result = list(dict.fromkeys(result))
    if not result:
        raise DiscoveryError("Provide at least one mint or --file")
    if len(result) > 500:
        raise DiscoveryError("A scan is limited to 500 mints")
    return result


def _service(stack: ExitStack, settings: Settings) -> TradingService:
    if settings.keypair_path is None:
        raise ConfigurationError("SOLANA_KEYPAIR_PATH is required for trade commands")
    keypair = load_keypair(settings.keypair_path)
    rpc = stack.enter_context(RpcClient(settings.rpc_url))
    jupiter = stack.enter_context(JupiterClient(settings.jupiter_api_key))
    journal = stack.enter_context(TradeJournal(settings.state_path))
    return TradingService(
        settings=settings, rpc=rpc, jupiter=jupiter, keypair=keypair, journal=journal
    )


def _trade_json(report: Any) -> dict[str, Any]:
    return {
        "client_order_id": report.client_order_id,
        "mode": "live" if report.result.submitted else "dry_run",
        "audit": audit_as_dict(report.audit) if report.audit else None,
        "quote": {
            "input_mint": report.quote.input_mint,
            "output_mint": report.quote.output_mint,
            "in_amount": report.quote.in_amount,
            "out_amount": report.quote.out_amount,
            "price_impact_bps": report.quote.price_impact_bps,
            "router": report.quote.router,
            "fee_bps": report.quote.fee_bps,
        },
        "result": asdict(report.result),
    }


def _cmd_check(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    checks: dict[str, Any] = {
        "rpc_url_configured": bool(settings.rpc_url),
        "jupiter_api_key_configured": bool(settings.jupiter_api_key),
        "live_trading_enabled": False,
        "state_parent_exists": settings.state_path.parent.exists(),
    }
    if settings.keypair_path:
        keypair = load_keypair(settings.keypair_path)
        checks["wallet"] = str(keypair.pubkey())
        checks["keypair_loaded"] = True
    else:
        checks["keypair_loaded"] = False
    with RpcClient(settings.rpc_url) as rpc:
        checks["rpc_health"] = rpc.call("getHealth", [], attempts=1)
    _json(checks)
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    with RpcClient(settings.rpc_url) as rpc:
        result = audit_token(
            rpc,
            args.mint,
            max_top10_concentration_bps=settings.max_top10_concentration_bps,
            allow_token_2022=settings.allow_token_2022,
        )
    _json(audit_as_dict(result))
    return 0 if result.passed else 2


def _cmd_scan(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    mints = _load_mints(args.mint, args.file)
    failures = 0
    with RpcClient(settings.rpc_url) as rpc:
        for mint in mints:
            try:
                result = audit_token(
                    rpc,
                    mint,
                    max_top10_concentration_bps=settings.max_top10_concentration_bps,
                    allow_token_2022=settings.allow_token_2022,
                )
                failures += int(not result.passed)
                _json(audit_as_dict(result))
            except Exception as exc:
                failures += 1
                _json(
                    {"mint": mint, "passed": False, "error": type(exc).__name__},
                    stream=sys.stderr,
                )
    return 0 if failures == 0 else 2


def _cmd_discover(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    with RpcClient(settings.rpc_url) as rpc:
        mints = discover_recent_mints(
            rpc,
            args.program_id,
            signatures_per_program=args.signatures,
            max_transactions=args.max_transactions,
        )
    _json(
        {
            "candidate_count": len(mints),
            "candidates": mints,
            "warning": (
                "Candidates are not guaranteed to be new or safe; run scan/audit before trading."
            ),
        }
    )
    return 0


def _cmd_buy(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    with ExitStack() as stack:
        service = _service(stack, settings)
        report = service.buy(
            mint=args.mint,
            lamports=args.amount_sol,
            slippage_bps=args.slippage_bps,
            execute=args.execute,
            acknowledgement=args.acknowledge_risk,
            client_order_id=args.client_order_id,
        )
    _json(_trade_json(report))
    return 0


def _cmd_sell(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    with ExitStack() as stack:
        service = _service(stack, settings)
        amount = service.wallet_balance(args.mint) if args.all else args.amount_atomic
        if not amount:
            raise RiskRejectedError("No token balance is available to sell")
        report = service.sell(
            mint=args.mint,
            amount_atomic=amount,
            slippage_bps=args.slippage_bps,
            execute=args.execute,
            acknowledgement=args.acknowledge_risk,
            client_order_id=args.client_order_id,
        )
    _json(_trade_json(report))
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    with ExitStack() as stack:
        service = _service(stack, settings)
        journal = service.journal
        initial_amount = args.initial_token_amount or service.wallet_balance(args.mint)
        position_id = args.position_id or f"{args.mint[:12]}-{str(service.keypair.pubkey())[:12]}"

        def emit(event: PositionEvent) -> None:
            _json({"position_id": position_id, **asdict(event)})

        monitor_position(
            service,
            journal,
            position_id=position_id,
            mint=args.mint,
            entry_lamports=args.entry_sol,
            initial_token_amount=initial_amount,
            take_profit_bps=args.take_profit_bps,
            stop_loss_bps=args.stop_loss_bps,
            take_profit_sell_bps=args.take_profit_sell_bps,
            slippage_bps=args.slippage_bps,
            poll_seconds=args.poll_seconds,
            execute=args.execute,
            acknowledgement=args.acknowledge_risk,
            once=args.once,
            emit=emit,
        )
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    if args.cycles < 0:
        raise RiskRejectedError("cycles must be zero or greater")
    if not 5 <= args.poll_seconds <= 3_600:
        raise RiskRejectedError("poll-seconds must be between 5 and 3600")
    if not 1 <= args.max_candidates_per_cycle <= 50:
        raise RiskRejectedError("max-candidates-per-cycle must be between 1 and 50")
    if not 1 <= args.max_live_trades <= 100:
        raise RiskRejectedError("max-live-trades must be between 1 and 100")
    settings = _settings(args.env_file)
    seen: set[str] = set()
    trades = 0
    cycle = 0
    with ExitStack() as stack:
        service = _service(stack, settings)
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            candidates = discover_recent_mints(
                service.rpc,
                args.program_id,
                signatures_per_program=args.signatures,
                max_transactions=args.max_transactions,
            )
            new_candidates = [mint for mint in candidates if mint not in seen]
            for mint in new_candidates[: args.max_candidates_per_cycle]:
                if mint in seen:
                    continue
                seen.add(mint)
                try:
                    report = service.buy(
                        mint=mint,
                        lamports=args.amount_sol,
                        slippage_bps=args.slippage_bps,
                        execute=args.execute,
                        acknowledgement=args.acknowledge_risk,
                    )
                    _json({"event": "candidate_trade", **_trade_json(report)})
                    trades += int(report.result.submitted)
                    if args.execute and trades >= args.max_live_trades:
                        return 0
                except Exception as exc:
                    _json(
                        {"event": "candidate_rejected", "mint": mint, "error": type(exc).__name__},
                        stream=sys.stderr,
                    )
            if args.cycles == 0 or cycle < args.cycles:
                time.sleep(args.poll_seconds)
    return 0


def _cmd_research_backfill(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    raw_provider = getattr(args, "provider", "geckoterminal")
    if raw_provider not in ("geckoterminal", "coingecko-demo"):
        raise ValueError("Unsupported research backfill provider")
    provider: Literal["geckoterminal", "coingecko-demo"] = (
        "coingecko-demo" if raw_provider == "coingecko-demo" else "geckoterminal"
    )
    demo_api_key = getattr(settings, "coingecko_demo_api_key", None)
    if provider == "coingecko-demo" and not demo_api_key:
        raise ValueError("COINGECKO_DEMO_API_KEY is required for CoinGecko Demo backfill")
    with TradeJournal(settings.state_path) as journal:
        candidate = journal.discovery_candidate(args.mint)
        if candidate is None or candidate.get("pair_address") != args.pool:
            raise ValueError("Mint and pool must match a saved research candidate")
        windows = journal.research_entry_windows(
            args.mint, selection_method=RESEARCH_SELECTION_METHOD
        )
        if (
            not windows
            or args.start < min(start for start, _ in windows)
            or args.end > max(end for _, end in windows) + settings.paper_max_hold_seconds
        ):
            raise ValueError("Backfill must stay within the saved research and exit horizon")
        with GeckoTerminalOhlcvClient(
            provider=provider,
            demo_api_key=demo_api_key,
        ) as source:
            result = backfill_research_candles(
                journal,
                source,
                mint=args.mint,
                pool=args.pool,
                start=args.start,
                end=args.end,
                limit=args.limit,
                max_requests=args.max_requests,
            )
    _json(
        {
            "research_only": True,
            "provider": provider,
            "mint": args.mint,
            "pool": args.pool,
            **asdict(result),
        }
    )
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import run_dashboard

    settings = _settings(args.env_file)
    if args.free_only and (settings.live_trading_enabled or settings.narrative_openai_enabled):
        raise ConfigurationError(
            "Free-only dashboard requires LIVE_TRADING_ENABLED=false and "
            "NARRATIVE_OPENAI_ENABLED=false"
        )
    if not settings.live_trading_enabled:
        configure_paper_telemetry(settings.state_path)
    run_dashboard(settings, port=args.port, open_browser=not args.no_browser)
    return 0


def _cmd_alerts(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    if args.once:
        _json(run_persisted_alert_cycle(settings))
    else:
        _json({"alerts": "watching" if settings.alerts_enabled else "disabled"})
        watch_alerts(settings, interval_seconds=args.interval_seconds)
    return 0


def _cmd_paper_health(args: argparse.Namespace) -> int:
    healthy, issues = paper_worker_health(_settings(args.env_file))
    _json({"healthy": healthy, "issues": issues})
    return 0 if healthy else 1


def _cmd_telemetry_report(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    if settings.live_trading_enabled:
        raise ConfigurationError("Telemetry report requires paper mode")
    _json(telemetry_report(settings.state_path, args.hours))
    return 0


def _cmd_init_paper_identity(args: argparse.Namespace) -> int:
    created = ensure_paper_identity(_settings(args.env_file))
    _json({"paper_only": True, "created": created})
    return 0


def _cmd_new_paper_run(args: argparse.Namespace) -> int:
    settings = _settings(args.env_file)
    result = create_forward_paper_run(settings.state_path, Path(args.output))
    _json({"paper_only": True, "source_unchanged": True, **result})
    return 0


def _add_live_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true", help="submit live instead of dry-run")
    parser.add_argument("--acknowledge-risk", help=f"must equal {LIVE_RISK_PHRASE} in live mode")
    parser.add_argument("--client-order-id", help="unique idempotency key for this order")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memecoin-bot",
        description="Safe-by-default Solana token scanner and Jupiter Swap V2 executor",
    )
    parser.add_argument("--env-file", help="path to a dotenv file (default: .env)")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="validate local configuration and RPC health")
    check.set_defaults(func=_cmd_check)

    audit = commands.add_parser("audit", help="audit one SPL mint")
    audit.add_argument("--mint", required=True)
    audit.set_defaults(func=_cmd_audit)

    scan = commands.add_parser("scan", help="audit a bounded list of SPL mints")
    scan.add_argument("--mint", action="append", default=[])
    scan.add_argument("--file", help="UTF-8 file containing one mint per line")
    scan.set_defaults(func=_cmd_scan)

    discover = commands.add_parser(
        "discover", help="find candidate mints in recent program traffic"
    )
    discover.add_argument("--program-id", action="append", required=True)
    discover.add_argument("--signatures", type=int, default=20)
    discover.add_argument("--max-transactions", type=int, default=100)
    discover.set_defaults(func=_cmd_discover)

    buy = commands.add_parser("buy", help="audit, quote, simulate, and optionally buy a token")
    buy.add_argument("--mint", required=True)
    buy.add_argument("--amount-sol", type=_sol_to_lamports, required=True)
    buy.add_argument("--slippage-bps", type=int, default=100)
    _add_live_flags(buy)
    buy.set_defaults(func=_cmd_buy)

    sell = commands.add_parser("sell", help="quote, simulate, and optionally sell a token")
    sell.add_argument("--mint", required=True)
    amount = sell.add_mutually_exclusive_group(required=True)
    amount.add_argument("--amount-atomic", type=int)
    amount.add_argument("--all", action="store_true")
    sell.add_argument("--slippage-bps", type=int, default=100)
    _add_live_flags(sell)
    sell.set_defaults(func=_cmd_sell)

    monitor = commands.add_parser("monitor", help="monitor one position and enforce TP/SL")
    monitor.add_argument("--mint", required=True)
    monitor.add_argument("--entry-sol", type=_sol_to_lamports, required=True)
    monitor.add_argument("--initial-token-amount", type=int)
    monitor.add_argument("--position-id")
    monitor.add_argument("--take-profit-bps", type=int, default=20_000)
    monitor.add_argument("--stop-loss-bps", type=int, default=6_000)
    monitor.add_argument("--take-profit-sell-bps", type=int, default=5_000)
    monitor.add_argument("--slippage-bps", type=int, default=100)
    monitor.add_argument("--poll-seconds", type=int, default=15)
    monitor.add_argument("--once", action="store_true")
    _add_live_flags(monitor)
    monitor.set_defaults(func=_cmd_monitor)

    watch = commands.add_parser(
        "watch", help="continuously discover, audit, and simulate candidates"
    )
    watch.add_argument("--program-id", action="append", required=True)
    watch.add_argument("--amount-sol", type=_sol_to_lamports, required=True)
    watch.add_argument("--signatures", type=int, default=10)
    watch.add_argument("--max-transactions", type=int, default=50)
    watch.add_argument("--max-candidates-per-cycle", type=int, default=10)
    watch.add_argument("--slippage-bps", type=int, default=100)
    watch.add_argument("--poll-seconds", type=int, default=30)
    watch.add_argument("--cycles", type=int, default=1, help="0 means run until interrupted")
    watch.add_argument("--max-live-trades", type=int, default=1)
    _add_live_flags(watch)
    watch.set_defaults(func=_cmd_watch)

    backfill = commands.add_parser(
        "research-backfill", help="retrieve bounded historical 1m pool outcomes for research"
    )
    backfill.add_argument("--mint", required=True)
    backfill.add_argument("--pool", required=True)
    backfill.add_argument("--start", type=int, required=True, help="inclusive Unix open timestamp")
    backfill.add_argument("--end", type=int, required=True, help="exclusive Unix open timestamp")
    backfill.add_argument("--limit", type=int, default=500)
    backfill.add_argument("--max-requests", type=int, default=3)
    backfill.add_argument(
        "--provider", choices=("geckoterminal", "coingecko-demo"), default="geckoterminal"
    )
    backfill.set_defaults(func=_cmd_research_backfill)

    dashboard = commands.add_parser("dashboard", help="launch the secure local trading dashboard")
    dashboard.add_argument("--port", type=int, default=8787)
    dashboard.add_argument("--no-browser", action="store_true")
    dashboard.add_argument(
        "--free-only",
        action="store_true",
        help="refuse startup if live mode or paid OpenAI research is enabled",
    )
    dashboard.set_defaults(func=_cmd_dashboard)
    alerts = commands.add_parser(
        "alerts", help="watch paper-worker health and send optional alerts"
    )
    alerts.add_argument("--once", action="store_true", help="check worker health once and exit")
    alerts.add_argument("--interval-seconds", type=int, default=60)
    alerts.set_defaults(func=_cmd_alerts)
    health = commands.add_parser("paper-health", help="check unattended paper-worker freshness")
    health.set_defaults(func=_cmd_paper_health)
    telemetry = commands.add_parser(
        "telemetry-report", help="summarize bounded paper data-feed telemetry"
    )
    telemetry.add_argument("--hours", type=int, required=True)
    telemetry.set_defaults(func=_cmd_telemetry_report)
    identity = commands.add_parser(
        "init-paper-identity", help="create an unfunded identity in the paper-state volume"
    )
    identity.set_defaults(func=_cmd_init_paper_identity)
    new_run = commands.add_parser(
        "new-paper-run", help="create a fresh paper ledger with copied research observations"
    )
    new_run.add_argument("--output", required=True, help="new SQLite journal path")
    new_run.set_defaults(func=_cmd_new_paper_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SubmissionUncertainError as exc:
        _json(
            {
                "error": type(exc).__name__,
                "message": str(exc),
                "signature": exc.signature,
                "explorer_url": f"https://solscan.io/tx/{exc.signature}",
            },
            stream=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        _json({"error": "Interrupted", "message": "Stopped by user"}, stream=sys.stderr)
        return 130
    except (
        ConfigurationError,
        DiscoveryError,
        JournalError,
        JupiterError,
        OhlcvError,
        PaperCampaignError,
        RiskRejectedError,
        RpcError,
        TransactionError,
        ValueError,
        WalletError,
    ) as exc:
        _json({"error": type(exc).__name__, "message": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
