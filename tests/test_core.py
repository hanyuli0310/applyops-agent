"""Sanity tests for the modules M1 depends on.

Scope is deliberate: these cover the logic that must not silently break --
platform detection, the flywheel's memory contract, reference-string stability,
the profile file, and the safety rails. The browser layer is validated against
live pages rather than mocked here, because a mock of LinkedIn would only ever
confirm my assumptions about LinkedIn.
"""

import json
import tempfile
from pathlib import Path

from applyops.guardrails import Guardrails
from applyops.locator import build_ref, parse_ref
from applyops.memory import MemoryStore
from applyops.platforms.detector import Platform, detect_platform, is_supported
from applyops.profile import (
    REQUIRED_KEYS,
    ProfileStore,
    migrate_legacy_profile,
    parse_profile,
    render_example,
)


def test_platform_detector():
    assert detect_platform("https://www.linkedin.com/jobs/view/123456789") == Platform.LINKEDIN
    assert detect_platform("https://www.indeed.com/viewjob?jk=abcdef") == Platform.INDEED
    assert detect_platform("https://www.amazon.jobs/en/jobs/10529830") == Platform.AMAZON
    # The apply flow's own host is on the same registrable domain, so matching
    # `amazon.jobs` has to catch the passport subdomain too.
    assert detect_platform("https://passport.amazon.jobs/") == Platform.AMAZON
    # ...but the storefront is not a careers site. Matching `amazon.com` would
    # file shopping URLs as job postings.
    assert detect_platform("https://www.amazon.com/dp/B0EXAMPLE") == Platform.UNKNOWN
    assert (
        detect_platform("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/123")
        == Platform.WORKDAY
    )
    assert detect_platform("https://boards.greenhouse.io/stripe/jobs/4567") == Platform.GREENHOUSE
    assert detect_platform("https://jobs.lever.co/spotify/789") == Platform.LEVER
    assert is_supported("https://www.linkedin.com/jobs/view/999") is True
    assert is_supported("https://unknown-random-site.xyz/careers") is False


def test_locator_refs():
    # 1. An automation id wins over everything else: it is authored, not generated.
    assert (
        build_ref(
            {
                "automationId": "legalNameSection_firstName",
                "id": "input-17",
                "label": "First Name",
            }
        )
        == "auto=legalNameSection_firstName"
    )

    # 2. A short, hand-written id is stable enough to use verbatim.
    assert build_ref({"id": "first_name"}) == "id=first_name"

    # 3. A long generated id is anchored on its semantic tail. This is the case
    #    that matters for LinkedIn: matching the full id breaks on the next
    #    posting, because the urn in the middle changes.
    linkedin_id = (
        "single-line-text-form-component-formElement-urn-li-jobs-applyformcommon-"
        "easyApplyFormElement-4453247844-31371469220-phoneNumber-nationalNumber"
    )
    assert build_ref({"id": linkedin_id}) == "idsuffix=phoneNumber-nationalNumber"

    # 4. Framework instance counters are noise, so it must fall back to the
    #    label. Anchoring on `jobsDocumentCardToggle-ember244` would poison the
    #    selector memory with a key that is stale by the next render.
    assert build_ref({"id": "jobsDocumentCardToggle-ember244", "label": "Dismiss"}) == (
        "label=Dismiss"
    )
    assert build_ref({"id": "input-17", "label": "City", "labelSource": "placeholder"}) == (
        'css=[placeholder="City"]'
    )
    assert build_ref({"id": "«r3»", "label": "Email", "labelSource": "aria-label"}) == (
        'css=[aria-label="Email"]'
    )

    # 5. Nothing stable at all: an unstable id still beats an empty ref, but only
    #    just, and it must never be preferred over a label.
    assert build_ref({"id": "«r3»"}) == "id=«r3»"
    assert build_ref({}) == ""

    # 6. Round trip. `parse_ref` splits what `build_ref` produces, and a caller
    #    that passes a raw selector is tolerated rather than rejected.
    assert parse_ref("idsuffix=phoneNumber-nationalNumber") == (
        "idsuffix",
        "phoneNumber-nationalNumber",
    )
    assert parse_ref("auto=firstName") == ("auto", "firstName")
    assert parse_ref("#submit") == ("css", "#submit")
    assert parse_ref("") == ("", "")


