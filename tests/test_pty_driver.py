import os
import time

from ai_pal.providers.pty_driver import drive_screen


def test_exec_failure_terminates_child_immediately_not_via_timeout():
    # Regression test for the pty.fork() child branch: if os.execvp fails
    # (binary not on PATH), the child must os._exit(1) immediately rather
    # than let the exception unwind through the forked copy of the whole
    # calling process's Python state, which would leave a live duplicate
    # process running until drive_screen's own unconditional SIGKILL fires
    # at total_timeout. A fixed child returns almost instantly; a broken one
    # would consume close to the full budget below.
    start = time.monotonic()
    result = drive_screen(
        ["definitely-not-a-real-binary-xyz-123"], [], total_timeout=5.0
    )
    elapsed = time.monotonic() - start
    assert isinstance(result, str)
    assert elapsed < 2.0, (
        f"drive_screen took {elapsed:.2f}s for an exec failure -- expected "
        "an early return well under the 5.0s budget"
    )


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_does_not_leak_the_pty_master_fd():
    # Regression test for a system-wide leak: every drive_screen() call
    # allocates a pty master via pty.fork() and used to never close it.
    # /proc/sys/kernel/pty/max is a machine-wide limit, so an always-on poll
    # over three PTY providers eventually exhausts the pty pool for *every*
    # process on the box (new terminals, tmux, ssh), not just this one.
    drive_screen(["true"], [], total_timeout=2.0)  # warm up any lazy imports
    before = _open_fd_count()
    for _ in range(3):
        drive_screen(["true"], [], total_timeout=2.0)
    assert _open_fd_count() == before


def test_done_when_returns_early_instead_of_burning_the_full_timeout():
    # Without an early-exit condition every PTY fetch runs its whole budget
    # (~11s in the adapters), which is also how long quitting the app could
    # stall waiting on the worker thread.
    start = time.monotonic()
    out = drive_screen(
        ["bash", "-c", "printf READY; sleep 30"],
        [],
        total_timeout=10.0,
        done_when=lambda text: "READY" in text,
        settle_s=0.3,
    )
    elapsed = time.monotonic() - start
    assert "READY" in out
    assert elapsed < 4.0, f"expected an early return, took {elapsed:.2f}s"


def test_done_when_waits_for_the_settle_window_before_returning():
    # The predicate firing means "the data is on screen", not "stop reading
    # this instant": a screen painted in pieces gets a further settle window
    # to finish, so a second window rendered a frame later isn't lost.
    start = time.monotonic()
    out = drive_screen(
        ["bash", "-c", "printf FIRST; sleep 0.4; printf SECOND; sleep 30"],
        [],
        total_timeout=10.0,
        done_when=lambda text: "FIRST" in text,
        settle_s=1.0,
    )
    elapsed = time.monotonic() - start
    assert "SECOND" in out
    assert elapsed < 5.0


def test_without_done_when_the_full_timeout_is_still_honoured():
    # Existing adapters rely on drive_screen running its whole budget when no
    # predicate is supplied; the early-exit path must be strictly opt-in.
    start = time.monotonic()
    drive_screen(["bash", "-c", "printf READY; sleep 30"], [], total_timeout=1.5)
    assert time.monotonic() - start >= 1.4


def test_broken_done_when_predicate_does_not_break_the_capture():
    def boom(text: str) -> bool:
        raise ValueError("bad predicate")

    out = drive_screen(
        ["bash", "-c", "printf READY; sleep 30"],
        [],
        total_timeout=1.5,
        done_when=boom,
    )
    assert "READY" in out
