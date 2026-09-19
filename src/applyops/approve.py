"""The human side of the authorization boundary.

    python -m applyops.approve            # list requests waiting for a decision
    python -m applyops.approve <id>       # read one request, then decide

Why this is a separate command and not a flag on the MCP surface: whoever wants
to submit something must not be able to grant permission to submit it. An agent
that can both ask "may I?" and answer "you may" has no boundary -- which is what
`acknowledged=True` was.

So a request is opened by the process that wants to act, and closed here, by a
person reading the values that were read back off the live form and typing yes
or no. The grant it mints is bound to those exact values, the resume digest and
the fact revisions; if any of them move afterwards, `execute_authorized_submission`
refuses to use it.

It deliberately does **not** auto-approve anything, has no `--yes` shortcut for a
request id, and never runs unattended.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .authorization import SubmissionAuthorizer
from .mcp.runtime import DATA_DIR_ENV, default_data_dir

YES = {"y", "yes"}
NO = {"n", "no"}


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _authorizer(data_dir: str | None) -> SubmissionAuthorizer:
    root = Path(data_dir).expanduser() if data_dir else default_data_dir()
    return SubmissionAuthorizer(root)


def list_requests(authorizer: SubmissionAuthorizer) -> int:
    pending = authorizer.pending_requests()
    if not pending:
        print("No submission requests are waiting for a decision.")
        return 0
    print(f"{len(pending)} request(s) waiting:\n")
    for request in pending:
        first_line = request.summary_for_human().splitlines()[1].strip()
        print(f"  {request.request_id}  {first_line}")
        print(f"      requested by: {request.requested_by}   at: {request.created_at}")
        print(f"      approve with: python -m applyops.approve {request.request_id}")
        print()
    return 0


def decide(authorizer: SubmissionAuthorizer, request_id: str) -> int:
    request = authorizer.get_request(request_id)
    if request is None:
        print(f"No request with id {request_id!r}.", file=sys.stderr)
        return 1
    if request.status != "pending":
        print(f"This request was already {request.status} at {request.decided_at}.", file=sys.stderr)
        return 1
    if request.expired:
        print("This request expired. Ask for a fresh one; the form may have moved on.")
        return 1
    if not _is_interactive():
        # A non-interactive yes would make automated approval trivial, which is
        # the one thing this command exists to prevent.
        print(
            "Refusing to decide non-interactively: approval has to come from a "
            "person reading the values above.",
            file=sys.stderr,
        )
        return 2

    print()
    print(request.summary_for_human())
    print()
    print("Approving sends this to the employer and cannot be undone.")
    try:
        answer = input("Approve this submission? [y/N] ").strip().lower()
    except EOFError:
        print("\nNo input available; nothing was approved.", file=sys.stderr)
        return 2

    if answer not in YES:
        authorizer.reject_request(request_id)
        print("Rejected. Nothing was submitted.")
        return 0

    grant = authorizer.approve_request(request_id, source="cli_human")
    if grant is None:
        print("Could not approve: the request is no longer pending.", file=sys.stderr)
        return 1
    print()
    print(f"Approved. Grant id: {grant.grant_id}")
    print("Give this id to submit_final. It is single-use and expires in 15 minutes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m applyops.approve",
        description="Review and approve ApplyOps submission requests.",
    )
    parser.add_argument("request_id", nargs="?", help="the request to decide")
    parser.add_argument(
        "--data-dir",
        default=None,
        help=f"state directory (default: ${DATA_DIR_ENV} or <repo>/data)",
    )
    args = parser.parse_args(argv)

    authorizer = _authorizer(args.data_dir)
    if args.request_id:
        return decide(authorizer, args.request_id)
    return list_requests(authorizer)


if __name__ == "__main__":
    raise SystemExit(main())