def test_flywheel_asks_once():
    """One answer from the user must be enough to stop asking.

    The alternative -- needing the human to state the same answer twice before it
    is trusted -- reads as "ask once, then ask again just to be sure", which is
    the whole promise broken. It is also easy to miss: the entry looks healthy
    (trust 0.70) while sitting just under the 0.60 auto-fill threshold, so the
    caller gets `suggestion` forever and a batch run skips the posting.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        store = MemoryStore(Path(tmpdir) / "flywheel.json")

        question = "How many years of work experience do you have with MCP?"
        assert store.get_confident_answer(question) is None
        assert store.get_suggestion(question) is None

        entry = store.learn(question, "3", source="user")
        assert entry.times_used == 0, "stating an answer is not a deployment"
        assert entry.is_auto_ready, f"confidence {entry.confidence} must clear the bar"

        # Same question, this posting's wording -- must recall without a human.
        found = store.get_confident_answer(question)
        assert found is not None and found.answer == "3"

        # It becomes a deployment only once a form actually used it.
        found.mark_used()
        assert store.get_qa(entry.id).times_used == 1

        # And it still decays: contradictions retire an answer even though the
        # user stated it, so a stale answer cannot be sent forever.
        for _ in range(3):
            entry._reinforce(False)
        assert not store.get_qa(entry.id).is_auto_ready


def test_memory_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "test_memory.json"
        store = MemoryStore(json_path)

        # 1. Profile
        store.set_profile("name", "Jane Doe")
        store.set_profile("email", "jane@example.com")
        store.update_profile({"phone": "+1234567890", "location": "Austin, TX"})

        prof = store.get_profile()
        assert prof["name"] == "Jane Doe"
        assert prof["phone"] == "+1234567890"

        # 2. Learned QA
        qa1 = store.learn("What is your expected salary range?", "150k - 180k USD", context="salary")
        assert qa1.question == "What is your expected salary range?"

        # 3. Match answer (recall is side-effect free; usage must be recorded explicitly)
        matched = store.find_answer("expected salary range")
        assert matched is not None
        assert matched.answer == "150k - 180k USD"
        assert matched.times_used == 0
        matched.mark_used()
        assert store.get_qa(matched.id).times_used == 1

        # 3b. Reworded duplicates collapse into one memory instead of piling up
        store.learn("Salary range expectation?", "150k - 180k USD", context="salary")
        assert len(store.get_all_qa()) == 1

        # 3c. The flywheel's anti-starvation counter moves on every attempt, not
        #     only on a hit. A memory that is tried and missed is still learning;
        #     one that is never tried is dead.
        store.record_selector_result("LinkedIn", "phone", "idsuffix=phoneNumber-nationalNumber", True)
        store.record_selector_result("LinkedIn", "phone", "idsuffix=phone-number", False)
        stats = store.get_stats()
        assert stats["selectors_suggested"] == 2

        # 4. Applications
        store.add_application(
            job_url="https://linkedin.com/jobs/view/100",
            job_title="Software Engineer",
            company="Google",
            platform="LinkedIn",
            status="applied",
        )
        assert store.is_already_applied("https://linkedin.com/jobs/view/100") is True
        assert store.is_already_applied("https://linkedin.com/jobs/view/200") is False

        # 5. Reload from disk to verify persistence
        store2 = MemoryStore(json_path)
        assert store2.get_profile()["name"] == "Jane Doe"
        assert len(store2.get_all_qa()) == 1
        assert store2.is_already_applied("https://linkedin.com/jobs/view/100") is True


def test_profile_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "profile.md"
        store = ProfileStore(path)

        # 1. A fresh store invents nothing. Every required field is reported
        #    missing, which is what makes the setup wizard ask rather than guess.
        assert store.get() == {}
        assert store.is_ready() is False
        assert store.missing_required() == REQUIRED_KEYS

        # 2. Values are normalized the way a human writes them. "$180,000" and
        #    "180k" must both land on 180000 -- a silent "180" would put a wrong
        #    salary on a real form.
        report = store.set_many({
            "expected_salary": "$180,000",
            "requires_sponsorship": "No",
            "phone": "+1 555 010 4477",
            "years_experience": "4",
        })
        assert report["warnings"] == []
        assert store.value("expected_salary") == "180000"
        assert store.value("requires_sponsorship") == "no"
        assert store.value("years_experience") == "4"

        store.set_many({"expected_salary": "180k"})
        assert store.value("expected_salary") == "180000"
        store.set_many({"expected_salary": "180000"})

        # 3. Bad input is rejected with a reason, and is NOT stored. Silently
        #    keeping a mistyped phone number is worse than dropping it, because
        #    the failure would only show up on a submitted application.
        report = store.set_many({
            "phone": "123",
            "requires_sponsorship": "maybe",
            "expected_salary": "about 180k",
        })
        assert len(report["warnings"]) == 3
        assert report["applied"] == {}
        assert store.value("phone") == "+1 555 010 4477"

        # 4. An empty value clears a field. That is how a user retracts an
        #    answer, so it must not read as "leave it alone".
        store.set_many({"years_experience": ""})
        assert store.value("years_experience") == ""

        # 5. The file is markdown a human can edit, and it round-trips. Blank
        #    fields carry guidance in an HTML comment, which must not leak into
        #    the parsed value.
        text = store.render()
        assert "- phone: +1 555 010 4477" in text
        assert "<!--" in text, "blank fields should explain themselves in the file"
        assert parse_profile(text)["phone"] == "+1 555 010 4477"

        reloaded = ProfileStore(path)
        assert reloaded.value("phone") == "+1 555 010 4477"
        assert reloaded.value("expected_salary") == "180000"

        # 6. A hand-added key is preserved, not dropped, and is reported rather
        #    than silently ignored.
        reloaded.set_many({"security_clearance": "TS/SCI"})
        assert reloaded.value("security_clearance") == "TS/SCI"
        assert "security_clearance" in reloaded.unknown_keys()
        assert "security_clearance" in reloaded.render()
        assert reloaded.get()["security_clearance"] == "TS/SCI"

        # 7. The questionnaire drives setup, so it must expose what is missing
        #    and stay quiet about what is already known.
        questions = reloaded.questionnaire(include_optional=False, only_missing=True)
        asked = {f["key"] for g in questions for f in g["fields"]}
        assert "phone" not in asked, "an answered field must not be re-asked"
        assert "resume_path" in asked
        assert all(f["question"] for g in questions for f in g["fields"])

        # 8. Groups with nothing required in them are skippable as a block, which
        #    is what keeps a 30-odd-field questionnaire bearable.
        by_group = {g["group"]: g for g in reloaded.questionnaire()}
        assert by_group["demographics"]["skippable"] is True
        assert by_group["identity"]["skippable"] is False

        # 9. The committed example is generated from the same spec, so it cannot
        #    drift away from what the code actually asks for.
        example = render_example()
        for key in REQUIRED_KEYS:
            assert f"`{key}`" in example


def test_profile_migration():
    """A profile that predates profile.md must not be asked for twice."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        memory_path = tmp / "memory.json"
        memory_path.write_text(json.dumps({
            "version": 3,
            "profile": {"name": "Legacy Person", "location": "Boston, MA"},
        }))

        store = ProfileStore(tmp / "profile.md")
        moved = migrate_legacy_profile(store, memory_path)

        assert moved == 2
        assert store.value("name") == "Legacy Person"
        assert store.value("location") == "Boston, MA"

        # The staged copy is removed. A leftover duplicate is worse than none:
        # a later reader would have to guess which file is authoritative.
        assert json.loads(memory_path.read_text())["profile"] == {}

        # Idempotent, and an existing value is never overwritten by the legacy
        # one -- that is what makes it safe to run on every startup.
        store.set_many({"name": "Corrected Name"})
        assert migrate_legacy_profile(store, memory_path) == 0
        assert store.value("name") == "Corrected Name"

        # And a store reached through MemoryStore sees the same thing, since both
        # entry points share this one function.
        memory_path.write_text(json.dumps({
            "version": 3, "profile": {"phone": "+1 555 0100"},
        }))
        merged = MemoryStore(memory_path)
        assert merged.get_profile()["phone"] == "+1 555 0100"
        assert merged.get_profile()["name"] == "Corrected Name"
        assert json.loads(memory_path.read_text())["profile"] == {}


