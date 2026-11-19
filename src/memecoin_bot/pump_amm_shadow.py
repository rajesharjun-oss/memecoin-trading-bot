"""Strict PumpSwap event-prefix decoder for isolated research only.

The byte offsets and discriminators follow pump-fun/pump-public-docs
``idl/pump_amm.json`` as observed on 2026-11-19. Event tails can grow, so this
decoder reads only the price-relevant prefix and rejects malformed inputs.
It is not connected to paper candles, strategy, or execution.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from solders.pubkey import Pubkey

PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
POOL_DISCRIMINATOR = bytes((241, 154, 109, 4, 17, 177, 109, 188))
BUY_DISCRIMINATOR = bytes((103, 244, 82, 31, 44, 245, 119, 119))
SELL_DISCRIMINATOR = bytes((62, 47, 55, 10, 165, 3, 220, 42))


class PumpAmmDecodeError(ValueError):
    """An event cannot be trusted as a PumpSwap trade for this pool."""


@dataclass(frozen=True, slots=True)
class PoolIdentity:
    pool: str
    target_mint: str
    target_decimals: int
    base_is_sol: bool


@dataclass(frozen=True, slots=True)
class SwapObservation:
    timestamp: int
    target_side: Literal["buy", "sell"]
    token_amount_atomic: int
    sol_amount_lamports: int
    price_sol: Decimal


def parse_pool_account(
    data: bytes, *, owner: str, pool: str, target_mint: str, target_decimals: int
) -> PoolIdentity:
    if owner != PUMP_AMM_PROGRAM or len(data) < 107 or data[:8] != POOL_DISCRIMINATOR:
        raise PumpAmmDecodeError("Not a supported PumpSwap pool account")
    if target_mint == WSOL_MINT or not 0 <= target_decimals <= 18:
        raise PumpAmmDecodeError("Invalid target mint decimals")
    try:
        Pubkey.from_string(pool)
        Pubkey.from_string(target_mint)
        base_mint = str(Pubkey.from_bytes(data[43:75]))
        quote_mint = str(Pubkey.from_bytes(data[75:107]))
    except ValueError:
        raise PumpAmmDecodeError("Invalid pool or mint identity") from None
    if (base_mint, quote_mint) == (WSOL_MINT, target_mint):
        base_is_sol = True
    elif (base_mint, quote_mint) == (target_mint, WSOL_MINT):
        base_is_sol = False
    else:
        raise PumpAmmDecodeError("Pool is not the requested SOL/token pair")
    return PoolIdentity(pool, target_mint, target_decimals, base_is_sol)


def _trade_event(encoded: str, identity: PoolIdentity) -> SwapObservation | None:
    if len(encoded) > 2048:
        raise PumpAmmDecodeError("Oversized PumpSwap event")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise PumpAmmDecodeError("Invalid PumpSwap event encoding") from None
    discriminator = raw[:8]
    if discriminator not in (BUY_DISCRIMINATOR, SELL_DISCRIMINATOR):
        return None
    if len(raw) < 152:
        raise PumpAmmDecodeError("Truncated PumpSwap trade event")
    if str(Pubkey.from_bytes(raw[120:152])) != identity.pool:
        return None  # A transaction can trade through more than one pool.
    timestamp = struct.unpack_from("<q", raw, 8)[0]
    base_amount = struct.unpack_from("<Q", raw, 16)[0]
    quote_amount = struct.unpack_from("<Q", raw, 64)[0]
    lp_fee_bps = struct.unpack_from("<Q", raw, 72)[0]
    protocol_fee_bps = struct.unpack_from("<Q", raw, 88)[0]
    if (
        timestamp <= 0
        or base_amount == 0
        or quote_amount == 0
        or lp_fee_bps > 1000
        or protocol_fee_bps > 1000
    ):
        raise PumpAmmDecodeError("Implausible PumpSwap trade event")
    token_amount, sol_amount = (
        (quote_amount, base_amount) if identity.base_is_sol else (base_amount, quote_amount)
    )
    price_sol = (Decimal(sol_amount) / Decimal(1_000_000_000)) / (
        Decimal(token_amount) / Decimal(10**identity.target_decimals)
    )
    target_side: Literal["buy", "sell"] = (
        "buy" if (discriminator == BUY_DISCRIMINATOR) != identity.base_is_sol else "sell"
    )
    return SwapObservation(timestamp, target_side, token_amount, sol_amount, price_sol)


def decode_program_logs(logs: object, identity: PoolIdentity) -> tuple[SwapObservation, ...]:
    """Accept event data only while the actual PumpSwap program is active."""
    if not isinstance(logs, list) or len(logs) > 500:
        raise PumpAmmDecodeError("Invalid transaction logs")
    stack: list[str] = []
    events: list[SwapObservation] = []
    for line in logs:
        if not isinstance(line, str) or len(line) > 4096:
            raise PumpAmmDecodeError("Invalid transaction log line")
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split(" ", 2)[1])
        elif line.startswith("Program ") and (line.endswith(" success") or " failed:" in line):
            program = line.split(" ", 2)[1]
            if not stack or stack[-1] != program:
                raise PumpAmmDecodeError("Malformed program invocation stack")
            stack.pop()
        elif line.startswith("Program data: ") and stack and stack[-1] == PUMP_AMM_PROGRAM:
            event = _trade_event(line.removeprefix("Program data: "), identity)
            if event is not None:
                events.append(event)
    if stack:
        raise PumpAmmDecodeError("Truncated program invocation stack")
    return tuple(events)
