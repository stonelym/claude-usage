"""
claude_usage_tray.py — Claude plan-usage widget for the Windows taskbar.

Two display modes:
  1. Taskbar text (default): "Claude ~ weekly 2% | session 8%" painted
     directly on the taskbar surface, left of the tray area. Falls back to
     mode 2 automatically if the embed fails.
  2. Tray icon (always on): colored square with session % in the system
     tray, carrying the menu and toast notifications.

Tray menu toggles:
  - "Run at startup": registers/removes an HKCU Run entry, which also makes
    the app appear in Task Manager > Startup apps with its own toggle.
  - "Show on taskbar": persisted to %APPDATA%\\ClaudeUsage\\config.json,
    applies on next launch.

Packaging: pyinstaller --onefile --noconsole --name ClaudeUsage (see
build.ps1). Script mode still works: pythonw claude_usage_tray.py.
Debug the taskbar embed with: python claude_usage_tray.py --debug

Data source: the unofficial endpoint Claude Code's /usage uses
(GET https://api.anthropic.com/api/oauth/usage). Token is read from
%USERPROFILE%\\.claude\\.credentials.json and auto-refreshed on 401 via
the Claude Code OAuth refresh flow (new tokens are written back to the
credentials file so Claude Code stays in sync).

Deps:  pip install pystray pillow requests        (tkinter ships with Python)

The taskbar embed is an unsupported Windows hack (DeskBands are deprecated);
it re-attaches itself after explorer restarts and repositions as the tray
grows/shrinks, but a future Windows update could break it. The tray icon
mode has no such risk.
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
# /api/oauth/usage is an on-demand endpoint (Claude Code only hits it when you
# run /usage). The 5h/7d windows move slowly, so polling every 5 min is plenty
# and — crucially — avoids tripping the endpoint's rate limit. See fetch_usage.
POLL_SECONDS = 300
APP_NAME = "ClaudeUsage"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code public client
CRED_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
USAGE_PAGE = "https://claude.ai/settings/usage"
USER_AGENT = "claude-code/2.0.31"

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


def build_tooltip(usage: dict, stale: bool, retry_at=None) -> str:
    """Detail line for tray tooltip / taskbar hover (tray caps at ~127 chars).

    `retry_at` (a datetime) means we're rate-limited; surface when usage will
    be readable again instead of a mute "no data".
    """
    rl = f"Rate-limited, retry {fmt_clock(retry_at)}" if retry_at else None
    if not usage:
        if rl:
            return rl
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
    if rl:
        tip += " | " + rl
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


def pick_color(pct, stale: bool):
    if stale or pct is None:
        return COLOR_STALE
    if pct > 80:
        return COLOR_HIGH
    if pct >= 50:
        return COLOR_WARN
    return COLOR_OK


def pick_text_color(pct, stale: bool) -> str:
    if stale or pct is None:
        return TEXT_STALE
    if pct > 80:
        return TEXT_HIGH
    if pct >= 50:
        return TEXT_WARN
    return TEXT_OK


# ----------------------------------------------------------------------------
# Fetch result classification (pure — unit-testable without network)
# ----------------------------------------------------------------------------
class FetchResult:
    """Outcome of one usage fetch.

    kind: "ok" | "auth" | "rate_limited" | "error"
      ok           -> .usage holds the parsed window dict
      auth         -> 401; caller may refresh the token and retry once
      rate_limited -> 429; .retry_after holds the server's backoff seconds
      error        -> anything else (network, 5xx, bad body); keep last data
    """
    __slots__ = ("kind", "usage", "retry_after")

    def __init__(self, kind, usage=None, retry_after=None):
        self.kind = kind
        self.usage = usage
        self.retry_after = retry_after


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


def compute_next_wait(result: FetchResult, base_poll: int) -> int:
    """Seconds until the next poll. After a 429, wait at least the server's
    Retry-After so the rolling rate-limit window can actually drain."""
    if result.kind == "rate_limited" and result.retry_after:
        return max(base_poll, result.retry_after)
    return base_poll


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
    if not handle or k32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


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
    """True if our HKCU Run entry exists.

    The Run key is one of the two sources Task Manager's Startup tab reads,
    so once enabled here the entry appears there with its own Enable/Disable
    toggle (Windows tracks that separately in StartupApproved without
    touching our value).
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except (ImportError, OSError):
        return False


