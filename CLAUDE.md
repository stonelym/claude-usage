# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows taskbar/tray widget that displays Claude plan-usage (`session` and `weekly`
utilization %) by polling the same unofficial endpoint Claude Code's `/usage` command
uses. Single-file Python app: `claude_usage_tray.py`. No git, no test suite, no
dependency manifest.

## Commands

```powershell
# Run in script mode (no console window)
pythonw claude_usage_tray.py

# Run with a visible console + taskbar-embed diagnostics
python claude_usage_tray.py --debug

# Force taskbar text mode on and persist it to config
python claude_usage_tray.py --taskbar

# Install deps manually
pip install pystray pillow requests comtypes   # tkinter ships with Python

# Build a standalone exe and install to %LOCALAPPDATA%\Programs\ClaudeUsage, then launch
powershell -ExecutionPolicy Bypass -File .\build.ps1

# Run the unit tests (stdlib unittest, no display/network needed)
python -m unittest -v test_claude_usage
```

Tests live in `test_claude_usage.py` (stdlib `unittest`, no extra deps). They cover the
display-free logic: usage parsing, 429/Retry-After classification, poll-backoff math, the
single-instance guard, and badge-position math. The pure functions are deliberately
importable without a display — but the module does a top-level `import requests`, so
`requests` must be installed to import it at all.

## Architecture

The app has **two display modes** that run simultaneously when possible:

1. **Taskbar text badge** (`TaskbarBadge`, default on, Windows-only) — a borderless
   tkinter window reparented into `Shell_TrayWnd` so text paints directly on the taskbar
   left of the tray. Pure Win32 via `ctypes`. Falls back silently to mode 2 if the embed
   throws.
2. **Tray icon** (`pystray`, always on) — colored square showing session %, owns the
   right-click menu and toast notifications.

`UsageTray` is the controller: it owns the poll loop, shared `usage`/`stale` state, the
pystray icon, and threshold notifications. `TaskbarBadge` reads `UsageTray`'s state each
tick but doesn't own it.

### Threading model (don't rearrange casually)

- A daemon thread runs `poll_loop` every `POLL_SECONDS` (60s).
- **tkinter must run on the main thread**; pystray can run in a daemon thread. So when
  the badge is active, `icon.run()` is pushed to a daemon thread and `badge.run()` blocks
  the main thread. When the badge isn't active, `icon.run()` blocks the main thread
  directly. See `UsageTray.run`.
- `self.stop` (an `Event`) is the shutdown signal; the badge's `tick` polls it and
  destroys its own window when set.

### Data source & auth (the load-bearing detail)

- Reads the OAuth token from Claude Code's own `~/.claude/.credentials.json`
  (`claudeAiOauth.accessToken`).
- GETs `https://api.anthropic.com/api/oauth/usage` with the `anthropic-beta:
  oauth-2025-04-20` header and a `claude-code/...` User-Agent.
- On a 401 it does **one** refresh via the Claude Code OAuth flow and **writes the new
  tokens back** to `.credentials.json` (atomic temp-file + `os.replace`) so Claude Code
  stays in sync. This file is shared state with Claude Code — treat writes carefully.
- **This endpoint rate-limits aggressively and account-wide.** It's meant to be hit
  on-demand (Claude Code's `/usage`), not polled. `POLL_SECONDS` is therefore **300**, not
  60 — do not lower it. `fetch_usage()` returns a `FetchResult` (`ok` / `auth` /
  `rate_limited` / `error`), not a bare dict/None. On a 429, `poll_loop` honors the
  server's `Retry-After` via `compute_next_wait` so the rolling limit can drain; polling
  straight through a 429 keeps the account permanently limited. `poll_once` also refuses to
  hit the endpoint while still inside a cooldown (so a manual "Refresh" can't re-arm it).
  The rate-limited state is surfaced in the tooltip ("Rate-limited, retry HH:MM"), not
  muted to `—`.
- **Single instance only** (`acquire_single_instance`, a named mutex). Two instances both
  polling is what trips the rate limit; a second launch exits immediately.
- All of the above (`FetchResult`, `classify_usage_response`, `parse_retry_after`,
  `compute_next_wait`) is pure and unit-tested; `fetch_usage` is just the network shell
  around it.

### Windows-specific plumbing

- `_signed32` and the `& 0xFFFFFFFF` masking exist because `GetWindowLongW` returns signed
  LONGs but Python bitwise ops on negatives overflow ctypes' 32-bit conversion. Don't
  remove the masking when touching window styles.
- `TaskbarBadge._declare_prototypes` sets explicit `argtypes`/`restype` for every Win32
  call — required so HWNDs aren't truncated on 64-bit Python. New Win32 calls need an entry
  here.
- The badge re-embeds after explorer restarts and repositions as the tray resizes (checked
  every ~3s in `tick`). This embed is an unsupported, deprecated-DeskBand-style technique;
  a Windows update could break it, which is why tray-icon mode always stays on.
- **Badge positioning is collision-aware (Win11).** On Windows 11 the visible taskbar is
  painted by a single full-width `Windows.UI.Composition.DesktopWindowContentBridge`; the
  Widgets/weather button lives inside that XAML island with **no HWND**, so Win32/MSAA
  can't see it. `detect_taskbar_obstacles` uses **UI Automation** (`comtypes`) to find
  right-docked ToggleButtons and feeds their left edges, plus the tray's, into the pure
  `compute_badge_x` so the badge anchors clear of all of them. The UIA object is cached
  (`_uia`), the scan is throttled to ~every 10th reposition, and any UIA failure degrades
  gracefully to a tray-only anchor. `comtypes` generates a typelib wrapper at first use;
  when frozen, `_uia_setup` redirects `gen_dir` to `%APPDATA%\ClaudeUsage\comtypes_gen`
  because site-packages is read-only in a `--onefile` exe.
- Startup-at-login is an HKCU `...\Run` entry (`set_startup`/`is_startup_enabled`), which
  is what surfaces the app in Task Manager > Startup apps.

### Config

`%APPDATA%\ClaudeUsage\config.json` holds `{"taskbar": bool}`. Taskbar mode defaults ON.
The "Show on taskbar" toggle persists but only applies on next launch.

## Conventions

- Known usage windows are whitelisted in `KNOWN_WINDOWS`; the API carries transient
  internal fields that are intentionally ignored. `parse_usage` skips any window that's
  missing or has a null `utilization`.
- Color thresholds: `>80%` high (red), `>=50%` warn (amber), else ok (green); gray when
  stale. Both icon (RGB tuples) and taskbar text (hex) have parallel pickers.
- Lazy imports inside methods (`PIL`, `tkinter`, `pystray`, `winreg`, `ctypes`) keep the
  pure-function layer importable without a display or Windows.
