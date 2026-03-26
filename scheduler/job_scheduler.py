"""Economic event-aware job scheduler — ramps up polling near any tracked release."""

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
import structlog

from config.settings import settings
from config.economic_calendar import (
    ET, get_upcoming_events, is_event_day, is_in_any_blackout,
    days_to_next_event, get_next_event,
)
from data.kalshi_client import KalshiClient
from signals.signal_aggregator import generate_signals
from strategy.trade_decision import decide_trades
from execution.order_manager import execute_all_orders
from execution.position_monitor import check_exits
from monitoring.pnl_tracker import get_portfolio_summary
from db.database import insert_price_snapshot
from monitoring.alerts import (
    alert_signal_found, alert_trade_executed, alert_circuit_breaker,
    alert_event_approaching, alert_error, alert_kill_switch,
    alert_daily_summary,
)

log = structlog.get_logger()


def _run_async(coro_func):
    """Wrap an async method so APScheduler 3.x can call it."""
    def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro_func(*args, **kwargs))
    return wrapper


def _check_kill_switch() -> bool:
    """Returns True if kill switch is active."""
    return Path(settings.kill_switch_path).exists()


class BotScheduler:
    def __init__(self, kalshi: KalshiClient):
        self.kalshi = kalshi
        self.scheduler = AsyncIOScheduler(timezone=ET)
        self._current_interval = None
        self._running = False

    def start(self):
        """Start the scheduler with event-aware intervals."""
        self._update_interval()

        # Frequency adjuster: checks every minute if we need to change polling speed
        self.scheduler.add_job(
            _run_async(self._adjust_frequency),
            IntervalTrigger(minutes=1),
            id="frequency_adjuster",
            max_instances=1,
            misfire_grace_time=30,
            coalesce=True,
        )

        # Daily summary at 6 PM ET
        self.scheduler.add_job(
            _run_async(self._daily_summary),
            CronTrigger(hour=18, minute=0, timezone=ET),
            id="daily_summary",
            max_instances=1,
            misfire_grace_time=3600,
        )

        self.scheduler.start()
        log.info("scheduler_started", interval=self._current_interval,
                 mode=settings.trading_mode)

    def stop(self):
        self.scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def _get_optimal_interval(self) -> int:
        """Determine polling interval based on proximity to ANY economic event."""
        now = datetime.now(ET)

        # Check blackout for any event
        blacked, reason = is_in_any_blackout(now)
        if blacked:
            return 0

        # Check if today is any event day
        todays_events = is_event_day(now.date())
        if todays_events:
            return settings.poll_interval_fomc_day  # 30 sec on event days

        # Check proximity to next event of any type
        days = days_to_next_event()
        if days is not None:
            if days <= 1:
                return settings.poll_interval_fomc_day
            elif days <= 7:
                return settings.poll_interval_fomc_week
            elif days <= 14:
                return settings.poll_interval_fomc_week * 2  # 10 min

        return settings.poll_interval_normal

    def _update_interval(self):
        """Update the main job interval."""
        interval = self._get_optimal_interval()

        if interval == self._current_interval:
            return

        if self.scheduler.get_job("main_loop"):
            self.scheduler.remove_job("main_loop")

        if interval > 0:
            self.scheduler.add_job(
                _run_async(self._main_loop),
                IntervalTrigger(seconds=interval),
                id="main_loop",
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=30,
                coalesce=True,
            )
            log.info("interval_updated", seconds=interval)
        else:
            log.info("trading_paused_blackout")

        self._current_interval = interval

    async def _adjust_frequency(self):
        """Check if we need to change polling frequency."""
        self._update_interval()

        # Alert about upcoming events within 7 days
        upcoming = get_upcoming_events(within_days=7)
        for event in upcoming:
            days = (event.date - datetime.now(ET).date()).days
            if days <= 3:
                alert_event_approaching(event.name, days, str(event.date))

    async def _main_loop(self):
        """Main trading loop — fetch data, generate signals, execute trades."""
        # Kill switch check FIRST
        if _check_kill_switch():
            log.warning("kill_switch_active")
            alert_kill_switch()
            return

        if self._running:
            log.debug("main_loop_already_running")
            return
        self._running = True

        try:
            log.info("main_loop_tick", mode=settings.trading_mode)

            # Get upcoming events to process
            upcoming = get_upcoming_events(within_days=7)

            # 0. Check exits on existing positions first
            exits = await check_exits(self.kalshi)
            if exits:
                for ex in exits:
                    log.info("position_exit", ticker=ex["ticker"],
                             action=ex["action"], pnl=round(ex.get("pnl", 0), 2))

            # 1. Generate signals for all upcoming events
            signals = await generate_signals(self.kalshi, upcoming if upcoming else None)

            # Track price history for all signals
            for s in (signals or []):
                try:
                    await insert_price_snapshot(
                        market_ticker=s.market_ticker,
                        yes_price=s.kalshi_price,
                        no_price=1.0 - s.kalshi_price,
                        volume=0,
                        fedwatch_prob=s.fedwatch_prob,
                        spread=s.edge_estimate,
                    )
                except Exception:
                    pass

            if not signals:
                log.info("no_signals_this_tick")
                return

            for s in signals:
                alert_signal_found(s.market_ticker, s.direction,
                                   s.edge_estimate, s.confidence)

            # 2. Decide trades
            orders = await decide_trades(signals, self.kalshi)

            if not orders:
                log.info("no_trades_after_risk_check")
                return

            # 3. Execute
            results = await execute_all_orders(self.kalshi, orders)

            for order, result in zip(orders, results):
                if result:
                    alert_trade_executed(
                        order.market_ticker, order.side,
                        order.count, order.price,
                        edge=order.signal.edge_estimate,
                    )

            # 4. Portfolio summary
            await get_portfolio_summary(self.kalshi)

        except Exception as e:
            alert_error("main_loop", str(e))
            log.exception("main_loop_error")
        finally:
            self._running = False

    async def _daily_summary(self):
        """Send daily P&L summary via Telegram."""
        try:
            summary = await get_portfolio_summary(self.kalshi)

            # Add upcoming events info
            upcoming = get_upcoming_events(within_days=7)
            summary["upcoming_events"] = [
                {"name": e.name, "date": str(e.date), "type": e.event_type}
                for e in upcoming
            ]

            await alert_daily_summary(summary)
        except Exception as e:
            log.error("daily_summary_failed", error=str(e))
