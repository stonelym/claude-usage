"""Unit tests for claude_usage_tray pure logic (no display, no network).

Run:  python -m unittest -v test_claude_usage
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import claude_usage_tray as cu


# ---------------------------------------------------------------------------
# Bug 1: 429 rate-limit handling
# ---------------------------------------------------------------------------
class ParseRetryAfter(unittest.TestCase):
    def test_integer_seconds(self):
        self.assertEqual(cu.parse_retry_after({"Retry-After": "2787"}), 2787)

    def test_missing_header_uses_default(self):
        self.assertEqual(cu.parse_retry_after({}, default=300), 300)

    def test_non_integer_uses_default(self):
        # HTTP-date form isn't emitted by this API; fall back rather than crash.
        self.assertEqual(
            cu.parse_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                                 default=300),
            300,
        )

    def test_negative_clamped_to_zero(self):
        self.assertEqual(cu.parse_retry_after({"Retry-After": "-5"}), 0)


class ClassifyUsageResponse(unittest.TestCase):
    def test_200_returns_ok_with_parsed_usage(self):
        payload = {"five_hour": {"utilization": 8, "resets_at": None}}
        res = cu.classify_usage_response(200, {}, lambda: payload)
        self.assertEqual(res.kind, "ok")
        self.assertIn("five_hour", res.usage)
        self.assertEqual(res.usage["five_hour"]["utilization"], 8.0)

    def test_200_with_bad_body_is_error(self):
        def boom():
            raise ValueError("not json")
        res = cu.classify_usage_response(200, {}, boom)
        self.assertEqual(res.kind, "error")

    def test_401_returns_auth(self):
        res = cu.classify_usage_response(401, {}, lambda: {})
        self.assertEqual(res.kind, "auth")

    def test_429_returns_rate_limited_with_retry_after(self):
        res = cu.classify_usage_response(
            429, {"Retry-After": "2787"}, lambda: None)
        self.assertEqual(res.kind, "rate_limited")
        self.assertEqual(res.retry_after, 2787)

    def test_500_returns_error(self):
        res = cu.classify_usage_response(500, {}, lambda: None)
        self.assertEqual(res.kind, "error")


class ComputeNextWait(unittest.TestCase):
    def test_rate_limited_waits_at_least_retry_after(self):
        res = cu.FetchResult("rate_limited", retry_after=2787)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300), 2787)

    def test_rate_limited_never_shorter_than_base(self):
        res = cu.FetchResult("rate_limited", retry_after=10)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300), 300)

    def test_ok_uses_base_poll(self):
        res = cu.FetchResult("ok", usage={})
        self.assertEqual(cu.compute_next_wait(res, base_poll=300), 300)

    def test_streak_zero_or_one_is_base(self):
        # First 429 (streak 1) still backs off only to the base interval.
        res = cu.FetchResult("rate_limited", retry_after=None)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300, streak=0), 300)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300, streak=1), 300)

    def test_streak_doubles_each_consecutive_429(self):
        res = cu.FetchResult("rate_limited", retry_after=None)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300, streak=2), 600)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300, streak=3), 1200)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300, streak=4), 2400)

    def test_backoff_is_capped(self):
        res = cu.FetchResult("rate_limited", retry_after=None)
        # 300 * 2**9 would be 153600; the cap holds it at 3600.
        self.assertEqual(
            cu.compute_next_wait(res, base_poll=300, streak=10, cap=3600), 3600)

    def test_server_retry_after_overrides_backoff(self):
        # A Retry-After longer than the computed backoff still wins.
        res = cu.FetchResult("rate_limited", retry_after=5000)
        self.assertEqual(cu.compute_next_wait(res, base_poll=300, streak=2), 5000)

    def test_non_rate_limited_ignores_streak(self):
        for kind in ("ok", "auth", "error"):
            res = cu.FetchResult(kind, usage={})
            self.assertEqual(
                cu.compute_next_wait(res, base_poll=300, streak=9), 300)


class TooltipRateLimited(unittest.TestCase):
    def test_no_data_but_rate_limited_explains_why(self):
        from datetime import datetime
        retry = datetime(2026, 6, 13, 10, 47).astimezone()
        tip = cu.build_tooltip({}, stale=True, retry_at=retry)
        self.assertIn("ate-limit", tip)   # "Rate-limited"
        self.assertIn("10:47", tip)

    def test_rate_limit_note_appended_to_existing_data(self):
        from datetime import datetime
        retry = datetime(2026, 6, 13, 10, 47).astimezone()
        usage = {"five_hour": {"utilization": 8.0, "resets_at": None}}
        tip = cu.build_tooltip(usage, stale=True, retry_at=retry)
        self.assertIn("Session 8%", tip)
        self.assertIn("ate-limit", tip)

    def test_signed_out_says_so_instead_of_stale(self):
        usage = {"five_hour": {"utilization": 8.0, "resets_at": None}}
        tip = cu.build_tooltip(usage, stale=True, signed_out=True)
        self.assertIn("Signed out", tip)
        self.assertNotIn("STALE", tip)


# ---------------------------------------------------------------------------
# Bug 2: collision-aware taskbar badge positioning
# ---------------------------------------------------------------------------
class IsFullscreen(unittest.TestCase):
    MON = (0, 0, 2560, 1440)

    def test_exact_cover_is_fullscreen(self):
        self.assertTrue(cu.is_fullscreen((0, 0, 2560, 1440), self.MON))

    def test_smaller_window_is_not(self):
        self.assertFalse(cu.is_fullscreen((100, 100, 800, 600), self.MON))

    def test_partial_height_is_not(self):
        # leaves the taskbar band uncovered
        self.assertFalse(cu.is_fullscreen((0, 0, 2560, 1400), self.MON))

    def test_window_spilling_past_monitor_is_fullscreen(self):
        # borderless fullscreen often sits a few px outside the monitor
        self.assertTrue(cu.is_fullscreen((-8, -8, 2568, 1448), self.MON))

    def test_fullscreen_on_secondary_monitor(self):
        mon = (2560, 0, 5120, 1440)
        self.assertTrue(cu.is_fullscreen((2560, 0, 5120, 1440), mon))

    def test_desktop_is_not_fullscreen(self):
        # A desktop click / Win+D makes Progman foreground; it covers the
        # monitor exactly, and used to hide the badge.
        self.assertFalse(cu.is_fullscreen(self.MON, self.MON, "Progman"))
        self.assertFalse(cu.is_fullscreen(self.MON, self.MON, "WorkerW"))


class SelectTaskbar(unittest.TestCase):
    PRIMARY = {"hwnd": 1, "device": r"\\.\DISPLAY1", "is_primary": True}
    SECONDARY = {"hwnd": 2, "device": r"\\.\DISPLAY2", "is_primary": False}

    def test_matches_wanted_device(self):
        bars = [self.PRIMARY, self.SECONDARY]
        self.assertIs(cu.select_taskbar(bars, r"\\.\DISPLAY2"), self.SECONDARY)

    def test_falls_back_to_primary_when_wanted_absent(self):
        # Saved display was disconnected/renamed -> don't vanish, use primary.
        bars = [self.PRIMARY, self.SECONDARY]
        self.assertIs(cu.select_taskbar(bars, r"\\.\DISPLAY9"), self.PRIMARY)

    def test_no_wanted_uses_primary(self):
        bars = [self.SECONDARY, self.PRIMARY]
        self.assertIs(cu.select_taskbar(bars, None), self.PRIMARY)

    def test_falls_back_to_first_when_no_primary(self):
        bars = [self.SECONDARY]
        self.assertIs(cu.select_taskbar(bars, None), self.SECONDARY)

    def test_none_when_no_taskbars(self):
        self.assertIsNone(cu.select_taskbar([], r"\\.\DISPLAY1"))


class DisplaySetter(unittest.TestCase):
    """Regression guard for the menu-callback arity bug: pystray keys off
    co_argcount (counting default params), so a `lambda dev=dev:` is misread
    as 1-arg and the icon is passed in as the device. The setter must be a
    true 0-arg callable, and calling it must persist the device."""

    def _app(self):
        return cu.UsageTray(config={})

    def test_setter_is_zero_arg(self):
        cb = self._app()._make_display_setter(r"\\.\DISPLAY2")
        self.assertEqual(cb.__code__.co_argcount, 0)

    def test_calling_setter_persists_device(self):
        orig = cu.save_config
        cu.save_config = lambda cfg: None        # no disk write
        try:
            app = self._app()
            app._make_display_setter(r"\\.\DISPLAY2")()
            self.assertEqual(app.config["display"], r"\\.\DISPLAY2")
            app._make_display_setter(None)()      # Auto clears it
            self.assertIsNone(app.config["display"])
        finally:
            cu.save_config = orig


class ComputeBadgeX(unittest.TestCase):
    def test_anchors_left_of_single_obstacle(self):
        # tray at left 2345, badge 97 wide, 10px margin
        self.assertEqual(
            cu.compute_badge_x([2345], badge_w=97, margin=10, floor=0),
            2345 - 97 - 10)

    def test_dodges_widget_left_of_tray(self):
        # Regression for the reported bug: a right-docked Widgets button
        # (left 2181) sits left of the tray (left 2345). The badge must clear
        # the WIDGET, not just the tray.
        x = cu.compute_badge_x([2345, 2181], badge_w=97, margin=10, floor=0)
        self.assertEqual(x, 2181 - 97 - 10)
        self.assertLess(x + 97, 2181)  # badge ends before the widget begins

    def test_no_room_beside_app_buttons_is_none(self):
        # Live numbers from a crowded 1280px taskbar: app buttons end at 879,
        # tray starts at 988, badge is 251 wide. It would cover three buttons.
        self.assertIsNone(
            cu.compute_badge_x([988], badge_w=251, margin=10, floor=879 + 10))

    def test_fits_right_after_app_buttons(self):
        self.assertEqual(
            cu.compute_badge_x([988], badge_w=80, margin=10, floor=889), 898)

    def test_monitor_left_of_primary_keeps_negative_x(self):
        # Secondary taskbar spans x -1920..0; its fallback anchor is -250.
        # Clamping to 0 used to throw the badge onto the primary monitor.
        self.assertEqual(
            cu.compute_badge_x([-250], badge_w=300, margin=10, floor=-1920),
            -560)


class SafeWarn(unittest.TestCase):
    def test_warn_does_not_raise_when_stderr_is_none(self):
        # --noconsole frozen builds set sys.stderr to None; writing to it
        # raises AttributeError, which PyInstaller turns into a modal dialog
        # that hangs the process. _warn must swallow that.
        import sys
        saved = sys.stderr
        sys.stderr = None
        try:
            cu._warn("anything")   # must not raise
        finally:
            sys.stderr = saved


class SingleInstance(unittest.TestCase):
    def test_first_acquisition_succeeds(self):
        h = cu.acquire_single_instance("ClaudeUsageUnitTestA")
        self.assertTrue(h)

    def test_second_acquisition_while_held_is_blocked(self):
        # Same process still owns the named mutex from the first call, so a
        # second instance must be told it's already running.
        first = cu.acquire_single_instance("ClaudeUsageUnitTestB")
        self.assertTrue(first)
        second = cu.acquire_single_instance("ClaudeUsageUnitTestB")
        self.assertIsNone(second)


# ---------------------------------------------------------------------------
# Self-update: pure helpers
# ---------------------------------------------------------------------------
class _Key(dict):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Just enough winreg for the startup toggle; never touches the real
    registry (a real Run entry would launch at the next sign-in)."""
    HKEY_CURRENT_USER = KEY_SET_VALUE = REG_SZ = 0

    def __init__(self, keys):
        self.keys = {path: _Key(values) for path, values in keys.items()}

    def OpenKey(self, _root, path, *_):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self.keys[path]

    def QueryValueEx(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], 0

    def SetValueEx(self, key, name, _reserved, _type, value):
        key[name] = value

    def DeleteValue(self, key, name):
        if key.pop(name, None) is None:
            raise FileNotFoundError(name)


