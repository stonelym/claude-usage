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


if __name__ == "__main__":
    unittest.main()
