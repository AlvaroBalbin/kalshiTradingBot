"""Position sizing using fractional Kelly criterion with time decay awareness."""

import math

import structlog

from config.settings import settings
from config.fomc_calendar import days_to_next_fomc

log = structlog.get_logger()


def _time_decay_multiplier() -> float:
    """Scale position size based on days until FOMC.

    Far from FOMC (>14 days): spreads wider, more opportunity → 1.0x
    FOMC week (1-7 days): spreads narrowing, moderate → 0.7x
    FOMC day (0 days): volatile, conservative → 0.4x
    """
    days = days_to_next_fomc()
    if days is None:
        return 0.5  # No upcoming FOMC, be cautious

    if days == 0:
        return 0.4
    elif days <= 3:
        return 0.5
    elif days <= 7:
        return 0.7
    elif days <= 14:
        return 0.85
    else:
        return 1.0


def kelly_size(edge: float, price: float, bankroll: float) -> int:
    """Calculate position size using quarter-Kelly criterion with time decay.

    Args:
        edge: Expected edge (probability advantage, e.g., 0.05 = 5 cents)
        price: Buy price as fraction (0 to 1, e.g., 0.65 = 65 cents)
        bankroll: Available capital in dollars

    Returns:
        Number of contracts to buy (integer, minimum 0)
    """
    if edge <= 0 or price <= 0 or price >= 1 or bankroll <= 0:
        return 0

    # Odds: how much you win vs how much you risk
    # Buying YES at 65 cents: win 35 cents, risk 65 cents → odds = 35/65
    win_amount = 1.0 - price
    odds = win_amount / price

    # Kelly fraction: f* = (p*b - q) / b
    # where p = true probability (price + edge), b = odds, q = 1-p
    true_prob = price + edge
    true_prob = min(true_prob, 0.99)  # Cap at 99%
    q = 1 - true_prob

    kelly_f = (true_prob * odds - q) / odds
    if kelly_f <= 0:
        return 0

    # Apply fractional Kelly (quarter-Kelly for safety)
    adjusted_f = kelly_f * settings.kelly_fraction

    # Apply time decay multiplier — scale down as FOMC approaches
    time_mult = _time_decay_multiplier()
    adjusted_f *= time_mult

    # Calculate dollar amount and convert to contracts
    dollar_amount = adjusted_f * bankroll
    contracts = int(dollar_amount / price)

    # Apply caps
    contracts = min(contracts, settings.max_position_per_market)

    # Ensure we don't exceed total exposure
    max_by_exposure = int(settings.max_portfolio_exposure / price)
    contracts = min(contracts, max_by_exposure)

    # Minimum 1 contract if we have any signal
    contracts = max(contracts, 1) if contracts > 0 or (edge > settings.min_edge_after_fees and bankroll > price) else 0

    log.info("position_sized",
             edge=round(edge, 3), price=round(price, 3),
             bankroll=round(bankroll, 2), kelly_f=round(kelly_f, 3),
             adjusted_f=round(adjusted_f, 3), time_mult=time_mult,
             contracts=contracts)

    return contracts
