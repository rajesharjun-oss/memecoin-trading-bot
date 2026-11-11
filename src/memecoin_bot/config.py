from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from solders.pubkey import Pubkey

from .constants import DEFAULT_DISCOVERY_PROGRAM_IDS


class ConfigurationError(ValueError):
    """Raised when configuration is missing or unsafe."""


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return raw == "true"


def _https_url(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError(f"{name} must be an HTTPS URL without embedded credentials")
    if parsed.fragment:
        raise ConfigurationError(f"{name} must not contain a fragment")
    return value


def _pubkey_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, ",".join(default))
    values = tuple(dict.fromkeys(value.strip() for value in raw.split(",") if value.strip()))
    if not 1 <= len(values) <= 10:
        raise ConfigurationError(f"{name} must contain between 1 and 10 program IDs")
    for value in values:
        try:
            Pubkey.from_string(value)
        except ValueError as exc:
            raise ConfigurationError(f"{name} contains an invalid Solana program ID") from exc
    return values


def _integer_list(
    name: str,
    default: tuple[int, ...],
    *,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    raw = os.getenv(name, ",".join(str(value) for value in default))
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not 1 <= len(parts) <= 10:
        raise ConfigurationError(f"{name} must contain between 1 and 10 comma-separated values")
    try:
        values = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must contain only integers") from exc
    if any(value < minimum or value > maximum for value in values):
        raise ConfigurationError(f"{name} values must each be between {minimum} and {maximum}")
    return values


@dataclass(frozen=True, slots=True)
class Settings:
    rpc_url: str
    keypair_path: Path | None
    jupiter_api_key: str | None = field(repr=False)
    live_trading_enabled: bool
    max_trade_lamports: int
    max_daily_lamports: int
    max_slippage_bps: int
    max_price_impact_bps: int
    max_top10_concentration_bps: int
    max_priority_fee_lamports: int
    confirmation_timeout_seconds: int
    allow_token_2022: bool
    state_path: Path
    pump_api_key: str | None = field(default=None, repr=False)
    coingecko_demo_api_key: str | None = field(default=None, repr=False)
    dexscreener_discovery_enabled: bool = True
    discovery_program_ids: tuple[str, ...] = DEFAULT_DISCOVERY_PROGRAM_IDS
    discovery_signatures_per_program: int = 8
    discovery_max_transactions: int = 30
    discovery_candidate_limit: int = 20
    paper_starting_lamports: int = 1_000_000_000
    strategy_min_liquidity_usd: int = 25_000
    strategy_min_volume_24h_usd: int = 10_000
    strategy_min_score: int = 65
    paper_risk_per_trade_bps: int = 100
    strategy_max_open_positions: int = 3
    paper_automation_autostart: bool = False
    paper_automation_interval_seconds: int = 300
    paper_tp_levels_bps: tuple[int, ...] = (10_000, 30_000, 90_000)
    paper_tp_sell_bps: tuple[int, ...] = (5_000, 2_000, 1_500)
    paper_stop_loss_bps: int = 3_500
    paper_trailing_activation_bps: int = 3_000
    paper_trailing_stop_bps: int = 1_500
    paper_max_hold_seconds: int = 86_400
    paper_max_drawdown_bps: int = 1_000
    paper_daily_loss_bps: int = 500
    paper_max_consecutive_losses: int = 3
    paper_liquidity_breaker_usd: int = 15_000
    strategy_lab_collection_enabled: bool = True
    strategy_lab_collection_interval_seconds: int = 30
    narrative_intelligence_enabled: bool = False
    narrative_gdelt_enabled: bool = True
    narrative_google_news_enabled: bool = True
    narrative_bluesky_enabled: bool = True
    narrative_refresh_interval_seconds: int = 300
    openai_api_key: str | None = field(default=None, repr=False)
    narrative_openai_enabled: bool = False
    narrative_openai_daily_limit: int = 10
    token_forensics_enabled: bool = True
    forensics_origin_trace_enabled: bool = True
    forensics_max_single_controller_bps: int = 1_500
    forensics_signature_limit: int = 100
    forensics_transaction_candidates: int = 5
    paper_execution_base_slippage_bps: int = 30
    paper_execution_liquidity_shock_bps: int = 0
    paper_execution_network_fee_lamports: int = 5_000
    paper_execution_priority_fee_lamports: int = 50_000
    paper_execution_ata_rent_lamports: int = 2_039_280
    paper_execution_max_quote_age_ms: int = 5_000
    paper_execution_max_trade_liquidity_bps: int = 100
    operations_backup_enabled: bool = True
    operations_backup_interval_seconds: int = 86_400
    operations_backup_directory: Path | None = None
    alerts_enabled: bool = False
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.narrative_openai_enabled and not self.openai_api_key:
            raise ConfigurationError("NARRATIVE_OPENAI_ENABLED requires OPENAI_API_KEY")
        if self.paper_trailing_stop_bps >= self.paper_trailing_activation_bps:
            raise ConfigurationError(
                "PAPER_TRAILING_STOP_BPS must be below PAPER_TRAILING_ACTIVATION_BPS"
            )
        if len(self.paper_tp_levels_bps) != len(self.paper_tp_sell_bps):
            raise ConfigurationError(
                "PAPER_TP_LEVELS_BPS and PAPER_TP_SELL_BPS must contain the same number of values"
            )
        if any(
            current <= previous
            for previous, current in zip(
                self.paper_tp_levels_bps, self.paper_tp_levels_bps[1:], strict=False
            )
        ):
            raise ConfigurationError("PAPER_TP_LEVELS_BPS must be strictly increasing")
        if sum(self.paper_tp_sell_bps) > 10_000:
            raise ConfigurationError("PAPER_TP_SELL_BPS cannot sell more than 100% in total")
        if self.paper_execution_priority_fee_lamports > self.max_priority_fee_lamports:
            raise ConfigurationError(
                "PAPER_EXECUTION_PRIORITY_FEE_LAMPORTS cannot exceed MAX_PRIORITY_FEE_LAMPORTS"
            )
        if (
            self.paper_execution_base_slippage_bps + self.paper_execution_liquidity_shock_bps
            > self.max_slippage_bps
        ):
            raise ConfigurationError(
                "Paper execution slippage plus liquidity shock cannot exceed MAX_SLIPPAGE_BPS"
            )

    @property
    def paper_exit_interval_seconds(self) -> int:
        """Keep exit marks inside the stricter three-minute entry freshness gate."""
        return min(120, self.paper_automation_interval_seconds)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        # Load only the explicitly selected file or the caller's working-directory
        # .env. python-dotenv's implicit search otherwise starts near this source
        # file and can unexpectedly import a developer's project credentials.
        load_dotenv(dotenv_path=env_file or Path.cwd() / ".env", override=False)
        keypair = os.getenv("SOLANA_KEYPAIR_PATH", "").strip()
        api_key = os.getenv("JUPITER_API_KEY", "").strip()
        pump_api_key = os.getenv("PUMP_API_KEY", "").strip()
        coingecko_demo_api_key = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        return cls(
            rpc_url=_https_url("SOLANA_RPC_URL", "https://api.mainnet.solana.com"),
            keypair_path=Path(keypair).expanduser().resolve() if keypair else None,
            jupiter_api_key=api_key or None,
            coingecko_demo_api_key=coingecko_demo_api_key or None,
            openai_api_key=openai_api_key or None,
            live_trading_enabled=_boolean("LIVE_TRADING_ENABLED"),
            max_trade_lamports=_integer(
                "MAX_TRADE_LAMPORTS", 50_000_000, minimum=1, maximum=10_000_000_000
            ),
            max_daily_lamports=_integer(
                "MAX_DAILY_LAMPORTS", 100_000_000, minimum=1, maximum=100_000_000_000
            ),
            max_slippage_bps=_integer("MAX_SLIPPAGE_BPS", 300, minimum=1, maximum=2_000),
            max_price_impact_bps=_integer("MAX_PRICE_IMPACT_BPS", 500, minimum=1, maximum=5_000),
            max_top10_concentration_bps=_integer(
                "MAX_TOP10_CONCENTRATION_BPS", 7_000, minimum=1, maximum=10_000
            ),
            max_priority_fee_lamports=_integer(
                "MAX_PRIORITY_FEE_LAMPORTS", 1_000_000, minimum=0, maximum=100_000_000
            ),
            confirmation_timeout_seconds=_integer(
                "CONFIRMATION_TIMEOUT_SECONDS", 45, minimum=5, maximum=300
            ),
            allow_token_2022=_boolean("ALLOW_TOKEN_2022"),
            state_path=Path(os.getenv("TRADE_STATE_PATH", "./trade-state.sqlite3")).resolve(),
            pump_api_key=pump_api_key or None,
            dexscreener_discovery_enabled=_boolean("DEXSCREENER_DISCOVERY_ENABLED", True),
            discovery_program_ids=_pubkey_list(
                "DISCOVERY_PROGRAM_IDS", DEFAULT_DISCOVERY_PROGRAM_IDS
            ),
            discovery_signatures_per_program=_integer(
                "DISCOVERY_SIGNATURES_PER_PROGRAM", 8, minimum=1, maximum=25
            ),
            discovery_max_transactions=_integer(
                "DISCOVERY_MAX_TRANSACTIONS", 30, minimum=1, maximum=100
            ),
            discovery_candidate_limit=_integer(
                "DISCOVERY_CANDIDATE_LIMIT", 20, minimum=1, maximum=30
            ),
            paper_starting_lamports=_integer(
                "PAPER_STARTING_LAMPORTS",
                1_000_000_000,
                minimum=1_000_000,
                maximum=1_000_000_000_000,
            ),
            strategy_min_liquidity_usd=_integer(
                "STRATEGY_MIN_LIQUIDITY_USD", 25_000, minimum=1_000, maximum=100_000_000
            ),
            strategy_min_volume_24h_usd=_integer(
                "STRATEGY_MIN_VOLUME_24H_USD", 10_000, minimum=0, maximum=1_000_000_000
            ),
            strategy_min_score=_integer("STRATEGY_MIN_SCORE", 65, minimum=1, maximum=100),
            paper_risk_per_trade_bps=_integer(
                "PAPER_RISK_PER_TRADE_BPS", 100, minimum=1, maximum=2_000
            ),
            strategy_max_open_positions=_integer(
                "STRATEGY_MAX_OPEN_POSITIONS", 3, minimum=1, maximum=20
            ),
            paper_automation_autostart=_boolean("PAPER_AUTOMATION_AUTOSTART"),
            paper_automation_interval_seconds=_integer(
                "PAPER_AUTOMATION_INTERVAL_SECONDS", 300, minimum=30, maximum=86_400
            ),
            paper_tp_levels_bps=_integer_list(
                "PAPER_TP_LEVELS_BPS",
                (10_000, 30_000, 90_000),
                minimum=100,
                maximum=1_000_000,
            ),
            paper_tp_sell_bps=_integer_list(
                "PAPER_TP_SELL_BPS",
                (5_000, 2_000, 1_500),
                minimum=1,
                maximum=10_000,
            ),
            paper_stop_loss_bps=_integer("PAPER_STOP_LOSS_BPS", 3_500, minimum=100, maximum=9_500),
            paper_trailing_activation_bps=_integer(
                "PAPER_TRAILING_ACTIVATION_BPS", 3_000, minimum=100, maximum=100_000
            ),
            paper_trailing_stop_bps=_integer(
                "PAPER_TRAILING_STOP_BPS", 1_500, minimum=100, maximum=9_500
            ),
            paper_max_hold_seconds=_integer(
                "PAPER_MAX_HOLD_SECONDS", 86_400, minimum=60, maximum=2_592_000
            ),
            paper_max_drawdown_bps=_integer(
                "PAPER_MAX_DRAWDOWN_BPS", 1_000, minimum=100, maximum=9_500
            ),
            paper_daily_loss_bps=_integer("PAPER_DAILY_LOSS_BPS", 500, minimum=100, maximum=9_500),
            paper_max_consecutive_losses=_integer(
                "PAPER_MAX_CONSECUTIVE_LOSSES", 3, minimum=1, maximum=20
            ),
            paper_liquidity_breaker_usd=_integer(
                "PAPER_LIQUIDITY_BREAKER_USD", 15_000, minimum=1_000, maximum=100_000_000
            ),
            strategy_lab_collection_enabled=_boolean("STRATEGY_LAB_COLLECTION_ENABLED", True),
            strategy_lab_collection_interval_seconds=_integer(
                "STRATEGY_LAB_COLLECTION_INTERVAL_SECONDS", 30, minimum=30, maximum=3_600
            ),
            narrative_intelligence_enabled=_boolean("NARRATIVE_INTELLIGENCE_ENABLED", True),
            narrative_gdelt_enabled=_boolean("NARRATIVE_GDELT_ENABLED", True),
            narrative_google_news_enabled=_boolean("NARRATIVE_GOOGLE_NEWS_ENABLED", True),
            narrative_bluesky_enabled=_boolean("NARRATIVE_BLUESKY_ENABLED", True),
            narrative_refresh_interval_seconds=_integer(
                "NARRATIVE_REFRESH_INTERVAL_SECONDS", 300, minimum=60, maximum=86_400
            ),
            narrative_openai_enabled=_boolean("NARRATIVE_OPENAI_ENABLED", False),
            narrative_openai_daily_limit=_integer(
                "NARRATIVE_OPENAI_DAILY_LIMIT", 10, minimum=1, maximum=100
            ),
            token_forensics_enabled=_boolean("TOKEN_FORENSICS_ENABLED", True),
            forensics_origin_trace_enabled=_boolean("FORENSICS_ORIGIN_TRACE_ENABLED", True),
            forensics_max_single_controller_bps=_integer(
                "FORENSICS_MAX_SINGLE_CONTROLLER_BPS", 1_500, minimum=1, maximum=10_000
            ),
            forensics_signature_limit=_integer(
                "FORENSICS_SIGNATURE_LIMIT", 100, minimum=10, maximum=1_000
            ),
            forensics_transaction_candidates=_integer(
                "FORENSICS_TRANSACTION_CANDIDATES", 5, minimum=1, maximum=20
            ),
            paper_execution_base_slippage_bps=_integer(
                "PAPER_EXECUTION_BASE_SLIPPAGE_BPS", 30, minimum=0, maximum=2_000
            ),
            paper_execution_liquidity_shock_bps=_integer(
                "PAPER_EXECUTION_LIQUIDITY_SHOCK_BPS", 0, minimum=0, maximum=2_000
            ),
            paper_execution_network_fee_lamports=_integer(
                "PAPER_EXECUTION_NETWORK_FEE_LAMPORTS", 5_000, minimum=0, maximum=1_000_000
            ),
            paper_execution_priority_fee_lamports=_integer(
                "PAPER_EXECUTION_PRIORITY_FEE_LAMPORTS",
                50_000,
                minimum=0,
                maximum=100_000_000,
            ),
            paper_execution_ata_rent_lamports=_integer(
                "PAPER_EXECUTION_ATA_RENT_LAMPORTS",
                2_039_280,
                minimum=0,
                maximum=10_000_000,
            ),
            paper_execution_max_quote_age_ms=_integer(
                "PAPER_EXECUTION_MAX_QUOTE_AGE_MS", 5_000, minimum=250, maximum=60_000
            ),
            paper_execution_max_trade_liquidity_bps=_integer(
                "PAPER_EXECUTION_MAX_TRADE_LIQUIDITY_BPS",
                100,
                minimum=1,
                maximum=10_000,
            ),
            operations_backup_enabled=_boolean("OPERATIONS_BACKUP_ENABLED", True),
            operations_backup_interval_seconds=_integer(
                "OPERATIONS_BACKUP_INTERVAL_SECONDS", 86_400, minimum=300, maximum=2_592_000
            ),
            operations_backup_directory=(
                Path(os.environ["OPERATIONS_BACKUP_DIRECTORY"]).expanduser().resolve()
                if os.getenv("OPERATIONS_BACKUP_DIRECTORY", "").strip()
                else None
            ),
            alerts_enabled=_boolean("ALERTS_ENABLED"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
        )
