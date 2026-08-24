# Operator CLI guide

The `fnx` command is a thin client for the instructor-only admin API. It uses
the default API Gateway URL and never needs direct AWS access.

## Setup

Install the repository in an isolated Python environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

Put the allowlisted admin key in the environment. The CLI intentionally has no
command-line key option, keeping the secret out of shell history and process
lists:

```sh
export FNX_API_KEY="paste-the-admin-key-here"
export FNX_BASE_URL="https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1"
```

No custom domain is configured. Never enable shell tracing while the key is in
the environment.

## Cohorts

```sh
fnx cohorts create "HDFC-GradBatch-2026Q3" \
  --quota 2000 \
  --expires 2026-12-31

fnx cohorts list
```

The expiry date is inclusive in UTC. Cohort and key revocation can take up to
300 seconds to propagate through the API Gateway authorizer cache.

## Issue and manage keys

Create a UTF-8 label file with one handle per line. Labels must be unique and
must not start with `=`, `+`, `-`, or `@`, preventing spreadsheet formula
injection:

```text
amara
team-02
portfolio-lab
```

Issue credentials to a new file:

```sh
fnx keys issue \
  --cohort cohort_1234567890abcdef \
  --labels-file students.txt \
  --output issued-keys.csv
```

The file contains exactly these columns:

```csv
label,key
amara,fnx_dev_example
```

The CLI creates `--output` with mode `0600` and refuses to overwrite an existing
file. Without `--output`, CSV goes to standard output and operational messages
go to standard error. The API returns each plaintext key once; distribute the
CSV through an approved secret-delivery channel and delete local copies when no
longer needed.

The server accepts at most 25 labels per issuance request. The CLI chunks larger
files while preserving their original order.

```sh
fnx keys list --cohort cohort_1234567890abcdef
fnx keys revoke student_1234567890abcdef
```

Listing never returns plaintext keys or hashes. A lost key must be revoked and
reissued.

## Backfills

Start a curated-universe backfill and poll the returned job ID:

```sh
fnx backfill \
  --universe us-phase2-v1 \
  --from 2016-01-01 \
  --to 2026-07-22

fnx jobs status job_1234567890abcdef
```

The admin API returns `202` when it accepts the job. `done`, `total`, and each
entry in `failed` represent symbol-year work units. Poll at a measured interval
(for example, every 30 seconds), not in a tight loop.

## Interactive reference

The interactive reference is public at
`https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1/docs`. Enter an admin
key in the authentication panel to exercise admin operations. Clear the
authorization before sharing the screen or leaving the workstation.
