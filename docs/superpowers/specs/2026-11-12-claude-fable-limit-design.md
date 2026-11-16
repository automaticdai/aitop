# Claude per-model weekly limit (Fable) — design

## Intent

Claude Code's `/usage` panel now renders a per-model weekly limit next to the
aggregate one:

```
Current week (all models)   33% used   Resets Nov 17, 8am (Europe/London)
Current week (Fable)        64% used   Resets Nov 17, 8am (Europe/London)
```

aitop only parses `Current week (all models)`, so the card hides the limit the
user is likely to hit first. The Claude card should show it.

## Goals / non-goals

- Goal: the Claude card shows every `Current week (<name>)` limit the panel
  renders, in the TUI and the web dashboard.
- Goal: accounts whose panel has no per-model row are unaffected.
- Non-goal: any rendering, config, or settings change.
- Anti-goal: a per-model number leaking into the aggregate `weekly` slot.

## Approaches

1. **Labelled group (chosen).** Each `Current week (<name>)` block other than
   "all models" becomes a `QuotaGroup(label=<name>, weekly=...)` on
   `UsageSnapshot.groups`. Both renderers already draw groups, so this is a
   parser-only change, and matching by pattern picks up future per-model limits
   without a release. Cost: a "Fable" header line on the card.
2. **Flat third row.** A new snapshot field rendered as a `fable` row. More
   compact, but needs a model field plus changes in both renderers, and is
   hard-coded to one model. Rejected.

## Design

- `src/aitop/providers/claude.py`: find all `Current week (<name>)` headers;
  skip "all models"; build each quota with the existing block-scoped
  percentage/reset lookup; set `groups` to the list, or `None` when empty.
- Capture completion (`_DONE_PATTERNS`) is unchanged: the panel paints all
  bars together, and requiring a per-model row would break accounts without one.
- Tests: new fixture from a real capture containing the Fable row; the existing
  fixture asserts `groups is None`; the existing "(Opus) must not fill the
  aggregate weekly slot" guard keeps that assertion and additionally expects
  the Opus group.
- CHANGELOG entry.

## Addendum: intermittent row (found during implementation)

Polled at aitop's default 30s cadence, the real CLI rendered the Fable row in
only 1 of 10 fully painted captures; after a few idle minutes it always
rendered. Waiting the full timeout did not bring it back, so it is not a paint
race. To stop the group blinking, `ClaudeProvider` holds the last per-model
groups it saw and reuses them for up to 30 minutes when a capture (that still
parsed the aggregate weekly bar) comes back without them. After 30 minutes
unseen, the group is dropped. The held value is stored in
`~/.cache/aitop/claude_model_limits.json` rather than in memory: the Poller
rebuilds providers every round (so instance state is lost), and the file lets
a service and a TUI share sightings and keeps the group across restarts. This is the builder's call, not something the
user chose.

## Left to the builder

Regex details and helper naming.