class StartupToggle(unittest.TestCase):
    """Task Manager's Startup tab keeps its own off switch beside our Run
    value; the tray checkmark must honor it and turning on must clear it."""

    def use_registry(self, approved):
        fake = FakeWinreg({cu.RUN_KEY: {cu.APP_NAME: '"x.exe"'},
                           cu.APPROVED_KEY: approved})
        p = mock.patch.dict(sys.modules, {"winreg": fake})
        p.start()
        self.addCleanup(p.stop)

    def test_switched_off_in_task_manager_reads_as_off(self):
        self.use_registry({cu.APP_NAME: b"\x03" + bytes(11)})
        self.assertFalse(cu.is_startup_enabled())

    def test_never_touched_in_task_manager_reads_as_on(self):
        self.use_registry({})
        self.assertTrue(cu.is_startup_enabled())

    def test_turning_on_clears_task_manager_off_switch(self):
        self.use_registry({cu.APP_NAME: b"\x03" + bytes(11)})
        cu.set_startup(True)
        self.assertTrue(cu.is_startup_enabled())


class ParseVersion(unittest.TestCase):
    def test_strips_leading_v(self):
        self.assertEqual(cu.parse_version("v1.2.0"), (1, 2, 0))

    def test_plain_dotted(self):
        self.assertEqual(cu.parse_version("1.2"), (1, 2))

    def test_ignores_prerelease_suffix(self):
        self.assertEqual(cu.parse_version("v1.2.3-beta"), (1, 2, 3))

    def test_garbage_is_empty(self):
        self.assertEqual(cu.parse_version("garbage"), ())


