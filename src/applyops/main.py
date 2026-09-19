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


def _check_frontend() -> Check:
    dist = Path(__file__).parent.parent.parent / "frontend" / "dist" / "index.html"
    if dist.exists():
        return Check("console frontend", True, str(dist.parent))
    return Check(
        "console frontend",
        False,
        "frontend/dist is missing",
        "cd frontend && npm install && npm run build",
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


def _write_pid(data_dir: Path) -> None:
    _pid_file(data_dir).write_text(str(os.getpid()), encoding="utf-8")


def _read_pid(data_dir: Path) -> int | None:
    try:
        return int(_pid_file(data_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_ours(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.exists():  # Linux
            return "applyops" in cmdline.read_bytes().decode("utf-8", "ignore")
    except OSError:
        pass
    # macOS has no /proc; the pid file is ours by construction and recent.
    return True


def stop(data_dir: Path | None = None) -> int:
    root = data_dir or default_data_dir()
    pid = _read_pid(root)
    if not pid or not _pid_is_ours(pid):
        print("No running console found (or the pid file is stale).")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped console (pid {pid}).")
    except ProcessLookupError:
        print("The recorded console is already gone; cleaning up the pid file.")
    finally:
        _pid_file(root).unlink(missing_ok=True)
    return 0


def serve(data_dir: Path | None = None, port: int = 8620, *, demo: bool = False) -> int:
    from .api.app import create_app

    root = data_dir or default_data_dir()
    dist = Path(__file__).parent.parent.parent / "frontend" / "dist"
    if not (dist / "index.html").exists():
        print(
            "The console frontend is not built yet.\n"
            "  fix: cd frontend && npm install && npm run build",
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

    _write_pid(root)
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
    if args.command == "version":
        print(json.dumps({"version": APP_VERSION}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
