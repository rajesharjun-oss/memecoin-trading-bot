from __future__ import annotations

import base64
import binascii
import math
import struct
import time
from collections.abc import Iterable
from typing import Any

from solders.address_lookup_table_account import (
    AddressLookupTable,
    AddressLookupTableAccount,
)
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from .constants import (
    ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    JUPITER_PROGRAM_ID,
    MAX_COMPUTE_UNITS,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
)
from .models import TradeResult
from .rpc import RpcClient, RpcError


class TransactionError(RuntimeError):
    """Raised when a transaction cannot be validated, simulated, or confirmed."""


class SubmissionUncertainError(TransactionError):
    """Raised after submission when final confirmation could not be established."""

    def __init__(self, message: str, signature: str) -> None:
        super().__init__(message)
        self.signature = signature


def _pubkey(value: Any, field: str) -> Pubkey:
    if not isinstance(value, str):
        raise TransactionError(f"{field} must be a public-key string")
    try:
        return Pubkey.from_string(value)
    except ValueError as exc:
        raise TransactionError(f"{field} is not a valid Solana public key") from exc


def _instruction(value: Any, *, payer: Pubkey) -> Instruction:
    if not isinstance(value, dict):
        raise TransactionError("Jupiter returned a malformed instruction")
    program_id = _pubkey(value.get("programId"), "instruction programId")
    raw_accounts = value.get("accounts")
    if not isinstance(raw_accounts, list) or len(raw_accounts) > 128:
        raise TransactionError("Jupiter instruction accounts are missing or excessive")

    accounts: list[AccountMeta] = []
    for item in raw_accounts:
        if not isinstance(item, dict):
            raise TransactionError("Jupiter returned a malformed account meta")
        account = _pubkey(item.get("pubkey"), "instruction account")
        is_signer = item.get("isSigner")
        is_writable = item.get("isWritable")
        if not isinstance(is_signer, bool) or not isinstance(is_writable, bool):
            raise TransactionError("Jupiter returned invalid account permissions")
        if is_signer and account != payer:
            raise TransactionError("Jupiter requested an unexpected additional signer")
        accounts.append(AccountMeta(account, is_signer, is_writable))

    encoded = value.get("data")
    if not isinstance(encoded, str):
        raise TransactionError("Jupiter instruction data is missing")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TransactionError("Jupiter instruction data is not valid base64") from exc
    if len(data) > 100_000:
        raise TransactionError("Jupiter instruction data exceeds the safety limit")
    return Instruction(program_id, data, accounts)


def _instructions(values: Any, *, payer: Pubkey) -> list[Instruction]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 32:
        raise TransactionError("Jupiter returned an invalid instruction list")
    return [_instruction(item, payer=payer) for item in values]


def _optional_instruction(value: Any, *, payer: Pubkey) -> list[Instruction]:
    """Parse a nullable single instruction while preserving list composition."""
    if value is None:
        return []
    return [_instruction(value, payer=payer)]


def _compute_unit_price(values: Any, *, payer: Pubkey) -> int:
    """Return Jupiter's micro-lamports/CU while discarding its CU limit instruction."""
    price: int | None = None
    for instruction in _instructions(values, payer=payer):
        if str(instruction.program_id) != COMPUTE_BUDGET_PROGRAM_ID:
            raise TransactionError("Unexpected program in compute-budget instructions")
        data = bytes(instruction.data)
        if len(data) == 5 and data[0] == 2:  # SetComputeUnitLimit(u32)
            continue
        if len(data) == 9 and data[0] == 3:  # SetComputeUnitPrice(u64)
            if price is not None:
                raise TransactionError("Jupiter returned multiple compute-unit prices")
            price = struct.unpack("<Q", data[1:])[0]
            continue
        raise TransactionError("Jupiter returned an unsupported compute-budget instruction")
    return price or 0