class IsNewerVersion(unittest.TestCase):
    def test_higher_patch_is_newer(self):
        self.assertTrue(cu.is_newer_version("1.0.1", "1.0.0"))

    def test_equal_is_not_newer(self):
        self.assertFalse(cu.is_newer_version("1.0.0", "1.0.0"))

    def test_short_equals_zero_padded(self):
        # 1.2 and 1.2.0 are the same version
        self.assertFalse(cu.is_newer_version("1.2", "1.2.0"))

    def test_older_is_not_newer(self):
        self.assertFalse(cu.is_newer_version("1.0.0", "1.1.0"))

    def test_v_prefixed_remote(self):
        self.assertTrue(cu.is_newer_version("v2.0.0", "1.9.9"))


class SelectReleaseAssets(unittest.TestCase):
    def _release(self, names):
        return {"tag_name": "v1.2.0",
                "assets": [{"name": n,
                            "browser_download_url": f"https://x/{n}"} for n in names]}

    def test_returns_info_when_both_assets_present(self):
        rel = self._release(["ClaudeUsage.exe", "ClaudeUsage.exe.sha256", "extra.txt"])
        info = cu.select_release_assets(rel, "ClaudeUsage.exe", "ClaudeUsage.exe.sha256")
        self.assertIsNotNone(info)
        self.assertEqual(info.tag, "v1.2.0")
        self.assertEqual(info.exe_url, "https://x/ClaudeUsage.exe")
        self.assertEqual(info.sha_url, "https://x/ClaudeUsage.exe.sha256")

    def test_none_when_exe_missing(self):
        rel = self._release(["ClaudeUsage.exe.sha256"])
        self.assertIsNone(
            cu.select_release_assets(rel, "ClaudeUsage.exe", "ClaudeUsage.exe.sha256"))

    def test_none_when_sha_missing(self):
        rel = self._release(["ClaudeUsage.exe"])
        self.assertIsNone(
            cu.select_release_assets(rel, "ClaudeUsage.exe", "ClaudeUsage.exe.sha256"))

    def test_none_on_empty_payload(self):
        self.assertIsNone(
            cu.select_release_assets({}, "ClaudeUsage.exe", "ClaudeUsage.exe.sha256"))


