from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from solders.pubkey import Pubkey

from memecoin_bot.constants import WSOL_MINT
from memecoin_bot.jupiter import JupiterClient, JupiterError


def _response(status: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.jup.ag/swap/v2/order"),
    )


def test_parse_json_surfaces_bounded_provider_error() -> None:
    response = _response(400, {"error": "Failed to get quotes", "requestId": "safe-id"})
    with pytest.raises(JupiterError, match="Failed to get quotes"):
        JupiterClient._parse_json(response)


def test_parse_json_rejects_non_object_response() -> None:
    response = httpx.Response(
        200,
        json=["unexpected"],
        request=httpx.Request("GET", "https://api.jup.ag/swap/v2/order"),
    )
    with pytest.raises(JupiterError, match="non-object"):
        JupiterClient._parse_json(response)


@pytest.mark.parametrize(
    ("price", "block_id", "valid"),
    [(125.5, 123_456, True), (None, 123_456, False), (125.5, None, False), ("NaN", 123_456, False)],
)
def test_sol_usd_reference_requires_valid_price_and_block(
    price: object, block_id: object, valid: bool
) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={WSOL_MINT: {"usdPrice": price, "blockId": block_id, "decimals": 9}},
        )

    with JupiterClient("test-key") as jupiter:
        jupiter._client.close()
        jupiter._client = httpx.Client(
            base_url="https://api.jup.ag", transport=httpx.MockTransport(respond)
        )
        if valid:
            assert jupiter.sol_usd_reference() == (Decimal("125.5"), 123_456)
        else:
            with pytest.raises(JupiterError, match="SOL price"):
                jupiter.sol_usd_reference()


def test_router_quote_uses_fee_free_build_response_without_second_request() -> None:
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    taker = str(Pubkey.new_unique())
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "inAmount": "100",
                "outAmount": "120",
                "slippageBps": 100,
                "priceImpactPct": "0.002",
                "swapInstruction": {"programId": str(Pubkey.new_unique())},
            },
        )

    with JupiterClient("test-key") as jupiter:
        jupiter._client.close()
        jupiter._client = httpx.Client(
            base_url="https://api.jup.ag", transport=httpx.MockTransport(respond)
        )
        quote = jupiter.router_quote(input_mint, output_mint, 100, taker=taker, slippage_bps=100)
        built = jupiter.build(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=100,
            taker=taker,
            slippage_bps=100,
            quote=quote,
            max_priority_fee_lamports=1_000,
        )
    assert quote.out_amount == 120
    assert quote.api_version == "v2-build"
    assert quote.fee_bps == 0
    assert quote.router == "metis"
    assert quote.price_impact_bps == 20
    assert built["outAmount"] == "120"
    assert len(requests) == 1
    assert requests[0].url.path == "/swap/v2/build"
    assert requests[0].url.params["platformFeeBps"] == "0"


def test_router_quote_rejects_changed_swap_identity() -> None:
    mint = str(Pubkey.new_unique())
    with pytest.raises(JupiterError, match="changed the swap identity"):
        JupiterClient.quote_from_build(
            {
                "inputMint": mint,
                "outputMint": str(Pubkey.new_unique()),
                "inAmount": "100",
                "outAmount": "120",
                "swapInstruction": {},
            },
            input_mint=mint,
            output_mint=str(Pubkey.new_unique()),
            amount=100,
            slippage_bps=100,
        )


def test_router_quote_timeout_is_a_safe_provider_error() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake timed out", request=request)

    with JupiterClient("test-key") as jupiter:
        jupiter._client.close()
        jupiter._client = httpx.Client(
            base_url="https://api.jup.ag", transport=httpx.MockTransport(time_out)
        )
        with pytest.raises(JupiterError, match="Jupiter transport is unavailable"):
            jupiter.router_quote(
                str(Pubkey.new_unique()),
                WSOL_MINT,
                100,
                taker=str(Pubkey.new_unique()),
            )
