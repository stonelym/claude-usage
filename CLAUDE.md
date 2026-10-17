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

# Install deps for local dev (tkinter ships with Python). requirements-build.txt pins every
# package CI ships, transitive ones too; regenerate it with pip freeze in a clean 3.12 venv.
pip install -r requirements-build.txt

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

Tests live in `test_claude_usage.py` (stdlib `unittest` + `unittest.mock`, no extra deps). They
cover the display-free logic: usage parsing, 429/Retry-After classification, poll-backoff math,
the `poll_once`/`poll_loop` state machine (cooldown skip, click gap, in-flight lock; with
`fetch_usage` and the clock faked), threshold toasts, `fetch_usage`'s token handling (reads,
never renews), the single-instance guard, badge-position math
(`compute_badge_x`/`is_fullscreen`/`select_taskbar`), the self-update helpers
(`parse_version`/`is_newer_version`/`select_release_assets`/`parse_sha256_sidecar`/
`verify_sha256`), the `apply_update` swap/rollback in a temp dir, and `_relaunch`'s
PyInstaller environment reset.
The pure functions are deliberately importable
without a display — but the module does a top-level `import requests`, so `requests` must be
installed to import it at all.

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

- Reads the OAuth token from Claude Code's own `.credentials.json` (`claudeAiOauth.accessToken`)
  in `~/.claude`, or in `CLAUDE_CONFIG_DIR` when that is set.
- GETs `https://api.anthropic.com/api/oauth/usage` with the `anthropic-beta:
  oauth-2025-04-20` header and a `claude-code/...` User-Agent.
- **The app never renews the token and never writes `.credentials.json`.** Do not add a
  refresh. Refresh tokens are single-use. Claude Code refreshes under its own cross-process
  lock (`~/.claude/.oauth_refresh.lock`) and, when a refresh token comes back rejected,
  **wipes the saved login**. So a second refresher that wins a race can sign the user out
  of Claude Code. (The old refresh also posted to `console.anthropic.com/v1/oauth/token`,
  which now 404s; Claude Code uses `platform.claude.com`.) A missing token or one past
  `expiresAt` is `signed_out`: no request is made, it doesn't count against `MIN_FETCH_GAP`,
  and `poll_loop` re-reads the file every `SIGNIN_RECHECK` (30s) so the badge recovers soon
  after Claude Code signs back in. A 401 is `auth` and keeps the 300s pace. Either way the
  tooltip says "Signed out: open Claude Code to sign in". A missing credentials file isn't
  fatal: the app starts gray.
