# ai-pal — Design Spec

**Date:** 2026-08-20
**Repo:** https://github.com/automaticdai/ai-pal

## 1. Overview

`ai-pal` is a terminal UI (TUI) that shows, in one live dashboard, how much of
each AI coding assistant's usage limits have been consumed. It monitors four
tools:

| Tool | What it shows |
|---|---|
| Claude Code | % of daily and weekly limits used (real Anthropic/Claude account) |
| Codex | % of daily and weekly limits used (ChatGPT subscription) |
| Gemini | % of daily and weekly limits used (Gemini subscription) |
| DeepSeek | Account balance (pay-per-use; no daily/weekly limit) |

The user checks the first three via each CLI's in-app `/usage` command and
DeepSeek via the official website. `ai-pal` consolidates all four into one
screen that refreshes automatically.

## 2. Goals and non-goals

**Goals**

- Show, per provider, the % of daily and weekly limits consumed, live.
- Show DeepSeek's balance (and currency) since it has no limit concept.
- Refresh automatically on a configurable interval; support manual refresh.
- Degrade gracefully: one provider failing must not affect the others.

**Non-goals** (explicitly out of scope for v1)

- No aggregation of raw token counts or cost history.
- No alerting/notifications.
- No multi-machine or daemon/service mode (it is a foreground TUI).
- No editing of limits or submitting quota requests.

## 3. Architecture

```
┌──────────────────────────────────────────────┐
│                 Textual App                   │
│   (layout, progress bars, timer, key bindings)│
└───────────────┬──────────────────────────────┘
                │  renders list of UsageSnapshot
┌───────────────▼──────────────────────────────┐
│              Scheduler                        │
│   async poll each provider on interval,       │
│   staggered, per-provider timeout, cache      │
└───────┬──────────┬──────────┬──────────┬──────┘
        │          │          │          │
   ┌────▼───┐ ┌────▼───┐ ┌────▼───┐ ┌────▼───┐
   │Claude  │ │ Codex  │ │ Gemini │ │DeepSeek│   ← Provider adapters
   │Adapter │ │ Adapter│ │ Adapter│ │Adapter │
   └────────┘ └────────┘ └────────┘ └────────┘
```

Each provider adapter implements a single async method and returns a
normalized `UsageSnapshot`. Adapters are the only code that knows anything
about a specific provider's data source; the app and scheduler operate purely
on the normalized model.

## 4. Data model

`UsageSnapshot` — the single normalized type the TUI renders.

```python
@dataclass
class Quota:
    used: float            # consumed amount
    limit: float           # plan limit (0 or None => "unlimited"/unknown)
    unit: str              # "messages" | "hours" | "tokens"
    pct: float | None      # used/limit*100, None when limit unknown

@dataclass
class UsageSnapshot:
    provider: str          # "claude" | "codex" | "gemini" | "deepseek"
    ok: bool               # did this fetch succeed?
    error: str | None      # message when ok is False
    fetched_at: float      # epoch seconds
    daily: Quota | None
    weekly: Quota | None
    balance: Balance | None   # DeepSeek only
    raw: dict                 # provider-specific payload, for the detail pane

@dataclass
class Balance:
    amount: float
    currency: str
```

Provider protocol:

```python
class Provider(Protocol):
    name: str
    async def fetch(self) -> UsageSnapshot: ...
```

Rules:

- `daily`/`weekly` are `None` for DeepSeek (it has no limit); `balance` is
  `None` for the subscription providers.
- `pct` is derived, never stored by the adapter; adapters return `used`/`limit`
  and the model computes `pct`.
- On any failure the adapter returns a snapshot with `ok=False` and a human
  `error` string; it never raises into the scheduler.

## 5. Provider adapters and data sourcing

| Provider | Primary source | Fallback |
|---|---|---|
| DeepSeek | `GET https://api.deepseek.com/user/balance` with `DEEPSEEK_API_KEY` (verified working) | — |
| Codex | OpenAI ChatGPT rate-limit endpoint, hit directly with the OAuth token in `~/.codex/auth.json` (refresh via its `refresh_token`) | Drive `/usage` via PTY + parse screen |
| Gemini | Google Gemini subscription usage endpoint, OAuth from `~/.gemini/oauth_creds.json` | Drive `/usage` via PTY |
| Claude | claude.ai plan-usage endpoint, `claudeAiOauth` from `~/.claude/.credentials.json` | Drive `/usage` via PTY |

**Endpoint pinning.** Codex and Gemini are open-source; their exact `/usage`
endpoint and response shape are read from their source during implementation
(the same data `/usage` renders, fetched directly — not blind
reverse-engineering). Claude is a closed binary; its endpoint is pinned from
its debug output / network capture during implementation. Each adapter's
parser is unit-tested against a saved fixture of the real response.

**PTY fallback.** When the direct HTTP path is unavailable, the adapter spawns
the CLI in a pseudo-terminal, sends `/usage`, and parses the rendered
full-screen buffer. This is the least-preferred path (slower, format-fragile)
and is isolated behind the same `Provider.fetch()` interface.

**Auth.** Credentials are read, never written, by `ai-pal`:

