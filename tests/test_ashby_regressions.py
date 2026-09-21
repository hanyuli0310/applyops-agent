"""Regressions from one Ashby application that failed to submit three times.

Each test here exists because the live form did the wrong thing, and the wrong
thing was invisible: the report said the form was filled while a required
question sat empty. They are kept together so the shape of the failure stays
legible -- a required question that is neither answered nor reported.
"""

from __future__ import annotations

import json

import pytest

from applyops.answers import AnswerStore, classify_question, tidy_question
from applyops.filling import (
    SPONSORSHIP_PATTERN,
    _answer_selects_option,
    choice_answer,
)
from applyops.memory import MemoryStore
from applyops.service import ApplicationService

# The multi-select as the applicant answered it: several options in one answer,
# and every option's own text is full of commas.
MULTI_ANSWER = (
    "Local storage (SQLite, IndexedDB, or filesystem), "
    "Performance profiling or memory optimization, "
    "Native OS features (notifications, file system access, deep linking, auto-updates)"
)

WANTED = (
    "Local storage (SQLite, IndexedDB, or filesystem)",
    "Native OS features (notifications, file system access, deep linking, auto-updates)",
    "Performance profiling or memory optimization",
)
NOT_WANTED = (
    "IPC communication between main and renderer processes",
    "None of the above",
)


@pytest.mark.parametrize("option", WANTED)
def test_multi_select_answer_selects_each_option(option):
    """A multi-select answer names several options at once.

    Matched with `==`, none of them matched, every option was skipped, and the
    skip was silent -- a required multi-select shipped blank while the report
    claimed success.
    """
    assert _answer_selects_option(MULTI_ANSWER, option) is True


@pytest.mark.parametrize("option", NOT_WANTED)
def test_multi_select_answer_does_not_select_other_options(option):
    assert _answer_selects_option(MULTI_ANSWER, option) is False


def test_option_text_may_contain_commas():
    """The reason the answer is not split on commas.

    Splitting it does not separate the options -- it tears the options apart.
    """
    single = "Local storage (SQLite, IndexedDB, or filesystem)"
    assert _answer_selects_option(single, single) is True


def test_single_choice_does_not_select_its_siblings():
    answer = "C. I have experimented with Electron or desktop apps, but not shipped production software"
    assert _answer_selects_option(answer, answer) is True
    assert _answer_selects_option(answer, "A. I have built and shipped Electron") is False


def test_ashby_visa_question_is_a_sponsorship_question():
    """Ashby words it "Do you need a Work VISA…" -- without the word sponsor.

    Unclassified, it could not inherit the sponsorship answer the applicant had
    already given, and a required Yes/No blocked the submit.
    """
    question = "Do you need a Work VISA to work in the country where this job is located?"
    assert classify_question(question) == "sponsorship"
    assert SPONSORSHIP_PATTERN.search(question)


def test_work_authorization_is_not_answered_from_sponsorship():
    """A different question that happens to want the same letter.

    Answering "authorized to work" from `requires_sponsorship` would be right
    only by accident, so the pattern must not claim it.
    """
    assert not SPONSORSHIP_PATTERN.search(
        "Are you legally authorized to work in the United States?"
    )


