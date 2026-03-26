"""Alerting for key bot events — logs + Telegram notifications."""

import asyncio

import structlog

from config.settings import settings

log = structlog.get_logger()


def _fire_and_forget(coro):
    """Schedule an async coroutine from sync context without blocking."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        pass  # No event loop — skip Telegram (e.g., during tests)


def alert_trade_executed(ticker: str, side: str, count: int, price: int,
                         edge: float = 0.0):
    log.info("ALERT_TRADE",
             msg=f"Executed: {side.upper()} {count}x {ticker} @ {price}c",
             ticker=ticker, side=side, count=count, price=price)
    from monitoring.telegram import send_trade_alert
    _fire_and_forget(send_trade_alert(ticker, side, count, price, edge, settings.trading_mode))


def alert_exit(ticker: str, action: str, pnl: float):
    log.info("ALERT_EXIT",
             msg=f"Exit: {ticker} — {action}, P&L: ${pnl:.2f}",
             ticker=ticker, action=action, pnl=pnl)
    from monitoring.telegram import send_exit_alert
    _fire_and_forget(send_exit_alert(ticker, action, pnl))


def alert_circuit_breaker(reason: str):
    log.warning("ALERT_CIRCUIT_BREAKER",
                msg=f"Circuit breaker triggered: {reason}",
                reason=reason)
    from monitoring.telegram import send_circuit_breaker_alert
    _fire_and_forget(send_circuit_breaker_alert(reason))


def alert_signal_found(ticker: str, direction: str, edge: float, confidence: float):
    log.info("ALERT_SIGNAL",
             msg=f"Signal: {direction} {ticker} | Edge: {edge:.1%} | Confidence: {confidence:.1%}",
             ticker=ticker, direction=direction, edge=edge, confidence=confidence)
    from monitoring.telegram import send_signal_alert
    _fire_and_forget(send_signal_alert(ticker, direction, edge, confidence))


def alert_error(component: str, error: str):
    log.error("ALERT_ERROR",
              msg=f"Error in {component}: {error}",
              component=component, error=error)
    from monitoring.telegram import send_error_alert
    _fire_and_forget(send_error_alert(component, error))


def alert_bot_started(mode: str, balance: float | None = None):
    log.info("ALERT_BOT_STARTED",
             msg=f"Bot started in {mode} mode",
             mode=mode)
    from monitoring.telegram import send_bot_started
    _fire_and_forget(send_bot_started(mode, balance))


def alert_event_approaching(event_name: str, days: int, event_date: str):
    log.info("ALERT_EVENT",
             msg=f"{event_name} in {days} days ({event_date})",
             event_name=event_name, days=days, event_date=event_date)
    from monitoring.telegram import send_event_approaching
    _fire_and_forget(send_event_approaching(event_name, days, event_date))


def alert_kill_switch():
    log.warning("ALERT_KILL_SWITCH", msg="Kill switch activated — all trading halted")
    from monitoring.telegram import send_kill_switch_alert
    _fire_and_forget(send_kill_switch_alert())


async def alert_daily_summary(summary: dict):
    log.info("ALERT_DAILY_SUMMARY",
             balance=summary.get("balance", 0),
             pnl=summary.get("net_pnl_today", 0))
    from monitoring.telegram import send_daily_summary
    await send_daily_summary(summary)