- DeepSeek: `DEEPSEEK_API_KEY` env var.
- Codex: `~/.codex/auth.json` (`access_token`, `refresh_token`, `account_id`).
- Gemini: `~/.gemini/oauth_creds.json` (`access_token`, `refresh_token`).
- Claude: `~/.claude/.credentials.json` (`claudeAiOauth`).

OAuth refresh is handled by a shared `sources.py` helper that exchanges the
stored `refresh_token` and persists the new access token back to the same file,
so the adapter uses a valid token next poll.

## 6. TUI design

Layout (top to bottom):

1. **Header** — title `ai-pal`, last-refresh timestamp, and one status dot per
   provider (green=ok, amber=stale, red=error).
2. **Provider rows** — one per provider:
   - Claude / Codex / Gemini: two progress bars (daily %, weekly %) with
     `used/limit` numbers and unit, plus the % as text. Color: green below 70%,
     amber 70–90%, red above 90%.
   - DeepSeek: no bars; shows `balance` and currency (e.g. `¥225.05 CNY`).
3. **Footer** — key hints: `q` quit, `r` refresh now, `d` detail pane, and the
   configured auto-refresh interval.

Interactions:

- `q` — quit.
- `r` — force an immediate refresh of all providers.
- `d` — toggle a detail pane that dumps `raw` for the focused provider.
- Mouse/scroll — focus a row (Textual default).

## 7. Refresh and resilience

- Poll interval: default **30 s**, configurable.
- Providers are polled **asynchronously** (not serial) and **staggered** so the
  three CLIs are not spawned simultaneously when the PTY fallback is active.
- Per-provider timeout: default **15 s**.
- On failure: the row is marked **stale**, keeps showing the last good value,
  and gets a red status dot; the error text is available in the detail pane.
  A provider error never crashes the dashboard or blocks other providers.

## 8. Configuration

File: `~/.config/ai-pal/config.toml` (created on first run with defaults).

```toml
refresh_interval_s = 30

[providers.claude]
enabled = true
timeout_s = 15

[providers.codex]
enabled = true
timeout_s = 15

[providers.gemini]
enabled = true
timeout_s = 15

[providers.deepseek]
enabled = true
timeout_s = 15
```

Defaults are used when the file is absent; only non-default values need to be
present.

## 9. Testing strategy

- **Model unit tests** — `Quota`/`UsageSnapshot` derivation (e.g. `pct`
  rounding, `limit=0` => unlimited, unit normalization).
- **Adapter parser tests** — each adapter's HTTP response parser is tested
  against a saved fixture of a real response (DeepSeek's is already captured).
- **Auth helper tests** — token file reading, missing-file handling, OAuth
  refresh error paths (mocked HTTP).
- **`--mock` mode** — the app accepts `--mock` and feeds canned
  `UsageSnapshot`s so the TUI runs with no live credentials, for development
  and manual testing.

## 10. Project structure

```
ai-pal/
  pyproject.toml               # name "ai-pal", deps: textual, httpx, pexpect
  src/ai_pal/
    __init__.py
    cli.py                     # entrypoint, --mock, --config
    app.py                     # Textual app + layout + key bindings
    models.py                  # UsageSnapshot, Quota, Balance, Provider
    config.py                  # TOML load with defaults
    scheduler.py               # async polling, staggering, caching
    sources.py                 # credential load + OAuth refresh
    providers/
      __init__.py
      base.py                  # Provider protocol + shared PTY driver
      deepseek.py
      claude.py
      codex.py
      gemini.py
  tests/
    test_models.py
    test_deepseek.py
    test_sources.py
    fixtures/
      deepseek_balance.json
      codex_usage.json
      gemini_usage.json
      claude_usage.json
  README.md
```

## 11. Implementation order

1. Scaffold (`pyproject.toml`, package layout, `cli.py` running an empty
   Textual app).
2. `models.py` + `config.py` + unit tests.
3. `scheduler.py` with `--mock` provider, wired into the TUI.
4. DeepSeek adapter (endpoint already verified) + parser tests.
5. Codex adapter (read open-source endpoint, pin it) + tests.
6. Gemini adapter (same) + tests.
7. Claude adapter (pin endpoint from debug output) + tests.
8. PTY fallback driver + wire as fallback per adapter.
9. README, polish (color thresholds, detail pane), final review.

## 12. Risks and open questions

- **Claude sourcing.** The user's Claude Code is currently routed to DeepSeek
  via `ANTHROPIC_BASE_URL`, but they want the *real* Claude account limits.
  The Claude adapter targets `claude.ai` (`claudeAiOauth`) directly. If that
  OAuth is invalid or the endpoint is not reachable, the adapter reports
  `ok=False` with a clear message rather than showing DeepSeek numbers. This is
  the highest-risk adapter.
- **Private endpoints.** The three subscription endpoints are undocumented and
  may change. Mitigation: parser fixtures + PTY fallback + the adapter
  interface makes each independently swappable.
- **PTY fragility.** Full-screen TUI scraping is slow and format-sensitive. It
  is a last resort only.
- **DeepSeek has no daily/weekly limit.** Its row intentionally shows balance,
  not %. If the user later wants DeepSeek spend *rate*, that is a follow-up.