def test_flywheel_answers_reach_the_answer_store(tmp_path):
    """Answers accumulated in memory.json used to be unreachable.

    The store read one file, the flywheel wrote another, and a question the
    applicant had answered many times was reported as unanswered on a form that
    merely worded it differently.
    """
    (tmp_path / "memory.json").write_text(
        json.dumps(
            {
                "learned_qa": [
                    {
                        "id": "qa-1",
                        "question": "Will you now or in the future require sponsorship "
                        "for employment visa status?",
                        "answer": "Yes",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = AnswerStore(tmp_path)

    reworded = "Do you need a Work VISA to work in the country where this job is located?"
    entry = store.resolve(reworded)

    assert entry is not None, "a reworded sponsorship question must inherit the answer"
    assert entry.answer == "Yes"
    assert entry.scope == "global"


def test_a_scoped_answer_still_beats_the_flywheel(tmp_path):
    """Reading the flywheel through must not let it outrank a scoped answer."""
    (tmp_path / "memory.json").write_text(
        json.dumps(
            {
                "learned_qa": [
                    {"id": "qa-1", "question": "Do you require sponsorship?", "answer": "Yes"}
                ]
            }
        ),
        encoding="utf-8",
    )
    store = AnswerStore(tmp_path)
    store.set_answer("Do you require sponsorship?", "No", scope="global")

    assert store.resolve("Do you require sponsorship?").answer == "No"


def test_a_malformed_flywheel_row_does_not_hide_the_rest(tmp_path):
    (tmp_path / "memory.json").write_text(
        json.dumps(
            {
                "learned_qa": [
                    {"id": "broken", "question": "", "answer": "Yes"},
                    {"id": "good", "question": "Do you require sponsorship?", "answer": "Yes"},
                ]
            }
        ),
        encoding="utf-8",
    )
    store = AnswerStore(tmp_path)
    assert store.resolve("Do you require sponsorship?").answer == "Yes"


def test_a_retracted_blockage_stays_retracted(tmp_path):
    """Clearing a gate used to be undone by the next save.

    The merge rebuilds `blocked_at` from disk by taking the largest count, so
    dropping the entry in memory alone resurrected it on save -- a route that
    had been walked to the end kept announcing a wall that was not there, and
    the next run stopped in front of an open door.
    """
    store_path = tmp_path / "memory.json"
    mem = MemoryStore(store_path)
    mem.record_route_blockage("Ashby", "external_ats", "captcha required")
    assert MemoryStore(store_path).get_route("Ashby", "external_ats").blocked_at

    mem.clear_route_blockage("Ashby", "external_ats", "captcha required")
    reloaded = MemoryStore(store_path).get_route("Ashby", "external_ats")

    assert "captcha required" not in reloaded.blocked_at
    assert "captcha required" in reloaded.retracted_at  # retracted, with the count it covers


def test_new_evidence_of_a_blockage_overrides_an_old_retraction(tmp_path):
    """Neither kind of evidence is permanent; the most recent one wins."""
    store_path = tmp_path / "memory.json"
    mem = MemoryStore(store_path)
    mem.record_route_blockage("Ashby", "external_ats", "captcha required")
    mem.clear_route_blockage("Ashby", "external_ats", "captcha required")

    mem.record_route_blockage("Ashby", "external_ats", "captcha required")
    reloaded = MemoryStore(store_path).get_route("Ashby", "external_ats")

    # The new blockage outgrows the retraction: it forgave everything up to
    # count 1, and this one is counted 2.
    assert reloaded.blocked_at.get("captcha required") == 2
    assert reloaded.retracted_at.get("captcha required") == 1


def test_an_old_shaped_routes_file_still_loads(tmp_path):
    """A shape change must never make the memory file unreadable.

    `retracted_at` was briefly written as a list; a model accepting only the
    newer mapping could not parse such a file, and an unreadable memory file is
    treated as no memory at all -- every accumulated answer and every recorded
    route gone in a single read. This is that file, and it must still open.
    """
    store_path = tmp_path / "memory.json"
    store_path.write_text(
        json.dumps(
            {
                "learned_qa": [{"id": "qa-1", "question": "Do you require sponsorship?", "answer": "Yes"}],
                "routes": {
                    "Ashby/external_ats": {
                        "platform": "Ashby",
                        "route": "external_ats",
                        "steps": [{"ordinal": 1, "kind": "open", "detail": "open the posting"}],
                        "blocked_at": {"captcha required": 3},
                        "retracted_at": ["captcha required"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    mem = MemoryStore(store_path)

    assert len(mem._data.learned_qa) == 1  # not an empty memory
    route = mem.get_route("Ashby", "external_ats")
    assert route.steps, "the recorded journey must survive"
    assert "captcha required" not in route.blocked_at  # still retracted
    # The retraction forgives the three blockages counted so far; a fourth would stand.
    assert route.retracted_at == {"captcha required": 3}


def test_saving_preserves_blockage_history(tmp_path):
    """The counts are the evidence; a derived field must not eat them.

    Without seeding `blockage_counts` from the older `blocked_at`, the first
    save recomputed the derived value from an empty record and discarded every
    gate the route was known to die at.
    """
    store_path = tmp_path / "memory.json"
    store_path.write_text(
        json.dumps(
            {
                "routes": {
                    "LinkedIn/easy_apply": {
                        "platform": "LinkedIn",
                        "route": "easy_apply",
                        "blocked_at": {"no form on this page": 2},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    mem = MemoryStore(store_path)
    mem.set_profile("name", "Hanyu Li")  # forces a save

    reloaded = MemoryStore(store_path).get_route("LinkedIn", "easy_apply")
    assert reloaded.blocked_at.get("no form on this page") == 2
    assert reloaded.blockage_counts.get("no form on this page") == 2


def test_enqueue_refuses_a_senior_title(tmp_path):
    """Senior postings must not enter the queue.

    The check existed in `preferences.evaluate` but nothing called it, so
    senior roles were enqueued like any other and had to be cleared by hand
    every time.
    """
    svc = ApplicationService(tmp_path, memory=MemoryStore(tmp_path / "memory.json"))

    with pytest.raises(Exception) as excinfo:
        svc.enqueue(
            job_url="https://example.com/jobs/1",
            job_id="senior-1",
            title="Senior Software Engineer (AI Agents)",
            company="Probe",
        )
    assert "not entry level" in str(excinfo.value)


def test_enqueue_accepts_an_entry_level_title(tmp_path):
    svc = ApplicationService(tmp_path, memory=MemoryStore(tmp_path / "memory.json"))
    row = svc.enqueue(
        job_url="https://example.com/jobs/2",
        job_id="entry-1",
        title="Software Engineer",
        company="Probe",
    )
    assert row.title == "Software Engineer"


Q_EDUCATION = "Have you completed the following level of education: Bachelor's Degree?"


def test_tidy_question_strips_form_chrome():
    """"... Degree? Required" and the question rendered twice are one question.

    LinkedIn renders the question twice inside the group and appends its own
    "Required" marker, so the lookup key never matched a stored answer and a
    question the applicant had answered many times came back unanswered.
    """
    assert tidy_question(f"{Q_EDUCATION} Required") == Q_EDUCATION
    assert tidy_question(Q_EDUCATION + Q_EDUCATION) == Q_EDUCATION
    assert tidy_question(Q_EDUCATION + Q_EDUCATION + " Required") == Q_EDUCATION


def test_a_noisy_question_still_finds_its_answer(tmp_path):
    store = AnswerStore(tmp_path)
    store.set_answer(Q_EDUCATION, "Yes", scope="global")

    noisy = f"{Q_EDUCATION}{Q_EDUCATION} Required"
    entry = store.resolve(noisy)

    assert entry is not None
    assert entry.answer == "Yes"


def test_background_check_falls_back_to_the_profile():
    """"Willing to undergo a background check?" is answered by the profile.

    It was reported unanswered on a form that asks it everywhere, while the
    profile had held `background_check_ok: yes` all along.
    """
    profile = {"background_check_ok": "yes"}
    question = (
        "Are you willing to undergo a background check, in accordance with "
        "local law/regulations?"
    )
    assert classify_question(question) == "background_check"
    assert choice_answer(question, profile=profile, answers=None) == (
        True,
        "profile:background_check_ok",
    )
