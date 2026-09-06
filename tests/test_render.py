import re

from aitop.models import Balance, Quota, QuotaGroup, Spend, UsageSnapshot
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
        "copilot": "GitHub Copilot",
        "glm": "GLM",
        "openrouter": "OpenRouter",
    }


def test_logos_cover_every_known_provider():
    assert set(LOGOS) == {"claude", "codex", "gemini", "deepseek", "copilot", "glm", "openrouter"}


def test_render_snapshot_prepends_the_provider_logo():
    snap = UsageSnapshot("claude", daily=Quota(12, 50, "messages"))
    out = render_snapshot(snap)
    assert out.startswith(LOGOS["claude"] + "\n\n")


def test_render_snapshot_shows_client_info_under_logo():
    snap = UsageSnapshot(
        "claude",
        daily=Quota(25, 100, "%"),
        client_info="Claude Code v2.1.237",
    )
    out = render_snapshot(snap)
    assert out.startswith(LOGOS["claude"] + "\n\n")
    lines = out.splitlines()
    # logo (5 rows) + blank, then the dim client-info line, a blank, and the
    # status dot before the quota data -- "under the logo at top".
    assert lines[6] == "[dim]Claude Code v2.1.237[/dim]"
    assert lines[7] == ""
    assert lines[8] == "[green]●[/green]"


def test_render_snapshot_omits_client_info_when_absent():
    snap = UsageSnapshot("claude", daily=Quota(25, 100, "%"))
    out = render_snapshot(snap)
    assert out.splitlines()[6] == "[green]●[/green]"


def test_render_stale_keeps_client_info_under_logo():
    last_good = UsageSnapshot(
        "codex",
        daily=Quota(12, 50, "messages"),
        client_info="OpenAI Codex (v0.148.0)",
    )
    out = render_stale(last_good, "pty timed out")
    lines = out.splitlines()
    # stale cards have no green status dot, so client info sits directly above
    # the stale header.
    assert lines[6] == "[dim]OpenAI Codex (v0.148.0)[/dim]"
    assert lines[7] == ""
    assert "stale" in lines[8]


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


def test_render_snapshot_spend_block():
    snap = UsageSnapshot("openrouter", balance=Balance(42.13, "USD"), spend=[
        Spend("today", 1.2, "USD"), Spend("this week", 8.44, "USD"), Spend("this month", 31.02, "USD")])
    out = render_snapshot(snap)
    assert "42.13" in out
    lines = out.splitlines()
    assert "[dim]spend[/dim]" in lines
    # No bar and no color: there is no limit to draw one from. Labels are
    # padded past "this month" and the amounts share a right-aligned column,
    # so the decimal points line up.
    assert "today        1.20 USD" in lines
    assert "this week    8.44 USD" in lines
    assert "this month  31.02 USD" in lines
    assert "█" not in out and "░" not in out


def test_spend_alone_is_enough_to_render_a_card():
    # An uncapped OpenRouter key whose account credits are out of reach has
    # spend and nothing else -- that must be a real card, not "no data".
    out = render_snapshot(UsageSnapshot("openrouter", spend=[Spend("today", 0.0, "USD")]))
    assert "today  0.00 USD" in out
    assert "no data" not in out


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


def test_render_snapshot_renders_monthly_window():
    # codex-cli reports a "Monthly limit:" row on some plans; it is a window
    # in its own right, so it gets its own labelled bar rather than being
    # folded into weekly (which would misreport the reset horizon).
    snap = UsageSnapshot("test-provider", monthly=Quota(5, 100, "%"))
    lines = render_snapshot(snap).splitlines()
    assert lines[1].startswith("monthly ")
    assert "5.0%" in lines[1]


def test_render_snapshot_orders_windows_shortest_first():
    snap = UsageSnapshot(
        "codex",
        daily=Quota(1, 100, "%"),
        weekly=Quota(2, 100, "%"),
        monthly=Quota(3, 100, "%"),
    )
    windows = {"session", "weekly", "monthly"}
    labels = [
        line.split()[0]
        for line in render_snapshot(snap).splitlines()
        if line.split() and line.split()[0] in windows
    ]
    assert labels == ["session", "weekly", "monthly"]


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
    lines = [_visible(line) for line in render_snapshot(snap).splitlines()]
    assert "" in lines
    blank_idx = lines.index("")
    assert lines[blank_idx - 1].startswith("weekly")
    assert lines[blank_idx + 1] == "Claude & GPT-OSS"


def _visible(line: str) -> str:
    """Strip Textual markup tags, leaving what the terminal actually draws."""
    return re.sub(r"\[/?[^\]]*\]", "", line)


def _two_groups() -> UsageSnapshot:
    return UsageSnapshot(
        "gemini",
        groups=[
            QuotaGroup(
                label="Gemini",
                daily=Quota(0, 100, "%"),
                weekly=Quota(6, 100, "%", reset_note="Refreshes in 97h 24m"),
            ),
            QuotaGroup(
                label="Claude & GPT-OSS",
                daily=Quota(0, 100, "%"),
                weekly=Quota(35, 100, "%", reset_note="Refreshes in 97h 25m"),
            ),
        ],
    )


def test_groups_render_side_by_side_when_the_width_allows():
    # Antigravity reports two pools sharing one account; stacking them makes
    # that card twice as tall as every other one. Given room, they lay out as
    # columns instead -- both labels land on a single line.
    out = render_snapshot(_two_groups(), width=80)
    lines = [_visible(line) for line in out.splitlines()]
    header = next(line for line in lines if "Gemini" in line)
    assert "Claude & GPT-OSS" in header
    assert sum(1 for line in lines if line.strip().startswith("weekly")) == 1


