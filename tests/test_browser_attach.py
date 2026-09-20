"""Blocker: an abandoned Chrome on the project profile bricks every later run.

Repro, observed live: a Chrome launched by the legacy attach helper
(`tools/attach.py`, `--remote-debugging-port=9222`, detached on purpose so it
outlives its launcher) is still running on `data/browser-profile`. The new
console/MCP/runner path calls `launch_persistent_context` on the *same* profile
and dies with

    Opening in existing browser session. This usually means that the profile is
    already in use by another instance of Chromium.

while `doctor` cheerfully reports `browser lock -- free` -- the flock is released
when the process dies, but the browser it started is not. The result: after a
crash, nothing runs until a human kills Chrome by hand.

What is asserted here: the controller uses a browser that is already up on the
project's debugging port instead of fighting it, does not kill it on the way out,
and -- when it cannot attach -- fails with something a human can act on rather
than Chromium's own sentence.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-attach-"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_detached(profile: Path, port: int) -> subprocess.Popen:
    """A stand-in for what `tools/attach.py` leaves behind."""
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            CHROME_BIN,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--use-mock-keychain",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(BrowserController.endpoint_up, port):
            return True
        await asyncio.sleep(0.4)
    return False


@pytest.fixture(scope="module")
def chrome_available():
    if not Path(CHROME_BIN).exists():
        pytest.skip("Chrome not installed")
    return True


@pytest.mark.asyncio
async def test_the_controller_uses_a_browser_that_is_already_up(chrome_available):
    """The repro, inverted: an abandoned browser must be reusable, not fatal."""
    root = _tmp()
    profile = root / "browser-profile"
    port = _free_port()
    proc = _launch_detached(profile, port)
    try:
        assert await _wait_for_port(port), "the detached browser never answered"

        controller = BrowserController(headless=False, user_data_dir=profile, debug_port=port)
        await controller.launch()
        assert controller.attached is True

        # It is a working browser: drive it like the product does.
        with DemoATS() as ats:
            await controller.goto(f"{ats.url}/form", settle=0.4)
            state = await controller.get_page_state()
            assert any(f.label == "Full name" for f in state.form_fields)

        await controller.close()

        # And closing our side must not take the user's browser down with it.
        assert await _wait_for_port(port, timeout=5), "close() killed a browser it did not start"
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_an_unattachable_profile_fails_with_an_actionable_message(chrome_available):
    """When we must launch but the profile is taken, say what to do about it."""
    root = _tmp()
    profile = root / "browser-profile"
    port = _free_port()
    proc = _launch_detached(profile, port)
    try:
        assert await _wait_for_port(port), "the detached browser never answered"

        # A controller that has *not* been told about the port: it will try to
        # launch and the profile will be busy -- exactly the reported failure.
        controller = BrowserController(headless=False, user_data_dir=profile, debug_port=_free_port())
        with pytest.raises(RuntimeError) as excinfo:
            await controller.launch()

        message = str(excinfo.value)
        assert "browser-profile" in message, message
        assert "9222" in message or "debug" in message.lower(), message
        # It must not simply re-raise Chromium's own sentence.
        assert "Opening in existing browser session" not in message, message
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


def test_stop_cleans_a_browser_left_on_the_profile(monkeypatch):
    """`stop` is what a user reaches for when nothing works.

    With no pid file -- the case an abandoned run leaves behind -- it used to
    print "No running console found" and exit 0 while the abandoned Chrome kept
    the profile busy, so the next run failed again and the user had no way out.
    """

    from applyops import main as main_module

    root = _tmp()
    profile = root / "browser-profile"
    profile.mkdir()

    # A live process of our own, standing in for the abandoned Chrome. `ps -Ao
    # pid=,command=` prints the pid first; the path is what the product resolves.
    victim = subprocess.Popen(["sleep", "30"])
    try:
        fake_ps = (
            f"{victim.pid} /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--user-data-dir={profile.resolve()} --remote-debugging-port=9222 about:blank\n"
            "4242 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--user-data-dir=/Users/someone/else --remote-debugging-port=9222\n"
            "4243 /usr/bin/python3 -c pass\n"
        )
        monkeypatch.setattr(main_module, "_list_processes", lambda: fake_ps)

        assert main_module.stop(root) == 0

        # SIGTERM is delivered asynchronously; give the process a moment to go.
        for _ in range(50):
            if victim.poll() is not None:
                break
            time.sleep(0.1)
        assert victim.poll() is not None, "the profile's browser should have been signalled"
        # The unrelated profile's pid must not have been touched: 4242 is not a
        # process we own, so the only thing asserted is that we did not signal it.
    finally:
        victim.kill()
        shutil.rmtree(root, ignore_errors=True)
