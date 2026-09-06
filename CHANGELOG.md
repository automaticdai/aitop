# Changelog

## v1.2 — 2026-09-06

- Add Kimi pay-as-you-go balance monitoring for Moonshot Global and China accounts, marking a balance at or below zero as insufficient because the vendor then rejects every call.
- Add MiniMax Token Plan monitoring with rolling 5-hour session and weekly request quotas. The endpoint is not vendor-documented; a key without a Token Plan reports that rather than failing.
- Add region selectors for Kimi and MiniMax alongside GLM's, and accept `MOONSHOT_API_KEY` for Kimi. Both providers are opt-in.
- Document starting WSL at Windows sign-in, so the service runs without opening a terminal.

Upgrade the package, restart aitop, and reload the browser. Enable Kimi or MiniMax under **Menu → Providers**, pick the region matching the account, and enter its API key.

## v1.1 — 2026-09-06

- Add GLM Coding Plan quota monitoring for Z.ai and BigModel accounts.
- Report GLM account errors clearly and replace outdated dashboard errors when no valid usage was fetched.
- Add compact, automatically saved API key and region controls, disabled when their provider is off. GLM is opt-in.
- Add OpenRouter credit and spend monitoring from an API key. A key with a spending limit reports what is left on it; an uncapped key falls back to the account's credits, and drops the balance rather than failing if that endpoint is refused.
- Show uncapped spend windows (today, this week, this month) as plain amounts in both interfaces, since they have no vendor-set cap to draw a bar from. OpenRouter is opt-in.

Upgrade the package, restart aitop, and reload the browser. Enable GLM or OpenRouter under **Menu → Providers** and enter an API key for each you want polled.

## v1.0.1 — 2026-09-06

- Add GitHub Copilot account quotas using an existing GitHub CLI login or environment token, with monthly pools, unlimited allowances, and reset countdowns in both interfaces.
- Add a Copilot provider switch, logo, mock data, and setup documentation. Copilot is off by default to preserve existing layouts.
- Split the Menu into Providers and Layouts tabs, keeping layout and ordering together.
- Nest compact Claude & GPT-OSS and API key controls under Antigravity and DeepSeek; disable each sub-option when its provider is off.
- Save Menu changes automatically, queue edits made during a save, and finish pending saves before closing. Show errors with a Retry button when saving fails.

Upgrade the package, restart aitop, and reload the browser. Enable Copilot under **Menu → Providers** after authenticating GitHub on the machine running aitop.

## v1.0 — 2026-09-06

First stable release. Python package version: **1.0.0**.

- Monitor Claude Code, Codex, Antigravity, and DeepSeek in the terminal or browser.
- Show remaining quota with matching labels and severity colours in both interfaces.
- Display session reset timers in hours and minutes, and longer windows in days, hours, and minutes.
- Keep the last good values when a provider fails, with a stale indicator.
- Configure adaptive or fixed terminal grids and responsive web layouts.
- Save web layouts, provider order, and Antigravity group visibility to `config.toml`.
- Turn providers on/off in the Menu, updating polling and the TUI immediately.
- Configure a DeepSeek API key through a masked Menu field; saved keys use owner-only file permissions and are never returned by the settings API.
- Hide the redundant Gemini title when Antigravity's Claude group is hidden.
- Reorder web cards by dragging, touch, arrow keys, or the Menu.
- Run the web dashboard without a terminal using `--headless`, with a systemd service example for WSL2 startup.
- Include provider logos, updated branding, demo screenshots, and configuration and service guides.

### Install

Requires Python 3.11 or later. Install the attached wheel with:

```bash
python -m pip install aitop-1.0.0-py3-none-any.whl
aitop --mock
```

Live mode requires authenticated provider CLIs on `PATH`, or `DEEPSEEK_API_KEY` for DeepSeek. See the README for setup.

### Existing installations

Existing config files remain supported. Remaining quota and reset countdowns are enabled by default; set `show_remaining = false` or `reset_countdown = false` for the previous display. Existing `[web]` display overrides take precedence for the browser.

Restart aitop after upgrading. Web service users should restart `aitop.service` and reload the browser.
