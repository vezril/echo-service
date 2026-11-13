"""Echo's own state: what it has seen, resolved, downloaded and posted.

The store is the memory the pipeline needs and nothing more. Every method is named for the
question the worker actually asks — ``seen_video_ids``, ``is_first_sync``,
``due_for_fetch`` — so the poll loop reads as the decision sequence it is, and the SQL stays
in one file.

Deliberately **not** a general ORM-shaped thing: the rows are small, the writer is single, and
the queries are the five the pipeline runs. Writes are committed per call, because a worker
that dies mid-cycle must not lose the fact that it already downloaded a 2 GiB file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Self

from echo.fetch.download import FetchOutcome, FetchReport
from echo.follow.config import Channel
from echo.follow.feed import FeedEntry
from echo.state.schema import (
    FETCH_STATES,
    INGEST_STATES,
    RETRY_BACKOFF,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    SEEN_STATES,
    VideoState,
)

__all__ = [
    "EchoStore",
    "FeedValidators",
    "VideoRecord",
    "next_attempt_delay",
    "open_store",
]

#: Keeps the file readable by other tools and survives an unclean shutdown better than the
#: rollback journal; a poll loop writing a few rows every 30 minutes is its ideal case.
_PRAGMAS: Final = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
)


def next_attempt_delay(attempts: int) -> timedelta:
    """How long to wait before attempt number ``attempts + 1``, capped at the last step."""
    if attempts < 1:
        return RETRY_BACKOFF[0]
    return RETRY_BACKOFF[min(attempts, len(RETRY_BACKOFF)) - 1]


@dataclass(frozen=True, slots=True)
class FeedValidators:
    """Conditional-GET validators for one channel's feed."""

    etag: str | None = None
    last_modified: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.etag or self.last_modified)


@dataclass(frozen=True, slots=True)
class VideoRecord:
    """One row of ``videos`` — what ``echo-ui`` displays and what ``ingest/`` consumes."""

    video_id: str
    channel_id: str
    title: str
    url: str
    state: VideoState
    uploader: str | None = None
    published_at: datetime | None = None
    reason: str | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    file_path: Path | None = None
    filesize: int | None = None
    duration: timedelta | None = None
    post_id: str | None = None
    first_seen_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def source(self) -> str:
        """The value stamped on the Artemis post's ``source`` — the bare video id."""
        return self.video_id


