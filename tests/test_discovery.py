from __future__ import annotations

from typing import Any, cast

from solders.pubkey import Pubkey

from memecoin_bot.discovery import discover_recent_mints
from memecoin_bot.rpc import RpcClient, RpcError


class PartialHistoryRpc:
    def __init__(self, mint: str) -> None:
        self.mint = mint

    def call(self, method: str, params: list[Any], **_: object) -> Any:
        if method == "getSignaturesForAddress":
            return [{"signature": "missing"}, {"signature": "available"}]
        if params[0] == "missing":
            raise RpcError("historical transaction unavailable")
        return {
            "meta": {
                "err": None,
                "preTokenBalances": [],
                "postTokenBalances": [{"mint": self.mint}],
            }
        }


def test_discovery_skips_one_unavailable_historical_transaction() -> None:
    mint = str(Pubkey.new_unique())
    program = str(Pubkey.new_unique())
    rpc = cast(RpcClient, PartialHistoryRpc(mint))
    assert discover_recent_mints(rpc, [program], signatures_per_program=2) == [mint]
