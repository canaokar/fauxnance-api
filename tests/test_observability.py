from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


class PackagedObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._package = tempfile.TemporaryDirectory(
            prefix="fauxnance-observability-test-"
        )
        result = subprocess.run(
            [
                "npx",
                "serverless",
                "package",
                "--package",
                cls._package.name,
                "--stage",
                "dev",
                "--region",
                "eu-west-2",
                "--aws-profile",
                "megh.io",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                "Serverless package failed:\n" + result.stdout + result.stderr
            )
        path = Path(cls._package.name) / "cloudformation-template-update-stack.json"
        cls.template = json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._package.cleanup()

    def test_dashboard_has_the_six_operational_widgets_and_exact_dimensions(self):
        resource = self.template["Resources"]["OperationsDashboard"]
        self.assertEqual(resource["Type"], "AWS::CloudWatch::Dashboard")
        self.assertEqual(
            resource["Properties"]["DashboardName"],
            "fauxnance-api-dev-operations",
        )
        body = json.loads(_resolve_dashboard(resource["Properties"]["DashboardBody"]))
        widgets = {
            widget["properties"]["title"]: widget["properties"]
            for widget in body["widgets"]
        }
        self.assertEqual(
            set(widgets),
            {
                "HTTP API traffic and errors",
                "HTTP API p95 latency",
                "Lambda invocations, errors, and throttles",
                "Lambda p95 duration",
                "Ingest queues",
                "DynamoDB capacity and throttles",
            },
        )

        traffic = widgets["HTTP API traffic and errors"]["metrics"]
        self.assertEqual(traffic[0][:6], ["AWS/ApiGateway", "Count", "ApiId", "api-id", "Stage", "$default"])
        self.assertEqual([row[1] for row in traffic], ["Count", "4xx", "5xx"])
        latency = widgets["HTTP API p95 latency"]["metrics"]
        self.assertEqual(latency[0][:6], ["AWS/ApiGateway", "Latency", "ApiId", "api-id", "Stage", "$default"])
        self.assertEqual([row[1] for row in latency], ["Latency", "IntegrationLatency"])
        self.assertTrue(all(row[-1]["stat"] == "p95" for row in latency))

        expected_functions = [
            "fauxnance-api-dev-api",
            "fauxnance-api-dev-admin",
            "fauxnance-api-dev-authorizer",
            "fauxnance-api-dev-dispatcher",
            "fauxnance-api-dev-backfillCoordinator",
            "fauxnance-api-dev-ingestWorker",
        ]
        lambda_metrics = widgets["Lambda invocations, errors, and throttles"]["metrics"]
        self.assertEqual(len(lambda_metrics), 18)
        self.assertEqual(
            [lambda_metrics[index][3] for index in range(0, 18, 3)],
            expected_functions,
        )
        self.assertEqual(
            [lambda_metrics[index][1] for index in range(18)],
            ["Invocations", "Errors", "Throttles"] * 6,
        )
        duration = widgets["Lambda p95 duration"]["metrics"]
        self.assertEqual([row[3] for row in duration], expected_functions)
        self.assertTrue(all(row[1] == "Duration" and row[-1]["stat"] == "p95" for row in duration))

        queues = widgets["Ingest queues"]["metrics"]
        self.assertEqual(queues[0][3], "fauxnance-api-dev-ingest")
        self.assertEqual(
            [row[1] for row in queues[:3]],
            [
                "ApproximateNumberOfMessagesVisible",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateAgeOfOldestMessage",
            ],
        )
        self.assertEqual(queues[3][3], "fauxnance-api-dev-ingest-dlq")

        dynamodb = widgets["DynamoDB capacity and throttles"]["metrics"]
        self.assertEqual(dynamodb[0][3], "fauxnance-dev-data")
        self.assertEqual(dynamodb[4][3], "fauxnance-dev-control")
        self.assertEqual(
            [row[1] for row in dynamodb],
            [
                "ConsumedReadCapacityUnits",
                "ConsumedWriteCapacityUnits",
                "ReadThrottleEvents",
                "WriteThrottleEvents",
            ]
            * 2,
        )

    def test_alarm_names_metrics_dimensions_and_thresholds_are_exact(self):
        resources = self.template["Resources"]
        expected = {
            "Api5xxAlarm": (
                "fauxnance-api-dev-api-5xx",
                "AWS/ApiGateway",
                "5xx",
                [{"Name": "ApiId", "Value": {"Ref": "HttpApi"}}, {"Name": "Stage", "Value": "$default"}],
                1,
                1,
                1,
            ),
            "AuthorizerErrorsAlarm": (
                "fauxnance-api-dev-authorizer-errors",
                "AWS/Lambda",
                "Errors",
                [{"Name": "FunctionName", "Value": {"Ref": "AuthorizerLambdaFunction"}}],
                1,
                1,
                1,
            ),
            "DispatcherErrorsAlarm": (
                "fauxnance-api-dev-dispatcher-errors",
                "AWS/Lambda",
                "Errors",
                [{"Name": "FunctionName", "Value": {"Ref": "DispatcherLambdaFunction"}}],
                1,
                1,
                1,
            ),
            "BackfillCoordinatorErrorsAlarm": (
                "fauxnance-api-dev-backfill-coordinator-errors",
                "AWS/Lambda",
                "Errors",
                [{"Name": "FunctionName", "Value": {"Ref": "BackfillCoordinatorLambdaFunction"}}],
                1,
                1,
                1,
            ),
            "IngestQueueAgeAlarm": (
                "fauxnance-api-dev-ingest-queue-age",
                "AWS/SQS",
                "ApproximateAgeOfOldestMessage",
                [{"Name": "QueueName", "Value": {"Fn::GetAtt": ["IngestQueue", "QueueName"]}}],
                1800,
                2,
                2,
            ),
        }
        for logical_id, values in expected.items():
            with self.subTest(alarm=logical_id):
                properties = resources[logical_id]["Properties"]
                name, namespace, metric, dimensions, threshold, periods, datapoints = values
                self.assertEqual(resources[logical_id]["Type"], "AWS::CloudWatch::Alarm")
                self.assertEqual(properties["AlarmName"], name)
                self.assertEqual(properties["Namespace"], namespace)
                self.assertEqual(properties["MetricName"], metric)
                self.assertEqual(properties["Dimensions"], dimensions)
                self.assertEqual(properties["Period"], 300)
                self.assertEqual(properties["Threshold"], threshold)
                self.assertEqual(properties["EvaluationPeriods"], periods)
                self.assertEqual(properties["DatapointsToAlarm"], datapoints)
                self.assertEqual(properties["TreatMissingData"], "notBreaching")

        dlq = resources["IngestDeadLetterQueueAlarm"]["Properties"]
        self.assertEqual(dlq["AlarmName"], "fauxnance-api-dev-ingest-dlq-not-empty")
        self.assertEqual(dlq["MetricName"], "ApproximateNumberOfMessagesVisible")
        lambda_error_targets = {
            alarm["Properties"]["Dimensions"][0]["Value"].get("Ref")
            for alarm in resources.values()
            if alarm.get("Type") == "AWS::CloudWatch::Alarm"
            and alarm.get("Properties", {}).get("Namespace") == "AWS/Lambda"
            and alarm["Properties"].get("MetricName") == "Errors"
        }
        self.assertNotIn("IngestWorkerLambdaFunction", lambda_error_targets)

    def test_template_has_no_budget_resource(self):
        types = {resource.get("Type") for resource in self.template["Resources"].values()}
        self.assertNotIn("AWS::Budgets::Budget", types)


def _resolve_dashboard(value: Any) -> str:
    refs = {
        "HttpApi": "api-id",
        "ApiLambdaFunction": "fauxnance-api-dev-api",
        "AdminLambdaFunction": "fauxnance-api-dev-admin",
        "AuthorizerLambdaFunction": "fauxnance-api-dev-authorizer",
        "DispatcherLambdaFunction": "fauxnance-api-dev-dispatcher",
        "BackfillCoordinatorLambdaFunction": "fauxnance-api-dev-backfillCoordinator",
        "IngestWorkerLambdaFunction": "fauxnance-api-dev-ingestWorker",
    }
    get_atts = {
        ("IngestQueue", "QueueName"): "fauxnance-api-dev-ingest",
        ("IngestDeadLetterQueue", "QueueName"): "fauxnance-api-dev-ingest-dlq",
    }
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Ref" in value:
        return refs[value["Ref"]]
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return get_atts[tuple(value["Fn::GetAtt"])]
    if isinstance(value, dict) and "Fn::Join" in value:
        delimiter, parts = value["Fn::Join"]
        return delimiter.join(_resolve_dashboard(part) for part in parts)
    raise AssertionError(f"unsupported dashboard intrinsic: {value!r}")


if __name__ == "__main__":
    unittest.main()
