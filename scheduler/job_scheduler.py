"""FOMC-aware job scheduler — ramps up polling frequency near meetings."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import structlog

from config.settings import settings
from config.fomc_calendar import (
    is_fomc_week, is_fomc_day, is_in_blackout,
    days_to_next_fomc, get_next_fomc_date, ET,
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
    alert_fomc_approaching, alert_error,
)

log = structlog.get_logger()


def _run_async(coro_func):
    """Wrap an async method so APScheduler 3.x can call it."""
    def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro_func(*args, **kwargs))
    return wrapper


class BotScheduler:
    def __init__(self, kalshi: KalshiClient):
        self.kalshi = kalshi
        self.scheduler = AsyncIOScheduler(timezone=ET)
        self._current_interval = None
        self._running = False

    def start(self):
        """Start the scheduler with FOMC-aware intervals."""
        self._update_interval()
        self.scheduler.add_job(
            _run_async(self._adjust_frequency),
            IntervalTrigger(minutes=1),
            id="frequency_adjuster",
            max_instances=1,
            misfire_grace_time=30,
            coalesce=True,
        )
        self.scheduler.start()
        log.info("scheduler_started", interval=self._current_interval)

    def stop(self):
        self.scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def _get_optimal_interval(self) -> int:
        """Determine polling interval based on proximity to FOMC."""
        now = datetime.now(ET)

        if is_in_blackout(now):
            return 0

        if is_fomc_day(now.date()):
            if now.hour < 14:
                return settings.poll_interval_fomc_day
            elif now.hour == 14 and now.minute < 55:
                return settings.poll_interval_fomc_day
            elif now.hour >= 14 and now.minute >= 15:
                return 60
            else:
                return 0
        if is_fomc_week(now.date()):
            return settings.poll_interval_fomc_week

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

        days = days_to_next_fomc()
        if days is not None and days <= 7:
            next_date = get_next_fomc_date()
            alert_fomc_approaching(days, str(next_date))

    async def _main_loop(self):
        """Main trading loop — fetch data, generate signals, execute trades."""
        if self._running:
            log.debug("main_loop_already_running")
            return
        self._running = True

        try:
            log.info("main_loop_tick")

            # 0. Check exits on existing positions first
            exits = await check_exits(self.kalshi)
            if exits:
                for ex in exits:
                    log.info("position_exit", ticker=ex["ticker"],
                             action=ex["action"], pnl=round(ex.get("pnl", 0), 2))

            # 1. Generate signals
            signals = await generate_signals(self.kalshi)

            # Track price history for all signals (even if we don't trade)
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
                    )

            # 4. Portfolio summary
            await get_portfolio_summary(self.kalshi)

        except Exception as e:
            alert_error("main_loop", str(e))
            log.exception("main_loop_error")
        finally:
            self._running = False