def test_guardrails_rails():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        memory = MemoryStore(tmp / "memory.json")
        rails = Guardrails(
            path=tmp / "guard_state.json",
            memory=memory,
            daily_cap=1,
            min_gap=(0.0, 0.0),
            max_consecutive_failures=3,
        )

        # 1. A fresh run is allowed, and the cap is reported honestly.
        decision = rails.preflight("https://linkedin.com/jobs/view/111")
        assert decision.allowed is True
        assert decision.remaining_today == 1

        # 2. Submitting requires a token issued for this job.
        ok, message = rails.consume_confirmation("no-such-token")
        assert ok is False
        assert "request_submit_confirmation" in message

        confirmation = rails.request_submit_confirmation("summary text", "https://linkedin.com/jobs/view/111")
        assert confirmation.summary == "summary text"

        # 2b. Peeking must not spend the token -- otherwise a caller that simply
        #     forgot to acknowledge would burn the approval it still needs.
        assert rails.peek_confirmation(confirmation.id).used is False

        ok, summary = rails.consume_confirmation(confirmation.id)
        assert ok is True
        assert summary == "summary text"

        # 2c. Single use. A replay is refused.
        ok, message = rails.consume_confirmation(confirmation.id)
        assert ok is False
        assert "already used" in message

        # 3. Daily cap. Recorded success consumes the only slot of the day.
        rails.record_outcome(success=True)
        assert rails.preflight("https://linkedin.com/jobs/view/222").allowed is False
        assert "daily cap reached (1/1)" in rails.preflight("https://linkedin.com/jobs/view/222").reason

        # 4. Deduplication is keyed on the job id, so the same posting reached
        #    through a different URL is still refused.
        memory.add_application(
            job_url="https://linkedin.com/jobs/view/333",
            job_title="Engineer",
            company="Acme",
            platform="LinkedIn",
            status="applied",
        )
        deduped = rails.preflight("https://www.linkedin.com/jobs/view/333/?trackingId=xyz")
        assert deduped.allowed is False
        assert deduped.already_applied is True

        # 5. Circuit breaker: three consecutive failures halt the run, and the
        #    halt is reported ahead of the cap so the reason names the real cause.
        for _ in range(3):
            rails.record_outcome(success=False, note="selector not found")
        halted = rails.preflight("https://linkedin.com/jobs/view/444")
        assert halted.allowed is False
        assert "consecutive failures" in halted.reason

        # 5b. Clearing the breaker resumes the run but must not also hand back
        #     spent daily quota -- the two rails are independent.
        rails.reset_halt()
        after_reset = rails.preflight("https://linkedin.com/jobs/view/444")
        assert "consecutive failures" not in after_reset.reason
        assert "daily cap reached" in after_reset.reason

        # 5c. A passing day does restore capacity. Written through a
        #     transaction, because every rail read now comes from the file:
        #     poking the in-memory state directly is discarded by the next
        #     read, which is precisely what stops two live processes from each
        #     believing they hold the whole day's quota.
        with rails._transaction():
            rails._state.applied_today = 0
        assert rails.preflight("https://linkedin.com/jobs/view/444").allowed is True


