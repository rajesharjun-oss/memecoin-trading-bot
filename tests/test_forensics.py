from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, cast

from solders.pubkey import Pubkey

from memecoin_bot.audit import audit_token
from memecoin_bot.config import Settings
from memecoin_bot.constants import PUMP_PROGRAM_ID, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
from memecoin_bot.forensics import analyze_token_forensics, recognized_pump_vaults
from memecoin_bot.journal import TradeJournal
from memecoin_bot.jupiter import JupiterClient
from memecoin_bot.rpc import RpcClient, RpcError
from memecoin_bot.service import TradingService


class ForensicRpc:
    def __init__(self) -> None:
        self.mint = str(Pubkey.new_unique())
        self.wallet = str(Pubkey.new_unique())
        self.vaults = recognized_pump_vaults(self.mint, TOKEN_PROGRAM_ID)
        self.vault = next(iter(self.vaults))
        self.wallet_account_one = str(Pubkey.new_unique())
        self.wallet_account_two = str(Pubkey.new_unique())
        mint_key = Pubkey.from_string(self.mint)
        self.bonding_curve = str(
            Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint_key)], Pubkey.from_string(PUMP_PROGRAM_ID)
            )[0]
        )
        self.largest = [
            {"address": self.vault, "amount": "6000"},
            {"address": self.wallet_account_one, "amount": "1000"},
            {"address": self.wallet_account_two, "amount": "500"},
        ]

    def _token_account(self, controller: str, amount: str) -> dict[str, Any]:
        return {
            "owner": TOKEN_PROGRAM_ID,
            "data": {
                "parsed": {
                    "type": "account",
                    "info": {
                        "mint": self.mint,
                        "owner": controller,
                        "tokenAmount": {"amount": amount},
                    },
                }
            },
        }

    def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
        del attempts
        if method == "getAccountInfo":
            return {
                "value": {
                    "owner": TOKEN_PROGRAM_ID,
                    "data": {
                        "parsed": {
                            "type": "mint",
                            "info": {
                                "supply": "10000",
                                "decimals": 6,
                                "mintAuthority": None,
                                "freezeAuthority": None,
                            },
                        }
                    },
                }
            }
        if method == "getTokenLargestAccounts":
            return {"value": self.largest}
        if method == "getMultipleAccounts":
            addresses = params[0]
            if addresses == [item["address"] for item in self.largest]:
                return {
                    "value": [
                        self._token_account(self.bonding_curve, "6000"),
                        self._token_account(self.wallet, "1000"),
                        self._token_account(self.wallet, "500"),
                    ]
                }
            assert addresses == [self.bonding_curve, self.wallet]
            return {
                "value": [
                    None,
                    {
                        "owner": SYSTEM_PROGRAM_ID,
                        "executable": False,
                        "data": ["", "base64"],
                    },
                ]
            }
        if method == "getSignaturesForAddress":
            return [
                {"signature": "newer", "slot": 11},
                {"signature": "creation", "slot": 10},
            ]
        if method == "getTransaction":
            assert params[0] == "creation"
            return {
                "slot": 10,
                "blockTime": 1_700_000_000,
                "transaction": {
                    "message": {
                        "accountKeys": [
                            {"pubkey": self.wallet, "signer": True, "writable": True},
                            {"pubkey": self.mint, "signer": True, "writable": True},
                        ],
                        "instructions": [
                            {
                                "parsed": {
                                    "type": "initializeMint2",
                                    "info": {"mint": self.mint},
                                }
                            }
                        ],
                    }
                },
                "meta": {"innerInstructions": []},
            }
        raise AssertionError(method)


