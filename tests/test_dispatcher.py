from datetime import date
import json
import unittest

from src.ingest.dispatcher import DispatchError, EodDispatcher, handler
from src.ingest.messages import EodBatchMessage, parse_message


class PagedTable:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def query(self, **request):
        self.calls.append(request)
        index = 1 if "ExclusiveStartKey" in request else 0
        response = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"PK": "SYMBOLS", "SK": "NEXT"}
        return response


class FakeSqs:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def send_message_batch(self, **request):
        self.calls.append(request)
        if self.fail:
            return {"Failed": [{"Id": request["Entries"][0]["Id"]}]}
        return {"Successful": [{"Id": item["Id"]} for item in request["Entries"]]}


class DispatcherTests(unittest.TestCase):
    def test_paginates_filters_sorts_and_sends_strict_single_symbol_messages(self):
        active = [
            {"PK": "SYMBOLS", "SK": f"S{i:02}", "active": True, "market": "US"}
            for i in range(11, -1, -1)
        ]
        table = PagedTable(
            [
                active[:6]
                + [
                    {"SK": "INACTIVE", "active": False, "market": "US"},
                    {"SK": "RELIANCE", "active": True, "market": "IN"},
                ],
                active[6:] + [active[-1]],
            ]
        )
        sqs = FakeSqs()
        dispatcher = EodDispatcher(
            table,
            sqs,
            "queue-url",
            today=lambda: date(2026, 7, 21),
        )

        count = dispatcher.dispatch()

        self.assertEqual(count, 12)
        self.assertEqual(len(table.calls), 2)
        self.assertEqual([len(call["Entries"]) for call in sqs.calls], [10, 2])
        bodies = [
            entry["MessageBody"]
            for call in sqs.calls
            for entry in call["Entries"]
        ]
        documents = [json.loads(body) for body in bodies]
        self.assertTrue(all(document["v"] == 2 for document in documents))
        self.assertEqual(
            [document["symbol"] for document in documents],
            [f"S{i:02}" for i in range(12)],
        )
        for body in bodies:
            message = parse_message(body)
            self.assertIsInstance(message, EodBatchMessage)
            self.assertEqual(message.start, date(2026, 7, 15))
            self.assertEqual(message.end, date(2026, 7, 21))

    def test_handler_returns_count_and_sqs_failures_are_explicit(self):
        empty = EodDispatcher(PagedTable([[]]), FakeSqs(), "queue")
        self.assertEqual(
            handler({}, object(), dispatcher=empty),
            {"enqueued": 0},
        )

        failing = EodDispatcher(
            PagedTable([[{"SK": "AAPL", "active": True, "market": "US"}]]),
            FakeSqs(fail=True),
            "queue",
        )
        with self.assertRaises(DispatchError):
            failing.dispatch()

    def test_handler_dispatches_the_requested_market(self):
        table = PagedTable(
            [[
                {"SK": "AAPL", "active": True, "market": "US"},
                {"SK": "INFY.NS", "active": True, "market": "IN"},
            ]]
        )
        sqs = FakeSqs()
        dispatcher = EodDispatcher(table, sqs, "queue")

        response = handler({"market": "IN"}, object(), dispatcher=dispatcher)

        self.assertEqual(response, {"enqueued": 1})
        body = json.loads(sqs.calls[0]["Entries"][0]["MessageBody"])
        self.assertEqual((body["market"], body["symbol"]), ("IN", "INFY.NS"))

    def test_discovered_cap_is_global_and_never_excludes_curated_symbols(self):
        discovered = [
            {
                "SK": f"IND{i:03}.NS",
                "symbol": f"IND{i:03}.NS",
                "active": True,
                "market": "IN",
                "discovered": True,
                "discoveredAt": f"2026-07-22T12:{i // 60:02}:{i % 60:02}Z",
            }
            for i in range(300)
        ]
        discovered.append(
            {
                "SK": "OLDUS",
                "symbol": "OLDUS",
                "active": True,
                "market": "US",
                "discovered": True,
                "discoveredAt": "2020-01-01T00:00:00Z",
            }
        )
        discovered.append(
            {"SK": "AAPL", "symbol": "AAPL", "active": True, "market": "US"}
        )
        sqs = FakeSqs()
        dispatcher = EodDispatcher(PagedTable([discovered]), sqs, "queue")

        count = dispatcher.dispatch("US")

        self.assertEqual(count, 1)
        body = json.loads(sqs.calls[0]["Entries"][0]["MessageBody"])
        self.assertEqual(body["symbol"], "AAPL")


if __name__ == "__main__":
    unittest.main()
