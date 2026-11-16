"""The Hermes client — two calls, verified against hermesmq's REST routes.

* ``POST /v1/topics`` — ``{"topicId", "labels"}`` → ``201``, or ``409`` when it exists
  (treated as success). Needs the ``admin`` scope **if** broker auth is enabled.
* ``POST /v1/topics/{id}/messages`` — ``{"payload", "attributes", "ttlSeconds",
  "idempotencyKey", "producerId"}`` → ``202 {messageId, deduplicated}``; ``404`` when the
  topic does not exist; ``400`` on a bad body; ``503`` when the broker cannot accept it
  (``http/PubSubRoutes.scala:95-115``).

Three details that are easy to get wrong, so they are encoded here once:

* **``payload`` is a plain UTF-8 string**, not base64 — the route does
  ``req.payload.getBytes(UTF_8)`` (``PubSubRoutes.scala:242``). JSON text goes straight in.
* **The correlation id rides the ``X-Correlation-Id`` header**, not the body. REST has no
  ``correlationId`` field; the route adopts the header and never mints one, so an absent
  header publishes an uncorrelated message. The header name comes from the constellation's
  one source of truth (``the-lexicon``'s ``codex.correlation``), mirrored in
  :data:`CORRELATION_HEADER`.
* **``idempotencyKey`` is what makes a retried publish safe** — a second publish with the
  same key inside the broker's dedup window collapses onto the first and returns the same
  message id with ``deduplicated: true``. Every Echo notification sets one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

__all__ = [
    "CORRELATION_HEADER",
    "HermesClient",
    "HermesError",
    "PublishedMessage",
]

#: ``X-Correlation-Id`` — the HTTP spelling from the lexicon's ``codex.correlation``
#: (``HTTP_HEADER``). Title-case here and lower-case on gRPC is deliberate, not a typo.
CORRELATION_HEADER: Final = "X-Correlation-Id"

#: Identifies Echo to the broker's per-producer metrics and console.
PRODUCER_ID: Final = "echo-service"

_DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)


class HermesError(Exception):
    """A Hermes call failed. Carries the status when there was one."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """``4xx`` other than 404 and 429 means Echo sent something wrong.

        A ``404`` *is* retryable here: it means the topic is missing, which the next cycle
        fixes by creating it.
        """
        if self.status is None:
            return True
        if self.status in {httpx.codes.NOT_FOUND, httpx.codes.TOO_MANY_REQUESTS}:
            return True
        return not httpx.codes.BAD_REQUEST <= self.status < httpx.codes.INTERNAL_SERVER_ERROR


@dataclass(frozen=True, slots=True)
class PublishedMessage:
    """A published message. ``deduplicated`` means the idempotency key had been seen."""

    message_id: str
    deduplicated: bool = False


class HermesClient:
    """A thin, synchronous HermesMQ client for publishing.

    Echo only ever produces; consuming is somebody else's half of the contract. ``token`` is
    the broker API key when auth is enabled (it is off by default) and comes from the
    environment, never from the channel-follow file.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        producer_id: str = PRODUCER_ID,
        timeout: httpx.Timeout | float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._producer_id = producer_id
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers
        )

    def ensure_topic(self, topic_id: str, *, labels: Mapping[str, str] | None = None) -> None:
        """Create a topic, treating "already exists" as success.

        Publishing to a topic that does not exist is a ``404``, so Echo creates it rather than
        assuming someone else has. ``409`` is the steady-state answer.
        """
        try:
            self._request(
                "POST",
                "/v1/topics",
                json={"topicId": topic_id, "labels": dict(labels or {})},
            )
        except HermesError as exc:
            if exc.status != httpx.codes.CONFLICT:
                raise

    def publish(
        self,
        topic_id: str,
        payload: str,
        *,
        attributes: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> PublishedMessage:
        """Publish one message. ``payload`` is sent as a UTF-8 string, exactly as given."""
        body: dict[str, Any] = {"payload": payload, "producerId": self._producer_id}
        if attributes:
            body["attributes"] = dict(attributes)
        if idempotency_key:
            body["idempotencyKey"] = idempotency_key
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds

        headers = {CORRELATION_HEADER: correlation_id} if correlation_id else None
        answer = self._request(
            "POST", f"/v1/topics/{topic_id}/messages", json=body, headers=headers
        )

        message_id = answer.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            raise HermesError(f"publish to {topic_id} returned no messageId")
        return PublishedMessage(
            message_id=message_id, deduplicated=bool(answer.get("deduplicated"))
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HermesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HermesError(f"{method} {path} did not reach Hermes: {exc}") from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise HermesError(
                f"{method} {path} returned HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
