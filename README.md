# aitop

`aitop` monitors usage quotas for Claude Code, Codex, Antigravity (agy), and DeepSeek. Use the terminal dashboard, built with [Textual](https://github.com/Textualize/textual), or the web dashboard to follow session, weekly, and monthly quotas, reset times, and account balances.

## Install

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs `aitop` itself plus its runtime dependencies (`textual`, `httpx`, `pyte`, `starlette`, `uvicorn`, `tomlkit`) and, via the `dev` extra, `pytest` for running the test suite. The install also registers an `aitop` console script (from the `[project.scripts]` entry point), so once the venv is active you can just run `aitop` instead of `python -m aitop.cli`.

## TUI

The terminal interface shows each provider in a bordered panel, with configurable grid placement and automatic resizing.

![aitop terminal dashboard](docs/screenshot.png)

### Run the TUI

```bash
aitop              # live mode: polls real accounts/CLIs
aitop --mock       # mock mode: synthetic data, no credentials or CLIs needed
aitop --config path/to/config.toml   # use a config file at a custom path
aitop --no-web     # force the web view off (even if config enables it)
```

`--mock` is the easiest way to try the tool or develop against it: it swaps in a `MockProvider` for every enabled provider, which returns fixed, made-up quota/balance numbers immediately, with no network calls, no API keys, and none of the vendor CLIs installed.

In live mode, each provider only actually reports data if its own credentials/CLI are set up correctly (see below) — a provider that isn't ready simply shows an error line rather than crashing the app.

### Reading a row

Each provider gets its own bordered block, titled with its display name (Claude Code / Codex / Antigravity (agy) / DeepSeek — the internal provider keys used in the config file are unchanged: `claude`, `codex`, `gemini`, `deepseek`). Inside, a status dot: green when the last fetch returned usable numbers, amber when the fetch succeeded but nothing parseable came back (`no data (check the CLI's login state)`), red on an outright error. A failed fetch does **not** wipe the row: it keeps showing the last good numbers, marked `(stale — <error>)`, so one transient CLI timeout doesn't lose the values you were watching. Usage bars are colored by severity — green below 70% used, amber 70–90%, red above 90% — and the header carries the time of the most recent update.

When a CLI's screen shows a reset time for a quota window, that's captured verbatim (no timezone conversion or normalization — each vendor's own wording, as-is) and shown on a line under that bar. The one exception is Claude Code's session reset, which arrives as a wall-clock time (`Resets 11pm (Europe/London)`) and is converted to a relative `Reset in 2h 30m` countdown instead.

Antigravity's block shows two named groups rather than one bar, separated by a blank line, because the `agy` CLI reports two separate quota pools sharing the same account: "Gemini" (Gemini Flash/Pro) and "Claude & GPT-OSS" (a combined pool for Claude Opus/Sonnet and GPT-OSS models). Both are read from the same screen and rendered under their own labels.

DeepSeek's block shows only its account balance (it has no quota/limit concept, so there's no bar). If the API reports the balance as insufficient for calls (`is_available: false` — which can happen even with a nonzero balance, e.g. during a payment hold) that's flagged inline in red rather than left for you to notice the hard way.

### Key bindings

- `q` — quit
- `r` — refresh all providers immediately (instead of waiting for the next scheduled poll)
- `w` — show the web view's status (running/disabled, host, port, URL)

Note: a per-provider "detail" pane (originally sketched as a `d` binding) is not implemented in this version — each row shows only the summary bar. The full raw response/screen-scrape text is still captured internally on every snapshot (`UsageSnapshot.raw`), so a detail view can be added later without changing any adapter.

## Web

The web dashboard shows provider logos, remaining-quota bars, balances, and reset notes. Its Menu controls the grid, provider order, and visibility of Antigravity's Claude & GPT-OSS quota group. Drag cards to reorder them; preferences are saved to `config.toml` and shared across browsers.

![aitop web dashboard with provider logos and a two-column grid](docs/screenshot_web.png)

*Web dashboard with demo data.*

### Run the web dashboard

```bash
aitop --web             # serve the web dashboard alongside the TUI
aitop --headless        # run only the web dashboard
aitop --headless --mock # try the web dashboard with demo data
```

The web server is off by default. Enable it with `--web`, `--headless`, or `[web] enabled = true` in the config file. `--no-web` disables it when running the TUI. The server serves:

- `http://127.0.0.1:8787/` — the dashboard page, refreshed every `refresh_interval_s` seconds.
- `http://127.0.0.1:8787/api/snapshots` — the same data as raw JSON.

The page shows one card per provider with a bar showing quota left (`100% − usage`), labelled “left”: green above 30% remaining, amber from 10–30%, red below 10%, the balance, reset notes, and Antigravity's two quota groups — and it honors the same stale/error semantics: a failed fetch keeps showing the last good numbers, marked stale. It binds to loopback by default, so nothing is exposed off-box unless you change `[web] host`. Under WSL2 the default is widened to `0.0.0.0` automatically, so your Windows browser can open `localhost` (WSL2 is NAT-isolated, so this still doesn't expose the page to the LAN). **The endpoints have no authentication** — keep `host` on `127.0.0.1` (or the WSL2 default) unless you're on a network you trust. Press `w` in the TUI to see the web view's status. The server runs in a background thread and is optional — a bind failure (e.g. the port is taken) is logged and the dashboard keeps running.

