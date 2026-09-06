# Refresh the README screenshots

Run these commands from the repository root.

Both screenshots use demo data with remaining quota and reset timers (hours/minutes for sessions; days/hours/minutes for other windows).
For the TUI, use the [terminal demo config](screenshot_tui.toml) in a 140-column × 40-row terminal and capture it to `docs/screenshot.png`:

```bash
aitop --mock --config docs/screenshot_tui.toml
```

For Web, use the [web demo config](screenshot_web.toml):

```bash
aitop --headless --mock --config docs/screenshot_web.toml
```

Open `http://localhost:8788` with a 1440-pixel-wide browser window and dark mode,
then capture the dashboard to `docs/screenshot_web.png`.
