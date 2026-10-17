"""
claude_usage_tray.py — Claude plan-usage widget for Windows: a tray icon plus a
text badge floating over the taskbar (an unowned overlay, never parented into
explorer). Commands, architecture and the details not to change: CLAUDE.md.
"""

import argparse
import functools
import hashlib
import json
import os
import re
import sys
import threading
import time
import webbrowser
from collections import namedtuple
from datetime import datetime

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
# /api/oauth/usage is an on-demand endpoint (Claude Code only hits it when you
# run /usage). The 5h/7d windows move slowly, so polling every 5 min is plenty
# and — crucially — avoids tripping the endpoint's rate limit. See fetch_usage.
VERSION = "1.0.9"  # single source of truth; release.ps1/CI tag releases from this
POLL_SECONDS = 300
MIN_FETCH_GAP = 60   # a click/Refresh never fetches sooner than this after the last fetch
SIGNIN_RECHECK = 30  # signed out: re-read the token file this often (no request is made)
APP_NAME = "ClaudeUsage"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Claude Code keeps its login here (CLAUDE_CONFIG_DIR moves the whole folder).
CRED_PATH = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR")
                         or os.path.join(os.path.expanduser("~"), ".claude"),
                         ".credentials.json")
USAGE_PAGE = "https://claude.ai/settings/usage"
USER_AGENT = "claude-code/2.0.31"  # the usage endpoint expects the Claude Code UA

# Self-update via GitHub Releases (public repo). The app reads releases/latest,
# compares VERSION, and offers a verified swap. See fetch_latest_release.
GITHUB_REPO = "stonelym/claude-usage"
RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_UA = f"ClaudeUsage/{VERSION}"   # GitHub requires a User-Agent
UPDATE_INTERVAL = 86400                # check at most once a day
ASSET_EXE = "ClaudeUsage.exe"
ASSET_SHA = "ClaudeUsage.exe.sha256"

KNOWN_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
NOTIFY_THRESHOLDS = (80.0, 95.0)

# Kept alive for the process lifetime so the single-instance mutex isn't freed.
_instance_handle = None

# Icon background colors (RGB)
COLOR_OK = (46, 160, 67)
COLOR_WARN = (210, 153, 34)
COLOR_HIGH = (218, 54, 51)
COLOR_STALE = (110, 118, 129)

# Taskbar text colors (hex, tuned for dark taskbar)
TEXT_OK = "#3fb950"
TEXT_WARN = "#d29922"
TEXT_HIGH = "#f85149"
TEXT_STALE = "#8b949e"

ICON_PALETTE = (COLOR_OK, COLOR_WARN, COLOR_HIGH, COLOR_STALE)
TEXT_PALETTE = (TEXT_OK, TEXT_WARN, TEXT_HIGH, TEXT_STALE)

# Color-key for taskbar transparency: everything this color becomes see-through.
# Near-black so antialiasing fringes are invisible on a dark taskbar.
KEY_COLOR = "#010101"
KEY_COLORREF = 0x00010101  # 0x00BBGGRR


def _warn(msg: str) -> None:
    """Write a warning to stderr, tolerating its absence.

    A PyInstaller --noconsole build sets sys.stderr to None; writing to it
    raises, and the windowed bootloader turns that into a modal "Unhandled
    exception" dialog that hangs the (single-threaded) process. Swallow it.
    """
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
    except (OSError, ValueError, AttributeError):
        pass


def _signed32(v: int) -> int:
    """Wrap an unsigned 32-bit value into the signed range a Win32 LONG expects.

    GetWindowLongW returns signed; Python bitwise ops on negatives produce
    arbitrary-precision values that overflow ctypes' 32-bit conversion.
    Mask to 32 bits, then reinterpret as signed.
    """
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


# ----------------------------------------------------------------------------
# Parsing (pure functions — unit-testable without a display)
# ----------------------------------------------------------------------------
def parse_usage(payload: dict) -> dict:
    """Extract known usage windows from the raw API payload.

    Returns {window_name: {"utilization": float, "resets_at": datetime|None}}
    for each window present and non-null. Unknown fields are ignored
    (the API carries internal feature-flag fields that come and go).
    `utilization` may arrive as int or float; `resets_at` may be null even
    when the window object exists.
    """
    out = {}
    if not isinstance(payload, dict):
        return out
    for name in KNOWN_WINDOWS:
        w = payload.get(name)
        if not isinstance(w, dict):
            continue
        util = w.get("utilization")
        if util is None:
            continue
        resets = None
        raw = w.get("resets_at")
        if raw:
            try:
                resets = datetime.fromisoformat(raw).astimezone()
            except (ValueError, TypeError):
                resets = None
        out[name] = {"utilization": float(util), "resets_at": resets}
    return out


def fmt_clock(dt) -> str:
    """'1:00 PM' today, 'Wed 9:00 AM' if a different day. Windows-safe."""
    if dt is None:
        return "?"
    s = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() != datetime.now().astimezone().date():
        s = dt.strftime("%a ") + s
    return s


def build_tooltip(usage: dict, stale: bool, retry_at=None,
                  signed_out: bool = False) -> str:
    """Detail line for tray tooltip / taskbar hover (tray caps at ~127 chars).

    `retry_at` (a datetime) means we're rate-limited; `signed_out` means the
    token is missing/expired. Either way, say why instead of a mute "stale".
    """
    note = (f"Rate-limited, retry {fmt_clock(retry_at)}" if retry_at
            else "Signed out: open Claude Code to sign in" if signed_out
            else None)
    if not usage:
        if note:
            return note
        return "Claude usage: no data" + (" (stale)" if stale else "")
    parts = []
    fh = usage.get("five_hour")
    if fh:
        parts.append(f"Session {fh['utilization']:.0f}% \u2192 {fmt_clock(fh['resets_at'])}")
    sd = usage.get("seven_day")
    if sd:
        parts.append(f"Week {sd['utilization']:.0f}% \u2192 {fmt_clock(sd['resets_at'])}")
    for name, label in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
        w = usage.get(name)
        if w and w["utilization"] > 0:
            parts.append(f"{label} {w['utilization']:.0f}%")
    tip = " | ".join(parts)
    if note:
        tip += " | " + note
    elif stale:
        tip += " (STALE)"
    return tip[:127]


def build_badge_text(usage: dict, stale: bool) -> str:
    """Compact string painted on the taskbar: 'Claude ~ weekly 2% | session 8%'."""
    fh = usage.get("five_hour")
    sd = usage.get("seven_day")
    parts = []
    if sd is not None:
        parts.append(f"weekly {sd['utilization']:.0f}%")
    if fh is not None:
        parts.append(f"session {fh['utilization']:.0f}%")
    txt = "Claude ~ " + (" | ".join(parts) if parts else "\u2014")
    if stale:
        txt += " *"
    return txt