def test_daily_cap_override():
    """A raised cap must be deliberate, dated, and self-expiring."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        state_path = tmp / "guard_state.json"
        rails = Guardrails(
            path=state_path,
            memory=MemoryStore(tmp / "memory.json"),
            daily_cap=1,
            min_gap=(0.0, 0.0),
        )

        # 1. Nothing changes without a human act.
        assert rails.effective_daily_cap == 1
        assert rails.stats()["cap_override"] is None

        rails.record_outcome(success=True)
        assert rails.preflight("https://linkedin.com/jobs/view/555").allowed is False

        # 2. Raising it for today reopens the day, and the move is on the record.
        assert rails.set_daily_cap(3, "a batch of 29 candidates is ready now") == 3
        assert rails.preflight("https://linkedin.com/jobs/view/555").allowed is True
        override = rails.stats()["cap_override"]
        assert override["cap"] == 3
        assert "batch of 29" in override["reason"]
        assert rails.stats()["base_daily_cap"] == 1

        # 3. A cap that cannot be reasoned about is not accepted.
        try:
            rails.set_daily_cap(0, "nope")
        except ValueError:
            pass
        else:
            raise AssertionError("a zero cap should be refused")

        # 4. It expires on its own at the next day boundary rather than quietly
        #    becoming the new normal -- tomorrow's cap is the coded default.
        #    Forced by back-dating the override inside a transaction: the rails
        #    re-read on every access now, so an edit outside one would not
        #    survive to be seen by the assertion below.
        with rails._transaction():
            rails._state.daily_cap_override_day = "2000-01-01"
            rails._roll_over_if_new_day()
        assert rails.effective_daily_cap == 1
        assert rails.stats()["cap_override"] is None

        # 5. The exception survives a reload of the state file, so a daemon that
        #    started earlier still honours it on its next pass.
        rails.set_daily_cap(7, "second window")
        assert Guardrails(path=state_path, memory=MemoryStore(tmp / "m2.json")).effective_daily_cap == 7


def test_route_knowledge():
    """An application is not always an Easy Apply, and the memory must know that.

    The selector layer assumes the form lives on the page you opened. This is
    the other half: routes that leave the page, sit behind an account gate, and
    stop at a step no machine can pass alone.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "routes.json"
        store = MemoryStore(path)

        # 1. The seeds are installed before anything has ever been run. That is
        #    what gives a first attempt on an unfamiliar route a map instead of
        #    a blank page.
        keys = store.list_routes()
        assert "Amazon/external_ats" in keys
        assert "LinkedIn/easy_apply" in keys

        amazon = store.get_route("Amazon", "external_ats")
        assert amazon.runs == 0, "a seeded route carries no evidence"
        assert amazon.success_rate == 0.0
        assert amazon.hardest_gate == "", "no evidence must not read as no failure"
        assert amazon.human_gates, "the sign-in gate must be declared up front"
        assert any("passport.amazon.jobs" in s.detail for s in amazon.steps)
        # The one selector the seed is allowed to name, because it was observed.
        assert "#preLoginEmailField" in {s.selector for s in amazon.steps}

        # 2. A URL is routed to the right platform's routes, and an unknown
        #    employer still gets the generic shape rather than an empty answer.
        assert [
            r.key for r in store.routes_for_url("https://www.amazon.jobs/en/jobs/10529830")
        ] == ["Amazon/external_ats"]
        assert [r.key for r in store.routes_for_url("https://careers.acme.example/jobs/9")] == [
            "Generic/external_ats"
        ]

        # 3. A real attempt votes through add_application. A route that is only
        #    ever reported when it breaks would read as one that never succeeds,
        #    so the outcome is counted on the same path as the platform's.
        store.add_application(
            job_url="https://www.amazon.jobs/en/jobs/10529830",
            job_title="Software Development Engineer, Early Career",
            company="Audible",
            platform="Amazon",
            apply_route="external_ats",
            status="applied",
        )
        amazon = store.get_route("Amazon", "external_ats")
        assert (amazon.runs, amazon.successes) == (1, 1)
        assert amazon.success_rate == 1.0

        # 4. A blockage names the step, which is the sentence an adapter gets
        #    written from -- and it accumulates rather than overwriting.
        store.record_route_blockage(
            "Amazon", "external_ats", "sign-in wall", notes="code never arrived"
        )
        store.record_route_blockage("Amazon", "external_ats", "sign-in wall")
        store.record_route_blockage("Amazon", "external_ats", "resume parse")
        amazon = store.get_route("Amazon", "external_ats")
        assert amazon.hardest_gate == "sign-in wall"
        assert amazon.blocked_at["sign-in wall"] == 2

        # 5. It survives a reload, and a prior never overwrites a route that has
        #    actually been run -- re-seeding on top of measured history is the
        #    same mistake as resetting a learned answer to its default.
        reloaded = MemoryStore(path).get_route("Amazon", "external_ats")
        assert reloaded.runs == 1
        assert reloaded.blocked_at["sign-in wall"] == 2
        assert reloaded.notes == "code never arrived"

        # 6. It reaches the harness as prose, with the human gate called out --
        #    the point of knowing a route is to warn before the wall, not after.
        text = store.get_route_hints_text("Amazon", "external_ats")
        assert "passport.amazon.jobs" in text
        assert "[needs the human]" in text
        assert "sign-in wall" in text

        # 7. The flywheel reports routes next to platforms, because a platform
        #    can look healthy while its only ever-run route is the easy one.
        routes = {r["route"]: r for r in store.get_stats()["routes"]}
        assert routes["Amazon/external_ats"]["runs"] == 1
        assert routes["Amazon/external_ats"]["hardest_gate"] == "sign-in wall"
        assert routes["LinkedIn/easy_apply"]["runs"] == 0


