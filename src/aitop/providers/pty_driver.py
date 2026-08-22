from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
from collections.abc import Callable

import pyte

# A process-wide "please abort" latch. `drive_screen` runs in a worker thread
# (via asyncio.to_thread), which Python cannot cancel; the only way to cut one
# short is to have it poll this flag. `Poller.stop()` sets it (and resets it at
# the start of each round), so quitting mid-poll makes every in-flight capture
# break out of its loop, SIGKILL its child, and return -- instead of running
# out the full total_timeout while the interpreter waits for the executor to
# drain (the "quit leaves threads/CLIs running" bug).
_stop = threading.Event()


def request_stop() -> None:
    """Signal every in-flight drive_screen() call to abort promptly."""
    _stop.set()


def reset_stop() -> None:
    _stop.clear()


def _terminal_queries_response(chunk: bytes) -> bytes:
    """Answer the terminal capability queries a modern TUI sends at startup.

    Codex 0.149.0 (and other crossterm-style CLIs) interrogate the terminal --
    device attributes, cursor position, theme colors -- and wait for a reply
    before enabling their input handling. `pyte` only renders *output*; it never
    answers these, so without this the CLI never begins to accept keystrokes and
    every typed command just sits unsubmitted in the prompt.
    """
    response = b""
    if b"\x1b[c" in chunk:  # DA1: "what terminal are you?"
        response += b"\x1b[?1;2c"  # VT100 with ANSI color
    if b"\x1b[6n" in chunk:  # DSR-CPR: "where is the cursor?"
        response += b"\x1b[1;1R"
    if b"\x1b]10;?" in chunk:  # OSC 10: "what's the foreground color?"
        response += b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"
    if b"\x1b]11;?" in chunk:  # OSC 11: "what's the background color?"
        response += b"\x1b]11;rgb:0000/0000/0000\x1b\\"
    if b"\x1b[?u" in chunk:  # kitty keyboard protocol query: report current flags
        response += b"\x1b[?7u"  # honor the flags the app pushed (CSI > 7 u)
    return response


def _drive(
    command: list[str],
    key_sequence: list[tuple[float, str]],
    cols: int,
    rows: int,
    total_timeout: float,
    done_when: Callable[[str], bool] | None,
    settle_s: float,
    collect_steps: bool,
) -> tuple[str, list[tuple[str, str]]]:
    """Drive a TUI once; return (final_screen, steps).

    `steps` is populated only when `collect_steps` is True: one (keys,
    screen) pair per keystroke, captured `settle_s` after that keystroke was
    sent (so a screen still painting has time to finish). Used to read two
    successive screens (e.g. Codex's /status then /usage) in one session.
    """
    # Each capture starts fresh: clear any stop request left over from an
    # earlier quit (in a single app run that never matters, but tests and
    # embedded apps run many captures in one process).
    reset_stop()
    pid, fd = pty.fork()
    if pid == 0:
        # Child branch: control must never return past os.execvp. If exec
        # fails (binary not on PATH, etc.), the raised exception would
        # otherwise unwind through this forked copy of the whole parent
        # process's Python state instead of terminating it, leaving a live
        # duplicate process running. os._exit skips that unwind entirely.
        try:
            os.environ["TERM"] = "xterm-256color"
            os.execvp(command[0], command)
        except Exception:
            os._exit(1)

    screen = pyte.Screen(cols, rows)
    steps: list[tuple[str, str]] = []
    # Everything after pty.fork() returns in the parent runs under try/finally:
    # `fd` is a real pty master device and `/proc/sys/kernel/pty/max` is a
    # *system-wide* limit, so leaking one per call (this polls forever, every
    # ~30s, across three PTY providers) exhausts the whole machine's pty pool,
    # not just this process's fd budget. Cleanup lives in exactly one place so
    # that every exit path -- normal end of loop, early `break` on EOF/OSError,
    # stop-signal, done_when early return, or an unexpected exception -- closes
    # the master.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        stream = pyte.Stream(screen)
        start = time.monotonic()
        sent: set[float] = set()
        done_at: float | None = None
        pending_snapshots: list[tuple[float, str]] = []

        while True:
            now = time.monotonic() - start
            if now >= total_timeout:
                break
            if _stop.is_set():
                break

            readable, _, _ = select.select([fd], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                response = _terminal_queries_response(chunk)
                if response:
                    try:
                        os.write(fd, response)
                    except OSError:
                        pass
                stream.feed(chunk.decode("utf-8", "replace"))
                if done_when is not None and done_at is None:
                    try:
                        if done_when(_render(screen)):
                            done_at = time.monotonic()
                    except Exception:
                        # A broken predicate must never turn into a failed
                        # capture -- fall back to running the full budget.
                        done_when = None

            now = time.monotonic() - start
            for at, keys in key_sequence:
                if at not in sent and now >= at:
                    try:
                        os.write(fd, keys.encode())
                    except OSError:
                        pass
                    sent.add(at)
                    if collect_steps:
                        pending_snapshots.append((time.monotonic() + settle_s, keys))

            if collect_steps and pending_snapshots:
                now = time.monotonic()
                remaining: list[tuple[float, str]] = []
                for ready_at, keys in pending_snapshots:
                    if now >= ready_at:
                        steps.append((keys, _render(screen)))
                    else:
                        remaining.append((ready_at, keys))
                pending_snapshots = remaining
                # Once every keystroke has been snapshotted there is nothing
                # left to wait for -- return without burning the full budget.
                if len(steps) >= len(key_sequence):
                    break

            if done_at is not None and time.monotonic() - done_at >= settle_s:
                break
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    return _render(screen), steps


def drive_screen(
    command: list[str],
    key_sequence: list[tuple[float, str]],
    cols: int = 120,
    rows: int = 40,
    total_timeout: float = 30.0,
    done_when: Callable[[str], bool] | None = None,
    settle_s: float = 1.0,
) -> str:
    """Spawn a TUI in a PTY, feed a timed key sequence, return the rendered screen text.

    `done_when`, if given, is called with the screen text rendered so far each
    time new output arrives. Once it returns True the driver keeps reading for
    a further `settle_s` seconds (so a screen still being painted in pieces can
    finish) and then returns early instead of burning the whole
    `total_timeout`. Returning early is what keeps a poll cycle -- and a quit
    while a poll is in flight -- from blocking for the full budget every time.
    """
    text, _ = _drive(
        command, key_sequence, cols, rows, total_timeout, done_when, settle_s,
        collect_steps=False,
    )
    return text


def drive_screen_steps(
    command: list[str],
    key_sequence: list[tuple[float, str]],
    cols: int = 120,
    rows: int = 40,
    total_timeout: float = 30.0,
    settle_s: float = 1.0,
) -> list[tuple[str, str]]:
    """Like drive_screen, but capture the screen after each keystroke settles.

    Returns one (keys, screen_text) pair per keystroke, in order, where each
    screen_text is the rendered screen `settle_s` after that keystroke was
    sent. Lets a caller read several successive screens (e.g. a CLI's `/status`
    then `/usage`) from a single session rather than spawning a fresh CLI per
    screen.
    """
    _, steps = _drive(
        command, key_sequence, cols, rows, total_timeout, None, settle_s,
        collect_steps=True,
    )
    return steps


def _render(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display)