def pick_color(pct, stale: bool, palette):
    """One threshold rule for icon and taskbar text: >80% high, >=50% warn,
    else ok; gray when stale/unknown. `palette` is (ok, warn, high, stale)."""
    ok, warn, high, gray = palette
    if stale or pct is None:
        return gray
    if pct > 80:
        return high
    if pct >= 50:
        return warn
    return ok


# ----------------------------------------------------------------------------
# Fetch result classification (pure — unit-testable without network)
# ----------------------------------------------------------------------------
# Outcome of one usage fetch.
# kind: "ok" | "signed_out" | "auth" | "rate_limited" | "error"
#   ok           -> .usage holds the parsed window dict
#   signed_out   -> token missing or expired; no request was made
#   auth         -> 401; Claude Code must renew the token
#   rate_limited -> 429; .retry_after holds the server's backoff seconds
#   error        -> anything else (network, 5xx, bad body); keep last data
FetchResult = namedtuple("FetchResult", "kind usage retry_after",
                         defaults=(None, None))


def parse_retry_after(headers, default: int = POLL_SECONDS) -> int:
    """Seconds to wait from a Retry-After header.

    This API returns an integer count of seconds. Anything non-integer
    (e.g. an HTTP-date) falls back to `default` rather than crashing.
    Negative values are clamped to 0.
    """
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return default
    try:
        return max(0, int(str(raw).strip()))
    except (ValueError, TypeError):
        return default


def classify_usage_response(status_code: int, headers, json_loader) -> FetchResult:
    """Map an HTTP response to a FetchResult.

    `json_loader` is a zero-arg callable returning the parsed body; it is
    only invoked for 200 so callers can defer/replace parsing. A 429 is a
    first-class outcome (not lumped into generic failure) so the poll loop
    can honor the server's backoff instead of hammering the endpoint.
    """
    if status_code == 200:
        try:
            payload = json_loader()
        except (ValueError, TypeError):
            return FetchResult("error")
        return FetchResult("ok", usage=parse_usage(payload))
    if status_code == 401:
        return FetchResult("auth")
    if status_code == 429:
        return FetchResult("rate_limited",
                           retry_after=parse_retry_after(headers))
    return FetchResult("error")


def compute_next_wait(result: FetchResult, base_poll: int,
                      streak: int = 0, cap: int = 3600) -> int:
    """Seconds until the next poll.

    After a 429 we back off exponentially on *consecutive* rate-limits
    (`streak`): base, base, 2x, 4x ... capped at `cap`. The usage endpoint
    limits account-wide and aggressively, so retrying flat every `base_poll`
    can keep re-tripping a rolling window that needs longer to drain (the app
    then never escapes "stale"). The wait is always at least the server's
    Retry-After when it sends one. `streak<=1` keeps the original base
    interval so a single transient 429 doesn't over-penalize."""
    if result.kind == "rate_limited":
        backoff = min(base_poll * 2 ** max(0, streak - 1), cap)
        return max(backoff, result.retry_after or 0)
    return base_poll


# ----------------------------------------------------------------------------
# Self-update (pure helpers — unit-testable without network)
# ----------------------------------------------------------------------------
UpdateInfo = namedtuple("UpdateInfo", "tag exe_url sha_url")  # a newer release to offer


def parse_version(tag: str) -> tuple:
    """'v1.2.3-beta' -> (1, 2, 3). Leading 'v' optional; parsing stops at the
    first dotted component without a leading integer (so pre-release suffixes
    and junk are ignored). Unparseable -> ()."""
    if not tag:
        return ()
    s = tag.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    out = []
    for part in s.split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        out.append(int(m.group()))
    return tuple(out)


def is_newer_version(remote: str, local: str) -> bool:
    """True if `remote` is a strictly higher version than `local`. Shorter
    versions are zero-padded, so 1.2 == 1.2.0."""
    r, l = parse_version(remote), parse_version(local)
    n = max(len(r), len(l))
    r += (0,) * (n - len(r))
    l += (0,) * (n - len(l))
    return r > l


def select_release_assets(release_json, exe_name: str, sha_name: str):
    """Map a GitHub releases/latest payload to an UpdateInfo, or None if the
    tag or either required asset is missing. Pure (no network)."""
    if not isinstance(release_json, dict):
        return None
    tag = release_json.get("tag_name")
    if not tag:
        return None
    urls = {}
    for asset in release_json.get("assets") or []:
        name = asset.get("name")
        if name in (exe_name, sha_name):
            urls[name] = asset.get("browser_download_url")
    if not (urls.get(exe_name) and urls.get(sha_name)):
        return None
    return UpdateInfo(tag, urls[exe_name], urls[sha_name])


def parse_sha256_sidecar(text: str):
    """Pull the 64-hex SHA-256 out of a sidecar file. Accepts a bare hash or
    sha256sum format ('<hash>  filename'). Returns it lowercased, or None."""
    if not text:
        return None
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return m.group(1).lower() if m else None


def verify_sha256(path: str, expected_hex: str, chunk: int = 1 << 20) -> bool:
    """Stream-hash a file and compare to expected_hex (case-insensitive)."""
    if not expected_hex:
        return False
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
    except OSError:
        return False
    return h.hexdigest() == expected_hex.lower()


def should_check_for_update(now: float, last_check, interval_s: int) -> bool:
    """True if at least interval_s has elapsed since last_check (or never)."""
    if last_check is None:
        return True
    return (now - last_check) >= interval_s


# ----------------------------------------------------------------------------
# Config + startup registration
# ----------------------------------------------------------------------------
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


def acquire_single_instance(name: str = APP_NAME):
    """Claim a session-local named mutex so only one instance polls.

    Two instances each polling the rate-limited usage endpoint is exactly
    what trips the 429s. Returns a handle to keep alive for the process
    lifetime if we're first, or None if another instance already holds it.
    Non-Windows is a no-op (returns True).
    """
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR)
    k32.CreateMutexW.restype = wintypes.HANDLE
    handle = k32.CreateMutexW(None, False, f"{name}_singleton")
    ERROR_ALREADY_EXISTS = 183
    if not handle:
        return None
    if k32.GetLastError() == ERROR_ALREADY_EXISTS:
        # CreateMutexW still hands back a valid handle to the existing object;
        # close it so a retry loop doesn't accumulate handles.
        k32.CloseHandle(handle)
        return None
    return handle


def acquire_single_instance_blocking(name: str = APP_NAME, timeout_s: float = 10.0):
    """Like acquire_single_instance, but waits up to timeout_s for an existing
    holder to exit before giving up. Used only by an update relaunch, where the
    outgoing process releases the mutex within ~1s. Normal launches use the
    one-shot variant so a genuine duplicate still exits immediately."""
    if sys.platform != "win32":
        return True
    deadline = time.time() + timeout_s
    while True:
        handle = acquire_single_instance(name)
        if handle is not None:
            return handle
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def startup_command() -> str:
    """Command line registered under HKCU\\...\\Run.

    Frozen (PyInstaller) -> the exe itself. Script mode -> pythonw + script
    so no console window appears at login.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interp = pyw if os.path.exists(pyw) else sys.executable
    return f'"{interp}" "{os.path.abspath(__file__)}"'


def is_startup_enabled() -> bool:
    """True if our HKCU Run entry exists and Task Manager hasn't switched it off.

    The Run key is one of the two sources Task Manager's Startup tab reads.
    Its Enable/Disable toggle is a separate per-entry flag under
    StartupApproved\\Run (first byte 2 = on, 3 = off; absent = on) that never
    touches our Run value, so both must be read.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
    except (ImportError, OSError):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            flag = winreg.QueryValueEx(k, APP_NAME)[0]
    except OSError:
        return True                   # never switched off in Task Manager
    return not (isinstance(flag, bytes) and flag[:1] and flag[0] & 1)


