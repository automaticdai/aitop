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
