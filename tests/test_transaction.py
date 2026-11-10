from __future__ import annotations

import base64
from typing import Any

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from memecoin_bot.constants import (
    COMPUTE_BUDGET_PROGRAM_ID,
    JUPITER_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
)
from memecoin_bot.transaction import (
    TransactionError,
    _bounded_compute_price,
    build_simulate_and_submit,
)


def _api_instruction(program: str, data: bytes, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "programId": program,
        "accounts": accounts,
        "data": base64.b64encode(data).decode("ascii"),
    }


class SimulatingRpc:
    def __init__(self) -> None:
        self.calls = 0

    def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
        del params, attempts
        assert method == "simulateTransaction"
        self.calls += 1
        return {"value": {"err": None, "logs": [], "unitsConsumed": 100_000}}


class RentRpc(SimulatingRpc):
    def __init__(self, *, existing: bool = False, post_account: bool = True) -> None:
        super().__init__()
        self.existing = existing
        self.post_account = post_account

    def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
        del attempts
        if method == "getAccountInfo":
            return {"value": {"owner": TOKEN_PROGRAM_ID} if self.existing else None}
        assert method == "simulateTransaction"
        self.calls += 1
        value: dict[str, Any] = {"err": None, "logs": [], "unitsConsumed": 100_000}
        if "accounts" in params[1]:
            value["accounts"] = (
                [{"owner": TOKEN_PROGRAM_ID, "lamports": 1_513_840}]
                if self.post_account
                else [None]
            )
        return {"value": value}


def test_priority_price_is_capped_by_total_lamports() -> None:
    assert _bounded_compute_price(20_000, 100_000, 1_000) == 10_000
    assert _bounded_compute_price(20_000, 100_000, 0) == 0


def test_dry_run_compiles_and_simulates_twice() -> None:
    keypair = Keypair()
    blockhash = Hash.new_unique()
    limit = set_compute_unit_limit(1_400_000)
    price = set_compute_unit_price(5_000)
    signer = {
        "pubkey": str(keypair.pubkey()),
        "isSigner": True,
        "isWritable": True,
    }
    build: dict[str, Any] = {
        "blockhashWithMetadata": {
            "blockhash": list(bytes(blockhash)),
            "lastValidBlockHeight": 100,
        },
        "addressesByLookupTableAddress": {},
        "computeBudgetInstructions": [
            _api_instruction(COMPUTE_BUDGET_PROGRAM_ID, bytes(limit.data), []),
            _api_instruction(COMPUTE_BUDGET_PROGRAM_ID, bytes(price.data), []),
        ],
        "setupInstructions": [],
        "swapInstruction": _api_instruction(JUPITER_PROGRAM_ID, b"\x01", [signer]),
        "cleanupInstruction": _api_instruction(
            TOKEN_PROGRAM_ID,
            b"\x02",
            [],
        ),
        "otherInstructions": [],
        "_expectedInputMint": WSOL_MINT,
        "_expectedInputAmount": 1_000,
    }
    rpc = SimulatingRpc()
    result = build_simulate_and_submit(
        rpc,  # type: ignore[arg-type]
        keypair,
        build,
        max_priority_fee_lamports=1_000,
        submit=False,
        confirmation_timeout_seconds=10,
    )
    assert not result.submitted
    assert result.simulated_units == 100_000
    assert rpc.calls == 2

    for existing, expected_rent in ((False, 1_513_840), (True, 0)):
        rent_rpc = RentRpc(existing=existing)
        rent_result = build_simulate_and_submit(
            rent_rpc,  # type: ignore[arg-type]
            keypair,
            build,
            max_priority_fee_lamports=1_000,
            submit=False,
            confirmation_timeout_seconds=10,
            output_ata=str(Pubkey.new_unique()),
            output_ata_program=TOKEN_PROGRAM_ID,
        )
        assert rent_result.output_ata_rent_lamports == expected_rent
        assert rent_rpc.calls == 2

    with pytest.raises(TransactionError, match="expected output token account"):
        build_simulate_and_submit(
            RentRpc(post_account=False),  # type: ignore[arg-type]
            keypair,
            build,
            max_priority_fee_lamports=1_000,
            submit=False,
            confirmation_timeout_seconds=10,
            output_ata=str(Pubkey.new_unique()),
            output_ata_program=TOKEN_PROGRAM_ID,
        )


def test_unexpected_signer_is_rejected() -> None:
    keypair = Keypair()
    build: dict[str, Any] = {
        "blockhashWithMetadata": {
            "blockhash": list(bytes(Hash.new_unique())),
            "lastValidBlockHeight": 100,
        },
        "addressesByLookupTableAddress": {},
        "computeBudgetInstructions": [],
        "setupInstructions": [],
        "swapInstruction": _api_instruction(
            str(Pubkey.new_unique()),
            b"\x01",
            [
                {
                    "pubkey": str(Pubkey.new_unique()),
                    "isSigner": True,
                    "isWritable": True,
                }
            ],
        ),
        "cleanupInstruction": None,
        "otherInstructions": [],
        "_expectedInputMint": WSOL_MINT,
        "_expectedInputAmount": 1_000,
    }
    with pytest.raises(TransactionError, match="unexpected additional signer"):
        build_simulate_and_submit(
            SimulatingRpc(),  # type: ignore[arg-type]
            keypair,
            build,
            max_priority_fee_lamports=1_000,
            submit=False,
            confirmation_timeout_seconds=10,
        )
