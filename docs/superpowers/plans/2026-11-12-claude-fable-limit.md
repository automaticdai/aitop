# Claude Per-Model Weekly Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Claude card shows every `Current week (<name>)` per-model limit (today: Fable) as a labelled group.

**Architecture:** Parser-only change in the Claude provider: per-model weekly blocks become `QuotaGroup`s on `UsageSnapshot.groups`, which the TUI and web renderers already draw.

**Tech Stack:** Python, pytest.

**Spec:** `docs/superpowers/specs/2026-11-12-claude-fable-limit-design.md`

## Global Constraints

- No changes to `models.py`, `render.py`, `web.py`, or `_DONE_PATTERNS`.
- `snap.weekly` is filled only from `Current week (all models)`.
- `snap.groups` is `None` (not `[]`) when the panel has no per-model row.

## Review Focus

- Panel with a per-model row but no "(all models)" row: `weekly is None`, group still present.
- Per-model block with no `Resets` line: `reset_note is None`, never the next block's.
- Per-model header on screen whose bar has not parsed: no group, no fabricated number.
- Two per-model rows: two groups, in screen order.
- Old-format panel (existing fixture): `groups is None`.

---

### Task 1: Parse per-model weekly limits into groups

**Files:**
- Create: `tests/fixtures/claude_usage_fable.txt` (real capture, account identifiers scrubbed)
- Modify: `src/aitop/providers/claude.py`
- Modify: `tests/test_claude.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `ClaudeProvider.parse(text).groups: list[QuotaGroup] | None`, each `QuotaGroup(label=<name>, weekly=Quota(used, 100.0, "%", reset_note))`.

- [ ] **Step 1: Write failing tests** in `tests/test_claude.py`:
  - `test_parse_fable_fixture`: weekly `33%`, `groups == [QuotaGroup("Fable", weekly=Quota(64.0, 100.0, "%", "Resets Nov 17, 8am (Europe/London)"))]`.
  - `test_real_fixture_has_no_groups`: old fixture gives `groups is None`.
  - `test_two_per_model_rows_in_screen_order`, `test_per_model_row_without_reset_does_not_borrow`, `test_per_model_header_without_bar_yields_no_group`.
  - Extend `test_parse_does_not_match_per_model_weekly_variant`: `weekly is None` and one `Opus` group.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_claude.py -q` — new tests FAIL.
- [ ] **Step 3: Implement** `_model_groups(text: str) -> list[QuotaGroup] | None` in `claude.py` using `_MODEL_WEEK_RE = re.compile(r"Current week \(([^()\n]+)\)")`, skipping `all models`, reusing `_quota_from_block`; wire into `parse`.
- [ ] **Step 4: Run** the full suite `.venv/bin/python -m pytest -q` — all PASS.
- [ ] **Step 5: Verify live**: run the provider against the real CLI and render the card.
- [ ] **Step 6: CHANGELOG** entry under an Unreleased heading.
