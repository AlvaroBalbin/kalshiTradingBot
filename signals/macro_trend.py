"""Macro trend signal from FRED data — provides directional bias."""

from dataclasses import dataclass

import structlog

from data.fred_client import fred_client

log = structlog.get_logger()


@dataclass
class MacroBias:
    direction: str  # "dovish" (cuts likely), "hawkish" (hikes likely), "neutral"
    confidence: float  # 0.0 to 1.0
    reasons: list[str]


def compute_macro_bias() -> MacroBias:
    """Analyze FRED macro data to determine directional bias for rate decisions.

    Dovish signals (rate cuts more likely):
    - Inverted yield curve (T10Y2Y < 0)
    - Rising unemployment
    - Falling inflation

    Hawkish signals (rate hikes more likely):
    - Steep yield curve
    - Low/falling unemployment
    - Rising/high inflation (CPI YoY > 3%)
    """
    snapshot = fred_client.get_macro_snapshot()
    reasons = []
    score = 0.0  # Negative = dovish, positive = hawkish

    # Yield curve
    yc = snapshot.get("yield_curve_spread")
    if yc is not None:
        if yc < -0.5:
            score -= 0.4
            reasons.append(f"Deeply inverted yield curve ({yc:.2f}%)")
        elif yc < 0:
            score -= 0.2
            reasons.append(f"Inverted yield curve ({yc:.2f}%)")
        elif yc > 1.0:
            score += 0.2
            reasons.append(f"Steep yield curve ({yc:.2f}%)")

    # Unemployment
    unemp = snapshot.get("unemployment")
    if unemp is not None:
        if unemp > 5.0:
            score -= 0.3
            reasons.append(f"High unemployment ({unemp:.1f}%)")
        elif unemp > 4.0:
            score -= 0.1
            reasons.append(f"Rising unemployment ({unemp:.1f}%)")
        elif unemp < 3.5:
            score += 0.2
            reasons.append(f"Very low unemployment ({unemp:.1f}%)")

    # Inflation (CPI YoY)
    cpi = snapshot.get("cpi_yoy")
    if cpi is not None:
        if cpi > 4.0:
            score += 0.4
            reasons.append(f"High inflation ({cpi:.1f}% YoY)")
        elif cpi > 3.0:
            score += 0.2
            reasons.append(f"Elevated inflation ({cpi:.1f}% YoY)")
        elif cpi < 2.0:
            score -= 0.2
            reasons.append(f"Below-target inflation ({cpi:.1f}% YoY)")

    if not reasons:
        return MacroBias(direction="neutral", confidence=0.0, reasons=["No macro data available"])

    if score < -0.2:
        direction = "dovish"
    elif score > 0.2:
        direction = "hawkish"
    else:
        direction = "neutral"

    confidence = min(abs(score), 1.0)

    log.info("macro_bias", direction=direction, confidence=round(confidence, 2),
             score=round(score, 2), reasons=reasons)

    return MacroBias(direction=direction, confidence=confidence, reasons=reasons)
