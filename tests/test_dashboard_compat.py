"""The dashboard against data written by the real scraper: crawler status line, stale-data flags,
and full compatibility with an older index.json that lacks the new fields.

Needs Playwright with a browser; skipped automatically where that isn't installed (e.g. Termux).
"""
import functools
import http.server
import json
import shutil
import socketserver
import sys
import threading
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO, ScraperCase, scrape  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                  # pragma: no cover
    sync_playwright = None


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class TestDashboard(ScraperCase):
    def setUp(self):
        super().setUp()
        self.site = self.tmp / "site"
        (self.site).mkdir()
        shutil.copy(REPO / "docs" / "index.html", self.site / "index.html")
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(self.site)))
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/index.html"
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        try:
            self.browser = self.pw.chromium.launch()
        except Exception as exc:                     # browsers not downloaded
            self.skipTest(f"no Chromium available: {exc}")
        self.addCleanup(self.browser.close)

    def publish(self):
        """Copy what the scraper just wrote into the dashboard's data/ folder."""
        target = self.site / "data"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(scrape.DATA_DIR, target)

    def open(self, hash_=""):
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.goto(self.url + hash_)
        page.wait_for_selector(".scheme-card, .ins-row, .error-state")
        self.addCleanup(page.close)
        return page

    def test_a_fully_successful_run(self):
        self.run_main()
        self.publish()
        page = self.open()
        self.assertEqual(page.locator(".scheme-card").count(), 24)
        bar = page.inner_text(".crawl-status")
        self.assertIn("Crawler: ✓ SUCCESS", bar)
        self.assertIn("Successful 24/24", bar)
        self.assertIn("Failed 0", bar)
        self.assertIn("Unprocessed 0", bar)
        self.assertEqual(page.locator(".stale-note").count(), 0)
        self.assertEqual(self.errors, [])

    def test_a_scheme_that_failed_every_attempt_shows_as_an_error_not_as_success(self):
        e = requests.exceptions.ConnectionError("down")
        self.script[self.code(4)] = [e, e, e]
        self.run_main()
        self.publish()
        page = self.open()
        self.assertIn("Crawler: ! WARNING", page.inner_text(".crawl-status"))
        self.assertIn("Successful 23/24", page.inner_text(".crawl-status"))
        self.assertIn("Failed 1", page.inner_text(".crawl-status"))
        self.assertEqual(page.locator(".status-badge.error").count(), 1)
        self.assertEqual(page.locator(".scheme-card").count(), 24, "every other scheme's data is still there")
        self.assertEqual(self.errors, [])

    def test_a_partial_run_keeps_data_visible_but_never_claims_24_of_24(self):
        self.run_main()                                              # yesterday: everything scraped
        self.script[self.code(11)] = [KeyboardInterrupt("killed")]
        self.calls = []
        self.run_main()                                              # today: cut short after 10 schemes
        self.publish()
        page = self.open()
        bar = page.inner_text(".crawl-status")
        self.assertIn("Crawler: ◐ PARTIAL", bar)
        self.assertIn("Successful 10/24", bar)
        self.assertIn("Unprocessed 14", bar)
        self.assertNotIn("24/24", bar)
        self.assertEqual(page.locator(".scheme-card").count(), 24, "yesterday's data for the 14 stays visible...")
        self.assertEqual(page.locator(".stale-note").count(), 14, "...but is flagged as not refreshed")
        self.assertEqual(page.locator(".status-badge.stale").count(), 14, "and never wears the green 'Live data' badge")
        self.assertEqual(page.locator(".status-badge.ok").count(), 10)
        self.assertIn("not refreshed in the latest run", page.locator(".scheme-card").nth(15).inner_text())
        self.assertNotIn("not refreshed", page.locator(".scheme-card").nth(2).inner_text())
        self.assertEqual(self.errors, [])

    def test_insights_shows_the_status_and_flags_stale_rows(self):
        self.run_main()
        self.script[self.code(11)] = [KeyboardInterrupt("killed")]
        self.calls = []
        self.run_main()
        self.publish()
        page = self.open("#/insights")
        self.assertIn("Crawler: ◐ PARTIAL", page.inner_text(".crawl-status"))
        self.assertEqual(page.locator(".ins-schemes .ins-row").count(), 24)
        self.assertEqual(page.locator(".ins-schemes .stale-note").count(), 14)
        self.assertEqual(self.errors, [])

    def test_a_scheme_never_scraped_yet_shows_as_not_fetched(self):
        self.script[self.code(1)] = [KeyboardInterrupt("killed at the very start")]
        self.run_main()
        self.publish()
        page = self.open()
        self.assertIn("Not fetched yet", page.locator(".scheme-card").first.inner_text())
        self.assertEqual(self.errors, [])

    def test_17_an_old_index_json_without_the_new_fields_renders_exactly_as_before(self):
        (self.site / "data").mkdir()
        shutil.copytree(REPO / "docs" / "data", self.site / "data", dirs_exist_ok=True)
        legacy = json.loads((self.site / "data" / "index.json").read_text(encoding="utf-8"))
        for key in ("execution_id", "overall_status", "expected_scheme_count", "successful_scheme_count",
                    "failed_scheme_count", "unprocessed_scheme_count"):
            legacy.pop(key, None)
        for entry in legacy["schemes"]:
            entry.pop("crawl_status", None)
        (self.site / "data" / "index.json").write_text(json.dumps(legacy), encoding="utf-8")
        page = self.open()
        self.assertEqual(page.locator(".scheme-card").count(), len(legacy["schemes"]))
        self.assertEqual(page.locator(".crawl-status").count(), 0, "no status line without the new fields")
        self.assertEqual(page.locator(".stale-note").count(), 0)
        page.locator(".scheme-card").first.click()
        page.wait_for_selector(".back-link")
        self.assertIn("Back to schemes", page.inner_text(".back-link"))
        insights = self.open("#/insights")
        self.assertGreater(insights.locator(".ins-schemes .ins-row").count(), 0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
