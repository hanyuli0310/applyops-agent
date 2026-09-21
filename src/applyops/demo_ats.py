"""A local, deterministic Applicant Tracking System -- for tests and demos.

Everything the safety machinery has to prove, it has to prove somewhere: a
submit that succeeds, a submit that never confirms, a submit that visibly fails,
a form that carries a stale resume before we touch it, a control that silently
refuses a value. Doing that against a real employer's site would mean real
applications, sent against real terms of service, by a thing that is trying to
find out whether it accidentally sent them.

This server is that place. It is deliberately boring:

- **stdlib only**, so tests never depend on a server framework being installed.
- **Deterministic**: the same request always produces the same page. No
  randomness, no clock-dependent choices, no network.
- **Local and ephemeral**: it binds ``127.0.0.1`` on an OS-chosen port, and is
  meant to be started and stopped inside a single test.
- **Honest about being fake**: every page says it is the demo ATS, so nobody can
  mistake a green test for a real submission.

It is shipped in the package rather than in `tests/` because it is also the
target of `applyops demo`: the same surface a new user walks through is the one
the safety tests exercise.
"""

from __future__ import annotations

import html
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, quote, urlparse

CONFIRMED_TEXT = "Application received"
FAILED_TEXT = "There was a problem with your submission"

#: What a real ATS refuses to accept an application without. The demo validates
#: these for the same reason: an empty form that "succeeds" would let a broken
#: filler look green, which is exactly the bug this server has to be able to
#: catch.
REQUIRED_FIELDS = ("name", "email", "phone", "years", "notice_period", "needs_sponsorship")
REQUIRED_FILE_FIELD = "resume"

#: Three independent Yes/No questions on one page, each in its own fieldset with
#: a legend. They exist so that "answer the sponsorship question" cannot be
#: mistaken for "answer every Yes/No on the page" -- the two failure modes this
#: fixture is built to catch are cross-answering and silently skipping.
CHOICE_QUESTIONS = {
    "work_authorised": (
        "Are you legally authorised to work in the United States?",
        "yes",
    ),
    "needs_sponsorship": (
        "Do you now, or will you in the future, require visa sponsorship?",
        "no",
    ),
    "worked_here_before": ("Have you previously worked at ApplyOps Demo Co?", "yes"),
}


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, str]]:
    """Pull fields and filenames out of a multipart/form-data body.

    Minimal on purpose -- enough to prove what actually arrived, and nothing
    more. A parser that was clever about encodings would be a place for the
    verification to hide a mistake.
    """
    fields: dict[str, str] = {}
    files: dict[str, str] = {}
    if "boundary=" not in (content_type or ""):
        return fields, files
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    delimiter = b"--" + boundary.encode()
    for part in body.split(delimiter):
        if not part.strip() or part.strip() == b"--":
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        headers = head.decode("utf-8", "ignore")
        payload = payload.rstrip(b"\r\n-")
        name_match = re.search(r'name="([^"]*)"', headers)
        name = name_match.group(1) if name_match else ""
        if not name:
            continue
        filename_match = re.search(r'filename="([^"]*)"', headers)
        if filename_match:
            if filename_match.group(1):
                files[name] = filename_match.group(1)
        else:
            fields[name] = payload.decode("utf-8", "ignore").strip()
    return fields, files


def validate_submission(fields: dict[str, str], files: dict[str, str]) -> list[str]:
    """What is wrong with this application, as a list a human can read."""
    problems = [
        f"missing or empty field: {name}"
        for name in REQUIRED_FIELDS
        if not fields.get(name)
    ]
    if not files.get(REQUIRED_FILE_FIELD):
        problems.append("no resume file attached")
    return problems

#: What the demo says when the same file has been attached before.
STALE_RESUME_NAME = "resume-v1-old.pdf"

