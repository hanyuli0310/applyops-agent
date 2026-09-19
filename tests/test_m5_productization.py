"""M5 -- Productization.

The bar from `PLAN.md`: a user who has never read this repository can

    install/start -> complete profile -> upload resume -> run the demo
    -> approve -> see a verified result -> inspect history

without a developer in the room. The M3 browser E2E already walks the second
half of that path against the built frontend; here we test the parts that make
the *first* half work: the doctor that explains what is missing and how to fix
it, the CLI entry points, and the refusal to serve anywhere but loopback.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from applyops.demo_ats import write_sample_resume
from applyops.main import doctor, main, stop
from applyops.memory import MemoryStore


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-m5-"))


# Synthetic Jane Doe data for temp dirs -- never the user's real profile.
SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "location": "Austin, TX",
    "work_authorization": "authorized to work",
    "requires_sponsorship": "no",
    "years_experience": "4",
    "current_title": "Backend Engineer",
    "current_company": "Acme",
    "expected_salary": "180000",
    "salary_currency": "USD",
    "willing_locations": "Remote, Austin",
}


def _complete_setup(root: Path) -> None:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )


def test_doctor_on_a_fresh_machine_lists_whats_missing_and_how_to_fix():
    """An empty data dir is the clean-machine case. Exit 1, every gap named,
    every gap with a fix -- a report a stranger can act on."""
    import io
    from contextlib import redirect_stdout

    root = _tmp()
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = doctor(root)
    output = buffer.getvalue()

    assert code == 1
    assert "profile" in output and "missing" in output
    assert "resume" in output
    assert "fix:" in output  # every failure carries the next step


def test_doctor_passes_when_setup_is_complete():
    import io
    from contextlib import redirect_stdout

    root = _tmp()
    _complete_setup(root)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = doctor(root)
    output = buffer.getvalue()
    assert code == 0
    assert "Everything checks out" in output


def test_doctor_never_writes_to_the_profile():
    """A read-only diagnostic must stay read-only."""
    root = _tmp()
    _complete_setup(root)
    before = (root / "memory.json").read_bytes()
    import io
    from contextlib import redirect_stdout

    with redirect_stdout(io.StringIO()):
        doctor(root)
    assert (root / "memory.json").read_bytes() == before


def test_cli_version_and_help_are_real_commands(capsys):
    assert main(["version"]) == 0
    assert json.loads(capsys.readouterr().out)["version"]
    # No subcommand -> help, exit 0, no traceback.
    assert main([]) == 0


def test_serve_refuses_anything_but_loopback():
    from applyops.api.app import serve

    # The guard is in the serve entry point: the approval endpoint is only
    # meaningful on a machine a single person controls.
    with pytest.raises(SystemExit):
        serve(_tmp(), host="0.0.0.0")


def test_stop_is_safe_when_nothing_is_running():
    """Stopping a console that was never started is a no-op, not an error."""
    root = _tmp()
    assert stop(root) == 0
    assert not (root / "ui.pid").exists()


def test_stop_only_touches_its_own_pid_file():
    """A record naming a port where no console answers is not ours to signal."""
    root = _tmp()
    (root / "ui.pid").write_text(
        json.dumps({"pid": 999999999, "port": 1}), encoding="utf-8"
    )
    assert stop(root) == 1  # nothing answered, so the pid was left alone
    assert not (root / "ui.pid").exists()  # and the stale record is cleaned up

    # A file that cannot even be parsed is stale too.
    (root / "ui.pid").write_text("not-json", encoding="utf-8")
    assert stop(root) == 0
    assert not (root / "ui.pid").exists()


def test_console_script_is_registered():
    """`applyops` must exist after `uv sync` -- the whole point of M5."""
    import subprocess
    import sys

    project = Path(__file__).parent.parent
    result = subprocess.run(  # noqa: PLW1510 - the returncode IS the assertion
        [str(project / ".venv" / "bin" / "applyops"), "version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["version"]
    _ = sys


@pytest.mark.asyncio
async def test_clean_machine_onboarding_reaches_verified_result():
    """The full acceptance path with only CLI + API -- the same walk the M3
    browser test does through the UI, expressed as the steps a user takes:

    doctor says what's missing -> profile+resume -> demo -> prepare -> approve
    -> submit -> verified -> history.
    """
    import io
    from contextlib import redirect_stdout

    from fastapi.testclient import TestClient

    from applyops.api.app import create_app

    root = _tmp()
    # 1. doctor on the fresh dir: exit 1, gaps named.
    with redirect_stdout(io.StringIO()) as report:
        assert doctor(root) == 1
    assert "profile" in report.getvalue()

    # 2. The user completes setup through the console (the API the UI drives).
    app = create_app(root, headless=True)
    # The console page carries the session token; a client acting as that page
    # has to send it on every state change.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"X-ApplyOps-Token": app.state.applyops.session_token})
        client.post("/api/profile", json=dict(SYNTHETIC_PROFILE))
        payload = write_sample_resume(root / "resume.pdf").read_bytes()
        client.post("/api/resumes", files={"file": ("resume.pdf", payload, "application/pdf")})

        # doctor is happy now.
        with redirect_stdout(io.StringIO()) as report:
            assert doctor(root) == 0
        assert "Everything checks out" in report.getvalue()

        # 3. Demo job -> prepare (which fills) -> answer what is missing ->
        #    prepare again -> approve -> submit -> verified.
        demo = client.post("/api/demo/start").json()
        app_id = demo["application"]["id"]
        prepared = client.post(f"/api/applications/{app_id}/prepare").json()
        if prepared["state"] == "waiting_for_input":
            # The demo form asks for a notice period, which nothing knows yet.
            assert prepared["missing"]
            client.post(
                f"/api/applications/{app_id}/answer",
                json={"question": prepared["missing"][0], "answer": "Two weeks"},
            )
            prepared = client.post(f"/api/applications/{app_id}/prepare").json()
        assert prepared["state"] == "waiting_for_approval", prepared

        approved = client.post(f"/api/requests/{prepared['request_id']}/approve").json()
        outcome = client.post(
            f"/api/applications/{app_id}/submit", json={"grant_id": approved["grant_id"]}
        ).json()
        assert outcome["status"] == "verified", outcome

        # 4. History shows the real record.
        detail = client.get(f"/api/applications/{app_id}").json()
        assert detail["application"]["state"] == "submitted_verified"
        assert detail["attempts"][0]["outcome"] == "verified"
        assert any(e["kind"] == "attempt_started" for e in detail["events"])