### Menu and layout

The web view stores its layout in `[web.layout]`: `adaptive` fits the browser
width and `custom` uses saved rows and columns. The base `[layout]` and provider
positions still determine which providers are enabled and their order.
The page reloads snapshots every
`refresh_interval_s` seconds; provider fetches and timeouts are shared with the TUI.
Edit the config and restart `aitop` to apply changes.

Use **Menu** in the web page header to automatically fit cards to the screen
or set custom rows and columns. Stack, two-column, and
single-row presets preview immediately. The **Claude & GPT-OSS** switch shows or
hides that quota group inside Antigravity; the separate Claude Code and Codex
cards remain available. **Save settings** writes the layout to `[web.layout]`
and the switch to `[web] show_claude_gpt` in the active config file. Changes apply
immediately, reach other open browsers on their next refresh, and survive a
service restart. Closing the menu cancels the preview. Custom grids keep all enabled providers
visible. Provider polling and the base TUI layout are unchanged by these web
display settings. Old browser-local layout preferences are no longer used.
The menu also shows the author, installed version, and a link to the GitHub repository.

Drag a card or its grip to reorder providers; each drop saves immediately to
`[web] provider_order`. The grips also support touch dragging and arrow keys.
**Menu → Provider order** offers earlier/later buttons with live preview; click
**Save settings** to keep that order, or close the menu to cancel. Both controls
share the same saved order across browsers and service restarts. A failed drop
save restores the last saved order and shows an error.

Config saves preserve comments and unrelated settings, and a failed save is
reported in the menu. The settings API requires the token embedded in the page;
reload an old page if you restarted the service before saving.

