"""Blocker 2 -- Yes/No questions on one page answer each other.

Repro, as reported: the filler looked at the page as a whole, saw the word
"sponsor" somewhere, and then treated *every* bare Yes/No option on that page as
the sponsorship question. On a real screening page that silently puts the
sponsorship answer into unrelated questions -- and because the branch `continue`d
past everything else, a question that had no answer at all was never reported
either, so the form looked complete and the ATS rejected it at the end.

Two properties are tested here:

1. **Three groups, three answers.** Same page, different questions, different
   answers (Yes / No / Yes) -- each group must end up with its own.
2. **A missing answer parks the application**, naming the question. It is never
   filled with a neighbouring answer and never silently skipped.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from applyops.answers import AnswerStore
from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.filling import fill_application_form, resume_for_fill
from applyops.memory import MemoryStore

#: What the user said, per question. The sponsorship answer is the one the old
#: heuristic would have copied into all three.
USER_ANSWERS = {
    "Are you legally authorised to work in the United States?": "Yes",
    "Have you previously worked at ApplyOps Demo Co?": "Yes",
}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-blocker2-"))


def _setup(root: Path):
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "requires_sponsorship": "no",  # the sponsorship question's own answer
            "resume_path": str(write_sample_resume(root / "resume.pdf")),
        }
    )
    answers = AnswerStore(root)
    for question, answer in USER_ANSWERS.items():
        answers.set_answer(question, answer)
    return memory, answers


@pytest.mark.asyncio
async def test_three_yes_no_questions_do_not_share_an_answer():
    root = _tmp()
    memory, answers = _setup(root)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await browser.goto(f"{ats.url}/choices", settle=0.6)
            report = await fill_application_form(
                browser,
                memory=memory,
                answers=answers,
                resume=resume_for_fill(memory),
                application_id="app-1",
            )

            # The sponsorship question is not in USER_ANSWERS; it is answered from
            # the profile, and only that question may use it.
            assert report.ready, report.to_dict()

            received = await browser.page.evaluate(
                """() => {
                    const out = {};
                    for (const el of document.querySelectorAll("input[type=radio]:checked")) {
                        out[el.name] = el.value;
                    }
                    return out;
                }"""
            )
            assert received["work_authorised"] == "yes", received
            assert received["needs_sponsorship"] == "no", received
            assert received["worked_here_before"] == "yes", received
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_question_without_an_answer_parks_and_is_named():
    """No answer for a question means a stop, not a guess and not silence."""
    root = _tmp()
    memory, answers = _setup(root)
    # Take away one answer after the fact: the user simply never answered it.
    answers.withdraw(answers.resolve("Have you previously worked at ApplyOps Demo Co?").id)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await browser.goto(f"{ats.url}/choices", settle=0.6)
            report = await fill_application_form(
                browser,
                memory=memory,
                answers=answers,
                resume=resume_for_fill(memory),
                application_id="app-1",
            )

            assert report.ready is False, report.to_dict()
            # The unanswered question is reported *by its question*, not skipped
            # and not answered with the work-authorisation answer.
            reported = " | ".join(report.unfilled_required)
            assert "previously worked" in reported.lower(), report.to_dict()

            checked = await browser.page.evaluate(
                """() => {
                    const out = {};
                    for (const el of document.querySelectorAll("input[type=radio]:checked")) {
                        out[el.name] = el.value;
                    }
                    return out;
                }"""
            )
            assert "worked_here_before" not in checked, checked
            # And the questions that *were* answered kept their own answers.
            assert checked["work_authorised"] == "yes", checked
            assert checked["needs_sponsorship"] == "no", checked
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_partial_submission_never_reaches_the_ats():
    """The consequence of the bug: an incomplete form must not be sent."""
    root = _tmp()
    memory, answers = _setup(root)
    answers.withdraw(answers.resolve("Have you previously worked at ApplyOps Demo Co?").id)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await browser.goto(f"{ats.url}/choices", settle=0.6)
            report = await fill_application_form(
                browser,
                memory=memory,
                answers=answers,
                resume=resume_for_fill(memory),
                application_id="app-1",
            )
            assert report.ready is False

            # The form is invalid in the browser, so nothing was posted at all.
            await browser.click(name="Submit application")
            assert ats.last_submission == {}
        finally:
            await browser.close()
