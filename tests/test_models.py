from ai_pal.models import Balance, Quota, UsageSnapshot


def test_quota_pct():
    assert Quota(5, 10, "messages").pct == 50.0
    assert Quota(1, 3, "messages").pct == 33.3  # rounded to 1 decimal


def test_quota_pct_unlimited():
    assert Quota(5, 0, "messages").pct is None
    assert Quota(5, -1, "messages").pct is None


def test_snapshot_defaults():
    s = UsageSnapshot("claude")
    assert s.ok is True
    assert s.error is None
    assert s.daily is None and s.weekly is None and s.balance is None
    assert s.raw == {}


def test_balance_fields():
    b = Balance(225.05, "CNY")
    assert b.amount == 225.05
    assert b.currency == "CNY"