The dashboard uses a navy usage-bar icon with blue, mint, and amber accents
in its header and browser tab, alongside local provider
logos, larger page margins, and card spacing that adjusts for smaller screens.
Provider SVGs are from [Lobe Icons](https://github.com/lobehub/lobe-icons), with
their MIT license included in the package.

### Run automatically when WSL starts

`--headless` runs the same provider polling and web dashboard without a terminal.
It implies `--web`; combining it with `--no-web` is an error. SIGTERM stops polling
and cleans up active provider CLI processes before the server exits.

WSL needs [systemd enabled](https://learn.microsoft.com/en-us/windows/wsl/systemd)
(`systemd=true` under `[boot]` in `/etc/wsl.conf`). If you change that setting,
restart WSL from PowerShell with `wsl --shutdown`, then reopen the distribution.

Install the example [user service](docs/aitop.service):

```bash
mkdir -p ~/.config/systemd/user
cp docs/aitop.service ~/.config/systemd/user/aitop.service
```

Edit the installed unit to match your checkout, Python environment, config path,
and provider CLI `PATH`. The example assumes a checkout at `~/aitop` with a
`.venv`; if you use nvm, include the directory containing `node` and `codex`
(check with `command -v node` and `command -v codex`). The service runs as your
Linux user and uses your existing provider logins. It does not source shell
startup files. Put any required environment variables, such as
`DEEPSEEK_API_KEY=...`, in `~/.config/aitop/service.env` and set its permissions
to `600`.

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now aitop.service
```

[Lingering](https://www.freedesktop.org/software/systemd/man/252/loginctl.html)
starts the user service manager at boot and keeps it running after logout.
aitop will start when this WSL distribution starts; this does not launch WSL
at Windows sign-in. Open `http://localhost:8787` with the default web port.

```bash
systemctl --user status aitop.service           # status
journalctl --user -u aitop.service -n 50         # recent logs
systemctl --user restart aitop.service         # apply config/code changes
systemctl --user disable --now aitop.service    # stop and disable startup
```

While the service is running, use `aitop --no-web` if you also want the TUI,
so it does not try to bind the service's port. The TUI starts its own polling.

## What each provider needs

`aitop` gets Claude Code, Codex, and Antigravity usage a different way than DeepSeek's balance: DeepSeek has a normal HTTP usage API, but Codex and Antigravity do not expose one that a personal/consumer account can call directly (Codex's usage endpoint is blocked by Cloudflare for consumer accounts; the old per-individual Gemini OAuth tier was deprecated by Google in favor of the Antigravity CLI). Claude Code's own CLI happens to be the most reliable way to read *its* own account's usage too, so all three are handled the same way for consistency. See "The PTY-scrape caveat" below.

| Provider | How it's fetched | What you need |
|---|---|---|
| **DeepSeek** | Direct HTTPS call to `https://api.deepseek.com/user/balance` | The `DEEPSEEK_API_KEY` environment variable set to a valid DeepSeek API key |
| **Codex** | Screen-scrapes the `codex` CLI's `/status` panel over a PTY | The `codex` CLI installed and already logged in (`codex` on your `PATH`) |
| **Antigravity (agy)** | Screen-scrapes the Antigravity CLI's `/usage` panel over a PTY | The `agy` CLI (Antigravity, not the `antigravity` desktop app) installed and already logged in; its screen reports quota under a "GEMINI MODELS" section and a "CLAUDE AND GPT MODELS" section, both of which are shown |
| **Claude Code** | Screen-scrapes the `claude` CLI's `/usage` panel over a PTY | The `claude` CLI installed and already logged in |

For DeepSeek, set the key before launching:

```bash
export DEEPSEEK_API_KEY=sk-...
aitop
```

For Codex/Antigravity/Claude Code, "already logged in" means: run that vendor's CLI by hand once (`codex`, `agy`, or `claude`) and complete whatever login/auth flow it prompts for, so it has a working, non-expired session saved on disk. `aitop` does not perform any login flow itself and does not refresh expired tokens — it only reads whatever session the CLI already has. If a CLI's session has expired, its usage screen never renders (the CLI shows a login prompt instead) and the adapter's regexes simply find nothing to match. There's no explicit expired-session detection, so this isn't reported as a specific error — the row shows an amber dot and `no data (check the CLI's login state)`, which is your cue to go re-run that CLI by hand and check its login state yourself. See "The PTY-scrape caveat" below for why this failure mode can't be distinguished from other parse failures.

### The PTY-scrape caveat

For Codex, Antigravity, and Claude Code, `aitop` does not talk to any usage API directly. Instead it spawns the vendor's own interactive CLI inside a pseudo-terminal (PTY), responds only to specifically recognized startup dialogs, runs the CLI's own usage/status slash command, and then parses the resulting on-screen text with regular expressions to pull out percentages.

This is a deliberate v1 tradeoff, not an accident or a bug:

- It requires each vendor's CLI to be **installed and already authenticated** on the same machine `aitop` runs on — `aitop` piggybacks on that CLI's session rather than doing its own OAuth.
- Each CLI runs in a private app-owned working directory under the user cache, not the directory where you launched `aitop`. Claude Code and Antigravity may ask whether that directory is trusted on first use; `aitop` accepts only their specifically recognized trust screens. This prevents polling from trusting your current repository or loading its project-scoped configuration, while the stable path avoids creating a new vendor trust record on every poll.
- It is inherently fragile to each CLI changing its own UI: if a future version of `codex`, `agy`, or `claude` renames its usage command, restyles its output, or adds/removes a dialog, the corresponding adapter's screen-scrape can silently stop matching and start reporting "no data" for that window (each adapter is written to fail closed — returning `None` for a quota it can't confidently parse — rather than fabricate a number).
- Each fetch briefly spawns and kills a real CLI subprocess on every poll. PTY work runs in a dedicated single-threaded helper process, avoiding an unsafe fork of the multithreaded dashboard. Cancellation waits for that helper to kill and reap its CLI child, so timed-out or interrupted rounds cannot leave hidden captures overlapping the next round. The capture ends as soon as the needed numbers are on screen and is hard-bounded by an internal timeout (~11s). The three CLIs are staggered rather than launched together, and a manual `r` refresh is ignored while a round is already running.

If a vendor ever ships a real, individually-authenticatable HTTP usage API, that provider's adapter could be swapped for a direct HTTP call (like DeepSeek's) without changing anything else in the app.

## Configuration

On startup, `aitop` looks for a TOML config file in this order:

1. `./config.toml` — the project-local file (i.e. the current working directory, when you run `aitop` from there). This overrides everything below.
2. `~/.config/aitop/config.toml` — the per-user fallback, which also keeps working after a real `pip install` (when "the current directory" is no longer the project folder).

You can also point it at a specific path with `--config`, which bypasses both. If neither of the two default locations exists, `aitop` writes a default config to `~/.config/aitop/config.toml` on first run — so you always have a concrete file to edit — then reads it back. You don't need to create one yourself.

Example config showing everything that's currently configurable:

```toml
refresh_interval_s = 30

