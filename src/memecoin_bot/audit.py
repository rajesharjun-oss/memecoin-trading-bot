from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from solders.pubkey import Pubkey

from .constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from .forensics import analyze_token_forensics
from .models import OriginEvidence, RiskFinding, TokenAudit
from .rpc import RpcClient


class TokenAuditError(RuntimeError):
    """Raised when a mint cannot be parsed or audited."""


BLOCKED_TOKEN_2022_EXTENSIONS = {
    "confidentialtransfermint",
    "defaultaccountstate",
    "nontransferable",
    "permanentdelegate",
    "transferfeeconfig",
    "transferhook",
}
ALLOWED_TOKEN_2022_EXTENSIONS = {"metadatapointer", "tokenmetadata"}


def _token_2022_extension_findings(info: dict[str, Any], mint: str) -> list[RiskFinding]:
    raw = info.get("extensions")
    if not isinstance(raw, list):
        return [
            RiskFinding(
                "token_2022_extensions_unverified",
                "Token-2022 extension list is unavailable",
            )
        ]
    extensions: dict[str, dict[str, Any]] = {}
    for item in raw:
        name = item.get("extension") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name or name.lower() in extensions:
            return [
                RiskFinding(
                    "token_2022_extensions_unverified",
                    "Token-2022 extension list is malformed or duplicated",
                )
            ]
        extensions[name.lower()] = item

    findings: list[RiskFinding] = []
    blocked = sorted(extensions.keys() & BLOCKED_TOKEN_2022_EXTENSIONS)
    if blocked:
        findings.append(
            RiskFinding(
                "dangerous_extensions", "Blocked Token-2022 extensions: " + ", ".join(blocked)
            )
        )
    unsupported = sorted(extensions.keys() - ALLOWED_TOKEN_2022_EXTENSIONS - set(blocked))
    if unsupported:
        findings.append(
            RiskFinding(
                "unsupported_extensions",
                "Unreviewed Token-2022 extensions: " + ", ".join(unsupported),
            )
        )
    pointer = extensions.get("metadatapointer")
    if pointer is not None:
        state = pointer.get("state")
        if (
            not isinstance(state, dict)
            or state.get("authority") is not None
            or state.get("metadataAddress") != mint
        ):
            findings.append(
                RiskFinding(
                    "mutable_metadata_pointer",
                    "Metadata pointer must be immutable and point to the mint",
                )
            )
    metadata = extensions.get("tokenmetadata")
    if metadata is not None:
        state = metadata.get("state")
        if (
            pointer is None
            or not isinstance(state, dict)
            or state.get("updateAuthority") is not None
            or state.get("mint") != mint
        ):
            findings.append(
                RiskFinding(
                    "mutable_token_metadata",
                    "Token metadata must match the mint and have no update authority",
                )
            )
    return findings


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise TokenAuditError(f"Mint {field} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TokenAuditError(f"Mint {field} is invalid") from exc
    if parsed < 0:
        raise TokenAuditError(f"Mint {field} cannot be negative")
    return parsed


def audit_token(
    rpc: RpcClient,
    mint: str,
    *,
    mint_account: dict[str, Any] | None = None,
    cached_origin: OriginEvidence | None = None,
    origin_trace_deferred: bool = False,
    max_top10_concentration_bps: int,
    allow_token_2022: bool,
    forensics_enabled: bool = True,
    origin_trace_enabled: bool = True,
    max_single_controller_bps: int = 1_500,
    forensics_signature_limit: int = 100,
    forensics_transaction_candidates: int = 5,
) -> TokenAudit:
    try:
        Pubkey.from_string(mint)
    except ValueError as exc:
        raise TokenAuditError("Mint must be a valid base58 Solana public key") from exc

    if mint_account is None:
        account_result = rpc.call(
            "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        account = account_result.get("value") if isinstance(account_result, dict) else None
    else:
        account = mint_account
    if not isinstance(account, dict):
        raise TokenAuditError("Mint account was not found")

    owner = account.get("owner")
    if owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
        raise TokenAuditError("Account is not owned by an accepted SPL Token program")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        raise TokenAuditError("Account is not a parsed SPL mint")
    info = parsed.get("info")
    if not isinstance(info, dict):
        raise TokenAuditError("Mint response omitted parsed information")

    supply = _integer(info.get("supply"), "supply")
    decimals = _integer(info.get("decimals"), "decimals")
    if decimals > 18:
        raise TokenAuditError("Mint decimals exceed the supported safety limit")

    findings: list[RiskFinding] = []
    if supply == 0:
        findings.append(RiskFinding("zero_supply", "Token supply is zero"))
    if info.get("mintAuthority") is not None:
        findings.append(RiskFinding("mint_authority", "Mint authority is still active"))
    if info.get("freezeAuthority") is not None:
        findings.append(RiskFinding("freeze_authority", "Freeze authority is still active"))

    if owner == TOKEN_2022_PROGRAM_ID:
        if not allow_token_2022:
            findings.append(RiskFinding("token_2022_disabled", "Token-2022 mints are disabled"))
        findings.extend(_token_2022_extension_findings(info, mint))

    largest_result = rpc.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    largest = largest_result.get("value") if isinstance(largest_result, dict) else None
    if not isinstance(largest, list):
        raise TokenAuditError("Largest-account response was malformed")
    amounts: list[int] = []
    for item in largest[:10]:
        if not isinstance(item, dict):
            raise TokenAuditError("Largest-account entry was malformed")
        amounts.append(_integer(item.get("amount"), "holder amount"))
    top10_bps = min(10_000, (sum(amounts) * 10_000 // supply) if supply else 10_000)
    largest_context = largest_result.get("context") if isinstance(largest_result, dict) else None
    largest_slot = largest_context.get("slot") if isinstance(largest_context, dict) else None
    forensics = None
    effective_top10_bps = top10_bps
    if forensics_enabled:
        forensics = analyze_token_forensics(
            rpc,
            mint=mint,
            token_program=str(owner),
            supply=supply,
            largest_accounts=[item for item in largest if isinstance(item, dict)][:20],
            origin_trace_enabled=origin_trace_enabled,
            cached_origin=cached_origin,
            origin_trace_deferred=origin_trace_deferred,
            signature_limit=forensics_signature_limit,
            transaction_candidates=forensics_transaction_candidates,
            min_context_slot=(largest_slot if isinstance(largest_slot, int) else None),
        )
        if forensics.status != "unavailable" and forensics.recognized_vault_bps > 0:
            effective_top10_bps = forensics.adjusted_top10_bps
            findings.append(
                RiskFinding(
                    "recognized_vault_exclusion",
                    f"Excluded {forensics.recognized_vault_bps / 100:.2f}% held in exact, "
                    "deterministically derived Pump/PumpSwap vault accounts from controller "
                    "concentration; raw concentration remains visible",
                    blocking=False,
                )
            )

    if effective_top10_bps > max_top10_concentration_bps:
        findings.append(
            RiskFinding(
                "holder_concentration",
                f"Effective top token accounts hold {effective_top10_bps / 100:.2f}% of supply; "
                f"limit is {max_top10_concentration_bps / 100:.2f}%",
            )
        )

    if forensics is None or forensics.status == "unavailable":
        findings.append(
            RiskFinding(
                "identity_limit",
                "Raw token-account concentration could not be enriched with controller identity "
                "or exact vault evidence",
                blocking=False,
            )
        )
    else:
        if forensics.largest_controller_bps > max_single_controller_bps:
            findings.append(
                RiskFinding(
                    "controller_concentration",
                    f"One observed non-vault controller holds "
                    f"{forensics.largest_controller_bps / 100:.2f}% of adjusted supply; limit "
                    f"is {max_single_controller_bps / 100:.2f}%",
                )
            )
        material_clusters = [
            cluster for cluster in forensics.controller_clusters if cluster.supply_bps >= 100
        ]
        if material_clusters:
            largest_cluster = material_clusters[0]
            findings.append(
                RiskFinding(
                    "related_token_accounts",
                    f"A single controller owns {largest_cluster.token_account_count} top token "
                    f"accounts totaling {largest_cluster.supply_bps / 100:.2f}% of adjusted supply",
                    blocking=False,
                )
            )
        if forensics.origin is not None:
            origin = forensics.origin
            findings.append(
                RiskFinding(
                    "creation_signer",
                    f"Parsed mint initialization links the creation signer "
                    f"{origin.creation_signer} to {origin.current_observed_bps / 100:.2f}% of "
                    "currently observed top-account supply",
                    blocking=origin.current_observed_bps > max_single_controller_bps,
                )
            )
        if forensics.limitations:
            findings.append(
                RiskFinding(
                    "forensics_limit",
                    f"Forensic coverage is {forensics.status}: {forensics.limitations[0]}",
                    blocking=False,
                )
            )
    return TokenAudit(
        mint=mint,
        token_program=str(owner),
        decimals=decimals,
        supply_atomic=supply,
        top10_concentration_bps=top10_bps,
        findings=tuple(findings),
        effective_top10_concentration_bps=effective_top10_bps,
        forensics=forensics,
    )


def blocking_messages(findings: Iterable[RiskFinding]) -> list[str]:
    return [finding.message for finding in findings if finding.blocking]
