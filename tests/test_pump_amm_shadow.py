"""The shadow decoder must reject spoofed, malformed, and misoriented events."""

from __future__ import annotations

import base64
import struct
from decimal import Decimal

import pytest
from solders.pubkey import Pubkey

from memecoin_bot.pump_amm_shadow import (
    BUY_DISCRIMINATOR,
    POOL_DISCRIMINATOR,
    PUMP_AMM_PROGRAM,
    SELL_DISCRIMINATOR,
    WSOL_MINT,
    PumpAmmDecodeError,
    decode_program_logs,
    parse_pool_account,
)

POOL = str(Pubkey.new_unique())
MINT = str(Pubkey.new_unique())


def pool_bytes(base: str, quote: str) -> bytes:
    data = bytearray(107)
    data[:8] = POOL_DISCRIMINATOR
    data[43:75] = bytes(Pubkey.from_string(base))
    data[75:107] = bytes(Pubkey.from_string(quote))
    return bytes(data)


def event_bytes(discriminator: bytes, pool: str = POOL) -> str:
    data = bytearray(152)
    data[:8] = discriminator
    struct.pack_into("<q", data, 8, 1_700_000_000)
    struct.pack_into("<Q", data, 16, 1_000_000_000)  # 1 SOL or 1000 tokens
    struct.pack_into("<Q", data, 64, 2_000_000_000)  # 2000 tokens or 2 SOL
    struct.pack_into("<Q", data, 72, 25)
    struct.pack_into("<Q", data, 88, 5)
    data[120:152] = bytes(Pubkey.from_string(pool))
    return base64.b64encode(data).decode("ascii")


def logs(encoded: str, program: str = PUMP_AMM_PROGRAM) -> list[str]:
    return [
        f"Program {program} invoke [1]",
        f"Program data: {encoded}",
        f"Program {program} success",
    ]


def test_sol_base_orientation_reverses_pool_buy_side() -> None:
    identity = parse_pool_account(
        pool_bytes(WSOL_MINT, MINT),
        owner=PUMP_AMM_PROGRAM,
        pool=POOL,
        target_mint=MINT,
        target_decimals=6,
    )
    buy = decode_program_logs(logs(event_bytes(BUY_DISCRIMINATOR)), identity)
    sell = decode_program_logs(logs(event_bytes(SELL_DISCRIMINATOR)), identity)
    assert buy[0].target_side == "sell"
    assert sell[0].target_side == "buy"
    assert buy[0].price_sol == Decimal("0.0005")


def test_token_base_orientation_preserves_pool_buy_side() -> None:
    identity = parse_pool_account(
        pool_bytes(MINT, WSOL_MINT),
        owner=PUMP_AMM_PROGRAM,
        pool=POOL,
        target_mint=MINT,
        target_decimals=6,
    )
    result = decode_program_logs(logs(event_bytes(BUY_DISCRIMINATOR)), identity)
    assert result[0].target_side == "buy"
    assert result[0].price_sol == Decimal("0.002")


def test_ignores_events_from_other_programs_and_other_pools() -> None:
    identity = parse_pool_account(
        pool_bytes(WSOL_MINT, MINT),
        owner=PUMP_AMM_PROGRAM,
        pool=POOL,
        target_mint=MINT,
        target_decimals=6,
    )
    attacker = str(Pubkey.new_unique())
    assert decode_program_logs(logs(event_bytes(BUY_DISCRIMINATOR), attacker), identity) == ()
    assert (
        decode_program_logs(
            logs(event_bytes(BUY_DISCRIMINATOR, str(Pubkey.new_unique()))), identity
        )
        == ()
    )


def test_rejects_wrong_pool_owner_and_truncated_event() -> None:
    with pytest.raises(PumpAmmDecodeError):
        parse_pool_account(
            pool_bytes(WSOL_MINT, MINT),
            owner=str(Pubkey.new_unique()),
            pool=POOL,
            target_mint=MINT,
            target_decimals=6,
        )
    identity = parse_pool_account(
        pool_bytes(WSOL_MINT, MINT),
        owner=PUMP_AMM_PROGRAM,
        pool=POOL,
        target_mint=MINT,
        target_decimals=6,
    )
    with pytest.raises(PumpAmmDecodeError):
        decode_program_logs(logs(base64.b64encode(BUY_DISCRIMINATOR).decode("ascii")), identity)
    with pytest.raises(PumpAmmDecodeError):
        decode_program_logs([f"Program {PUMP_AMM_PROGRAM} invoke [1]"], identity)