[layout]
adaptive = false
rows = 2
columns = 2

[providers.claude]
position = [1, 1]

[providers.codex]
position = [1, 2]

[providers.gemini]
position = [2, 1]

[providers.deepseek]
position = [-1, -1]   # off

[web]
enabled = false
host = "127.0.0.1"
port = 8787
show_claude_gpt = true
show_remaining = true
provider_order = ["claude", "codex", "gemini", "deepseek"]

[web.layout]
mode = "adaptive"     # "adaptive" or "custom"
rows = 2              # custom grid dimensions, 1–8
columns = 2
```

- `refresh_interval_s` — how often (in seconds) the app polls all on providers again after a full round finishes. Defaults to `30`.
- `[layout]` — the dashboard's grid. `rows` × `columns`. Defaults to a single column of 4 rows (the original vertical stack), so omitting this section changes nothing.
  - `adaptive` — automatically chooses as many columns as fit the terminal while keeping cards wide enough for their wordmarks. Defaults to `false`. When `true`, `rows` and `columns` are derived from the terminal width and every configured built-in provider is shown in the standard order; all `position` values, including `[-1, -1]`, are ignored.
  - `rows` — number of rows. Defaults to `4`.
  - `columns` — number of columns. Defaults to `1`.
- `[providers.<name>]` — one optional table per provider (`claude`, `codex`, `gemini`, `deepseek`). Any provider omitted from the file keeps its defaults.
  - `position = [row, col]` — where to place the provider, **1-based with row first** (so `[1, 1]` is the top-left cell, and `[1, 2]` is top-right in a 2×2 grid). `[-1, -1]` (or any coordinate with a row/column below 1 or beyond the grid) turns the provider off entirely — it's neither shown nor polled. A provider with no `position` at all auto-fills the next free cell in row-major order. Defaults to unset (auto-fill).
  - `timeout_s` — per-provider fetch timeout in seconds: how long one provider's fetch may run before it's reported as timed out. Defaults to `15`. PTY adapters also cap normal captures at about 11 seconds; a lower configured timeout cancels and cleans up the helper and CLI child immediately.
- `[web]` — the optional web view (off by default). When enabled, `aitop` also serves a live web page and JSON endpoint.
  - `enabled` — whether to start the web server. Defaults to `false`. `--web` / `--no-web` on the command line override this.
  - `host` — address to bind. Defaults to `"127.0.0.1"` (loopback only), widened automatically to `"0.0.0.0"` under WSL2 so a Windows browser can reach `localhost`.
  - `port` — port to bind. Defaults to `8787`.
  - `show_claude_gpt` — show the Claude & GPT-OSS quota group inside Antigravity. Defaults to `true`.
  - `show_remaining` — show quota left in web bars and labels. Defaults to `true`; set `false` for usage. Edit the config and restart to apply. Unknown limits show “Remaining unknown”.
  - `provider_order` — preferred card order, saved by dragging or the Menu. Defaults to `[]`, which follows the base provider order. Disabled providers stay hidden, and enabled providers missing from this list are appended.
- `[web.layout]` — shared web display preferences, saved by the settings menu.
  - `mode` — `"adaptive"` fits the browser width and `"custom"` uses the dimensions below. Defaults to `"adaptive"`.
  - `rows`, `columns` — custom grid dimensions, each from `1` to `8`, defaulting to `2`. The settings menu requires enough cells for all enabled providers.

You only need to specify the keys you want to override; anything left out falls back to the default shown above.

## Development

Run the test suite from the repo root with the project's venv:

```bash
.venv/bin/python -m pytest -q
```

### Refresh the web screenshot

The README screenshot uses the [demo config](docs/screenshot_web.toml):

```bash
aitop --headless --mock --config docs/screenshot_web.toml
```

Open `http://localhost:8788` with a 1440-pixel-wide browser window and dark mode,
then capture the dashboard to `docs/screenshot_web.png`.
