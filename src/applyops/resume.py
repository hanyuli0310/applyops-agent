"""One resume, one source of truth.

The earlier code carried the resume as a bare path, in two places that did not
agree: `profile.md` has a `resume_path` field, while `tools/auto_apply.py`
hard-coded ``DATA / "resume.pdf"``. Two sources means the file actually attached
to an application can differ from the one the user chose, and the log line meant
to reassure them reports the hard-coded name either way.

So:

- **There is exactly one way to obtain a resume**: :func:`resolve_resume`. It
  reads the profile's configured path, and it refuses rather than falling back
  to a default. A missing or unreadable resume is an error the user must fix,
  not a guess this code should make quietly.
- **Everything downstream takes a `ResumeRef`, never a path string.** A ref
  carries the digest, so "did we attach the right file" is answerable later
  against what the page reports.
- **The user must be able to see which file will go out**: `describe()` returns
  what to show them before anything is attached.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_SUFFIXES = {".pdf", ".doc", ".docx"}


class ResumeError(Exception):
    """Base class: everything here carries a message a user can act on."""


class ResumeNotConfigured(ResumeError):
    """No resume path is set in the profile."""


class ResumeNotFound(ResumeError):
    """The configured path does not exist, or is not a file."""


class ResumeUnsupported(ResumeError):
    """Suffix outside what ATS forms accept."""


class ResumeUnreadable(ResumeError):
    """The file exists but its bytes could not be read."""


@dataclass(frozen=True)
class ResumeRef:
    """The one resume, identified by content rather than by path alone."""

    path: Path
    filename: str
    size: int
    sha256: str

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
        }

    def describe(self) -> str:
        """What to show a human before their resume goes anywhere."""
        return f"{self.filename} ({self.size:,} bytes, sha256:{self.sha256[:12]}…)"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_resume(configured_path: str | Path | None) -> ResumeRef:
    """Turn the configured path into a `ResumeRef`, or refuse.

    Refusing is the point. The failure modes here -- the file was moved, the
    profile still points at a draft, the extension is something the ATS rejects
    after the form is already half-filled -- are all cheap to fix *before* an
    application starts and expensive afterwards. Returning a placeholder lets
    the run continue into exactly that state.
    """
    raw = str(configured_path or "").strip()
    if not raw:
        raise ResumeNotConfigured(
            "no resume configured. Set `resume_path` in data/profile.md "
            "(or run applyops-init) to the file you want to submit."
        )

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ResumeNotFound(
            f"resume path is not absolute: {raw!r}. Give the full path; relative "
            "paths resolve against whatever directory the process started in."
        )
    if not path.exists() or not path.is_file():
        raise ResumeNotFound(f"resume not found at {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ResumeUnsupported(
            f"{path.name}: unsupported type {path.suffix!r}. "
            f"Expected one of {', '.join(sorted(SUPPORTED_SUFFIXES))}."
        )

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ResumeUnreadable(f"could not read {path}: {exc}") from exc

    if not payload:
        raise ResumeUnreadable(f"{path.name} is empty (0 bytes)")

    return ResumeRef(
        path=path,
        filename=path.name,
        size=len(payload),
        sha256=sha256_of(path),
    )
