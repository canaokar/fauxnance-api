# 08 — Instructor Console

A web console for day-to-day class administration, sitting on top of the
admin API's existing cohort and key logic rather than reimplementing it.

Two roles:

- **admin** — manages instructor accounts and every class.
- **instructor** — manages students in the classes they're assigned to.

A **class is a cohort**: the same entity the `fnx` CLI and the admin API
already operate on (see [05-auth-and-quotas](05-auth-and-quotas.md)). The
console, the CLI, and the admin API are three interchangeable views of one
underlying cohort/key model — nothing durable is console-only.

## Architecture

The console is its own Lambda (`console`, `src/console/handler.py`), scoped
to the control table only — it never touches the data table, SQS, or SSM.
`ConsoleService` constructs an `AdminService` directly (without a backfill
coordinator or data table, both optional on `AdminService.__init__`) and
calls its public cohort/key methods — `create_cohort`, `get_cohort_detail`,
`list_cohorts`, `issue_keys`, `revoke_key` — instead of duplicating cohort
or key logic. Everything console-specific (accounts, sessions, login
throttling, instructor↔class assignment) lives in new control-table item
types owned by `src/console/repository.py`:

| Item | PK | SK | Attributes |
|---|---|---|---|
| Console user | `USER#<userId>` | `META` | `userId`, `email` (lowercased), `name`, `passwordHash`, `role` (`admin`\|`instructor`), `status` (`active`\|`disabled`), `createdAt`, `updatedAt` |
| Email lookup | `USEREMAIL#<email>` | `META` | `userId`, `createdAt` — a conditional put enforces email uniqueness |
| Session | `SESSION#<sha256(token)>` | `META` | `userId`, `createdAt`, `expiresAt` (epoch seconds — the DynamoDB TTL attribute) |
| Login throttle | `LOGINFAIL#<email>` | `META` | `count`, `expiresAt` (epoch seconds, TTL attribute) — see Login throttle below |
| User index projection | `USERS` | `<userId>` | `userId`, `email`, `name`, `role`, `status`, `createdAt` — a thin copy of the console-user item used by `all_users()` (a `Query` on `PK = USERS`) so listing users never scans the table; it carries no `passwordHash` |
| Class assignment | `COHORT#<cohortId>` | `INSTRUCTOR#<userId>` | `userId`, `cohortId`, `name`, `email`, `assignedAt` |
| Reverse assignment | `USER#<userId>` | `COHORT#<cohortId>` | `cohortId`, `assignedAt` |

**Student PII placement is deliberate.** A student's name and email are
written to exactly one place: `COHORT#<cohortId>` / `KEY#<keyId>`, as
`studentName` and `studentEmail`. They are never written to `KEY#<hash>`
(the item the authorizer reads on every student request, cached for up to
300 seconds) or to `KEYID#<keyId>`. Each issued key writes three items —
`KEY#<hash>`, `KEYID#<keyId>`, and `COHORT#<cohortId>`/`KEY#<keyId>` — and
identity fields go on only the last one, keeping PII off the hot path the
whole student-facing request volume runs through.

## Auth model

Console accounts are separate from student API keys and from admin keys.
Login is `POST /v1/console/login` with `{email, password}`; every other
route requires `Authorization: Bearer <token>`.

- Passwords are hashed with **PBKDF2-HMAC-SHA256, 210,000 iterations**, a
  16-byte random salt, encoded as `pbkdf2_sha256$<iterations>$<salt>$<hash>`
  (`src/console/passwords.py`). Verification uses `hmac.compare_digest` and
  parses the iteration count out of the stored string, so the cost factor
  can be raised later without invalidating existing hashes. There are no
  composition rules — passwords must be 12–200 characters, nothing else.
- A successful login returns a `secrets.token_urlsafe(32)` bearer token,
  shown once. Only its SHA-256 hash is stored, under `SESSION#<hash>`.
  Sessions live **12 hours**.
- **Why a bearer token and not a cookie:** a cookie-based session would
  require `allowCredentials: true` and a named CORS origin. The HTTP API's
  CORS policy is `allowedOrigins: "*"` with credentials disabled, because
  student browser code calls the public API from arbitrary origins
  (`docs/05-auth-and-quotas.md`). A bearer token lets the console share that
  same API Gateway CORS configuration instead of forking it.
- Expiry is always checked in code (`int(expiresAt) <= int(now.timestamp())`),
  never assumed from TTL deletion — see Operational notes.

### Login throttle

Up to **10 failed attempts per email in a fixed 15-minute window**
(`LOGINFAIL#<email>`). The window is fixed from the first failure, not
sliding: a failure that lands while a window is still active only
increments `count` and leaves the stored `expiresAt` untouched, so repeated
mistypes cannot extend the lockout. Only a failure recorded after the
window has elapsed (or with no record present) resets `count` to 1 and
arms a new `expiresAt` = now + 900s. `record_login_failure` implements this
as a conditional write — `attribute_not_exists(PK) OR expiresAt <= :now`
triggers the reset — falling back to a plain `ADD count :one` (no
`expiresAt` write) when that condition fails. Without this, an operator who
mistypes their password once right after a lockout expires would re-arm a
fresh 15-minute window on every subsequent attempt, including the correct
one, and could be locked out indefinitely. At the limit, the API rejects
with `429 TOO_MANY_ATTEMPTS` without even checking the password.
Unknown-email and wrong-password both return the same
`401 INVALID_CREDENTIALS` — "Incorrect email or password." — so a failed
login never reveals which account exists.

## Routes

All routes are under `/v1/console`. The envelope matches the rest of the
API: success is `{"data": {...}, "meta": {"asOf": "..."}}`, errors are
`{"error": {"code": "...", "message": "...", "details": {}}}`.

