from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from test_dashboard import _settings

from memecoin_bot import cli
from memecoin_bot.config import ConfigurationError
from memecoin_bot.dashboard import create_dashboard_app
from memecoin_bot.service import RiskRejectedError, TradingService


def test_service_live_execution_is_hard_locked_even_when_env_flag_is_true(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), live_trading_enabled=True)
    service = TradingService(
        settings=settings,
        rpc=cast(Any, None),
        jupiter=cast(Any, None),
        keypair=Keypair(),
        journal=cast(Any, None),
    )
    with pytest.raises(RiskRejectedError, match="locked"):
        service.buy(
            mint=str(Keypair().pubkey()),
            lamports=1_000_000,
            slippage_bps=100,
            execute=True,
            acknowledgement="I_ACCEPT_MAINNET_RISK",
        )


def test_dashboard_live_endpoint_is_hard_locked(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), live_trading_enabled=True)
    app = create_dashboard_app(settings, "test-pairing-code")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        paired = client.post("/api/auth/pair", json={"code": "test-pairing-code"})
        assert paired.status_code == 200
        response = client.post(
            "/api/trades/execute",
            json={"preview_id": "0" * 32, "acknowledgement": "I_ACCEPT_MAINNET_RISK"},
            headers={"x-csrf-token": paired.json()["csrf_token"]},
        )
        assert response.status_code == 409
        assert "locked" in response.json()["detail"]


def test_free_only_dashboard_refuses_paid_or_live_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = cli.build_parser().parse_args(["dashboard", "--no-browser", "--free-only"])
    safe_settings = _settings(tmp_path)
    started: list[bool] = []
    monkeypatch.setattr(cli, "_settings", lambda _: safe_settings)
    monkeypatch.setattr(
        "memecoin_bot.dashboard.run_dashboard", lambda *_a, **_k: started.append(True)
    )
    assert cli._cmd_dashboard(args) == 0
    assert started == [True]
    for unsafe in (
        replace(safe_settings, live_trading_enabled=True),
        replace(safe_settings, narrative_openai_enabled=True, openai_api_key="test-key"),
    ):
        monkeypatch.setattr(cli, "_settings", lambda _, value=unsafe: value)
        with pytest.raises(ConfigurationError, match="Free-only dashboard"):
            cli._cmd_dashboard(args)
    assert started == [True]
