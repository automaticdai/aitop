# aitop

Monitor **Claude Code, Codex, GitHub Copilot, Antigravity (agy), DeepSeek, GLM, OpenRouter, Kimi, and MiniMax** from your terminal or browser. See remaining quota, reset countdowns, and account balances in one place.

See the [v1.2.1 release notes](CHANGELOG.md) for highlights and upgrade instructions.

## Install

Requires **Python 3.11+**. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
aitop --mock
```

Demo mode needs no credentials or provider CLIs. For live data, complete the [provider setup](#provider-setup) below.

## TUI

```bash
aitop                    # live dashboard
aitop --mock             # demo data
aitop --no-web           # terminal only, even if web is enabled in config
```

![aitop TUI showing remaining quota and reset countdowns](docs/screenshot.png)

*Terminal dashboard with demo data.*

Use **q** to quit, **r** to refresh, and **w** to view the web server status. The grid supports fixed positions or automatic resizing.

Both interfaces show **quota left**: 25% used becomes **75% left**. Bars turn amber at 30% remaining and red below 10%. Session timers show `2h 30m`; other windows show `4d 1h 24m`. Failed fetches retain the last good values, marked **stale**; missing values show **no data**.

## Web

```bash
aitop --web              # web dashboard alongside the TUI
aitop --headless          # web dashboard only
aitop --headless --mock   # web demo
```

Open **http://localhost:8787**. The web server is off by default.

![aitop Web showing remaining quota and reset countdowns](docs/screenshot_web.png)

*Web dashboard with demo data.*

- **Menu:** turn providers on/off, set provider API keys and regions, choose a grid, and reorder cards. Toggle Antigravity's Claude & GPT-OSS group; its remaining Gemini group needs no extra title. Author, version, and GitHub are also listed here.
- **Drag cards** to reorder and save immediately. Grips support touch and arrow keys.
- **Automatic saving** keeps Menu changes in the active `config.toml`, shared across browsers and restarts. Changes save as you make them; closing the Menu finishes pending saves. If saving fails, the Menu shows an error and a Retry button.

The dashboard refreshes at the configured interval; `/api/snapshots` provides the data as JSON. Hover over a reset timer to see its original note. Reload the page after restarting the service before saving settings.

The server binds to `127.0.0.1` by default, or `0.0.0.0` under WSL2 for Windows localhost forwarding. It has **no login authentication**; keep it on a trusted network.

To run automatically when WSL starts, follow the [service setup guide](docs/wsl.md). When the service is running, use `aitop --no-web` for a separate TUI to avoid a port conflict.

## Provider setup

| Provider | Required setup | Data source |
|---|---|---|
| Claude Code | `claude` on `PATH`, already logged in | CLI `/usage` |
| Codex | `codex` on `PATH`, already logged in | CLI `/status` (weekly limit only) |
| Antigravity | `agy` CLI on `PATH`, already logged in | CLI `/usage`, including both quota groups |
| DeepSeek | API key saved through **Menu**, or `DEEPSEEK_API_KEY` | HTTPS balance API |
| GLM | API key in **Menu**, or `GLM_API_KEY`; choose Z.ai or BigModel | HTTPS Coding Plan quota API |
| GitHub Copilot | `gh auth login`, or `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` | HTTPS account quotas |
| OpenRouter | API key in **Menu**, or `OPENROUTER_API_KEY` | HTTPS key and credits API |
| Kimi | API key in **Menu**, or `KIMI_API_KEY` / `MOONSHOT_API_KEY`; choose a region | HTTPS balance API |
| MiniMax | API key in **Menu**, or `MINIMAX_API_KEY`; choose a region | HTTPS Token Plan quota API |

Run each CLI once to log in before starting aitop. The `agy` CLI is required for Antigravity; the desktop app alone is insufficient. DeepSeek, OpenRouter, and Kimi show an account balance rather than a quota bar.

Enable **GitHub Copilot** under **Menu → Providers** after signing in to GitHub on the machine running aitop. It is off by default to preserve existing layouts. For the TUI, set `[providers.copilot] enabled = true` and use an adaptive layout or leave a grid cell for it. Copilot shows monthly premium-request or AI-credit, chat, and completion pools when available; unlimited pools are labelled explicitly. The adapter uses the internal [entitlement API used by VS Code](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/services/chat/common/chatEntitlementService.ts), which may change. It reads quotas without making model requests or saving GitHub credentials in aitop's config. Environment tokens take precedence over the GitHub CLI login, in the order listed above.

Enable **GLM** under **Menu → Providers**, select your account's region, and enter its API key. Changes save automatically. GLM is off by default. GLM uses the [official usage plugin's quota endpoint](https://github.com/zai-org/zai-coding-plugins/blob/main/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs) to show Coding Plan session, weekly, and MCP tool quotas when returned. The official usage plugin supports personal Coding Plans. A pay-as-you-go key alone does not provide these plan quotas. This integration only reads usage; it does not make model requests.

Enable **OpenRouter** under **Menu → Providers** and enter an API key; it is off by default. The card shows credits left plus spend for today, this week, and this month. Spend windows have no vendor-set cap, so they appear as plain amounts rather than bars, and a free-tier key is labelled as such under the logo.

Where the balance comes from depends on the key. A key with a spending limit reports what is left on that limit, which is what decides whether its next call succeeds. A key with no limit falls back to the account's own credits (purchased credits less lifetime usage). OpenRouter documents that second endpoint as needing a [provisioning key](https://openrouter.ai/docs/features/provisioning-api-keys), but an ordinary inference key reads it in practice; if an account does refuse it, the card drops the balance and shows spend alone rather than failing. A new account with no purchased credits correctly reads `0.00 USD` — free models still work at that balance. This integration only reads usage; it does not make model requests.

Enable **Kimi** under **Menu → Providers**, select the region matching your account, and enter its API key. Kimi is off by default. The card shows the pay-as-you-go balance from Moonshot's [balance endpoint](https://platform.kimi.ai/docs/api/balance). At or below zero the vendor rejects every call, so the card marks the balance as insufficient. Keys are issued per platform and are not interchangeable: a global key sent to the China host returns 401, which is why a failure names the region as well as the key. A **Kimi Code** subscription is a different product with its own request quotas and no published API; this adapter does not read it.

Enable **MiniMax** under **Menu → Providers**, select the region, and enter a Token Plan key. MiniMax is off by default. The card shows the Token Plan's rolling 5-hour session window and its weekly window as request quotas. MiniMax does not publish a quota endpoint, so this adapter reads the same `token_plan/remains` route the community tooling uses; it is **not vendor-documented and may change or be withdrawn**. The route and its `base_resp` envelope have been confirmed against a live account, but the shape of a populated quota response has not: an account without a subscription answers `2062`, which the card reports as no active Token Plan rather than as an error. A key aimed at the other region answers `2049`, reported as a key or region problem. This integration only reads usage; it does not make model requests.

GLM also accepts `ZAI_API_KEY` for the Global region or `ZHIPU_API_KEY` for China, after `GLM_API_KEY`. Saved keys take precedence. When running aitop as a service, set environment keys in that service's environment, or use the Menu. API key fields stay blank after saving and are disabled when their provider is off.

CLI data is parsed from terminal output. Expired logins or changes to vendor screens may produce **no data**. Reopen the relevant CLI and check its login and usage screen; aitop does not log in or refresh credentials for you.

## Configuration

Use `aitop --config path/to/config.toml` to select a file. Otherwise aitop reads `./config.toml`, then `~/.config/aitop/config.toml`, creating the latter on first run if neither exists.

```toml
refresh_interval_s = 30
show_remaining = true
reset_countdown = true

[layout]
adaptive = true

[web]
enabled = false
port = 8787
```

Set `show_remaining = false` for usage bars, or `reset_countdown = false` for native reset notes. Web-specific overrides, provider positions, timeouts, and grid settings are covered in the [configuration reference](docs/configuration.md).

Provider switches control both display and polling. Saved provider keys stay in the local config with owner-only permissions and are never returned to the browser.

Restart aitop after editing the file. Changes saved through the Web Menu apply immediately.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

See [screenshot instructions](docs/screenshots.md) to refresh the demo images. The TUI uses [Textual](https://github.com/Textualize/textual); provider logos come from [Lobe Icons](https://github.com/lobehub/lobe-icons), with the [MIT attribution](src/aitop/static/LICENSE.txt) included.
