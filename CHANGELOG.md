# Changelog

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