class ParseSha256Sidecar(unittest.TestCase):
    HEX = "a" * 64

    def test_bare_hex(self):
        self.assertEqual(cu.parse_sha256_sidecar(self.HEX), self.HEX)

    def test_sha256sum_format(self):
        self.assertEqual(
            cu.parse_sha256_sidecar(f"{self.HEX}  ClaudeUsage.exe"), self.HEX)

    def test_uppercase_is_lowercased(self):
        self.assertEqual(cu.parse_sha256_sidecar("A" * 64), self.HEX)

    def test_garbage_is_none(self):
        self.assertIsNone(cu.parse_sha256_sidecar("not a hash"))

    def test_wrong_length_is_none(self):
        self.assertIsNone(cu.parse_sha256_sidecar("abc123"))


class VerifySha256(unittest.TestCase):
    def _tmp(self, data: bytes):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(lambda: os.remove(path))
        return path

    def test_matching_hash(self):
        data = b"hello world"
        path = self._tmp(data)
        self.assertTrue(cu.verify_sha256(path, hashlib.sha256(data).hexdigest()))

    def test_mismatched_hash(self):
        path = self._tmp(b"hello world")
        self.assertFalse(cu.verify_sha256(path, "0" * 64))

    def test_case_insensitive(self):
        data = b"abc"
        path = self._tmp(data)
        self.assertTrue(
            cu.verify_sha256(path, hashlib.sha256(data).hexdigest().upper()))


