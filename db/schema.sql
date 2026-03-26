CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    event_type TEXT DEFAULT 'fomc',  -- fomc, cpi, nfp, claims, gdp
    direction TEXT NOT NULL,  -- BUY_YES or BUY_NO
    confidence REAL NOT NULL,
    edge_estimate REAL NOT NULL,
    fedwatch_prob REAL NOT NULL,  -- consensus probability (kept as fedwatch_prob for compat)
    kalshi_price REAL NOT NULL,
    acted_on INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    timestamp TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,       -- yes or no
    action TEXT NOT NULL,     -- buy or sell
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    order_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, filled, cancelled, failed
    fill_price REAL,
    pnl REAL,
    fees REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    kalshi_yes_price REAL,
    kalshi_no_price REAL,
    kalshi_volume REAL,
    fedwatch_prob REAL,
    spread REAL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    fees_paid REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- startup, shutdown, kill_switch, mode_change, error
    details TEXT,
    trading_mode TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(market_ticker);
CREATE INDEX IF NOT EXISTS idx_signals_event_type ON signals(event_type);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(market_ticker);
CREATE INDEX IF NOT EXISTS idx_price_history_ticker ON price_history(market_ticker, timestamp);
