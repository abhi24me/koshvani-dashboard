"""telegram_alert.py: final-state reports (Telegram + Gmail), baseline protection, channel isolation."""
import argparse
import contextlib
import email
import hashlib
import io
import json
import re
import shutil
import smtplib
import socket
import ssl
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from html import unescape
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO, alert, make_schemes, ok_result, scrape  # noqa: E402

REAL_LOAD_ENV = alert.load_env
FIXED_TIME = "2026-09-19T10:00:00+00:00"

FAKE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456789:FAKE-token_ABCdef",
    "TELEGRAM_CHAT_ID": "-1009999",
    "GMAIL_SENDER": "sender.fake@example.test",
    "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
    "GMAIL_RECIPIENTS": " a.fake@example.test , ,b.fake@example.test,, C.fake@example.test ,A.FAKE@example.test",
}
SECRETS = ["FAKE-token_ABCdef", "123456789:FAKE", "abcd efgh ijkl mnop", "abcdefghijklmnop", "sender.fake@example.test",
           "a.fake@example.test", "b.fake@example.test", "C.fake@example.test"]


class FakeSMTP:
    behavior, instances = {}, []

    def __init__(self, host, port, context=None, timeout=None):
        if FakeSMTP.behavior.get("on_connect"):
            raise FakeSMTP.behavior["on_connect"]
        if FakeSMTP.behavior.get("hook"):
            FakeSMTP.behavior["hook"]()
        self.host, self.port, self.context, self.timeout = host, port, context, timeout
        self.logged_in, self.sent = None, []
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def login(self, user, password):
        if FakeSMTP.behavior.get("on_login"):
            raise FakeSMTP.behavior["on_login"]
        self.logged_in = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if FakeSMTP.behavior.get("on_send"):
            raise FakeSMTP.behavior["on_send"]
        self.sent.append((msg, from_addr, list(to_addrs)))
        return FakeSMTP.behavior.get("refused", {})


class FakeResponse:
    def __init__(self, code=200, text="ok"):
        self.status_code, self.text = code, text


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(directory):
    directory = Path(directory)
    return {p.name: sha(p) for p in sorted(directory.glob("*.json"))} if directory.exists() else {}


def parse_message(msg):
    parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    return (parsed, parsed.get_body(preferencelist=("plain",)).get_content(),
            parsed.get_body(preferencelist=("html",)).get_content())


class AlertCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="koshvani_alert_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data, self.baseline = self.tmp / "data", self.tmp / "baseline"
        self.data.mkdir()
        self.schemes = make_schemes(24)
        self.env = dict(FAKE_ENV)
        self.tg_calls, self.tg_behavior = [], None
        FakeSMTP.behavior, FakeSMTP.instances = {}, []
        self.logs = io.StringIO()
        for patcher in (
            mock.patch.object(alert, "DATA_DIR", self.data),
            mock.patch.object(alert, "INDEX_PATH", self.data / "index.json"),
            mock.patch.object(alert, "BASELINE_DIR", self.baseline),
            mock.patch.object(alert, "SCHEMES_CONFIG_PATH", self.tmp / "schemes.json"),
            mock.patch.object(alert, "load_env", lambda *a, **k: dict(self.env)),
            mock.patch.object(alert.smtplib, "SMTP_SSL", FakeSMTP),
            mock.patch.object(alert.requests, "post", self._fake_post),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.tmp / "schemes.json").write_text(json.dumps(self.schemes), encoding="utf-8")

    def _fake_post(self, url, data=None, timeout=None):
        self.tg_calls.append((url, dict(data or {})))
        if isinstance(self.tg_behavior, Exception):
            raise self.tg_behavior
        return FakeResponse(self.tg_behavior or 200, "boom" if self.tg_behavior else "ok")

    # ---- disk state of one finished (or interrupted) run ----
    def write_run(self, statuses=None, overall="SUCCESS", fatal="", started_ago=30, names=None, files=None):
        """statuses: {scheme number (1-based): (status, attempts, remark)}; default (1, 1, "").
        Status 1 -> a fresh ok file; -1 -> the error stub; 0 -> LAST run's stale file (spent 700)."""
        statuses = statuses or {}
        names = names or {}
        cfg = [dict(s, name=names.get(i, s["name"])) for i, s in enumerate(self.schemes, 1)]
        (self.tmp / "schemes.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        crawl = {}
        for i, scheme in enumerate(cfg, 1):
            status, attempts, remark = statuses.get(i, (1, 1, ""))
            crawl[scheme["scheme_code"]] = {"status": status, "attempts": attempts, "remark": remark}
            if files and i in files:
                result = files[i]
            elif status == 1:
                result = ok_result(scheme, spent=500)
            elif status == -1:
                result = scrape.error_result(scheme, remark)
            else:
                result = dict(ok_result(scheme, spent=700), generated_at="2026-09-01T00:00:00+00:00")
            if result.get("generated_at", "").startswith("2026-09-01") is False:
                result = dict(result, generated_at=FIXED_TIME)          # deterministic bytes, run after run
            (self.data / f"{scheme['id']}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        started = (datetime.now(timezone.utc) - timedelta(seconds=started_ago)).isoformat(timespec="seconds")
        (self.data / "crawler_status.json").write_text(json.dumps({
            "execution_id": started, "started_at": started, "updated_at": started, "finished_at": None if overall == "RUNNING" else started,
            "overall_status": overall, "fatal_error": fatal, "schemes": crawl}), encoding="utf-8")
        (self.data / "index.json").write_text(json.dumps({"schemes": [], "generated_at": FIXED_TIME}), encoding="utf-8")

    def write_baseline(self, number, result):
        self.baseline.mkdir(exist_ok=True)
        result = result if result.get("generated_at", "").startswith("2026-09-01") else dict(result, generated_at=FIXED_TIME)
        (self.baseline / f"{self.schemes[number - 1]['id']}.json").write_text(json.dumps(result), encoding="utf-8")

    def baseline_all_current(self):
        for i, s in enumerate(self.schemes, 1):
            self.write_baseline(i, json.loads((self.data / f"{s['id']}.json").read_text(encoding="utf-8")))

    def report(self, start_ago=60):
        return alert.build_report(time.time() - start_ago)

    def run_cmd(self, fn, **kw):
        buf, exc = io.StringIO(), None
        with contextlib.redirect_stderr(buf):
            try:
                fn(argparse.Namespace(**kw))
            except BaseException as e:      # noqa: BLE001 - the point is to see whether anything escapes
                exc = e
        self.logs.write(buf.getvalue())
        return buf.getvalue(), exc


class TestReportFromFinalState(AlertCase):
    def test_success_when_all_24_succeeded(self):
        self.write_run()
        r = self.report()
        self.assertEqual((r["level"], r["subject"]), ("success", "🟢 KOSHVANI CRAWLER — SUCCESS | 24/24 Schemes"))
        self.assertIn("📊 Schemes: 24/24 successful", r["text"])
        self.assertIn("📈 Data changes: None", r["text"])
        self.assertIn("✅ Job completed successfully.", r["text"])
        self.assertNotIn("Recovered", r["text"])
        self.assertEqual(len(r["fresh_ids"]), 24)

    def test_14_recovered_schemes_are_reported_as_success_using_the_final_state(self):
        self.write_run({3: (1, 2, "Recovered on retry"), 9: (1, 3, "Recovered on retry")})
        r = self.report()
        self.assertEqual(r["level"], "success")
        self.assertIn("📊 Schemes: 24/24 successful", r["text"])
        self.assertIn("🔁 Recovered by retry: 2", r["text"])
        self.assertNotIn("Failed", r["text"], "an attempt that failed and then recovered is not a failure")
        self.assertEqual(r["counts"]["recovered"], 2)

    def test_warning_when_a_scheme_failed_all_retries(self):
        self.write_run({5: (-1, 3, "ReadTimeout: HTTPSConnectionPool read timed out"), 8: (1, 2, "Recovered on retry")})
        r = self.report()
        self.assertEqual((r["level"], r["subject"]), ("warning", "🟠 KOSHVANI CRAWLER — WARNING | 23/24 Schemes"))
        for expected in ("📊 Schemes: 23/24 successful", "🔁 Recovered by retry: 1", "❌ Failed: 1", "Failed:",
                         "• Scheme 5 — ReadTimeout: HTTPSConnectionPool read timed out", "⚠️ Review required."):
            self.assertIn(expected, r["text"])
        self.assertNotIn("Unprocessed", r["text"])
        self.assertEqual(r["fresh_ids"].count(self.schemes[4]["id"]), 0)

    def test_partial_after_a_fatal_interruption(self):
        statuses = {i: (0, 0, "") for i in range(11, 25)}
        self.write_run(statuses, overall="PARTIAL", fatal="Interrupted: received signal 15")
        r = self.report()
        self.assertEqual((r["level"], r["subject"]), ("partial", "🟣 KOSHVANI CRAWLER — PARTIAL | 10/24 Schemes"))
        for expected in ("📊 Schemes: 10/24 successful", "⏸ Unprocessed: 14", "🛑 Fatal error: Interrupted: received signal 15",
                         "⚠️ Execution incomplete. Review required."):
            self.assertIn(expected, r["text"])
        self.assertEqual(len(r["fresh_ids"]), 10)

    def test_a_killed_run_that_never_finalized_is_partial_too(self):
        self.write_run({i: (0, 0, "") for i in range(11, 25)}, overall="RUNNING")
        r = self.report()
        self.assertEqual(r["level"], "partial")
        self.assertIn("stopped before every scheme was processed", r["text"])

    def test_error_when_nothing_was_attempted(self):
        self.write_run({i: (0, 0, "") for i in range(1, 25)}, overall="ERROR", fatal="Fatal crawler error: ValueError: schemes.json is empty")
        r = self.report()
        self.assertEqual((r["level"], r["subject"]), ("error", "🔴 KOSHVANI CRAWLER — ERROR"))
        self.assertIn("Fatal crawler error", r["text"])
        self.assertFalse(r["can_update_baseline"])

    def test_missing_status_file_is_an_error(self):
        self.write_run()
        (self.data / "crawler_status.json").unlink()
        r = self.report()
        self.assertEqual(r["level"], "error")
        self.assertIn("left no status for this run", r["text"])
        self.assertEqual(r["fresh_ids"], [])

    def test_status_from_an_earlier_execution_is_never_mistaken_for_this_one(self):
        self.write_run(started_ago=3 * 3600)               # a perfect SUCCESS status - from three hours ago
        r = self.report(start_ago=60)
        self.assertEqual(r["level"], "error", "yesterday's SUCCESS must not be reported as today's")
        self.assertIn("earlier run", r["text"])

    def test_corrupt_status_file_is_an_error(self):
        self.write_run()
        (self.data / "crawler_status.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.report()["level"], "error")

    def test_data_changes_are_listed_for_freshly_scraped_schemes(self):
        self.write_run()
        self.baseline_all_current()
        self.write_baseline(3, ok_result(self.schemes[2], spent=100))      # baseline says 100, now 500
        r = self.report()
        self.assertEqual(r["level"], "success")
        self.assertIn("📈 Data changes: 2", r["text"])
        self.assertIn("• Scheme 3", r["text"])
        self.assertIn("Total Expenditure up to Month: 100 → 500", r["text"])
        self.assertIn("Change: +400", r["text"])

    def test_warning_still_shows_data_changes(self):
        self.write_run({6: (-1, 3, "ConnectionError: down")})
        self.baseline_all_current()
        self.write_baseline(3, ok_result(self.schemes[2], spent=100))
        r = self.report()
        self.assertEqual(r["level"], "warning")
        self.assertIn("📈 Data changes: 2", r["text"])
        self.assertLess(r["text"].index("Failed:"), r["text"].index("Data changes"))

    def test_no_changes_section_for_a_warning_without_changes(self):
        self.write_run({6: (-1, 3, "ConnectionError: down")})
        self.baseline_all_current()
        self.assertNotIn("Data changes", self.report()["text"])

    def test_data_that_vanished_since_the_baseline_downgrades_success_to_warning(self):
        empty = {**scrape.base_meta(self.schemes[3], "2026-2027"), "status": "empty", "message": "none", "column_headers": [],
                 "column_widths": None, "rows": [], "totals": {}}
        self.write_run({4: (1, 1, "No expenditure recorded (portal reports no record)")}, files={4: empty})
        self.baseline_all_current()
        self.write_baseline(4, ok_result(self.schemes[3]))               # it used to have financial data
        r = self.report()
        self.assertEqual(r["level"], "warning")
        self.assertIn("🔎 Data review needed: 1", r["text"])
        self.assertIn("Previously had financial data; now empty", r["text"])
        self.assertIn("📊 Schemes: 24/24 successful", r["text"], "the crawl itself succeeded")

    def test_expected_total_comes_from_schemes_json(self):
        self.write_run()
        status = json.loads((self.data / "crawler_status.json").read_text(encoding="utf-8"))
        del status["schemes"][self.schemes[23]["scheme_code"]]           # the status file lost one scheme
        (self.data / "crawler_status.json").write_text(json.dumps(status), encoding="utf-8")
        r = self.report()
        self.assertEqual(r["counts"]["total"], 24)
        self.assertEqual(r["level"], "partial")


class TestRendering(AlertCase):
    def scenarios(self):
        out = {}
        self.write_run()
        out["success"] = self.report()
        self.write_run({5: (-1, 3, "ReadTimeout: x"), 9: (1, 2, "Recovered on retry")})
        out["warning"] = self.report()
        self.write_run({i: (0, 0, "") for i in range(11, 25)}, overall="PARTIAL", fatal="Interrupted")
        out["partial"] = self.report()
        return out

    def test_12_the_html_email_lists_every_scheme_in_a_table(self):
        for level, r in self.scenarios().items():
            with self.subTest(level):
                page = r["email_html"]
                tbody = page[page.index("<tbody>"):page.index("</tbody>")]
                self.assertEqual(tbody.count("<tr"), 24, "one table row per scheme")
                self.assertEqual(page.count("<th "), 6, "six column headers")
                for h in ("#", "Scheme Code", "Scheme Name", "Status", "Attempts", "Remark"):
                    self.assertIn(f">{h}</th>", page)
                for scheme in self.schemes:
                    self.assertIn(scheme["scheme_code"], page)
                    self.assertIn(f">{scheme['name']}</td>", page)
                self.assertIn("1 = SUCCESS, 0 = NOT PROCESSED, -1 = FAILED", page)
                self.assertIn("All 24 schemes", page)
                self.assertNotRegex(page, r"https?://")

    def test_12_status_attempts_and_remarks_are_visible_in_the_html_table(self):
        page = self.scenarios()["warning"]["email_html"]
        text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", page)))
        self.assertIn("ReadTimeout: x", text)
        self.assertIn("Recovered on retry", text)
        self.assertRegex(page, r"<b style=\"color:#b3261e\">-1</b>")
        self.assertRegex(page, r"<b style=\"color:#1a7f37\">1</b>")
        partial = self.scenarios()["partial"]["email_html"]
        self.assertRegex(partial, r"<b style=\"color:#6b7280\">0</b>")
        self.assertIn("Not processed", partial)

    def test_13_the_plain_text_email_lists_every_scheme(self):
        for level, r in self.scenarios().items():
            with self.subTest(level):
                lines = [ln for ln in r["email_text"].splitlines() if re.match(r"^\d+ \| ", ln)]
                self.assertEqual(len(lines), 24)
                for i, scheme in enumerate(self.schemes, 1):
                    self.assertTrue(any(ln.startswith(f"{i} | {scheme['scheme_code']} | {scheme['name']} | ") for ln in lines), scheme["name"])
                self.assertIn("# | Scheme Code | Scheme Name | Status | Attempts | Remark", r["email_text"])
                self.assertIn("Status legend: 1 = SUCCESS, 0 = NOT PROCESSED, -1 = FAILED", r["email_text"])

    def test_the_email_summary_block_has_every_required_figure(self):
        text = self.scenarios()["warning"]["email_text"]
        for expected in ("Execution:", "Duration:", "Schemes: 23/24 successful", "Total: 24 | Successful: 23 | Failed: 1 | Unprocessed: 0",
                         "Recovered by retry: 1", "Data changes: None"):
            self.assertIn(expected, text)
        html = self.scenarios()["warning"]["email_html"]
        for label in ("Execution", "Duration", "Schemes", "Recovered by retry", "Failed", "Unprocessed", "Data changes"):
            self.assertIn(f">{label}</td>", html)

    def test_dynamic_values_are_html_escaped_but_plain_text_is_raw(self):
        names = {5: '<b>Evil</b> & "co"'}
        self.write_run({5: (-1, 3, "<img src=x onerror=alert(1)> & more")}, names=names)
        r = self.report()
        self.assertNotIn("<b>Evil", r["email_html"])
        self.assertNotIn("<img", r["email_html"])
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; &amp; &quot;co&quot;", r["email_html"])
        self.assertIn("&lt;img src=x onerror=alert(1)&gt; &amp; more", r["email_html"])
        self.assertIn('<b>Evil</b> & "co"', r["email_text"])

    def test_telegram_stays_concise_and_has_no_table(self):
        r = self.scenarios()["success"]
        self.assertLess(len(r["text"]), 400)
        self.assertNotIn("Scheme Code", r["text"])
        self.assertNotIn("Scheme 1 ", r["text"])
        self.assertGreater(len(self.scenarios()["success"]["email_text"]), 4 * len(r["text"]))

    def test_telegram_and_email_report_the_same_facts(self):
        r = self.scenarios()["warning"]
        for fact in ("23/24", "Scheme 5", "ReadTimeout: x"):
            self.assertIn(fact, r["text"])
            self.assertIn(fact, r["email_text"])
            self.assertIn(fact, unescape(r["email_html"]))

    def test_the_email_message_is_one_multipart_message_for_every_recipient(self):
        r = self.scenarios()["warning"]
        msg = alert.build_email(r, "sender.fake@example.test", ["a.fake@example.test", "b.fake@example.test"])
        parsed, plain, html = parse_message(msg)
        self.assertEqual(parsed["Subject"], "🟠 KOSHVANI CRAWLER — WARNING | 23/24 Schemes")
        self.assertEqual([p.get_content_type() for p in parsed.iter_parts()], ["text/plain", "text/html"])
        self.assertEqual(plain.strip(), r["email_text"].strip())
        self.assertEqual(html.strip(), r["email_html"].strip())
        self.assertIn("a.fake@example.test", parsed["To"])
        self.assertIn("b.fake@example.test", parsed["To"])

    def test_job_level_error_reports_keep_their_short_format(self):
        r = alert.build_error_report(time.time() - 42, "git-push")
        self.assertEqual((r["level"], r["subject"]), ("error", "🔴 KOSHVANI CRAWLER — ERROR"))
        for expected in ("❌ Git push failed.", "⏳ Duration:", "❌ Job failed. Review required."):
            self.assertIn(expected, r["text"])
        self.assertNotIn("Schemes:", r["text"])


class TestBaselineProtection(AlertCase):
    """The last-known-good baseline only ever advances for schemes freshly scraped THIS run."""

    def scenario(self):
        # 1 = fresh + changed | 2 = failed today | 3 = never processed (stale file on disk) | rest unchanged
        self.write_run({2: (-1, 3, "ReadTimeout: x"), 3: (0, 0, "")}, overall="PARTIAL", fatal="cut short")
        self.baseline_all_current()
        self.write_baseline(1, ok_result(self.schemes[0], spent=100))
        self.write_baseline(2, ok_result(self.schemes[1], spent=222))          # yesterday's good data
        self.write_baseline(3, ok_result(self.schemes[2], spent=333))
        self.before = snapshot(self.baseline)

    def file_id(self, n):
        return f"{self.schemes[n - 1]['id']}.json"

    def test_11_a_failed_scheme_never_overwrites_its_last_known_good_baseline(self):
        self.scenario()
        alert.update_baseline(self.report()["fresh_ids"])
        after = snapshot(self.baseline)
        self.assertEqual(after[self.file_id(2)], self.before[self.file_id(2)])
        self.assertEqual(json.loads((self.baseline / self.file_id(2)).read_text())["rows"][0]["cells"][7], "222.00")

    def test_a_scheme_that_was_never_processed_is_not_copied_into_the_baseline(self):
        self.scenario()
        alert.update_baseline(self.report()["fresh_ids"])
        self.assertEqual(snapshot(self.baseline)[self.file_id(3)], self.before[self.file_id(3)],
                         "last run's stale file must not be treated as freshly scraped")

    def test_a_freshly_scraped_healthy_scheme_updates_its_baseline(self):
        self.scenario()
        alert.update_baseline(self.report()["fresh_ids"])
        self.assertNotEqual(snapshot(self.baseline)[self.file_id(1)], self.before[self.file_id(1)])
        self.assertEqual(snapshot(self.baseline)[self.file_id(1)], sha(self.data / self.file_id(1)))

    def test_a_scheme_that_recovers_later_updates_its_baseline_again(self):
        self.scenario()
        alert.update_baseline(self.report()["fresh_ids"])
        self.write_run({2: (1, 2, "Recovered on retry")})                       # next run: scheme 2 succeeds on retry
        alert.update_baseline(self.report()["fresh_ids"])
        self.assertEqual(snapshot(self.baseline)[self.file_id(2)], sha(self.data / self.file_id(2)))

    def test_first_run_creates_baselines_only_for_fresh_healthy_schemes(self):
        self.write_run({2: (-1, 3, "x"), 3: (0, 0, "")}, overall="PARTIAL")
        alert.update_baseline(self.report()["fresh_ids"])
        names = {p.name for p in self.baseline.glob("agri-*.json")}
        self.assertEqual(len(names), 22)
        self.assertNotIn(self.file_id(2), names)
        self.assertNotIn(self.file_id(3), names)

    def test_an_empty_result_does_not_replace_a_healthy_baseline(self):
        empty = {**scrape.base_meta(self.schemes[3], "2026-2027"), "status": "empty", "message": "none", "column_headers": [],
                 "column_widths": None, "rows": [], "totals": {}}
        self.write_run({4: (1, 1, "No expenditure recorded")}, files={4: empty})
        self.baseline_all_current()
        self.write_baseline(4, ok_result(self.schemes[3], spent=444))
        before = sha(self.baseline / self.file_id(4))
        alert.update_baseline(self.report()["fresh_ids"])
        self.assertEqual(sha(self.baseline / self.file_id(4)), before)


class TestDeliveryIsolation(AlertCase):
    def scenario(self, tg=None, smtp=None, hook=None):
        self.write_run({2: (-1, 3, "ReadTimeout: x"), 3: (0, 0, "")}, overall="PARTIAL", fatal="cut short")
        self.baseline_all_current()
        self.write_baseline(1, ok_result(self.schemes[0], spent=100))
        self.write_baseline(2, ok_result(self.schemes[1], spent=222))
        self.write_baseline(3, ok_result(self.schemes[2], spent=333))
        self.tg_calls, self.tg_behavior = [], tg
        FakeSMTP.behavior, FakeSMTP.instances = dict(smtp or {}), []
        if hook:
            FakeSMTP.behavior["hook"] = hook
        self.before = snapshot(self.baseline)
        self.data_before = snapshot(self.data)
        logs, exc = self.run_cmd(alert.cmd_report, start=time.time() - 60)
        return logs, exc, snapshot(self.baseline)

    def healthy_reference(self):
        _, exc, after = self.scenario()
        self.assertIsNone(exc)
        return after

    def test_both_channels_deliver_the_same_final_report(self):
        _, exc, _ = self.scenario()
        self.assertIsNone(exc)
        self.assertEqual(len(self.tg_calls), 1)
        self.assertEqual(len(FakeSMTP.instances), 1)
        msg = FakeSMTP.instances[0].sent[0][0]
        self.assertEqual(msg["Subject"], "🟣 KOSHVANI CRAWLER — PARTIAL | 22/24 Schemes")
        self.assertIn("22/24 successful", self.tg_calls[0][1]["text"])
        self.assertIn("22/24 successful", parse_message(msg)[1])

    def test_15_notification_failure_never_deletes_or_rolls_back_scraped_data(self):
        logs, exc, after = self.scenario(tg=requests.exceptions.ConnectionError("down"),
                                         smtp={"on_login": smtplib.SMTPAuthenticationError(535, b"x")})
        self.assertIsNone(exc)
        self.assertEqual(snapshot(self.data), self.data_before, "docs/data (scheme files, index, crawler_status) untouched")
        self.assertEqual(after, self.before, "baseline untouched when Telegram could not deliver")
        self.assertIn("baseline NOT updated", logs)

    def test_gmail_failure_of_any_kind_changes_nothing_else(self):
        reference = self.healthy_reference()
        password_in_message = "535 5.7.8 Username and Password not accepted (abcdefghijklmnop) for sender.fake@example.test"
        modes = {
            "auth failed": ({"on_login": smtplib.SMTPAuthenticationError(535, password_in_message.encode())}, "SMTP authentication failed"),
            "timeout": ({"on_connect": socket.timeout("timed out")}, "connection to Gmail timed out"),
            "tls": ({"on_connect": ssl.SSLError("CERTIFICATE_VERIFY_FAILED")}, "TLS/SSL error"),
            "unreachable": ({"on_connect": OSError(101, "Network is unreachable")}, "could not reach Gmail SMTP"),
            "disconnected": ({"on_send": smtplib.SMTPServerDisconnected("closed")}, "SMTP error (SMTPServerDisconnected)"),
            "recipients refused": ({"on_send": smtplib.SMTPRecipientsRefused({"a.fake@example.test": (550, b"x")})}, "all recipients were refused"),
            "smtp code": ({"on_send": smtplib.SMTPDataError(554, b"rejected")}, "SMTP error 554"),
            "unexpected, carrying secrets": ({"on_login": RuntimeError("boom abcdefghijklmnop 123456789:FAKE-token_ABCdef")}, "unexpected error (RuntimeError)"),
        }
        for name, (behavior, reason) in modes.items():
            with self.subTest(name):
                logs, exc, after = self.scenario(smtp=behavior)
                self.assertIsNone(exc)
                self.assertIn(f"Email notification failed: {reason}", logs)
                self.assertEqual(len(self.tg_calls), 1, "Telegram unaffected")
                self.assertEqual(after, reference, "baseline identical to a fully healthy run")
                self.assertEqual(snapshot(self.data), self.data_before)
                self.assertFalse([s for s in SECRETS if s in logs], "no secret or address in the log")

    def test_baseline_is_settled_before_gmail_is_even_contacted(self):
        reference = self.healthy_reference()
        seen = {}
        self.scenario(hook=lambda: seen.update(at_connect=snapshot(self.baseline)))
        self.assertEqual(seen["at_connect"], reference)

    def test_telegram_failure_still_sends_the_email_and_keeps_history(self):
        for name, behavior in {"network error carrying the token": requests.exceptions.ConnectionError(
                f"Max retries exceeded with url: /bot{FAKE_ENV['TELEGRAM_BOT_TOKEN']}/sendMessage"),
                "http 500": 500, "odd exception": ValueError("weird " + FAKE_ENV["TELEGRAM_BOT_TOKEN"])}.items():
            with self.subTest(name):
                logs, exc, after = self.scenario(tg=behavior)
                self.assertIsNone(exc)
                self.assertEqual(len(FakeSMTP.instances), 1)
                self.assertEqual(len(FakeSMTP.instances[0].sent), 1)
                self.assertEqual(after, self.before, "change history kept for the next run")
                self.assertNotIn(FAKE_ENV["TELEGRAM_BOT_TOKEN"], logs)
                self.assertNotRegex(logs, r"bot\d{5,}:")

    def test_the_email_step_crashing_is_contained(self):
        reference = self.healthy_reference()
        with mock.patch.object(alert, "build_email", side_effect=KeyError("subject")):
            logs, exc, after = self.scenario()
        self.assertIsNone(exc)
        self.assertEqual(after, reference)
        self.assertEqual(len(self.tg_calls), 1)
        self.assertIn("Email notification failed", logs)

    def test_gmail_not_configured_is_skipped_and_telegram_still_works(self):
        self.env = {k: v for k, v in FAKE_ENV.items() if not k.startswith("GMAIL")}
        logs, exc, _ = self.scenario()
        self.assertIsNone(exc)
        self.assertIn("skipped", logs)
        self.assertEqual((len(self.tg_calls), len(FakeSMTP.instances)), (1, 0))

    def test_cli_error_command_sends_the_same_error_everywhere(self):
        with mock.patch.object(sys, "argv", ["telegram_alert.py", "error", "--start", str(time.time() - 42), "--stage", "git-push"]):
            with contextlib.redirect_stderr(io.StringIO()):
                alert.main()
        msg = FakeSMTP.instances[0].sent[0][0]
        self.assertEqual(msg["Subject"], "🔴 KOSHVANI CRAWLER — ERROR")
        self.assertIn("Git push failed.", self.tg_calls[0][1]["text"])
        self.assertIn("Git push failed.", parse_message(msg)[1])

    def test_cli_report_command_end_to_end(self):
        self.write_run()
        with mock.patch.object(sys, "argv", ["telegram_alert.py", "report", "--start", str(time.time() - 60)]):
            with contextlib.redirect_stderr(io.StringIO()):
                alert.main()
        self.assertEqual((len(self.tg_calls), len(FakeSMTP.instances)), (1, 1))
        self.assertEqual(len(list(self.baseline.glob("agri-*.json"))), 24, "first run establishes the baseline")


class TestChannelBasics(AlertCase):
    def test_gmail_config_parsing(self):
        sender, password, recipients = alert.gmail_config(FAKE_ENV)
        self.assertEqual(recipients, ["a.fake@example.test", "b.fake@example.test", "C.fake@example.test"])
        self.assertEqual(password, "abcdefghijklmnop")
        for patch in ({"GMAIL_SENDER": ""}, {"GMAIL_APP_PASSWORD": ""}, {"GMAIL_RECIPIENTS": ""}, {"GMAIL_RECIPIENTS": " , ,, "}, {"GMAIL_RECIPIENTS": "nope"}):
            self.assertIsNone(alert.gmail_config({**FAKE_ENV, **patch}), patch)

    def test_send_email_envelope(self):
        self.write_run()
        r = self.report()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(alert.send_email(r, dict(FAKE_ENV)))
        self.assertEqual(len(FakeSMTP.instances), 1)
        smtp = FakeSMTP.instances[0]
        self.assertEqual((smtp.host, smtp.port, smtp.timeout), ("smtp.gmail.com", 465, 30))
        self.assertTrue(smtp.context.verify_mode == ssl.CERT_REQUIRED and smtp.context.check_hostname)
        self.assertEqual(smtp.logged_in, ("sender.fake@example.test", "abcdefghijklmnop"))
        msg, from_addr, to_addrs = smtp.sent[0]
        self.assertEqual(from_addr, "sender.fake@example.test")
        self.assertEqual(to_addrs, ["a.fake@example.test", "b.fake@example.test", "C.fake@example.test"])
        self.assertEqual(len(smtp.sent), 1, "ONE message per run")

    def test_partial_refusal_is_reported_without_addresses(self):
        FakeSMTP.behavior = {"refused": {"b.fake@example.test": (550, b"no such user")}}
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = alert.send_email(alert.build_error_report(time.time(), "git-push"), dict(FAKE_ENV))
        self.assertTrue(ok)
        self.assertIn("2 of 3 recipient(s); 1 refused", buf.getvalue())
        self.assertNotIn("fake@example.test", buf.getvalue())

    def test_telegram_payload_and_message_splitting(self):
        self.assertTrue(alert.send_telegram("hello", dict(FAKE_ENV)))
        self.assertEqual(self.tg_calls[0], (f"https://api.telegram.org/bot{FAKE_ENV['TELEGRAM_BOT_TOKEN']}/sendMessage",
                                            {"chat_id": FAKE_ENV["TELEGRAM_CHAT_ID"], "text": "hello"}))
        self.tg_calls.clear()
        alert.send_telegram("\n\n".join(f"block {i} " + "x" * 300 for i in range(40)), dict(FAKE_ENV))
        self.assertGreater(len(self.tg_calls), 1)
        self.assertTrue(all(len(c[1]["text"]) <= 4000 for c in self.tg_calls))

    def test_an_explicitly_empty_env_never_falls_back_to_the_real_env_file(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(alert.send_telegram("hi", {}))
            self.assertFalse(alert.send_email(alert.build_error_report(time.time(), "git-push"), {}))
        self.assertEqual((self.tg_calls, FakeSMTP.instances), ([], []))

    def test_env_loader_tolerates_bom_crlf_quotes_and_comments(self):
        path = self.tmp / "e.env"
        path.write_bytes("﻿# comment\r\nTELEGRAM_BOT_TOKEN=tok123\r\nGMAIL_SENDER=\"me@x.test\"\r\nGMAIL_APP_PASSWORD='ab cd'\r\n\r\n".encode("utf-8"))
        env = REAL_LOAD_ENV(path)
        self.assertEqual(env, {"TELEGRAM_BOT_TOKEN": "tok123", "GMAIL_SENDER": "me@x.test", "GMAIL_APP_PASSWORD": "ab cd"})
        self.assertEqual(REAL_LOAD_ENV(self.tmp / "missing.env"), {})

    def test_no_real_credential_is_hardcoded_anywhere_in_the_repo(self):
        real_env = REPO / ".env"
        if not real_env.exists():
            self.skipTest("no local .env to compare against")
        secrets = []
        for key, value in REAL_LOAD_ENV(real_env).items():
            secrets += [p.strip() for p in (value.split(",") if key == "GMAIL_RECIPIENTS" else [value]) if len(p.strip()) >= 4]
        for path in [REPO / "telegram_alert.py", REPO / "scraper" / "scrape.py", REPO / "run_daily.sh", REPO / "docs" / "index.html",
                     REPO / "docs" / "data" / "crawler_status.json", *sorted((REPO / "tests").glob("*.py"))]:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                self.assertFalse([s for s in secrets if s in text], f"a credential appears in {path.name}")


if __name__ == "__main__":
    unittest.main()