def test_groups_fall_back_to_stacked_when_too_narrow():
    out = render_snapshot(_two_groups(), width=40)
    lines = [_visible(line) for line in out.splitlines()]
    header = next(line for line in lines if "Gemini" in line)
    assert "Claude & GPT-OSS" not in header
    assert sum(1 for line in lines if line.strip().startswith("weekly")) == 2


def test_groups_stack_when_width_is_unknown():
    # None means "no width information" -- take the layout that cannot clip
    # rather than optimistically assuming the card is wide.
    out = render_snapshot(_two_groups())
    header = next(line for line in _visible(out).splitlines() if "Gemini" in line)
    assert "Claude & GPT-OSS" not in header


def test_side_by_side_columns_align_on_visible_width_not_markup_length():
    # Regression guard: the bar carries Textual color tags, so padding the
    # columns with len(markup) would push each row's second column out by the
    # tag length and leave the block visibly ragged.
    out = render_snapshot(_two_groups(), width=80)
    lines = [_visible(line) for line in out.splitlines()]
    starts = {
        line.index("Claude & GPT-OSS") for line in lines if "Claude & GPT-OSS" in line
    }
    starts |= {line.rindex("daily") for line in lines if line.count("daily") == 2}
    starts |= {line.rindex("weekly") for line in lines if line.count("weekly") == 2}
    # The reset note is indented 8 inside its own cell, so back that out to
    # compare column starts rather than word starts.
    starts |= {
        line.rindex("Refreshes") - 8 for line in lines if line.count("Refreshes") == 2
    }
    assert len(starts) == 1, f"second column starts at differing offsets: {starts}"


def test_group_labels_are_dimmed_without_affecting_column_alignment():
    # The dim tags are markup, not drawn characters -- if they were counted as
    # width the label column would pad short by their length.
    out = render_snapshot(_two_groups(), width=80)
    assert "[dim]Gemini[/dim]" in out
    assert "[dim]Claude & GPT-OSS[/dim]" in out
    lines = [_visible(line) for line in out.splitlines()]
    header = next(line for line in lines if "Gemini" in line)
    daily = next(line for line in lines if line.count("daily") == 2)
    assert header.index("Claude & GPT-OSS") == daily.rindex("daily")


def test_single_group_is_not_laid_out_as_columns():
    snap = UsageSnapshot(
        "gemini", groups=[QuotaGroup(label="Gemini", weekly=Quota(6, 100, "%"))]
    )
    out = render_snapshot(snap, width=200)
    # A lone group keeps the normal full-width bar rather than the narrow one
    # reserved for columns.
    assert render_bar(6.0) in out


def test_render_stale_accepts_a_width():
    out = render_stale(_two_groups(), "pty timed out", width=80)
    header = next(line for line in _visible(out).splitlines() if "Gemini" in line)
    assert "Claude & GPT-OSS" in header


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
    assert has_data(UsageSnapshot("codex", monthly=Quota(5, 100, "%")))
    assert has_data(UsageSnapshot("deepseek", balance=Balance(1.0, "CNY")))
    assert has_data(UsageSnapshot("gemini", groups=[QuotaGroup(label="Gemini", weekly=Quota(1, 2, "%"))]))
    assert has_data(UsageSnapshot("openrouter", spend=[Spend("today", 0.0, "USD")]))
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


def test_codex_daily_window_is_labeled_session():
    # Codex's "daily" window is actually its rolling rate-limit window (the
    # CLI's own "5h limit:" row, see providers/codex.py's _DAILY_RE) rather
    # than a calendar day, and its length isn't always 5 hours across plans --
    # so it reads "session" like Claude's special case, not the generic
    # "daily".
    snap = UsageSnapshot("codex", daily=Quota(12, 50, "messages"))
    lines = render_snapshot(snap).splitlines()
    assert any(line.startswith("session") for line in lines)
    assert not any(line.startswith("daily") for line in lines)


def test_other_providers_keep_the_daily_label():
    snap = UsageSnapshot("gemini", daily=Quota(12, 50, "messages"))
    lines = render_snapshot(snap).splitlines()
    assert any(line.startswith("daily") for line in lines)
    assert not any(line.startswith("session") for line in lines)


def test_render_snapshot_group_separates_daily_and_weekly():
    snap = UsageSnapshot(
        "gemini",
        groups=[QuotaGroup(label="Gemini", daily=Quota(0, 100, "%"), weekly=Quota(6, 100, "%"))],
    )
    lines = render_snapshot(snap).splitlines()
    daily_idx = next(i for i, line in enumerate(lines) if line.startswith("daily"))
    weekly_idx = next(i for i, line in enumerate(lines) if line.startswith("weekly"))
    assert weekly_idx == daily_idx + 2
    assert lines[daily_idx + 1] == ""
def test_remaining_render_matches_web_including_groups_and_stale():
    from aitop.render import QuotaDisplay, format_remaining_value, render_stale
    from aitop.web import snapshot_to_dict

    snap = UsageSnapshot("codex", daily=Quota(12, 50, "messages"),
                         groups=[QuotaGroup("pool", weekly=Quota(95, 100, "%", "Refreshes in 97h 24m"))])
    display = QuotaDisplay(show_remaining=True, reset_countdown=True)
    text = render_snapshot(snap, display=display)
    data = snapshot_to_dict(snap)
    assert data["daily"]["remaining_value"] in text
    assert "38/50 messages left (76.0%)" in text
    assert "5.0% left" in text and "[red]" in text
    assert "Reset in 4d 1h 24m" in text
    assert "38/50 messages left (76.0%)" in render_stale(snap, "offline", display=display)
    assert format_remaining_value(Quota(1, 0, "%")) == "Remaining unknown"
