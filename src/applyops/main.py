"""The `applyops` command line -- one entry point for a person, not a developer.

    applyops doctor    # what works, what is missing, what to do about it
    applyops serve     # the local console (loopback only)
    applyops demo      # serve + one local demo job in the queue
    applyops stop      # stop a running console

Design rules:

- **Every failure says what to do next.** "Chrome missing" with no instruction
  is a bug report waiting to happen; a doctor that names the fix is the product.
- **No JSON editing, no TOML, no absolute paths typed by hand.** If a step needs
  any of those, the step is wrong.
- **`stop` only stops what we started.** A pid file written by `serve`, checked
  to still be this project's process before it is signalled.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from .concurrency import browser_lock_path, is_free
from .mcp.runtime import DATA_DIR_ENV, default_data_dir

APP_VERSION = "0.2.0"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""

    def render(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        line = f"  [{mark}] {self.name}"
        if self.detail:
            line += f" -- {self.detail}"
        if not self.ok and self.fix:
            line += f"\n         fix: {self.fix}"
        return line


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        return Check("python", True, f"{major}.{minor}")
    return Check(
        "python",
        False,
        f"{major}.{minor} (need >= 3.12)",
        "install Python 3.12+ (uv does this automatically: `uv sync`)",
    )


def _check_os() -> Check:
    system = sys.platform
    if system == "darwin":
        return Check("operating system", True, "macOS (verified platform)")
    if system.startswith("linux"):
        return Check("operating system", True, "Linux (verified where tested)")
    return Check(
        "operating system", False, system, "macOS and Linux are the supported platforms"
    )


def _check_chrome() -> Check:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
    ]
    if any(c and Path(c).exists() for c in candidates):
        return Check("google chrome", True)
    return Check(
        "google chrome",
        False,
        "not found",
        "run `uv run playwright install chrome` (installs the real Chrome, not a bundled copy)",
    )


def _check_data_dir(data_dir: Path) -> list[Check]:
    checks: list[Check] = []
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("data directory", True, str(data_dir)))
    except OSError as exc:
        checks.append(
            Check(
                "data directory", False, str(exc),
                f"make {data_dir} writable, or point ${DATA_DIR_ENV} somewhere writable",
            )
        )

    locks = data_dir / ".locks"
    if is_free(browser_lock_path(data_dir)):
        checks.append(Check("browser lock", True, "free"))
    else:
        from .concurrency import describe_holder

        checks.append(
            Check(
                "browser lock", False, describe_holder(browser_lock_path(data_dir)),
                "another driver holds the shared Chrome profile; stop it or wait",
            )
        )
    _ = locks  # lock files live here; nothing to check beyond writability above
    return checks


def _check_profile(memory) -> list[Check]:
    checks: list[Check] = []
    profile = memory.profile
    missing = profile.missing_required()
    if profile.is_ready():
        checks.append(Check("profile", True, "all required fields present"))
    else:
        checks.append(
            Check(
                "profile",
                False,
                f"missing: {', '.join(missing)}",
                "open the console (applyops serve) and fill them in, or run applyops-init",
            )
        )
    configured = profile.value("resume_path")
    try:
        from .resume import resolve_resume

        ref = resolve_resume(configured)
        checks.append(Check("resume", True, ref.describe()))
    except Exception as exc:  # noqa: BLE001 - each cause is user-actionable
        checks.append(
            Check(
                "resume", False, str(exc),
                "upload a PDF in the console (资料与简历 page)",
            )
        )
    return checks


def frontend_dist() -> Path | None:
    """Where the built console lives, if it is available anywhere.

    Packaged installs ship the build inside the wheel (`applyops/web`), so a
    user who installed ApplyOps never runs npm. A checkout keeps using
    `frontend/dist`. Only if neither exists does anything ask the user to build
    -- and then with the one command that fixes it.
    """
    packaged = Path(__file__).parent / "web"
    if (packaged / "index.html").exists():
        return packaged
    repo = Path(__file__).parent.parent.parent / "frontend" / "dist"
    if (repo / "index.html").exists():
        return repo
    return None


def _check_frontend() -> Check:
    dist = frontend_dist()
    if dist is not None:
        return Check("console frontend", True, str(dist))
    return Check(
        "console frontend",
        False,
        "no built console found (neither packaged nor frontend/dist)",
        "reinstall ApplyOps, or from a checkout run: cd frontend && npm install && npm run build",
    )


def doctor(data_dir: Path | None = None) -> int:
    """Run every check, print a report, exit non-zero if anything is broken."""
    root = data_dir or default_data_dir()
    print(f"ApplyOps doctor -- data: {root}\n")

    checks: list[Check] = [_check_os(), _check_python(), _check_chrome()]
    checks.extend(_check_data_dir(root))

    from .memory import MemoryStore

    memory = MemoryStore(root / "memory.json")
    checks.extend(_check_profile(memory))
    checks.append(_check_frontend())

    failed = 0
    for check in checks:
        print(check.render())
        if not check.ok:
            failed += 1

    print()
    if failed:
        print(f"{failed} check(s) need attention. Fix the items above and re-run.")
        return 1
    print("Everything checks out. `applyops serve` to open the console.")
    return 0


def _pid_file(data_dir: Path) -> Path:
    return data_dir / "ui.pid"


def _write_run_file(data_dir: Path, port: int) -> None:
    """Record pid + port. The token is not written here on purpose."""
    _pid_file(data_dir).write_text(
        json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8"
    )


def _read_run_file(data_dir: Path) -> dict | None:
    try:
        payload = json.loads(_pid_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("pid") else None


def _console_is_ours(port: int) -> bool:
    """Ask the process on `port` to identify itself.

    macOS has no `/proc`, so "the pid file says so" was the entire check -- and a
    recycled pid would then have meant signalling an unrelated process. The
    replacement does not care what the OS knows: it speaks to the port the
    console recorded and requires an ApplyOps status response. A stranger
    listening there answers with something else, or nothing.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=2.0
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(body, dict) and "profile_ready" in body and "version" in body


