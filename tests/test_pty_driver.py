import asyncio
import os
import time
from pathlib import Path

import pytest

from aitop.providers.pty_driver import drive_screen, drive_screen_async, drive_screen_steps


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
    # stall waiting on the helper process.
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
    # The helper relies on drive_screen running its whole budget when no
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


def test_drive_screen_steps_returns_one_screen_per_keystroke():
    # The core contract is one (keys, screen) pair per keystroke, in order,
    # with keys preserved verbatim so a caller can identify a specific step.
    steps = drive_screen_steps(
        ["bash", "-c", "sleep 5"],
        [(0.1, "a"), (0.2, "b"), (0.3, "c")],
        total_timeout=1.0,
        settle_s=0.1,
    )
    assert [keys for keys, _ in steps] == ["a", "b", "c"]
    assert all(isinstance(screen, str) for _, screen in steps)


def test_async_capture_uses_a_private_app_workspace(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    async def scenario() -> str:
        return await drive_screen_async(["pwd"], [], total_timeout=2.0)

    text = asyncio.run(scenario())
    workspace = cache / "aitop" / "provider-workspace"
    assert str(workspace) in text
    assert workspace != Path.cwd()
    assert workspace.exists()
    assert workspace.stat().st_mode & 0o777 == 0o700


def test_dialog_response_is_sent_only_after_identifying_text_is_visible(tmp_path):
    command = [
        "bash",
        "-c",
        'printf "Quick safety check\\nYes, I trust this folder\\n"; '
        'IFS= read -r answer; printf "ACCEPTED"; sleep 30',
    ]

    async def scenario(workspace: Path) -> str:
        return await drive_screen_async(
            command,
            [],
            total_timeout=5.0,
            done_patterns=["ACCEPTED"],
            settle_s=0.2,
            dialog_responses=[
                (("Quick safety check", "Yes, I trust this folder"), "\r")
            ],
            cwd=workspace,
        )

    workspace = tmp_path / "provider-workspace"
    workspace.mkdir()
    assert "ACCEPTED" in asyncio.run(scenario(workspace))


def test_dialog_response_is_not_sent_to_an_unrecognized_screen(tmp_path):
    workspace = tmp_path / "provider-workspace"
    workspace.mkdir()

    async def scenario() -> str:
        return await drive_screen_async(
            [
                "bash",
                "-c",
                'printf "Unrelated login prompt\\n"; '
                'if IFS= read -r -t 0.5 answer; then printf "UNSAFE"; '
                'else printf "UNTOUCHED"; fi; sleep 30',
            ],
            [],
            total_timeout=5.0,
            done_patterns=["UNSAFE|UNTOUCHED"],
            settle_s=0.2,
            dialog_responses=[
                (("Quick safety check", "Yes, I trust this folder"), "\r")
            ],
            cwd=workspace,
        )

    text = asyncio.run(scenario())
    assert "UNTOUCHED" in text
    assert "UNSAFE" not in text


def test_cancelling_async_capture_reaps_its_cli_child(tmp_path):
    pid_file = tmp_path / "cli.pid"

    async def scenario() -> tuple[int, float]:
        workspace = tmp_path / "provider-workspace"
        workspace.mkdir()
        task = asyncio.create_task(
            drive_screen_async(
                [
                    "bash",
                    "-c",
                    'printf "%s" "$$" > "$1"; sleep 30',
                    "bash",
                    str(pid_file),
                ],
                [],
                total_timeout=20.0,
                cwd=workspace,
            )
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        start = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid, time.monotonic() - start

    pid, elapsed = asyncio.run(scenario())
    assert elapsed < 3.0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
