"""Alerting for key bot events (console for now, Discord/email can be added)."""

import structlog

log = structlog.get_logger()


def alert_trade_executed(ticker: str, side: str, count: int, price: int):
    log.info("ALERT_TRADE",
             msg=f"Executed: {side.upper()} {count}x {ticker} @ {price}c",
             ticker=ticker, side=side, count=count, price=price)


def alert_circuit_breaker(reason: str):
    log.warning("ALERT_CIRCUIT_BREAKER",
                msg=f"Circuit breaker triggered: {reason}",
                reason=reason)


def alert_signal_found(ticker: str, direction: str, edge: float, confidence: float):
    log.info("ALERT_SIGNAL",
             msg=f"Signal: {direction} {ticker} | Edge: {edge:.1%} | Confidence: {confidence:.1%}",
             ticker=ticker, direction=direction, edge=edge, confidence=confidence)


def alert_error(component: str, error: str):
    log.error("ALERT_ERROR",
              msg=f"Error in {component}: {error}",
              component=component, error=error)


def alert_bot_started(mode: str):
    log.info("ALERT_BOT_STARTED",
             msg=f"Bot started in {mode} mode",
             mode=mode)


def alert_fomc_approaching(days: int, meeting_date: str):
    log.info("ALERT_FOMC",
             msg=f"FOMC meeting in {days} days ({meeting_date})",
             days=days, meeting_date=meeting_date)