- **This endpoint rate-limits aggressively and account-wide.** It's meant to be hit
  on-demand (Claude Code's `/usage`), not polled. `POLL_SECONDS` is therefore **300**, not
  60 — do not lower it. `fetch_usage()` returns a `FetchResult` (`ok` / `signed_out` /
  `auth` / `rate_limited` / `error`), not a bare dict/None. On a 429, `poll_once` honors the
  server's `Retry-After` and, via `compute_next_wait`, **backs off exponentially on
  consecutive 429s** (`rate_limit_streak`: 300s → 600 → 1200 … capped at 1h) so the rolling
  limit can actually drain; retrying flat every 300s keeps re-tripping an account-wide
  window that needs longer than that to clear (the "constantly stale" failure mode).
  `poll_loop` sleeps until exactly `rate_limited_until` (a manual refresh can set it
  mid-interval). `poll_once` also refuses to hit the endpoint while still inside a cooldown
  (so a manual "Refresh" can't re-arm it), while another fetch is in flight (`_poll_lock`),
  or within `MIN_FETCH_GAP` (60s) of the last fetch, so repeated clicks on the badge or
  "Refresh" can't hammer it; `--debug` emits `[poll]` lines (HTTP status, kind, streak,
  backoff) so a stuck-stale state is diagnosable on the live machine.
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
  dynamic tray item "Update to vX.Y.Z and restart". On click, `start_update` (one at a time,
  `_update_lock`, so a double-click can't race two downloads) runs `apply_update()`, which
  toasts "Downloading…", downloads the exe + its `.sha256` sidecar, **verifies SHA-256
  before touching anything**, then swaps. "Check for updates now" passes `manual=True`, so it
  always answers with a toast (update available / up to date / couldn't reach GitHub).
- The swap is the only delicate part (a running `--onefile` exe can't be overwritten, only
  renamed): rename `ClaudeUsage.exe`→`.old.exe`, `os.replace` the verified `.new.exe`→
  `ClaudeUsage.exe` (with rollback if that fails), relaunch with `--updated`, then quit to
  release the mutex. If the relaunch itself fails (e.g. antivirus blocks the new unsigned
  exe), the old exe is moved back and the app keeps running. `--updated` makes the relaunch
  use `acquire_single_instance_blocking` (waits out the outgoing instance) instead of exiting
  on contention.
- **The relaunch must pass `PYINSTALLER_RESET_ENVIRONMENT=1`.** Without it the new
  `--onefile` exe inherits the old one's `_PYI_*` variables and runs out of the old
  process's `_MEI` unpack dir (old DLLs). The old launcher then can't delete that dir and
  hangs on a modal "Failed to remove temporary directory" dialog. Observed live on the
  v1.0.6→v1.0.7 update. The fix lives on the *outgoing* side, so updating *from* a build
  without it still shows the dialog once.
- `main` deletes leftover `.old`/`.new` after it owns the mutex (so a duplicate launch can't
  delete a running update's download), on a 5s timer (the outgoing launcher holds
  `.old.exe` briefly after releasing the mutex).
- The pure layer (`parse_version`, `is_newer_version`, `select_release_assets`,
  `parse_sha256_sidecar`, `verify_sha256`, `should_check_for_update`) is unit-tested, and so
  is `apply_update` with the network faked. A failed release check isn't stamped into
  `last_update_check`, so `update_loop`'s hourly wake retries it instead of waiting a day.

### Windows-specific plumbing

- `_signed32` and the `& 0xFFFFFFFF` masking exist because `GetWindowLongW` returns signed
  LONGs but Python bitwise ops on negatives overflow ctypes' 32-bit conversion. Don't
  remove the masking when touching window styles.
- `_user32()` (cached) sets explicit `argtypes`/`restype` for every user32 call, once, on a
  private `WinDLL` — required so HWNDs aren't truncated on 64-bit Python. New Win32 calls
  need an entry there. Never declare prototypes or ctypes structs per call: Python 3.12 keeps
  every struct class forever (that leaked ~170 MB/day at the 3s `reposition` rate), and two
  struct types for one function made `GetMonitorInfoW` fail inside the fullscreen guard.
- **NEVER reparent the badge into `Shell_TrayWnd`** (no `SetParent`/`WS_CHILD`). A previous
  version did, which made our window a child of explorer's taskbar; explorer's UI thread
  then sent it synchronous messages and **hung waiting on our tk loop** whenever the loop
  stalled — Windows logged `AppHangXProcB1` (partner `ClaudeUsage.exe`) and restarted
  explorer. The badge is now a standalone `WS_EX_TOPMOST | TOOLWINDOW | NOACTIVATE | LAYERED`
  overlay positioned in **screen coords** via non-blocking reads (`GetWindowRect`/
  `FindWindowExW`) + `MoveWindow` on our own window + a periodic `SetWindowPos(HWND_TOPMOST)`
  re-assert. `reposition` skips `MoveWindow` when geometry is unchanged.
  Explorer never waits on an unowned window, so it can't hang us or be hung by us.
- **Topmost is re-asserted on a fast ~120ms timer** (`_topmost_tick` → `_assert_topmost`),
  separate from the 750ms `tick`. Without it, clicking a taskbar item raises the (topmost)
  taskbar above the overlay and it stays buried until the next ~3s `reposition` — the
  "blinks away on click" bug. A `SetWinEventHook` was rejected: `WINEVENT_OUTOFCONTEXT`
  callbacks aren't reliably dispatched by tk's mainloop and would need a separate pump
  thread. The re-assert is a Z-only, non-activating `SetWindowPos` (no repaint, doesn't eat
  clicks). A **fullscreen guard** (`_fullscreen_on_badge_monitor` via `GetForegroundWindow` +
  `MonitorFromWindow`/`GetMonitorInfoW` + the pure `is_fullscreen`) withdraws the overlay
  while a fullscreen app is foreground, so the aggressive topmost doesn't cover videos/games.
  It is **monitor-scoped**: it compares the foreground window's `HMONITOR` to the badge's
  (the docked taskbar's, `_dock_hwnd`) and only hides when they match — a fullscreen app on
  another display leaves the badge visible. The desktop (`Progman`/`WorkerW`, foreground
  after a desktop click or Win+D) covers its monitor too and is excluded in `is_fullscreen`.
- **The badge can dock to a chosen display (multi-monitor).** It floats over a taskbar, so it
  can live on any monitor that has one: the primary `Shell_TrayWnd` or a secondary monitor's
  `Shell_SecondaryTrayWnd` (present only with Windows' "Show my taskbar on all displays").
  `enumerate_taskbar_displays()` lists those taskbars (HWND + monitor `szDevice` + primary
  flag) and the pure `select_taskbar` picks the one matching `config["display"]`, falling
  back to primary if it's gone. `reposition` reads this each tick so the tray menu's "Show on
  display" submenu applies **live**. On a secondary taskbar (no `TrayNotifyWnd`, no Widgets
  button) the badge anchors via the right-edge fallback; the cached UIA probe (Widgets
  obstacles, app-button floor) is used **only** when docked to the primary (it's in
  primary-monitor coords).
  `compute_badge_x` clamps to the docked taskbar's left edge, not 0: screen x is negative on
  a monitor placed left of the primary.
- **Badge positioning is collision-aware (Win11).** On Windows 11 the Widgets/weather button
  and the app buttons live in XAML composition islands with **no HWND**, so Win32 can't see
  them. `detect_taskbar_obstacles` uses **UI Automation** (`comtypes`) to find right-docked
  ToggleButtons (left edges the badge must stay left of) and the right edge of the last
  `Taskbar.TaskListButtonAutomationPeer` (the badge's floor). The pure `compute_badge_x`
  returns `None` when the badge doesn't fit between them. The badge then hides (`_no_room`,
  honored by `_assert_topmost`) rather than cover app buttons; the tray icon still shows the
  session %. This happens on a crowded left-aligned taskbar on a small screen (e.g. a 1280px
  Remote Desktop session).
  The UIA walk (~20 ms) is the one heavy cross-process call, so it runs on **one background
  daemon thread** (`_probe_obstacles`), **every 5s** (`PROBE_SECONDS`): apps open and close,
  and the Widgets button changes width. Never on the tk loop: a slow walk can't stall the
  overlay (and, since we're unparented, can't hang explorer). Always the same thread,
  because the UIA COM objects belong to the thread that created them. The result is cached
  for `reposition` to read; UIA failure degrades to a tray-only anchor. `comtypes` generates a
  typelib wrapper at first use; when frozen, `_uia_setup` redirects `gen_dir` to
  `%APPDATA%\ClaudeUsage\comtypes_gen` (site-packages is read-only in a `--onefile` exe).
- Startup-at-login is an HKCU `...\Run` entry (`set_startup`/`is_startup_enabled`), which
  is what surfaces the app in Task Manager > Startup apps. Task Manager's on/off switch is a
  separate flag (`APPROVED_KEY`, first byte 2 = on, 3 = off): the tray checkmark reads both,
  and turning startup on deletes that flag so a Task Manager "off" can't stick.
- The installer closes a running copy with a plain `taskkill` first and `/f` only as a
  fallback: a forced kill also kills PyInstaller's parent process, which then never deletes
  its 42 MB `_MEI` unpack dir in `%TEMP%`.

### Config

`%APPDATA%\ClaudeUsage\config.json` holds
`{"taskbar": bool, "last_update_check": float, "display": str|null}`.
Taskbar mode defaults ON. The "Show on taskbar" toggle persists but only applies on next
launch. `last_update_check` (epoch seconds) throttles the daily release check. `display` is
the monitor device name (e.g. `\\.\DISPLAY2`) the badge docks to, set by the "Show on
display" menu; absent/`null` means follow the primary taskbar. Unlike the taskbar toggle,
the display choice applies live (`reposition` reads it each tick).

## Conventions

- Known usage windows are whitelisted in `KNOWN_WINDOWS`; the API carries transient
  internal fields that are intentionally ignored. `parse_usage` skips any window that's
  missing or has a null `utilization`.
- Color thresholds: `>80%` high (red), `>=50%` warn (amber), else ok (green); gray when
  stale. One picker, `pick_color`, serves the icon (`ICON_PALETTE`, RGB) and the taskbar
  text (`TEXT_PALETTE`, hex).
- Threshold toasts (80/95%) fire once per 5h window, keyed on `resets_at` truncated to the
  minute: the API adds per-request microseconds, so the raw value changes on every poll.
  Only the highest threshold crossed toasts (a first poll at 96% gives one toast, not two).
- Lazy imports inside methods (`PIL`, `tkinter`, `pystray`, `winreg`, `ctypes`) keep the
  pure-function layer importable without a display or Windows.
- Tray menu text goes straight into a Win32 menu, which eats a lone `&` as a shortcut-key
  marker: write "and" (or `&&`).
