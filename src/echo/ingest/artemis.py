"""The Artemis client — the only service Echo hands media to.

**Verified topology (2026-11-13), and it corrects the canonical note:** Artemis's
``POST /uploads`` streams the request body to Apollo itself, content-addressed, then seeds the
post and publishes the ``ProcessMediaJob`` that Hephaestus consumes
(``artemis-service/.../http/UploadRoutes.scala``, ``ingest/UploadService.scala``). So Echo talks
to **Artemis only** — there is no Apollo client here and no Hephaestus client either.

The four calls Echo needs, all verified against the routes:

* ``POST /uploads`` — raw body + ``Content-Type`` → ``201 {postId, status}``; ``502`` when
  Apollo or Hermes fails underneath; ``400`` on an empty body.
* ``PATCH /posts/{id}/tags`` — ``{"tags": [...]}``, a **full-set replace**; ``400`` on a tag
  Artemis rejects.
* ``POST /pools`` — ``{"id", "name"}`` → ``201``, or ``409`` when the pool already exists,
  which this client treats as success.
* ``POST /pools/{id}/posts`` — ``{"postId"}``; adding a post already in the pool is an
  idempotent no-op in the pool aggregate.

**Everything here is idempotent on retry**, which is what makes a mid-ingest failure safe to
re-run: the upload dedups on md5 (the same bytes return the same post), the tags PATCH is a
replace, and both pool calls are no-ops when already applied.

Artemis exposes no authentication on these routes today (in-cluster trust). An optional bearer
token is accepted anyway so that turning auth on later is a config change here, not a code
change — and it comes from the environment, never from the channel-follow file.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

__all__ = [
    "ArtemisClient",
    "ArtemisError",
    "UploadedPost",
    "content_type_for",
]

# mimetypes does not know Matroska on every platform, and guessing wrong changes the media
# class Artemis derives (`video/mp4` → "video"), which decides whether Hephaestus produces a
# transcode at all. An explicit map for the containers yt-dlp actually writes.
_CONTENT_TYPES: Final[dict[str, str]] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
}

_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"

# A 1080p upload is gigabytes over the LAN, and Artemis buffers the whole body to compute its
# md5 before answering, so the write and read timeouts are generous by necessity.
_DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=10.0, write=1800.0, read=1800.0, pool=10.0)


class ArtemisError(Exception):
    """An Artemis call failed. Carries the status when there was one."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """``4xx`` other than 429 means Echo sent something wrong; retrying won't fix it."""
        if self.status is None:
            return True  # transport failure — the call never landed
        if self.status == httpx.codes.TOO_MANY_REQUESTS:
            return True
        return not httpx.codes.BAD_REQUEST <= self.status < httpx.codes.INTERNAL_SERVER_ERROR


@dataclass(frozen=True, slots=True)
class UploadedPost:
    """The result of an upload: the post id, and the status Artemis reported.

    ``status`` is ``"pending"`` for a fresh upload (derivatives not built yet) or ``"active"``
    when the md5 matched an existing post and Artemis deduped or restored it instead.
    """

    post_id: str
    status: str

    @property
    def deduplicated(self) -> bool:
        """True when this upload matched an existing post rather than creating one."""
        return self.status == "active"


def content_type_for(path: Path) -> str:
    """The MIME type to send for a downloaded file.

    The media class Artemis derives from this (``video/mp4`` → ``video``) is what decides
    which derivatives Hephaestus is asked for, so an unknown extension falling back to
    ``application/octet-stream`` is reported rather than guessed as video.
    """
    suffix = path.suffix.lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or _FALLBACK_CONTENT_TYPE


class ArtemisClient:
    """A thin, synchronous Artemis client — four calls, no abstraction over them."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: httpx.Timeout | float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers
        )

    def upload(self, path: Path, *, content_type: str | None = None) -> UploadedPost:
        """Stream a file to ``POST /uploads`` and return the post it landed on.

        The file is streamed from disk rather than read into memory — Echo's side of a
        multi-gigabyte upload should not need the file's size in RAM. (Artemis's side still
        buffers it to compute the md5; see ``docs/DESIGN.md``.)

        ``mediaType`` is sent explicitly as well as implied by the ``Content-Type``, so a
        mistyped content type cannot make Artemis classify a video as ``application`` and
        quietly skip the video derivatives.
        """
        if not path.is_file():
            raise ArtemisError(f"{path}: not a file, nothing to upload")

        mime = content_type or content_type_for(path)
        media_type = mime.split("/", 1)[0] or "application"

        with path.open("rb") as handle:
            payload = self._request(
                "POST",
                "/uploads",
                params={"mediaType": media_type},
                content=handle,
                headers={"Content-Type": mime},
            )

        post_id = payload.get("postId")
        if not isinstance(post_id, str) or not post_id:
            raise ArtemisError(f"upload of {path.name} returned no postId")
        status = payload.get("status")
        return UploadedPost(post_id=post_id, status=status if isinstance(status, str) else "")

    def replace_tags(self, post_id: str, tags: Iterable[str]) -> None:
        """Set a post's **complete** tag set.

        Named ``replace_`` rather than ``add_`` on purpose: the route is a full-set replace, so
        a caller that sends a partial set silently deletes the rest.
        """
        self._request("PATCH", f"/posts/{post_id}/tags", json={"tags": list(tags)})

    def ensure_pool(self, pool_id: str, name: str) -> None:
        """Create a pool, treating "already exists" as success.

        Echo re-creates a channel's pool on every ingest rather than tracking whether it did
        so: ``409 Conflict`` is the expected steady-state answer and costs one request.
        """
        try:
            self._request("POST", "/pools", json={"id": pool_id, "name": name})
        except ArtemisError as exc:
            if exc.status != httpx.codes.CONFLICT:
                raise

    def add_to_pool(self, pool_id: str, post_id: str) -> None:
        """Append a post to a pool. A post already in the pool is an idempotent no-op."""
        self._request("POST", f"/pools/{pool_id}/posts", json={"postId": post_id})

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ArtemisClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ArtemisError(f"{method} {path} did not reach Artemis: {exc}") from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ArtemisError(
                f"{method} {path} returned HTTP {response.status_code}: {_error_message(response)}",
                status=response.status_code,
            )

        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}


def _error_message(response: httpx.Response) -> str:
    """Artemis answers errors as ``{"error": "..."}``; fall back to the raw text."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str):
            return error
    return response.text[:200]
