from datetime import UTC, date, datetime
import hashlib
import unittest

from scripts.bootstrap_admin import (
    bootstrap_admin,
    generate_plaintext_key,
    seed_dev_student,
)


ADMIN_KEY = "fnx_dev_" + "A" * 32
STUDENT_KEY = "fnx_dev_" + "B" * 32
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class FakeDynamoDB:
    def __init__(self):
        self.transactions = []

    def transact_write_items(self, **request):
        self.transactions.append(request)


class FakeSSM:
    def __init__(self, value="REPLACE_ME"):
        self.value = value
        self.puts = []

    def get_parameter(self, **_request):
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, **request):
        self.puts.append(request)
        self.value = request["Value"]


class BootstrapAdminTests(unittest.TestCase):
    def test_generated_key_has_documented_shape(self):
        key = generate_plaintext_key("dev", chooser=lambda _alphabet: "Z")
        self.assertEqual(key, "fnx_dev_" + "Z" * 32)

    def test_admin_writes_hash_and_lookup_then_updates_allowlist(self):
        dynamodb = FakeDynamoDB()
        ssm = FakeSSM("existing-admin,REPLACE_ME")

        issued = bootstrap_admin(
            dynamodb,
            ssm,
            table_name="control",
            parameter_name="/fauxnance/dev/admin/key_ids",
            stage="dev",
            label="operator",
            now=NOW,
            key_id="admin-new",
            plaintext=ADMIN_KEY,
        )

        self.assertEqual(issued.plaintext, ADMIN_KEY)
        puts = [entry["Put"] for entry in dynamodb.transactions[0]["TransactItems"]]
        digest = hashlib.sha256(ADMIN_KEY.encode()).hexdigest()
        self.assertEqual(puts[0]["Item"]["PK"], {"S": f"KEY#{digest}"})
        self.assertEqual(puts[1]["Item"]["PK"], {"S": "KEYID#admin-new"})
        self.assertEqual(puts[1]["Item"]["keyHash"], {"S": digest})
        self.assertNotIn(ADMIN_KEY, repr(dynamodb.transactions))
        self.assertEqual(ssm.value, "admin-new,existing-admin")
        self.assertTrue(ssm.puts[0]["Overwrite"])

    def test_student_cohort_and_three_key_records_are_one_transaction(self):
        dynamodb = FakeDynamoDB()

        issued = seed_dev_student(
            dynamodb,
            table_name="control",
            stage="dev",
            label="team-1",
            daily_quota=25,
            expires_at=date(2026, 12, 31),
            now=NOW,
            cohort_id="cohort-dev",
            key_id="student-dev",
            plaintext=STUDENT_KEY,
        )

        self.assertEqual(issued.key_id, "student-dev")
        puts = [entry["Put"] for entry in dynamodb.transactions[0]["TransactItems"]]
        self.assertEqual(len(puts), 4)
        self.assertEqual(puts[0]["Item"]["PK"], {"S": "COHORT#cohort-dev"})
        self.assertEqual(puts[3]["Item"]["SK"], {"S": "KEY#student-dev"})
        self.assertEqual(puts[1]["Item"]["dailyQuota"], {"N": "25"})
        self.assertNotIn(STUDENT_KEY, repr(dynamodb.transactions))


if __name__ == "__main__":
    unittest.main()
