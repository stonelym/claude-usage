# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows taskbar/tray widget that displays Claude plan-usage (`session` and `weekly`
utilization %) by polling the same unofficial endpoint Claude Code's `/usage` command
uses. Single-file Python app: `claude_usage_tray.py`, with stdlib `unittest` tests in
`test_claude_usage.py`. Distributed as a PyInstaller `--onefile` exe and self-updates from
GitHub Releases (repo: `stonelym/claude-usage`). The exe and `ClaudeUsageSetup.exe` installer
are built by **GitHub Actions** (`.github/workflows/release.yml`), not locally.

## Commands

```powershell
# Run in script mode (no console window)
pythonw claude_usage_tray.py

# Run with a visible console + taskbar-embed diagnostics
python claude_usage_tray.py --debug

# Force taskbar text mode on and persist it to config
python claude_usage_tray.py --taskbar

# Install deps manually for local dev (tkinter ships with Python)
pip install pystray pillow requests comtypes

# Run the unit tests (stdlib unittest, no display/network needed)
python -m unittest -v test_claude_usage

# Cut a release: bump VERSION, commit, then tag + push (CI builds & publishes)
powershell -ExecutionPolicy Bypass -File .\release.ps1
```

**Shipping an update:** bump `VERSION` in `claude_usage_tray.py`, commit, then run
`release.ps1` (tags `vX.Y.Z` and pushes). GitHub Actions builds the exe + installer, asserts
the tag matches `VERSION`, and publishes the release with three assets: `ClaudeUsage.exe`
(self-update target), `ClaudeUsage.exe.sha256`, and `ClaudeUsageSetup.exe` (fresh installs).
Installed copies poll `releases/latest` (~daily), notify, and self-swap on user confirm.

**Installing on a new machine:** download `ClaudeUsageSetup.exe` from the latest release. The
Inno Setup installer (`installer/ClaudeUsage.iss`) is **per-user** — installs to
`%LOCALAPPDATA%\Programs\ClaudeUsage` with no admin prompt, which is the same directory the
app self-updates in, so updates stay unprivileged. It optionally adds the HKCU `Run` startup
entry (the same value the tray toggle uses) and a Start Menu shortcut.

Tests live in `test_claude_usage.py` (stdlib `unittest`, no extra deps). They cover the
display-free logic: usage parsing, 429/Retry-After classification, poll-backoff math, the
single-instance guard, and badge-position math. The pure functions are deliberately
importable without a display — but the module does a top-level `import requests`, so
`requests` must be installed to import it at all.

## Architecture

The app has **two display modes** that run simultaneously when possible:

1. **Taskbar text badge** (`TaskbarBadge`, default on, Windows-only) — a borderless,
   always-on-top tkinter overlay floating over the taskbar just left of the tray. It is
   **NOT** parented into `Shell_TrayWnd` (see the warning below). Pure Win32 via `ctypes`.
   Falls back silently to mode 2 if it throws.
2. **Tray icon** (`pystray`, always on) — colored square showing session %, owns the
   right-click menu and toast notifications.

`UsageTray` is the controller: it owns the poll loop, shared `usage`/`stale` state, the
pystray icon, and threshold notifications. `TaskbarBadge` reads `UsageTray`'s state each
tick but doesn't own it.

### Threading model (don't rearrange casually)

- A daemon thread runs `poll_loop` every `POLL_SECONDS` (300s). A second daemon thread
  (`update_loop`, frozen builds only) checks GitHub Releases ~daily.
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

### Self-update (GitHub Releases)

- `VERSION` is the single source of truth; `release.ps1` and the CI workflow parse it to tag
  releases (and CI asserts the tag matches it), and it's embedded in the GitHub User-Agent.
  `USER_AGENT` (the `claude-code/...` string) is separate and
  must stay as-is — the usage endpoint depends on it; GitHub uses `GITHUB_UA`.
- Flow: `update_loop` → `fetch_latest_release()` (reads `releases/latest`, public repo, no
  auth) → `select_release_assets` → `is_newer_version` → arm `self._pending`, toast, and a
  dynamic tray item "Update to vX.Y.Z & restart". On click, `apply_update()` downloads the
  exe + its `.sha256` sidecar, **verifies SHA-256 before touching anything**, then swaps.
- The swap is the only delicate part (a running `--onefile` exe can't be overwritten, only
  renamed): rename `ClaudeUsage.exe`→`.old.exe`, `os.replace` the verified `.new.exe`→
  `ClaudeUsage.exe` (with rollback if that fails), relaunch with `--updated`, then quit to
  release the mutex. `--updated` makes the relaunch use `acquire_single_instance_blocking`
  (waits out the outgoing instance) instead of exiting on contention. `main` best-effort
  deletes leftover `.old`/`.new` on every startup.
