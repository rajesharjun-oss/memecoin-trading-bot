from __future__ import annotations

from typing import Any

from solders.pubkey import Pubkey

from memecoin_bot.audit import audit_token
from memecoin_bot.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID


class FakeRpc:
    def __init__(
        self,
        *,
        mint_authority: str | None = None,
        top_amount: int = 100,
        token_program: str = TOKEN_PROGRAM_ID,
        extensions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.mint_authority = mint_authority
        self.top_amount = top_amount
        self.token_program = token_program
        self.extensions = extensions

    def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
        del params, attempts
        if method == "getAccountInfo":
            return {
                "value": {
                    "owner": self.token_program,
                    "data": {
                        "parsed": {
                            "type": "mint",
                            "info": {
                                "supply": "10000",
                                "decimals": 6,
                                "mintAuthority": self.mint_authority,
                                "freezeAuthority": None,
                                **(
                                    {"extensions": self.extensions}
                                    if self.extensions is not None
                                    else {}
                                ),
                            },
                        }
                    },
                }
            }
        if method == "getTokenLargestAccounts":
            return {"value": [{"amount": str(self.top_amount)} for _ in range(10)]}
        raise AssertionError(method)


def test_audit_passes_revoked_mint_with_bounded_concentration() -> None:
    mint = str(Pubkey.new_unique())
    audit = audit_token(
        FakeRpc(),  # type: ignore[arg-type]
        mint,
        max_top10_concentration_bps=2_000,
        allow_token_2022=False,
    )
    assert audit.passed
    assert audit.top10_concentration_bps == 1_000
    assert any(item.code == "identity_limit" and not item.blocking for item in audit.findings)


def test_audit_rejects_active_mint_authority() -> None:
    authority = str(Pubkey.new_unique())
    audit = audit_token(
        FakeRpc(mint_authority=authority),  # type: ignore[arg-type]
        str(Pubkey.new_unique()),
        max_top10_concentration_bps=2_000,
        allow_token_2022=False,
    )
    assert not audit.passed
    assert any(item.code == "mint_authority" for item in audit.findings)


def test_prefetched_mint_account_keeps_authority_check_without_second_lookup() -> None:
    mint = str(Pubkey.new_unique())
    authority = str(Pubkey.new_unique())
    rpc = FakeRpc(mint_authority=authority)
    account = rpc.call("getAccountInfo", [mint])["value"]

    class NoMintLookupRpc(FakeRpc):
        def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
            if method == "getAccountInfo":
                raise AssertionError("prefetched mint must not be fetched again")
            return super().call(method, params, attempts=attempts)

    audit = audit_token(
        NoMintLookupRpc(),  # type: ignore[arg-type]
        mint,
        mint_account=account,
        max_top10_concentration_bps=2_000,
        allow_token_2022=False,
        forensics_enabled=False,
    )
    assert not audit.passed
    assert any(item.code == "mint_authority" for item in audit.findings)


def test_audit_uses_integer_holder_math() -> None:
    audit = audit_token(
        FakeRpc(top_amount=501),  # type: ignore[arg-type]
        str(Pubkey.new_unique()),
        max_top10_concentration_bps=5_000,
        allow_token_2022=False,
    )
    assert audit.top10_concentration_bps == 5_010
    assert not audit.passed


def test_token_2022_opt_in_accepts_only_immutable_mint_metadata() -> None:
    mint = str(Pubkey.new_unique())
    extensions = [
        {
            "extension": "metadataPointer",
            "state": {"authority": None, "metadataAddress": mint},
        },
        {
            "extension": "tokenMetadata",
            "state": {"mint": mint, "updateAuthority": None},
        },
    ]
    audit = audit_token(
        FakeRpc(token_program=TOKEN_2022_PROGRAM_ID, extensions=extensions),  # type: ignore[arg-type]
        mint,
        max_top10_concentration_bps=2_000,
        allow_token_2022=True,
        forensics_enabled=False,
    )
    assert audit.passed


def test_token_2022_opt_in_rejects_unknown_and_mutable_extensions() -> None:
    mint = str(Pubkey.new_unique())
    scenarios = [
        (None, "token_2022_extensions_unverified"),
        ([{"extension": "futureExtension", "state": {}}], "unsupported_extensions"),
        (
            [
                {
                    "extension": "metadataPointer",
                    "state": {"authority": str(Pubkey.new_unique()), "metadataAddress": mint},
                }
            ],
            "mutable_metadata_pointer",
        ),
        (
            [
                {
                    "extension": "metadataPointer",
                    "state": {"authority": None, "metadataAddress": mint},
                },
                {
                    "extension": "tokenMetadata",
                    "state": {"mint": mint, "updateAuthority": str(Pubkey.new_unique())},
                },
            ],
            "mutable_token_metadata",
        ),
        ([{"extension": "transferHook", "state": {}}], "dangerous_extensions"),
    ]
    for extensions, expected_code in scenarios:
        audit = audit_token(
            FakeRpc(token_program=TOKEN_2022_PROGRAM_ID, extensions=extensions),  # type: ignore[arg-type]
            mint,
            max_top10_concentration_bps=2_000,
            allow_token_2022=True,
            forensics_enabled=False,
        )
        assert not audit.passed
        assert any(item.code == expected_code for item in audit.findings)
