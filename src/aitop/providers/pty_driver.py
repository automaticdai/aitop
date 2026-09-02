from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pyte

DialogResponse = tuple[tuple[str, ...], str]


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
    dialog_responses: list[DialogResponse] | None,
    should_stop: Callable[[], bool] | None,
) -> tuple[str, list[tuple[str, str]]]:
    """Drive a TUI once; return (final_screen, steps).

    `steps` is populated only when `collect_steps` is True: one (keys,
    screen) pair per keystroke, captured `settle_s` after that keystroke was
    sent (so a screen still painting has time to finish). Used to read two
    successive screens (e.g. Codex's /status then /usage) in one session.
    """
    # Construct fallible Python-side state before allocating the PTY, so an
    # allocation error cannot leave a freshly forked child without cleanup.
    screen = pyte.Screen(cols, rows)
    steps: list[tuple[str, str]] = []
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
        responded: set[int] = set()
        done_at: float | None = None
        pending_snapshots: list[tuple[float, str]] = []

        while True:
            now = time.monotonic() - start
            if now >= total_timeout:
                break
            if should_stop is not None and should_stop():
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
                rendered = _render(screen)
                # Startup dialogs are answered only after their identifying
                # text is visible. This avoids sending blind confirmation keys
                # into an unexpected login, update, or consent prompt.
                for index, (markers, keys) in enumerate(dialog_responses or []):
                    if index in responded:
                        continue
                    folded = rendered.casefold()
                    if all(marker.casefold() in folded for marker in markers):
                        try:
                            os.write(fd, keys.encode())
                        except OSError:
                            pass
                        responded.add(index)
                if done_when is not None and done_at is None:
                    try:
                        if done_when(rendered):
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
    dialog_responses: list[DialogResponse] | None = None,
    should_stop: Callable[[], bool] | None = None,
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
        command,
        key_sequence,
        cols,
        rows,
        total_timeout,
        done_when,
        settle_s,
        collect_steps=False,
        dialog_responses=dialog_responses,
        should_stop=should_stop,
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
        command,
        key_sequence,
        cols,
        rows,
        total_timeout,
        None,
        settle_s,
        collect_steps=True,
        dialog_responses=None,
        should_stop=None,
    )
    return steps


async def drive_screen_async(
    command: list[str],
    key_sequence: list[tuple[float, str]],
    cols: int = 120,
    rows: int = 40,
    total_timeout: float = 30.0,
    done_patterns: list[str] | None = None,
    done_all: bool = False,
    settle_s: float = 1.0,
    dialog_responses: list[DialogResponse] | None = None,
    cwd: Path | None = None,
) -> str:
    """Drive a TUI in a cancellable, single-threaded helper process.

    `pty.fork()` is unsafe when called from an asyncio worker thread because it
    forks the entire multithreaded Python process. The helper starts as a normal
    subprocess and performs the fork from its single main thread instead.

    Unless a caller explicitly supplies `cwd`, the vendor CLI runs in a private
    app-owned workspace under the user cache. Project trust prompts therefore
    never approve the user's current repository or load its project-scoped
    configuration. The stable path also avoids accumulating one vendor trust
    record for every poll.
    """
    request = {
        "command": command,
        "key_sequence": key_sequence,
        "cols": cols,
        "rows": rows,
        "total_timeout": total_timeout,
        "done_patterns": done_patterns or [],
        "done_all": done_all,
        "settle_s": settle_s,
        "dialog_responses": dialog_responses or [],
    }

    workdir = provider_workspace() if cwd is None else Path(cwd)

    env = os.environ.copy()
    # Some CLIs consult PWD instead of getcwd() when locating project config.
    env["PWD"] = str(workdir)
    process: subprocess.Popen[bytes] | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        # Popen's fork/exec path is implemented for multithreaded callers and
        # runs no Python code in the child before exec. The unsafe pty.fork()
        # remains confined to the helper's single main thread.
        process = subprocess.Popen(
            [sys.executable, "-m", "aitop.providers.pty_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            start_new_session=True,
        )
        payload = json.dumps(request).encode("utf-8")
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        communication = asyncio.create_task(
            _collect_output(process.stdout, process.stderr)
        )
        process.stdin.write(payload)
        process.stdin.close()
        try:
            stdout, stderr = await asyncio.shield(communication)
        except BaseException:
            # wait_for() and Textual shutdown both cancel this coroutine. Do
            # not return until the helper has handled SIGTERM and reaped its
            # own PTY child, otherwise later poll rounds can overlap it.
            await asyncio.shield(_terminate_worker(process, communication))
            raise
        await _wait_for_exit(process)

        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = stderr.decode("utf-8", "replace").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"PTY helper returned an invalid response{suffix}"
            ) from exc
        if process.returncode != 0 or not response.get("ok"):
            error = response.get("error") or stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(error or f"PTY helper exited with status {process.returncode}")
        return str(response["text"])
    finally:
        if (
            process is not None
            and process.poll() is None
            and communication is not None
        ):
            await asyncio.shield(_terminate_worker(process, communication))


def provider_workspace() -> Path:
    """Return aitop's private, stable workspace for interactive provider CLIs."""
    configured_cache = os.environ.get("XDG_CACHE_HOME")
    if configured_cache and Path(configured_cache).expanduser().is_absolute():
        cache_root = Path(configured_cache).expanduser()
    else:
        cache_root = Path.home() / ".cache"
    workspace = cache_root / "aitop" / "provider-workspace"
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if workspace.is_symlink() or not workspace.is_dir():
        raise RuntimeError(f"provider workspace is not a real directory: {workspace}")
    if hasattr(os, "getuid") and workspace.stat().st_uid != os.getuid():
        raise RuntimeError(f"provider workspace is not owned by this user: {workspace}")
    workspace.chmod(0o700)
    return workspace


async def _terminate_worker(
    process: subprocess.Popen[bytes],
    communication: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    """Ask the helper to reap its PTY child, then force it down if wedged."""
    if process.poll() is not None:
        await asyncio.shield(communication)
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await asyncio.shield(communication)
        return
    try:
        await asyncio.wait_for(asyncio.shield(communication), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.shield(communication)
    await _wait_for_exit(process)


async def _collect_output(
    stdout: BinaryIO, stderr: BinaryIO
) -> tuple[bytes, bytes]:
    """Read both helper pipes without asyncio's process-wide child watcher."""
    stdout_bytes, stderr_bytes = await asyncio.gather(
        _read_pipe(stdout), _read_pipe(stderr)
    )
    return stdout_bytes, stderr_bytes


async def _read_pipe(pipe: BinaryIO) -> bytes:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    try:
        return await reader.read()
    finally:
        transport.close()


async def _wait_for_exit(
    process: subprocess.Popen[bytes], timeout_s: float = 2.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while process.poll() is None:
        if time.monotonic() >= deadline:
            raise TimeoutError("PTY helper did not exit after closing its output pipes")
        await asyncio.sleep(0.01)


def _render(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display)
