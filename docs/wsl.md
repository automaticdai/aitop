# Run aitop when WSL starts

`--headless` runs the same provider polling and web dashboard without a terminal.
It implies `--web`; combining it with `--no-web` is an error. SIGTERM stops polling
and cleans up active provider CLI processes before the server exits.

WSL needs [systemd enabled](https://learn.microsoft.com/en-us/windows/wsl/systemd)
(`systemd=true` under `[boot]` in `/etc/wsl.conf`). If you change that setting,
restart WSL from PowerShell with `wsl --shutdown`, then reopen the distribution.

Install the example [user service](aitop.service):

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
aitop will start when this WSL distribution starts. Launching the distribution
itself at Windows sign-in takes one more step, below. Open
`http://localhost:8787` with the default web port.

## Start WSL at Windows sign-in

Lingering starts aitop when the distribution starts, but nothing starts the
distribution itself until you open a WSL terminal. To close that gap, have
Windows launch the distribution at sign-in. Save this as
`%LOCALAPPDATA%\aitop\start-wsl.vbs`, substituting your distribution name
(`wsl.exe -l -q` lists them). The wrapper exists only to keep the console
window hidden; window style `0` runs it invisibly.

```vbscript
CreateObject("WScript.Shell").Run "wsl.exe -d Ubuntu-24.04 --exec /bin/true", 0, False
```

Register it as a logon task in PowerShell (no administrator rights needed):

```powershell
$vbs = "$env:LOCALAPPDATA\aitop\start-wsl.vbs"
Register-ScheduledTask -TaskName 'Start WSL for aitop' `
  -Action (New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`"") `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
  -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
      -DontStopIfGoingOnBatteries -StartWhenAvailable `
      -ExecutionTimeLimit ([TimeSpan]::Zero))
```

`/bin/true` exits immediately; the distribution keeps running because systemd
is PID 1 and the lingering user manager holds aitop open. The dashboard is
then reachable at `http://localhost:8787` after sign-in without opening a
terminal. This triggers at sign-in, not at boot: Windows must reach your
desktop first. Remove it with
`Unregister-ScheduledTask -TaskName 'Start WSL for aitop'`.

```bash
systemctl --user status aitop.service           # status
journalctl --user -u aitop.service -n 50         # recent logs
systemctl --user restart aitop.service         # apply config/code changes
systemctl --user disable --now aitop.service    # stop and disable startup
```

While the service is running, use `aitop --no-web` if you also want the TUI,
so it does not try to bind the service's port. The TUI starts its own polling.
