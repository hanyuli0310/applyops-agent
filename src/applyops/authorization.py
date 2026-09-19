"""Submission grants: the authorization boundary around a final external submit.

Why this exists
---------------
The previous approval (``request_submit_confirmation``) binds *almost* nothing.
It stores a free-text summary, the job key and an expiry, and it gates nothing:
the actual click on Submit goes through the generic ``click_target`` tool, so the
token is checked *after* the external side effect has already happened. It is a
receipt, not a lock.

A grant binds the approval to what will actually be sent, and it is checked
*before* the external action:

| bound to | why it must be part of the binding |
|---|---|
| `job_key` | a token minted for posting A must not authorize posting B |
| `field_snapshot` | the values the page **currently reports**, read back from the DOM |
| `resume_sha256` | approving a run with resume version 1 must not send version 2 |
| `answers_revision` / `profile_revision` | a fact changed after approval changes what is sent |
| `route` | the set of steps that produce the final action differs per route |
| `expires_at` | a stale approval cannot be replayed tomorrow |
| one-time use | approval for one submission is not approval for the next |

The snapshot is taken from the browser, never from the harness's prose summary.
A model writing "all fields look fine" is not evidence about the DOM; letting
that sentence stand in for the values is how a *summary* gets approved while the
form still holds something else.

`consume` is executed inside one file lock, so two processes racing on the same
grant cannot both see it as unused. That is the same argument as elsewhere in
this project: the dangerous window is not worth closing with a pid check.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .concurrency import FileLock, atomic_write_json, data_lock_path, read_json

DEFAULT_TTL_SECONDS = 900  # 15 min: long enough to read a form, short enough to go stale.
REQUEST_TTL_SECONDS = 1800  # a request waits for a person; the grant itself is shorter


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def snapshot_digest(
    *,
    fields: dict[str, str],
    resume_sha256: str,
    answers_revision: str,
    profile_revision: str,
    route: str,
) -> str:
    """A content address for "exactly what will be submitted".

    Canonically serialised so that dict ordering -- which comes from whichever
    order the page happened to render its fields in -- cannot make two identical
    forms look different and thereby invalidate a legitimate approval.
    """
    payload = json.dumps(
        {
            "fields": dict(sorted(fields.items())),
            "resume_sha256": resume_sha256,
            "answers_revision": answers_revision,
            "profile_revision": profile_revision,
            "route": route,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class SubmissionRequest:
    """A *request* for permission. Approving requires someone else.

    Why there are two objects instead of one: a grant is the authority to send
    something, and it must be impossible for whoever benefits from that authority
    to create it. The caller that wants to submit can only open a request; a
    human closes it, from outside that caller, with `applyops approve`. The whole
    check is worthless if the process asking "may I?" is also the one answering
    "you may" -- which is exactly what the old `acknowledged=True` flag allowed.
    """

    request_id: str
    job_key: str
    job_url: str
    route: str
    platform: str
    fields: dict[str, str] = field(default_factory=dict)
    resume_filename: str = ""
    resume_sha256: str = ""
    answers_revision: str = ""
    profile_revision: str = ""
    created_at: str = ""
    expires_at_epoch: float = 0.0
    requested_by: str = "mcp"  # who is asking; never also the approver
    status: str = "pending"  # pending | approved | rejected
    grant_id: str = ""
    decided_at: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at_epoch

    @property
    def live(self) -> bool:
        return self.status == "pending" and not self.expired

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "job_key": self.job_key,
            "job_url": self.job_url,
            "route": self.route,
            "platform": self.platform,
            "fields": dict(self.fields),
            "resume_filename": self.resume_filename,
            "resume_sha256": self.resume_sha256,
            "answers_revision": self.answers_revision,
            "profile_revision": self.profile_revision,
            "created_at": self.created_at,
            "expires_at_epoch": self.expires_at_epoch,
            "requested_by": self.requested_by,
            "status": self.status,
            "grant_id": self.grant_id,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> SubmissionRequest:
        known = {k: v for k, v in payload.items() if k in cls.__annotations__}
        return cls(**known)

    def summary_for_human(self) -> str:
        """Exactly what a person must read before granting anything.

        Generated from the stored request, not from whatever narrative the
        requesting process wrote: the values here are the ones that were read
        back off the page, which is the only thing anyone can meaningfully
        approve.
        """
        lines = [
            "Review this submission -- it will be sent to the employer:",
            f"  job:   {self.job_url or self.job_key}  (key {self.job_key})",
            f"  route: {self.route or 'unknown'}",
        ]
        if self.fields:
            lines.append("  fields as read back from the form:")
            for label, value in sorted(self.fields.items()):
                lines.append(f"    - {label}: {value}")
        else:
            lines.append("  fields: NONE OBSERVED -- this form read as empty")
        if self.resume_filename:
            lines.append(f"  resume: {self.resume_filename} (sha256:{self.resume_sha256[:12]}…)")
        else:
            lines.append("  resume: NONE -- no file will be attached")
        return "\n".join(lines)


@dataclass
class SubmissionGrant:
    """A one-time authorization to perform exactly one final submit."""

    grant_id: str
    job_key: str
    job_url: str
    route: str
    platform: str
    fields: dict[str, str] = field(default_factory=dict)
    resume_filename: str = ""
    resume_sha256: str = ""
    answers_revision: str = ""
    profile_revision: str = ""
    snapshot_digest: str = ""
    created_at: str = ""
    expires_at_epoch: float = 0.0
    source: str = "unspecified"  # who or what produced this: "local_ui" | "cli" | ...
    used_at: str = ""
    revoked: bool = False
    detail: str = ""

    @property
    def used(self) -> bool:
        return bool(self.used_at)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at_epoch

    def to_dict(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "job_key": self.job_key,
            "job_url": self.job_url,
            "route": self.route,
            "platform": self.platform,
            "fields": dict(self.fields),
            "resume_filename": self.resume_filename,
            "resume_sha256": self.resume_sha256,
            "answers_revision": self.answers_revision,
            "profile_revision": self.profile_revision,
            "snapshot_digest": self.snapshot_digest,
            "created_at": self.created_at,
            "expires_at_epoch": self.expires_at_epoch,
            "source": self.source,
            "used_at": self.used_at,
            "revoked": self.revoked,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> SubmissionGrant:
        known = {k: v for k, v in payload.items() if k in cls.__annotations__}
        return cls(**known)

    def summary_for_human(self) -> str:
        """What must be shown to a person *before* they grant anything."""
        lines = [
            f"Job: {self.job_url or self.job_key} (key: {self.job_key})",
            f"Route: {self.route or 'unknown'}",
        ]
        if self.fields:
            lines.append("Fields as read back from the page:")
            lines.extend(f"  - {label}: {value}" for label, value in sorted(self.fields.items()))
        else:
            lines.append("Fields: none observed -- not verified.")
        if self.resume_filename:
            lines.append(f"Resume: {self.resume_filename} (sha256:{self.resume_sha256[:12]}…)")
        else:
            lines.append("Resume: none attached.")
        return "\n".join(lines)


@dataclass(frozen=True)
class GrantVerdict:
    ok: bool
    reason: str = ""
    grant: SubmissionGrant | None = None


class SubmissionAuthorizer:
    """Issue, verify and consume grants. File-backed, locked, single-writer."""

    def __init__(self, data_dir: str | Path, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.data_dir = Path(data_dir)
        self.ttl_seconds = ttl_seconds
        self.path = self.data_dir / "submission_grants.json"
        self.requests_path = self.data_dir / "submission_requests.json"
        self._lock_path = data_lock_path(self.data_dir, "submission_grants")

    # ── storage ──────────────────────────────────────────────────────

    def _read_all(self) -> list[SubmissionGrant]:
        payload = read_json(self.path, default=[]) or []
        if isinstance(payload, dict):  # tolerate a future keyed shape
            payload = list(payload.values())
        return [SubmissionGrant.from_dict(item) for item in payload if isinstance(item, dict)]

    def _write_all(self, grants: list[SubmissionGrant]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, [g.to_dict() for g in grants])

    def _read_requests(self) -> list[SubmissionRequest]:
        payload = read_json(self.requests_path, default=[]) or []
        return [
            SubmissionRequest.from_dict(item) for item in payload if isinstance(item, dict)
        ]

    def _write_requests(self, requests: list[SubmissionRequest]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.requests_path, [r.to_dict() for r in requests])

    # ── requesting permission ────────────────────────────────────────

    def create_request(
        self,
        *,
        job_key: str,
        job_url: str,
        route: str,
        platform: str,
        fields: dict[str, str],
        resume_filename: str = "",
        resume_sha256: str = "",
        answers_revision: str = "",
        profile_revision: str = "",
        requested_by: str = "mcp",
    ) -> SubmissionRequest:
        """Open a request for permission. Grants nothing."""
        request = SubmissionRequest(
            request_id=str(uuid.uuid4()),
            job_key=job_key,
            job_url=job_url,
            route=route,
            platform=platform,
            fields=dict(fields),
            resume_filename=resume_filename,
            resume_sha256=resume_sha256,
            answers_revision=answers_revision,
            profile_revision=profile_revision,
            created_at=_now(),
            expires_at_epoch=time.time() + REQUEST_TTL_SECONDS,
            requested_by=requested_by,
        )
        with FileLock(self._lock_path):
            requests = self._read_requests()
            requests.append(request)
            self._write_requests(requests)
        return request

    def pending_requests(self) -> list[SubmissionRequest]:
        return [r for r in self._read_requests() if r.live]

    def get_request(self, request_id: str) -> SubmissionRequest | None:
        return next((r for r in self._read_requests() if r.request_id == request_id), None)

    def approve_request(self, request_id: str, *, source: str) -> SubmissionGrant | None:
        """Turn a human's decision into a grant. This is the authority step."""
        with FileLock(self._lock_path):
            requests = self._read_requests()
            request = next((r for r in requests if r.request_id == request_id), None)
            if request is None or not request.live:
                return None
            grant = SubmissionGrant(
                grant_id=str(uuid.uuid4()),
                job_key=request.job_key,
                job_url=request.job_url,
                route=request.route,
                platform=request.platform,
                fields=dict(request.fields),
                resume_filename=request.resume_filename,
                resume_sha256=request.resume_sha256,
                answers_revision=request.answers_revision,
                profile_revision=request.profile_revision,
                snapshot_digest=snapshot_digest(
                    fields=request.fields,
                    resume_sha256=request.resume_sha256,
                    answers_revision=request.answers_revision,
                    profile_revision=request.profile_revision,
                    route=request.route,
                ),
                created_at=_now(),
                expires_at_epoch=time.time() + self.ttl_seconds,
                source=source,
                detail=f"approved from request {request_id}",
            )
            request.status = "approved"
            request.grant_id = grant.grant_id
            request.decided_at = _now()
            self._write_requests(requests)

            grants = self._read_all()
            grants.append(grant)
            self._write_all(grants)
        return grant

    def reject_request(self, request_id: str) -> bool:
        with FileLock(self._lock_path):
            requests = self._read_requests()
            for request in requests:
                if request.request_id == request_id and request.live:
                    request.status = "rejected"
                    request.decided_at = _now()
                    self._write_requests(requests)
                    return True
        return False

    # ── issuing ──────────────────────────────────────────────────────

    def issue_grant(
        self,
        *,
        job_key: str,
        job_url: str,
        route: str,
        platform: str,
        fields: dict[str, str],
        resume_filename: str = "",
        resume_sha256: str = "",
        answers_revision: str = "",
        profile_revision: str = "",
        source: str = "unspecified",
        detail: str = "",
    ) -> SubmissionGrant:
        """Mint a grant carrying everything the decision was based on.

        `fields` must come from the browser's read-back of the live form. The
        caller that knows what the user approved supplies it; there is deliberately
        no way to issue a grant without naming the values.
        """
        grant = SubmissionGrant(
            grant_id=str(uuid.uuid4()),
            job_key=job_key,
            job_url=job_url,
            route=route,
            platform=platform,
            fields=dict(fields),
            resume_filename=resume_filename,
            resume_sha256=resume_sha256,
            answers_revision=answers_revision,
            profile_revision=profile_revision,
            snapshot_digest=snapshot_digest(
                fields=fields,
                resume_sha256=resume_sha256,
                answers_revision=answers_revision,
                profile_revision=profile_revision,
                route=route,
            ),
            created_at=_now(),
            expires_at_epoch=time.time() + self.ttl_seconds,
            source=source,
            detail=detail,
        )
        with FileLock(self._lock_path):
            grants = self._read_all()
            grants.append(grant)
            self._write_all(grants)
        return grant

    def peek(self, grant_id: str) -> SubmissionGrant | None:
        return next((g for g in self._read_all() if g.grant_id == grant_id), None)

    # ── verification ─────────────────────────────────────────────────

    def verify(
        self,
        grant_id: str,
        *,
        job_key: str,
        fields: dict[str, str],
        resume_sha256: str,
        answers_revision: str,
        profile_revision: str,
        route: str,
    ) -> GrantVerdict:
        """Does this grant authorize submitting *what the page holds right now*?

        The critical part is that `fields` here is re-read at execution time and
        compared by digest against what was approved. A form that changed after
        approval -- a field edited, a different resume selected, a fact updated in
        the profile -- produces a different digest and the grant stops applying,
        which is the only honest answer to "the user approved something, but not
        this".
        """
        grant = self.peek(grant_id)
        if grant is None:
            return GrantVerdict(False, "unknown grant id")
        if grant.revoked:
            return GrantVerdict(False, "grant was revoked", grant)
        if grant.used:
            return GrantVerdict(False, "grant already used; approvals are one-time", grant)
        if grant.expired:
            return GrantVerdict(False, "grant expired; ask for approval again", grant)
        if grant.job_key != job_key:
            return GrantVerdict(
                False,
                f"grant was issued for job {grant.job_key!r}, not {job_key!r}",
                grant,
            )

        observed = snapshot_digest(
            fields=fields,
            resume_sha256=resume_sha256,
            answers_revision=answers_revision,
            profile_revision=profile_revision,
            route=route,
        )
        if observed != grant.snapshot_digest:
            return GrantVerdict(
                False,
                (
                    "the application no longer matches what was approved "
                    "(fields, resume, profile or answers changed) -- re-review and "
                    "issue a new grant"
                ),
                grant,
            )
        return GrantVerdict(True, "", grant)

    # ── consumption ──────────────────────────────────────────────────

    def consume(self, grant_id: str) -> GrantVerdict:
        """Mark a grant spent. Exactly one caller can win this race."""
        with FileLock(self._lock_path):
            grants = self._read_all()
            for grant in grants:
                if grant.grant_id != grant_id:
                    continue
                if grant.used:
                    return GrantVerdict(False, "grant already used", grant)
                if grant.revoked:
                    return GrantVerdict(False, "grant was revoked", grant)
                if grant.expired:
                    return GrantVerdict(False, "grant expired", grant)
                grant.used_at = _now()
                self._write_all(grants)
                return GrantVerdict(True, "", grant)
        return GrantVerdict(False, "unknown grant id")

    def revoke(self, grant_id: str) -> bool:
        with FileLock(self._lock_path):
            grants = self._read_all()
            for grant in grants:
                if grant.grant_id == grant_id and not grant.used:
                    grant.revoked = True
                    self._write_all(grants)
                    return True
        return False

    def pending(self) -> list[SubmissionGrant]:
        return [g for g in self._read_all() if not g.used and not g.revoked and not g.expired]
