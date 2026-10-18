"""Serial verifier: one warm browser, real requests first, tokens solved before they are needed."""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from qyz_reference.world_boss_browser import BrowserVerificationError, NativeTurnstileBrowser
from qyz_verification import (
    PRESOLVED_MAX_AGE_SECONDS,
    SCREENSHOT_CLIP,
    SHANGHAI,
    PreparedNativeTurnstileBrowser,
    PresolveTurnstileBroker,
    SerialVerificationService,
)

EVENT = "a" * 64
OTHER_EVENT = "b" * 64
ACCOUNTS = ("tg9100000001", "tg9100000002", "tg9100000003")


def create(broker, account, event=EVENT):
    return broker.create_request(account=account, identity="主魂", challenge_id="fixture_" + account,
                                 event_fingerprint=event, message_id=123,
                                 origin="https://asc.aiopenai.app")


def shanghai(hour, minute, second=0):
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=SHANGHAI).timestamp()


class FakeBrowser:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])
        self.closed = 0
        self.prepared = 0
        self.chrome = None

    def prepare(self):
        self.prepared += 1
        self.loaded = True

    def park(self):
        if not getattr(self, "loaded", False):
            return False
        self.loaded = False
        self.parked = getattr(self, "parked", 0) + 1
        return True

    def verify(self, origin=None, *, on_event=None, still_pending=None, timeout=None):
        self.calls.append(origin)
        self.loaded = True
        if on_event:
            on_event("widget_ready")
        result = self.results.pop(0) if self.results else "fixture_token_%d" % len(self.calls)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed += 1


def service_with(browser, *, wall=None):
    now, mono = [1000.0], [50.0]
    wall = wall or [shanghai(20, 0)]
    broker = PresolveTurnstileBroker(clock=lambda: now[0])
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        service = SerialVerificationService(root / "browser-profile", account_order=ACCOUNTS, trusted_root=root,
                                            browser=browser, broker=broker, monotonic=lambda: mono[0],
                                            wall_clock=lambda: wall[0])
    return service, broker, now, mono, wall


class PresolveBrokerTests(unittest.TestCase):
    def test_presolved_token_is_handed_once_to_the_same_account_for_any_event(self):
        now = [1000.0]
        broker = PresolveTurnstileBroker(clock=lambda: now[0])
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        self.assertEqual([ACCOUNTS[0]], broker.presolve_demand())
        self.assertEqual("stored", broker.store_presolved(ACCOUNTS[0], "fixture_presolved"))
        self.assertEqual([], broker.presolve_demand())
        # Another account never receives it; the token is not bound to an event.
        self.assertEqual("pending", create(broker, ACCOUNTS[1])["status"])
        row = create(broker, ACCOUNTS[0], OTHER_EVENT)
        self.assertEqual("submitted", row["status"])
        self.assertTrue(row["presolved"])
        self.assertNotIn("fixture_presolved", repr(row))
        self.assertEqual("fixture_presolved", broker.for_account(ACCOUNTS[0]).take_token(row["request_id"]))
        # A second request (after a rejected /begin) needs a fresh widget.
        self.assertEqual("pending", create(broker, ACCOUNTS[0])["status"])
        self.assertEqual(1, broker.presolve_snapshot()["attached"])

    def test_stale_presolved_token_is_dropped(self):
        now = [1000.0]
        broker = PresolveTurnstileBroker(clock=lambda: now[0])
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        broker.store_presolved(ACCOUNTS[0], "fixture_presolved")
        now[0] += PRESOLVED_MAX_AGE_SECONDS + 1
        self.assertEqual("pending", create(broker, ACCOUNTS[0])["status"])
        self.assertEqual(1, broker.presolve_snapshot()["expired"])

    def test_token_finished_after_the_request_arrived_goes_to_that_request(self):
        broker = PresolveTurnstileBroker()
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        row = create(broker, ACCOUNTS[0])
        self.assertEqual([], broker.presolve_demand())
        self.assertTrue(broker.presolve_useful(ACCOUNTS[0]))
        self.assertEqual("attached", broker.store_presolved(ACCOUNTS[0], "fixture_late"))
        self.assertEqual("fixture_late", broker.take_token(row["request_id"]))

    def test_demand_expires_and_is_not_created_while_a_request_is_live(self):
        now = [1000.0]
        broker = PresolveTurnstileBroker(clock=lambda: now[0])
        create(broker, ACCOUNTS[0])
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        broker.want_presolve(ACCOUNTS[1], valid_for_seconds=30)
        self.assertEqual([ACCOUNTS[1]], broker.presolve_demand())
        now[0] += 31
        self.assertEqual([], broker.presolve_demand())

    def test_a_ready_token_is_not_solved_twice(self):
        broker = PresolveTurnstileBroker()
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        broker.store_presolved(ACCOUNTS[0], "fixture_presolved")
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        self.assertEqual([], broker.presolve_demand())

    def test_cancel_all_forgets_presolved_tokens(self):
        broker = PresolveTurnstileBroker()
        broker.want_presolve(ACCOUNTS[0], valid_for_seconds=90)
        broker.store_presolved(ACCOUNTS[0], "fixture_presolved")
        broker.cancel_all()
        self.assertEqual("pending", create(broker, ACCOUNTS[0])["status"])