class ShouldCheckForUpdate(unittest.TestCase):
    def test_true_when_interval_elapsed(self):
        self.assertTrue(cu.should_check_for_update(now=1000, last_check=0, interval_s=600))

    def test_false_when_recent(self):
        self.assertFalse(cu.should_check_for_update(now=1000, last_check=900, interval_s=600))

    def test_true_when_never_checked(self):
        self.assertTrue(cu.should_check_for_update(now=1000, last_check=None, interval_s=600))


class CheckForUpdate(unittest.TestCase):
    def test_failed_fetch_is_retried_not_pushed_a_day_out(self):
        app = cu.UsageTray(config={})
        with mock.patch.object(cu, "fetch_latest_release", lambda: None), \
                mock.patch.object(cu, "save_config", lambda cfg: None):
            app.check_for_update()            # offline at login
        self.assertTrue(cu.should_check_for_update(
            time.time(), app.last_update_check, cu.UPDATE_INTERVAL))

    def manual_check(self, release):
        app = cu.UsageTray(config={})
        app.icon = mock.Mock()
        with mock.patch.object(cu, "fetch_latest_release", lambda: release), \
                mock.patch.object(cu, "save_config", lambda cfg: None):
            app.check_for_update(manual=True)
        return app.icon.notify.call_args.args[0]

    def test_manual_check_answers_when_up_to_date(self):
        same = cu.UpdateInfo(f"v{cu.VERSION}", "exe-url", "sha-url")
        self.assertIn("latest version", self.manual_check(same))

    def test_manual_check_answers_when_offline(self):
        self.assertIn("Couldn't reach GitHub", self.manual_check(None))


