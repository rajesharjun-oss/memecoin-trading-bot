from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any

from solders.pubkey import Pubkey

from .constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    PUMP_AMM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    RECOGNIZED_LIQUIDITY_PROGRAM_IDS,
    SOLANA_INCINERATOR_ADDRESS,
    SYSTEM_PROGRAM_ID,
    WSOL_MINT,
)
from .models import (
    ControllerCluster,
    HolderControllerEvidence,
    OriginEvidence,
    TokenForensics,
)
from .rpc import RpcClient, RpcError


def _bps(amount: int, supply: int) -> int:
    return min(10_000, amount * 10_000 // supply) if supply > 0 else 10_000


def derive_associated_token_account(owner: str, mint: str, token_program: str) -> str:
    """Derive the canonical ATA without trusting an external indexer."""
    owner_key = Pubkey.from_string(owner)
    mint_key = Pubkey.from_string(mint)
    token_key = Pubkey.from_string(token_program)
    associated_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
    address, _ = Pubkey.find_program_address(
        [bytes(owner_key), bytes(token_key), bytes(mint_key)], associated_program
    )
    return str(address)


def recognized_pump_vaults(mint: str, token_program: str) -> dict[str, str]:
    """Return vault addresses whose derivations are published in Pump's official docs."""
    mint_key = Pubkey.from_string(mint)
    pump_program = Pubkey.from_string(PUMP_PROGRAM_ID)
    pump_amm_program = Pubkey.from_string(PUMP_AMM_PROGRAM_ID)

    bonding_curve, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_key)], pump_program
    )
    bonding_vault = derive_associated_token_account(str(bonding_curve), mint, token_program)

    pool_authority, _ = Pubkey.find_program_address(
        [b"pool-authority", bytes(mint_key)], pump_program
    )
    canonical_pool, _ = Pubkey.find_program_address(
        [
            b"pool",
            (0).to_bytes(2, "little"),
            bytes(pool_authority),
            bytes(mint_key),
            bytes(Pubkey.from_string(WSOL_MINT)),
        ],
        pump_amm_program,
    )
    pool_vault = derive_associated_token_account(str(canonical_pool), mint, token_program)
    return {
        bonding_vault: "Pump bonding-curve vault (derived from official PDA seeds)",
        pool_vault: "PumpSwap canonical SOL-pool vault (derived from official PDA seeds)",
    }


def _parsed_token_controller(account: object, mint: str, token_program: str) -> str | None:
    if not isinstance(account, dict):
        return None
    if account.get("owner") != token_program:
        return None
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict) or info.get("mint") != mint:
        return None
    controller = info.get("owner")
    if not isinstance(controller, str):
        return None
    try:
        Pubkey.from_string(controller)
    except ValueError:
        return None
    return controller


def _controller_classification(controller: str, account: object) -> tuple[str, str | None]:
    if controller == SOLANA_INCINERATOR_ADDRESS:
        return "burn", None
    key = Pubkey.from_string(controller)
    if account is None:
        return ("pda_controller", None) if not key.is_on_curve() else ("wallet", None)
    if not isinstance(account, dict):
        return "unknown", None
    owner = account.get("owner")
    program = owner if isinstance(owner, str) else None
    if program in RECOGNIZED_LIQUIDITY_PROGRAM_IDS:
        return "program_controlled", program
    if account.get("executable") is True:
        return "program_controlled", program
    if program and program != SYSTEM_PROGRAM_ID:
        return "program_controlled", program
    return "wallet", program


