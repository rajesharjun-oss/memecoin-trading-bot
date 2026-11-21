"""The research probe must be bounded and never imply candle accuracy."""

from __future__ import annotations

import sys
from decimal import Decimal
from types import CoroutineType
from unittest.mock import Mock

import pytest

from memecoin_bot.pump_amm_shadow import PUMP_AMM_PROGRAM, PoolIdentity, SwapObservation
from memecoin_bot.shadow_swap_stream import (
    ProbeError,
    _compare,
    _compare_native_prices,
    _message_bytes,
    _notification,
    _record_trade_events,
    _reference_window,
    main,
    websocket_url,
)


def test_websocket_url_accepts_only_the_fixed_helius_host() -> None:
    key_marker = "private-test-key"
    assert (
        websocket_url(f"https://mainnet.helius-rpc.com/?api-key={key_marker}")
        == f"wss://mainnet.helius-rpc.com/?api-key={key_marker}"
    )
    for bad in (
        f"https://attacker.example/?api-key={key_marker}",
        f"http://mainnet.helius-rpc.com/?api-key={key_marker}",
        f"https://mainnet.helius-rpc.com/?api-key={key_marker}&extra=1",
    ):
        with pytest.raises(ProbeError) as error:
            websocket_url(bad)
        assert key_marker not in str(error.value)


def test_notification_requires_a_known_subscription_and_signature() -> None:
    valid = {
        "method": "logsNotification",
        "params": {"subscription": 12, "result": {"value": {"signature": "sig"}}},
    }
    assert _notification(valid, {12: "pool"}) == ("pool", "sig")
    with pytest.raises(ProbeError):
        _notification(valid, {13: "other"})


def test_reference_window_is_bounded_and_requires_both_anchors() -> None:
    client = Mock()
    client.post.return_value.status_code = 200
    client.post.return_value.content = b"{}"
    client.post.return_value.json.return_value = {
        "result": [{"signature": value} for value in ("after", "end", "middle", "start")]
    }
    signatures, complete, calls = _reference_window(
        client, "https://example", "pool", "start", "end"
    )
    assert complete and calls == 1
    assert signatures == {"end", "middle"}

    signatures, complete, calls = _reference_window(
        client, "https://example", "pool", "missing", "end"
    )
    assert not complete and calls == 1
    assert signatures == set()


def test_coverage_is_undefined_when_reference_is_incomplete_or_empty() -> None:
    incomplete = _compare("pool", {"one"}, set(), False)
    assert incomplete.coverage_pct is None
    assert incomplete.reference_events is None
    empty = _compare("pool", {"one"}, set(), True)
    assert empty.coverage_pct is None
    measured = _compare("pool", {"one", "extra"}, {"one", "missing"}, True)
    assert measured.missed_events == 1
    assert measured.extra_events == 1
    assert measured.coverage_pct == 50.0


def test_stream_cost_uses_utf8_bytes() -> None:
    assert _message_bytes("é") == 2
    assert _message_bytes(b"abc") == 3


def test_transport_error_cannot_print_rpc_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key_marker = "private-test-key"
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--pool", "6Z4G14B6P7wE7CVmcmE1CojBhbcxxyEzpzC7rJsCNUFL", "--seconds", "10"],
    )
    monkeypatch.setenv("SOLANA_RPC_URL", f"https://mainnet.helius-rpc.com/?api-key={key_marker}")
    monkeypatch.setattr("memecoin_bot.shadow_swap_stream.load_dotenv", lambda: None)

    def fail(_coroutine: object) -> None:
        if isinstance(_coroutine, CoroutineType):
            _coroutine.close()
        raise RuntimeError(f"transport failed at https://example/?api-key={key_marker}")

    monkeypatch.setattr("memecoin_bot.shadow_swap_stream.asyncio.run", fail)
    assert main() == 1
    output = capsys.readouterr().out
    assert key_marker not in output
    assert "RuntimeError" in output


def test_native_price_check_uses_closed_quote_token_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = 1_700_000_040
    monkeypatch.setattr("memecoin_bot.shadow_swap_stream.time.time", lambda: opened + 180)
    identity = PoolIdentity("pool", "mint", 6, True)
    client = Mock()
    client.get.return_value.status_code = 200
    client.get.return_value.content = b"{}"
    client.get.return_value.json.return_value = {
        "meta": {"base": {"address": "sol"}, "quote": {"address": "mint"}},
        "data": {
            "attributes": {"ohlcv_list": [[opened, "0.0005", "0.0006", "0.0004", "0.0005", "10"]]}
        },
    }
    trade = SwapObservation(opened + 15, "buy", 1_000_000, 500, Decimal("0.0005"))
    report = _compare_native_prices(client, identity, {"sig"}, {"sig": (trade,)})
    assert report["matched_events"] == 1
    assert report["outside_reference_range"] == 0
    assert report["status"] == "provisional_event_price_check"
    assert "api.geckoterminal.com" in client.get.call_args.args[0]


def test_failed_transaction_cannot_become_a_price_event() -> None:
    identity = PoolIdentity("pool", "mint", 6, True)
    trades: dict[str, tuple[SwapObservation, ...]] = {}
    message = {
        "params": {"result": {"value": {"signature": "sig", "err": {"InstructionError": [0, 1]}}}}
    }
    _record_trade_events(message, identity, trades)
    assert trades == {}
    successful = {
        "params": {
            "result": {
                "value": {
                    "signature": "sig",
                    "err": None,
                    "logs": [
                        f"Program {PUMP_AMM_PROGRAM} invoke [1]",
                        f"Program {PUMP_AMM_PROGRAM} success",
                    ],
                }
            }
        }
    }
    _record_trade_events(successful, identity, trades)
    assert trades == {"sig": ()}