| Method + path | Who |
|---|---|
| `POST /v1/console/login` | anyone |
| `POST /v1/console/logout` | any session |
| `GET /v1/console/me` | any session |
| `GET /v1/console/users` | admin |
| `POST /v1/console/users` | admin |
| `PATCH /v1/console/users/{userId}` | admin |
| `GET /v1/console/classes` | any session (instructors see only assigned classes) |
| `POST /v1/console/classes` | admin |
| `GET /v1/console/classes/{cohortId}` | admin, or an instructor assigned to that class |
| `POST /v1/console/classes/{cohortId}/instructors` | admin |
| `DELETE /v1/console/classes/{cohortId}/instructors/{userId}` | admin |
| `POST /v1/console/classes/{cohortId}/students` | admin, or an instructor assigned to that class |
| `DELETE /v1/console/classes/{cohortId}/students/{keyId}` | admin, or an instructor assigned to that class |

Error codes: `VALIDATION_ERROR` 400, `INVALID_CREDENTIALS` 401,
`UNAUTHENTICATED` 401, `FORBIDDEN` 403, `NOT_FOUND` 404, `CONFLICT` 409,
`TOO_MANY_ATTEMPTS` 429, `INTERNAL_ERROR` 500.

### Authorization matrix

Every check below is enforced in `ConsoleService`, not in the HTTP layer or
the frontend, so it is unit-tested independent of routing:

- **admin** — everything, every class.
- **instructor** — `GET /classes` returns only classes present in their
  `USER#<id>/COHORT#<id>` index. Any `/classes/{cohortId}` route requires
  that assignment item to exist, or it's `403 FORBIDDEN`. All `/users`
  routes, `POST /classes`, and both `/instructors` routes are
  `403 FORBIDDEN` for instructors.
- An admin cannot disable or demote their own account
  (`PATCH /users/{self}` with `status: "disabled"` is `409 CONFLICT`), and
  assigning a user whose role is `admin` as a class instructor is a
  `VALIDATION_ERROR` — admins already see everything.

## Issuing student keys

`POST /v1/console/classes/{cohortId}/students` accepts a list of
`{name, email}` and issues one key per student, exactly like the admin API's
`labels`/`students` form of key issuance. It accepts **at most 25 students
per call**; a longer list gets `400 VALIDATION_ERROR` telling the caller to
split it.

25 is a hard ceiling, not a tunable preference. `TransactWriteItems` allows
at most 100 items per transaction, and issuing one key writes three items
(`KEY#<hash>`, `KEYID#<keyId>`, `COHORT#<cohortId>`/`KEY#<keyId>`) plus one
condition check that the cohort is still active: `25 × 3 + 1 = 76`, safely
under the limit. The console does not chunk a larger list into several
`issue_keys` calls, because that would give up all-or-nothing issuance and
could leave a class half-populated if a later chunk failed.

Labels are derived from each student's name (lowercase, non-alphanumerics
collapsed to `-`, base trimmed to 40 characters; collisions inside one
request get a `-2`, `-3`, ... de-duplication suffix, appended *after*
truncation). The 40-character figure is therefore the base length, not the
final bound — with up to 25 students per call the suffix can add up to
`-25`, so a generated label can reach 43 characters. That is still well
under the admin layer's own 120-character label limit, so it is safe.
Returned keys are plaintext and shown **exactly once**, in the `issued`
response — see Operational notes.

## First-admin bootstrap

The console has no way to create its own first account — every `/users`
route requires an admin session already. `scripts/create_console_user.py`
is the one operator utility that writes a console user record directly to
the control table:

```sh
.venv/bin/python scripts/create_console_user.py \
  --email operator@institution.edu \
  --name "Operator Name" \
  --role admin
```

The password is never accepted as a command-line argument — an argv value
would land in shell history and be visible to other processes via `ps`. Set
`FAUXNANCE_CONSOLE_PASSWORD` in the environment to choose it, or leave it
unset and the script generates a 24-character random password and prints it
once. `--role` defaults to `admin`; `--stage`, `--region`, `--profile`
(default `megh.io`), and `--table` follow the same conventions as the rest
of the operator tooling.

## Frontend deployment

`frontend/` is plain HTML/CSS/vanilla JS — no build step, no dependencies,
hash-routed so a deep link like `#/classes/cohort_xyz` still works served as
static files.

Deploy to Cloudflare Pages with:

- Build command: **none**
- Output directory: **`frontend`**

`frontend/config.js` is the single file that points the console at an API:

```js
window.FAUXNANCE_API = "https://your-api-id.execute-api.eu-west-2.amazonaws.com";
```

The signed-in session token lives in `sessionStorage` under
`fauxnance.session` — cleared when the tab closes, and cleared with a
redirect to the login screen whenever the API responds `401`.

## Operational notes

- **Revocation lag is unchanged.** `DELETE .../students/{keyId}` calls the
  same `AdminService.revoke_key` the admin API and `fnx keys revoke` use.
  The student key still stops working only once the API Gateway
  authorizer's 300-second response cache expires — the console does not
  shorten or bypass that.
- **TTL is cleanup, not enforcement.** DynamoDB TTL reaps `SESSION#...` and
  `LOGINFAIL#...` items once their epoch-second `expiresAt` passes, but TTL
  deletion is asynchronous and never the thing enforcing expiry — session
  validation and the login throttle both compare `expiresAt` numerically in
  code first.
- **Keys are shown once.** Like admin-issued keys, a student's plaintext
  key appears only in the `issued` response at creation time; only its hash
  is stored. A lost key must be revoked and a new one issued — there is no
  recovery path.