- The pure layer (`parse_version`, `is_newer_version`, `select_release_assets`,
  `parse_sha256_sidecar`, `verify_sha256`, `should_check_for_update`) is unit-tested;
  `fetch_latest_release`/`download_to`/`apply_update` are the impure shell.

### Windows-specific plumbing

- `_signed32` and the `& 0xFFFFFFFF` masking exist because `GetWindowLongW` returns signed
  LONGs but Python bitwise ops on negatives overflow ctypes' 32-bit conversion. Don't
  remove the masking when touching window styles.
- `TaskbarBadge._declare_prototypes` sets explicit `argtypes`/`restype` for every Win32
  call — required so HWNDs aren't truncated on 64-bit Python. New Win32 calls need an entry
  here.
- **NEVER reparent the badge into `Shell_TrayWnd`** (no `SetParent`/`WS_CHILD`). A previous
  version did, which made our window a child of explorer's taskbar; explorer's UI thread
  then sent it synchronous messages and **hung waiting on our tk loop** whenever the loop
  stalled — Windows logged `AppHangXProcB1` (partner `ClaudeUsage.exe`) and restarted
  explorer. The badge is now a standalone `WS_EX_TOPMOST | TOOLWINDOW | NOACTIVATE | LAYERED`
  overlay positioned in **screen coords** via non-blocking reads (`GetWindowRect`/
  `FindWindowExW`) + `MoveWindow` on our own window + a periodic `SetWindowPos(HWND_TOPMOST)`
  re-assert. `reposition` skips `MoveWindow` when geometry is unchanged (`should_move`).
  Explorer never waits on an unowned window, so it can't hang us or be hung by us.
- **Topmost is re-asserted on a fast ~120ms timer** (`_topmost_tick` → `_assert_topmost`),
  separate from the 750ms `tick`. Without it, clicking a taskbar item raises the (topmost)
  taskbar above the overlay and it stays buried until the next ~3s `reposition` — the
  "blinks away on click" bug. A `SetWinEventHook` was rejected: `WINEVENT_OUTOFCONTEXT`
  callbacks aren't reliably dispatched by tk's mainloop and would need a separate pump
  thread. The re-assert is a Z-only, non-activating `SetWindowPos` (no repaint, doesn't eat
  clicks). A **fullscreen guard** (`_foreground_is_fullscreen` via `GetForegroundWindow` +
  `MonitorFromWindow`/`GetMonitorInfoW` + the pure `is_fullscreen`) withdraws the overlay
  while a fullscreen app is foreground, so the aggressive topmost doesn't cover videos/games.
- **Badge positioning is collision-aware (Win11).** On Windows 11 the Widgets/weather button
  lives in a XAML composition island with **no HWND**, so Win32 can't see it.
  `detect_taskbar_obstacles` uses **UI Automation** (`comtypes`) to find right-docked
  ToggleButtons; their absolute left edges feed the pure `compute_badge_x` so the overlay
  clears them. This UIA walk is the one heavy cross-process call, so it runs **once on a
  background daemon thread** (`_probe_obstacles`), never on the tk loop — a slow walk can't
  stall the overlay (and, since we're unparented, can't hang explorer). Result is cached for
  `reposition` to read; UIA failure degrades to a tray-only anchor. `comtypes` generates a
  typelib wrapper at first use; when frozen, `_uia_setup` redirects `gen_dir` to
  `%APPDATA%\ClaudeUsage\comtypes_gen` (site-packages is read-only in a `--onefile` exe).
- Startup-at-login is an HKCU `...\Run` entry (`set_startup`/`is_startup_enabled`), which
  is what surfaces the app in Task Manager > Startup apps.

### Config

`%APPDATA%\ClaudeUsage\config.json` holds `{"taskbar": bool, "last_update_check": float}`.
Taskbar mode defaults ON. The "Show on taskbar" toggle persists but only applies on next
launch. `last_update_check` (epoch seconds) throttles the daily release check.

## Conventions

- Known usage windows are whitelisted in `KNOWN_WINDOWS`; the API carries transient
  internal fields that are intentionally ignored. `parse_usage` skips any window that's
  missing or has a null `utilization`.
- Color thresholds: `>80%` high (red), `>=50%` warn (amber), else ok (green); gray when
  stale. Both icon (RGB tuples) and taskbar text (hex) have parallel pickers.
- Lazy imports inside methods (`PIL`, `tkinter`, `pystray`, `winreg`, `ctypes`) keep the
  pure-function layer importable without a display or Windows.
