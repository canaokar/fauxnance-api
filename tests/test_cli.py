from __future__ import annotations

import csv
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.request import Request

from cli.client import DEFAULT_BASE_URL, TransportResponse
from cli.main import main


API_KEY = "fnx_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


def response(data, *, status=200, headers=None):
    payload = data if status >= 400 else {"data": data, "meta": {}}
    return TransportResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


class RecordingTransport:
    def __init__(self, responder=None):
        self.requests: list[tuple[Request, float]] = []
        self._responder = responder or (lambda _request: response({"ok": True}))

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self._responder(request)


def invoke(argv, transport, *, environ=None):
    stdout = StringIO()
    stderr = StringIO()
    code = main(
        argv,
        environ=environ or {"FNX_API_KEY": API_KEY},
        stdout=stdout,
        stderr=stderr,
        transport=transport,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def request_json(request):
    return json.loads(request.data.decode("utf-8")) if request.data else None


class ClientCommandTests(unittest.TestCase):
    def test_cohort_create_uses_default_gateway_url_and_expected_contract(self):
        transport = RecordingTransport(
            lambda _request: response({"cohortId": "cohort_1"}, status=201)
        )

        code, stdout, stderr = invoke(
            [
                "cohorts",
                "create",
                "Graduate Batch",
                "--quota",
                "2000",
                "--expires",
                "2026-12-31",
            ],
            transport,
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"cohortId": "cohort_1"})
        request, timeout = transport.requests[0]
        self.assertEqual(request.full_url, f"{DEFAULT_BASE_URL}/admin/cohorts")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 15.0)
        self.assertEqual(
            request_json(request),
            {
                "name": "Graduate Batch",
                "defaultDailyQuota": 2000,
                "expiresAt": "2026-12-31",
            },
        )
        self.assertEqual(request.get_header("X-api-key"), API_KEY)

    def test_cohort_list_uses_configured_base_url(self):
        transport = RecordingTransport(lambda _request: response({"cohorts": []}))
        code, _, _ = invoke(
            ["cohorts", "list"],
            transport,
            environ={
                "FNX_API_KEY": API_KEY,
                "FNX_BASE_URL": "https://example.test/custom/",
            },
        )

        self.assertEqual(code, 0)
        request, _ = transport.requests[0]
        self.assertEqual(request.full_url, "https://example.test/custom/admin/cohorts")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)

    def test_key_list_and_revoke_encode_parameters(self):
        transport = RecordingTransport()

        list_code, _, _ = invoke(
            ["keys", "list", "--cohort", "cohort one"], transport
        )
        revoke_code, _, _ = invoke(["keys", "revoke", "key/id"], transport)

        self.assertEqual((list_code, revoke_code), (0, 0))
        list_request, _ = transport.requests[0]
        revoke_request, _ = transport.requests[1]
        self.assertEqual(
            list_request.full_url,
            f"{DEFAULT_BASE_URL}/admin/keys?cohortId=cohort+one",
        )
        self.assertEqual(list_request.get_method(), "GET")
        self.assertEqual(
            revoke_request.full_url, f"{DEFAULT_BASE_URL}/admin/keys/key%2Fid"
        )
        self.assertEqual(revoke_request.get_method(), "DELETE")

    def test_backfill_and_job_status_use_documented_routes(self):
        transport = RecordingTransport()

        backfill_code, _, _ = invoke(
            [
                "backfill",
                "--universe",
                "us-phase2-v1",
                "--from",
                "2016-01-01",
                "--to",
                "2026-07-22",
            ],
            transport,
        )
        status_code, _, _ = invoke(["jobs", "status", "job/a"], transport)

        self.assertEqual((backfill_code, status_code), (0, 0))
        backfill, _ = transport.requests[0]
        status, _ = transport.requests[1]
        self.assertEqual(backfill.get_method(), "POST")
        self.assertEqual(
            backfill.full_url, f"{DEFAULT_BASE_URL}/admin/ingest/backfill"
        )
        self.assertEqual(
            request_json(backfill),
            {
                "universe": "us-phase2-v1",
                "from": "2016-01-01",
                "to": "2026-07-22",
            },
        )
        self.assertEqual(
            status.full_url, f"{DEFAULT_BASE_URL}/admin/ingest/jobs/job%2Fa"
        )

    def test_missing_key_fails_without_making_a_request(self):
        transport = RecordingTransport()
        code, stdout, stderr = invoke(
            ["cohorts", "list"], transport, environ={"FNX_API_KEY": ""}
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("FNX_API_KEY is required", stderr)
        self.assertEqual(transport.requests, [])


class KeyIssuanceTests(unittest.TestCase):
    def test_fifty_labels_are_chunked_and_csv_preserves_input_order_and_quoting(self):
        labels = ["team,zero", *[f"team-{index}" for index in range(1, 50)]]
        chunks = []

        def responder(request):
            body = request_json(request)
            chunks.append(body)
            # Deliberately reverse the service response; output follows input.
            keys = [
                {"label": label, "key": f"key-for-{label}"}
                for label in reversed(body["labels"])
            ]
            return response({"keys": keys}, status=201)

        transport = RecordingTransport(responder)
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.txt"
            labels_path.write_text("\n".join(labels) + "\n", encoding="utf-8")
            code, stdout, stderr = invoke(
                [
                    "keys",
                    "issue",
                    "--cohort",
                    "cohort_1",
                    "--labels-file",
                    str(labels_path),
                    "--quota",
                    "1500",
                ],
                transport,
            )

        self.assertEqual(code, 0)
        self.assertEqual([len(chunk["labels"]) for chunk in chunks], [25, 25])
        self.assertTrue(all(chunk["dailyQuota"] == 1500 for chunk in chunks))
        rows = list(csv.reader(StringIO(stdout)))
        self.assertEqual(rows[0], ["label", "key"])
        self.assertEqual([row[0] for row in rows[1:]], labels)
        self.assertEqual(rows[1], ["team,zero", "key-for-team,zero"])
        self.assertEqual(stderr, "Issued 50 key(s).\n")
        self.assertTrue(stdout.startswith('label,key\n"team,zero"'))

    def test_output_file_is_exclusively_created_with_owner_only_permissions(self):
        def responder(request):
            labels = request_json(request)["labels"]
            return response(
                {"keys": [{"label": label, "key": f"secret-{label}"} for label in labels]},
                status=201,
            )

        transport = RecordingTransport(responder)
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.txt"
            output_path = Path(directory) / "issued.csv"
            labels_path.write_text("student-1\n", encoding="utf-8")
            code, stdout, stderr = invoke(
                [
                    "keys",
                    "issue",
                    "--cohort",
                    "cohort_1",
                    "--labels-file",
                    str(labels_path),
                    "--output",
                    str(output_path),
                ],
                transport,
            )
            mode = stat.S_IMODE(output_path.stat().st_mode)
            contents = output_path.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "Issued 1 key(s).\n")
        self.assertEqual(mode, 0o600)
        self.assertEqual(contents, "label,key\nstudent-1,secret-student-1\n")

    def test_existing_output_file_is_not_overwritten_or_sent_to_api(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.txt"
            output_path = Path(directory) / "issued.csv"
            labels_path.write_text("student-1\n", encoding="utf-8")
            output_path.write_text("keep me", encoding="utf-8")
            code, stdout, stderr = invoke(
                [
                    "keys",
                    "issue",
                    "--cohort",
                    "cohort_1",
                    "--labels-file",
                    str(labels_path),
                    "--output",
                    str(output_path),
                ],
                transport,
            )
            contents = output_path.read_text(encoding="utf-8")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("File exists", stderr)
        self.assertEqual(contents, "keep me")
        self.assertEqual(transport.requests, [])

    def test_duplicate_and_spreadsheet_formula_labels_are_rejected(self):
        for contents in ("same\nsame\n", "@malicious\n"):
            with self.subTest(contents=contents):
                transport = RecordingTransport()
                with tempfile.TemporaryDirectory() as directory:
                    labels_path = Path(directory) / "labels.txt"
                    labels_path.write_text(contents, encoding="utf-8")
                    code, _, _ = invoke(
                        [
                            "keys",
                            "issue",
                            "--cohort",
                            "cohort_1",
                            "--labels-file",
                            str(labels_path),
                        ],
                        transport,
                    )
                self.assertEqual(code, 1)
                self.assertEqual(transport.requests, [])


class ErrorHandlingTests(unittest.TestCase):
    def test_api_error_includes_code_and_retry_after_but_redacts_keys(self):
        leaked = "fnx_dev_1234567890abcdefghijklmnopqrstuv"
        transport = RecordingTransport(
            lambda _request: response(
                {
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"Do not show {API_KEY} or {leaked}",
                    }
                },
                status=429,
                headers={"retry-after": "60"},
            )
        )

        code, stdout, stderr = invoke(["cohorts", "list"], transport)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("RATE_LIMITED (429)", stderr)
        self.assertIn("Retry after 60 seconds", stderr)
        self.assertNotIn(API_KEY, stderr)
        self.assertNotIn(leaked, stderr)
        self.assertIn("[REDACTED]", stderr)

    def test_transport_exception_text_cannot_leak_the_api_key(self):
        def fail(_request):
            raise RuntimeError(f"request headers contained {API_KEY}")

        code, _, stderr = invoke(["cohorts", "list"], RecordingTransport(fail))

        self.assertEqual(code, 1)
        self.assertEqual(
            stderr, "fnx: request failed before receiving a response\n"
        )
        self.assertNotIn(API_KEY, stderr)

    def test_malformed_success_envelope_returns_nonzero(self):
        transport = RecordingTransport(
            lambda _request: TransportResponse(200, {}, b'{"cohorts":[]}')
        )

        code, stdout, stderr = invoke(["cohorts", "list"], transport)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("data envelope", stderr)


if __name__ == "__main__":
    unittest.main()