def test_route_backfill_from_history():
    """A route with applications on record must not report `runs: 0`.

    `runs: 0` is the flywheel's word for "never tried". Leaving it at zero while
    the history already holds the applications is the exact signal confusion the
    counters exist to prevent -- the same failure mode as a hit rate that is 0
    because nothing was ever attempted.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "memory.json"
        path.write_text(json.dumps({
            "version": 3,
            "application_history": [
                {"job_url": "https://linkedin.com/jobs/view/1", "platform": "LinkedIn",
                 "apply_route": "easy_apply", "status": "applied"},
                {"job_url": "https://linkedin.com/jobs/view/2", "platform": "LinkedIn",
                 "apply_route": "easy_apply", "status": "applied"},
                {"job_url": "https://linkedin.com/jobs/view/3", "platform": "LinkedIn",
                 "apply_route": "easy_apply", "status": "failed"},
            ],
        }))

        store = MemoryStore(path)
        route = store.get_route("LinkedIn", "easy_apply")
        assert (route.runs, route.successes) == (3, 2)

        # The prior still supplies the shape the history could never carry, so
        # backfilling a counter must not cost us the seeded steps.
        assert len(route.steps) == 4
        assert route.human_gates == []

        # And the migration is not re-applied on the next load, which would
        # multiply every counter it touched.
        again = MemoryStore(path).get_route("LinkedIn", "easy_apply")
        assert (again.runs, again.successes) == (3, 2)

        # A process still running the pre-schema-4 code writes the file back
        # with the old version, so this migration can legitimately run twice on
        # one file. When the routes survived, re-running it must not stack the
        # history on top of counts that already include it.
        doc = json.loads(path.read_text())
        doc["version"] = 3
        assert doc["routes"]["LinkedIn/easy_apply"]["runs"] == 3
        path.write_text(json.dumps(doc))
        third = MemoryStore(path).get_route("LinkedIn", "easy_apply")
        assert (third.runs, third.successes) == (3, 2), "backfill must be idempotent"
        assert len(third.steps) == 4


if __name__ == "__main__":
    test_platform_detector()
    test_locator_refs()
    test_memory_store()
    test_route_knowledge()
    test_route_backfill_from_history()
    test_profile_store()
    test_profile_migration()
    test_guardrails_rails()
    test_daily_cap_override()
    print("ALL TESTS PASSED!")