class EchoStore:
    """SQLite-backed state for one Echo instance.

    Not thread-safe by design: Echo is a single worker, and a lock around a store that only
    one loop touches would be ceremony. Use :func:`open_store` to get a migrated instance.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    # --- lifecycle --------------------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open (creating if needed) a store at ``path``. ``:memory:`` works for tests."""
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target)
        store = cls(connection)
        store.migrate()
        return store

    def migrate(self) -> None:
        """Apply the schema. Idempotent, so starting an existing deployment is a no-op."""
        for pragma in _PRAGMAS:
            self._connection.execute(pragma)
        with self._transaction() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
            cursor.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")

    @property
    def schema_version(self) -> int:
        row = self._connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """One committed unit of work. Rolls back on any exception."""
        with closing(self._connection.cursor()) as cursor:
            try:
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            self._connection.commit()

    # --- channels ---------------------------------------------------------------------

    def upsert_channel(self, channel: Channel, *, channel_id: str | None = None) -> str:
        """Record a followed channel, returning its ``channel_id``.

        ``channel_id`` overrides the one on ``channel``, which is how a handle resolved by
        :mod:`echo.follow.resolve` gets recorded. Re-running with the same channel updates
        the human-facing fields and leaves sync state untouched — a renamed channel in the
        config must not reset its history.
        """
        resolved = channel_id or channel.channel_id
        if not resolved:
            raise ValueError(f"{channel.label}: needs a resolved channel_id before recording")

        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO channels (channel_id, handle, url, name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (channel_id) DO UPDATE SET
                    handle = COALESCE(excluded.handle, channels.handle),
                    url    = COALESCE(excluded.url, channels.url),
                    name   = COALESCE(excluded.name, channels.name)
                """,
                (resolved, channel.handle, channel.url, channel.name),
            )
        return resolved

    def resolved_channel_id(
        self, *, handle: str | None = None, url: str | None = None
    ) -> str | None:
        """The ``channel_id`` already recorded for a handle or URL, if any.

        This is what makes resolution happen once per channel rather than once per poll, and
        what makes a handle rename harmless: the id, not the handle, is the identity.
        """
        if handle:
            row = self._connection.execute(
                "SELECT channel_id FROM channels WHERE handle = ? COLLATE NOCASE", (handle,)
            ).fetchone()
        elif url:
            row = self._connection.execute(
                "SELECT channel_id FROM channels WHERE url = ? COLLATE NOCASE", (url,)
            ).fetchone()
        else:
            raise ValueError("give a handle or a url")
        return str(row["channel_id"]) if row else None

    def is_first_sync(self, channel_id: str) -> bool:
        """True until a poll of this channel has completed.

        Drives ``backfill``: it must apply to the first poll only, and must not re-trigger
        when the worker restarts.
        """
        row = self._connection.execute(
            "SELECT first_synced_at FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row is None or row["first_synced_at"] is None

    def mark_polled(
        self,
        channel_id: str,
        *,
        validators: FeedValidators | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record that a poll completed, closing the first-sync window."""
        moment = _iso(at or _now())
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE channels
                   SET last_polled_at  = ?,
                       first_synced_at = COALESCE(first_synced_at, ?),
                       etag            = COALESCE(?, etag),
                       last_modified   = COALESCE(?, last_modified)
                 WHERE channel_id = ?
                """,
                (
                    moment,
                    moment,
                    validators.etag if validators else None,
                    validators.last_modified if validators else None,
                    channel_id,
                ),
            )

    def feed_validators(self, channel_id: str) -> FeedValidators:
        """The stored validators for a channel's next conditional request."""
        row = self._connection.execute(
            "SELECT etag, last_modified FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return FeedValidators()
        return FeedValidators(etag=row["etag"], last_modified=row["last_modified"])

    # --- videos -----------------------------------------------------------------------

    def seen_video_ids(self, channel_id: str) -> frozenset[str]:
        """The ids :func:`echo.follow.detect.select_new` should treat as already handled.

        Excludes ``deferred`` and ``failed`` on purpose: those are owed another attempt, and
        including them would strand a livestream's finished recording forever.
        """
        placeholders = ", ".join("?" * len(SEEN_STATES))
        rows = self._connection.execute(
            f"SELECT video_id FROM videos WHERE channel_id = ? AND state IN ({placeholders})",
            (channel_id, *sorted(state.value for state in SEEN_STATES)),
        ).fetchall()
        return frozenset(str(row["video_id"]) for row in rows)

    def record_entries(
        self,
        channel_id: str,
        entries: Iterable[FeedEntry],
        *,
        state: VideoState = VideoState.PENDING,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> int:
        """Record feed entries, returning how many rows were inserted.

        The **verbatim** title is stored here, straight off the feed: it is the only copy
        that will exist once the post is in Artemis.

        An existing row is left alone apart from its title and URL. Re-recording must not
        reset ``attempts`` or walk a video backwards from ``downloaded`` to ``pending``.
        """
        moment = _iso(at or _now())
        rows = [
            (
                entry.video_id,
                channel_id,
                entry.title,
                entry.url,
                entry.uploader,
                _iso(entry.published),
                state.value,
                reason,
                moment,
                moment,
            )
            for entry in entries
        ]
        if not rows:
            return 0

        with self._transaction() as cursor:
            before = self._count()
            cursor.executemany(
                """
                INSERT INTO videos (
                    video_id, channel_id, title, url, uploader, published_at,
                    state, reason, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (video_id) DO UPDATE SET
                    title      = excluded.title,
                    url        = excluded.url,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            inserted = self._count() - before
        return inserted

    def record_report(self, report: FetchReport, *, at: datetime | None = None) -> VideoState:
        """Apply a :class:`~echo.fetch.FetchReport`, returning the state it landed in.

        The mapping is the whole point of the report's ``retryable`` flag:

        * downloaded → ``downloaded``, with the file facts, retry counter cleared;
        * filtered → ``filtered``, terminal, so it never costs bandwidth again;
        * deferred / failed → the same state, ``attempts`` incremented and
          ``next_attempt_at`` pushed out by :func:`next_attempt_delay`.
        """
        moment = at or _now()
        state = _STATE_FOR_OUTCOME[report.outcome]
        media = report.media

        if state is VideoState.DOWNLOADED and media is None:
            raise ValueError(f"{report.video_id}: a downloaded report must carry its media")

        if report.retryable:
            # The delay depends on the *new* attempt count, so it is derived from the stored
            # one rather than from anything the caller passes in.
            retry_at = moment + next_attempt_delay(self._attempts(report.video_id) + 1)
            self._update_video(
                report.video_id,
                """
                UPDATE videos
                   SET state           = ?,
                       reason          = ?,
                       attempts        = attempts + 1,
                       next_attempt_at = ?,
                       updated_at      = ?
                 WHERE video_id = ?
                """,
                (state.value, report.reason, _iso(retry_at), _iso(moment), report.video_id),
            )
            return state

        self._update_video(
            report.video_id,
            """
            UPDATE videos
               SET state            = ?,
                   reason           = ?,
                   next_attempt_at  = NULL,
                   file_path        = COALESCE(?, file_path),
                   filesize         = COALESCE(?, filesize),
                   duration_seconds = COALESCE(?, duration_seconds),
                   title            = COALESCE(?, title),
                   updated_at       = ?
             WHERE video_id = ?
            """,
            (
                state.value,
                report.reason,
                str(media.path) if media else None,
                media.filesize if media else None,
                _seconds(media.duration) if media else None,
                # yt-dlp's title is better than the feed's when they differ, and still
                # verbatim. Only overwritten when a download actually produced one.
                media.title if media else None,
                _iso(moment),
                report.video_id,
            ),
        )
        return state

    def mark_ingested(self, video_id: str, *, post_id: str, at: datetime | None = None) -> None:
        """Record the Artemis post id — the terminal success for a video."""
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE videos
                   SET state = ?, post_id = ?, reason = NULL, next_attempt_at = NULL,
                       updated_at = ?
                 WHERE video_id = ?
                """,
                (VideoState.INGESTED.value, post_id, _iso(at or _now()), video_id),
            )
            if cursor.rowcount == 0:
                raise LookupError(f"{video_id}: not in the store")

    def due_for_fetch(
        self,
        *,
        channel_id: str | None = None,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[VideoRecord, ...]:
        """Videos owed a download attempt, oldest-published first.

        A row whose ``next_attempt_at`` is still in the future is held back — that is the
        backoff actually taking effect.
        """
        return self._queue(FETCH_STATES, channel_id=channel_id, now=now, limit=limit)

    def due_for_ingest(
        self, *, channel_id: str | None = None, limit: int | None = None
    ) -> tuple[VideoRecord, ...]:
        """Downloaded videos that still owe an Artemis post."""
        return self._queue(INGEST_STATES, channel_id=channel_id, now=None, limit=limit)

    def video(self, video_id: str) -> VideoRecord | None:
        """One video by id — which is also its ``source`` on the Artemis post.

        This is the post → Echo direction of the join that keeps the verbatim title
        reachable (``docs/DESIGN.md``).
        """
        row = self._connection.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return _record(row) if row else None

    def videos_for_channel(
        self, channel_id: str, *, states: Collection[VideoState] | None = None
    ) -> tuple[VideoRecord, ...]:
        """Every recorded video for a channel, newest-published first — ``echo-ui``'s list."""
        if states:
            placeholders = ", ".join("?" * len(states))
            rows = self._connection.execute(
                f"""
                SELECT * FROM videos
                 WHERE channel_id = ? AND state IN ({placeholders})
                 ORDER BY published_at DESC, video_id DESC
                """,
                (channel_id, *sorted(state.value for state in states)),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM videos WHERE channel_id = ?
                 ORDER BY published_at DESC, video_id DESC
                """,
                (channel_id,),
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def counts_by_state(self, *, channel_id: str | None = None) -> dict[VideoState, int]:
        """Row counts per state — the queue summary ``echo-ui`` shows.

        States with no rows are present with a zero, so a caller can render a stable set of
        counters without special-casing an empty store.
        """
        if channel_id:
            rows = self._connection.execute(
                "SELECT state, COUNT(*) AS n FROM videos WHERE channel_id = ? GROUP BY state",
                (channel_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT state, COUNT(*) AS n FROM videos GROUP BY state"
            ).fetchall()
        counts = dict.fromkeys(VideoState, 0)
        for row in rows:
            counts[VideoState(row["state"])] = int(row["n"])
        return counts

    # --- internals --------------------------------------------------------------------

    def _queue(
        self,
        states: Collection[VideoState],
        *,
        channel_id: str | None,
        now: datetime | None,
        limit: int | None,
    ) -> tuple[VideoRecord, ...]:
        clauses = [f"state IN ({', '.join('?' * len(states))})"]
        params: list[Any] = sorted(state.value for state in states)

        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if now is not None:
            clauses.append("(next_attempt_at IS NULL OR next_attempt_at <= ?)")
            params.append(_iso(now))

        sql = f"""
            SELECT * FROM videos
             WHERE {" AND ".join(clauses)}
             ORDER BY published_at ASC, video_id ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self._connection.execute(sql, params).fetchall()
        return tuple(_record(row) for row in rows)

    def _update_video(self, video_id: str, sql: str, params: tuple[Any, ...]) -> None:
        """Run an update that must match exactly one video.

        A report for a video nobody recorded is a caller bug, not a no-op: silently updating
        zero rows would lose the outcome of a download that really happened.
        """
        with self._transaction() as cursor:
            cursor.execute(sql, params)
            if cursor.rowcount == 0:
                raise LookupError(
                    f"{video_id}: not in the store — record the feed entry before its outcome"
                )

    def _attempts(self, video_id: str) -> int:
        row = self._connection.execute(
            "SELECT attempts FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def _count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM videos").fetchone()
        return int(row["n"])


@contextmanager
def open_store(path: str | Path) -> Iterator[EchoStore]:
    """Open a migrated store and close it afterwards."""
    store = EchoStore.open(path)
    try:
        yield store
    finally:
        store.close()


_STATE_FOR_OUTCOME: Final[dict[FetchOutcome, VideoState]] = {
    FetchOutcome.DOWNLOADED: VideoState.DOWNLOADED,
    FetchOutcome.FILTERED: VideoState.FILTERED,
    FetchOutcome.DEFERRED: VideoState.DEFERRED,
    FetchOutcome.FAILED: VideoState.FAILED,
}


def _record(row: sqlite3.Row) -> VideoRecord:
    duration = row["duration_seconds"]
    return VideoRecord(
        video_id=str(row["video_id"]),
        channel_id=str(row["channel_id"]),
        title=str(row["title"]),
        url=str(row["url"]),
        state=VideoState(row["state"]),
        uploader=row["uploader"],
        published_at=_parse(row["published_at"]),
        reason=row["reason"],
        attempts=int(row["attempts"]),
        next_attempt_at=_parse(row["next_attempt_at"]),
        file_path=Path(row["file_path"]) if row["file_path"] else None,
        filesize=row["filesize"],
        duration=timedelta(seconds=float(duration)) if duration is not None else None,
        post_id=row["post_id"],
        first_seen_at=_parse(row["first_seen_at"]),
        updated_at=_parse(row["updated_at"]),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    """Store UTC, always: a naive timestamp would compare wrong against a stale window."""
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds(value: timedelta | None) -> float | None:
    return value.total_seconds() if value is not None else None
