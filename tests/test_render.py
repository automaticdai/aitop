from aitop.models import Balance, Quota, QuotaGroup, UsageSnapshot
from aitop.render import (
    DISPLAY_NAME,
    LOGOS,
    bar_color,
    bar_pct,
    fmt_pct,
    format_quota_value,
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


def test_display_name_maps_internal_keys_to_shown_text():
    # Internal provider keys (config schema, PROVIDER_NAMES) stay as-is;
    # only what's actually shown to the user changes.
    assert DISPLAY_NAME == {
        "claude": "Claude Code",
        "codex": "Codex",
        "gemini": "Antigravity (agy)",
        "deepseek": "DeepSeek",
    }


def test_logos_cover_every_known_provider():
    assert set(LOGOS) == {"claude", "codex", "gemini", "deepseek"}


def test_render_snapshot_prepends_the_provider_logo():
    snap = UsageSnapshot("claude", daily=Quota(12, 50, "messages"))
    out = render_snapshot(snap)
    assert out.startswith(LOGOS["claude"] + "\n\n")


def test_render_snapshot_unknown_provider_has_no_logo():
    snap = UsageSnapshot("test-provider", daily=Quota(12, 50, "messages"))
    out = render_snapshot(snap)
    assert out.startswith("[green]●[/green]")


def test_render_stale_prepends_the_provider_logo():
    last_good = UsageSnapshot("codex", daily=Quota(12, 50, "messages"))
    out = render_stale(last_good, "pty timed out")
    assert out.startswith(LOGOS["codex"] + "\n\n")


def test_render_snapshot_quota():
    # The provider's identity is carried by the panel's border title
    # (app.py), not repeated in the body -- render_snapshot only needs to
    # produce the status + data lines. Provider name is a generic
    # placeholder with no LOGOS entry, since the logo isn't under test here.
    snap = UsageSnapshot(
        "test-provider",
        daily=Quota(12, 50, "messages"),
        weekly=Quota(30, 200, "messages"),
    )
    out = render_snapshot(snap)
    assert out.splitlines()[0] == "[green]●[/green]"
    assert "12/50" in out
    assert "30/200" in out
    assert "messages" in out


def test_render_snapshot_colors_bars_by_severity():
    snap = UsageSnapshot(
        "test-provider",
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


def test_render_snapshot_balance_flags_when_unavailable():
    snap = UsageSnapshot("deepseek", balance=Balance(225.05, "CNY", available=False))
    out = render_snapshot(snap)
    assert "225.05" in out
    assert "insufficient for API calls" in out


def test_render_snapshot_balance_available_shows_no_warning():
    snap = UsageSnapshot("deepseek", balance=Balance(225.05, "CNY", available=True))
    out = render_snapshot(snap)
    assert "insufficient" not in out


def test_render_snapshot_error():
    snap = UsageSnapshot("test-provider", ok=False, error="token expired")
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
    # must not reduce that to a blank body with no diagnostic.
    out = render_snapshot(UsageSnapshot("test-provider"))
    assert out == "[yellow]●[/yellow] no data (check the CLI's login state)"


def test_render_snapshot_quota_line_shows_reset_note():
    snap = UsageSnapshot(
        "test-provider", weekly=Quota(0, 100, "%", reset_note="resets 14:11 on 27 Aug")
    )
    lines = render_snapshot(snap).splitlines()
    assert lines[1].startswith("weekly  ")
    assert lines[2].strip() == "resets 14:11 on 27 Aug"


def test_render_snapshot_quota_line_omits_reset_line_when_absent():
    snap = UsageSnapshot("codex", weekly=Quota(0, 100, "%"))
    out = render_snapshot(snap)
    assert "resets" not in out


def test_render_snapshot_percent_quota_shows_bare_percentage():
    # "x/100 (x%)" is a redundant restatement for percent-based quotas
    # (Codex/Antigravity/Claude Code) -- just show the percentage.
    snap = UsageSnapshot("codex", weekly=Quota(6, 100, "%"))
    out = render_snapshot(snap)
    assert "6.0%" in out
    assert "/100" not in out


def test_render_snapshot_non_percent_quota_still_shows_the_fraction():
    # Non-percent units (messages, hours) don't have this redundancy --
    # used/limit is genuinely informative there.
    snap = UsageSnapshot("codex", weekly=Quota(30, 200, "messages"))
    out = render_snapshot(snap)
    assert "30/200 messages" in out


def test_render_snapshot_groups():
    snap = UsageSnapshot(
        "gemini",
        groups=[
            QuotaGroup(label="Gemini", daily=Quota(0, 100, "%"), weekly=Quota(6, 100, "%")),
            QuotaGroup(label="Claude & GPT-OSS", weekly=Quota(35, 100, "%", reset_note="Refreshes in 97h 25m")),
        ],
    )
    out = render_snapshot(snap)
    assert "Gemini" in out
    assert "Claude & GPT-OSS" in out
    assert "6.0%" in out
    assert "35.0%" in out
    assert "Refreshes in 97h 25m" in out


def test_render_snapshot_groups_have_a_blank_line_between_them():
    snap = UsageSnapshot(
        "test-provider",
        groups=[
            QuotaGroup(label="Gemini", weekly=Quota(6, 100, "%")),
            QuotaGroup(label="Claude & GPT-OSS", weekly=Quota(35, 100, "%")),
        ],
    )
    lines = render_snapshot(snap).splitlines()
    assert "" in lines
    blank_idx = lines.index("")
    assert lines[blank_idx - 1].startswith("weekly")
    assert lines[blank_idx + 1] == "Claude & GPT-OSS"


def test_render_stale_keeps_last_good_values():
    last_good = UsageSnapshot(
        "claude",
        daily=Quota(25, 100, "%"),
        weekly=Quota(20, 100, "%"),
    )
    out = render_stale(last_good, "pty timed out")
    assert "stale" in out
    assert "pty timed out" in out
    assert "25.0%" in out
    assert "20.0%" in out


def test_render_stale_keeps_last_good_groups():
    last_good = UsageSnapshot(
        "gemini",
        groups=[QuotaGroup(label="Gemini", weekly=Quota(6, 100, "%"))],
    )
    out = render_stale(last_good, "pty timed out")
    assert "Gemini" in out
    assert "6.0%" in out


def test_has_data():
    assert has_data(UsageSnapshot("codex", daily=Quota(1, 2, "messages")))
    assert has_data(UsageSnapshot("deepseek", balance=Balance(1.0, "CNY")))
    assert has_data(UsageSnapshot("gemini", groups=[QuotaGroup(label="Gemini", weekly=Quota(1, 2, "%"))]))
    assert not has_data(UsageSnapshot("codex"))


def test_bar_pct_clamps_and_defaults():
    assert bar_pct(None) == 0.0
    assert bar_pct(50.0) == 50.0
    assert bar_pct(120.0) == 100.0
    assert bar_pct(-5.0) == 0.0


def test_format_quota_value_percent_shows_bare_percentage():
    assert format_quota_value(Quota(6, 100, "%")) == "6.0%"
    assert format_quota_value(Quota(0, 0, "%")) == "—"


def test_format_quota_value_non_percent_keeps_fraction():
    assert format_quota_value(Quota(30, 200, "messages")) == "30/200 messages (15.0%)"


def test_claude_daily_window_is_labeled_session():
    # Claude Code's "Current session" is a rolling session, not a calendar day,
    # so its top-level daily window reads "session" rather than "daily".
    snap = UsageSnapshot("claude", daily=Quota(2, 100, "%"), weekly=Quota(5, 100, "%"))
    lines = render_snapshot(snap).splitlines()
    assert any(line.startswith("session") for line in lines)
    assert not any(line.startswith("daily") for line in lines)


def test_other_providers_keep_the_daily_label():
    snap = UsageSnapshot("codex", daily=Quota(12, 50, "messages"))
    lines = render_snapshot(snap).splitlines()
    assert any(line.startswith("daily") for line in lines)
    assert not any(line.startswith("session") for line in lines)


def test_render_snapshot_group_orders_daily_before_weekly():
    snap = UsageSnapshot(
        "gemini",
        groups=[QuotaGroup(label="Gemini", daily=Quota(0, 100, "%"), weekly=Quota(6, 100, "%"))],
    )
    lines = render_snapshot(snap).splitlines()
    daily_idx = next(i for i, line in enumerate(lines) if line.startswith("daily"))
    weekly_idx = next(i for i, line in enumerate(lines) if line.startswith("weekly"))
    assert daily_idx < weekly_idx
