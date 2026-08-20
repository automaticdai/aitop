# ai-pal

`ai-pal` is a terminal dashboard (built with [Textual](https://github.com/Textualize/textual)) that polls your AI coding-assistant accounts on a timer and shows how much of each one's usage quota you've burned through. It watches four providers — Claude, Codex, Gemini, and DeepSeek — and renders a daily/weekly usage bar (or an account balance, for DeepSeek) for each one in a single always-on-screen view, so you can tell at a glance which assistant you're about to rate-limit yourself out of.

## Install

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs `ai-pal` itself plus its runtime dependencies (`textual`, `httpx`, `pyte`) and, via the `dev` extra, `pytest` for running the test suite. The install also registers an `ai-pal` console script (from the `[project.scripts]` entry point), so once the venv is active you can just run `ai-pal` instead of `python -m ai_pal.cli`.

## Running it

```bash
ai-pal              # live mode: polls real accounts/CLIs
ai-pal --mock       # mock mode: synthetic data, no credentials or CLIs needed
ai-pal --config path/to/config.toml   # use a config file at a custom path
```

`--mock` is the easiest way to try the tool or develop against it: it swaps in a `MockProvider` for every enabled provider, which returns fixed, made-up quota/balance numbers immediately, with no network calls, no API keys, and none of the vendor CLIs installed.

In live mode, each provider only actually reports data if its own credentials/CLI are set up correctly (see below) — a provider that isn't ready simply shows an error line rather than crashing the app.

### Key bindings

- `q` — quit
- `r` — refresh all providers immediately (instead of waiting for the next scheduled poll)

Note: a per-provider "detail" pane (originally sketched as a `d` binding) is not implemented in this version — each row shows only the summary bar. The full raw response/screen-scrape text is still captured internally on every snapshot (`UsageSnapshot.raw`), so a detail view can be added later without changing any adapter.

## What each provider needs

`ai-pal` gets Claude, Codex, and Gemini usage a different way than DeepSeek's balance: DeepSeek has a normal HTTP usage API, but Codex and Gemini do not expose one that a personal/consumer account can call directly (Codex's usage endpoint is blocked by Cloudflare for consumer accounts; Gemini's old per-individual OAuth tier was deprecated by Google in favor of the Antigravity CLI). Claude's own CLI happens to be the most reliable way to read *its* own account's usage too, so all three are handled the same way for consistency. See "The PTY-scrape caveat" below.

| Provider | How it's fetched | What you need |
|---|---|---|
| **DeepSeek** | Direct HTTPS call to `https://api.deepseek.com/user/balance` | The `DEEPSEEK_API_KEY` environment variable set to a valid DeepSeek API key |
| **Codex** | Screen-scrapes the `codex` CLI's `/status` panel over a PTY | The `codex` CLI installed and already logged in (`codex` on your `PATH`) |
| **Gemini** | Screen-scrapes the Antigravity CLI's `/usage` panel over a PTY | The `agy` CLI (Antigravity, not the `antigravity` desktop app) installed and already logged in, with quota reported under a "GEMINI MODELS" section |
| **Claude** | Screen-scrapes the `claude` CLI's `/usage` panel over a PTY | The `claude` CLI installed and already logged in |

For DeepSeek, set the key before launching:

```bash
export DEEPSEEK_API_KEY=sk-...
ai-pal
```

For Codex/Gemini/Claude, "already logged in" means: run that vendor's CLI by hand once (`codex`, `agy`, or `claude`) and complete whatever login/auth flow it prompts for, so it has a working, non-expired session saved on disk. `ai-pal` does not perform any login flow itself and does not refresh expired tokens — it only reads whatever session the CLI already has. If a CLI's session has expired, that provider's row will show an error (e.g. a 401-style message) telling you to re-run the CLI to refresh it, rather than the app trying to fix it for you.

### The PTY-scrape caveat

For Codex, Gemini, and Claude, `ai-pal` does not talk to any usage API directly. Instead it spawns the vendor's own interactive CLI inside a pseudo-terminal (PTY), drives it with a scripted sequence of keystrokes (dismiss any first-run dialog, run the CLI's own usage/status slash command, wait for it to render), and then parses the resulting on-screen text with regular expressions to pull out percentages.

This is a deliberate v1 tradeoff, not an accident or a bug:

- It requires each vendor's CLI to be **installed and already authenticated** on the same machine `ai-pal` runs on — `ai-pal` piggybacks on that CLI's session rather than doing its own OAuth.
- It is inherently fragile to each CLI changing its own UI: if a future version of `codex`, `agy`, or `claude` renames its usage command, restyles its output, or adds/removes a dialog, the corresponding adapter's screen-scrape can silently stop matching and start reporting "no data" for that window (each adapter is written to fail closed — returning `None` for a quota it can't confidently parse — rather than fabricate a number).
- Each fetch briefly spawns and kills a real CLI subprocess on every poll (bounded by an internal timeout, ~11s), so a slow-starting CLI can occasionally show up as a timeout on a given cycle even when everything is configured correctly.

If a vendor ever ships a real, individually-authenticatable HTTP usage API, that provider's adapter could be swapped for a direct HTTP call (like DeepSeek's) without changing anything else in the app.

## Configuration

On startup, `ai-pal` looks for a TOML config file at:

```
~/.config/ai-pal/config.toml
```

(or at the path given via `--config`). If the file doesn't exist, built-in defaults are used — you don't need to create one to run the app.

Example config showing everything that's currently configurable:

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

- `refresh_interval_s` — how often (in seconds) the app polls all enabled providers again after a full round finishes. Defaults to `30`.
- `[providers.<name>]` — one optional table per provider (`claude`, `codex`, `gemini`, `deepseek`). Any provider omitted from the file keeps its defaults.
  - `enabled` — set to `false` to skip a provider entirely (it won't be polled or fetched at all; its row stays in the layout showing the initial "loading…" placeholder, since nothing ever updates it). Defaults to `true`.
  - `timeout_s` — per-provider timeout setting in seconds. Defaults to `15`.

You only need to specify the keys you want to override; anything left out falls back to the default shown above.

## Development

Run the test suite from the repo root with the project's venv:

```bash
.venv/bin/python -m pytest -q
```
