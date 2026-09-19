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
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlparse

CONFIRMED_TEXT = "Application received"

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
        "<label for='name'>Full name</label><input id='name' name='name' type='text'>"
        "<label for='email'>Email</label><input id='email' name='email' type='email'>"
        "<label for='phone'>Phone</label><input id='phone' name='phone' type='text'>"
        "<label for='years'>Years of experience</label><input id='years' name='years' type='number'>"
        "<label for='notice'>Notice period</label>"
        "<select id='notice' name='notice_period'>"
        "<option value=''>Select…</option>"
        "<option value='immediately'>Immediately</option>"
        "<option value='two_weeks'>Two weeks</option>"
        "<option value='one_month'>One month</option>"
        "</select>"
        "<label>Do you now, or will you in the future, require visa sponsorship?"
        "</label>"
        "<label><input type='radio' name='needs_sponsorship' value='yes'> Yes</label>"
        "<label><input type='radio' name='needs_sponsorship' value='no'> No</label>"
        f"{stale_block}"
        "<label for='resume'>Resume</label><input id='resume' name='resume' type='file'>"
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
                self._write(200, _index(outer.url))

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    # Consume the multipart body so the socket closes cleanly.
                    # Its content is irrelevant to what is being tested here.
                    self.rfile.read(length)
                parsed = urlparse(self.path)
                scenario = parse_qs(parsed.query).get("scenario", ["standard"])[0]
                if scenario == "error":
                    self._write(
                        200,
                        _page(
                            "Submission failed — Demo ATS",
                            "<div class='banner error'>There was a problem with your "
                            "submission. Please review the highlighted fields.</div>"
                            "<p><a href='/form'>Back to the form</a></p>",
                        ),
                    )
                    return
                if scenario == "slow":
                    # Never answer: the request stays open until the socket dies.
                    import time

                    # Never answer within the lifetime of a test: the request
                    # stays open, which is the SubMITTED_UNVERIFIED shape. The
                    # delay must outlast the whole reconcile window, otherwise
                    # "still unknown" and "eventually known" race each other.
                    time.sleep(SLOW_DELAY_SECONDS)
                    try:
                        self._write(
                            200,
                            _page("Late — Demo ATS", f"<p>{CONFIRMED_TEXT}</p>"),
                        )
                    except Exception:  # noqa: BLE001 - socket died while we slept
                        return
                    return
                confirmation = str(uuid.uuid4())[:8].upper()
                self._write(
                    200,
                    _page(
                        "Received — Demo ATS",
                        f"<div class='banner'>{CONFIRMED_TEXT}</div>"
                        f"<p>Your confirmation id is <strong>{confirmation}</strong>.</p>"
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
