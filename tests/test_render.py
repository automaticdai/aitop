from ai_pal.models import Balance, Quota, UsageSnapshot
from ai_pal.render import (
    bar_color,
    fmt_pct,
    has_data,
    render_bar,
    render_snapshot,
    render_stale,
)


def test_fmt_pct():
    assert fmt_pct(None) == "—"
    assert fmt_pct(37.5) == "37.5%"


def test_render_bar():
    assert render_bar(None) == "?" * 24
    assert render_bar(50.0, width=10) == "█████░░░░░"


def test_bar_color_thresholds():
    # Spec §6: green below 70%, amber 70-90%, red above 90% (of quota used).
    assert bar_color(0.0) == "green"
    assert bar_color(69.9) == "green"
    assert bar_color(70.0) == "yellow"
    assert bar_color(90.0) == "yellow"
    assert bar_color(90.1) == "red"
    assert bar_color(100.0) == "red"
    assert bar_color(None) == "dim"


def test_render_snapshot_quota():
    snap = UsageSnapshot(
        "codex",
        daily=Quota(12, 50, "messages"),
        weekly=Quota(30, 200, "messages"),
    )
    out = render_snapshot(snap)
    assert out.splitlines()[0] == "[green]●[/green] codex"
    assert "12/50" in out
    assert "30/200" in out
    assert "messages" in out


def test_render_snapshot_colors_bars_by_severity():
    snap = UsageSnapshot(
        "codex",
        daily=Quota(10, 100, "messages"),  # 10% used -> green
        weekly=Quota(95, 100, "messages"),  # 95% used -> red
    )
    daily_line, weekly_line = render_snapshot(snap).splitlines()[1:]
    assert daily_line.startswith("daily   [green]")
    assert weekly_line.startswith("weekly  [red]")


def test_render_snapshot_balance():
    snap = UsageSnapshot("deepseek", balance=Balance(225.05, "CNY"))
    out = render_snapshot(snap)
    assert "225.05" in out and "CNY" in out


def test_render_snapshot_error():
    snap = UsageSnapshot("claude", ok=False, error="token expired")
    out = render_snapshot(snap)
    assert "token expired" in out
    assert out.startswith("[red]●[/red]")


def test_render_snapshot_error_text_is_markup_escaped():
    # Error strings are arbitrary text (exception messages, CLI output). An
    # unescaped "[...]" is silently swallowed as a style tag by Textual's
    # markup parser, so the user would lose part of their own diagnostic.
    snap = UsageSnapshot("claude", ok=False, error="HTTP 401 [unauthorized]")
    assert "\\[unauthorized]" in render_snapshot(snap)


def test_render_snapshot_ok_but_empty_says_no_data():
    # All three PTY adapters return ok=True with everything None when their
    # regexes match nothing (expired session, vendor UI change, truncated
    # capture) -- deliberately, rather than fabricating numbers. Rendering
    # must not reduce that to a bare provider name with no diagnostic.
    out = render_snapshot(UsageSnapshot("gemini"))
    assert out == "[yellow]●[/yellow] gemini: no data (check the CLI's login state)"


def test_render_stale_keeps_last_good_values():
    last_good = UsageSnapshot(
        "claude",
        daily=Quota(25, 100, "%"),
        weekly=Quota(20, 100, "%"),
    )
    out = render_stale(last_good, "pty timed out")
    assert "stale" in out
    assert "pty timed out" in out
    assert "25/100" in out
    assert "20/100" in out


def test_has_data():
    assert has_data(UsageSnapshot("codex", daily=Quota(1, 2, "messages")))
    assert has_data(UsageSnapshot("deepseek", balance=Balance(1.0, "CNY")))
    assert not has_data(UsageSnapshot("codex"))