# ---------------------------------------------------------------------------
# Self-update: the exe swap (network faked, real file moves in a temp dir)
# ---------------------------------------------------------------------------
class ApplyUpdate(unittest.TestCase):
    """The swap is the one step that can leave the install with no exe."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.exe = os.path.join(self.dir, "ClaudeUsage.exe")
        with open(self.exe, "wb") as f:
            f.write(b"OLD")

        def download(url, dest):
            with open(dest, "wb") as f:
                f.write(b"NEW")
            return True
        for target, attr, value in ((cu.sys, "frozen", True),
                                    (cu.sys, "executable", self.exe),
                                    (cu, "download_to", download)):
            p = mock.patch.object(target, attr, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        self.app = cu.UsageTray(config={})
        self.app._pending = cu.UpdateInfo("v9.9.9", "exe-url", "sha-url")
        self.app._fetch_sidecar_hash = lambda url: hashlib.sha256(b"NEW").hexdigest()
        self.app._relaunch = mock.Mock()
        self.app.quit = mock.Mock()

    def installed(self):
        with open(self.exe, "rb") as f:
            return f.read()

    def test_swaps_in_verified_build_and_relaunches(self):
        self.app.apply_update()
        self.assertEqual(self.installed(), b"NEW")
        self.app._relaunch.assert_called_once()

    def test_bad_hash_leaves_install_untouched(self):
        self.app._fetch_sidecar_hash = lambda url: "0" * 64
        self.app.apply_update()
        self.assertEqual(self.installed(), b"OLD")
        self.app._relaunch.assert_not_called()

    def test_failed_install_rolls_back(self):
        real = self.app._move_retry
        self.app._move_retry = lambda src, dst, replace: (
            False if src.endswith(".new.exe") else real(src, dst, replace, tries=1))
        self.app.apply_update()
        self.assertEqual(self.installed(), b"OLD")
        self.app._relaunch.assert_not_called()

    def test_new_exe_that_wont_start_is_rolled_back_and_app_stays_up(self):
        # e.g. antivirus blocks the fresh unsigned exe at launch
        self.app._relaunch = mock.Mock(return_value=False)
        self.app.apply_update()
        self.assertEqual(self.installed(), b"OLD")
        self.app.quit.assert_not_called()

    def test_second_click_while_updating_is_ignored(self):
        # Two downloads into the same files report a false failure.
        self.app._update_lock.acquire()          # first click still downloading
        self.app.apply_update = mock.Mock()
        self.app.start_update()
        self.app.apply_update.assert_not_called()


class Relaunch(unittest.TestCase):
    def test_new_exe_unpacks_fresh_instead_of_borrowing_our_temp_dir(self):
        # Without the reset, the relaunched --onefile exe runs from the old
        # process's _MEI dir, whose launcher then hangs on a cleanup dialog.
        with mock.patch("subprocess.Popen") as popen:
            self.assertTrue(cu.UsageTray(config={})._relaunch("x.exe", "."))
        self.assertEqual(popen.call_args.kwargs["env"]
                         ["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_launch_failure_is_reported(self):
        with mock.patch("subprocess.Popen", side_effect=OSError(225, "blocked")):
            self.assertFalse(cu.UsageTray(config={})._relaunch("x.exe", "."))


# ---------------------------------------------------------------------------
# Poll state machine (fetch_usage and the clock faked; no network)
# ---------------------------------------------------------------------------
class PollOnce(unittest.TestCase):
    OK = cu.FetchResult("ok", usage={})
    LIMITED = cu.FetchResult("rate_limited")

    def setUp(self):
        self.now = 1_790_000_000.0
        self.replies, self.fetches = [], 0

        def fake_fetch(log=None):
            self.fetches += 1
            return self.replies.pop(0)
        for target, attr, value in ((cu, "fetch_usage", fake_fetch),
                                    (cu.time, "time", lambda: self.now)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.app = cu.UsageTray(config={})

    def poll(self, reply=None, after=0):
        self.now += after
        if reply:
            self.replies.append(reply)
        return self.app.poll_once()

    def test_429s_back_off_then_ok_resets(self):
        self.poll(self.LIMITED)
        self.assertEqual(self.app.rate_limited_until - self.now, 300)
        self.poll(self.LIMITED, after=300)
        self.assertEqual(self.app.rate_limited_until - self.now, 600)
        self.poll(self.OK, after=600)
        self.assertEqual((self.app.rate_limit_streak, self.app.rate_limited_until,
                          self.app.stale), (0, None, False))

    def test_no_fetch_inside_cooldown(self):
        self.poll(self.LIMITED)
        self.poll(after=120)              # past the click gap, still cooling down
        self.assertEqual(self.fetches, 1)

    def test_rapid_clicks_fetch_once(self):
        self.poll(self.OK)
        for _ in range(4):
            self.poll(after=5)
        self.assertEqual(self.fetches, 1)

    def test_fetch_in_flight_is_not_doubled(self):
        self.app._poll_lock.acquire()     # another thread is mid-fetch
        self.poll(self.OK)
        self.assertEqual(self.fetches, 0)

    def test_loop_sleeps_out_remaining_cooldown(self):
        # A manual refresh set the cooldown mid-interval: the loop must wake
        # when it ends, not restart a full backoff from now.
        self.app.rate_limited_until = self.now + 100
        waits = []
        self.app.stop = mock.Mock(is_set=lambda: bool(waits), wait=waits.append)
        self.app.poll_loop()
        self.assertEqual(waits, [100])

    def test_signed_out_rechecks_soon_and_never_blocks_a_refresh(self):
        # Right after Claude Code signs back in, a click must fetch.
        self.poll(cu.FetchResult("signed_out"))
        self.poll(self.OK, after=5)
        self.assertEqual((self.fetches, self.app.stale), (2, False))
        self.app.last_result = cu.FetchResult("signed_out")
        waits = []
        self.app.stop = mock.Mock(is_set=lambda: bool(waits), wait=waits.append)
        self.app.poll_once = lambda: None
        self.app.poll_loop()
        self.assertEqual(waits, [cu.SIGNIN_RECHECK])


class ThresholdToasts(unittest.TestCase):
    def test_once_per_window_despite_changing_microseconds(self):
        # The live API returns resets_at with per-request microseconds.
        app = cu.UsageTray(config={})
        app.icon = mock.Mock()
        for stamp in ("06:20:00.253081", "06:20:00.439678", "11:20:00.120000"):
            app.usage = cu.parse_usage({"five_hour": {
                "utilization": 85, "resets_at": f"2026-10-17T{stamp}+00:00"}})
            app.check_thresholds()
        self.assertEqual(app.icon.notify.call_count, 2)   # 06:20 window, 11:20 window

    def test_first_poll_past_both_thresholds_toasts_once(self):
        app = cu.UsageTray(config={})
        app.icon = mock.Mock()
        app.usage = cu.parse_usage({"five_hour": {"utilization": 96,
                                                  "resets_at": None}})
        app.check_thresholds()
        app.check_thresholds()
        self.assertEqual(app.icon.notify.call_count, 1)


class FetchUsage(unittest.TestCase):
    """The widget reads Claude Code's token but never renews it: a second
    refresher can win a race and make Claude Code wipe the login."""

    def fetch(self, oauth, status=200):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, ".credentials.json")
        with open(path, "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)
        reply = mock.Mock(status_code=status, headers={}, json=lambda: {
            "five_hour": {"utilization": 8, "resets_at": None}})
        with mock.patch.object(cu, "CRED_PATH", path), \
                mock.patch.object(cu.requests, "get", return_value=reply) as get, \
                mock.patch.object(cu.requests, "post") as post:
            res = cu.fetch_usage()
        post.assert_not_called()                  # never touches the token endpoint
        return res, get.call_count

    def test_valid_token_reads_usage(self):
        res, gets = self.fetch({"accessToken": "a1",
                                "expiresAt": (time.time() + 3600) * 1000})
        self.assertEqual((res.kind, gets), ("ok", 1))

    def test_401_is_auth_without_renewing(self):
        res, gets = self.fetch({"accessToken": "a1", "refreshToken": "r1"}, 401)
        self.assertEqual((res.kind, gets), ("auth", 1))

    def test_expired_token_skips_the_request(self):
        # Spares the rate-limited endpoint a guaranteed 401.
        res, gets = self.fetch({"accessToken": "a1",
                                "expiresAt": (time.time() - 60) * 1000})
        self.assertEqual((res.kind, gets), ("signed_out", 0))

    def test_token_wiped_by_claude_code_is_signed_out(self):
        res, gets = self.fetch({"accessToken": "", "expiresAt": 0})
        self.assertEqual((res.kind, gets), ("signed_out", 0))


if __name__ == "__main__":
    unittest.main()
