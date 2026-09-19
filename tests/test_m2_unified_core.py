"""M2 -- Unified Core.

The properties under test are the ones a locked-JSON merge could not give:

- an illegal move is refused, with the reason;
- state survives a restart (it lives in SQLite, not in anyone's memory);
- two writers race a claim and exactly one executes;
- a crash inside `SUBMITTING` lands in `SUBMITTED_UNVERIFIED`, never back at
  "ready to submit again";
- legacy history is imported with a backup, idempotently, and is *never*
  promoted into a verified success.

The one browser test drives the demo ATS end to end through the service.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.filling import fill_application_form
from applyops.ledger import ApplicationRow, Ledger
from applyops.memory import MemoryStore
from applyops.resume import resolve_resume
from applyops.service import ApplicationService
from applyops.state_machine import (
    ApplicationState,
    InvalidTransition,
    can_submit,
    is_terminal,
    require_transition,
)
from applyops.submission import FinalAction


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-m2-"))


def _service(data_dir: Path | None = None, **kwargs) -> ApplicationService:
    """A service with a real memory store: the filler and the rails need one."""
    root = data_dir or _tmp()
    kwargs.setdefault("memory", MemoryStore(root / "memory.json"))
    return ApplicationService(root, **kwargs)


def _enqueue(service: ApplicationService, job_key: str = "job-1") -> ApplicationRow:
    return service.enqueue(
        job_url=f"https://example.test/jobs/{job_key}",
        job_id=job_key,
        route="demo",
        platform="DemoATS",
        title="Backend Engineer",
        company="ApplyOps Demo Co",
    )


# ── A. the state machine itself ──────────────────────────────────────


def test_the_happy_path_is_legal():
    path = [
        (ApplicationState.QUEUED, ApplicationState.PREPARING),
        (ApplicationState.PREPARING, ApplicationState.WAITING_FOR_APPROVAL),
        (ApplicationState.WAITING_FOR_APPROVAL, ApplicationState.SUBMITTING),
        (ApplicationState.SUBMITTING, ApplicationState.SUBMITTED_VERIFIED),
    ]
    for current, target in path:
        require_transition(current, target)  # must not raise


def test_waiting_for_approval_cannot_go_back_to_submitting():
    """Re-entering SUBMITTING from an approval state is the duplicate machine."""
    with pytest.raises(InvalidTransition):
        require_transition(
            ApplicationState.SUBMITTING, ApplicationState.WAITING_FOR_APPROVAL
        )


def test_submitted_unverified_cannot_be_submitted_again():
    """The only exits from unknown are evidence, never another attempt."""
    with pytest.raises(InvalidTransition):
        require_transition(
            ApplicationState.SUBMITTED_UNVERIFIED, ApplicationState.SUBMITTING
        )
    with pytest.raises(InvalidTransition):
        require_transition(
            ApplicationState.SUBMITTED_UNVERIFIED, ApplicationState.PREPARING
        )
    # Evidence found later is the one legal move.
    require_transition(
        ApplicationState.SUBMITTED_UNVERIFIED, ApplicationState.SUBMITTED_VERIFIED
    )


def test_terminal_states_do_not_move():
    for state in (
        ApplicationState.SUBMITTED_VERIFIED,
        ApplicationState.CANCELLED,
        ApplicationState.SKIPPED,
        ApplicationState.LEGACY_IMPORTED,
    ):
        with pytest.raises(InvalidTransition):
            require_transition(state, ApplicationState.PREPARING)


def test_legacy_history_is_terminal_and_never_submittable():
    assert is_terminal(ApplicationState.LEGACY_IMPORTED) is True
    assert can_submit(ApplicationState.LEGACY_IMPORTED) is False
    with pytest.raises(InvalidTransition):
        require_transition(
            ApplicationState.LEGACY_IMPORTED, ApplicationState.SUBMITTING
        )


def test_failed_can_prepare_a_retry():
    """A retry is legal -- as a new attempt, never as a rewrite of the old one."""
    require_transition(ApplicationState.FAILED, ApplicationState.PREPARING)
    assert can_submit(ApplicationState.WAITING_FOR_APPROVAL) is True
    assert can_submit(ApplicationState.QUEUED) is False


# ── B. the ledger: durability and concurrency ────────────────────────


def test_state_survives_a_restart():
    root = _tmp()
    service = _service(root)
    row = _enqueue(service)
    service.ledger.transition(row.id, ApplicationState.PREPARING)

    # A brand-new service over the same data directory is the restart.
    reopened = _service(root)
    reread = reopened.get(row.id)
    assert reread is not None
    assert reread.state == ApplicationState.PREPARING.value


def test_enqueue_is_idempotent_per_job_key():
    service = _service()
    first = _enqueue(service, "job-x")
    again = _enqueue(service, "job-x")
    assert first.id == again.id
    assert len(service.list()) == 1


def test_stale_writer_cannot_move_a_row_it_did_not_see():
    service = _service()
    row = _enqueue(service)
    service.ledger.transition(row.id, ApplicationState.PREPARING)

    with pytest.raises(Exception) as excinfo:
        # The writer believes the row is still QUEUED and passes that explicitly.
        service.ledger.transition(
            row.id, ApplicationState.WAITING_FOR_APPROVAL,
            expected_state=ApplicationState.QUEUED.value,
        )
    assert "another writer" in str(excinfo.value)


def test_two_processes_cannot_both_claim():
    service = _service()
    row = _enqueue(service)

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker(owner: str) -> None:
        barrier.wait()
        results.append(service.ledger.claim(row.id, owner))

    threads = [
        threading.Thread(target=worker, args=(f"proc-{i}",)) for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True], "exactly one claim may win"


def test_two_services_cannot_both_execute_the_same_application():
    """The race the ledger exists for: two entry points, one application."""
    root = _tmp()
    service_a = _service(root)
    service_b = _service(root)
    row = _enqueue(service_a)
    assert service_a.ledger.path == service_b.ledger.path

    # Drive to the approval state through legal transitions first.
    service_a.ledger.transition(row.id, ApplicationState.PREPARING)
    service_a.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def attempt_claim(service: ApplicationService, name: str) -> None:
        barrier.wait()
        outcomes.append("won" if service.ledger.claim(row.id, name) else "lost")

    threads = [
        threading.Thread(target=attempt_claim, args=(s, n))
        for s, n in ((service_a, "A"), (service_b, "B"))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("won") == 1


def test_attempts_accumulate_and_are_never_overwritten():
    root = _tmp()
    service = _service(root)
    row = _enqueue(service)

    first = service.ledger.start_attempt(row.id)
    service.ledger.finish_attempt(
        first.id, outcome="failed", detail="click never landed"
    )
    second = service.ledger.start_attempt(row.id)
    service.ledger.finish_attempt(
        second.id, outcome="verified", detail="page confirmed"
    )

    attempts = service.ledger.attempts(row.id)
    assert [(a.ordinal, a.outcome) for a in attempts] == [(1, "failed"), (2, "verified")]


def test_attempt_rejects_an_unknown_outcome():
    root = _tmp()
    service = _service(root)
    row = _enqueue(service)
    attempt = service.ledger.start_attempt(row.id)
    with pytest.raises(ValueError):
        service.ledger.finish_attempt(attempt.id, outcome="probably_fine")


def test_events_are_append_only_history():
    root = _tmp()
    service = _service(root)
    row = _enqueue(service)
    service.ledger.transition(row.id, ApplicationState.PREPARING)

    events = service.ledger.events(row.id)
    kinds = [e["kind"] for e in events]
    assert kinds == ["created", "transition"]


def test_expired_submitting_claim_lands_in_unverified_not_back_at_ready():
    """The crash recovery: nobody knows if the request was sent. Nobody may
    guess 'no', because guessing 'no' invites a second send."""
    root = _tmp()
    service = _service(root)
    row = _enqueue(service)

    service.ledger.transition(row.id, ApplicationState.PREPARING)
    service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)
    service.ledger.transition(row.id, ApplicationState.SUBMITTING)
    # Simulate the holder dying: its claim expiry is in the past.
    with service.ledger._conn:
        service.ledger._conn.execute(
            "UPDATE applications SET claim_expires_at = ? WHERE id = ?",
            (0.0, row.id),
        )

    recovered = service.recover()
    assert [r.id for r in recovered] == [row.id]
    assert service.get(row.id).state == ApplicationState.SUBMITTED_UNVERIFIED.value

    # And from there, submission is structurally impossible.
    with pytest.raises(InvalidTransition):
        require_transition(
            ApplicationState.SUBMITTED_UNVERIFIED, ApplicationState.SUBMITTING
        )


def test_schema_downgrade_is_refused():
    """Newer data must not be silently mangled by older code."""
    root = _tmp()
    service = _service(root)
    service.ledger._conn.execute("PRAGMA user_version = 99")
    service.ledger._conn.commit()

    with pytest.raises(RuntimeError) as excinfo:
        Ledger(service.ledger.path)
    assert "v99" in str(excinfo.value)


# ── C. legacy migration ──────────────────────────────────────────────


def _legacy_memory_file(root: Path, records: list[dict]) -> Path:
    path = root / "memory.json"
    path.write_text(
        json.dumps({"application_history": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_legacy_import_never_upgrades_to_verified():
    root = _tmp()
    _legacy_memory_file(
        root,
        [
            {
                "job_url": "https://example.test/jobs/old-1",
                "job_id": "old-1",
                "job_title": "Engineer",
                "company": "Old Co",
                "platform": "LinkedIn",
                "status": "applied",
            }
        ],
    )
    service = _service(root)
    result = service.import_legacy_history()

    assert result["imported"] == 1
    row = service.ledger.find_by_job_key("old-1")
    assert row.state == ApplicationState.LEGACY_IMPORTED.value

    # A legacy row has no attempts, no evidence, and no path to success.
    assert service.ledger.attempts(row.id) == []
    with pytest.raises(InvalidTransition):
        service.ledger.transition(row.id, ApplicationState.SUBMITTED_VERIFIED)


def test_legacy_import_is_idempotent_and_backs_up_first():
    root = _tmp()
    records = [
        {"job_url": "https://example.test/jobs/old-1", "job_id": "old-1"},
        {"job_url": "https://example.test/jobs/old-2", "job_id": "old-2"},
    ]
    memory_path = _legacy_memory_file(root, records)

    service = _service(root)
    first = service.import_legacy_history()
    assert first["imported"] == 2
    backup = Path(first["backup"])
    assert backup.exists(), "the source must be backed up before anything else"
    assert backup.read_text(encoding="utf-8") == memory_path.read_text(encoding="utf-8")

    # A second run, plus a new row in the file, imports only the new row.
    records.append({"job_url": "https://example.test/jobs/old-3", "job_id": "old-3"})
    memory_path.write_text(
        json.dumps({"application_history": records}), encoding="utf-8"
    )
    second = service.import_legacy_history()
    assert second["imported"] == 1
    assert second["skipped"] == 2
    assert len(service.list()) == 3


def test_legacy_import_failure_leaves_the_source_untouched():
    root = _tmp()
    records = [{"job_url": "https://example.test/jobs/old-1", "job_id": "old-1"}]
    memory_path = _legacy_memory_file(root, records)
    before = memory_path.read_bytes()

    # A ledger that cannot open: make its sqlite file a directory, before the
    # service is ever constructed.
    broken_dir = root / "unwritable"
    broken_dir.mkdir()
    (broken_dir / "app.sqlite").mkdir()

    with pytest.raises(sqlite3.OperationalError):
        _service(broken_dir).import_legacy_history(memory_path)

    assert memory_path.read_bytes() == before, "the source file must survive any failure"
    # And the real service can still import it afterwards.
    service = _service(root)
    assert service.import_legacy_history()["imported"] == 1


def test_migration_fresh_data_root_imports_nothing():
    # Deliberately no memory store: the point is a data dir with no history file
    # at all, which is the state a brand-new install is in.
    root = _tmp()
    service = ApplicationService(root)
    assert service.import_legacy_history() == {
        "imported": 0, "skipped": 0, "backup": ""
    }


# ── D. the service, end to end on the demo ATS ───────────────────────


@pytest.mark.asyncio
async def test_service_drives_one_verified_submission_end_to_end():
    root = _tmp()
    service = _service(root)
    row = _enqueue(service, "job-e2e")

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(
            headless=True, user_data_dir=root / "chrome-profile"
        )
        await controller.launch()
        try:
            # Prepare: nothing answered yet, so the service parks it for input.
            row = service.prepare(row.id, ready=False, detail="form not answered yet")
            assert row.state == ApplicationState.WAITING_FOR_INPUT.value

            # Answer the one thing nothing knows, then fill the form the way the
            # console does -- fill, read back, attach, read back.
            service.memory.update_profile(
                {
                    "name": "Jane Doe",
                    "email": "jane@example.com",
                    "phone": "+1 555 010 4477",
                    "years_experience": "4",
                    "requires_sponsorship": "no",
                }
            )
            await controller.goto(f"{ats.url}/form", settle=0.4)
            service.answers.set_answer("Notice period", "Two weeks")
            report = await fill_application_form(
                controller,
                memory=service.memory,
                answers=service.answers,
                resume=resolve_resume(str(write_sample_resume(root / "resume.pdf"))),
                application_id=row.id,
            )
            assert report.ready, report.to_dict()

            row = service.prepare(row.id, ready=True, detail="form filled and verified")
            assert row.state == ApplicationState.WAITING_FOR_APPROVAL.value

            # Approval flows through the request -> human -> grant split, with
            # the same revisions every real driver uses.
            snapshot = await controller.field_snapshot()
            resume = resolve_resume(str(root / "resume.pdf"))
            profile_revision, answers_revision = service.revisions()
            request = service.authorizer.create_request(
                job_key=row.job_key,
                job_url=row.job_url,
                route=row.route,
                platform=row.platform,
                fields=snapshot,
                resume_filename=resume.filename,
                resume_sha256=resume.sha256,
                answers_revision=answers_revision,
                profile_revision=profile_revision,
                application_id=row.id,
                page_url=controller.page.url,
                requested_by="service_test",
            )
            grant = service.authorizer.approve_request(
                request.request_id, source="cli_human"
            )

            from applyops.evidence import detect_final_action

            action, detail = await detect_final_action(controller)
            assert action is not None, detail

            outcome = await service.submit(
                row.id,
                grant_id=grant.grant_id,
                controller=controller,
                resume=resume,
                action=action,
            )
            assert outcome.status == "verified", outcome.to_dict()

            row = service.get(row.id)
            assert row.state == ApplicationState.SUBMITTED_VERIFIED.value
            attempts = service.ledger.attempts(row.id)
            assert len(attempts) == 1 and attempts[0].outcome == "verified"
        finally:
            await controller.close()


@pytest.mark.asyncio
async def test_submit_from_the_wrong_state_writes_no_attempt():
    root = _tmp()
    service = _service(root)
    row = _enqueue(service, "job-wrong-state")

    from applyops.browser import BrowserController

    controller = BrowserController(
        headless=True, user_data_dir=root / "chrome-profile"
    )
    await controller.launch()
    try:
        with pytest.raises(InvalidTransition):
            await service.submit(
                row.id,
                grant_id="whatever",
                controller=controller,
                resume=resolve_resume(str(write_sample_resume(root / "r.pdf"))),
                action=FinalAction(name="Submit application"),
            )
        # No attempt row, no state damage, claim released.
        assert service.ledger.attempts(row.id) == []
        assert service.get(row.id).state == ApplicationState.QUEUED.value
    finally:
        await controller.close()


def test_history_mirror_only_counts_verified_outcomes():
    from applyops.memory import MemoryStore
    from applyops.submission import SubmitOutcome

    root = _tmp()
    service = _service(root, memory=MemoryStore(root / "memory.json"))
    row = _enqueue(service, "job-mirror")

    service._record_in_memory(
        row,
        SubmitOutcome(
            status="unverified", job_key=row.job_key, grant_id="g1",
            detail="sent but unconfirmed", evidence={"resume": {}},
        ),
    )
    platform = next(
        p for p in service.memory.get_stats()["platforms"]
        if p["platform"] == "DemoATS"
    )
    assert platform["runs"] == 1
    assert platform["success_rate"] == 0.0