def set_startup(enabled: bool) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                  startup_command())
                # Clear Task Manager's "off" too, or the entry stays disabled.
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY,
                                        0, winreg.KEY_SET_VALUE) as a:
                        winreg.DeleteValue(a, APP_NAME)
                except OSError:
                    pass
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
    except (ImportError, OSError):
        pass


# ----------------------------------------------------------------------------
# Credentials (read-only: Claude Code owns them)
# ----------------------------------------------------------------------------
def read_credentials():
    try:
        with open(CRED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def fetch_usage(log=None) -> FetchResult:
    """GET usage as a FetchResult.

    We never renew the token. Claude Code refreshes it under its own lock
    (~/.claude/.oauth_refresh.lock) and wipes the saved login when a refresh
    token comes back rejected, so a second refresher that wins a race can sign
    the user out. A missing or expired token is "signed_out" without a
    request; the next poll picks up whatever Claude Code renewed. A 429 returns
    "rate_limited" with the server's Retry-After; anything else that isn't a
    clean 200 is "error" (keep last-known numbers, gray them out). `log` is an
    optional callable (str)->None for --debug diagnostics.
    """
    def _emit(msg):
        if log:
            log(msg)
    oauth = (read_credentials() or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        _emit("no access token in credentials -> signed_out")
        return FetchResult("signed_out")
    expires = oauth.get("expiresAt")   # epoch ms
    if isinstance(expires, (int, float)) and expires <= time.time() * 1000:
        _emit("access token expired; waiting for Claude Code to renew -> signed_out")
        return FetchResult("signed_out")
    try:
        r = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        _emit(f"GET usage raised {e!r} -> error")
        return FetchResult("error")
    _emit(f"GET usage -> HTTP {r.status_code}")
    return classify_usage_response(r.status_code, r.headers, r.json)


# ----------------------------------------------------------------------------
# Self-update (network — thin wrappers around the pure helpers)
# ----------------------------------------------------------------------------
def fetch_latest_release():
    """Query GitHub for the latest release; return an UpdateInfo or None.

    Public repo, so no auth. Never raises — update checks must not crash the
    app or interfere with usage polling.
    """
    try:
        r = requests.get(
            RELEASE_URL,
            headers={"User-Agent": GITHUB_UA,
                     "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        return select_release_assets(r.json(), ASSET_EXE, ASSET_SHA)
    except (requests.RequestException, ValueError):
        return None


def download_to(url: str, dest_path: str) -> bool:
    """Stream a URL to dest_path atomically (temp + os.replace). True on
    success. Never raises."""
    tmp = dest_path + ".part"
    try:
        with requests.get(url, headers={"User-Agent": GITHUB_UA},
                          stream=True, timeout=60) as r:
            if r.status_code != 200:
                return False
            with open(tmp, "wb") as f:
                for block in r.iter_content(chunk_size=1 << 20):
                    if block:
                        f.write(block)
        os.replace(tmp, dest_path)
        return True
    except (requests.RequestException, OSError):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


# ----------------------------------------------------------------------------
# Tray icon rendering
# ----------------------------------------------------------------------------
def render_icon(pct, stale: bool):
    """64x64 rounded square with the session % as bold white text."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 63, 63], radius=14,
                        fill=pick_color(pct, stale, ICON_PALETTE))

    text = "?" if pct is None else f"{min(pct, 999):.0f}"
    size = 44 if len(text) <= 2 else 32
    font = None
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((64 - w) / 2 - box[0], (64 - h) / 2 - box[1]),
           text, font=font, fill=(255, 255, 255, 255))
    return img


# ----------------------------------------------------------------------------
# Taskbar text badge (Windows-only, on by default; an overlay, never embedded)
# ----------------------------------------------------------------------------
# UIA state, lazily initialized; cached so we don't rebuild the COM object or
# retry a broken UIA path every reposition.
_uia = {"auto": None, "walker": None, "failed": False}


def _uia_setup():
    """Create (once) the IUIAutomation object and tree walker. Returns
    (automation, walker) or None if UIA is unavailable on this system."""
    if _uia["failed"]:
        return None
    if _uia["auto"] is not None:
        return _uia["auto"], _uia["walker"]
    try:
        import comtypes.client as cc
        # GetModule generates a wrapper module on first use. site-packages is
        # read-only in a frozen --onefile exe, so direct it at a writable dir.
        if getattr(sys, "frozen", False):
            gen = os.path.join(CONFIG_DIR, "comtypes_gen")
            os.makedirs(gen, exist_ok=True)
            cc.gen_dir = gen
        UIA = cc.GetModule("UIAutomationCore.dll")
        auto = cc.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        _uia.update(auto=auto, walker=auto.ControlViewWalker)
        return auto, _uia["walker"]
    except Exception:
        _uia["failed"] = True   # don't keep paying the cost of a broken path
        return None


def detect_taskbar_obstacles(taskbar_hwnd, taskbar_left, taskbar_width):
    """What the badge must stay clear of on the taskbar, in screen coords:
    (lefts, apps_right).

    `lefts` are the left edges of right-docked furniture — chiefly the Windows
    11 Widgets/weather button (a right-docked ToggleButton). `apps_right` is
    the right edge of the last app button, or None. Both live in XAML islands
    with no HWND, so only UI Automation can see them. ([], None) when UIA is
    unavailable (caller falls back to anchoring on the tray alone).

    NOTE: this is the one heavy, cross-process call (~20 ms); it must only run
    on a background thread (never the tk UI thread), so a slow UIA walk can't
    stall the overlay's message loop. The overlay is not parented into
    explorer, so a stall here can't hang explorer either.
    """
    setup = _uia_setup()
    if not setup:
        return [], None
    auto, walker = setup
    midpoint_abs = taskbar_left + taskbar_width // 2
    lefts, app_rights = [], []
    try:
        root = auto.ElementFromHandle(taskbar_hwnd)

        def walk(el, depth):
            if el is None or depth > 5:
                return
            child = walker.GetFirstChildElement(el)
            while child:
                try:
                    cls = child.CurrentClassName or ""
                    r = child.CurrentBoundingRectangle
                    if (r.right - r.left) > 0:
                        if cls == "ToggleButton" and r.left > midpoint_abs:
                            lefts.append(r.left)
                        elif cls == "Taskbar.TaskListButtonAutomationPeer":
                            app_rights.append(r.right)
                except Exception:
                    pass
                walk(child, depth + 1)
                child = walker.GetNextSiblingElement(child)

        walk(root, 0)
    except Exception:
        return [], None
    return lefts, max(app_rights, default=None)


@functools.cache
def _user32():
    """(user32, MONITORINFOEX) with explicit argtypes/restype for every Win32
    call we make. Without them ctypes assumes C int everywhere: HWNDs get
    truncated on 64-bit Python, and 32-bit style masks raise OverflowError.

    Declared once, on a private WinDLL. Redeclaring per call leaked a new ctypes
    class every 3s on Python 3.12 (~170 MB/day), and two struct types for one
    function made GetMonitorInfoW reject the fullscreen guard's argument."""
    import ctypes
    from ctypes import wintypes

    class MONITORINFOEX(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    u = ctypes.WinDLL("user32")
    u.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    u.FindWindowW.restype = wintypes.HWND
    u.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND,
                                wintypes.LPCWSTR, wintypes.LPCWSTR)
    u.FindWindowExW.restype = wintypes.HWND
    u.GetParent.argtypes = (wintypes.HWND,)
    u.GetParent.restype = wintypes.HWND
    u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    u.GetClassNameW.restype = ctypes.c_int
    u.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT)
    u.SetWindowPos.restype = wintypes.BOOL
    u.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    u.GetWindowLongW.restype = wintypes.LONG
    u.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.LONG)
    u.SetWindowLongW.restype = wintypes.LONG
    u.SetLayeredWindowAttributes.argtypes = (
        wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD)
    u.SetLayeredWindowAttributes.restype = wintypes.BOOL
    u.MoveWindow.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.BOOL)
    u.MoveWindow.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetForegroundWindow.argtypes = ()
    u.GetForegroundWindow.restype = wintypes.HWND
    u.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    u.MonitorFromWindow.restype = wintypes.HANDLE
    u.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFOEX))
    u.GetMonitorInfoW.restype = wintypes.BOOL
    return u, MONITORINFOEX


