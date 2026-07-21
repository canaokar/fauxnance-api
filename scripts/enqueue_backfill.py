#!/usr/bin/env python3
"""Create or resume a queued US EOD backfill job."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from typing import Sequence


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from src.ingest.backfill import BackfillJobs
from src.ingest.repository import DynamoDBIngestRepository
from src.ingest.universe import load_universe


DEFAULT_UNIVERSE = _REPOSITORY_ROOT / "data" / "universes" / "us-v1.json"
AWS_PROFILE = "megh.io"
AWS_REGION = "eu-west-2"
DATA_TABLE = "fauxnance-dev-data"
CONTROL_TABLE = "fauxnance-dev-control"
INGEST_QUEUE_NAME = "fauxnance-api-dev-ingest"


def _parser() -> argparse.ArgumentParser:
    current_year = datetime.now(UTC).year
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--from-year", type=int, default=current_year - 10)
    parser.add_argument("--to-year", type=int, default=current_year)
    parser.add_argument("--job-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - operator environment guard
        raise SystemExit("boto3 is required; install requirements-dev.txt") from exc

    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    dynamodb = session.resource("dynamodb")
    data_table = dynamodb.Table(DATA_TABLE)
    control_table = dynamodb.Table(CONTROL_TABLE)
    sqs = session.client("sqs")
    queue_name = os.environ.get("INGEST_QUEUE_NAME", INGEST_QUEUE_NAME)
    queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    jobs = BackfillJobs(
        DynamoDBIngestRepository(data_table, control_table),
        control_table,
        sqs,
        queue_url,
    )

    universe = load_universe(args.universe)
    work_count = len(universe.symbols) * (args.to_year - args.from_year + 1)
    print(
        f"Preparing {work_count} work item(s); this can take several minutes...",
        flush=True,
    )
    job_id = jobs.create_job(
        universe,
        from_year=args.from_year,
        to_year=args.to_year,
        job_id=args.job_id,
    )
    if args.job_id is None:
        print(f"Created backfill job {job_id}")
    count = jobs.enqueue_pending(job_id)
    print(f"Enqueued {count} pending work item(s) for {job_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
