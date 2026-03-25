"""Paper trading stats — shows performance summary from the database.

Run: python3 scripts/stats.py
"""

import sys
import os
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.db")


def get_stats():
    if not os.path.exists(DB_PATH):
        print("No database found. Bot hasn't run yet.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("==========================================================")
    print("    KALSHI TRADING BOT -- PERFORMANCE STATS")
    print("==========================================================")

    # Signal stats
    cur = conn.execute("SELECT COUNT(*) as total FROM signals")
    total_signals = cur.fetchone()["total"]

    cur = conn.execute("SELECT COUNT(*) as total FROM signals WHERE acted_on=1")
    acted_signals = cur.fetchone()["total"]

    print(f"\n  SIGNALS")
    print(f"  Total signals generated:  {total_signals}")
    print(f"  Signals acted on:         {acted_signals}")

    # Trade stats
    cur = conn.execute("SELECT COUNT(*) as total FROM trades")
    total_trades = cur.fetchone()["total"]

    cur = conn.execute("SELECT COUNT(*) as total FROM trades WHERE status='paper_filled'")
    paper_trades = cur.fetchone()["total"]

    cur = conn.execute("SELECT COUNT(*) as total FROM trades WHERE status='filled'")
    real_trades = cur.fetchone()["total"]

    cur = conn.execute("SELECT COUNT(*) as total FROM trades WHERE status='closed'")
    closed_trades = cur.fetchone()["total"]

    cur = conn.execute("SELECT COUNT(*) as total FROM trades WHERE status='settled'")
    settled_trades = cur.fetchone()["total"]

    print(f"\n  TRADES")
    print(f"  Total trades:             {total_trades}")
    print(f"  Paper fills:              {paper_trades}")
    print(f"  Real fills:               {real_trades}")
    print(f"  Closed (exited):          {closed_trades}")
    print(f"  Settled (market ended):   {settled_trades}")

    # P&L stats
    cur = conn.execute("SELECT SUM(pnl) as total_pnl, SUM(fees) as total_fees FROM trades WHERE pnl IS NOT NULL")
    row = cur.fetchone()
    total_pnl = row["total_pnl"] or 0
    total_fees = row["total_fees"] or 0
    net_pnl = total_pnl - total_fees

    print(f"\n  P&L")
    print(f"  Gross P&L:                ${total_pnl:.2f}")
    print(f"  Fees paid:                ${total_fees:.2f}")
    print(f"  Net P&L:                  ${net_pnl:.2f}")

    # Win/loss ratio
    cur = conn.execute("SELECT COUNT(*) as wins FROM trades WHERE pnl > 0")
    wins = cur.fetchone()["wins"]

    cur = conn.execute("SELECT COUNT(*) as losses FROM trades WHERE pnl IS NOT NULL AND pnl <= 0")
    losses = cur.fetchone()["losses"]

    if wins + losses > 0:
        win_rate = wins / (wins + losses) * 100
        print(f"  Wins:                     {wins}")
        print(f"  Losses:                   {losses}")
        print(f"  Win rate:                 {win_rate:.1f}%")
    else:
        print(f"  No completed trades yet")

    # Average edge on signals
    cur = conn.execute("SELECT AVG(edge_estimate) as avg_edge, AVG(confidence) as avg_conf FROM signals")
    row = cur.fetchone()
    avg_edge = row["avg_edge"] or 0
    avg_conf = row["avg_conf"] or 0

    if total_signals > 0:
        print(f"\n  SIGNAL QUALITY")
        print(f"  Avg edge estimate:        {avg_edge:.1%}")
        print(f"  Avg confidence:           {avg_conf:.1%}")

    # Recent trades
    cur = conn.execute("""
        SELECT timestamp, market_ticker, side, quantity, price, status, pnl
        FROM trades ORDER BY timestamp DESC LIMIT 10
    """)
    recent = cur.fetchall()

    if recent:
        print(f"\n  RECENT TRADES (last 10)")
        print(f"  {'Time':<20} {'Ticker':<25} {'Side':<5} {'Qty':<5} {'Price':<7} {'Status':<13} {'P&L':<8}")
        print(f"  {'-'*83}")
        for t in recent:
            pnl_str = f"${t['pnl']:.2f}" if t['pnl'] is not None else "-"
            ts = t['timestamp'][:16] if t['timestamp'] else "?"
            print(f"  {ts:<20} {t['market_ticker']:<25} {t['side']:<5} {t['quantity']:<5} ${t['price']:<6.2f} {t['status']:<13} {pnl_str:<8}")

    # Price history
    cur = conn.execute("SELECT COUNT(*) as total FROM price_history")
    price_snapshots = cur.fetchone()["total"]

    print(f"\n  DATA COLLECTION")
    print(f"  Price snapshots:          {price_snapshots}")

    # Daily breakdown
    cur = conn.execute("""
        SELECT date(timestamp) as day, COUNT(*) as trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(pnl) as day_pnl
        FROM trades
        WHERE pnl IS NOT NULL
        GROUP BY date(timestamp)
        ORDER BY day DESC
        LIMIT 7
    """)
    daily = cur.fetchall()

    if daily:
        print(f"\n  DAILY BREAKDOWN (last 7 days)")
        print(f"  {'Date':<12} {'Trades':<8} {'Wins':<6} {'P&L':<10}")
        print(f"  {'-'*36}")
        for d in daily:
            pnl_str = f"${d['day_pnl']:.2f}" if d['day_pnl'] else "$0.00"
            print(f"  {d['day']:<12} {d['trades']:<8} {d['wins'] or 0:<6} {pnl_str:<10}")

    print(f"\n==========================================================")

    conn.close()


if __name__ == "__main__":
    get_stats()
