from __future__ import annotations

import unittest

from boto3.dynamodb.types import TypeDeserializer

from src.admin.repository import RepositoryConflict
from src.console.repository import DynamoConsoleRepository


class ConsoleRepositoryUserTests(unittest.TestCase):
    def test_create_user_is_one_transaction_of_three_conditional_puts(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        repository.create_user(
            {
                "PK": "USER#user-1",
                "SK": "META",
                "userId": "user-1",
                "email": "amara@example.com",
                "name": "Amara",
                "passwordHash": "hash",
                "role": "instructor",
                "status": "active",
                "createdAt": "2026-08-21T00:00:00Z",
                "updatedAt": "2026-08-21T00:00:00Z",
            }
        )

        self.assertEqual(len(client.transactions), 1)
        puts = client.transactions[0]["TransactItems"]
        self.assertEqual(len(puts), 3)
        items = [_deserialize(p["Put"]["Item"]) for p in puts]
        self.assertEqual(items[0]["PK"], "USER#user-1")
        self.assertEqual(items[0]["SK"], "META")
        self.assertEqual(items[1]["PK"], "USEREMAIL#amara@example.com")
        self.assertEqual(items[1]["SK"], "META")
        self.assertEqual(items[1]["userId"], "user-1")
        self.assertTrue(all("ConditionExpression" in p["Put"] for p in puts))

        # Third item is the USERS-index projection used by all_users(); it
        # must carry no passwordHash.
        projection = items[2]
        self.assertEqual(projection["PK"], "USERS")
        self.assertEqual(projection["SK"], "user-1")
        self.assertEqual(
            {projection["userId"], projection["email"], projection["role"], projection["status"]},
            {"user-1", "amara@example.com", "instructor", "active"},
        )
        self.assertNotIn("passwordHash", projection)

    def test_create_user_raises_conflict_on_transaction_cancellation(self):
        table, dynamodb, client = _aws_fakes()
        client.raise_on_transact = _cancelled_exception()
        repository = DynamoConsoleRepository(table, dynamodb)

        with self.assertRaises(RepositoryConflict):
            repository.create_user(
                {
                    "PK": "USER#user-1",
                    "SK": "META",
                    "userId": "user-1",
                    "email": "amara@example.com",
                    "createdAt": "2026-08-21T00:00:00Z",
                }
            )

    def test_get_user_by_email_resolves_lookup_then_user(self):
        table, dynamodb, _client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        with self.subTest("missing lookup"):
            self.assertIsNone(repository.get_user_by_email("nobody@example.com"))

        table.items[("USEREMAIL#amara@example.com", "META")] = {"userId": "user-1"}
        with self.subTest("lookup present but user missing"):
            self.assertIsNone(repository.get_user_by_email("amara@example.com"))

        table.items[("USER#user-1", "META")] = {"userId": "user-1", "name": "Amara"}
        with self.subTest("lookup and user present"):
            item = repository.get_user_by_email("amara@example.com")
            self.assertEqual(item["name"], "Amara")

    def test_all_users_queries_users_partition_and_handles_empty_result(self):
        table, dynamodb, _client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        result = repository.all_users()

        self.assertEqual(result, [])
        self.assertEqual(len(table.query_calls), 1)
        request = table.query_calls[0]
        self.assertEqual(request["KeyConditionExpression"], "PK = :pk")
        self.assertEqual(request["ExpressionAttributeValues"], {":pk": "USERS"})

    def test_update_user_status_updates_meta_and_projection_in_one_transaction(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        repository.update_user_status(
            "user-1", status="disabled", updated_at="2026-08-21T00:00:00Z"
        )

        self.assertEqual(len(client.transactions), 1)
        updates = client.transactions[0]["TransactItems"]
        self.assertEqual(len(updates), 2)
        keys = [_deserialize(u["Update"]["Key"]) for u in updates]
        self.assertEqual(keys[0], {"PK": "USER#user-1", "SK": "META"})
        self.assertEqual(keys[1], {"PK": "USERS", "SK": "user-1"})
        for update in updates:
            values = _deserialize(update["Update"]["ExpressionAttributeValues"])
            self.assertEqual(values[":status"], "disabled")


class ConsoleRepositorySessionTests(unittest.TestCase):
    def test_session_round_trips(self):
        table, dynamodb, _client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        repository.create_session(
            {
                "PK": "SESSION#abc",
                "SK": "META",
                "userId": "user-1",
                "createdAt": "2026-08-21T00:00:00Z",
                "expiresAt": 1700043200,
            }
        )
        stored = table.put_calls[0]["Item"]
        table.items[(stored["PK"], stored["SK"])] = stored

        fetched = repository.get_session("abc")
        self.assertEqual(fetched["userId"], "user-1")
        self.assertTrue(table.get_calls[-1]["ConsistentRead"])

        repository.delete_session("abc")
        self.assertEqual(table.delete_calls[-1]["Key"], {"PK": "SESSION#abc", "SK": "META"})
        del table.items[(stored["PK"], stored["SK"])]
        self.assertIsNone(repository.get_session("abc"))

    def test_create_session_stores_expires_at_as_int_not_str(self):
        table, dynamodb, _client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        repository.create_session(
            {
                "PK": "SESSION#abc",
                "SK": "META",
                "userId": "user-1",
                "createdAt": "2026-08-21T00:00:00Z",
                "expiresAt": 1700043200,
            }
        )

        stored = table.put_calls[0]["Item"]["expiresAt"]
        self.assertIsInstance(stored, int)
        self.assertNotIsInstance(stored, str)


class ConsoleRepositoryLoginThrottleTests(unittest.TestCase):
    def test_record_login_failure_resets_a_fresh_or_expired_record(self):
        table, dynamodb, _client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)
        email = "operator@example.com"
        # Simulate an expired window sitting in the table (no record is the
        # same case, since attribute_not_exists(PK) is trivially satisfied).
        table.items[(f"LOGINFAIL#{email}", "META")] = {
            "PK": f"LOGINFAIL#{email}",
            "SK": "META",
            "count": 10,
            "expiresAt": 1,
        }

        new_expiry = 9_999_999_999
        count = repository.record_login_failure(email, expires_at=new_expiry)

        self.assertEqual(count, 1)
        stored = table.items[(f"LOGINFAIL#{email}", "META")]
        self.assertEqual(stored["count"], 1)
        self.assertEqual(stored["expiresAt"], new_expiry)

    def test_record_login_failure_inside_active_window_increments_without_touching_expiry(
        self,
    ):
        table, dynamodb, _client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)
        email = "operator@example.com"
        original_expiry = 9_999_999_999
        table.items[(f"LOGINFAIL#{email}", "META")] = {
            "PK": f"LOGINFAIL#{email}",
            "SK": "META",
            "count": 5,
            "expiresAt": original_expiry,
        }

        count = repository.record_login_failure(email, expires_at=original_expiry + 900)

        self.assertEqual(count, 6)
        stored = table.items[(f"LOGINFAIL#{email}", "META")]
        self.assertEqual(stored["expiresAt"], original_expiry)


