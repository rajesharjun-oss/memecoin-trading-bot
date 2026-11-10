from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RiskFinding:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class HolderControllerEvidence:
    token_account: str
    controller: str | None
    amount_atomic: int
    supply_bps: int
    classification: str
    controller_program: str | None = None
    evidence: str | None = None


@dataclass(frozen=True, slots=True)
class ControllerCluster:
    controller: str
    token_account_count: int
    amount_atomic: int
    supply_bps: int
    classification: str


@dataclass(frozen=True, slots=True)
class OriginEvidence:
    creation_signer: str
    confidence: str
    signature: str
    slot: int | None
    block_time: int | None
    current_observed_bps: int
    history_complete: bool


@dataclass(frozen=True, slots=True)
class TokenForensics:
    status: str
    analyzed_accounts: int
    distinct_controllers: int
    raw_top10_bps: int
    adjusted_top10_bps: int
    recognized_vault_bps: int
    burn_bps: int
    largest_controller_bps: int
    holders: tuple[HolderControllerEvidence, ...] = field(default_factory=tuple)
    controller_clusters: tuple[ControllerCluster, ...] = field(default_factory=tuple)
    origin: OriginEvidence | None = None
    limitations: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TokenAudit:
    mint: str
    token_program: str
    decimals: int
    supply_atomic: int
    top10_concentration_bps: int
    findings: tuple[RiskFinding, ...] = field(default_factory=tuple)
    effective_top10_concentration_bps: int | None = None
    forensics: TokenForensics | None = None

    @property
    def passed(self) -> bool:
        return not any(finding.blocking for finding in self.findings)


@dataclass(frozen=True, slots=True)
class RouteQuote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_bps: int | None
    router: str | None
    fee_bps: int | None
    raw: dict[str, Any]
    api_version: str = "v2"


@dataclass(frozen=True, slots=True)
class TradeResult:
    signature: str | None
    simulated_units: int
    submitted: bool
    explorer_url: str | None
    output_ata_rent_lamports: int | None = None


@dataclass(frozen=True, slots=True)
class TradeReport:
    client_order_id: str
    audit: TokenAudit | None
    quote: RouteQuote
    result: TradeResult
    quote_requested_at: datetime | None = None
