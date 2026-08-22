from aitop.models import Balance, Quota, QuotaGroup, UsageSnapshot


def test_quota_pct():
    assert Quota(5, 10, "messages").pct == 50.0
    assert Quota(1, 3, "messages").pct == 33.3  # rounded to 1 decimal


def test_quota_pct_unlimited():
    assert Quota(5, 0, "messages").pct is None
    assert Quota(5, -1, "messages").pct is None


def test_quota_reset_note_defaults_to_none():
    assert Quota(5, 10, "messages").reset_note is None


def test_quota_reset_note_stores_vendor_native_text():
    q = Quota(5, 10, "%", reset_note="resets 14:11 on 27 Aug")
    assert q.reset_note == "resets 14:11 on 27 Aug"


def test_snapshot_defaults():
    s = UsageSnapshot("claude")
    assert s.ok is True
    assert s.error is None
    assert s.daily is None and s.weekly is None and s.balance is None
    assert s.raw == {}
    assert s.groups is None


def test_snapshot_groups_holds_named_quota_groups():
    groups = [
        QuotaGroup(label="Gemini", daily=Quota(0, 100, "%"), weekly=Quota(6, 100, "%")),
        QuotaGroup(label="Claude & GPT-OSS", daily=None, weekly=Quota(35, 100, "%")),
    ]
    s = UsageSnapshot("gemini", groups=groups)
    assert s.groups == groups
    assert s.groups[0].label == "Gemini"
    assert s.groups[1].daily is None


def test_balance_fields():
    b = Balance(225.05, "CNY")
    assert b.amount == 225.05
    assert b.currency == "CNY"
    assert b.available is True
