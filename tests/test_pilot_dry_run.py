from __future__ import annotations

import csv
from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.pilot_dry_run import KEY_COUNT, PilotRunError, main, run_pilot


ADMIN_KEY = "fnx_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


class FakeClock:
    def __init__(self, elapsed: float = 2.5) -> None:
        self._values = iter((100.0, 100.0 + elapsed))

    def now(self):
        return datetime(2026, 7, 22, 12, tzinfo=UTC)

    def monotonic(self):
        return next(self._values)


class FakeClient:
    def __init__(self, *, fail_chunk: int | None = None, leak_list=False) -> None:
        self.fail_chunk = fail_chunk
        self.leak_list = leak_list
        self.issue_calls: list[list[str]] = []
        self.keys: list[dict[str, str]] = []
        self.smoked: list[str] = []
        self.revoked: list[str] = []
        self.inactivated: list[str] = []

    def create_cohort(self, name, *, default_daily_quota, expires_at):
        self.cohort = (name, default_daily_quota, expires_at)
        return {"cohortId": "cohort_pilot", "status": "active"}

    def issue_keys(self, cohort_id, labels):
        self.issue_calls.append(list(labels))
        if self.fail_chunk == len(self.issue_calls):
            raise RuntimeError("simulated issue failure with a secret")
        batch = []
        for label in labels:
            index = len(self.keys) + 1
            item = {
                "keyId": f"student_{index:03d}",
                "label": label,
                "key": f"fnx_dev_{index:032d}",
            }
            self.keys.append(item)
            batch.append(dict(item))
        return {"cohortId": cohort_id, "keys": batch}

    def list_keys(self, cohort_id):
        values = [
            {"keyId": item["keyId"], "label": item["label"], "status": "active"}
            for item in self.keys
        ]
        if self.leak_list and values:
            values[0]["keyHash"] = "must-not-appear"
        return {"cohortId": cohort_id, "keys": values, "cursor": None}

    def smoke_usage(self, plaintext_key):
        self.smoked.append(plaintext_key)
        return {"usedToday": 1, "dailyQuota": 100}

    def revoke_key(self, key_id):
        self.revoked.append(key_id)
        return {"keyId": key_id, "status": "revoked"}

    def inactivate_cohort(self, cohort_id):
        self.inactivated.append(cohort_id)
        return {"cohortId": cohort_id, "status": "inactive"}


class PilotRunTests(unittest.TestCase):
    def test_success_issues_exactly_fifty_in_chunks_writes_0600_and_cleans_up(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot.csv"
            summary = run_pilot(client, output, clock=FakeClock())
            mode = stat.S_IMODE(output.stat().st_mode)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))

        self.assertEqual([len(chunk) for chunk in client.issue_calls], [25, 25])
        self.assertEqual(len({label for chunk in client.issue_calls for label in chunk}), 50)
        self.assertEqual(rows[0], ["label", "key"])
        self.assertEqual(len(rows), KEY_COUNT + 1)
        self.assertTrue(all(len(row) == 2 for row in rows))
        self.assertEqual(mode, 0o600)
        self.assertEqual(len(client.smoked), 3)
        self.assertEqual(len(client.revoked), KEY_COUNT)
        self.assertEqual(client.inactivated, ["cohort_pilot"])
        self.assertEqual(summary["issuanceSeconds"], 2.5)
        self.assertTrue(summary["success"])
        encoded = json.dumps(summary)
        self.assertNotIn("fnx_", encoded)
        self.assertNotIn("keyHash", encoded)

    def test_partial_issuance_failure_still_revokes_discovered_keys_and_inactivates(self):
        client = FakeClient(fail_chunk=2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot.csv"
            with self.assertRaises(PilotRunError) as caught:
                run_pilot(client, output, clock=FakeClock())
            self.assertFalse(output.exists())

        self.assertEqual(len(client.keys), 25)
        self.assertEqual(len(client.revoked), 25)
        self.assertEqual(client.inactivated, ["cohort_pilot"])
        self.assertEqual(caught.exception.summary["keysIssued"], 25)
        self.assertEqual(caught.exception.summary["keysRevocationAttempted"], 25)

    def test_secret_field_in_list_is_rejected_before_csv_but_cleanup_still_runs(self):
        client = FakeClient(leak_list=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot.csv"
            with self.assertRaises(PilotRunError):
                run_pilot(client, output, clock=FakeClock())
            self.assertFalse(output.exists())

        self.assertEqual(len(client.revoked), KEY_COUNT)
        self.assertEqual(client.inactivated, ["cohort_pilot"])

    def test_issuance_threshold_failure_is_cleaned_up(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot.csv"
            with self.assertRaises(PilotRunError) as caught:
                run_pilot(client, output, clock=FakeClock(elapsed=601), max_seconds=600)
            self.assertFalse(output.exists())

        self.assertEqual(caught.exception.summary["issuanceSeconds"], 601)
        self.assertEqual(len(client.revoked), KEY_COUNT)
        self.assertEqual(client.inactivated, ["cohort_pilot"])


class PilotCliTests(unittest.TestCase):
    def test_execute_and_api_key_are_required_before_client_creation(self):
        created = []

        def factory(*args):
            created.append(args)
            return FakeClient()

        with tempfile.TemporaryDirectory() as directory:
            output_path = str(Path(directory) / "pilot.csv")
            stdout = StringIO()
            stderr = StringIO()
            code = main(
                ["--output", output_path],
                environ={"FNX_API_KEY": ADMIN_KEY},
                stdout=stdout,
                stderr=stderr,
                client_factory=factory,
            )
            missing_key_code = main(
                ["--execute", "--output", output_path],
                environ={"FNX_API_KEY": ""},
                stdout=StringIO(),
                stderr=StringIO(),
                client_factory=factory,
            )

        self.assertEqual((code, missing_key_code), (2, 2))
        self.assertEqual(created, [])
        self.assertIn("--execute is required", stderr.getvalue())

    def test_cli_emits_only_non_secret_json_summary(self):
        client = FakeClient()
        stdout = StringIO()
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            code = main(
                ["--execute", "--output", str(Path(directory) / "pilot.csv")],
                environ={"FNX_API_KEY": ADMIN_KEY},
                stdout=stdout,
                stderr=stderr,
                client_factory=lambda *_args: client,
                clock=FakeClock(),
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        summary = json.loads(stdout.getvalue())
        self.assertTrue(summary["success"])
        self.assertNotIn(ADMIN_KEY, stdout.getvalue())
        self.assertNotIn("fnx_dev_", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