def enumerate_taskbar_displays():
    r"""Displays that currently host a Windows taskbar — the only places the
    badge can dock (it floats over a taskbar).

    Returns a list of {hwnd, device, is_primary, label, rect}, primary first.
    The primary taskbar is `Shell_TrayWnd`; each secondary monitor's taskbar
    is a `Shell_SecondaryTrayWnd` (present only when "Show my taskbar on all
    displays" is enabled). `device` (e.g. r"\\.\DISPLAY2") is the stable key we
    persist. Windows-only; non-blocking reads, so it's safe on the tk thread.
    Returns [] off Windows or on any failure (caller anchors to primary)."""
    if sys.platform != "win32":
        return []
    import ctypes

    MONITOR_DEFAULTTONEAREST = 0x2
    MONITORINFOF_PRIMARY = 0x1
    try:
        u, MONITORINFOEX = _user32()
        hwnds = []
        prim = u.FindWindowW("Shell_TrayWnd", None)
        if prim:
            hwnds.append(prim)
        h = 0
        while True:
            h = u.FindWindowExW(0, h, "Shell_SecondaryTrayWnd", None)
            if not h:
                break
            hwnds.append(h)

        out = []
        for hwnd in hwnds:
            mon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFOEX()
            mi.cbSize = ctypes.sizeof(MONITORINFOEX)
            if not u.GetMonitorInfoW(mon, ctypes.byref(mi)):
                continue
            device = mi.szDevice
            is_primary = bool(mi.dwFlags & MONITORINFOF_PRIMARY)
            r = mi.rcMonitor
            m = re.search(r"(\d+)$", device or "")
            label = f"Display {m.group(1)}" if m else (device or "Display")
            if is_primary:
                label += " (primary)"
            out.append({"hwnd": int(hwnd), "device": device,
                        "is_primary": is_primary, "label": label,
                        "rect": (r.left, r.top, r.right, r.bottom)})
        out.sort(key=lambda t: not t["is_primary"])   # primary first
        return out
    except Exception:
        return []


def compute_badge_x(obstacle_lefts, badge_w: int, margin: int, floor: int):
    """X for the badge so it sits clear of every right-docked obstacle, or
    None when it doesn't fit.

    `obstacle_lefts` are the screen-coordinate left edges of things the badge
    must not cover — the tray area, and on Windows 11 the right-docked
    Widgets/weather button. The badge anchors to the leftmost of them so it
    clears them all. `floor` is the leftmost x it may take: the end of the app
    buttons, else the docked taskbar's own left edge (negative on a monitor
    placed left of the primary). Below the floor it would cover app buttons,
    so the caller hides it (the tray icon still shows the session %).
    """
    x = min(obstacle_lefts) - badge_w - margin
    return x if x >= floor else None


def is_fullscreen(win_rect, monitor_rect, win_class: str = "") -> bool:
    """True if win_rect covers (>=) its monitor — a fullscreen app/video/game.
    Rects are (left, top, right, bottom). Borderless-fullscreen windows often
    spill a few px past the monitor, so use coverage, not equality. The
    desktop (Progman/WorkerW, foreground after a desktop click or Win+D)
    covers its monitor too but is not a fullscreen app."""
    if win_class in ("Progman", "WorkerW"):
        return False
    wl, wt, wr, wb = win_rect
    ml, mt, mr, mb = monitor_rect
    return wl <= ml and wt <= mt and wr >= mr and wb >= mb


def select_taskbar(taskbars, wanted_device):
    """Pick which taskbar the badge should dock to.

    `taskbars` is the list from enumerate_taskbar_displays() — dicts with at
    least `device` (monitor device name) and `is_primary`. Prefers the entry
    matching `wanted_device` (the user's saved choice); falls back to the
    primary taskbar, then the first available, so a disconnected/renamed
    display degrades to the primary instead of vanishing. Returns the chosen
    dict, or None when no taskbar exists at all."""
    if not taskbars:
        return None
    if wanted_device:
        for t in taskbars:
            if t.get("device") == wanted_device:
                return t
    for t in taskbars:
        if t.get("is_primary"):
            return t
    return taskbars[0]


