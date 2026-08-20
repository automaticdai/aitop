from ai_pal.models import Balance, Quota, UsageSnapshot
from ai_pal.render import fmt_pct, render_bar, render_snapshot


def test_fmt_pct():
    assert fmt_pct(None) == "—"
    assert fmt_pct(37.5) == "37.5%"


def test_render_bar():
    assert render_bar(None) == "?" * 24
    assert render_bar(50.0, width=10) == "█████░░░░░"


def test_render_snapshot_quota():
    snap = UsageSnapshot(
        "codex",
        daily=Quota(12, 50, "messages"),
        weekly=Quota(30, 200, "messages"),
    )
    out = render_snapshot(snap)
    assert out.splitlines()[0] == "codex"
    assert "12/50" in out
    assert "30/200" in out
    assert "messages" in out


def test_render_snapshot_balance():
    snap = UsageSnapshot("deepseek", balance=Balance(225.05, "CNY"))
    out = render_snapshot(snap)
    assert "225.05" in out and "CNY" in out


def test_render_snapshot_error():
    snap = UsageSnapshot("claude", ok=False, error="token expired")
    out = render_snapshot(snap)
    assert "token expired" in out