class SerialServiceTests(unittest.TestCase):
    def test_presolves_admitted_accounts_in_declared_order(self):
        browser = FakeBrowser()
        service, broker, *_ = service_with(browser)
        service.prepare_event(EVENT, valid_for_seconds=90, account=ACCOUNTS[2])
        service.prepare_event(EVENT, valid_for_seconds=90, account=ACCOUNTS[0])
        self.assertTrue(service._run_once())
        self.assertTrue(service._run_once())
        self.assertFalse(service._run_once())
        self.assertEqual(2, len(browser.calls))
        self.assertTrue(create(broker, ACCOUNTS[0])["presolved"])
        self.assertTrue(create(broker, ACCOUNTS[2])["presolved"])
        self.assertFalse(create(broker, ACCOUNTS[1])["presolved"])

    def test_real_requests_go_before_presolve(self):
        browser = FakeBrowser()
        service, broker, *_ = service_with(browser)
        service.prepare_event(EVENT, valid_for_seconds=90, account=ACCOUNTS[1])
        real = create(broker, ACCOUNTS[0])["request_id"]
        self.assertTrue(service._run_once())
        self.assertEqual("submitted", broker.get_request(real)["status"])
        self.assertEqual([ACCOUNTS[1]], broker.presolve_demand())

    def test_no_presolve_while_a_real_request_waits_for_its_turn(self):
        browser = FakeBrowser()
        service, broker, *_ = service_with(browser)
        service.prepare_event(EVENT, valid_for_seconds=90, account=ACCOUNTS[0])
        # The reference service holds a new event for up to 1 s waiting for the
        # first declared account; the free browser must not start a presolve.
        create(broker, ACCOUNTS[1])
        self.assertFalse(service._run_once())
        self.assertEqual([], browser.calls)

    def test_presolve_failures_are_bounded_and_close_broken_browsers(self):
        browser = FakeBrowser([BrowserVerificationError("turnstile_browser_timeout"),
                               BrowserVerificationError("browser_connection_timeout")])
        service, *_ = service_with(browser)
        service.prepare_event(EVENT, valid_for_seconds=90, account=ACCOUNTS[0])
        self.assertTrue(service._run_once())
        self.assertEqual(0, browser.closed)
        self.assertTrue(service._run_once())
        self.assertEqual(1, browser.closed)
        self.assertFalse(service._run_once())
        self.assertEqual(2, len(browser.calls))

    def test_daily_window_warms_the_page_then_presolves_every_account_before_the_entry(self):
        browser = FakeBrowser()
        service, broker, _now, mono, wall = service_with(browser, wall=[shanghai(13, 38, 29)])
        self.assertFalse(service._run_once())
        self.assertEqual(0, browser.prepared)
        # 2026-09-27: the warm page sat idle until 13:39:50 and one slow first
        # widget then left @demo_player_06 without a token; the presolve follows at once.
        wall[0] = shanghai(13, 38, 30)
        self.assertTrue(service._run_once())
        self.assertEqual((1, 1), (browser.prepared, len(browser.calls)))
        self.assertEqual(0, getattr(browser, "parked", 0))  # warm page kept for the other presolves
        for _ in ACCOUNTS[1:]:
            self.assertTrue(service._run_once())
        self.assertFalse(service._run_once())
        self.assertEqual(1, browser.prepared)
        self.assertEqual(len(ACCOUNTS), len(browser.calls))
        self.assertEqual(1, browser.parked)  # tokens ready: the page is dropped
        self.assertFalse(service._run_once())
        self.assertEqual(1, browser.parked)
        # Admission later in the window finds the tokens already solved.
        for account in ACCOUNTS:
            service.prepare_event(EVENT, valid_for_seconds=90, account=account)
        self.assertEqual([], broker.presolve_demand())
        self.assertTrue(all(create(broker, account)["presolved"] for account in ACCOUNTS))
        # The browser is held open through the window and released after it.
        mono[0] += 60
        service._run_once()
        self.assertEqual(mono[0], service.worker.last_work)
        wall[0] = shanghai(13, 47, 0)
        mono[0] += 60
        service._run_once()
        self.assertLess(service.worker.last_work, mono[0])

    def test_failed_warm_up_is_retried_later_and_presolve_still_runs(self):
        browser = FakeBrowser()
        attempts = []

        def failing_prepare():
            attempts.append(1)
            if len(attempts) == 1:
                raise BrowserVerificationError("turnstile_script_unavailable")
        browser.prepare = failing_prepare
        service, _broker, _now, mono, wall = service_with(browser, wall=[shanghai(13, 39, 5)])
        # The presolve does not wait for the retry: verify() loads the page itself.
        self.assertTrue(service._run_once())
        self.assertTrue(service._run_once())
        self.assertEqual(1, len(attempts))
        self.assertEqual(1, browser.closed)
        mono[0] += 16
        self.assertTrue(service._run_once())
        self.assertEqual(2, len(attempts))
        self.assertEqual(len(ACCOUNTS), len(browser.calls))

    def test_unknown_account_is_rejected(self):
        service, broker, *_ = service_with(FakeBrowser())
        self.assertFalse(service.capability("tg0")["available"])
        with self.assertRaisesRegex(ValueError, "verification_account_invalid"):
            service.start_account("tg0")
        service.prepare_event(EVENT, valid_for_seconds=90, account="tg0")
        self.assertEqual([], broker.presolve_demand())

    def test_account_close_cancels_only_its_requests(self):
        service, broker, *_ = service_with(FakeBrowser())
        mine = create(broker, ACCOUNTS[0])["request_id"]
        other = create(broker, ACCOUNTS[1])["request_id"]
        asyncio.run(service.close_account(ACCOUNTS[0]))
        self.assertEqual("cancelled", broker.get_request(mine)["status"])
        self.assertEqual("pending", broker.get_request(other)["status"])

    def test_profile_path_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "real").mkdir()
            (root / "browser-profile").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "verification_profile_path_invalid"):
                SerialVerificationService(root / "browser-profile", account_order=ACCOUNTS, trusted_root=root,
                                          browser=FakeBrowser())
            with self.assertRaisesRegex(ValueError, "verification_profile_path_invalid"):
                SerialVerificationService(root / ".." / "x", account_order=ACCOUNTS, trusted_root=root,
                                          browser=FakeBrowser())


class ScreenshotTests(unittest.TestCase):
    def test_pre_click_screenshot_is_clipped_to_one_pixel(self):
        sent = []
        with patch.object(NativeTurnstileBrowser, "command", lambda _self, method, params=None: sent.append((method, params)) or {}):
            browser = PreparedNativeTurnstileBrowser("/not-used")
            browser.command("Page.captureScreenshot", {"format": "png"})
            browser.command("Runtime.evaluate", {"expression": "1 + 1"})
            browser.command("Page.captureScreenshot", {"format": "png", "clip": {"x": 5, "y": 5, "width": 9, "height": 9, "scale": 1}})
        self.assertEqual(("Page.captureScreenshot", {"format": "png", "clip": SCREENSHOT_CLIP}), sent[0])
        self.assertEqual(("Runtime.evaluate", {"expression": "1 + 1"}), sent[1])
        self.assertEqual(9, sent[2][1]["clip"]["width"])


if __name__ == "__main__":
    unittest.main()
