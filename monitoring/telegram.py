"""Telegram Bot API notifications for trade alerts, summaries, and errors."""

import asyncio

import httpx
import structlog

from config.settings import settings

log = structlog.get_logger()

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


async def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        resp = await _get_client().post(url, json=payload)
        if resp.status_code != 200:
            log.warning("telegram_send_failed", status=resp.status_code, body=resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("telegram_send_error", error=str(e))
        return False


async def send_trade_alert(ticker: str, side: str, count: int, price: int,
                           edge: float, mode: str = ""):
    mode_tag = f" [{mode.upper()}]" if mode else ""
    text = (
        f"<b>Trade Executed{mode_tag}</b>\n"
        f"  {side.upper()} {count}x <code>{ticker}</code> @ {price}c\n"
        f"  Edge: {edge:.1%}"
    )
    await send_message(text)


async def send_signal_alert(ticker: str, direction: str, edge: float,
                            confidence: float):
    text = (
        f"Signal: <b>{direction}</b> <code>{ticker}</code>\n"
        f"  Edge: {edge:.1%} | Confidence: {confidence:.1%}"
    )
    await send_message(text)


async def send_exit_alert(ticker: str, action: str, pnl: float):
    emoji = "+" if pnl >= 0 else ""
    text = (
        f"<b>Position Exit</b>\n"
        f"  <code>{ticker}</code> — {action}\n"
        f"  P&L: <b>{emoji}${pnl:.2f}</b>"
    )
    await send_message(text)


async def send_daily_summary(summary: dict):
    balance = summary.get("balance", 0)
    positions = summary.get("num_positions", 0)
    trades = summary.get("todays_trades", 0)
    net_pnl = summary.get("net_pnl_today", 0)
    pnl_sign = "+" if net_pnl >= 0 else ""

    text = (
        f"<b>Daily Summary</b>\n"
        f"  Balance: ${balance:.2f}\n"
        f"  Open positions: {positions}\n"
        f"  Trades today: {trades}\n"
        f"  Net P&L: <b>{pnl_sign}${net_pnl:.2f}</b>"
    )
    await send_message(text)


async def send_error_alert(component: str, error: str):
    text = (
        f"<b>Error</b>\n"
        f"  Component: {component}\n"
        f"  <code>{error[:500]}</code>"
    )
    await send_message(text)


async def send_circuit_breaker_alert(reason: str):
    text = f"<b>Circuit Breaker Triggered</b>\n  {reason}"
    await send_message(text)


async def send_bot_started(mode: str, balance: float | None = None):
    bal_line = f"\n  Balance: ${balance:.2f}" if balance is not None else ""
    text = (
        f"<b>Bot Started</b>\n"
        f"  Mode: {mode.upper()}{bal_line}"
    )
    await send_message(text)


async def send_kill_switch_alert():
    text = "<b>KILL SWITCH ACTIVATED</b>\nAll trading halted. Remove KILL_SWITCH file to resume."
    await send_message(text)


async def send_event_approaching(event_name: str, days: int, event_date: str):
    text = (
        f"<b>{event_name}</b> in {days} day{'s' if days != 1 else ''}\n"
        f"  Date: {event_date}"
    )
    await send_message(text)
