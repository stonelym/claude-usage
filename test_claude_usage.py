"""Unit tests for claude_usage_tray pure logic (no display, no network).

Run:  python -m unittest -v test_claude_usage
"""
import unittest

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


# ---------------------------------------------------------------------------
# Bug 2: collision-aware taskbar badge positioning
# ---------------------------------------------------------------------------
class ShouldMove(unittest.TestCase):
    def test_no_move_when_geometry_unchanged(self):
        self.assertFalse(cu.should_move((100, 10, 97, 23), (100, 10, 97, 23)))

    def test_move_when_changed(self):
        self.assertTrue(cu.should_move((100, 10, 97, 23), (90, 10, 97, 23)))

    def test_move_on_first_call_no_prev(self):
        self.assertTrue(cu.should_move(None, (100, 10, 97, 23)))


class ComputeBadgeX(unittest.TestCase):
    def test_anchors_left_of_single_obstacle(self):
        # tray at relative-left 2345, badge 97 wide, 10px margin
        self.assertEqual(
            cu.compute_badge_x([2345], badge_w=97, margin=10), 2345 - 97 - 10)

    def test_dodges_widget_left_of_tray(self):
        # Regression for the reported bug: a right-docked Widgets button
        # (rel-left 2181) sits left of the tray (rel-left 2345). The badge
        # must clear the WIDGET, not just the tray.
        x = cu.compute_badge_x([2345, 2181], badge_w=97, margin=10)
        self.assertEqual(x, 2181 - 97 - 10)
        self.assertLess(x + 97, 2181)  # badge ends before the widget begins

    def test_clamps_to_zero(self):
        self.assertEqual(cu.compute_badge_x([50], badge_w=97, margin=10), 0)

    def test_empty_obstacles_uses_fallback(self):
        self.assertEqual(
            cu.compute_badge_x([], badge_w=97, margin=10, fallback_left=1000),
            1000 - 97 - 10)


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
        import tempfile, os
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(lambda: os.remove(path))
        return path

    def test_matching_hash(self):
        import hashlib
        data = b"hello world"
        path = self._tmp(data)
        self.assertTrue(cu.verify_sha256(path, hashlib.sha256(data).hexdigest()))

    def test_mismatched_hash(self):
        path = self._tmp(b"hello world")
        self.assertFalse(cu.verify_sha256(path, "0" * 64))

    def test_case_insensitive(self):
        import hashlib
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


if __name__ == "__main__":
    unittest.main()
