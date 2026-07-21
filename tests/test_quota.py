from datetime import UTC, datetime
from decimal import Decimal
import unittest

from src.shared.quota import QuotaExceeded, QuotaService


NOW = datetime(2026, 7, 21, 12, 34, 56, 500_000, tzinfo=UTC)


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self, *, result=None, error=None):
        self.result = result or {"Attributes": {"count": Decimal("1")}}
        self.error = error
        self.calls = []

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class QuotaServiceTests(unittest.TestCase):
    def test_consumes_quota_with_one_conditional_atomic_update(self):
        table = FakeTable(result={"Attributes": {"count": Decimal("27")}})
        usage = QuotaService(table).consume("student-1", 2_000, at=NOW)

        self.assertEqual(usage.used_today, 27)
        self.assertEqual(usage.daily_quota, 2_000)
        self.assertEqual(usage.resets_at, datetime(2026, 7, 22, tzinfo=UTC))
        self.assertEqual(len(table.calls), 1)
        call = table.calls[0]
        self.assertEqual(
            call["Key"],
            {"PK": "KEYID#student-1", "SK": "USAGE#2026-07-21"},
        )
        self.assertEqual(
            call["ConditionExpression"],
            "attribute_not_exists(#count) OR #count < :quota",
        )
        self.assertIn("if_not_exists(#count, :zero) + :one", call["UpdateExpression"])
        self.assertEqual(call["ExpressionAttributeValues"][":quota"], 2_000)
        self.assertEqual(
            call["ExpressionAttributeValues"][":expires_at"],
            int(datetime(2026, 8, 25, tzinfo=UTC).timestamp()),
        )

    def test_conditional_failure_maps_to_quota_error_and_retry_after(self):
        table = FakeTable(error=AwsError("ConditionalCheckFailedException"))

        with self.assertRaises(QuotaExceeded) as raised:
            QuotaService(table).consume("student-1", 2_000, at=NOW)

        error = raised.exception
        self.assertEqual(error.daily_quota, 2_000)
        self.assertEqual(error.resets_at, datetime(2026, 7, 22, tzinfo=UTC))
        self.assertEqual(error.retry_after, 41_104)
        self.assertEqual(
            str(error),
            "Daily quota of 2000 requests exhausted. Resets at 00:00 UTC.",
        )

    def test_non_conditional_aws_error_is_not_hidden(self):
        table = FakeTable(error=AwsError("ProvisionedThroughputExceededException"))
        with self.assertRaises(AwsError):
            QuotaService(table).consume("student-1", 2_000, at=NOW)

    def test_rejects_invalid_inputs_without_a_write(self):
        table = FakeTable()
        for key_id, quota in (("", 2_000), ("student-1", 0)):
            with self.subTest(key_id=key_id, quota=quota):
                with self.assertRaises(ValueError):
                    QuotaService(table).consume(key_id, quota, at=NOW)
        self.assertEqual(table.calls, [])


if __name__ == "__main__":
    unittest.main()