def _validated_setup(
    values: Any,
    *,
    payer: Pubkey,
    expected_input_mint: Any,
    expected_input_amount: Any,
) -> list[Instruction]:
    setup = _instructions(values, payer=payer)
    sync_native_accounts: set[Pubkey] = set()
    transfer_destinations: set[Pubkey] = set()
    try:
        expected_amount = int(expected_input_amount)
    except (TypeError, ValueError) as exc:
        raise TransactionError("Local expected input amount is missing") from exc
    if expected_amount <= 0:
        raise TransactionError("Local expected input amount is invalid")

    for instruction in setup:
        program = str(instruction.program_id)
        data = bytes(instruction.data)
        accounts = list(instruction.accounts)
        if program == ASSOCIATED_TOKEN_PROGRAM_ID:
            if data not in {b"", b"\x01"} or len(accounts) < 4:
                raise TransactionError("Unexpected associated-token setup instruction")
            if accounts[0].pubkey != payer or accounts[2].pubkey != payer:
                raise TransactionError("Associated-token setup is not scoped to the wallet")
        elif program == SYSTEM_PROGRAM_ID:
            if len(data) != 12 or struct.unpack("<I", data[:4])[0] != 2 or len(accounts) != 2:
                raise TransactionError("Unexpected system setup instruction")
            amount = struct.unpack("<Q", data[4:])[0]
            if expected_input_mint != WSOL_MINT or amount > expected_amount:
                raise TransactionError("System transfer exceeds the expected wrapped SOL input")
            if accounts[0].pubkey != payer or not accounts[0].is_signer:
                raise TransactionError("System transfer source is not the wallet")
            transfer_destinations.add(accounts[1].pubkey)
        elif program in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            if data != b"\x11" or len(accounts) != 1:
                raise TransactionError("Unexpected token-program setup instruction")
            sync_native_accounts.add(accounts[0].pubkey)
        else:
            raise TransactionError("Unexpected setup instruction program")

    if not transfer_destinations.issubset(sync_native_accounts):
        raise TransactionError("Wrapped SOL transfer is missing SyncNative validation")
    return setup


def _lookup_tables(value: Any) -> list[AddressLookupTableAccount]:
    if value is None:
        return []
    if not isinstance(value, dict) or len(value) > 16:
        raise TransactionError("Jupiter lookup-table data is malformed or excessive")
    tables: list[AddressLookupTableAccount] = []
    for address, entries in value.items():
        if not isinstance(entries, list) or len(entries) > 256:
            raise TransactionError("Jupiter lookup-table entry list is invalid")
        tables.append(
            AddressLookupTableAccount(
                _pubkey(address, "lookup-table address"),
                [_pubkey(item, "lookup-table entry") for item in entries],
            )
        )
    return tables


def _hydrate_network_metadata(rpc: RpcClient, build_response: dict[str, Any]) -> None:
    """Add RPC-owned blockhash and lookup-table data to a v1 instruction response."""
    if build_response.get("blockhashWithMetadata") is None:
        latest = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        value = latest.get("value") if isinstance(latest, dict) else None
        if not isinstance(value, dict):
            raise TransactionError("RPC returned malformed latest-blockhash data")
        build_response["blockhashWithMetadata"] = value

    if build_response.get("addressesByLookupTableAddress") is not None:
        return
    raw_addresses = build_response.get("addressLookupTableAddresses", [])
    if not isinstance(raw_addresses, list) or len(raw_addresses) > 16:
        raise TransactionError("Jupiter lookup-table address list is malformed or excessive")
    addresses: list[str] = []
    for raw_address in raw_addresses:
        address = str(_pubkey(raw_address, "lookup-table address"))
        if address in addresses:
            raise TransactionError("Jupiter returned a duplicate lookup-table address")
        addresses.append(address)

    hydrated: dict[str, list[str]] = {}
    if addresses:
        result = rpc.call(
            "getMultipleAccounts",
            [addresses, {"encoding": "base64", "commitment": "confirmed"}],
        )
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != len(addresses):
            raise TransactionError("RPC returned malformed lookup-table accounts")
        for address, account in zip(addresses, values, strict=True):
            if (
                not isinstance(account, dict)
                or account.get("owner") != ADDRESS_LOOKUP_TABLE_PROGRAM_ID
            ):
                raise TransactionError("RPC returned an invalid lookup-table owner")
            encoded = account.get("data")
            if not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != "base64":
                raise TransactionError("RPC returned malformed lookup-table data")
            try:
                raw = base64.b64decode(encoded[0], validate=True)
                table = AddressLookupTable.deserialize(raw)
            except (ValueError, TypeError, binascii.Error) as exc:
                raise TransactionError("RPC returned invalid lookup-table bytes") from exc
            hydrated[address] = [str(item) for item in table.addresses]
    build_response["addressesByLookupTableAddress"] = hydrated


