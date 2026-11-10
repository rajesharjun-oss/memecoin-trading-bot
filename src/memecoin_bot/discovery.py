from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from solders.pubkey import Pubkey

from .constants import WSOL_MINT
from .rpc import RpcClient, RpcError


class DiscoveryError(RuntimeError):
    """Raised when an on-chain discovery request is invalid."""


def discover_recent_mints(
    rpc: RpcClient,
    program_ids: Iterable[str],
    *,
    signatures_per_program: int = 20,
    max_transactions: int = 100,
) -> list[str]:
    """Extract token-balance mints from recent transactions touching selected programs.

    This deliberately returns candidates, not a claim that a mint is new or safe.
    """
    if not 1 <= signatures_per_program <= 100:
        raise DiscoveryError("signatures_per_program must be between 1 and 100")
    if not 1 <= max_transactions <= 500:
        raise DiscoveryError("max_transactions must be between 1 and 500")
    programs = list(dict.fromkeys(program_ids))
    if not programs or len(programs) > 20:
        raise DiscoveryError("Provide between 1 and 20 program IDs")
    for program in programs:
        try:
            Pubkey.from_string(program)
        except ValueError as exc:
            raise DiscoveryError(f"Invalid program ID: {program}") from exc

    signatures: list[str] = []
    for program in programs:
        result = rpc.call(
            "getSignaturesForAddress",
            [program, {"limit": signatures_per_program, "commitment": "confirmed"}],
        )
        if not isinstance(result, list):
            raise DiscoveryError("RPC returned malformed signature history")
        for entry in result:
            signature = entry.get("signature") if isinstance(entry, dict) else None
            if isinstance(signature, str) and signature and signature not in signatures:
                signatures.append(signature)
                if len(signatures) >= max_transactions:
                    break
        if len(signatures) >= max_transactions:
            break

    mints: list[str] = []
    seen_mints: set[str] = set()
    for signature in signatures:
        try:
            transaction = rpc.call(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
                attempts=2,
            )
        except RpcError:
            # Historical transactions can be temporarily unavailable or pruned from a
            # provider even when the signature list succeeds. A bounded discovery scan
            # should retain the remaining evidence instead of failing the whole batch.
            continue
        if not isinstance(transaction, dict):
            continue
        meta = transaction.get("meta")
        if not isinstance(meta, dict) or meta.get("err") is not None:
            continue
        for key in ("preTokenBalances", "postTokenBalances"):
            balances = meta.get(key)
            if not isinstance(balances, list):
                continue
            for balance in balances:
                mint = balance.get("mint") if isinstance(balance, dict) else None
                if isinstance(mint, str) and mint != WSOL_MINT:
                    try:
                        Pubkey.from_string(mint)
                    except ValueError:
                        continue
                    if mint not in seen_mints:
                        seen_mints.add(mint)
                        mints.append(mint)
    return mints


def unique_mints_from_file(lines: Iterable[str]) -> list[str]:
    mints: list[str] = []
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            Pubkey.from_string(value)
        except ValueError as exc:
            raise DiscoveryError(f"Invalid mint in input file: {value}") from exc
        if value not in mints:
            mints.append(value)
    return mints


def balance_mints(transaction: dict[str, Any]) -> set[str]:
    """Small testable helper for downstream custom discovery adapters."""
    output: set[str] = set()
    meta = transaction.get("meta")
    if not isinstance(meta, dict):
        return output
    for key in ("preTokenBalances", "postTokenBalances"):
        values = meta.get(key, [])
        if isinstance(values, list):
            output.update(
                item["mint"]
                for item in values
                if isinstance(item, dict) and isinstance(item.get("mint"), str)
            )
    return output
