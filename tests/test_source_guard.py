from datetime import UTC, datetime, timedelta
import unittest

from src.shared.source_guard import (
    ALPHA_VANTAGE_DAILY_LIMIT,
    DynamoDbSourceGuard,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.failure_count = 0
        self.budget_exhausted = False

    def get_item(self, *, Key, **_kwargs):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def update_item(self, **request):
        self.calls.append(request)
        key = request["Key"]
        if key["SK"].startswith("BUDGET#"):
            if self.budget_exhausted:
                raise ConditionalFailure()
            return {}

        expression = request["UpdateExpression"]
        if "ADD failureCount" in expression:
            self.failure_count += 1
            return {"Attributes": {"failureCount": self.failure_count}}
        return {}


class SourceGuardTests(unittest.TestCase):
    def test_alpha_budget_reserves_against_the_twenty_five_call_limit(self):
        table = FakeTable()
        guard = DynamoDbSourceGuard(
            table,
            "alpha_vantage",
            daily_limit=ALPHA_VANTAGE_DAILY_LIMIT,
            clock=lambda: NOW,
        )

        self.assertTrue(guard.try_acquire())

        budget = table.calls[-1]
        self.assertEqual(
            budget["Key"],
            {"PK": "SRC#alpha_vantage", "SK": "BUDGET#2026-07-21"},
        )
        self.assertEqual(
            budget["ExpressionAttributeValues"][":limit"],
            25,
        )
        self.assertGreater(
            budget["ExpressionAttributeValues"][":expires"],
            int(NOW.timestamp()),
        )

        table.budget_exhausted = True
        self.assertFalse(guard.try_acquire())

    def test_three_failures_open_the_circuit_for_fifteen_minutes(self):
        table = FakeTable()
        guard = DynamoDbSourceGuard(table, "yahoo", clock=lambda: NOW)

        guard.record_failure()
        guard.record_failure()
        self.assertEqual(len(table.calls), 2)

        guard.record_failure()

        self.assertEqual(len(table.calls), 4)
        opened = table.calls[-1]
        values = opened["ExpressionAttributeValues"]
        self.assertEqual(values[":open"], "open")
        self.assertEqual(values[":until"], "2026-07-21T12:15:00Z")

    def test_open_circuit_skips_calls_until_the_cooldown_expires(self):
        table = FakeTable()
        table.items[("SRC#yahoo", "STATE")] = {
            "state": "open",
            "openedUntil": "2026-07-21T12:15:00Z",
        }
        guard = DynamoDbSourceGuard(table, "yahoo", clock=lambda: NOW)

        self.assertFalse(guard.try_acquire())

        table.items[("SRC#yahoo", "STATE")]["openedUntil"] = (
            NOW - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        self.assertTrue(guard.try_acquire())

    def test_success_closes_and_resets_the_circuit(self):
        table = FakeTable()
        guard = DynamoDbSourceGuard(table, "yahoo", clock=lambda: NOW)

        guard.record_success()

        request = table.calls[-1]
        self.assertIn("REMOVE openedUntil", request["UpdateExpression"])
        self.assertEqual(request["ExpressionAttributeValues"][":closed"], "closed")
        self.assertEqual(request["ExpressionAttributeValues"][":zero"], 0)


if __name__ == "__main__":
    unittest.main()
