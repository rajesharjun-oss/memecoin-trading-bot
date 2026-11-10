from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .constants import WSOL_MINT
from .journal import TradeJournal
from .service import TradingService


@dataclass(frozen=True, slots=True)
class PositionEvent:
    action: str
    current_value_lamports: int
    pnl_bps: int
    token_balance: int
    order_id: str | None = None


def monitor_position(
    service: TradingService,
    journal: TradeJournal,
    *,
    position_id: str,
    mint: str,
    entry_lamports: int,
    initial_token_amount: int,
    take_profit_bps: int,
    stop_loss_bps: int,
    take_profit_sell_bps: int,
    slippage_bps: int,
    poll_seconds: int,
    execute: bool,
    acknowledgement: str | None,
    once: bool,
    emit: Callable[[PositionEvent], None],
) -> None:
    if entry_lamports <= 0 or initial_token_amount <= 0:
        raise ValueError("Position amounts must be positive")
    if not 10_001 <= take_profit_bps <= 1_000_000:
        raise ValueError("take_profit_bps must be greater than 10000")
    if not 1 <= stop_loss_bps < 10_000:
        raise ValueError("stop_loss_bps must be between 1 and 9999")
    if not 1 <= take_profit_sell_bps <= 10_000:
        raise ValueError("take_profit_sell_bps must be between 1 and 10000")
    if not 5 <= poll_seconds <= 3_600:
        raise ValueError("poll_seconds must be between 5 and 3600")

    state = journal.ensure_position(
        position_id=position_id,
        mint=mint,
        wallet=str(service.keypair.pubkey()),
        entry_lamports=entry_lamports,
        initial_token_amount=initial_token_amount,
    )
    if state["status"] == "closed":
        raise ValueError("Position is already closed")

    consecutive_errors = 0
    while True:
        try:
            balance = service.wallet_balance(mint)
            if balance <= 0:
                journal.update_position(position_id, status="closed", take_profit_taken=True)
                emit(PositionEvent("closed", 0, 0, 0))
                return
            quote = service.jupiter.quote(mint, WSOL_MINT, balance)
            current_value = quote.out_amount
            pnl_bps = current_value * 10_000 // entry_lamports
            event = PositionEvent("hold", current_value, pnl_bps, balance)

            if pnl_bps <= stop_loss_bps:
                order_id = f"{position_id}-stop-{uuid.uuid4().hex[:12]}"
                service.sell(
                    mint=mint,
                    amount_atomic=balance,
                    slippage_bps=slippage_bps,
                    execute=execute,
                    acknowledgement=acknowledgement,
                    client_order_id=order_id,
                )
                if execute:
                    journal.update_position(
                        position_id,
                        status="closed",
                        take_profit_taken=bool(state["take_profit_taken"]),
                    )
                emit(PositionEvent("stop_loss", current_value, pnl_bps, balance, order_id))
                return

            if pnl_bps >= take_profit_bps and not state["take_profit_taken"]:
                sell_amount = max(1, balance * take_profit_sell_bps // 10_000)
                order_id = f"{position_id}-take-{uuid.uuid4().hex[:12]}"
                service.sell(
                    mint=mint,
                    amount_atomic=sell_amount,
                    slippage_bps=slippage_bps,
                    execute=execute,
                    acknowledgement=acknowledgement,
                    client_order_id=order_id,
                )
                if execute:
                    journal.update_position(position_id, status="partial", take_profit_taken=True)
                emit(PositionEvent("take_profit", current_value, pnl_bps, balance, order_id))
                return

            emit(event)
            consecutive_errors = 0
            if once:
                return
        except KeyboardInterrupt:
            raise
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= 5 or once:
                raise
        time.sleep(poll_seconds)
