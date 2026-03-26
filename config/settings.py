from pydantic_settings import BaseSettings
from pathlib import Path


PRODUCTION_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Tier-specific limits: (max_position_per_market, max_daily_loss, max_portfolio_exposure)
TIER_LIMITS = {
    "paper":    (20,  25.0, 75.0),
    "cautious": (1,    5.0, 15.0),
    "normal":   (5,   15.0, 50.0),
}


class Settings(BaseSettings):
    # Kalshi API
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./kalshi_private_key.pem"
    kalshi_base_url: str = DEMO_URL
    use_demo: bool = True  # kept for backward compat, derived from trading_mode

    # FRED API
    fred_api_key: str = ""

    # Twitter/X (optional)
    twitter_bearer_token: str = ""
    twitter_consumer_key: str = ""
    twitter_secret_key: str = ""
    twitter_oauth2_client_id: str = ""
    twitter_oauth2_client_secret: str = ""

    # Finnhub
    finnhub_api_key: str = ""

    # Alpha Vantage
    alphavantage_api_key: str = ""

    # Trading mode: "paper", "cautious", "normal"
    trading_mode: str = "paper"

    # Legacy flag — kept so existing code doesn't break; derived from trading_mode
    paper_trading: bool = True

    # Telegram notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Kill switch file path
    kill_switch_path: str = "./KILL_SWITCH"

    # Strategy params (base values — effective values depend on trading_mode)
    probability_threshold: float = 0.05
    min_edge_after_fees: float = 0.02
    max_position_per_market: int = 20
    max_daily_loss: float = 25.0
    max_portfolio_exposure: float = 75.0
    kelly_fraction: float = 0.25

    # Position management
    profit_target_cents: int = 15
    stop_loss_cents: int = 10

    # Kalshi fee (percentage of profit)
    kalshi_fee_rate: float = 0.07

    # Scheduling intervals (seconds)
    poll_interval_normal: int = 3600
    poll_interval_fomc_week: int = 300
    poll_interval_fomc_day: int = 30

    # --- Derived properties ---

    @property
    def is_live(self) -> bool:
        return self.trading_mode != "paper"

    @property
    def effective_max_position_per_market(self) -> int:
        return TIER_LIMITS.get(self.trading_mode, TIER_LIMITS["paper"])[0]

    @property
    def effective_max_daily_loss(self) -> float:
        return TIER_LIMITS.get(self.trading_mode, TIER_LIMITS["paper"])[1]

    @property
    def effective_max_portfolio_exposure(self) -> float:
        return TIER_LIMITS.get(self.trading_mode, TIER_LIMITS["paper"])[2]

    @property
    def effective_base_url(self) -> str:
        if self.trading_mode == "paper":
            return self.kalshi_base_url  # use whatever is configured (demo or prod)
        return PRODUCTION_URL  # live modes always use production

    @property
    def kalshi_production_url(self) -> str:
        return PRODUCTION_URL

    @property
    def kalshi_ws_url(self) -> str:
        base = self.effective_base_url.replace("https://", "wss://").replace("/trade-api/v2", "")
        return f"{base}/trade-api/ws/v2"

    @property
    def private_key_bytes(self) -> bytes:
        return Path(self.kalshi_private_key_path).read_bytes()

    def model_post_init(self, __context):
        # Derive paper_trading and use_demo from trading_mode
        object.__setattr__(self, "paper_trading", self.trading_mode == "paper")
        object.__setattr__(self, "use_demo", self.trading_mode == "paper" and "demo" in self.kalshi_base_url)
        # Override base_url for live modes
        if self.trading_mode != "paper":
            object.__setattr__(self, "kalshi_base_url", PRODUCTION_URL)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
