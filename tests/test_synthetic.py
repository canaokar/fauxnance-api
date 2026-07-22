from datetime import UTC, date, datetime
from decimal import Decimal
import unittest

from src.shared.market_data import Candle
from src.synthetic.generator import fill_candle_gaps, synthetic_quote


def candle(day: str, close: str = "100") -> Candle:
    value = Decimal(close)
    return Candle(
        date=date.fromisoformat(day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=100,
        source="yahoo",
    )


class SyntheticGeneratorTests(unittest.TestCase):
    def test_candles_are_repeatable_forward_only_and_keep_real_rows(self):
        real = [candle("2026-07-20"), candle("2026-07-22", "102")]
        first = fill_candle_gaps(
            "AAPL", "equity", date(2026, 7, 17), date(2026, 7, 24), real
        )
        second = fill_candle_gaps(
            "AAPL", "equity", date(2026, 7, 17), date(2026, 7, 24), real
        )

        self.assertEqual(first, second)
        self.assertEqual(first[0].date, date(2026, 7, 20))
        self.assertEqual(next(row for row in first if row.date == date(2026, 7, 22)), real[1])
        generated = [row for row in first if row.source == "synthetic"]
        self.assertTrue(generated)
        self.assertTrue(all(row.low <= row.open <= row.high for row in generated))
        self.assertTrue(all(row.low <= row.close <= row.high for row in generated))
        self.assertTrue(all(row.volume is None for row in generated))
        self.assertNotIn(date(2026, 7, 18), [row.date for row in first])

    def test_crypto_uses_calendar_days_and_a_prior_history_anchor(self):
        result = fill_candle_gaps(
            "X:BTC-USD",
            "crypto",
            date(2026, 7, 18),
            date(2026, 7, 19),
            [],
            history=[candle("2026-07-17", "100000")],
        )
        self.assertEqual([row.date for row in result], [date(2026, 7, 18), date(2026, 7, 19)])
        self.assertTrue(all(row.source == "synthetic" for row in result))

    def test_quote_is_stable_within_a_five_minute_bucket(self):
        anchor = candle("2026-07-20")
        first = synthetic_quote(
            "AAPL", "equity", "USD", anchor,
            now=datetime(2026, 7, 21, 15, 42, 10, tzinfo=UTC),
        )
        same = synthetic_quote(
            "AAPL", "equity", "USD", anchor,
            now=datetime(2026, 7, 21, 15, 44, 59, tzinfo=UTC),
        )
        later = synthetic_quote(
            "AAPL", "equity", "USD", anchor,
            now=datetime(2026, 7, 21, 15, 45, tzinfo=UTC),
        )
        self.assertEqual(first, same)
        self.assertNotEqual(first.price, later.price)
        self.assertEqual(first.as_of, datetime(2026, 7, 21, 15, 40, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