def test_forensics_excludes_only_exact_vault_and_groups_controllers() -> None:
    rpc = ForensicRpc()
    report = analyze_token_forensics(
        rpc,  # type: ignore[arg-type]
        mint=rpc.mint,
        token_program=TOKEN_PROGRAM_ID,
        supply=10_000,
        largest_accounts=rpc.largest,
        origin_trace_enabled=True,
        signature_limit=100,
        transaction_candidates=5,
    )

    assert report.status == "complete"
    assert report.raw_top10_bps == 7_500
    assert report.adjusted_top10_bps == 1_500
    assert report.recognized_vault_bps == 6_000
    assert report.largest_controller_bps == 1_500
    assert report.holders[0].classification == "recognized_liquidity_vault"
    assert report.controller_clusters[0].token_account_count == 2
    assert report.controller_clusters[0].controller == rpc.wallet
    assert report.origin is not None
    assert report.origin.creation_signer == rpc.wallet
    assert report.origin.current_observed_bps == 1_500


def test_audit_uses_adjusted_concentration_only_with_exact_vault_evidence() -> None:
    rpc = ForensicRpc()
    result = audit_token(
        rpc,  # type: ignore[arg-type]
        rpc.mint,
        max_top10_concentration_bps=2_000,
        allow_token_2022=False,
        max_single_controller_bps=2_000,
    )

    assert result.passed
    assert result.top10_concentration_bps == 7_500
    assert result.effective_top10_concentration_bps == 1_500
    assert result.forensics is not None
    assert any(finding.code == "recognized_vault_exclusion" for finding in result.findings)
    assert any(
        finding.code == "creation_signer" and not finding.blocking for finding in result.findings
    )


def test_unknown_pda_controller_is_not_excluded_as_liquidity() -> None:
    rpc = ForensicRpc()
    unknown_pda = str(
        Pubkey.find_program_address(
            [b"unknown", bytes(Pubkey.from_string(rpc.mint))], Pubkey.default()
        )[0]
    )
    rpc.largest = [{"address": rpc.wallet_account_one, "amount": "5000"}]

    def replacement(method: str, params: list[Any], *, attempts: int = 3) -> Any:
        del attempts
        if method == "getMultipleAccounts":
            if params[0] == [rpc.wallet_account_one]:
                return {"value": [rpc._token_account(unknown_pda, "5000")]}
            if params[0] == [unknown_pda]:
                return {"value": [None]}
        if method == "getSignaturesForAddress":
            return []
        raise AssertionError(method)

    rpc.call = replacement  # type: ignore[method-assign]
    report = analyze_token_forensics(
        rpc,  # type: ignore[arg-type]
        mint=rpc.mint,
        token_program=TOKEN_PROGRAM_ID,
        supply=10_000,
        largest_accounts=rpc.largest,
        origin_trace_enabled=False,
        signature_limit=100,
        transaction_candidates=5,
    )

    assert report.holders[0].classification == "pda_controller"
    assert report.recognized_vault_bps == 0
    assert report.adjusted_top10_bps == 5_000


def _auditing_service(journal: TradeJournal, rpc: ForensicRpc) -> TradingService:
    settings = Settings(
        rpc_url="https://rpc.example.test",
        keypair_path=None,
        jupiter_api_key=None,
        live_trading_enabled=False,
        max_trade_lamports=100_000_000,
        max_daily_lamports=200_000_000,
        max_slippage_bps=300,
        max_price_impact_bps=500,
        max_top10_concentration_bps=9_000,
        max_priority_fee_lamports=1_000_000,
        confirmation_timeout_seconds=45,
        allow_token_2022=False,
        state_path=journal.path,
        forensics_max_single_controller_bps=2_000,
    )
    return TradingService(
        settings=settings,
        rpc=cast(RpcClient, rpc),
        jupiter=cast(JupiterClient, object()),
        keypair=cast(Any, object()),
        journal=journal,
    )


