"""HTTP API payload-v2 simple-response Lambda authorizer."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import Any, Callable, Mapping, Protocol

from src.shared.auth import AuthContext, hash_api_key, is_api_key_shape, is_expired


class KeyRepository(Protocol):
    def get_key(self, digest: str) -> Mapping[str, Any] | None: ...

    def get_cohort(self, cohort_id: str) -> Mapping[str, Any] | None: ...


class AdminAllowlist(Protocol):
    def contains(self, key_id: str) -> bool: ...


class AuthorizationDenied(Exception):
    """A normal authentication rejection, safe to map to ``isAuthorized=false``."""


class AuthorizerService:
    def __init__(
        self,
        repository: KeyRepository,
        admin_allowlist: AdminAllowlist,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._admin_allowlist = admin_allowlist
        self._clock = clock or (lambda: datetime.now(UTC))

    def authorize(self, plaintext_key: str) -> AuthContext:
        if not is_api_key_shape(plaintext_key):
            raise AuthorizationDenied("unknown key")

        key = self._repository.get_key(hash_api_key(plaintext_key))
        if key is None:
            raise AuthorizationDenied("unknown key")
        if key.get("status") != "active":
            raise AuthorizationDenied("inactive key")

        now = self._clock()
        try:
            expired = is_expired(key.get("expiresAt"), now=now)
        except ValueError as exc:
            raise AuthorizationDenied("invalid key expiry") from exc
        if expired:
            raise AuthorizationDenied("expired key")

        key_id = _required_string(key, "keyId")
        key_type = key.get("type")
        if key_type == "admin":
            if not self._admin_allowlist.contains(key_id):
                raise AuthorizationDenied("admin key is not allowlisted")
            return AuthContext(key_id=key_id, key_type="admin")
        if key_type != "student":
            raise AuthorizationDenied("unknown key type")

        cohort_id = _required_string(key, "cohortId")
        cohort = self._repository.get_cohort(cohort_id)
        if cohort is None or cohort.get("status") != "active":
            raise AuthorizationDenied("inactive cohort")
        try:
            cohort_expired = is_expired(cohort.get("expiresAt"), now=now)
        except ValueError as exc:
            raise AuthorizationDenied("invalid cohort expiry") from exc
        if cohort_expired:
            raise AuthorizationDenied("expired cohort")

        quota_value = key.get("dailyQuota")
        if quota_value is None:
            quota_value = cohort.get("defaultDailyQuota")
        try:
            daily_quota = int(quota_value)
        except (TypeError, ValueError) as exc:
            raise AuthorizationDenied("invalid daily quota") from exc
        if daily_quota < 1:
            raise AuthorizationDenied("invalid daily quota")
        return AuthContext(
            key_id=key_id,
            key_type="student",
            cohort_id=cohort_id,
            daily_quota=daily_quota,
        )


class DynamoKeyRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def get_key(self, digest: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(
            Key={"PK": f"KEY#{digest}", "SK": "META"},
            ConsistentRead=True,
        )
        return response.get("Item")

    def get_cohort(self, cohort_id: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(
            Key={"PK": f"COHORT#{cohort_id}", "SK": "META"},
            ConsistentRead=True,
        )
        return response.get("Item")


class SsmAdminAllowlist:
    """Read the comma-separated admin key IDs once per warm Lambda instance."""

    def __init__(self, ssm_client: Any, parameter_name: str) -> None:
        self._ssm_client = ssm_client
        self._parameter_name = parameter_name
        self._key_ids: frozenset[str] | None = None

    def contains(self, key_id: str) -> bool:
        if self._key_ids is None:
            response = self._ssm_client.get_parameter(
                Name=self._parameter_name,
                WithDecryption=False,
            )
            value = response["Parameter"]["Value"]
            self._key_ids = frozenset(
                candidate.strip() for candidate in value.split(",") if candidate.strip()
            )
        return key_id in self._key_ids


_service: AuthorizerService | None = None


def handler(
    event: Mapping[str, Any],
    _context: object,
    *,
    service: AuthorizerService | None = None,
) -> dict[str, Any]:
    """Authorize one API Gateway HTTP API request using simple responses."""

    candidate = _extract_api_key(event)
    if candidate is None:
        return {"isAuthorized": False}

    try:
        auth_context = (service or _default_service()).authorize(candidate)
    except AuthorizationDenied:
        return {"isAuthorized": False}
    return {
        "isAuthorized": True,
        "context": auth_context.to_gateway_context(),
    }


# Descriptive alias for local callers; Serverless invokes ``handler`` above.
lambda_handler = handler


def _default_service() -> AuthorizerService:
    global _service
    if _service is None:
        import boto3

        table_name = os.environ["CONTROL_TABLE"]
        parameter_name = os.environ["ADMIN_KEY_IDS_PARAMETER"]
        table = boto3.resource("dynamodb").Table(table_name)
        allowlist = SsmAdminAllowlist(boto3.client("ssm"), parameter_name)
        _service = AuthorizerService(DynamoKeyRepository(table), allowlist)
    return _service


def _extract_api_key(event: Mapping[str, Any]) -> str | None:
    identity_source = event.get("identitySource")
    if isinstance(identity_source, list) and identity_source:
        value = identity_source[0]
        if isinstance(value, str) and value:
            return value.strip()

    headers = event.get("headers")
    if not isinstance(headers, Mapping):
        return None
    for name, value in headers.items():
        if str(name).lower() == "x-api-key" and isinstance(value, str):
            return value.strip()
    return None


def _required_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise AuthorizationDenied(f"missing {name}")
    return value