class ConsoleRepositoryAssignmentTests(unittest.TestCase):
    def test_assign_instructor_is_one_transaction_of_two_conditional_puts(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        repository.assign_instructor(
            {"PK": "COHORT#cohort-1", "SK": "INSTRUCTOR#user-1", "userId": "user-1"},
            {"PK": "USER#user-1", "SK": "COHORT#cohort-1", "cohortId": "cohort-1"},
        )

        self.assertEqual(len(client.transactions), 1)
        puts = client.transactions[0]["TransactItems"]
        self.assertEqual(len(puts), 2)
        items = [_deserialize(p["Put"]["Item"]) for p in puts]
        self.assertEqual(items[0]["PK"], "COHORT#cohort-1")
        self.assertEqual(items[0]["SK"], "INSTRUCTOR#user-1")
        self.assertEqual(items[1]["PK"], "USER#user-1")
        self.assertEqual(items[1]["SK"], "COHORT#cohort-1")
        self.assertTrue(all("ConditionExpression" in p["Put"] for p in puts))

    def test_unassign_instructor_deletes_both_items_in_one_transaction(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoConsoleRepository(table, dynamodb)

        repository.unassign_instructor("cohort-1", "user-1")

        self.assertEqual(len(client.transactions), 1)
        deletes = client.transactions[0]["TransactItems"]
        self.assertEqual(len(deletes), 2)
        keys = [_deserialize(d["Delete"]["Key"]) for d in deletes]
        self.assertEqual(keys[0], {"PK": "COHORT#cohort-1", "SK": "INSTRUCTOR#user-1"})
        self.assertEqual(keys[1], {"PK": "USER#user-1", "SK": "COHORT#cohort-1"})


def _aws_fakes():
    class Client:
        def __init__(self):
            self.transactions = []
            self.raise_on_transact = None

        def transact_write_items(self, **request):
            if self.raise_on_transact is not None:
                raise self.raise_on_transact
            self.transactions.append(request)

    class Meta:
        def __init__(self, client):
            self.client = client

    class Dynamo:
        def __init__(self, client):
            self.meta = Meta(client)

    class Table:
        name = "control"

        def __init__(self):
            self.items = {}
            self.get_calls = []
            self.put_calls = []
            self.delete_calls = []
            self.query_calls = []

        def get_item(self, **request):
            self.get_calls.append(request)
            key = request["Key"]
            item = self.items.get((key["PK"], key["SK"]))
            return {"Item": item} if item is not None else {}

        def put_item(self, **request):
            self.put_calls.append(request)

        def delete_item(self, **request):
            self.delete_calls.append(request)

        def query(self, **request):
            self.query_calls.append(request)
            return {"Items": []}

        def update_item(self, **request):
            key = request["Key"]
            item = dict(self.items.get((key["PK"], key["SK"]), {}))
            names = request.get("ExpressionAttributeNames", {})
            values = request.get("ExpressionAttributeValues", {})

            def attr(placeholder):
                return names.get(placeholder, placeholder.lstrip("#"))

            condition = request.get("ConditionExpression")
            if condition is not None:
                exists = bool(item)
                ok = not exists
                if not ok and "#expiresAt" in condition:
                    ok = item.get(attr("#expiresAt"), 0) <= values[":now"]
                if not ok:
                    raise _FakeClientError("ConditionalCheckFailedException")

            expression = request["UpdateExpression"]
            item.setdefault("PK", key["PK"])
            item.setdefault("SK", key["SK"])
            if expression.startswith("SET"):
                item[attr("#count")] = values[":one"]
                item[attr("#expiresAt")] = values[":expiresAt"]
            elif expression.startswith("ADD"):
                count_attr = attr("#count")
                item[count_attr] = item.get(count_attr, 0) + values[":one"]
            self.items[(key["PK"], key["SK"])] = item
            return {"Attributes": dict(item)}

    client = Client()
    return Table(), Dynamo(client), client


class _FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _cancelled_exception():
    return _FakeClientError("TransactionCanceledException")


def _deserialize(item):
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


if __name__ == "__main__":
    unittest.main()
