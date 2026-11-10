from __future__ import annotations

import httpx
import pytest

import memecoin_bot.rpc as rpc_module
from memecoin_bot.rpc import RpcClient


def test_rpc_retries_429_and_honors_larger_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "1.25"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    monkeypatch.setattr(rpc_module, "_pace_rpc_request", lambda: None)
    monkeypatch.setattr("memecoin_bot.rpc.time.sleep", sleeps.append)
    client = RpcClient("https://rpc.example")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    with client:
        assert client.call("getHealth", []) == "ok"

    assert calls == 2
    assert sleeps == [1.25]
    assert client.request_counts() == {"getHealth": 2}


def test_rpc_process_pacer_serializes_adjacent_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("memecoin_bot.rpc.time.monotonic", lambda: 10.0)
    monkeypatch.setattr("memecoin_bot.rpc.time.sleep", sleeps.append)
    monkeypatch.setattr(rpc_module, "_RPC_NEXT_REQUEST_AT", 0.0)

    rpc_module._pace_rpc_request()
    rpc_module._pace_rpc_request()

    assert sleeps == [pytest.approx(rpc_module._RPC_MIN_INTERVAL_SECONDS)]
