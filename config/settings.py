from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Kalshi API
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./kalshi_private_key.pem"
    kalshi_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = True

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

    # Paper trading
    paper_trading: bool = True

    # Strategy params
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

    @property
    def kalshi_production_url(self) -> str:
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def kalshi_ws_url(self) -> str:
        base = self.kalshi_base_url.replace("https://", "wss://").replace("/trade-api/v2", "")
        return f"{base}/trade-api/ws/v2"

    @property
    def private_key_bytes(self) -> bytes:
        return Path(self.kalshi_private_key_path).read_bytes()

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
