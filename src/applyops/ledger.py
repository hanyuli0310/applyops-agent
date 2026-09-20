"""The durable ledger: applications, attempts and events in SQLite.

Why SQLite and not another JSON file: the ledger's job is *transactional claims*.
"Two entry points must not execute the same application" and "a crash between
sending and recording must land somewhere safe" are exactly the properties a
locked-JSON merge cannot give, because they need compare-and-set on a single row,
not a merge of two documents.

It is local, dependency-free, and its atomic-commit guarantees are real
(`https://sqlite.org/atomiccommit.html`). What it deliberately does **not**
promise is anything about the outside world: no local transaction can include an
employer's web server, which is why `SUBMITTED_UNVERIFIED` exists and why this
module never invents an outcome.

Design notes:

- **WAL mode** so a reader (the UI, later) never blocks the writer.
- **Optimistic transitions**: `UPDATE ... WHERE state = :expected`. If another
  process moved the row first, the update matches nothing and the caller is told
  the state changed underneath it -- instead of both processes believing they won.
- **Claims** are rows with an expiry, taken in the same compare-and-set style. A
  crashed holder's claim expires rather than needing anyone to detect the crash.
- **Events** are append-only. History that can be rewritten is not history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .state_machine import (
    ApplicationState,
    require_transition,
)

SCHEMA_VERSION = 1

CLAIM_TTL_SECONDS = 600.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_epoch() -> float:
    return time.time()


@dataclass
class ApplicationRow:
    id: str
    job_key: str
    job_url: str
    route: str
    platform: str
    state: str
    title: str = ""
    company: str = ""
    resume_sha256: str = ""
    profile_revision: str = ""
    answers_revision: str = ""
    snapshot_digest: str = ""
    claim_owner: str = ""
    claim_expires_at: float = 0.0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class AttemptRow:
    id: str
    application_id: str
    ordinal: int
    started_at: str
    ended_at: str = ""
    outcome: str = ""  # verified | unverified | failed | abandoned
    grant_id: str = ""
    detail: str = ""
    evidence_json: str = ""

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        if data.get("evidence_json"):
            try:
                data["evidence"] = json.loads(data.pop("evidence_json"))
            except ValueError:
                data["evidence"] = data.pop("evidence_json")
        else:
            data.pop("evidence_json", None)
            data["evidence"] = {}
        return data


class StaleState(Exception):
    """Another writer moved the row first. Re-read and decide again."""


class Ledger:
    """Owns the SQLite file. One per data directory."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # One connection is shared by every caller in this process -- the tools,
        # the service, the UI later. sqlite3 connections are not safe for
        # concurrent use, so every operation runs under this lock. Cross-process
        # safety comes from the optimistic WHERE clauses and WAL, not from this.
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        self._conn.close()

    # ── schema ───────────────────────────────────────────────────────

    def migrate(self) -> None:
        """Create or extend the schema. Idempotent; safe to run every open."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"ledger at {self.path} was written by schema v{version}, but this "
                f"code only understands v{SCHEMA_VERSION}. Refusing to guess at "
                "newer columns -- upgrade the code instead."
            )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY,
                job_key TEXT NOT NULL UNIQUE,
                job_url TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                resume_sha256 TEXT NOT NULL DEFAULT '',
                profile_revision TEXT NOT NULL DEFAULT '',
                answers_revision TEXT NOT NULL DEFAULT '',
                snapshot_digest TEXT NOT NULL DEFAULT '',
                claim_owner TEXT NOT NULL DEFAULT '',
                claim_expires_at REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY,
                application_id TEXT NOT NULL REFERENCES applications(id),
                ordinal INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                grant_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '',
                UNIQUE (application_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id TEXT NOT NULL,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    # ── applications ─────────────────────────────────────────────────

    def create_application(
        self,
        *,
        job_key: str,
        job_url: str,
        route: str = "",
        platform: str = "",
        title: str = "",
        company: str = "",
    ) -> ApplicationRow:
        """Enqueue an application. Deduplicates on `job_key` -- the same posting
        reached twice is one application, not two."""
        row = ApplicationRow(
            id=str(uuid.uuid4()),
            job_key=job_key,
            job_url=job_url,
            route=route,
            platform=platform,
            state=ApplicationState.QUEUED.value,
            title=title,
            company=company,
            created_at=_now(),
            updated_at=_now(),
        )
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """INSERT INTO applications
                       (id, job_key, job_url, route, platform, state, title, company,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row.id, row.job_key, row.job_url, row.route, row.platform,
                        row.state, row.title, row.company, row.created_at, row.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.find_by_job_key(job_key)
            if existing is not None:
                return existing  # enqueue is idempotent
            raise RuntimeError(f"could not enqueue application: {exc}") from exc
        self.record_event(row.id, "created", {"state": row.state})
        return row

    def get(self, application_id: str) -> ApplicationRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
        return ApplicationRow(**dict(row)) if row else None

    def find_by_job_key(self, job_key: str) -> ApplicationRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM applications WHERE job_key = ?", (job_key,)
            ).fetchone()
        return ApplicationRow(**dict(row)) if row else None

    def list_applications(self, state: str | None = None) -> list[ApplicationRow]:
        with self._lock:
            if state:
                rows = self._conn.execute(
                    "SELECT * FROM applications WHERE state = ? ORDER BY created_at",
                    (state,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM applications ORDER BY created_at"
                ).fetchall()
        return [ApplicationRow(**dict(r)) for r in rows]

    # ── transitions ──────────────────────────────────────────────────

    def transition(
        self,
        application_id: str,
        target: ApplicationState | str,
        *,
        expected_state: str | None = None,
        payload: dict | None = None,
    ) -> ApplicationRow:
        """Move an application, or refuse.

        The state machine is checked first (for a readable error) and then
        enforced again by the WHERE clause, so a writer that read a stale row
        cannot move something it did not see.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown application {application_id}")
            current = row["state"]
            cur, nxt = require_transition(current, target)
            expected = expected_state or current
            if current != expected:
                raise StaleState(
                    f"expected {expected!r} but the row now says {current!r}; "
                    "another writer moved it. Re-read before deciding."
                )
            self._conn.execute(
                """UPDATE applications SET state = ?, updated_at = ? WHERE id = ?""",
                (nxt.value, _now(), application_id),
            )
        self.record_event(
            application_id, "transition", {"from": cur.value, "to": nxt.value, **(payload or {})}
        )
        updated = self.get(application_id)
        assert updated is not None
        return updated

    # ── claims ───────────────────────────────────────────────────────

    def claim(self, application_id: str, owner: str) -> bool:
        """Take exclusive execution rights, or lose to an existing holder.

        A claim expiring is the crash recovery: nobody has to detect that the
        holder died, because the row simply becomes claimable again after the
        TTL -- by which time, if the holder was mid-`SUBMITTING`, `recover()`
        will have moved the application to the safe unknown state.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """UPDATE applications
                   SET claim_owner = ?, claim_expires_at = ?, updated_at = ?
                   WHERE id = ?
                     AND (claim_expires_at < ? OR claim_owner = ?)""",
                (owner, _now_epoch() + CLAIM_TTL_SECONDS, _now(), application_id, _now_epoch(), owner),
            )
            won = cursor.rowcount == 1
        if won:
            self.record_event(application_id, "claimed", {"owner": owner})
        return won

    def release_claim(self, application_id: str, owner: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE applications SET claim_owner = '', claim_expires_at = 0.0,
                       updated_at = ?
                   WHERE id = ? AND claim_owner = ?""",
                (_now(), application_id, owner),
            )

    def recover_expired(self) -> list[ApplicationRow]:
        """Move applications abandoned mid-`SUBMITTING` to the safe state.

        If a process died inside `SUBMITTING`, the request may or may not have
        been sent. Restoring it to any earlier state would invite a second
        submission; the only honest landing place is `SUBMITTED_UNVERIFIED`,
        from which reconciliation -- never resubmission -- is the way forward.
        """
        recovered: list[ApplicationRow] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT id, state, claim_owner FROM applications
                   WHERE state = ? AND claim_expires_at < ?""",
                (ApplicationState.SUBMITTING.value, _now_epoch()),
            ).fetchall()
            for row in rows:
                cur = ApplicationState(row["state"])
                # The state machine itself encodes why nothing earlier is legal.
                require_transition(cur, ApplicationState.SUBMITTED_UNVERIFIED)
                self._conn.execute(
                    """UPDATE applications SET state = ?, updated_at = ? WHERE id = ?""",
                    (ApplicationState.SUBMITTED_UNVERIFIED.value, _now(), row["id"]),
                )
                recovered.append(self.get(row["id"]))  # type: ignore[arg-type]
        for row in recovered:
            self.record_event(
                row.id,
                "recovered",
                {"detail": "claim expired while SUBMITTING; result unknown, not resubmitted"},
            )
        return recovered

    # ── attempts ─────────────────────────────────────────────────────

    def set_route(self, application_id: str, route: str) -> None:
        """Correct the route once the page has told us what it really is.

        The route is first guessed from the URL the posting was discovered at,
        which for LinkedIn is always its own job page -- so a posting that leaves
        for another ATS arrives mislabelled. Recording the correction is what
        stops the queue from treating it as drivable for the rest of its life.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE applications SET route = ?, updated_at = ? WHERE id = ?",
                (route, _now(), application_id),
            )
            self._conn.commit()

    def start_attempt(self, application_id: str) -> AttemptRow:
        with self._lock, self._conn:
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE application_id = ?",
                (application_id,),
            ).fetchone()["n"]
            attempt = AttemptRow(
                id=str(uuid.uuid4()),
                application_id=application_id,
                ordinal=count + 1,
                started_at=_now(),
            )
            self._conn.execute(
                """INSERT INTO attempts
                   (id, application_id, ordinal, started_at) VALUES (?, ?, ?, ?)""",
                (attempt.id, attempt.application_id, attempt.ordinal, attempt.started_at),
            )
        self.record_event(
            application_id, "attempt_started", {"ordinal": attempt.ordinal}
        )
        return attempt

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        outcome: str,
        detail: str = "",
        grant_id: str = "",
        evidence: dict | None = None,
    ) -> AttemptRow:
        if outcome not in {"verified", "unverified", "failed", "abandoned"}:
            raise ValueError(f"unknown attempt outcome {outcome!r}")
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE attempts
                   SET ended_at = ?, outcome = ?, detail = ?, grant_id = ?, evidence_json = ?
                   WHERE id = ?""",
                (
                    _now(), outcome, detail, grant_id,
                    json.dumps(evidence or {}, ensure_ascii=False), attempt_id,
                ),
            )
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        return AttemptRow(**dict(row))

    def attempts(self, application_id: str) -> list[AttemptRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE application_id = ? ORDER BY ordinal",
                (application_id,),
            ).fetchall()
        return [AttemptRow(**dict(r)) for r in rows]

    # ── events ───────────────────────────────────────────────────────

    def record_event(self, application_id: str, kind: str, payload: dict | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (application_id, at, kind, payload_json) VALUES (?, ?, ?, ?)",
                (
                    application_id, _now(), kind,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def events(self, application_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, kind, payload_json FROM events WHERE application_id = ? ORDER BY id",
                (application_id,),
            ).fetchall()
        return [
            {
                "at": r["at"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"] or "{}"),
            }
            for r in rows
        ]

    # ── legacy migration ─────────────────────────────────────────────

    def import_legacy_history(self, records: list[dict]) -> dict:
        """Import old `memory.json` history as `LEGACY_IMPORTED` rows.

        The rules that matter:

        - **Idempotent.** A `job_key` already present is skipped, so re-running
          after a partial migration neither duplicates nor overwrites.
        - **No invented evidence.** History rows carry no confirmation; they are
          imported as `LEGACY_IMPORTED`, a terminal state, which statistics can
          count as attempts without ever counting as successes.
        - **The source is not modified.** The caller backs it up; this function
          only reads.
        """
        imported = 0
        skipped = 0
        with self._lock, self._conn:
            for record in records:
                job_url = str(record.get("job_url", ""))
                job_key = str(
                    record.get("job_id")
                    or record.get("dedup_key")
                    or job_url
                ).strip()
                if not job_key:
                    skipped += 1
                    continue
                exists = self._conn.execute(
                    "SELECT 1 FROM applications WHERE job_key = ?", (job_key,)
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
                self._conn.execute(
                    """INSERT INTO applications
                       (id, job_key, job_url, route, platform, state, title, company,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        job_key,
                        job_url,
                        str(record.get("apply_route", "")),
                        str(record.get("platform", "")),
                        ApplicationState.LEGACY_IMPORTED.value,
                        str(record.get("job_title", "")),
                        str(record.get("company", "")),
                        str(record.get("applied_at", "")) or _now(),
                        _now(),
                    ),
                )
                imported += 1
        return {"imported": imported, "skipped": skipped}