#: How long the "slow" scenario hangs. Long enough that nothing in a test's
#: lifetime can mistake it for an answer, short enough not to leak a thread all
#: afternoon.
SLOW_DELAY_SECONDS = 45


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 3rem auto; max-width: 40rem; }}
 label {{ display: block; margin: 0.75rem 0 0.25rem; }}
 input, select {{ width: 100%; padding: 0.5rem; }}
 .banner {{ background: #eef6ee; border: 1px solid #9ac; padding: 0.75rem; margin-bottom: 1rem; }}
 .error {{ background: #fdeaea; border: 1px solid #d99; }}
</style></head>
<body>{body}</body></html>"""


def _index(base: str) -> str:
    links = "".join(
        f"<li><a href='{base}{path}'>{label}</a></li>"
        for path, label in (
            ("/form", "Standard form (submit succeeds)"),
            ("/form?scenario=slow", "Submit that never confirms (hangs)"),
            ("/form?scenario=error", "Submit that reports failure"),
            ("/form?scenario=stale", "Form already carrying a stale resume"),
        )
    )
    return _page(
        "Demo ATS",
        f"<div class='banner'>This is the local ApplyOps demo ATS. Nothing here is a real employer.</div>"
        f"<h1>Demo ATS</h1><ul>{links}</ul>",
    )


def _choices_form() -> str:
    """Three Yes/No groups, each bound to its own question by a fieldset legend."""
    groups = []
    for name, (question, _expected) in CHOICE_QUESTIONS.items():
        groups.append(
            f"<fieldset><legend>{html.escape(question)}</legend>"
            f"<label><input type='radio' name='{name}' value='yes' required> Yes</label>"
            f"<label><input type='radio' name='{name}' value='no' required> No</label>"
            "</fieldset>"
        )
    return _page(
        "Screening — Demo ATS",
        "<div class='banner'>Three short screening questions. Each one is "
        "independent; the form does not accept a partial answer.</div>"
        "<h1>Screening questions</h1>"
        "<form method='post' action='/submit' enctype='multipart/form-data'>"
        "<label for='name'>Full name</label>"
        "<input id='name' name='name' type='text' required>"
        + "".join(groups)
        + "<label for='resume'>Resume</label>"
        "<input id='resume' name='resume' type='file' required>"
        "<button type='submit'>Submit application</button>"
        "</form>",
    )


def validate_choices(fields: dict[str, str], files: dict[str, str]) -> list[str]:
    """Every question answered, with a value the form actually offers."""
    problems: list[str] = []
    if not fields.get("name"):
        problems.append("missing or empty field: name")
    for name in CHOICE_QUESTIONS:
        value = (fields.get(name) or "").strip().lower()
        if value not in {"yes", "no"}:
            problems.append(f"question {name!r} was not answered")
    if not files.get(REQUIRED_FILE_FIELD):
        problems.append("no resume file attached")
    return problems


def _offsite_posting(ats_host: str, local_target: str = "", decoy: bool = False) -> str:
    """A posting that is NOT Easy Apply: the apply control leaves for another site.

    Shaped after the real thing, down to the redirect wrapper LinkedIn uses --
    `<a href="https://www.linkedin.com/safety/go/?url=<encoded ATS url>">Apply</a>`
    -- because that wrapper is the only signal on the page that says "the
    application is not here".
    """
    if local_target:
        # Same redirect *shape* as LinkedIn's, on our own host -- a fixture that
        # pointed at the real linkedin.com would click out to the internet.
        wrapped = local_target
    else:
        target = "https://boards.greenhouse.io/acme/jobs/7742875"
        wrapped = f"https://www.linkedin.com/safety/go/?url={quote(target, safe='')}"
    # LinkedIn's own footer is full of "Apply"-ish links to other LinkedIn hosts,
    # and they come *before* the posting's control in the DOM. They are not the
    # employer, and following one is how a real walk got stuck on LinkedIn.
    footer = (
        "<footer><a href='https://business.linkedin.com/advertise'>Apply</a>"
        "<a href='https://safety.linkedin.com/'>Apply with safety</a></footer>"
        if decoy
        else ""
    )
    return _page(
        "Backend Engineer — Acme",
        "<div class='banner'>Local demo posting. The application lives on another "
        "site, exactly like a non-Easy-Apply LinkedIn posting.</div>"
        f"{footer}"
        "<h1>Backend Engineer</h1>"
        "<p>Acme · Remote (US)</p>"
        f"<a href='{wrapped}'>Easy Apply</a>"
        "<p>No application form on this page.</p>",
    )


def _onsite_posting() -> str:
    """A posting whose Easy Apply control is a link to this site's own apply page.

    This is what a real LinkedIn Easy Apply posting looks like: the form is not
    on the posting page, and the control is a same-host link (`/apply`), not a
    modal and not a redirect off-site.
    """
    return _page(
        "Easy Apply — Demo ATS",
        "<div class='banner'>Local demo posting. The application form is one click "
        "away, on this same site.</div>"
        "<h1>Backend Engineer</h1><p>Ordinary Co · Remote (US)</p>"
        "<a href='/apply'>Easy Apply</a>",
    )


def _form(scenario: str) -> str:
    """One application form, with variations selected by `scenario`.

    The fields cover the interesting cases rather than looking plausible:

    - `phone` is text and behaves normally.
    - `years` is `<input type="number">`: typing letters into it is silently
      dropped by the browser, which is exactly the "expected non-empty, read back
      empty" case that must never be reported as success.
    - `notice_period` is a select, whose selected value is what gets read back.
    - `needs_sponsorship` is a radio group -- the sensitive kind that must never
      be answered by inference.
    - `resume` is the attachment, and in the `stale` scenario the page also offers
      a previously uploaded file, pre-selected.

    The `scenario` also selects what the *response* does: `standard` confirms,
    `silent` accepts without confirming (the honest "we do not know" case),
    `error` refuses, `slow` never answers, `stale` offers an old attachment.
    """
    stale_block = ""
    if scenario == "stale":
        stale_block = (
            "<label for='existing'>Use a previously uploaded resume</label>"
            "<select id='existing' name='existing_resume'>"
            f"<option value='{STALE_RESUME_NAME}' selected>{STALE_RESUME_NAME} (uploaded earlier)</option>"
            "<option value=''>-- attach a new file instead --</option>"
            "</select>"
        )

    return _page(
        "Apply — Demo ATS",
        "<div class='banner'>Local demo job: Backend Engineer, ApplyOps Demo Co. "
        "This form never leaves your machine.</div>"
        "<h1>Application</h1>"
        "<form method='post' "
        f"action='/submit?scenario={html.escape(scenario)}' "
        "enctype='multipart/form-data'>"
        "<label for='name'>Full name</label>"
        "<input id='name' name='name' type='text' required>"
        "<label for='email'>Email</label>"
        "<input id='email' name='email' type='email' required>"
        "<label for='phone'>Phone</label>"
        "<input id='phone' name='phone' type='text' required>"
        "<label for='years'>Years of experience</label>"
        "<input id='years' name='years' type='number' required>"
        "<label for='notice'>Notice period</label>"
        "<select id='notice' name='notice_period' required>"
        "<option value=''>Select…</option>"
        "<option value='immediately'>Immediately</option>"
        "<option value='two_weeks'>Two weeks</option>"
        "<option value='one_month'>One month</option>"
        "</select>"
        "<fieldset><legend>Do you now, or will you in the future, require visa "
        "sponsorship?</legend>"
        "<label><input type='radio' name='needs_sponsorship' value='yes' required> Yes</label>"
        "<label><input type='radio' name='needs_sponsorship' value='no' required> No</label>"
        "</fieldset>"
        f"{stale_block}"
        "<label for='resume'>Resume</label>"
        "<input id='resume' name='resume' type='file' required>"
        "<p><button type='submit'>Submit application</button></p>"
        "</form>",
    )


class DemoATS:
    """A running demo ATS bound to localhost, or nothing at all."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        #: Exactly what the last POST carried: the fields, the filenames and
        #: any validation problems. Tests assert against this rather than against
        #: the word "received", because the interesting question is *what* arrived.
        self.last_submission: dict = {}
        #: How many submissions this ATS has received. The number is the point:
        #: "exactly once" is only checkable if somebody counts.
        self.submission_count = 0

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("demo ATS is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args) -> None:  # keep test output clean
                pass

            def _write(self, code: int, body: str, extra: dict | None = None) -> None:
                payload = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                scenario = parse_qs(parsed.query).get("scenario", [""])[0]
                if parsed.path == "/form":
                    self._write(200, _form(scenario))
                    return
                if parsed.path == "/choices":
                    self._write(200, _choices_form())
                    return
                if parsed.path == "/-/go":
                    target = (parse_qs(parsed.query).get("url") or [""])[0]
                    if target.startswith(("http://", "https://")):
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.end_headers()
                        return
                    self._write(400, _page("Bad redirect", "<p>missing url</p>"))
                    return

                if parsed.path == "/onsite":
                    self._write(200, _onsite_posting())
                    return
                if parsed.path == "/apply":
                    # The Easy Apply page: the same form at its own URL, plus the
                    # split first/last name fields a real LinkedIn modal asks for
                    # (and pre-fills from the member's own profile).
                    form = _form("standard").replace(
                        "action='/submit?scenario=standard'", "action='/submit'"
                    )
                    form = form.replace(
                        "<label for='name'>Full name</label>"
                        "<input id='name' name='name' type='text' required>",
                        "<label for='first_name'>First name</label>"
                        "<input id='first_name' name='first_name' type='text' value='Jane' required>"
                        "<label for='last_name'>Last name</label>"
                        "<input id='last_name' name='last_name' type='text' value='Doe' required>"
                        "<label for='city'>Location (city)</label>"
                        "<input id='city' name='city' type='text' required>",
                    )
                    self._write(200, form)
                    return

                if parsed.path == "/offsite":
                    # `?landing=1` points the Apply control at this same server
                    # under the other loopback hostname, so the whole two-hop
                    # flow (posting -> employer ATS) can be driven locally.
                    port = outer._server.server_address[1] if outer._server else 0
                    decoy = bool(parse_qs(parsed.query).get("decoy"))
                    local = ""
                    if parse_qs(parsed.query).get("landing"):
                        form = f"http://localhost:{port}/form"
                        # Wrap it the way LinkedIn wraps an outbound apply link,
                        # but through this server: `/-/go?url=<target>`.
                        local = f"http://localhost:{port}/-/go?url={quote(form, safe='')}"
                    self._write(
                        200, _offsite_posting(self.headers.get("Host", ""), local, decoy)
                    )
                    return
                if parsed.path == "/-/last-submission":
                    payload = json.dumps(outer.last_submission, ensure_ascii=False).encode(
                        "utf-8"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._write(200, _index(outer.url))

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                # Counted here, before any scenario branch: the number answers
                # "did the employer receive a request", which is true regardless
                # of what the page does next (answer, fail, or never respond).
                outer.submission_count += 1
                parsed = urlparse(self.path)
                scenario = parse_qs(parsed.query).get("scenario", ["standard"])[0]

                if scenario == "slow":
                    # Never answer within a test's lifetime: the SubMITTED_UNVERIFIED
                    # shape. Nothing is validated because nothing comes back.
                    import time

                    time.sleep(SLOW_DELAY_SECONDS)
                    try:
                        self._write(
                            200, _page("Late — Demo ATS", f"<p>{CONFIRMED_TEXT}</p>")
                        )
                    except Exception:  # noqa: BLE001 - socket died while we slept
                        return
                    return

                fields, files = parse_multipart(body, self.headers.get("Content-Type", ""))
                outer.last_submission = {
                    "scenario": scenario,
                    "fields": fields,
                    "files": files,
                    "path": self.path,
                }

                if scenario == "silent":
                    self._write(
                        200,
                        _page(
                            "Thank you — Demo ATS",
                            "<p>Your submission has been queued for review. "
                            "No reference number is available yet.</p>",
                        ),
                    )
                    return

                if scenario == "error":
                    self._write(
                        200,
                        _page(
                            "Submission failed — Demo ATS",
                            f"<div class='banner error'>{FAILED_TEXT}. Please review the "
                            "highlighted fields.</div>"
                            "<p><a href='/form'>Back to the form</a></p>",
                        ),
                    )
                    return

                # Real validation, because a demo that accepts anything cannot
                # tell a working filler from a broken one.
                problems = (
                    validate_choices(fields, files)
                    if parsed.path == "/choices"
                    else validate_submission(fields, files)
                )
                if problems:
                    outer.last_submission["problems"] = problems
                    self._write(
                        200,
                        _page(
                            "Submission failed — Demo ATS",
                            f"<div class='banner error'>{FAILED_TEXT}.</div><ul>"
                            + "".join(f"<li>{html.escape(p)}</li>" for p in problems)
                            + "</ul><p><a href='/form'>Back to the form</a></p>",
                        ),
                    )
                    return

                confirmation = str(uuid.uuid4())[:8].upper()
                outer.last_submission["confirmation"] = confirmation
                # Echo what we actually received. A page that only says
                # "received" cannot be used to prove the *contents* arrived.
                received = "".join(
                    f"<li>{html.escape(k)}: {html.escape(v)}</li>" for k, v in sorted(fields.items())
                )
                attachments = "".join(
                    f"<li>{html.escape(k)}: {html.escape(v)}</li>" for k, v in sorted(files.items())
                )
                self._write(
                    200,
                    _page(
                        "Received — Demo ATS",
                        f"<div class='banner'>{CONFIRMED_TEXT}</div>"
                        f"<p>Your confirmation id is <strong>{confirmation}</strong>.</p>"
                        f"<h2>What we received</h2><ul>{received}</ul>"
                        f"<h2>Attachments</h2><ul>{attachments}</ul>"
                        "<p><a href='/form'>Apply again</a></p>",
                    ),
                )


        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def write_sample_resume(path: str | Path, *, name: str = "Jane Doe") -> Path:
    """Write a tiny but real resume file for demos and tests.

    A PDF would be more realistic and less useful: nothing in the code paths
    under test parses PDF content, and a binary fixture nobody can read is worse
    to debug than a text file that says what it is.
    """
    target = Path(path)
    target.write_text(
        f"{name}\nBackend Engineer\n\nSample resume for ApplyOps local testing.\n",
        encoding="utf-8",
    )
    return target