def test_cached_origin_skips_history_but_rechecks_creator_holdings(tmp_path: Path) -> None:
    class CountingRpc(ForensicRpc):
        def __init__(self) -> None:
            super().__init__()
            self.calls: Counter[str] = Counter()

        def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
            self.calls[method] += 1
            return super().call(method, params, attempts=attempts)

    rpc = CountingRpc()
    with TradeJournal(tmp_path / "origin.sqlite3") as journal:
        service = _auditing_service(journal, rpc)
        first = service.audit(rpc.mint)
        assert first.passed
        assert rpc.calls["getSignaturesForAddress"] == 1
        assert rpc.calls["getTransaction"] == 1
        assert journal.origin_trace_cache(rpc.mint) is not None

        rpc.largest[1]["amount"] = "2500"
        second = service.audit(rpc.mint)
        assert not second.passed
        assert rpc.calls["getSignaturesForAddress"] == 1
        assert rpc.calls["getTransaction"] == 1
        assert rpc.calls["getTokenLargestAccounts"] == 2
        assert rpc.calls["getMultipleAccounts"] == 4
        assert second.forensics is not None and second.forensics.origin is not None
        assert second.forensics.origin.current_observed_bps == 3_000
        assert any(
            finding.code == "creation_signer" and finding.blocking for finding in second.findings
        )


def test_missing_origin_is_retried_after_bounded_negative_cache(tmp_path: Path) -> None:
    class MissingOriginRpc(ForensicRpc):
        def __init__(self) -> None:
            super().__init__()
            self.calls: Counter[str] = Counter()

        def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
            self.calls[method] += 1
            if method == "getSignaturesForAddress":
                return [{"signature": "not-creation", "slot": 10}]
            if method == "getTransaction":
                raise RpcError("one historical transaction is unavailable")
            return super().call(method, params, attempts=attempts)

    rpc = MissingOriginRpc()
    with TradeJournal(tmp_path / "missing-origin.sqlite3") as journal:
        service = _auditing_service(journal, rpc)
        first = service.audit(rpc.mint)
        assert first.passed
        assert first.forensics is not None
        assert "One creation-candidate transaction could not be read" in first.forensics.limitations
        assert rpc.calls["getSignaturesForAddress"] == 1
        assert rpc.calls["getTransaction"] == 1
        assert journal.origin_trace_cache(rpc.mint) is not None

        second = service.audit(rpc.mint)
        assert second.passed
        assert rpc.calls["getSignaturesForAddress"] == 1
        assert rpc.calls["getTransaction"] == 1
        assert second.forensics is not None
        assert "Mint creation history is awaiting a bounded retry" in second.forensics.limitations

        journal._connection.execute(
            "UPDATE token_origin_cache SET checked_at_us=0 WHERE mint=?", (rpc.mint,)
        )
        service.audit(rpc.mint)
        assert rpc.calls["getSignaturesForAddress"] == 2
        assert rpc.calls["getTransaction"] == 2


def test_deferred_origin_trace_runs_when_token_becomes_market_eligible(tmp_path: Path) -> None:
    class CountingRpc(ForensicRpc):
        def __init__(self) -> None:
            super().__init__()
            self.calls: Counter[str] = Counter()

        def call(self, method: str, params: list[Any], *, attempts: int = 3) -> Any:
            self.calls[method] += 1
            return super().call(method, params, attempts=attempts)

    rpc = CountingRpc()
    with TradeJournal(tmp_path / "deferred-origin.sqlite3") as journal:
        service = _auditing_service(journal, rpc)
        deferred = service.audit(rpc.mint, origin_trace_enabled=False)
        assert deferred.passed
        assert rpc.calls["getMultipleAccounts"] == 2
        assert rpc.calls["getSignaturesForAddress"] == 0
        assert deferred.forensics is not None
        assert "Mint creation history is awaiting a bounded retry" in deferred.forensics.limitations

        traced = service.audit(rpc.mint, origin_trace_enabled=True)
        assert traced.forensics is not None and traced.forensics.origin is not None
        assert rpc.calls["getSignaturesForAddress"] == 1
        assert rpc.calls["getTransaction"] == 1
