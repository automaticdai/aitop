from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time
from collections.abc import Callable

import pyte


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
    # Everything after pty.fork() returns in the parent runs under try/finally:
    # `fd` is a real pty master device and `/proc/sys/kernel/pty/max` is a
    # *system-wide* limit, so leaking one per call (this polls forever, every
    # ~30s, across three PTY providers) exhausts the whole machine's pty pool,
    # not just this process's fd budget. Cleanup lives in exactly one place so
    # that every exit path -- normal end of loop, early `break` on EOF/OSError,
    # done_when early return, or an unexpected exception -- closes the master.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        stream = pyte.Stream(screen)
        start = time.monotonic()
        sent: set[float] = set()
        done_at: float | None = None

        while time.monotonic() - start < total_timeout:
            readable, _, _ = select.select([fd], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                stream.feed(chunk.decode("utf-8", "replace"))
                if done_when is not None and done_at is None:
                    try:
                        if done_when(_render(screen)):
                            done_at = time.monotonic()
                    except Exception:
                        # A broken predicate must never turn into a failed
                        # capture -- fall back to running the full budget.
                        done_when = None
            for at, keys in key_sequence:
                if at not in sent and time.monotonic() - start >= at:
                    try:
                        os.write(fd, keys.encode())
                    except OSError:
                        pass
                    sent.add(at)
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

    return _render(screen)


def _render(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display)