def _blockhash(value: Any) -> tuple[Hash, int]:
    if not isinstance(value, dict):
        raise TransactionError("Jupiter omitted blockhash metadata")
    raw_hash = value.get("blockhash")
    if isinstance(raw_hash, str):
        try:
            blockhash = Hash.from_string(raw_hash)
        except ValueError as exc:
            raise TransactionError("Jupiter returned an invalid blockhash") from exc
    elif isinstance(raw_hash, list) and len(raw_hash) == 32:
        try:
            blockhash = Hash.from_bytes(bytes(raw_hash))
        except (TypeError, ValueError) as exc:
            raise TransactionError("Jupiter returned invalid blockhash bytes") from exc
    else:
        raise TransactionError("Jupiter returned an invalid blockhash")
    try:
        last_valid_height = int(value["lastValidBlockHeight"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransactionError("Jupiter omitted the last valid block height") from exc
    if last_valid_height <= 0:
        raise TransactionError("Jupiter returned an invalid last valid block height")
    return blockhash, last_valid_height


def _compile(
    *,
    payer: Keypair,
    instructions: Iterable[Instruction],
    lookup_tables: list[AddressLookupTableAccount],
    blockhash: Hash,
) -> VersionedTransaction:
    message = MessageV0.try_compile(payer.pubkey(), list(instructions), lookup_tables, blockhash)
    return VersionedTransaction(message, [payer])


def _serialize(transaction: VersionedTransaction) -> str:
    return base64.b64encode(bytes(transaction)).decode("ascii")


def _simulation_units(
    rpc: RpcClient,
    transaction: VersionedTransaction,
    *,
    capture_account: str | None = None,
    expected_account_owner: str | None = None,
) -> tuple[int, int | None]:
    options: dict[str, Any] = {
        "encoding": "base64",
        "sigVerify": True,
        "replaceRecentBlockhash": False,
        "commitment": "confirmed",
    }
    if capture_account is not None:
        options["accounts"] = {"encoding": "base64", "addresses": [capture_account]}
    result = rpc.call(
        "simulateTransaction",
        [_serialize(transaction), options],
        attempts=1,
    )
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        raise TransactionError("RPC returned a malformed simulation result")
    if value.get("err") is not None:
        logs = value.get("logs")
        tail = logs[-3:] if isinstance(logs, list) else []
        raise TransactionError(f"Transaction simulation failed: {value['err']!r}; logs={tail!r}")
    try:
        units = int(value["unitsConsumed"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransactionError("Simulation omitted compute-unit consumption") from exc
    if not 0 < units <= MAX_COMPUTE_UNITS:
        raise TransactionError("Simulation returned invalid compute-unit consumption")
    if capture_account is None:
        return units, None
    accounts = value.get("accounts")
    account = accounts[0] if isinstance(accounts, list) and len(accounts) == 1 else None
    if not isinstance(account, dict) or account.get("owner") != expected_account_owner:
        raise TransactionError("Simulation did not return the expected output token account")
    lamports = account.get("lamports")
    if type(lamports) is not int or lamports <= 0:
        raise TransactionError("Simulation returned an invalid output token-account reserve")
    return units, lamports


def _bounded_compute_price(
    requested_micro_lamports: int,
    compute_units: int,
    max_priority_fee_lamports: int,
) -> int:
    if max_priority_fee_lamports <= 0:
        return 0
    maximum_price = max_priority_fee_lamports * 1_000_000 // compute_units
    return min(max(0, requested_micro_lamports), maximum_price)


def _confirm(
    rpc: RpcClient,
    signature: str,
    *,
    last_valid_block_height: int,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status_result = rpc.call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}], attempts=1
        )
        values = status_result.get("value") if isinstance(status_result, dict) else None
        status = values[0] if isinstance(values, list) and values else None
        if isinstance(status, dict):
            if status.get("err") is not None:
                raise TransactionError(f"Transaction failed on-chain: {status['err']!r}")
            if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                return
        current_height = rpc.call("getBlockHeight", [{"commitment": "confirmed"}], attempts=1)
        if int(current_height) > last_valid_block_height:
            raise TransactionError("Transaction expired before confirmation")
        time.sleep(1.0)
    raise TransactionError("Timed out waiting for transaction confirmation")


def build_simulate_and_submit(
    rpc: RpcClient,
    keypair: Keypair,
    build_response: dict[str, Any],
    *,
    max_priority_fee_lamports: int,
    submit: bool,
    confirmation_timeout_seconds: int,
    output_ata: str | None = None,
    output_ata_program: str | None = None,
) -> TradeResult:
    """Validate Jupiter instructions, simulate twice, and optionally submit the transaction."""
    payer = keypair.pubkey()
    if output_ata is not None:
        _pubkey(output_ata, "output associated token account")
        if output_ata_program not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            raise TransactionError("Output token-account program is invalid")
    _hydrate_network_metadata(rpc, build_response)
    blockhash, last_valid_height = _blockhash(build_response.get("blockhashWithMetadata"))
    lookup_tables = _lookup_tables(build_response.get("addressesByLookupTableAddress"))
    requested_price = _compute_unit_price(
        build_response.get("computeBudgetInstructions"), payer=payer
    )

    if build_response.get("tokenLedgerInstruction") is not None:
        raise TransactionError("Unexpected token-ledger instruction")
    if build_response.get("otherInstructions") not in (None, []):
        raise TransactionError("Unexpected additional Jupiter instructions")

    setup = _validated_setup(
        build_response.get("setupInstructions"),
        payer=payer,
        expected_input_mint=build_response.get("_expectedInputMint"),
        expected_input_amount=build_response.get("_expectedInputAmount"),
    )
    swap = _instruction(build_response.get("swapInstruction"), payer=payer)
    if str(swap.program_id) != JUPITER_PROGRAM_ID:
        raise TransactionError("Unexpected swap instruction program")
    cleanup = _optional_instruction(build_response.get("cleanupInstruction"), payer=payer)
    if any(
        str(item.program_id) not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID} for item in cleanup
    ):
        raise TransactionError("Unexpected cleanup instruction program")

    body: list[Instruction] = [*setup, swap, *cleanup]
    if len(body) > 32:
        raise TransactionError("Jupiter returned too many transaction instructions")

    probe = _compile(
        payer=keypair,
        instructions=[set_compute_unit_limit(MAX_COMPUTE_UNITS), *body],
        lookup_tables=lookup_tables,
        blockhash=blockhash,
    )
    simulated_units, _ = _simulation_units(rpc, probe)
    compute_units = min(MAX_COMPUTE_UNITS, max(1, math.ceil(simulated_units * 1.20)))
    compute_price = _bounded_compute_price(
        requested_price, compute_units, max_priority_fee_lamports
    )
    final_instructions = [set_compute_unit_limit(compute_units)]
    if compute_price > 0:
        final_instructions.append(set_compute_unit_price(compute_price))
    final_instructions.extend(body)
    transaction = _compile(
        payer=keypair,
        instructions=final_instructions,
        lookup_tables=lookup_tables,
        blockhash=blockhash,
    )
    output_ata_rent: int | None = None
    capture_account: str | None = None
    if output_ata is not None:
        before = rpc.call(
            "getAccountInfo",
            [output_ata, {"encoding": "base64", "commitment": "confirmed"}],
            attempts=1,
        )
        if not isinstance(before, dict) or "value" not in before:
            raise TransactionError("RPC returned malformed output token-account data")
        existing = before["value"]
        if existing is None:
            capture_account = output_ata
        elif isinstance(existing, dict) and existing.get("owner") == output_ata_program:
            output_ata_rent = 0
        else:
            raise TransactionError("Existing output token account has an unexpected owner")
    _, simulated_ata_lamports = _simulation_units(
        rpc,
        transaction,
        capture_account=capture_account,
        expected_account_owner=output_ata_program,
    )
    if capture_account is not None:
        output_ata_rent = simulated_ata_lamports

    if not submit:
        return TradeResult(None, simulated_units, False, None, output_ata_rent)

    try:
        signature = rpc.call(
            "sendTransaction",
            [
                _serialize(transaction),
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                },
            ],
            attempts=1,
        )
    except RpcError as exc:
        raise TransactionError("RPC rejected transaction submission") from exc
    if not isinstance(signature, str) or not signature:
        raise TransactionError("RPC returned an invalid transaction signature")
    try:
        _confirm(
            rpc,
            signature,
            last_valid_block_height=last_valid_height,
            timeout_seconds=confirmation_timeout_seconds,
        )
    except (TransactionError, RpcError, ValueError) as exc:
        raise SubmissionUncertainError(
            "Transaction was submitted, but confirmation is uncertain; "
            "check the signature before retrying",
            signature,
        ) from exc
    return TradeResult(
        signature=signature,
        simulated_units=simulated_units,
        submitted=True,
        explorer_url=f"https://solscan.io/tx/{signature}",
        output_ata_rent_lamports=output_ata_rent,
    )
