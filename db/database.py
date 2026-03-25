import aiosqlite
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "bot.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db():
    """Synchronous init for startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.close()


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def insert_signal(market_ticker: str, direction: str, confidence: float,
                        edge_estimate: float, fedwatch_prob: float, kalshi_price: float) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """INSERT INTO signals (timestamp, market_ticker, direction, confidence,
               edge_estimate, fedwatch_prob, kalshi_price)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)""",
            (market_ticker, direction, confidence, edge_estimate, fedwatch_prob, kalshi_price),
        )
        await db.commit()
        return cursor.lastrowid


async def insert_trade(signal_id: int, market_ticker: str, side: str, action: str,
                       quantity: int, price: float, order_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO trades (signal_id, timestamp, market_ticker, side, action,
               quantity, price, order_id, status)
               VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, 'pending')""",
            (signal_id, market_ticker, side, action, quantity, price, order_id),
        )
        await db.commit()
        return cursor.lastrowid


async def update_trade_status(trade_id: int, status: str, fill_price: float | None = None,
                              pnl: float | None = None, fees: float | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE trades SET status=?, fill_price=?, pnl=?, fees=? WHERE id=?""",
            (status, fill_price, pnl, fees, trade_id),
        )
        await db.commit()


async def insert_price_snapshot(market_ticker: str, yes_price: float, no_price: float,
                                volume: float, fedwatch_prob: float, spread: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO price_history (timestamp, market_ticker, kalshi_yes_price,
               kalshi_no_price, kalshi_volume, fedwatch_prob, spread)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)""",
            (market_ticker, yes_price, no_price, volume, fedwatch_prob, spread),
        )
        await db.commit()


async def get_daily_pnl(date_str: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM daily_pnl WHERE date=?", (date_str,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_open_positions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades WHERE status='filled' AND pnl IS NULL"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_todays_trades() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades WHERE date(timestamp)=date('now')"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