def set_startup(enabled: bool) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                  startup_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
    except (ImportError, OSError):
        pass


# ----------------------------------------------------------------------------
# Credentials + refresh
# ----------------------------------------------------------------------------
def read_credentials():
    try:
        with open(CRED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_credentials(creds: dict) -> None:
    """Atomic write so a crash mid-write can't corrupt Claude Code's auth."""
    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    os.replace(tmp, CRED_PATH)


def refresh_token():
    """OAuth refresh; writes new tokens back to the credentials file.

    Returns a fresh access token, or None. Only called after a 401 so we
    don't race Claude Code's own proactive refresh more than necessary.
    """
    creds = read_credentials()
    oauth = (creds or {}).get("claudeAiOauth") or {}
    rt = oauth.get("refreshToken")
    if not rt:
        return None
    try:
        r = requests.post(
            TOKEN_URL,
            json={"grant_type": "refresh_token", "refresh_token": rt,
                  "client_id": CLIENT_ID},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        tok = r.json()
    except (requests.RequestException, ValueError):
        return None

    access = tok.get("access_token")
    if not access:
        return None
    oauth["accessToken"] = access
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(tok["expires_in"]) * 1000
    creds["claudeAiOauth"] = oauth
    try:
        write_credentials(creds)
    except OSError:
        pass  # still return the in-memory token; worst case we refresh again
    return access


def fetch_usage() -> FetchResult:
    """GET usage as a FetchResult; one 401-triggered refresh retry.

    A 429 returns kind "rate_limited" with the server's Retry-After so the
    caller can back off; everything else that isn't a clean 200 is "error"
    (keep last-known numbers, gray them out).
    """
    creds = read_credentials()
    token = ((creds or {}).get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        return FetchResult("auth")
    for attempt in (1, 2):
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
        except requests.RequestException:
            return FetchResult("error")
        res = classify_usage_response(r.status_code, r.headers, r.json)
        if res.kind == "auth" and attempt == 1:
            token = refresh_token()
            if not token:
                return FetchResult("auth")
            continue
        return res
    return FetchResult("error")


# ----------------------------------------------------------------------------
# Tray icon rendering
# ----------------------------------------------------------------------------
def render_icon(pct, stale: bool):
    """64x64 rounded square with the session % as bold white text."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 63, 63], radius=14, fill=pick_color(pct, stale))

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
# Taskbar-embedded text badge (Windows-only, opt-in via --taskbar)
# ----------------------------------------------------------------------------
# UIA state, lazily initialized; cached so we don't rebuild the COM object or
# retry a broken UIA path every reposition.
_uia = {"auto": None, "walker": None, "uia_mod": None, "failed": False}


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
        _uia.update(auto=auto, walker=auto.ControlViewWalker, uia_mod=UIA)
        return auto, _uia["walker"]
    except Exception:
        _uia["failed"] = True   # don't keep paying the cost of a broken path
        return None


def detect_taskbar_obstacles(taskbar_hwnd, taskbar_left, taskbar_width):
    """Relative-left edges of right-docked taskbar furniture the badge must
    clear — chiefly the Windows 11 Widgets/weather button, which lives in a
    XAML composition island with no HWND, so only UI Automation can see it.

    Returns [] when UIA is unavailable (caller falls back to anchoring on the
    tray alone). Targets right-docked ToggleButtons (the Widgets button) and
    ignores the left-clustered app icons / Start / Task View.
    """
    setup = _uia_setup()
    if not setup:
        return []
    auto, walker = setup
    midpoint = taskbar_width // 2
    lefts = []
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
                    rel_left = r.left - taskbar_left
                    if (cls == "ToggleButton" and rel_left > midpoint
                            and (r.right - r.left) > 0):
                        lefts.append(rel_left)
                except Exception:
                    pass
                walk(child, depth + 1)
                child = walker.GetNextSiblingElement(child)

        walk(root, 0)
    except Exception:
        return []
    return lefts


def compute_badge_x(obstacle_lefts, badge_w: int, margin: int,
                    fallback_left=None) -> int:
    """X for the badge so it sits clear of every right-docked obstacle.

    `obstacle_lefts` are the left edges (relative to the taskbar) of things
    the badge must not cover — the tray, the hidden-icons chevron, and on
    Windows 11 the right-docked Widgets/weather button. The badge anchors to
    the leftmost of them so it clears them all. Clamped to >= 0.
    `fallback_left` is used when nothing was detected.
    """
    lefts = [l for l in obstacle_lefts if l is not None]
    if not lefts:
        if fallback_left is None:
            return 0
        lefts = [fallback_left]
    return max(min(lefts) - badge_w - margin, 0)


class TaskbarBadge:
    """Borderless tkinter window parented into Shell_TrayWnd.

    Positioned just left of the tray notification area. Color-key
    transparency makes only the text visible. Survives explorer restarts
    (re-embeds) and tray-width changes (repositions). This is the same
    unsupported technique TrafficMonitor uses; a Windows update could
    break it, in which case fall back to tray-icon mode.
    """

    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CHILD = 0x40000000
    WS_POPUP = 0x80000000
    WS_EX_LAYERED = 0x00080000
    LWA_COLORKEY = 0x1
    MARGIN_RIGHT = 10  # px gap between badge and tray area

    def __init__(self, app, debug=False):
        import ctypes
        import tkinter as tk

        self.app = app
        self.debug = debug
        self.u32 = ctypes.windll.user32
        self._declare_prototypes(ctypes)
        self.tk = tk
        self.tooltip = None
        self.taskbar_hwnd = 0
        self._tick_count = 0
        self._obstacle_lefts = []   # cached UIA scan of right-docked furniture
        self._obstacle_age = 999    # force a scan on the first reposition

        # DPI awareness before any window is created, so coords line up
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

        self.root = tk.Tk()
        self.root.overrideredirect(True)
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
        self.embed()

    def log(self, msg):
        if self.debug:
            print(f"[badge] {msg}", flush=True)

    def _declare_prototypes(self, ctypes):
        """Explicit argtypes/restypes for every Win32 call we make.

        Without these, ctypes assumes C int everywhere: HWNDs get truncated
        on 64-bit Python, and 32-bit style masks raise OverflowError.
        """
        from ctypes import wintypes
        u = self.u32
        u.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        u.FindWindowW.restype = wintypes.HWND
        u.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND,
                                    wintypes.LPCWSTR, wintypes.LPCWSTR)
        u.FindWindowExW.restype = wintypes.HWND
        u.GetParent.argtypes = (wintypes.HWND,)
        u.GetParent.restype = wintypes.HWND
        u.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
        u.SetParent.restype = wintypes.HWND
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
        u.GetWindowRect.argtypes = (wintypes.HWND,
                                    ctypes.POINTER(wintypes.RECT))
        u.GetWindowRect.restype = wintypes.BOOL
        u.IsWindow.argtypes = (wintypes.HWND,)
        u.IsWindow.restype = wintypes.BOOL

    # -- win32 plumbing -------------------------------------------------------
    def _rect(self, hwnd):
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if not self.u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return r

    def embed(self):
        """(Re)parent our window into the taskbar and apply transparency."""
        self.taskbar_hwnd = self.u32.FindWindowW("Shell_TrayWnd", None)
        if not self.taskbar_hwnd:
            raise RuntimeError("Shell_TrayWnd not found (no taskbar?)")

        style = self.u32.GetWindowLongW(self.hwnd, self.GWL_STYLE) & 0xFFFFFFFF
        style = ((style & ~self.WS_POPUP) | self.WS_CHILD) & 0xFFFFFFFF
        self.u32.SetWindowLongW(self.hwnd, self.GWL_STYLE, _signed32(style))
        prev = self.u32.SetParent(self.hwnd, self.taskbar_hwnd)
        self.log(f"SetParent -> prev parent {prev}")

        ex = self.u32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE) & 0xFFFFFFFF
        self.u32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE,
                                _signed32(ex | self.WS_EX_LAYERED))
        self.u32.SetLayeredWindowAttributes(self.hwnd, KEY_COLORREF, 255,
                                            self.LWA_COLORKEY)
        self.log(f"embedded hwnd={self.hwnd:#x} into taskbar={self.taskbar_hwnd:#x}")
        self.reposition()

    def reposition(self):
        tb = self._rect(self.taskbar_hwnd)
        if tb is None:
            return
        tb_w, tb_h = tb.right - tb.left, tb.bottom - tb.top

        # Tray notification area (left edge), else a 250px fallback.
        tray = self.u32.FindWindowExW(self.taskbar_hwnd, 0, "TrayNotifyWnd", None)
        tr = self._rect(tray) if tray else None
        tray_left = (tr.left - tb.left) if tr else (tb_w - 250)

        # Re-scan UIA for right-docked obstacles (the Win11 Widgets/weather
        # button) only every ~10th reposition — it's relatively expensive and
        # the button rarely moves. The tray anchor still updates every cycle.
        self._obstacle_age += 1
        if self._obstacle_age >= 10:
            self._obstacle_age = 0
            self._obstacle_lefts = detect_taskbar_obstacles(
                self.taskbar_hwnd, tb.left, tb_w)

        self.root.update_idletasks()
        w = self.label.winfo_reqwidth()
        h = self.label.winfo_reqheight()
        x = compute_badge_x([tray_left] + self._obstacle_lefts, w,
                            self.MARGIN_RIGHT)
        y = max((tb_h - h) // 2, 0)
        self.u32.MoveWindow(self.hwnd, x, y, w, h, True)
        self.log(f"taskbar {tb_w}x{tb_h}, tray-left={tray_left}, "
                 f"obstacles={self._obstacle_lefts}, badge at ({x},{y}) {w}x{h}")

    # -- interactions ---------------------------------------------------------
    def on_click(self, _event):
        threading.Thread(target=self.app.poll_once, daemon=True).start()

    def show_tooltip(self, _event):
        self.hide_tooltip(None)
        tip = self.tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        lbl = self.tk.Label(tip,
                            text=build_tooltip(self.app.usage, self.app.stale,
                                               self.app.retry_at()),
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
                          fg=pick_text_color(pct, self.app.stale))

        # Every ~3s: verify the taskbar still exists (explorer restart) and
        # track tray-width changes
        self._tick_count += 1
        if self._tick_count % 4 == 0:
            try:
                if not self.u32.IsWindow(self.taskbar_hwnd):
                    self.log("taskbar gone; re-embedding")
                    self.embed()
                else:
                    self.reposition()
            except Exception as e:
                self.log(f"re-embed failed: {e}")

        self.root.after(750, self.tick)

    def run(self):
        self.root.after(100, self.tick)
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
        self.last_result = FetchResult("error")

    # -- polling --------------------------------------------------------------
    def poll_once(self):
        """Fetch once and fold the result into UI state. Returns the result.

        While inside a server-mandated 429 cooldown we DON'T hit the endpoint
        (a manual 'Refresh' otherwise just refreshes the rolling limit and
        keeps it from draining)."""
        if self.rate_limited_until and time.time() < self.rate_limited_until:
            self.update_icon()
            return self.last_result
        res = fetch_usage()
        self.last_result = res
        if res.kind == "ok":
            self.usage = res.usage
            self.stale = False
            self.rate_limited_until = None
            self.check_thresholds()
        elif res.kind == "rate_limited":
            self.stale = True          # keep last-known numbers, gray them out
            self.rate_limited_until = time.time() + (res.retry_after or POLL_SECONDS)
        else:                          # auth / error
            self.stale = True
        self.update_icon()
        return res

    def poll_loop(self):
        while not self.stop.is_set():
            try:
                res = self.poll_once()
                wait = compute_next_wait(res, POLL_SECONDS)
            except Exception:
                self.stale = True
                self.update_icon()
                wait = POLL_SECONDS
            self.stop.wait(wait)

    # -- notifications ----------------------------------------------------------
    def check_thresholds(self):
        fh = self.usage.get("five_hour")
        if not fh:
            return
        key = fh["resets_at"].isoformat() if fh["resets_at"] else None
        if key != self.window_key:     # new 5h window -> re-arm toasts
            self.window_key = key
            self.notified.clear()
        for t in NOTIFY_THRESHOLDS:
            if fh["utilization"] >= t and t not in self.notified:
                self.notified.add(t)
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

    def update_icon(self):
        if not self.icon:
            return
        fh = self.usage.get("five_hour")
        pct = fh["utilization"] if fh else None
        self.icon.icon = render_icon(pct, self.stale)
        self.icon.title = build_tooltip(self.usage, self.stale, self.retry_at())

    def toggle_startup(self):
        set_startup(not is_startup_enabled())

    def toggle_taskbar(self):
        self.config["taskbar"] = not self.config.get("taskbar", True)
        save_config(self.config)

    def build_icon(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        items = [
            Item("Refresh now", lambda: threading.Thread(
                target=self.poll_once, daemon=True).start()),
            Item("Open usage page", lambda: webbrowser.open(USAGE_PAGE)),
        ]
        if sys.platform == "win32":
            items += [
                Menu.SEPARATOR,
                Item("Run at startup", self.toggle_startup,
                     checked=lambda item: is_startup_enabled()),
                Item("Show on taskbar (applies on restart)",
                     self.toggle_taskbar,
                     checked=lambda item: bool(self.config.get("taskbar", True))),
            ]
        items += [Menu.SEPARATOR, Item("Quit", self.quit)]

        self.icon = pystray.Icon(
            "claude_usage",
            icon=render_icon(None, True),
            title="Claude usage: starting...",
            menu=Menu(*items),
        )

    def run(self, taskbar=False, debug=False):
        self.build_icon()
        threading.Thread(target=self.poll_loop, daemon=True).start()

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


def main():
    ap = argparse.ArgumentParser(description="Claude usage taskbar widget")
    ap.add_argument("--taskbar", action="store_true",
                    help="force taskbar text mode on (and persist it to config)")
    ap.add_argument("--debug", action="store_true",
                    help="print taskbar-embed diagnostics (run with python, not pythonw)")
    args = ap.parse_args()

    if not os.path.exists(CRED_PATH):
        _warn(f"Credentials not found at {CRED_PATH}.\n"
              "Authenticate Claude Code first, then relaunch.\n")
        sys.exit(1)

    # Only one instance may poll; a second would double the request rate into
    # the rate-limited usage endpoint. Hold the handle for the process life.
    global _instance_handle
    _instance_handle = acquire_single_instance()
    if _instance_handle is None:
        _warn("ClaudeUsage is already running.\n")
        sys.exit(0)

    config = load_config()
    if args.taskbar:
        config["taskbar"] = True
        save_config(config)
    # Taskbar mode defaults ON; embed failures fall back to tray-only anyway.
    UsageTray(config).run(taskbar=config.get("taskbar", True), debug=args.debug)


if __name__ == "__main__":
    main()
