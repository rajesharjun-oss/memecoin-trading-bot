from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from .audit import audit_token, blocking_messages
from .config import Settings
from .constants import LIVE_EXECUTION_LOCKED, LIVE_RISK_PHRASE, WSOL_MINT
from .forensics import derive_associated_token_account
from .journal import TradeJournal
from .jupiter import JupiterClient
from .models import RouteQuote, TokenAudit, TradeReport
from .rpc import RpcClient
from .transaction import SubmissionUncertainError, build_simulate_and_submit


class RiskRejectedError(RuntimeError):
    """Raised when a policy gate rejects a proposed trade."""


ORIGIN_NEGATIVE_RETRY_SECONDS = 3_600


def token_balance_atomic(rpc: RpcClient, owner: str, mint: str) -> int:
    for value, label in ((owner, "owner"), (mint, "mint")):
        try:
            Pubkey.from_string(value)
        except ValueError as exc:
            raise RiskRejectedError(f"Invalid {label} public key") from exc
    result = rpc.call(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    accounts = result.get("value") if isinstance(result, dict) else None
    if not isinstance(accounts, list):
        raise RiskRejectedError("RPC returned malformed token-account data")
    total = 0
    for account in accounts:
        try:
            amount = account["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
            atomic = int(amount)
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskRejectedError("RPC returned a malformed token balance") from exc
        if atomic < 0:
            raise RiskRejectedError("RPC returned a negative token balance")
        total += atomic
    return total


class TradingService:
    def __init__(
        self,
        *,
        settings: Settings,
        rpc: RpcClient,
        jupiter: JupiterClient,
        keypair: Keypair,
        journal: TradeJournal,
    ) -> None:
        self.settings = settings
        self.rpc = rpc
        self.jupiter = jupiter
        self.keypair = keypair
        self.journal = journal

    def audit(
        self,
        mint: str,
        *,
        mint_account: dict[str, Any] | None = None,
        origin_trace_enabled: bool = True,
    ) -> TokenAudit:
        trace_origin = (
            self.settings.token_forensics_enabled and self.settings.forensics_origin_trace_enabled
        )
        cached_origin = None
        trace_deferred = False
        if trace_origin:
            cached = self.journal.origin_trace_cache(mint)
            if cached is not None:
                checked_at_us, cached_origin = cached
                if cached_origin is not None:
                    trace_origin = False
                elif not origin_trace_enabled or (
                    0
                    <= datetime.now(UTC).timestamp() * 1_000_000 - checked_at_us
                    < (ORIGIN_NEGATIVE_RETRY_SECONDS * 1_000_000)
                ):
                    trace_origin = False
                    trace_deferred = True
            elif not origin_trace_enabled:
                trace_origin = False
                trace_deferred = True
        audit = audit_token(
            self.rpc,
            mint,
            mint_account=mint_account,
            cached_origin=cached_origin,
            origin_trace_deferred=trace_deferred,
            max_top10_concentration_bps=self.settings.max_top10_concentration_bps,
            allow_token_2022=self.settings.allow_token_2022,
            forensics_enabled=self.settings.token_forensics_enabled,
            origin_trace_enabled=trace_origin,
            max_single_controller_bps=self.settings.forensics_max_single_controller_bps,
            forensics_signature_limit=self.settings.forensics_signature_limit,
            forensics_transaction_candidates=self.settings.forensics_transaction_candidates,
        )
        if trace_origin and audit.forensics is not None:
            origin = audit.forensics.origin
            if origin is not None or (
                "No parsed mint-initialization transaction was found in the bounded lookback"
                in audit.forensics.limitations
            ):
                self.journal.save_origin_trace_cache(mint, origin)
        return audit

    def _quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int
    ) -> RouteQuote:
        quote = self.jupiter.quote(input_mint, output_mint, amount, slippage_bps)
        if (
            quote.api_version == "v1"
            and quote.price_impact_bps is not None
            and quote.price_impact_bps > self.settings.max_price_impact_bps
        ):
            raise RiskRejectedError(
                f"Price impact {quote.price_impact_bps / 100:.2f}% exceeds configured limit"
            )
        return quote

    def _live_gate(self, *, execute: bool, acknowledgement: str | None) -> bool:
        if not execute:
            return False
        if LIVE_EXECUTION_LOCKED:
            raise RiskRejectedError("Live execution is locked during paper strategy validation")
        if not self.settings.live_trading_enabled:
            raise RiskRejectedError("LIVE_TRADING_ENABLED is false")
        if acknowledgement != LIVE_RISK_PHRASE:
            raise RiskRejectedError(f"Live mode requires --acknowledge-risk {LIVE_RISK_PHRASE}")
        if not self.settings.jupiter_api_key:
            raise RiskRejectedError("JUPITER_API_KEY is required for live trading")
        return True

    def trade(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        slippage_bps: int,
        execute: bool,
        acknowledgement: str | None,
        client_order_id: str | None = None,
        audit_output: bool,
        capture_output_ata_rent: bool = False,
    ) -> TradeReport:
        live = self._live_gate(execute=execute, acknowledgement=acknowledgement)
        if amount_atomic <= 0:
            raise RiskRejectedError("Trade amount must be positive")
        if not 1 <= slippage_bps <= self.settings.max_slippage_bps:
            raise RiskRejectedError(
                f"Slippage must be 1-{self.settings.max_slippage_bps} basis points"
            )
        if input_mint == WSOL_MINT and amount_atomic > self.settings.max_trade_lamports:
            raise RiskRejectedError("Trade exceeds MAX_TRADE_LAMPORTS")

        audit: TokenAudit | None = None
        if audit_output:
            audit = self.audit(output_mint)
            if not audit.passed:
                messages = "; ".join(blocking_messages(audit.findings))
                raise RiskRejectedError("Token audit failed: " + messages)
        if capture_output_ata_rent and (not audit_output or audit is None):
            raise RiskRejectedError("Output token-account reserve requires a passing output audit")

        quote_requested_at = datetime.now(UTC)
        quote = self._quote(input_mint, output_mint, amount_atomic, slippage_bps)
        order_id = client_order_id or str(uuid.uuid4())
        reserved = False
        if live:
            self.journal.reserve(
                client_order_id=order_id,
                input_mint=input_mint,
                output_mint=output_mint,
                input_amount=amount_atomic,
                max_daily_lamports=self.settings.max_daily_lamports,
            )
            reserved = True

        try:
            if quote.api_version == "v2":
                quote_requested_at = datetime.now(UTC)
            build = self.jupiter.build(
                input_mint=input_mint,
                output_mint=output_mint,
                amount=amount_atomic,
                taker=str(self.keypair.pubkey()),
                slippage_bps=slippage_bps,
                quote=quote,
                max_priority_fee_lamports=self.settings.max_priority_fee_lamports,
            )
            if quote.api_version == "v2":
                quote = self.jupiter.quote_from_build(
                    build,
                    input_mint=input_mint,
                    output_mint=output_mint,
                    amount=amount_atomic,
                    slippage_bps=slippage_bps,
                )
                if (
                    quote.price_impact_bps is not None
                    and quote.price_impact_bps > self.settings.max_price_impact_bps
                ):
                    raise RiskRejectedError("Built route price impact exceeds the configured limit")
            result = build_simulate_and_submit(
                self.rpc,
                self.keypair,
                build,
                max_priority_fee_lamports=self.settings.max_priority_fee_lamports,
                submit=live,
                confirmation_timeout_seconds=self.settings.confirmation_timeout_seconds,
                output_ata=(
                    derive_associated_token_account(
                        str(self.keypair.pubkey()), output_mint, audit.token_program
                    )
                    if capture_output_ata_rent and audit is not None
                    else None
                ),
                output_ata_program=(
                    audit.token_program if capture_output_ata_rent and audit is not None else None
                ),
            )
        except SubmissionUncertainError as exc:
            if reserved:
                self.journal.update(order_id, "submitted", signature=exc.signature)
            raise
        except Exception as exc:
            if reserved:
                self.journal.update(order_id, "failed", error=type(exc).__name__)
            raise
        if reserved:
            self.journal.update(order_id, "confirmed", signature=result.signature)
        return TradeReport(order_id, audit, quote, result, quote_requested_at)

    def buy(
        self,
        *,
        mint: str,
        lamports: int,
        slippage_bps: int,
        execute: bool = False,
        acknowledgement: str | None = None,
        client_order_id: str | None = None,
        capture_output_ata_rent: bool = False,
    ) -> TradeReport:
        return self.trade(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount_atomic=lamports,
            slippage_bps=slippage_bps,
            execute=execute,
            acknowledgement=acknowledgement,
            client_order_id=client_order_id,
            audit_output=True,
            capture_output_ata_rent=capture_output_ata_rent,
        )

    def sell(
        self,
        *,
        mint: str,
        amount_atomic: int,
        slippage_bps: int,
        execute: bool = False,
        acknowledgement: str | None = None,
        client_order_id: str | None = None,
    ) -> TradeReport:
        return self.trade(
            input_mint=mint,
            output_mint=WSOL_MINT,
            amount_atomic=amount_atomic,
            slippage_bps=slippage_bps,
            execute=execute,
            acknowledgement=acknowledgement,
            client_order_id=client_order_id,
            audit_output=False,
        )

    def wallet_balance(self, mint: str) -> int:
        return token_balance_atomic(self.rpc, str(self.keypair.pubkey()), mint)


def audit_as_dict(audit: TokenAudit) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mint": audit.mint,
        "passed": audit.passed,
        "token_program": audit.token_program,
        "decimals": audit.decimals,
        "supply_atomic": audit.supply_atomic,
        "top10_concentration_bps": audit.top10_concentration_bps,
        "effective_top10_concentration_bps": audit.effective_top10_concentration_bps,
        "findings": [
            {"code": item.code, "message": item.message, "blocking": item.blocking}
            for item in audit.findings
        ],
    }
    if audit.forensics is not None:
        forensics = audit.forensics
        payload["forensics"] = {
            "status": forensics.status,
            "analyzed_accounts": forensics.analyzed_accounts,
            "distinct_controllers": forensics.distinct_controllers,
            "raw_top10_bps": forensics.raw_top10_bps,
            "adjusted_top10_bps": forensics.adjusted_top10_bps,
            "recognized_vault_bps": forensics.recognized_vault_bps,
            "burn_bps": forensics.burn_bps,
            "largest_controller_bps": forensics.largest_controller_bps,
            "holders": [
                {
                    "token_account": holder.token_account,
                    "controller": holder.controller,
                    "amount_atomic": holder.amount_atomic,
                    "supply_bps": holder.supply_bps,
                    "classification": holder.classification,
                    "controller_program": holder.controller_program,
                    "evidence": holder.evidence,
                }
                for holder in forensics.holders
            ],
            "controller_clusters": [
                {
                    "controller": cluster.controller,
                    "token_account_count": cluster.token_account_count,
                    "amount_atomic": cluster.amount_atomic,
                    "supply_bps": cluster.supply_bps,
                    "classification": cluster.classification,
                }
                for cluster in forensics.controller_clusters
            ],
            "origin": (
                {
                    "creation_signer": forensics.origin.creation_signer,
                    "confidence": forensics.origin.confidence,
                    "signature": forensics.origin.signature,
                    "slot": forensics.origin.slot,
                    "block_time": forensics.origin.block_time,
                    "current_observed_bps": forensics.origin.current_observed_bps,
                    "history_complete": forensics.origin.history_complete,
                }
                if forensics.origin is not None
                else None
            ),
            "limitations": list(forensics.limitations),
        }
    else:
        payload["forensics"] = None
    return payload