class TaskbarBadge:
    """A standalone always-on-top text overlay floating over the taskbar.

    It is deliberately NOT parented into Shell_TrayWnd. An earlier version
    reparented this window into explorer's taskbar; explorer's UI thread then
    sent it synchronous messages and would hang waiting on our tk loop whenever
    the loop stalled (Windows logged AppHangXProcB1 with partner ClaudeUsage.exe
    and restarted explorer). As an unowned top-level window, explorer never
    waits on us, so it cannot hang us or be hung by us.

    Positioning uses only non-blocking reads of explorer's window rects
    (GetWindowRect/FindWindowExW) plus MoveWindow on our OWN window \u2014 none of
    which couple us to explorer's UI thread. The one heavy call (the UIA widget
    probe) runs on a background thread, off the tk loop.
    """

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080    # no taskbar/alt-tab entry
    WS_EX_NOACTIVATE = 0x08000000    # clicks don't steal focus
    WS_EX_TOPMOST = 0x00000008
    LWA_COLORKEY = 0x1
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x1
    SWP_NOMOVE = 0x2
    SWP_NOACTIVATE = 0x10
    MONITOR_DEFAULTTONEAREST = 0x2
    MARGIN_RIGHT = 10  # px gap between badge and tray area / app buttons
    TOPMOST_MS = 120   # re-assert topmost this often so taskbar clicks can't bury us
    PROBE_SECONDS = 5  # re-read app buttons / Widgets; they move as apps open and close

    def __init__(self, app, debug=False):
        import ctypes
        import tkinter as tk

        self.app = app
        self.debug = debug
        self.u32, self._MONITORINFO = _user32()
        self.tk = tk
        self.tooltip = None
        self._tick_count = 0
        self._probe = ([], None)    # (obstacle lefts, apps_right) from the UIA probe
        self._last_geom = None      # (x,y,w,h) of the last MoveWindow; change-gate
        self._hidden = False        # withdrawn (fullscreen app, or no room)
        self._no_room = False       # badge would cover app buttons
        self._dock_hwnd = 0         # taskbar HWND we're docked to (which monitor)

        # DPI awareness before any window is created, so coords line up
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY_COLOR)
        self.label = tk.Label(self.root, text="Claude \u2026", fg=TEXT_STALE,
                              bg=KEY_COLOR, font=("Segoe UI Semibold", 10),
                              padx=6)
        self.label.pack()
        self.label.bind("<Button-1>", self.on_click)
        self.label.bind("<Enter>", self.show_tooltip)
        self.label.bind("<Leave>", self.hide_tooltip)
        self.root.update_idletasks()

        # Real top-level HWND is the wrapper around tk's client window
        self.hwnd = self.u32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        self._apply_overlay_styles()
        # Probe the app buttons / Widgets button off the UI thread so a slow
        # UIA walk can never stall the overlay loop (or explorer).
        threading.Thread(target=self._probe_obstacles, daemon=True).start()
        self.reposition()

    def log(self, msg):
        if self.debug:
            print(f"[badge] {msg}", flush=True)

    def _apply_overlay_styles(self):
        """Layered (color-key transparent), tool, no-activate, topmost \u2014 a
        click-through-safe overlay that owns no taskbar entry."""
        ex = self.u32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE) & 0xFFFFFFFF
        ex |= (self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW
               | self.WS_EX_NOACTIVATE | self.WS_EX_TOPMOST)
        self.u32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE, _signed32(ex))
        self.u32.SetLayeredWindowAttributes(self.hwnd, KEY_COLORREF, 255,
                                            self.LWA_COLORKEY)

    def _probe_obstacles(self):
        """Background UIA scan of the primary taskbar, repeated every
        PROBE_SECONDS (app buttons come and go; the Widgets button changes
        width). Always this one thread: the UIA COM objects belong to the
        thread that created them. reposition() reads the result. Best-effort."""
        while True:
            try:
                tb = self.u32.FindWindowW("Shell_TrayWnd", None)
                r = self._rect(tb) if tb else None
                if r:
                    probe = detect_taskbar_obstacles(tb, r.left, r.right - r.left)
                    if probe != self._probe:
                        self.log(f"obstacles (abs), apps_right: {probe}")
                    self._probe = probe
            except Exception as e:
                self.log(f"obstacle probe failed: {e}")
            if self.app.stop.wait(self.PROBE_SECONDS):
                return

    # -- win32 plumbing -------------------------------------------------------
    def _rect(self, hwnd):
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if not self.u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return r

    def reposition(self):
        """Place the overlay just left of the tray, in SCREEN coordinates.

        Docks to the taskbar of the user's chosen display (config["display"],
        read each call so the choice applies live), falling back to the primary
        taskbar. Uses only non-blocking reads of explorer's window rects +
        MoveWindow on our own window, then re-asserts topmost (the taskbar is
        itself topmost and can otherwise cover us). MoveWindow is skipped when
        geometry is unchanged, so the steady state does almost nothing.
        """
        chosen = select_taskbar(enumerate_taskbar_displays(),
                                self.app.config.get("display"))
        tb_hwnd = chosen["hwnd"] if chosen else self.u32.FindWindowW(
            "Shell_TrayWnd", None)
        tb = self._rect(tb_hwnd)
        if tb is None:
            return
        self._dock_hwnd = tb_hwnd      # which monitor the badge now lives on
        tb_h = tb.bottom - tb.top

        # Tray notification area left edge (screen coords), else a fallback.
        # Secondary taskbars have no TrayNotifyWnd, so the fallback (reserve a
        # strip at the right) is what anchors the badge there, clear of the clock.
        tray = self.u32.FindWindowExW(tb_hwnd, 0, "TrayNotifyWnd", None)
        tr = self._rect(tray) if tray else None
        tray_left = tr.left if tr else (tb.right - 250)

        # The probe reads the primary taskbar, in primary-monitor coords; only
        # use it when docked to the primary, else compute_badge_x's min() would
        # drag the badge back to the primary.
        # shortcut: no app-button floor on secondary taskbars; probe those too
        # if a crowded secondary taskbar ever gets covered.
        obstacles, apps_right = (self._probe if (chosen and chosen["is_primary"])
                                 else ([], None))
        floor = tb.left if apps_right is None else apps_right + self.MARGIN_RIGHT

        self.root.update_idletasks()
        w = self.label.winfo_reqwidth()
        h = self.label.winfo_reqheight()
        x = compute_badge_x([tray_left] + obstacles, w, self.MARGIN_RIGHT, floor)
        y = tb.top + max((tb_h - h) // 2, 0)

        if self._no_room != (x is None):
            self.log(f"no room left of the tray (apps end at {apps_right}) -> "
                     f"{'hide' if x is None else 'show'}")
        self._no_room = x is None
        geom = (x, y, w, h)
        if x is not None and geom != self._last_geom:
            self.u32.MoveWindow(self.hwnd, x, y, w, h, True)
            self._last_geom = geom
            self.log(f"moved to ({x},{y}) {w}x{h}; tray_left={tray_left}, "
                     f"obstacles={obstacles}, apps_right={apps_right}, "
                     f"dock={tb_hwnd}")
        self._assert_topmost()

    def _fullscreen_on_badge_monitor(self) -> bool:
        """True only if a fullscreen app/video/game is foreground *on the same
        monitor as the badge*. A fullscreen window on another display must not
        hide us (the reported dual-monitor bug). Best-effort; any failure →
        False (treat as not fullscreen)."""
        import ctypes
        try:
            fg = self.u32.GetForegroundWindow()
            if not fg or fg == self.hwnd:
                return False
            wr = self._rect(fg)
            if wr is None:
                return False
            cls = ctypes.create_unicode_buffer(16)
            self.u32.GetClassNameW(fg, cls, 16)
            fg_mon = self.u32.MonitorFromWindow(fg, self.MONITOR_DEFAULTTONEAREST)
            mi = self._MONITORINFO()
            mi.cbSize = ctypes.sizeof(self._MONITORINFO)
            if not self.u32.GetMonitorInfoW(fg_mon, ctypes.byref(mi)):
                return False
            m = mi.rcMonitor
            covers = is_fullscreen((wr.left, wr.top, wr.right, wr.bottom),
                                   (m.left, m.top, m.right, m.bottom), cls.value)
            if not covers:
                return False           # not a fullscreen window — nothing to do
            # It IS fullscreen; hide only if it's on the badge's own monitor.
            badge_mon = self.u32.MonitorFromWindow(
                self._dock_hwnd or self.hwnd, self.MONITOR_DEFAULTTONEAREST)
            same = fg_mon == badge_mon
            self.log(f"fullscreen fg={fg} fg_mon={fg_mon} badge_mon={badge_mon} "
                     f"dock={self._dock_hwnd} same_monitor={same} -> "
                     f"{'hide' if same else 'keep'}")
            return same
        except Exception as e:
            self.log(f"fullscreen check failed: {e!r}")
            return False

    def _assert_topmost(self):
        """Pop the overlay back above the (topmost) taskbar — clicking the
        taskbar otherwise raises it over us. Skipped (and the overlay hidden)
        while there's no room beside the app buttons, or a fullscreen app is
        foreground *on the badge's own monitor*, mirroring the taskbar's
        auto-hide; a fullscreen app on another display leaves us visible. A
        Z-only, non-activating change: cheap, no repaint, doesn't eat clicks."""
        if self._no_room or self._fullscreen_on_badge_monitor():
            if not self._hidden:
                self.root.withdraw()
                self._hidden = True
            return
        if self._hidden:
            self.root.deiconify()
            self._hidden = False
            self._last_geom = None      # force a reposition after re-showing
        self.u32.SetWindowPos(self.hwnd, self.HWND_TOPMOST, 0, 0, 0, 0,
                              self.SWP_NOMOVE | self.SWP_NOSIZE
                              | self.SWP_NOACTIVATE)

    def _topmost_tick(self):
        """Fast, cheap loop that keeps the overlay above the taskbar so a
        taskbar click can't bury it for seconds. Separate from the 750ms tick;
        only does the topmost re-assert, not the heavier reposition()."""
        if self.app.stop.is_set():
            return
        try:
            self._assert_topmost()
        except Exception as e:
            self.log(f"topmost re-assert failed: {e}")
        self.root.after(self.TOPMOST_MS, self._topmost_tick)

    # -- interactions ---------------------------------------------------------
    def on_click(self, _event):
        threading.Thread(target=self.app.poll_once, daemon=True).start()

    def show_tooltip(self, _event):
        self.hide_tooltip(None)
        tip = self.tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        lbl = self.tk.Label(tip,
                            text=self.app.tooltip_text(),
                            bg="#1f2428", fg="#e6edf3",
                            font=("Segoe UI", 9), padx=8, pady=4)
        lbl.pack()
        tip.update_idletasks()
        x = self.label.winfo_rootx()
        y = self.label.winfo_rooty() - tip.winfo_reqheight() - 8
        tip.geometry(f"+{x}+{y}")
        self.tooltip = tip

    def hide_tooltip(self, _event):
        if self.tooltip is not None:
            try:
                self.tooltip.destroy()
            except Exception:
                pass
            self.tooltip = None

    # -- main loop ------------------------------------------------------------
    def tick(self):
        if self.app.stop.is_set():
            self.root.destroy()
            return
        fh = self.app.usage.get("five_hour")
        pct = fh["utilization"] if fh else None
        self.label.config(text=build_badge_text(self.app.usage, self.app.stale),
                          fg=pick_color(pct, self.app.stale, TEXT_PALETTE))

        # Every ~3s: re-resolve the taskbar and reposition (handles explorer
        # restarts and tray-width changes — no re-embed needed since we're not
        # parented into the taskbar).
        self._tick_count += 1
        if self._tick_count % 4 == 0:
            try:
                self.reposition()
            except Exception as e:
                self.log(f"reposition failed: {e}")

        self.root.after(750, self.tick)

    def run(self):
        self.root.after(100, self.tick)
        self.root.after(150, self._topmost_tick)
        self.root.mainloop()


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
class UsageTray:
    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self.usage = {}
        self.stale = True
        self.notified = set()
        self.window_key = None
        self.stop = threading.Event()
        self.icon = None
        self.rate_limited_until = None   # epoch seconds; None when not limited
        self.rate_limit_streak = 0       # consecutive 429s; drives backoff
        self._last_fetch = 0.0           # epoch of the last real fetch (MIN_FETCH_GAP)
        self._poll_lock = threading.Lock()  # one fetch at a time: loop vs clicks
        self._update_lock = threading.Lock()  # one self-update at a time
        self.last_result = FetchResult("error")
        self._pending = None             # UpdateInfo when a newer release exists
        self.last_update_check = self.config.get("last_update_check")
        self.debug = False               # set by run(); gates [poll] logging

    def _log(self, msg):
        if self.debug:
            print(f"[poll] {msg}", flush=True)

    # -- polling --------------------------------------------------------------
    def poll_once(self):
        """Fetch once and fold the result into UI state. Returns the result.

        While inside a server-mandated 429 cooldown we DON'T hit the endpoint
        (a manual 'Refresh' otherwise just refreshes the rolling limit and
        keeps it from draining). Nor while another fetch is in flight, or
        within MIN_FETCH_GAP of the last one, so repeated clicks on the badge
        or 'Refresh' can't hammer the account-wide limit."""
        if not self._poll_lock.acquire(blocking=False):
            return self.last_result
        try:
            now = time.time()
            if self.rate_limited_until and now < self.rate_limited_until:
                self._log(f"in cooldown until {fmt_clock(self.retry_at())}, "
                          f"skipping fetch (streak={self.rate_limit_streak})")
                self.update_icon()
                return self.last_result
            if now - self._last_fetch < MIN_FETCH_GAP:
                self._log(f"fetched {now - self._last_fetch:.0f}s ago, skipping")
                return self.last_result
            self._last_fetch = now
            res = fetch_usage(log=self._log)
            self.last_result = res
            if res.kind == "signed_out":
                self._last_fetch = 0.0     # no request was made; don't hold back a Refresh
            if res.kind == "ok":
                self.usage = res.usage
                self.stale = False
                self.rate_limited_until = None
                self.rate_limit_streak = 0
                self.check_thresholds()
            elif res.kind == "rate_limited":
                self.stale = True          # keep last-known numbers, gray them out
                self.rate_limit_streak += 1
                wait = compute_next_wait(res, POLL_SECONDS, self.rate_limit_streak)
                self.rate_limited_until = time.time() + wait
                self._log(f"429 retry_after={res.retry_after} "
                          f"streak={self.rate_limit_streak} backoff={wait}s "
                          f"until {fmt_clock(self.retry_at())}")
            else:                          # signed_out / auth / error
                self.stale = True
            self._log(f"kind={res.kind} stale={self.stale} "
                      f"usage={list((self.usage or {}).keys())}")
            self.update_icon()
            return res
        finally:
            self._poll_lock.release()

    def poll_loop(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
                # Sleep out any cooldown (a manual refresh may have set it
                # mid-interval) instead of restarting a full backoff. Signed
                # out, re-check the token file soon so the badge recovers
                # right after Claude Code signs back in.
                wait = (self.rate_limited_until - time.time() if self.retry_at()
                        else SIGNIN_RECHECK if self.last_result.kind == "signed_out"
                        else POLL_SECONDS)
            except Exception as e:
                self.stale = True
                self.update_icon()
                self._log(f"poll raised {e!r}; retrying in {POLL_SECONDS}s")
                wait = POLL_SECONDS
            self._log(f"sleeping {wait:.0f}s")
            self.stop.wait(wait)

    # -- self-update ------------------------------------------------------------
    def update_loop(self):
        """Check for a newer release shortly after launch, then ~daily.
        Only started for frozen builds (script mode can't self-replace)."""
        if self.stop.wait(8):            # let startup settle
            return
        while not self.stop.is_set():
            if should_check_for_update(time.time(), self.last_update_check,
                                       UPDATE_INTERVAL):
                try:
                    self.check_for_update()
                except Exception:
                    pass
            if self.stop.wait(3600):     # wake hourly; the gate throttles to daily
                return

    def check_for_update(self, manual=False):
        """Fetch latest release; if newer, arm the tray item + toast once.
        A failed fetch (e.g. offline at login) isn't stamped, so the hourly
        wake in update_loop retries it instead of waiting a day. `manual` (the
        menu item) always answers with a toast, even when there's no update."""
        info = fetch_latest_release()
        if not info:
            if manual and self.icon:
                self.icon.notify("Couldn't reach GitHub to check for updates.",
                                 "ClaudeUsage")
            return
        self.last_update_check = time.time()
        self.config["last_update_check"] = self.last_update_check
        save_config(self.config)
        if is_newer_version(info.tag, VERSION):
            already = self._pending is not None and self._pending.tag == info.tag
            self._pending = info
            if self.icon:
                if manual or not already:
                    self.icon.notify(
                        f"ClaudeUsage {info.tag} is available — open the menu "
                        f"to update.", "Update available")
                self.icon.update_menu()
        elif manual and self.icon:
            self.icon.notify(f"You're on the latest version (v{VERSION}).",
                             "ClaudeUsage")

    def start_update(self):
        # One update at a time: a second click mid-download would race the
        # first over the same files and report a false failure.
        if not self._update_lock.acquire(blocking=False):
            return

        def run():
            try:
                self.apply_update()
            finally:
                self._update_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def apply_update(self):
        """Download, verify (SHA-256), swap the running exe, and relaunch.

        Critical-section ordering with rollback so a failure never leaves the
        install without a working ClaudeUsage.exe. INSTALL_DIR is captured up
        front — sys.executable is unreliable once the exe is renamed.
        """
        info = self._pending
        if not info or not getattr(sys, "frozen", False):
            return
        install_dir = os.path.dirname(os.path.abspath(sys.executable))
        canonical = os.path.join(install_dir, ASSET_EXE)
        old = os.path.join(install_dir, "ClaudeUsage.old.exe")
        new = os.path.join(install_dir, "ClaudeUsage.new.exe")
        if self.icon:
            self.icon.notify(f"Downloading {info.tag}...", "ClaudeUsage")

        # 1. Download + verify BEFORE touching the installed exe.
        if not download_to(info.exe_url, new):
            return self._update_failed("download failed", new)
        expected = self._fetch_sidecar_hash(info.sha_url)
        if not expected or not verify_sha256(new, expected):
            return self._update_failed("verification failed", new)

        # 2. Critical section: rename current out of the way, move new in.
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            return self._update_failed("could not clear old backup", new)
        if not self._move_retry(canonical, old, replace=False):
            return self._update_failed("could not stage current exe", new)
        if not self._move_retry(new, canonical, replace=True):
            self._move_retry(old, canonical, replace=False)   # rollback
            return self._update_failed("could not install new exe", None)

        # 3. Relaunch the new exe, then quit to release the mutex. If it won't
        # start (e.g. antivirus blocks the fresh unsigned exe), put this build
        # back and keep running rather than quit into nothing.
        if not self._relaunch(canonical, install_dir):
            self._move_retry(old, canonical, replace=True)
            return self._update_failed("the new version would not start", None)
        self.quit()

    def _fetch_sidecar_hash(self, url):
        try:
            r = requests.get(url, headers={"User-Agent": GITHUB_UA}, timeout=15)
            if r.status_code != 200:
                return None
            return parse_sha256_sidecar(r.text)
        except requests.RequestException:
            return None

    def _move_retry(self, src, dst, replace, tries=3, delay=0.2):
        """os.replace/os.rename with bounded retry for transient AV file locks."""
        for i in range(tries):
            try:
                os.replace(src, dst) if replace else os.rename(src, dst)
                return True
            except OSError:
                if i == tries - 1:
                    return False
                time.sleep(delay)
        return False

    def _relaunch(self, exe, cwd) -> bool:
        """Start the new exe as an independent process. True if it started."""
        import subprocess
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        try:
            subprocess.Popen(
                [exe, "--updated"],
                creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                               | CREATE_NO_WINDOW),
                close_fds=True, cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Without this the new --onefile exe inherits our _PYI_* vars,
                # runs out of OUR unpack dir (old DLLs), and our launcher can't
                # delete that dir: it hangs on a "Failed to remove temporary
                # directory" dialog.
                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
            )
            return True
        except OSError:
            return False

    def _update_failed(self, reason, cleanup_path):
        if cleanup_path:
            try:
                os.remove(cleanup_path)
            except OSError:
                pass
        if self.icon:
            self.icon.notify(f"Update failed: {reason}. Still on v{VERSION}.",
                             "ClaudeUsage")

    # -- notifications ----------------------------------------------------------
    def check_thresholds(self):
        fh = self.usage.get("five_hour")
        if not fh:
            return
        # resets_at carries per-request microseconds; compare to the minute or
        # every poll looks like a new window and re-fires the toasts.
        key = (fh["resets_at"].replace(second=0, microsecond=0)
               if fh["resets_at"] else None)
        if key != self.window_key:     # new 5h window -> re-arm toasts
            self.window_key = key
            self.notified.clear()
        # One toast for the highest threshold crossed, not one per threshold
        # (a first poll at 96% would otherwise fire 80% and 95% together).
        crossed = [t for t in NOTIFY_THRESHOLDS if fh["utilization"] >= t]
        if crossed and crossed[-1] not in self.notified:
            self.notified.update(crossed)
            if self.icon:
                self.icon.notify(
                    f"Session usage at {fh['utilization']:.0f}% "
                    f"(resets {fmt_clock(fh['resets_at'])})",
                    "Claude usage",
                )

    # -- UI ---------------------------------------------------------------------
    def retry_at(self):
        """datetime the rate limit lifts, or None if not (still) limited."""
        if self.rate_limited_until and time.time() < self.rate_limited_until:
            return datetime.fromtimestamp(self.rate_limited_until).astimezone()
        return None

    def tooltip_text(self):
        return build_tooltip(self.usage, self.stale, self.retry_at(),
                             signed_out=self.last_result.kind in ("signed_out", "auth"))

    def update_icon(self):
        if not self.icon:
            return
        fh = self.usage.get("five_hour")
        pct = fh["utilization"] if fh else None
        self.icon.icon = render_icon(pct, self.stale)
        self.icon.title = self.tooltip_text()

    def toggle_startup(self):
        set_startup(not is_startup_enabled())

    def toggle_taskbar(self):
        self.config["taskbar"] = not self.config.get("taskbar", True)
        save_config(self.config)

    def set_display(self, device):
        """Persist which display the badge docks to (None = auto/primary).
        reposition() reads this each tick, so it applies live."""
        self.config["display"] = device
        save_config(self.config)

    def _make_display_setter(self, device):
        """Return a genuinely 0-arg callback that selects `device`.

        pystray's _assert_action keys off co_argcount, which counts
        default-valued params too — so an inline `lambda dev=dev: ...` is
        misread as a 1-arg action and gets the Icon passed in as `dev`
        (set_display then stores an unserializable Icon and the choice is
        silently dropped). A factory closure is a true 0-arg callable."""
        return lambda: self.set_display(device)

    def build_icon(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        frozen = getattr(sys, "frozen", False)
        items = [Item(f"ClaudeUsage v{VERSION}", lambda: None, enabled=False)]
        if frozen:
            # Visible only once a newer release is staged; text/visibility are
            # callables so update_menu() reflects state without rebuilding.
            # ("and", not "&": Win32 menus eat a lone & as a shortcut marker.)
            items.append(Item(
                lambda item: (f"Update to {self._pending.tag} and restart"
                              if self._pending else "Update and restart"),
                self.start_update,
                visible=lambda item: self._pending is not None))
        items += [
            Menu.SEPARATOR,
            Item("Refresh now", lambda: threading.Thread(
                target=self.poll_once, daemon=True).start()),
            Item("Open usage page", lambda: webbrowser.open(USAGE_PAGE)),
        ]
        if frozen:
            items.append(Item("Check for updates now", lambda: threading.Thread(
                target=self.check_for_update, args=(True,), daemon=True).start()))
        if sys.platform == "win32":
            items += [
                Menu.SEPARATOR,
                Item("Run at startup", self.toggle_startup,
                     checked=lambda item: is_startup_enabled()),
                Item("Show on taskbar (applies on restart)",
                     self.toggle_taskbar,
                     checked=lambda item: bool(self.config.get("taskbar", True))),
            ]
            # "Show on display" — listed once at startup. Only displays with a
            # taskbar can host the badge. Auto = follow the primary taskbar.
            displays = enumerate_taskbar_displays()
            if len(displays) > 1:
                disp_items = [Item(
                    "Auto (primary)", self._make_display_setter(None),
                    radio=True,
                    checked=lambda item: not self.config.get("display"))]
                for d in displays:
                    dev = d["device"]
                    disp_items.append(Item(
                        d["label"],
                        self._make_display_setter(dev),
                        radio=True,
                        checked=lambda item, dev=dev:
                            self.config.get("display") == dev))
                items.append(Item("Show on display", Menu(*disp_items)))
        items += [Menu.SEPARATOR, Item("Quit", self.quit)]

        self.icon = pystray.Icon(
            "claude_usage",
            icon=render_icon(None, True),
            title="Claude usage: starting...",
            menu=Menu(*items),
        )

    def run(self, taskbar=False, debug=False):
        self.debug = debug
        self.build_icon()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        if getattr(sys, "frozen", False):
            threading.Thread(target=self.update_loop, daemon=True).start()

        badge = None
        if taskbar:
            if sys.platform != "win32":
                _warn("--taskbar is Windows-only; tray icon mode.\n")
            else:
                try:
                    badge = TaskbarBadge(self, debug=debug)
                except Exception as e:
                    _warn(f"Taskbar embed failed ({e!r}); tray icon mode.\n")
                    badge = None

        if badge:
            # tk needs the main thread; pystray runs its own message loop fine
            # in a daemon thread on Windows.
            threading.Thread(target=self.icon.run, daemon=True).start()
            badge.run()                # blocks until quit/stop
            self.icon.stop()
        else:
            self.icon.run()            # blocks until quit

    def quit(self):
        self.stop.set()                # badge tick sees this and destroys itself
        if self.icon:
            self.icon.stop()


def cleanup_update_leftovers():
    """Best-effort removal of files left by a self-update: the previous exe
    (.old, unlocked once that process exited) and any half-downloaded .new."""
    if not getattr(sys, "frozen", False):
        return
    install_dir = os.path.dirname(os.path.abspath(sys.executable))
    for name in ("ClaudeUsage.old.exe", "ClaudeUsage.new.exe",
                 "ClaudeUsage.new.exe.part"):
        try:
            p = os.path.join(install_dir, name)
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Claude usage taskbar widget")
    ap.add_argument("--taskbar", action="store_true",
                    help="force taskbar text mode on (and persist it to config)")
    ap.add_argument("--debug", action="store_true",
                    help="print taskbar-embed diagnostics (run with python, not pythonw)")
    ap.add_argument("--updated", action="store_true",
                    help="set by a self-update relaunch; waits briefly for the "
                         "outgoing instance to release the single-instance lock")
    args = ap.parse_args()

    # Only one instance may poll; a second would double the request rate into
    # the rate-limited usage endpoint. Hold the handle for the process life.
    # An update relaunch waits out the outgoing instance's lock; a normal
    # duplicate launch exits immediately.
    global _instance_handle
    _instance_handle = (acquire_single_instance_blocking() if args.updated
                        else acquire_single_instance())
    if _instance_handle is None:
        _warn("ClaudeUsage is already running.\n")
        sys.exit(0)

    # Only the instance that owns the mutex cleans up, so a duplicate launch
    # can't delete a running update's download. Delayed: the outgoing
    # instance's launcher still holds .old.exe for a moment after the mutex.
    threading.Timer(5, cleanup_update_leftovers).start()

    config = load_config()
    if args.taskbar:
        config["taskbar"] = True
        save_config(config)
    # Taskbar mode defaults ON; embed failures fall back to tray-only anyway.
    UsageTray(config).run(taskbar=config.get("taskbar", True), debug=args.debug)


if __name__ == "__main__":
    main()