def _all_instructions(transaction: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    payload = transaction.get("transaction")
    message = payload.get("message") if isinstance(payload, dict) else None
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if isinstance(instructions, list):
        found.extend(item for item in instructions if isinstance(item, dict))
    meta = transaction.get("meta")
    inner = meta.get("innerInstructions") if isinstance(meta, dict) else None
    if isinstance(inner, list):
        for group in inner:
            nested = group.get("instructions") if isinstance(group, dict) else None
            if isinstance(nested, list):
                found.extend(item for item in nested if isinstance(item, dict))
    return found


def _initializes_mint(transaction: dict[str, Any], mint: str) -> bool:
    for instruction in _all_instructions(transaction):
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in {
            "initializeMint",
            "initializeMint2",
        }:
            continue
        info = parsed.get("info")
        if isinstance(info, dict) and info.get("mint") == mint:
            return True
    return False


def _creation_signer(transaction: dict[str, Any], mint: str) -> str | None:
    payload = transaction.get("transaction")
    message = payload.get("message") if isinstance(payload, dict) else None
    keys = message.get("accountKeys") if isinstance(message, dict) else None
    if not isinstance(keys, list):
        return None
    for item in keys:
        if not isinstance(item, dict) or item.get("signer") is not True:
            continue
        address = item.get("pubkey")
        if isinstance(address, str) and address != mint:
            try:
                Pubkey.from_string(address)
            except ValueError:
                continue
            return address
    return None


def _trace_origin(
    rpc: RpcClient,
    mint: str,
    *,
    signature_limit: int,
    transaction_candidates: int,
    controller_amounts: dict[str, int],
    supply: int,
) -> tuple[OriginEvidence | None, list[str]]:
    limitations: list[str] = []
    try:
        signatures = rpc.call(
            "getSignaturesForAddress",
            [mint, {"commitment": "confirmed", "limit": signature_limit}],
            attempts=1,
        )
    except RpcError:
        return None, ["Mint creation history was unavailable from the configured RPC"]
    if not isinstance(signatures, list):
        return None, ["Mint creation history response was malformed"]
    history_complete = len(signatures) < signature_limit
    if not history_complete:
        limitations.append(
            f"Creation may be older than the bounded {signature_limit}-signature lookback"
        )
    oldest = [item for item in reversed(signatures) if isinstance(item, dict)]
    for item in oldest[:transaction_candidates]:
        signature = item.get("signature")
        if not isinstance(signature, str):
            continue
        try:
            transaction = rpc.call(
                "getTransaction",
                [
                    signature,
                    {
                        "commitment": "confirmed",
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                attempts=1,
            )
        except RpcError:
            limitations.append("One creation-candidate transaction could not be read")
            continue
        if not isinstance(transaction, dict) or not _initializes_mint(transaction, mint):
            continue
        signer = _creation_signer(transaction, mint)
        if signer is None:
            limitations.append(
                "Mint initialization was found but its creation signer was ambiguous"
            )
            return None, limitations
        slot = transaction.get("slot")
        block_time = transaction.get("blockTime")
        return (
            OriginEvidence(
                creation_signer=signer,
                confidence="high",
                signature=signature,
                slot=slot if isinstance(slot, int) and slot >= 0 else None,
                block_time=block_time if isinstance(block_time, int) and block_time >= 0 else None,
                current_observed_bps=_bps(controller_amounts.get(signer, 0), supply),
                history_complete=history_complete,
            ),
            limitations,
        )
    limitations.append(
        "No parsed mint-initialization transaction was found in the bounded lookback"
    )
    return None, limitations


def analyze_token_forensics(
    rpc: RpcClient,
    *,
    mint: str,
    token_program: str,
    supply: int,
    largest_accounts: list[dict[str, Any]],
    origin_trace_enabled: bool,
    cached_origin: OriginEvidence | None = None,
    origin_trace_deferred: bool = False,
    signature_limit: int,
    transaction_candidates: int,
    min_context_slot: int | None = None,
) -> TokenForensics:
    raw_top10 = _bps(
        sum(int(str(item.get("amount", "0"))) for item in largest_accounts[:10]), supply
    )
    addresses = [item.get("address") for item in largest_accounts]
    if not addresses or any(not isinstance(address, str) for address in addresses):
        return TokenForensics(
            status="unavailable",
            analyzed_accounts=0,
            distinct_controllers=0,
            raw_top10_bps=raw_top10,
            adjusted_top10_bps=raw_top10,
            recognized_vault_bps=0,
            burn_bps=0,
            largest_controller_bps=raw_top10,
            limitations=("Largest-account response omitted token-account addresses",),
        )

    token_addresses = [str(address) for address in addresses]
    limitations: list[str] = []
    token_config: dict[str, object] = {
        "encoding": "jsonParsed",
        "commitment": "confirmed",
    }
    if min_context_slot is not None:
        token_config["minContextSlot"] = min_context_slot
    try:
        token_result = rpc.call(
            "getMultipleAccounts",
            [token_addresses, token_config],
            attempts=1,
        )
        token_values = token_result.get("value") if isinstance(token_result, dict) else None
    except RpcError:
        token_values = None
    if not isinstance(token_values, list) or len(token_values) != len(token_addresses):
        return TokenForensics(
            status="unavailable",
            analyzed_accounts=0,
            distinct_controllers=0,
            raw_top10_bps=raw_top10,
            adjusted_top10_bps=raw_top10,
            recognized_vault_bps=0,
            burn_bps=0,
            largest_controller_bps=raw_top10,
            limitations=("Top token-account controllers were unavailable from the configured RPC",),
        )

    exact_vaults = recognized_pump_vaults(mint, token_program)
    rows: list[dict[str, Any]] = []
    controllers: list[str] = []
    for index, item in enumerate(largest_accounts):
        expected_amount = int(str(item.get("amount", "0")))
        controller = _parsed_token_controller(token_values[index], mint, token_program)
        if controller is None:
            limitations.append(f"Controller was unavailable for top account {index + 1}")
        else:
            controllers.append(controller)
        rows.append(
            {
                "token_account": token_addresses[index],
                "controller": controller,
                "amount": expected_amount,
            }
        )

    unique_controllers = list(dict.fromkeys(controllers))
    controller_meta: dict[str, object] = {}
    if unique_controllers:
        controller_config: dict[str, object] = {
            "encoding": "base64",
            "dataSlice": {"offset": 0, "length": 0},
            "commitment": "confirmed",
        }
        if min_context_slot is not None:
            controller_config["minContextSlot"] = min_context_slot
        try:
            controller_result = rpc.call(
                "getMultipleAccounts",
                [unique_controllers, controller_config],
                attempts=1,
            )
            values = controller_result.get("value") if isinstance(controller_result, dict) else None
            if isinstance(values, list) and len(values) == len(unique_controllers):
                controller_meta = dict(zip(unique_controllers, values, strict=True))
            else:
                limitations.append("Controller ownership metadata was malformed")
        except RpcError:
            limitations.append("Controller ownership metadata was unavailable")

    evidence_rows: list[HolderControllerEvidence] = []
    controller_amounts: dict[str, int] = defaultdict(int)
    controller_classes: dict[str, str] = {}
    for row in rows:
        token_account = str(row["token_account"])
        controller = row["controller"] if isinstance(row["controller"], str) else None
        amount = int(row["amount"])
        evidence = exact_vaults.get(token_account)
        if evidence is not None:
            controller_program = (
                PUMP_AMM_PROGRAM_ID if evidence.startswith("PumpSwap") else PUMP_PROGRAM_ID
            )
            classification = "recognized_liquidity_vault"
        elif controller is None:
            classification, controller_program = "unknown", None
        else:
            classification, controller_program = _controller_classification(
                controller, controller_meta.get(controller)
            )
        if controller is not None and classification not in {"recognized_liquidity_vault", "burn"}:
            controller_amounts[controller] += amount
            controller_classes.setdefault(controller, classification)
        evidence_rows.append(
            HolderControllerEvidence(
                token_account=token_account,
                controller=controller,
                amount_atomic=amount,
                supply_bps=_bps(amount, supply),
                classification=classification,
                controller_program=controller_program,
                evidence=evidence,
            )
        )

    burn_amount = sum(row.amount_atomic for row in evidence_rows if row.classification == "burn")
    vault_amount = sum(
        row.amount_atomic
        for row in evidence_rows
        if row.classification == "recognized_liquidity_vault"
    )
    adjusted_supply = max(1, supply - burn_amount)
    adjusted_top10_amount = sum(
        row.amount_atomic
        for row in evidence_rows[:10]
        if row.classification not in {"recognized_liquidity_vault", "burn"}
    )
    controller_counts = Counter(
        row.controller
        for row in evidence_rows
        if row.controller and row.classification not in {"recognized_liquidity_vault", "burn"}
    )
    clusters = tuple(
        ControllerCluster(
            controller=controller,
            token_account_count=controller_counts[controller],
            amount_atomic=amount,
            supply_bps=_bps(amount, adjusted_supply),
            classification=controller_classes.get(controller, "unknown"),
        )
        for controller, amount in sorted(
            controller_amounts.items(), key=lambda item: item[1], reverse=True
        )
        if controller_counts[controller] > 1
    )

    origin: OriginEvidence | None = None
    if cached_origin is not None:
        origin = replace(
            cached_origin,
            current_observed_bps=_bps(
                controller_amounts.get(cached_origin.creation_signer, 0), adjusted_supply
            ),
        )
    elif origin_trace_enabled:
        origin, origin_limitations = _trace_origin(
            rpc,
            mint,
            signature_limit=signature_limit,
            transaction_candidates=transaction_candidates,
            controller_amounts=controller_amounts,
            supply=adjusted_supply,
        )
        limitations.extend(origin_limitations)
    elif origin_trace_deferred:
        limitations.append("Mint creation history is awaiting a bounded retry")

    status = "complete" if not limitations else "partial"
    return TokenForensics(
        status=status,
        analyzed_accounts=len(evidence_rows),
        distinct_controllers=len(set(controllers)),
        raw_top10_bps=raw_top10,
        adjusted_top10_bps=_bps(adjusted_top10_amount, adjusted_supply),
        recognized_vault_bps=_bps(vault_amount, adjusted_supply),
        burn_bps=_bps(burn_amount, supply),
        largest_controller_bps=max(
            (_bps(amount, adjusted_supply) for amount in controller_amounts.values()), default=0
        ),
        holders=tuple(evidence_rows),
        controller_clusters=clusters,
        origin=origin,
        limitations=tuple(dict.fromkeys(limitations)),
    )