def stop(data_dir: Path | None = None) -> int:
    root = data_dir or default_data_dir()
    record = _read_run_file(root)
    if not record:
        # Unparseable or empty: nothing can be proven from it, and leaving it
        # behind would make the next `stop` print the same thing forever.
        _pid_file(root).unlink(missing_ok=True)
        print("No running console found (or the pid file was stale; cleaned up).")
        return 0

    port = int(record.get("port") or 0)
    if not port or not _console_is_ours(port):
        print(
            "The recorded console does not answer on its port, so this will not "
            "signal that pid -- it may have been recycled by another program."
        )
        _pid_file(root).unlink(missing_ok=True)
        return 1

    pid = int(record["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped console (pid {pid}, port {port}).")
    except ProcessLookupError:
        print("The recorded console is already gone; cleaning up the pid file.")
    finally:
        _pid_file(root).unlink(missing_ok=True)
    return 0


def serve(data_dir: Path | None = None, port: int = 8620, *, demo: bool = False) -> int:
    from .api.app import create_app

    root = data_dir or default_data_dir()
    dist = frontend_dist()
    if dist is None:
        print(
            "No built console found, and this copy of ApplyOps did not ship one.\n"
            "  fix (from a checkout): cd frontend && npm install && npm run build",
            file=sys.stderr,
        )
        return 1

    if root is None:
        root = default_data_dir()

    # One app instance is built here and served -- so the demo job enqueued for
    # a first-run user lives in the same stores the UI reads.
    app = create_app(root, frontend_dist=dist, headless=False)
    if demo:
        state = app.state.applyops
        demo_url = state.start_demo_ats()
        row = state.service.enqueue(
            job_url=f"{demo_url}/form",
            job_id=f"demo-{demo_url.rsplit(':', 1)[-1]}",
            route="demo",
            platform="DemoATS",
            title="Backend Engineer (demo)",
            company="ApplyOps Demo Co",
        )
        print(f"Demo job queued: {row.id}")

    import uvicorn

    _write_run_file(root, port)
    try:
        print(f"ApplyOps console: http://127.0.0.1:{port}  (loopback only)")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        _pid_file(root).unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="applyops", description="ApplyOps local console"
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=f"state directory (default: ${DATA_DIR_ENV} or <repo>/data)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="check the installation and explain what to fix")
    serve_parser = sub.add_parser("serve", help="run the local console")
    serve_parser.add_argument("--port", type=int, default=8620)
    demo_parser = sub.add_parser("demo", help="run the console with a demo job queued")
    demo_parser.add_argument("--port", type=int, default=8620)
    sub.add_parser("stop", help="stop a running console")
    approve_parser = sub.add_parser(
        "approve", help="review and decide a pending submission request"
    )
    approve_parser.add_argument(
        "request_id", nargs="?", help="the request to decide; omit to list pending"
    )
    sub.add_parser("version", help="print the version")

    args = parser.parse_args(argv)
    root = Path(args.data_dir).expanduser() if args.data_dir else None

    if args.command == "doctor":
        return doctor(root)
    if args.command == "serve":
        return serve(root, port=args.port)
    if args.command == "demo":
        return serve(root, port=args.port, demo=True)
    if args.command == "stop":
        return stop(root)
    if args.command == "approve":
        # The human channel, reachable from the one command a user knows.
        from .approve import decide, list_requests
        from .authorization import SubmissionAuthorizer

        authorizer = SubmissionAuthorizer(root or default_data_dir())
        if args.request_id:
            return decide(authorizer, args.request_id)
        return list_requests(authorizer)
    if args.command == "version":
        print(json.dumps({"version": APP_VERSION}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
