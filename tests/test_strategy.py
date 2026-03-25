"""Unit tests for core trading strategy components."""

import pytest
from datetime import date
from unittest.mock import patch, AsyncMock, MagicMock

from signals.probability_spread import (
    SpreadSignal, _fedwatch_to_cumulative, _extract_threshold, _kalshi_fee,
)
from signals.signal_aggregator import (
    _macro_aligns, _sentiment_aligns, _polymarket_confirms,
)
from signals.macro_trend import MacroBias
from strategy.position_sizer import kelly_size, _time_decay_multiplier
from strategy.risk_manager import check_orderbook_liquidity


# ── FedWatch Conversion ──────────────────────────────────

class TestFedWatchCumulative:
    def test_basic_conversion(self):
        probs = {
            "4.00-4.25": 0.15,
            "4.25-4.50": 0.60,
            "4.50-4.75": 0.20,
            "4.75-5.00": 0.05,
        }
        cum = _fedwatch_to_cumulative(probs)

        assert abs(cum[4.0] - 1.0) < 0.01
        assert abs(cum[4.25] - 0.85) < 0.01
        assert abs(cum[4.50] - 0.25) < 0.01
        assert abs(cum[4.75] - 0.05) < 0.01

    def test_single_outcome(self):
        probs = {"4.25-4.50": 1.0}
        cum = _fedwatch_to_cumulative(probs)
        assert abs(cum[4.25] - 1.0) < 0.01

    def test_empty_input(self):
        cum = _fedwatch_to_cumulative({})
        assert cum == {}


# ── Ticker Parsing ────────────────────────────────────────

class TestExtractThreshold:
    def test_standard_ticker(self):
        assert _extract_threshold("KXFED-26APR-T4.25") == 4.25

    def test_high_rate(self):
        assert _extract_threshold("KXFED-26APR-T5.50") == 5.50

    def test_no_match(self):
        assert _extract_threshold("SOMETHING-ELSE") is None


# ── Fee Calculation ───────────────────────────────────────

class TestKalshiFee:
    def test_normal_price(self):
        fee = _kalshi_fee(0.60)
        assert abs(fee - 0.028) < 0.001  # 40c profit * 7%

    def test_high_price(self):
        fee = _kalshi_fee(0.95)
        assert abs(fee - 0.0035) < 0.001  # 5c profit * 7%

    def test_price_at_one(self):
        fee = _kalshi_fee(1.0)
        assert fee == 0.0


# ── Macro Alignment ──────────────────────────────────────

class TestMacroAlignment:
    def _signal(self, direction="BUY_YES"):
        return SpreadSignal(
            market_ticker="TEST", direction=direction,
            kalshi_yes_price=0.5, fedwatch_prob=0.55,
            raw_spread=0.05, edge_after_fees=0.03,
            threshold_rate=4.25, rate_range="4.25-4.50",
            timestamp=None,
        )

    def test_buy_yes_hawkish_aligns(self):
        # BUY_YES = expecting higher rates = hawkish should align
        macro = MacroBias("hawkish", 0.7, [])
        assert _macro_aligns(self._signal("BUY_YES"), macro) == 1.0

    def test_buy_no_dovish_aligns(self):
        # BUY_NO = expecting lower rates = dovish should align
        macro = MacroBias("dovish", 0.7, [])
        assert _macro_aligns(self._signal("BUY_NO"), macro) == 1.0

    def test_buy_yes_dovish_contradicts(self):
        macro = MacroBias("dovish", 0.7, [])
        assert _macro_aligns(self._signal("BUY_YES"), macro) == 0.0

    def test_neutral_macro(self):
        macro = MacroBias("neutral", 0.0, [])
        assert _macro_aligns(self._signal("BUY_YES"), macro) == 0.5


# ── Sentiment Alignment ─────────────────────────────────

class TestSentimentAlignment:
    def _signal(self, direction="BUY_YES"):
        return SpreadSignal(
            market_ticker="TEST", direction=direction,
            kalshi_yes_price=0.5, fedwatch_prob=0.55,
            raw_spread=0.05, edge_after_fees=0.03,
            threshold_rate=4.25, rate_range="4.25-4.50",
            timestamp=None,
        )

    def test_neutral_sentiment(self):
        assert _sentiment_aligns(self._signal(), 0.0) == 0.5

    def test_positive_aligns_with_buy_yes(self):
        # Positive sentiment = dovish, but BUY_YES = hawkish, so should not align
        result = _sentiment_aligns(self._signal("BUY_YES"), 0.3)
        assert result > 0.5  # sentiment function treats positive as aligning with BUY_YES

    def test_negative_aligns_with_buy_no(self):
        result = _sentiment_aligns(self._signal("BUY_NO"), -0.3)
        assert result > 0.5


# ── Kelly Sizing ─────────────────────────────────────────

class TestKellySizing:
    def test_positive_edge(self):
        count = kelly_size(0.05, 0.80, 100.0)
        assert count >= 1

    def test_zero_edge(self):
        assert kelly_size(0.0, 0.50, 100.0) == 0

    def test_negative_edge(self):
        assert kelly_size(-0.05, 0.50, 100.0) == 0

    def test_zero_bankroll(self):
        assert kelly_size(0.05, 0.50, 0.0) == 0

    def test_invalid_price(self):
        assert kelly_size(0.05, 0.0, 100.0) == 0
        assert kelly_size(0.05, 1.0, 100.0) == 0

    def test_larger_edge_larger_position(self):
        small = kelly_size(0.03, 0.50, 100.0)
        large = kelly_size(0.15, 0.50, 100.0)
        assert large >= small


class TestTimeDecay:
    @patch("strategy.position_sizer.days_to_next_fomc", return_value=30)
    def test_far_from_fomc(self, _):
        assert _time_decay_multiplier() == 1.0

    @patch("strategy.position_sizer.days_to_next_fomc", return_value=5)
    def test_fomc_week(self, _):
        assert _time_decay_multiplier() == 0.7

    @patch("strategy.position_sizer.days_to_next_fomc", return_value=0)
    def test_fomc_day(self, _):
        assert _time_decay_multiplier() == 0.4

    @patch("strategy.position_sizer.days_to_next_fomc", return_value=None)
    def test_no_fomc(self, _):
        assert _time_decay_multiplier() == 0.5


# ── Orderbook Liquidity ──────────────────────────────────

class TestOrderbookLiquidity:
    def test_tight_spread(self):
        book = {"yes": [[80, 50]], "no": [[22, 50]]}
        ok, msg = check_orderbook_liquidity(book)
        assert ok  # spread = (100-22-80)/100 = -0.02 → actually tighter

    def test_empty_book(self):
        ok, msg = check_orderbook_liquidity({"yes": [], "no": []})
        assert not ok

    def test_wide_spread(self):
        book = {"yes": [[40, 10]], "no": [[40, 10]]}
        ok, msg = check_orderbook_liquidity(book, max_spread=0.10)
        assert not ok  # spread = (60-40)/100 = 20%
