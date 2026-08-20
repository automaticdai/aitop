from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time

import pyte


def drive_screen(
    command: list[str],
    key_sequence: list[tuple[float, str]],
    cols: int = 120,
    rows: int = 40,
    total_timeout: float = 30.0,
) -> str:
    """Spawn a TUI in a PTY, feed a timed key sequence, return the rendered screen text."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execvp(command[0], command)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    start = time.monotonic()
    sent: set[float] = set()

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
        for at, keys in key_sequence:
            if at not in sent and time.monotonic() - start >= at:
                try:
                    os.write(fd, keys.encode())
                except OSError:
                    pass
                sent.add(at)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.waitpid(pid, 0)
    except Exception:
        pass
    return "\n".join(line.rstrip() for line in screen.display)
