# Configuration reference

On startup, `aitop` looks for a TOML config file in this order:

1. `./config.toml` — the project-local file (i.e. the current working directory, when you run `aitop` from there). This overrides everything below.
2. `~/.config/aitop/config.toml` — the per-user fallback, which also keeps working after a real `pip install` (when "the current directory" is no longer the project folder).

You can also point it at a specific path with `--config`, which bypasses both. If neither of the two default locations exists, `aitop` writes a default config to `~/.config/aitop/config.toml` on first run — so you always have a concrete file to edit — then reads it back. You don't need to create one yourself.

Example configuration:

```toml
refresh_interval_s = 30
show_remaining = true
reset_countdown = true

[layout]
adaptive = false
rows = 2
columns = 2

[providers.claude]
enabled = true
position = [1, 1]
timeout_s = 15

[providers.codex]
position = [1, 2]

[providers.gemini]
position = [2, 1]

[providers.deepseek]
enabled = false      # off, even with adaptive layout
# api_key can be set in the web Menu; DEEPSEEK_API_KEY is the fallback.

[providers.copilot]
enabled = false      # enable after gh auth login; also available in Menu → Providers

[web]
enabled = false
host = "127.0.0.1"
port = 8787
show_claude_gpt = true
show_remaining = true
reset_countdown = true
provider_order = ["claude", "codex", "gemini", "deepseek"]

[web.layout]
mode = "adaptive"     # "adaptive" or "custom"
rows = 2              # custom grid dimensions, 1–8
columns = 2
```

- `refresh_interval_s` — how often (in seconds) the app polls all on providers again after a full round finishes. Defaults to `30`.
- `show_remaining` — show quota left in both interfaces by default. Set `false` to show usage.
- `reset_countdown` — format session reset timers as `yh zm` and other windows as `xd yh zm` in both interfaces. Defaults to `true`; set `false` for vendor wording. Unknown reset notes are preserved. Countdown dates use the vendor timezone when supplied, otherwise the server timezone.
- `[layout]` — the dashboard's grid. `rows` × `columns`. Defaults to a single column of 4 rows (the original vertical stack), so omitting this section changes nothing.
  - `adaptive` — automatically chooses as many columns as fit the terminal while keeping cards wide enough for their wordmarks. Defaults to `false`. When `true`, `rows` and `columns` are derived from the terminal width and every enabled built-in provider is shown in the standard order; all `position` values, including `[-1, -1]`, are ignored.
  - `rows` — number of rows. Defaults to `4`.
  - `columns` — number of columns. Defaults to `1`.
- `[providers.<name>]` — one optional table per provider (`claude`, `codex`, `gemini`, `deepseek`, `copilot`). Any provider omitted from the file keeps its defaults.
  - `enabled` — whether to display and poll this provider. Defaults to `true` for the original four providers; Copilot is off when omitted. An explicit provider table without `enabled` enables that provider. `false` takes precedence over adaptive layout and positions. Menu switches apply immediately to the running poller and TUI. Turning a provider on restores a valid position and expands a fixed grid when necessary.
  - `api_key` — DeepSeek only. A key saved through the Menu takes precedence over `DEEPSEEK_API_KEY`. Saving a key sets config permissions to `600`; API responses expose only whether a key is configured. Leave the Menu field blank to retain it; remove this config key manually to return to the environment variable.
  - Copilot authentication uses `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, then `GITHUB_TOKEN`, or falls back to `gh auth token --hostname github.com`. For a background service, authenticate `gh` as the service user or put the environment token in the service environment. The Copilot CLI is not required. No GitHub token is exposed through the dashboard or stored in aitop's config.
  - `position = [row, col]` — where to place the provider, **1-based with row first** (so `[1, 1]` is the top-left cell, and `[1, 2]` is top-right in a 2×2 grid). `[-1, -1]` (or any coordinate with a row/column below 1 or beyond the grid) turns the provider off entirely — it's neither shown nor polled. A provider with no `position` at all auto-fills the next free cell in row-major order. Defaults to unset (auto-fill).
  - `timeout_s` — per-provider fetch timeout in seconds: how long one provider's fetch may run before it's reported as timed out. Defaults to `15`. PTY adapters also cap normal captures at about 11 seconds; a lower configured timeout cancels and cleans up the helper and CLI child immediately.
- `[web]` — the optional web view (off by default). When enabled, `aitop` also serves a live web page and JSON endpoint.
  - `enabled` — whether to start the web server. Defaults to `false`. `--web` / `--no-web` on the command line override this.
  - `host` — address to bind. Defaults to `"127.0.0.1"` (loopback only), widened automatically to `"0.0.0.0"` under WSL2 so a Windows browser can reach `localhost`.
  - `port` — port to bind. Defaults to `8787`.
  - `show_claude_gpt` — show the Claude & GPT-OSS quota group inside Antigravity. Defaults to `true`.
  - `show_remaining` — optional web override of the top-level setting; omitted values inherit it. Edit the config and restart to apply. Unknown limits show “Remaining unknown”.
  - `reset_countdown` — optional web override of the top-level reset format. The original vendor note is available by hovering over the countdown.
  - `provider_order` — preferred card order, saved by dragging or the Menu. Defaults to `[]`, which follows the base provider order. Disabled providers stay hidden, and enabled providers missing from this list are appended.
- `[web.layout]` — shared web display preferences, saved by the settings menu.
  - `mode` — `"adaptive"` fits the browser width and `"custom"` uses the dimensions below. Defaults to `"adaptive"`.
  - `rows`, `columns` — custom grid dimensions, each from `1` to `8`, defaulting to `2`. The settings menu requires enough cells for all enabled providers.

You only need to specify the keys you want to override; anything left out falls back to the default shown above.
