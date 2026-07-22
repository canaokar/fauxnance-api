from datetime import UTC, datetime
import json
import unittest

from src.api.discovery import DiscoveryService, LazyBackfill
from src.shared.market_data import SymbolMetadata


NOW = datetime(2026, 7, 22, 12, tzinfo=UTC)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class ControlTable:
    def __init__(self):
        self.items = {}

    def get_item(self, *, Key, **_request):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def put_item(self, *, Item, **_request):
        key = (Item["PK"], Item["SK"])
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = dict(Item)

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeValues, **_request):
        item = self.items[(Key["PK"], Key["SK"])]
        if ":enqueueing" in ExpressionAttributeValues:
            if item["state"] != "prepared":
                raise ConditionalFailure()
            item["state"] = "enqueueing"
            item["enqueueLeaseUntil"] = ExpressionAttributeValues[":lease"]
        elif ":running" in ExpressionAttributeValues:
            item["state"] = "running"
            item.pop("enqueueLeaseUntil", None)
        elif ":prepared" in ExpressionAttributeValues:
            item["state"] = "prepared"
            item.pop("enqueueLeaseUntil", None)


class Sqs:
    def __init__(self):
        self.calls = []

    def send_message_batch(self, **request):
        self.calls.append(request)
        return {"Successful": [{"Id": item["Id"]} for item in request["Entries"]]}


class Guard:
    def __init__(self):
        self.calls = []

    def try_acquire(self):
        self.calls.append("acquire")
        return True

    def record_success(self):
        self.calls.append("success")

    def record_failure(self):
        self.calls.append("failure")


class Source:
    name = "yahoo"

    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = []

    def discover(self, symbol):
        self.calls.append(symbol)
        return self.metadata


class Repository:
    def __init__(self):
        self.item = None
        self.registrations = []

    def get_symbol(self, symbol):
        return self.item

    def register_discovered(self, metadata, **kwargs):
        self.registrations.append((metadata, kwargs))
        self.item = {
            "symbol": metadata.symbol,
            "active": True,
            "discovered": True,
            "market": kwargs["market"],
            "coverage": {},
        }
        return self.item


class DiscoveryTests(unittest.TestCase):
    def test_lazy_backfill_enqueues_eleven_versioned_years_once(self):
        control = ControlTable()
        sqs = Sqs()
        lazy = LazyBackfill(control, sqs, "queue")

        job_id = lazy.ensure_enqueued("INFY.NS", "IN", now=NOW)
        repeated = lazy.ensure_enqueued("INFY.NS", "IN", now=NOW)

        self.assertEqual(job_id, repeated)
        self.assertEqual(len(sqs.calls), 2)
        bodies = [
            json.loads(entry["MessageBody"])
            for call in sqs.calls
            for entry in call["Entries"]
        ]
        self.assertEqual(len(bodies), 11)
        self.assertEqual({body["year"] for body in bodies}, set(range(2016, 2027)))
        self.assertTrue(all(body["v"] == 2 and body["market"] == "IN" for body in bodies))
        self.assertEqual(control.items[(f"JOB#{job_id}", "META")]["state"], "running")

    def test_discovery_registers_metadata_then_starts_lazy_job(self):
        metadata = SymbolMetadata(
            symbol="INFY.NS",
            name="Infosys Limited",
            type="equity",
            exchange="NSE",
            currency="INR",
            adapter_hints={"yahooSymbol": "INFY.NS"},
        )
        repository = Repository()
        source = Source(metadata)
        guard = Guard()
        sqs = Sqs()
        service = DiscoveryService(
            repository,
            source,
            guard,
            LazyBackfill(ControlTable(), sqs, "queue"),
        )

        item = service.ensure_registered("INFY.NS", now=NOW)

        self.assertEqual(item["market"], "IN")
        self.assertEqual(source.calls, ["INFY.NS"])
        self.assertEqual(guard.calls, ["acquire", "success"])
        self.assertEqual(len(repository.registrations), 1)
        self.assertEqual(sum(len(call["Entries"]) for call in sqs.calls), 11)

    def test_semantic_not_found_is_not_registered_or_enqueued(self):
        repository = Repository()
        sqs = Sqs()
        service = DiscoveryService(
            repository,
            Source(None),
            Guard(),
            LazyBackfill(ControlTable(), sqs, "queue"),
        )
        self.assertIsNone(service.ensure_registered("NOPE", now=NOW))
        self.assertEqual(repository.registrations, [])
        self.assertEqual(sqs.calls, [])


if __name__ == "__main__":
    unittest.main()
