from __future__ import annotations

from pathlib import Path

import pytest

from memecoin_bot.config import ConfigurationError, Settings


def test_settings_are_dry_run_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    settings = Settings.from_env()
    assert settings.live_trading_enabled is False
    assert settings.keypair_path is None
    assert settings.paper_automation_autostart is False
    assert settings.pump_api_key is None
    assert settings.dexscreener_discovery_enabled is True
    assert settings.narrative_intelligence_enabled is True
    assert settings.narrative_google_news_enabled is True
    assert settings.narrative_openai_enabled is False
    assert settings.token_forensics_enabled is True
    assert settings.forensics_origin_trace_enabled is True
    assert settings.forensics_max_single_controller_bps == 1_500
    assert settings.paper_tp_levels_bps == (10_000, 30_000, 90_000)
    assert settings.paper_tp_sell_bps == (5_000, 2_000, 1_500)


def test_rpc_must_use_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "http://127.0.0.1:8899")
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_env()


def test_embedded_rpc_credentials_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "https://user:pass@example.com")
    with pytest.raises(ConfigurationError, match="without embedded credentials"):
        Settings.from_env()


def test_api_keys_are_loaded_without_appearing_in_settings_repr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JUPITER_API_KEY", "jupiter-test-secret")
    monkeypatch.setenv("PUMP_API_KEY", "pump-test-secret")
    monkeypatch.setenv("COINGECKO_DEMO_API_KEY", "coingecko-test-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    settings = Settings.from_env()
    assert settings.coingecko_demo_api_key == "coingecko-test-secret"
    assert settings.openai_api_key == "openai-test-secret"
    assert all(
        secret not in repr(settings)
        for secret in (
            "jupiter-test-secret",
            "pump-test-secret",
            "coingecko-test-secret",
            "openai-test-secret",
        )
    )


def test_openai_assessment_requires_explicit_opt_in_and_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("NARRATIVE_OPENAI_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="requires OPENAI_API_KEY"):
        Settings.from_env()


def test_trailing_stop_must_be_below_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PAPER_TRAILING_ACTIVATION_BPS", "1000")
    monkeypatch.setenv("PAPER_TRAILING_STOP_BPS", "1500")
    with pytest.raises(ConfigurationError, match="must be below"):
        Settings.from_env()


def test_take_profit_ladder_requires_matching_lengths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PAPER_TP_LEVELS_BPS", "10000,30000")
    monkeypatch.setenv("PAPER_TP_SELL_BPS", "5000")
    with pytest.raises(ConfigurationError, match="same number"):
        Settings.from_env()
